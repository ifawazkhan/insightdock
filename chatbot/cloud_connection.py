"""Per-session, public MySQL connections. No local secrets or shared tenant state."""

from dataclasses import dataclass, field, replace
import ipaddress
import re
import socket
import ssl
from threading import BoundedSemaphore, Lock

import pymysql
from pymysql.constants import CLIENT
from pymysql.cursors import SSCursor
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from chatbot.config import Settings, identifier
from chatbot.db import Database, DatabaseError


# Process-wide bound, shared by all per-session engines; no credentials are cached.
MAX_ACTIVE_CONNECTIONS = 10
_CONNECTION_SLOTS = BoundedSemaphore(MAX_ACTIVE_CONNECTIONS)


@dataclass(frozen=True)
class ConnectionRequest:
    host: str
    database: str
    username: str
    password: str = field(repr=False)
    table: str | None = None

    def __post_init__(self):
        for value in (self.database, self.username):
            identifier(value)
            if len(value) > 64:
                raise ValueError("Connection identifiers must not exceed 64 characters.")
        if self.username.lower() == "root":
            raise ValueError("Use a dedicated read-only MySQL account, not root.")
        if self.table is not None:
            identifier(self.table)
            if len(self.table) > 64:
                raise ValueError("Connection identifiers must not exceed 64 characters.")
        # DNS only: no URLs, ports, IP literals, socket paths or scoped IPv6.
        if (len(self.host) > 253 or "." not in self.host or
                not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                        for label in self.host.split("."))):
            raise ValueError("Supply a public DNS hostname without a port.")
        try:
            ipaddress.ip_address(self.host)
        except ValueError:
            pass
        else:
            raise ValueError("Supply a public DNS hostname, not an IP address.")


def _resolve_public_host(host: str) -> str:
    """Validate *every* answer once, then pin a numeric address for this engine."""
    try:
        answers = socket.getaddrinfo(host, 3306, type=socket.SOCK_STREAM,
                                     proto=socket.IPPROTO_TCP)
        addresses = []
        for family, kind, protocol, _, address in answers:
            ip = ipaddress.ip_address(address[0])
            if (family not in {socket.AF_INET, socket.AF_INET6} or
                    kind != socket.SOCK_STREAM or protocol != socket.IPPROTO_TCP or
                    address[1] != 3306 or not ip.is_global or
                    ip.is_multicast or ip.is_reserved or
                    (isinstance(ip, ipaddress.IPv6Address) and
                     (ip.ipv4_mapped is not None or ip.sixtofour is not None or
                      ip.teredo is not None or ip in ipaddress.ip_network("64:ff9b::/96") or
                      ip in ipaddress.ip_network("64:ff9b:1::/48")))):
                raise ValueError
            addresses.append(str(ip))
        if not addresses:
            raise ValueError
        return addresses[0]
    except Exception:
        raise DatabaseError("Host must resolve exclusively to public MySQL addresses.") from None


class _TLSConnection(pymysql.connections.Connection):
    def _force_close(self):
        # A file-wrapper close failure must not strand its underlying socket.
        try:
            if self._rfile is not None:
                self._rfile.close()
        finally:
            try:
                if self._sock is not None:
                    self._sock.close()
            finally:
                self._sock = None
                self._rfile = None

    def connect(self, sock=None):
        if sock is None:
            raise DatabaseError("Cloud reconnects require a newly pinned socket.")
        return super().connect(sock=sock)

    def _request_authentication(self):
        # Reject downgrade *before* sending authentication, including on older
        # PyMySQL releases that otherwise fall back to cleartext authentication.
        if not self.server_capabilities & CLIENT.SSL:
            raise DatabaseError("The MySQL server must support verified TLS.")
        return super()._request_authentication()


def create_cloud_database(settings: Settings, request: ConnectionRequest) -> Database:
    """Build a lazy, isolated NullPool engine; DNS validation is the only I/O here.

    The optional UI-owned ``database.action_guard = callback`` is called before
    each discovery/query. A raised exception prevents any connection being opened.
    """
    pinned_ip = _resolve_public_host(request.host)
    try:
        context = ssl.create_default_context()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
    except Exception:
        raise DatabaseError("Cannot initialize verified TLS.") from None
    session = replace(settings, host=request.host, port=3306, user=request.username,
                      password=request.password, database=request.database,
                      # Validators require an identifier even in schema mode.
                      # Only discovered tables confer access, never this sentinel.
                      table=request.table if request.table is not None else "_cloud_scope",
                      cloud_mode=True, allow_mutations=False,
                      schema_scope=request.table is None)
    timeout = min(max(session.timeout_seconds, 1), 30)

    def creator():
        if not _CONNECTION_SLOTS.acquire(blocking=False):
            raise DatabaseError("Cloud connection capacity reached; try again later.")
        connection = None
        sock = None
        released = False
        release_lock = Lock()

        def release():
            nonlocal released
            with release_lock:
                if not released:
                    released = True
                    _CONNECTION_SLOTS.release()

        try:
            connection = _TLSConnection(
                host=request.host, port=3306, user=request.username,
                password=request.password, database=request.database,
                ssl=context, defer_connect=True, charset="utf8mb4",
                cursorclass=SSCursor, connect_timeout=timeout,
                read_timeout=timeout, write_timeout=timeout, local_infile=False,
            )
            original_close = connection.close

            def close():
                try:
                    try:
                        original_close()
                    finally:
                        connection._force_close()
                except Exception:
                    raise DatabaseError("Cloud connection could not close cleanly.") from None
                finally:
                    release()

            connection.close = close
            # Never reconnect with the DNS hostname. PyMySQL retains it solely
            # for SNI/certificate verification when upgrading this pinned socket.
            sock = socket.create_connection((pinned_ip, 3306), timeout=timeout)
            connection.connect(sock=sock)
            if not isinstance(connection._sock, ssl.SSLSocket):
                raise DatabaseError("The MySQL server must support verified TLS.")
            return connection
        except BaseException as exc:
            try:
                if connection is not None:
                    connection.close()
            except Exception:
                pass
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
                release()
            if not isinstance(exc, Exception):
                raise
            raise DatabaseError("Cannot connect to cloud MySQL using verified TLS.") from None

    engine = None
    try:
        engine = create_engine("mysql+pymysql://", creator=creator,
                               poolclass=NullPool, hide_parameters=True)
        return Database(session, engine=engine)
    except Exception:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass
        raise DatabaseError("Cannot initialize the cloud database connection.") from None