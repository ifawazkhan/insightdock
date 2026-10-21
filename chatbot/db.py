"""Managed database access, live schema discovery, and bounded result fetching."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from time import monotonic
from typing import Callable

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine, URL
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from pymysql.cursors import SSCursor

from chatbot.config import Settings, identifier
from chatbot.sql_policy import PolicyError, ValidatedSQL, validate_sql

CLOUD_MAX_TABLES = 50
CLOUD_MAX_COLUMNS = 500
CLOUD_MAX_METADATA_BYTES = 128 * 1024
CLOUD_MAX_RESULT_BYTES = 1024 * 1024
CLOUD_MAX_CELL_BYTES = 64 * 1024
CLOUD_MAX_ROWS = 500


class DatabaseError(RuntimeError):
    """A sanitized database error safe to display without connection details."""


@dataclass(frozen=True)
class Schema:
    database: str
    table: str
    columns: tuple[dict, ...]
    primary_key: tuple[str, ...]
    indexes: tuple[dict, ...]
    version: str
    select_timeout_supported: bool = False
    tables: tuple[dict, ...] | None = None

    @property
    def table_names(self) -> tuple[str, ...]:
        return tuple(t["name"] for t in self.tables) if self.tables is not None else (self.table,)

    @property
    def exists(self) -> bool:
        return bool(self.tables) if self.tables is not None else bool(self.columns)

    @property
    def description(self) -> str:
        if self.tables is not None:
            return json.dumps({"database": self.database, "tables": self.tables}, default=str)
        return json.dumps({"database": self.database, "table": self.table,
                           "columns": self.columns, "primary_key": self.primary_key,
                           "indexes": self.indexes}, default=str)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.description.encode()).hexdigest()


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list]
    affected_rows: int
    truncated: bool
    elapsed_seconds: float

    @property
    def status(self) -> str:
        if self.columns:
            note = " (truncated; additional rows exist)" if self.truncated else ""
            return f"Returned {len(self.rows)} rows{note} in {self.elapsed_seconds:.2f}s."
        return f"Statement completed; MySQL reported {self.affected_rows} affected rows in {self.elapsed_seconds:.2f}s."


def normalize(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)  # Avoid losing monetary precision.
    if isinstance(value, bytes):
        return value.hex()
    return value


class Database:
    def __init__(self, settings: Settings, engine: Engine | None = None):
        self.settings = settings
        self.action_guard: Callable[[], None] | None = None
        if settings.cloud_mode and engine is None:
            raise DatabaseError("Cloud mode requires a verified cloud connection factory.")
        self.engine = engine if engine is not None else create_engine(
            URL.create("mysql+pymysql", username=settings.user, password=settings.password,
                       host=settings.host, port=settings.port, database=settings.database,
                       query={"charset": "utf8mb4"}),
            pool_pre_ping=True, pool_recycle=300,
            connect_args={"connect_timeout": settings.timeout_seconds,
                          "read_timeout": settings.timeout_seconds,
                          "write_timeout": settings.timeout_seconds,
                          "cursorclass": SSCursor},
            hide_parameters=True,
        )

    def list_databases(self) -> list[str]:
        """List schemas exposed by this account, without inspecting their data."""
        if self.settings.cloud_mode:
            raise DatabaseError("Cloud sessions are restricted to their selected database.")
        try:
            with self.engine.connect() as connection:
                return sorted(str(row[0]) for row in connection.exec_driver_sql("SHOW DATABASES"))
        except SQLAlchemyError as exc:
            raise DatabaseError("Cannot list schemas. Check your account's metadata privileges.") from exc

    def discover_schema(self) -> Schema:
        if self.action_guard is not None:
            self.action_guard()
        if self.settings.cloud_mode:
            return self._cloud_discover_schema()
        try:
            with self.engine.connect() as connection:
                version = str(connection.exec_driver_sql("SELECT VERSION()").scalar_one())
                supports_timeout = False
                if "mariadb" not in version.lower():
                    try:
                        supports_timeout = bool(connection.exec_driver_sql(
                            "SHOW SESSION VARIABLES LIKE 'max_execution_time'",
                            execution_options={"no_parameters": True},
                        ).fetchone())
                    except DBAPIError as exc:
                        if exc.connection_invalidated:
                            raise
                        # Older MySQL-compatible servers may not implement this metadata form.
                        supports_timeout = False
                inspector = inspect(connection)
                if self.settings.schema_scope:
                    tables = []
                    # Base tables only: views can hide cross-schema reads and side effects.
                    for name in sorted(inspector.get_table_names()):
                        columns = tuple({"name": c["name"], "type": str(c["type"]),
                                         "nullable": c["nullable"]}
                                        for c in inspector.get_columns(name))
                        tables.append({"name": name, "columns": columns,
                            "primary_key": inspector.get_pk_constraint(name).get("constrained_columns") or [],
                            "indexes": tuple({"name": i["name"], "columns": i["column_names"],
                                              "unique": bool(i["unique"])} for i in inspector.get_indexes(name)),
                            "foreign_keys": inspector.get_foreign_keys(name)})
                    return Schema(self.settings.database, self.settings.table, (), (), (),
                                  version, supports_timeout, tuple(tables))
                if not inspector.has_table(self.settings.table):
                    return Schema(self.settings.database, self.settings.table, (), (), (), version, supports_timeout)
                columns = tuple({"name": c["name"], "type": str(c["type"]),
                                 "nullable": c["nullable"], "default": str(c.get("default"))}
                                for c in inspector.get_columns(self.settings.table))
                primary_key = tuple(inspector.get_pk_constraint(self.settings.table).get("constrained_columns") or [])
                indexes = tuple({"name": i["name"], "columns": i["column_names"],
                                 "unique": bool(i["unique"])}
                                for i in inspector.get_indexes(self.settings.table))
                return Schema(self.settings.database, self.settings.table, columns, primary_key, indexes, version, supports_timeout)
        except SQLAlchemyError as exc:
            raise DatabaseError("Cannot inspect MySQL. Check connection settings, database name and table privileges.") from exc

    def execute(self, query: ValidatedSQL, schema: Schema) -> QueryResult:
        if self.action_guard is not None:
            self.action_guard()
        if self.settings.cloud_mode:
            return self._cloud_execute(query, schema)
        start = monotonic()
        try:
            with self.engine.connect() as connection:
                # Probe capabilities rather than trusting version strings from compatible servers.
                if query.read_only and schema.select_timeout_supported:
                    connection.exec_driver_sql(f"SET SESSION MAX_EXECUTION_TIME = {self.settings.select_timeout_ms}")
                # Driver placeholders are not involved; SQL is an already-validated complete statement.
                result = connection.execution_options(no_parameters=True).exec_driver_sql(query.sql)
                try:
                    columns = list(result.keys()) if result.returns_rows else []
                    fetched = result.fetchmany(self.settings.max_rows + 1) if columns else []
                    rows = [[normalize(v) for v in row] for row in fetched[:self.settings.max_rows]]
                    affected = max(0, result.rowcount)
                finally:
                    result.close()
                if not query.read_only:
                    connection.commit()
                return QueryResult(columns, rows, affected, len(fetched) > self.settings.max_rows,
                                   monotonic() - start)
        except DBAPIError as exc:
            code = exc.orig.args[0] if getattr(exc.orig, "args", ()) else None
            if not query.read_only and (exc.connection_invalidated or code in {2006, 2013, 2055}):
                raise DatabaseError("Connection lost during a change: outcome is UNKNOWN. Verify in MySQL before retrying.") from exc
            messages = {1064: "MySQL rejected this SQL syntax.", 1054: "Unknown column; refresh the schema.",
                        1146: "A requested table does not exist; refresh the schema.",
                        1062: "Duplicate key; no successful change was reported.",
                        1044: "Database access denied.", 1142: "Your MySQL account lacks this privilege.",
                        3024: "SELECT exceeded its execution time limit.",
                        1205: "MySQL lock wait timed out.", 1213: "Transaction deadlock; verify before retrying."}
            raise DatabaseError(messages.get(code, "MySQL could not complete the statement. Check syntax, privileges and constraints.")) from exc
        except SQLAlchemyError as exc:
            raise DatabaseError("Database operation failed; verify any change before retrying.") from exc

    def _cloud_deadline(self) -> float:
        return monotonic() + min(max(self.settings.timeout_seconds, 1), 30)

    @staticmethod
    def _cloud_json_size(value: object) -> int:
        return len(json.dumps(value, default=str, ensure_ascii=True,
                              separators=(",", ":")).encode("utf-8"))

    def _cloud_read(self, connection, sql: str, *, limit: int, deadline: float,
                    budget: list[int], parameters=None, timed: bool = True):
        """Stream bounded rows; invalidate before closing an unfinished cursor.

        SSCursor.close normally drains the remaining response. On *any* early
        stop, discard the socket first rather than consuming unbounded data.
        """
        result = None
        finished = False
        try:
            if monotonic() >= deadline:
                raise DatabaseError("Cloud database action exceeded its time limit.")
            if timed:
                timeout_ms = min(max(self.settings.select_timeout_ms, 100), 10000)
                setting = connection.exec_driver_sql(f"SET SESSION MAX_EXECUTION_TIME = {timeout_ms}")
                setting.close()
            result = connection.exec_driver_sql(sql, parameters) if parameters is not None else connection.exec_driver_sql(
                sql, execution_options={"no_parameters": True})
            columns = [str(key) for key in result.keys()]
            if len(columns) > CLOUD_MAX_COLUMNS:
                raise DatabaseError("Cloud result exceeds the column limit.")
            for key in columns:
                if self._cloud_json_size(key) > CLOUD_MAX_CELL_BYTES:
                    raise DatabaseError("Cloud result exceeds the cell size limit.")
            budget[0] -= self._cloud_json_size(columns) + 2
            if budget[0] < 0:
                raise DatabaseError("Cloud data exceeds the byte limit.")
            rows = []
            while True:
                if monotonic() >= deadline:
                    raise DatabaseError("Cloud database action exceeded its time limit.")
                row = result.fetchone()
                if monotonic() >= deadline:
                    raise DatabaseError("Cloud database action exceeded its time limit.")
                if row is None:
                    finished = True
                    break
                if len(rows) >= limit:
                    raise DatabaseError("Cloud metadata exceeds its row limit.")
                values = []
                for value in row:
                    # Bound before conversion too (e.g. bytes.hex doubles size).
                    if isinstance(value, (str, bytes)) and len(value) > CLOUD_MAX_CELL_BYTES:
                        raise DatabaseError("Cloud result exceeds the cell size limit.")
                    value = normalize(value)
                    if self._cloud_json_size(value) > CLOUD_MAX_CELL_BYTES:
                        raise DatabaseError("Cloud result exceeds the cell size limit.")
                    values.append(value)
                budget[0] -= self._cloud_json_size(values) + 1
                if budget[0] < 0:
                    raise DatabaseError("Cloud data exceeds the byte limit.")
                rows.append(values)
            if budget[0] < 0:
                raise DatabaseError("Cloud data exceeds the byte limit.")
            return columns, rows
        finally:
            try:
                if not finished:
                    connection.invalidate()
            finally:
                if result is not None:
                    try:
                        result.close()
                    except Exception:
                        if finished:
                            raise DatabaseError("Cannot close the cloud result.") from None

    def _cloud_prepare(self, connection, deadline: float) -> str:
        _, capabilities = self._cloud_read(
            connection,
            "SHOW SESSION VARIABLES WHERE Variable_name IN ('version', 'max_execution_time')",
            limit=2, deadline=deadline, budget=[4096], timed=False,
        )
        values = {str(row[0]).lower(): str(row[1]) for row in capabilities}
        version = values.get("version", "")
        if not version or "mariadb" in version.lower() or "max_execution_time" not in values:
            raise DatabaseError("Cloud mode requires MySQL with MAX_EXECUTION_TIME support; MariaDB is unsupported.")
        # Defense in depth, independent of Settings.allow_mutations and grants.
        result = connection.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
        result.close()
        return version

    def _cloud_discover_schema(self) -> Schema:
        deadline = self._cloud_deadline()
        budget = [CLOUD_MAX_METADATA_BYTES]
        try:
            with self.engine.connect() as connection:
                version = self._cloud_prepare(connection, deadline)
                params = (self.settings.database,)
                table_filter = ""
                if not self.settings.schema_scope:
                    identifier(self.settings.table)
                    table_filter = " AND TABLE_NAME = %s"
                    params += (self.settings.table,)
                _, names = self._cloud_read(connection,
                    "SELECT TABLE_NAME FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'" + table_filter +
                    " ORDER BY TABLE_NAME LIMIT 51", parameters=params,
                    limit=CLOUD_MAX_TABLES, deadline=deadline, budget=budget)
                tables = []
                total_columns = 0
                for (name,) in names:
                    identifier(name)
                    _, rows = self._cloud_read(connection,
                        "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY "
                        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                        "ORDER BY ORDINAL_POSITION LIMIT 501", parameters=(self.settings.database, name),
                        limit=CLOUD_MAX_COLUMNS - total_columns, deadline=deadline, budget=budget)
                    total_columns += len(rows)
                    columns = tuple({"name": identifier(row[0]), "type": row[1],
                                     "nullable": row[2] == "YES"} for row in rows)
                    # Do not expose raw defaults/comments, which may contain secrets.
                    tables.append({"name": name, "columns": columns,
                                   "primary_key": [row[0] for row in rows if row[3] == "PRI"],
                                   "indexes": (), "foreign_keys": []})
                if self.settings.schema_scope:
                    schema = Schema(self.settings.database, self.settings.table, (), (), (),
                                    version, True, tuple(tables))
                else:
                    table = tables[0] if tables else {"columns": (), "primary_key": (), "indexes": ()}
                    schema = Schema(self.settings.database, self.settings.table, table["columns"],
                                    tuple(table["primary_key"]), table["indexes"], version, True)
                if len(schema.description.encode("utf-8")) > CLOUD_MAX_METADATA_BYTES:
                    raise DatabaseError("Cloud schema exceeds the metadata byte limit.")
                return schema
        except DatabaseError:
            raise
        except Exception:
            raise DatabaseError("Cannot inspect cloud MySQL within the supported limits.") from None

    def _cloud_execute(self, query: ValidatedSQL, schema: Schema) -> QueryResult:
        start = monotonic()
        try:
            if (not query.read_only or query.operation not in {"SELECT", "UNION"} or
                    not schema.select_timeout_supported or schema.database != self.settings.database):
                raise DatabaseError("Cloud mode permits only bounded SELECT/UNION queries with server timeouts.")
            # Do not trust a caller-constructed ValidatedSQL or flags alone.
            checked = validate_sql(query.sql, self.settings.database,
                                   self.settings.table or (schema.table_names[0] if schema.table_names else "_cloud"),
                                   allowed_tables=schema.table_names)
            if not checked.read_only or checked.operation not in {"SELECT", "UNION"}:
                raise DatabaseError("Cloud mode permits only SELECT/UNION queries.")
            deadline = self._cloud_deadline()
            row_limit = min(max(self.settings.max_rows, 1), CLOUD_MAX_ROWS)
            # Bound the response at the server too, avoiding unbounded cursor drain
            # when a query would otherwise return millions of rows.
            sql = f"SELECT * FROM ({checked.sql}) AS _cloud_result LIMIT {row_limit + 1}"
            with self.engine.connect() as connection:
                self._cloud_prepare(connection, deadline)
                columns, rows = self._cloud_read(connection, sql, limit=row_limit + 1,
                    deadline=deadline, budget=[CLOUD_MAX_RESULT_BYTES])
                return QueryResult(columns, rows[:row_limit], 0, len(rows) > row_limit,
                                   monotonic() - start)
        except DatabaseError:
            raise
        except PolicyError:
            raise DatabaseError("Cloud query violates the read-only SQL policy.") from None
        except Exception:
            raise DatabaseError("Cloud SELECT failed or exceeded its time/resource limits.") from None