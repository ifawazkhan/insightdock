"""In-memory PDF delivery and private, profile-name-based recipient preferences.

Successful delivery means SMTP acceptance, not delivery to an inbox. Ambiguous
send failures must not be retried automatically. No report or SMTP secret is saved.
"""

from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from email.message import EmailMessage
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import smtplib
import ssl
import stat
import threading

from dotenv import load_dotenv

from chatbot.config import ROOT

MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_STORE_BYTES = 64 * 1024
MAX_PROFILES = 100
SMTP_TIMEOUT = 30
_STORE_LOCK = threading.RLock()
_LOCAL = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+\Z")
_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


class EmailError(Exception):
    """Safe, user-facing validation, storage, or definite SMTP failure."""


class DeliveryUncertain(EmailError):
    """The connection failed during sending; acceptance cannot be determined."""


def normalize_email(value: str) -> str:
    """Accept one ASCII dot-atom mailbox, without display names or address lists."""
    if not isinstance(value, str) or not value.isascii() or any(c in value for c in "\r\n"):
        raise EmailError("Enter a single valid ASCII email address.")
    value = value.strip()
    if len(value) > 254 or value.count("@") != 1:
        raise EmailError("Enter a single valid ASCII email address.")
    local, domain = value.split("@")
    labels = domain.split(".")
    if (not 1 <= len(local) <= 64 or not _LOCAL.fullmatch(local)
            or local.startswith(".") or local.endswith(".") or ".." in local
            or len(labels) < 2 or any(not _LABEL.fullmatch(label) for label in labels)):
        raise EmailError("Enter a single valid ASCII email address.")
    return local + "@" + domain.lower()


@dataclass(frozen=True)
class EmailSettings:
    host: str = ""
    port: int = 587
    user: str = ""
    password: str = field(default="", repr=False)
    from_address: str = ""
    tls_mode: str = "starttls"

    @classmethod
    def from_env(cls) -> "EmailSettings":
        """Load the project's local .env without overriding existing variables."""
        load_dotenv(ROOT / ".env")
        try:
            settings = cls(
                host=os.getenv("CHATBOT_SMTP_HOST", "").strip(),
                port=int(os.getenv("CHATBOT_SMTP_PORT", "587")),
                user=os.getenv("CHATBOT_SMTP_USER", ""),
                password=os.getenv("CHATBOT_SMTP_PASSWORD", ""),
                from_address=os.getenv("CHATBOT_SMTP_FROM", "").strip(),
                tls_mode=os.getenv("CHATBOT_SMTP_TLS_MODE", "starttls").strip().lower(),
            )
            settings._validate()
            return settings
        except (ValueError, TypeError, EmailError):
            raise EmailError("Invalid SMTP configuration.") from None

    def _validate(self) -> None:
        if (not isinstance(self.port, int) or isinstance(self.port, bool)
                or not 1 <= self.port <= 65535 or self.tls_mode not in {"starttls", "ssl"}
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in self.host)
                or bool(self.user) != bool(self.password)):
            raise EmailError("Invalid SMTP configuration.")
        if self.from_address:
            normalize_email(self.from_address)

    @property
    def configured(self) -> bool:
        try:
            self._validate()
            return bool(self.host and self.from_address)
        except (EmailError, TypeError, AttributeError):
            return False


def send_report_pdf(data: bytes, title: str, recipient: str, settings: EmailSettings) -> None:
    """Send once over verified TLS; None means accepted by SMTP, not inbox delivery."""
    if not isinstance(data, bytes) or not data.startswith(b"%PDF") or len(data) > MAX_PDF_BYTES:
        raise EmailError("A PDF of at most 10 MiB is required.")
    recipient = normalize_email(recipient)
    if not settings.configured:
        raise EmailError("SMTP email is not configured.")
    sender = normalize_email(settings.from_address)
    if not isinstance(title, str):
        raise EmailError("Invalid report title.")
    # Bound input before header encoding and never include question/SQL metadata.
    subject = " ".join("".join(c if c.isprintable() else " " for c in title[:1000]).split())[:160]
    filename = re.sub(r"[^A-Za-z0-9_-]+", "-", subject).strip("-_")[:80] or "report"
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject or "Report PDF"
    message.set_content("Your requested PDF report is attached.")
    message.add_attachment(data, maintype="application", subtype="pdf", filename=filename + ".pdf")
    client = None
    sending = False
    try:
        context = ssl.create_default_context()
        if settings.tls_mode == "ssl":
            client = smtplib.SMTP_SSL(settings.host, settings.port, timeout=SMTP_TIMEOUT, context=context)
            client.ehlo()
        else:
            client = smtplib.SMTP(settings.host, settings.port, timeout=SMTP_TIMEOUT)
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        if settings.user:
            client.login(settings.user, settings.password)
        sending = True
        refused = client.send_message(message, from_addr=sender, to_addrs=[recipient])
        sending = False
        if refused:
            raise EmailError("The SMTP server refused the email.")
    except smtplib.SMTPResponseException:
        raise EmailError("The SMTP server rejected the email or authentication.") from None
    except smtplib.SMTPRecipientsRefused:
        raise EmailError("The SMTP server refused the recipient.") from None
    except (smtplib.SMTPServerDisconnected, OSError):
        if sending:
            raise DeliveryUncertain("Email delivery is uncertain. Do not resend automatically.") from None
        raise EmailError("Unable to connect or authenticate to the SMTP server.") from None
    except EmailError:
        raise
    except Exception:
        raise EmailError("Unable to send the report email.") from None
    finally:
        if client is not None:
            with suppress(Exception):
                client.quit()
            with suppress(Exception):
                client.close()


def _profile_key(profile_name: str) -> str:
    if not isinstance(profile_name, str) or not profile_name.strip():
        raise EmailError("A profile name is required.")
    return hashlib.sha256(profile_name.strip().casefold().encode("utf-8")).hexdigest()


class RecipientStore:
    """Small JSON preference store; all instances share an in-process lock.

    Only hashed normalized profile names and validated mailboxes are persisted.
    Directory-relative no-follow operations prevent symlink traversal, including
    symlink ancestors. Atomic replacement prevents partial writes, not cross-process
    lost updates; callers in separate processes need external coordination.
    """

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else ROOT / ".local" / "recipient_emails.json"

    @contextmanager
    def _directory(self, create: bool):
        path = self.path.absolute()
        fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in path.parent.parts[1:]:
                if part in {".", ".."}:
                    raise EmailError("Unsafe recipient store path.")
                try:
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        yield None
                        return
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            if create:
                os.fchmod(fd, 0o700)
            yield fd
        finally:
            os.close(fd)

    def _load(self, directory: int | None) -> dict[str, str]:
        if directory is None:
            return {}
        try:
            fd = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STORE_BYTES:
                raise EmailError("Invalid recipient store.")
            raw = stream.read(MAX_STORE_BYTES + 1)
        if len(raw) > MAX_STORE_BYTES:
            raise EmailError("Recipient store size limit exceeded.")
        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise EmailError("Invalid recipient store.")
                result[key] = value
            return result
        values = json.loads(raw, object_pairs_hook=unique_pairs)
        if not isinstance(values, dict) or len(values) > MAX_PROFILES:
            raise EmailError("Invalid recipient store.")
        for key, value in values.items():
            if not re.fullmatch(r"[0-9a-f]{64}", key) or normalize_email(value) != value:
                raise EmailError("Invalid recipient store.")
        return values

    def read(self, profile_name: str) -> str:
        key = _profile_key(profile_name)
        try:
            with _STORE_LOCK, self._directory(create=False) as directory:
                return self._load(directory).get(key, "")
        except Exception:
            raise EmailError("Unable to read saved recipient preferences.") from None

    def save(self, profile_name: str, email: str) -> None:
        key = _profile_key(profile_name)
        if not isinstance(email, str) or "\r" in email or "\n" in email:
            raise EmailError("Enter a single valid ASCII email address.")
        address = normalize_email(email) if email.strip() else ""
        try:
            with _STORE_LOCK, self._directory(create=True) as directory:
                values = self._load(directory)
                if address:
                    values[key] = address
                else:
                    values.pop(key, None)
                if len(values) > MAX_PROFILES:
                    raise EmailError("Recipient profile limit exceeded.")
                data = json.dumps(values, ensure_ascii=True, sort_keys=True).encode("ascii")
                if len(data) > MAX_STORE_BYTES:
                    raise EmailError("Recipient store size limit exceeded.")
                temporary = ".recipients-" + secrets.token_hex(16) + ".tmp"
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        os.fchmod(stream.fileno(), 0o600)
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    # Refuse a destination changed to a symlink while writing.
                    self._load(directory)
                    os.replace(temporary, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
                    os.fsync(directory)
                finally:
                    with suppress(FileNotFoundError):
                        os.unlink(temporary, dir_fd=directory)
        except Exception:
            raise EmailError("Unable to save recipient preferences.") from None