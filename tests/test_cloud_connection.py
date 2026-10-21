"""Cloud-core contracts: all network, driver and engine I/O is mocked."""

from dataclasses import replace
import json
import socket
import ssl
import traceback
from unittest.mock import MagicMock, Mock

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from chatbot import cloud_connection as cloud
from chatbot.config import Settings
from chatbot.db import (
    CLOUD_MAX_CELL_BYTES, CLOUD_MAX_METADATA_BYTES, CLOUD_MAX_RESULT_BYTES,
    Database, DatabaseError, Schema,
)
from chatbot.llm import ChatModel, ModelError
from chatbot.report_automation import automatic_queries
from chatbot.report_service import ReportService
from chatbot.service import ChatService
from chatbot.sql_policy import PolicyError, ValidatedSQL, validate_sql


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    monkeypatch.setattr("chatbot.config.load_dotenv", Mock(side_effect=AssertionError("No local secrets")))
    monkeypatch.setattr(socket, "getaddrinfo", Mock(side_effect=AssertionError("No live DNS")))
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=AssertionError("No live sockets")))


@pytest.fixture
def settings():
    return Settings("", 3306, "", "", "tenant", "items", cloud_mode=True,
                    schema_scope=True, max_rows=2)


@pytest.fixture
def connection_request():
    return cloud.ConnectionRequest("db.example.com", "tenant", "reader", "private-password")


def answer(ip):
    return (socket.AF_INET6 if ":" in ip else socket.AF_INET,
            socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 3306))


def result(columns, rows):
    response = Mock()
    response.keys.return_value = columns
    response.fetchone.side_effect = [*rows, None]
    return response


def mock_database(settings, *, tables=None, metadata=None, query_rows=None,
                  query_columns=None, capabilities=None):
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    queries = []

    def execute(sql, *args, **kwargs):
        queries.append(sql)
        if sql.startswith("SHOW SESSION VARIABLES"):
            return result(["Variable_name", "Value"], capabilities if capabilities is not None else [
                ["version", "8.0.40"], ["max_execution_time", "0"]])
        if "FROM information_schema.TABLES" in sql:
            return result(["TABLE_NAME"], [[name] for name in (tables if tables is not None else ["items"])])
        if "FROM information_schema.COLUMNS" in sql:
            return result(["COLUMN_NAME", "COLUMN_TYPE", "IS_NULLABLE", "COLUMN_KEY"],
                          metadata if metadata is not None else [["id", "int", "NO", "PRI"]])
        if sql.startswith("SELECT * FROM ("):
            return result(query_columns if query_columns is not None else ["id"],
                          query_rows if query_rows is not None else [[1], [2], [3]])
        return Mock()

    connection.exec_driver_sql.side_effect = execute
    return Database(settings, engine), connection, queries


def schema():
    return Schema("tenant", "items", ({"name": "id"},), (), (), "8.0.40", True)


def select(sql="SELECT * FROM items"):
    return validate_sql(sql, "tenant", "items")


def test_cloud_environment_skips_dotenv_and_mysql(monkeypatch):
    monkeypatch.setenv("CHATBOT_MODE", "cloud")
    # These must not even be parsed in cloud mode.
    for key, value in {"MYSQL_HOST": "localhost", "MYSQL_PORT": "not-a-port",
                       "MYSQL_USER": "root", "MYSQL_PASSWORD": "private-mysql",
                       "MYSQL_DATABASE": "bad/name", "CHATBOT_DATABASE": "bad/name",
                       "CHATBOT_TABLE": "bad/name", "CHATBOT_ALLOW_MUTATIONS": "invalid"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CHATBOT_AI_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "private-api")
    monkeypatch.setenv("OPENAI_MODEL", "deployment")
    monkeypatch.delenv("CHATBOT_AI_SUMMARIES", raising=False)
    loaded = Settings.from_env()
    assert loaded.cloud_mode and not loaded.allow_mutations and not loaded.ai_summaries
    assert (loaded.host, loaded.user, loaded.password, loaded.database, loaded.table) == ("", "", "", "", "")
    assert loaded.port == 3306 and loaded.api_key == "private-api" and loaded.model == "deployment"
    assert "private" not in repr(loaded)


def test_cloud_ai_environment_azure_and_explicit_summary_opt_in(monkeypatch):
    monkeypatch.setenv("CHATBOT_MODE", "cloud")
    monkeypatch.setenv("CHATBOT_AI_PROVIDER", "azure")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-secret")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://operator.example.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "deployment")
    monkeypatch.setenv("CHATBOT_AI_SUMMARIES", "true")
    loaded = Settings.from_env()
    assert loaded.provider == "azure" and loaded.ai_summaries
    assert loaded.api_key == "azure-secret" and loaded.model == "deployment"


@pytest.mark.parametrize("mode", ["hosted", "", "production"])
def test_mode_validation_precedes_local_file_reads(monkeypatch, mode):
    monkeypatch.setenv("CHATBOT_MODE", mode)
    with pytest.raises(ValueError, match="CHATBOT_MODE"):
        Settings.from_env()


def test_local_settings_remain_backwards_compatible():
    assert not Settings("localhost", 3306, "reader", "secret", "tenant").cloud_mode


@pytest.mark.parametrize("changes", [
    {"database": "db;DROP"}, {"table": "items.name"}, {"username": "root"},
    {"username": "ROOT"}, {"username": "reader@host"}, {"database": "a" * 65},
    {"host": "localhost"}, {"host": "127.0.0.1"}, {"host": "8.8.8.8"},
    {"host": "::1"}, {"host": "https://db.example.com"},
    {"host": "db.example.com:3307"}, {"host": "db.example.com:3306"},
    {"host": "db.example.com/path"}, {"host": "db..example.com"},
    {"host": "db.example.com."}, {"host": "-db.example.com"},
])
def test_request_rejects_unsafe_inputs(connection_request, changes):
    with pytest.raises(ValueError):
        replace(connection_request, **changes)


def test_password_not_in_repr(connection_request):
    assert connection_request.password not in repr(connection_request)


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254",
    "100.64.0.1", "0.0.0.0", "192.0.2.1", "224.0.0.1", "255.255.255.255",
    "::1", "::", "fe80::1", "fc00::1", "2001:db8::1", "ff02::1",
    "::ffff:127.0.0.1", "::ffff:8.8.8.8", "64:ff9b::808:808", "2002:0808:0808::1",
])
def test_every_dns_answer_must_be_public(monkeypatch, settings, connection_request, ip):
    monkeypatch.setattr(socket, "getaddrinfo", Mock(return_value=[answer("8.8.8.8"), answer(ip)]))
    with pytest.raises(DatabaseError, match="public"):
        cloud.create_cloud_database(settings, connection_request)
    socket.create_connection.assert_not_called()


@pytest.mark.parametrize("answers", [[], [answer("8.8.8.8")[:4] + (("8.8.8.8", 3307),)]])
def test_dns_missing_or_non_mysql_port_rejected(monkeypatch, settings, connection_request, answers):
    monkeypatch.setattr(socket, "getaddrinfo", Mock(return_value=answers))
    with pytest.raises(DatabaseError):
        cloud.create_cloud_database(settings, connection_request)


@pytest.fixture
def factory(monkeypatch, settings, connection_request):
    dns = Mock(return_value=[answer("8.8.8.8"), answer("2606:4700:4700::1111")])
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    tcp = Mock()
    monkeypatch.setattr(socket, "create_connection", Mock(return_value=tcp))
    driver = Mock()
    driver._sock = MagicMock(spec=ssl.SSLSocket)
    constructor = Mock(return_value=driver)
    monkeypatch.setattr(cloud, "_TLSConnection", constructor)
    engine_factory = Mock()
    monkeypatch.setattr(cloud, "create_engine", engine_factory)
    db = cloud.create_cloud_database(settings, connection_request)
    creator = engine_factory.call_args.kwargs["creator"]
    return db, creator, dns, tcp, driver, constructor, engine_factory


def test_verified_tls_original_hostname_pinned_socket_and_nullpool(factory, connection_request):
    db, creator, dns, tcp, driver, constructor, engine_factory = factory
    assert db.engine is engine_factory.return_value
    assert engine_factory.call_args.args == ("mysql+pymysql://",)
    assert engine_factory.call_args.kwargs["poolclass"] is NullPool
    assert engine_factory.call_args.kwargs["hide_parameters"]
    assert not db.settings.allow_mutations and db.settings.cloud_mode
    assert db.settings.database == connection_request.database and db.settings.schema_scope
    socket.create_connection.assert_not_called()
    connection = creator()
    kwargs = constructor.call_args.kwargs
    assert kwargs["host"] == connection_request.host and kwargs["port"] == 3306
    assert kwargs["defer_connect"] and not kwargs["local_infile"]
    assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED and kwargs["ssl"].check_hostname
    assert kwargs["read_timeout"] <= 30 and kwargs["write_timeout"] <= 30
    socket.create_connection.assert_called_once_with(("8.8.8.8", 3306), timeout=15)
    driver.connect.assert_called_once_with(sock=tcp)
    dns.assert_called_once()
    connection.close()


def test_pinned_ip_is_not_reresolved_on_subsequent_connections(factory):
    _, creator, dns, _, driver, _, _ = factory
    first = creator()
    first.close()
    # Supply a fresh mocked DBAPI connection; an engine must never retry DNS.
    replacement = Mock()
    replacement._sock = MagicMock(spec=ssl.SSLSocket)
    factory[5].return_value = replacement
    second = creator()
    second.close()
    assert dns.call_count == 1
    assert all(call.args[0] == ("8.8.8.8", 3306) for call in socket.create_connection.call_args_list)


def test_semaphore_blocks_and_releases_on_close_errors(monkeypatch, factory):
    from threading import BoundedSemaphore
    slots = BoundedSemaphore(1)
    monkeypatch.setattr(cloud, "_CONNECTION_SLOTS", slots)
    _, creator, _, _, driver, _, _ = factory
    driver.close.side_effect = RuntimeError("private-password db.example.com")
    connection = creator()
    with pytest.raises(DatabaseError, match="capacity"):
        creator()
    with pytest.raises(DatabaseError) as caught:
        connection.close()
    assert "private-password" not in "".join(traceback.format_exception(caught.value))
    driver._force_close.assert_called_once()
    assert slots.acquire(blocking=False)
    assert not slots.acquire(blocking=False)
    slots.release()
    # Repeated closes must not over-release a bounded semaphore.
    with pytest.raises(DatabaseError):
        connection.close()


@pytest.mark.parametrize("failure", ["constructor", "socket", "connect", "plaintext"])
def test_connection_errors_clean_up_and_suppress_raw_details(monkeypatch, factory, failure):
    from threading import BoundedSemaphore
    slots = BoundedSemaphore(1)
    monkeypatch.setattr(cloud, "_CONNECTION_SLOTS", slots)
    _, creator, _, tcp, driver, constructor, _ = factory
    error = RuntimeError("private-password db.example.com")
    if failure == "constructor":
        constructor.side_effect = error
    elif failure == "socket":
        socket.create_connection.side_effect = error
    elif failure == "connect":
        driver.connect.side_effect = error
    else:
        driver._sock = tcp
    with pytest.raises(DatabaseError) as caught:
        creator()
    assert "private-password" not in "".join(traceback.format_exception(caught.value))
    assert slots.acquire(blocking=False)
    if failure in {"connect", "plaintext"}:
        tcp.close.assert_called_once()


def test_server_tls_downgrade_rejected_before_authentication(settings):
    connection = cloud._TLSConnection(host="db.example.com", user="reader", password="secret",
                                      defer_connect=True, ssl=ssl.create_default_context())
    connection.server_capabilities = 0
    with pytest.raises(DatabaseError, match="TLS"):
        connection._request_authentication()


def test_real_driver_tls_wrap_uses_original_dns_hostname(monkeypatch):
    context = ssl.create_default_context()
    wrapped = MagicMock(spec=ssl.SSLSocket)
    wrap = Mock(return_value=wrapped)
    monkeypatch.setattr(context, "wrap_socket", wrap)
    connection = cloud._TLSConnection(host="db.example.com", user="reader", password="secret",
                                      defer_connect=True, ssl=context)
    pinned_socket = Mock()
    connection._sock = pinned_socket
    connection.server_version = "8.0.40"
    connection.server_capabilities = cloud.CLIENT.SSL
    connection._auth_plugin_name = "mysql_native_password"
    connection.salt = b"01234567890123456789"
    # Stop after the SSL request and TLS wrap, before authentication is sent.
    connection.write_packet = Mock(side_effect=[None, RuntimeError("stop before auth")])
    with pytest.raises(RuntimeError, match="stop before auth"):
        connection._request_authentication()
    wrap.assert_called_once_with(pinned_socket, server_hostname="db.example.com")
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    connection.close()


def test_driver_cannot_reconnect_by_resolving_hostname():
    connection = cloud._TLSConnection(host="db.example.com", user="reader", password="secret",
                                      defer_connect=True, ssl=ssl.create_default_context())
    with pytest.raises(DatabaseError, match="pinned"):
        connection.connect()
    socket.create_connection.assert_not_called()


def test_force_close_releases_socket_even_if_file_close_fails():
    connection = cloud._TLSConnection(host="db.example.com", user="reader", password="secret",
                                      defer_connect=True, ssl=ssl.create_default_context())
    stream = Mock()
    stream.close.side_effect = RuntimeError("stream close failed")
    sock = Mock()
    connection._rfile, connection._sock = stream, sock
    with pytest.raises(RuntimeError, match="stream close failed"):
        connection._force_close()
    sock.close.assert_called_once()
    assert connection._sock is None and connection._rfile is None


def test_factory_disposes_engine_if_database_initialization_fails(monkeypatch, factory, connection_request):
    db, _, _, _, _, _, engine_factory = factory
    monkeypatch.setattr(cloud, "Database", Mock(side_effect=RuntimeError("private-password")))
    with pytest.raises(DatabaseError) as caught:
        cloud.create_cloud_database(db.settings, connection_request)
    engine_factory.return_value.dispose.assert_called_once()
    assert "private-password" not in "".join(traceback.format_exception(caught.value))


def test_factory_derives_session_settings_and_table_scope(monkeypatch, settings, connection_request):
    monkeypatch.setattr(socket, "getaddrinfo", Mock(return_value=[answer("8.8.8.8")]))
    monkeypatch.setattr(cloud, "create_engine", Mock())
    db = cloud.create_cloud_database(replace(settings, allow_mutations=True), replace(connection_request, table="items"))
    assert not db.settings.schema_scope and db.settings.table == "items"
    assert not db.settings.allow_mutations and db.settings.user == "reader"
    assert settings.user == ""  # Frozen deployment configuration was not mutated.


def test_schemawide_factory_chat_propose_execute_and_scope_enforcement(factory):
    db = factory[0]
    assert db.settings.schema_scope and db.settings.table == "_cloud_scope"
    mocked, connection, queries = mock_database(db.settings, tables=["items", "departments"])
    db.engine = mocked.engine
    discovered = db.discover_schema()
    assert discovered.table == "_cloud_scope"
    assert discovered.table_names == ("items", "departments")
    assert "_cloud_scope" not in discovered.description
    service = ChatService(db.settings, db)
    proposal = service.propose(
        "SELECT items.id FROM items JOIN departments ON items.id = departments.id", discovered)
    assert set(proposal.query.target_tables) == {"items", "departments"}
    response = service.execute(proposal)
    assert response.rows == [[1], [2]] and response.truncated
    assert service.pending is None
    connection.commit.assert_not_called()
    for sql in ("SELECT * FROM _cloud_scope", "SELECT * FROM missing",
                "SELECT * FROM other.items"):
        with pytest.raises(PolicyError):
            service.propose(sql, discovered)
        count = len(queries)
        with pytest.raises(DatabaseError):
            db.execute(ValidatedSQL(sql, "SELECT", True, False, ""), discovered)
        assert len(queries) == count


@pytest.mark.parametrize("table", ["items", "departments", "_cloud_scope", "missing", "other.items"])
def test_schemawide_factory_model_report_plan_validation(factory, monkeypatch, table):
    db = factory[0]
    mocked, _, _ = mock_database(db.settings, tables=["items", "departments"])
    db.engine = mocked.engine
    discovered = db.discover_schema()
    provider = Mock()
    monkeypatch.setattr("chatbot.llm.OpenAI", provider)
    model = ChatModel(replace(db.settings, api_key="mock-key", model="mock-model"))
    completion = MagicMock()
    completion.choices[0].message.content = json.dumps({"queries": [{
        "id": "q1", "question": "Record count", "sql": f"SELECT COUNT(*) AS record_count FROM {table}"
    }], "clarification": ""})
    monkeypatch.setattr(model, "_completion", Mock(return_value=completion))
    if table in discovered.table_names:
        plan = model.plan_report("Record count", discovered, [])
        assert len(plan) == 1
        checked = validate_sql(plan[0].sql, discovered.database, discovered.table,
                               allowed_tables=discovered.table_names)
        assert checked.target_tables == (table,)
    else:
        with pytest.raises(ModelError):
            model.plan_report("Record count", discovered, [])
    db.engine.connect.assert_called_once()  # Planning never executes SQL.


def test_schemawide_automatic_reports_use_discovered_tables_and_run_live(factory):
    db = factory[0]
    mocked, _, queries = mock_database(db.settings, tables=["items", "departments"],
                                     query_columns=["record_count"], query_rows=[[3]])
    db.engine = mocked.engine
    discovered = db.discover_schema()
    plan = automatic_queries(discovered)
    targets = {name for query in plan for name in validate_sql(
        query.sql, discovered.database, discovered.table,
        allowed_tables=discovered.table_names).target_tables}
    # Automatic reports select one eligible real table, not the schema sentinel.
    assert targets and targets <= {"items", "departments"}
    assert all("_cloud_scope" not in query.sql for query in plan)
    report = ReportService(db.settings, db).live("Counts", plan, "cloud-schema-report")
    assert report.scope == ("tenant", None)
    assert all(e.result is not None and not e.error for e in report.evidence)
    assert sum(sql.startswith("SELECT * FROM (") for sql in queries) == len(plan)


def test_cloud_requires_injected_verified_engine(settings):
    with pytest.raises(DatabaseError, match="factory"):
        Database(settings)


def test_cloud_does_not_list_other_databases(settings):
    db, connection, _ = mock_database(settings)
    with pytest.raises(DatabaseError):
        db.list_databases()
    db.engine.connect.assert_not_called()


def test_readonly_timeout_and_row_bounds_even_if_mutations_enabled(settings):
    db, connection, queries = mock_database(replace(settings, allow_mutations=True))
    response = db.execute(select(), schema())
    assert response.rows == [[1], [2]] and response.truncated and response.affected_rows == 0
    assert "SET SESSION TRANSACTION READ ONLY" in queries
    assert "SET SESSION MAX_EXECUTION_TIME = 10000" in queries
    assert queries[-1].endswith("LIMIT 3")
    connection.commit.assert_not_called()


def test_union_is_policy_read_operation(settings):
    db, _, _ = mock_database(settings)
    query = select("SELECT id FROM items UNION SELECT id FROM items")
    assert query.operation == "UNION"
    assert db.execute(query, schema()).columns == ["id"]


@pytest.mark.parametrize("query", [
    ValidatedSQL("DELETE FROM items", "DELETE", True, False, ""),
    ValidatedSQL("DELETE FROM items", "SELECT", True, False, ""),
    ValidatedSQL("SELECT * FROM items; DELETE FROM items", "SELECT", True, False, ""),
    ValidatedSQL("SELECT * FROM mysql.user", "SELECT", True, False, ""),
    ValidatedSQL("SELECT SLEEP(99) FROM items", "SELECT", True, False, ""),
    ValidatedSQL("SELECT * FROM items", "SELECT", False, False, ""),
])
def test_mutation_or_forged_policy_flags_never_reach_engine(settings, query):
    db, _, _ = mock_database(replace(settings, allow_mutations=True))
    with pytest.raises(DatabaseError):
        db.execute(query, schema())
    db.engine.connect.assert_not_called()


@pytest.mark.parametrize("capabilities", [
    [["version", "10.11.0-MariaDB"], ["max_execution_time", "0"]],
    [["version", "5.6.0"]], [],
])
def test_timeout_support_required_for_discover_and_execute(settings, capabilities):
    db, _, queries = mock_database(settings, capabilities=capabilities)
    for action in (db.discover_schema, lambda: db.execute(select(), schema())):
        with pytest.raises(DatabaseError, match="MAX_EXECUTION_TIME"):
            action()
    assert not any(sql.startswith("SELECT") for sql in queries)


def test_untrusted_schema_timeout_flag_cannot_disable_limits(settings):
    db, _, _ = mock_database(settings)
    with pytest.raises(DatabaseError):
        db.execute(select(), replace(schema(), select_timeout_supported=False))
    db.engine.connect.assert_not_called()


def test_guard_is_assignable_and_runs_before_any_database_action(settings):
    db, _, _ = mock_database(settings)
    guard = Mock(side_effect=DatabaseError("Session expired or query quota exhausted."))
    db.action_guard = guard
    for action in (db.discover_schema, lambda: db.execute(select(), schema())):
        with pytest.raises(DatabaseError, match="expired"):
            action()
    assert guard.call_count == 2
    db.engine.connect.assert_not_called()


def test_schema_discovery_metadata_and_per_read_timeout(settings):
    db, connection, queries = mock_database(settings)
    discovered = db.discover_schema()
    assert discovered.table_names == ("items",) and discovered.select_timeout_supported
    assert discovered.tables[0]["columns"] == ({"name": "id", "type": "int", "nullable": False},)
    assert discovered.tables[0]["primary_key"] == ["id"]
    selects = [index for index, sql in enumerate(queries) if sql.startswith("SELECT")]
    assert len(selects) == 2
    assert all(queries[index - 1] == "SET SESSION MAX_EXECUTION_TIME = 10000" for index in selects)
    assert len(discovered.description.encode()) <= CLOUD_MAX_METADATA_BYTES
    connection.commit.assert_not_called()


def test_single_table_schema_and_missing_table(settings):
    scoped = replace(settings, schema_scope=False)
    db, _, _ = mock_database(scoped)
    assert db.discover_schema().columns[0]["name"] == "id"
    db, _, _ = mock_database(scoped, tables=[])
    assert not db.discover_schema().exists


@pytest.mark.parametrize("tables,metadata", [
    ([f"t{index}" for index in range(51)], None),
    (["items"], [[f"c{index}", "int", "YES", ""] for index in range(501)]),
    (["a", "b"], [[f"c{index}", "int", "YES", ""] for index in range(251)]),
    (["items"], [[f"c{index}", "x" * 60000, "YES", ""] for index in range(3)]),
])
def test_schema_limits_reject_and_invalidate_without_draining(settings, tables, metadata):
    db, connection, _ = mock_database(settings, tables=tables, metadata=metadata)
    with pytest.raises(DatabaseError):
        db.discover_schema()
    connection.invalidate.assert_called_once()


def test_exact_table_and_column_limits_allowed(settings):
    db, _, _ = mock_database(settings, tables=[f"t{i}" for i in range(50)],
                            metadata=[[f"c{i}", "int", "YES", ""] for i in range(10)])
    discovered = db.discover_schema()
    assert len(discovered.tables) == 50
    assert sum(len(table["columns"]) for table in discovered.tables) == 500


@pytest.mark.parametrize("rows,columns", [
    ([["x" * (CLOUD_MAX_CELL_BYTES + 1)]], ["id"]),
    ([[b"x" * CLOUD_MAX_CELL_BYTES]], ["id"]),
    ([["\u2603" * 12000]], ["id"]),
    ([["x" * 60000] * 18], [f"c{i}" for i in range(18)]),
    ([], ["x" * (CLOUD_MAX_CELL_BYTES + 1)]),
    ([], [f"c{i}" for i in range(501)]),
])
def test_result_cell_byte_column_limits(settings, rows, columns):
    db, connection, _ = mock_database(settings, query_rows=rows, query_columns=columns)
    with pytest.raises(DatabaseError):
        db.execute(select(), schema())
    connection.invalidate.assert_called_once()
    connection.commit.assert_not_called()


def test_result_aggregate_byte_limit(settings):
    db, connection, _ = mock_database(settings, query_columns=[f"c{i}" for i in range(10)],
                                     query_rows=[["x" * 60000] * 10] * 2)
    with pytest.raises(DatabaseError, match="byte"):
        db.execute(select(), schema())
    connection.invalidate.assert_called_once()


def test_small_result_within_byte_bounds(settings):
    db, _, _ = mock_database(settings, query_rows=[["hello"]])
    response = db.execute(select(), schema())
    assert db._cloud_json_size([response.columns, response.rows]) < CLOUD_MAX_RESULT_BYTES
    assert not response.truncated


def test_header_budget_rejected_before_fetch_and_invalidate_precedes_close(settings):
    db, connection, _ = mock_database(settings)
    response = result(["id"], [[1]])
    connection.exec_driver_sql.return_value = response
    connection.exec_driver_sql.side_effect = None
    events = Mock()
    events.attach_mock(connection.invalidate, "invalidate")
    events.attach_mock(response.close, "close")
    with pytest.raises(DatabaseError, match="byte"):
        db._cloud_read(connection, "SELECT id FROM items", limit=2,
                       deadline=float("inf"), budget=[0], timed=False)
    response.fetchone.assert_not_called()
    assert [call[0] for call in events.mock_calls] == ["invalidate", "close"]


def test_result_closed_even_if_invalidation_fails(settings):
    db, connection, _ = mock_database(settings)
    response = result(["id"], [["x" * (CLOUD_MAX_CELL_BYTES + 1)]])
    connection.exec_driver_sql.side_effect = None
    connection.exec_driver_sql.return_value = response
    connection.invalidate.side_effect = RuntimeError("private-password")
    with pytest.raises(DatabaseError) as caught:
        db.execute(select(), schema())
    response.close.assert_called_once()
    assert "private-password" not in "".join(traceback.format_exception(caught.value))


def test_total_action_deadline(settings, monkeypatch):
    db, connection, queries = mock_database(settings)
    monkeypatch.setattr(db, "_cloud_deadline", lambda: -1)
    with pytest.raises(DatabaseError, match="time limit"):
        db.discover_schema()
    assert not queries
    connection.invalidate.assert_called_once()


@pytest.mark.parametrize("action", ["discover", "execute"])
def test_cloud_errors_never_expose_credentials_or_sql(settings, action):
    db, connection, _ = mock_database(settings)
    connection.exec_driver_sql.side_effect = OperationalError(
        "private SQL", {}, Exception("private-password db.example.com"))
    with pytest.raises(DatabaseError) as caught:
        db.discover_schema() if action == "discover" else db.execute(select(), schema())
    formatted = "".join(traceback.format_exception(caught.value))
    assert "private-password" not in formatted and "private SQL" not in formatted
    assert caught.value.__suppress_context__