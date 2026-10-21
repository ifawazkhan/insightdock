from dataclasses import replace
from unittest.mock import Mock, MagicMock

import pytest

from chatbot.config import Settings
from chatbot.db import Database, Schema
from chatbot.service import ChatService
from chatbot.sql_policy import PolicyError, validate_sql


TABLES = ("employees", "departments", "orders")


def test_single_table_scope_enforced_for_ai_and_execution():
    settings = Settings("localhost", 3306, "user", "secret", "hr_demo", schema_scope=True)
    schema = Schema("hr_demo", "employees_messy", (), (), (), "8", tables=(
        {"name": "employees", "columns": [{"name": "id"}], "foreign_keys": [
            {"referred_table": "departments"}, {"referred_table": "employees"}]},
        {"name": "departments", "columns": [{"name": "id"}]},))
    db = Mock()
    db.discover_schema.return_value = schema
    service = ChatService(settings, db)
    service.scope_table = "employees"
    narrowed = service.scoped_schema(schema)
    assert narrowed.table_names == ("employees",)
    assert "departments" not in narrowed.description
    for sql in ("SELECT * FROM departments", "SELECT * FROM employees e JOIN departments d ON e.id=d.id",
                "SELECT * FROM employees WHERE id IN (SELECT id FROM departments)"):
        with pytest.raises(PolicyError):
            service.propose(sql, schema)
    proposal = service.propose("WITH e AS (SELECT * FROM employees) SELECT * FROM e", schema)
    service.execute(proposal)
    assert db.execute.call_args.args[1].table_names == ("employees",)
    service.scope_table = None
    assert service.propose("SELECT * FROM departments", schema).query.read_only


def test_selected_table_metadata_change_blocks_execution():
    settings = Settings("localhost", 3306, "user", "secret", "hr_demo", schema_scope=True)
    schema = Schema("hr_demo", "employees_messy", (), (), (), "8", tables=(
        {"name": "employees", "columns": [{"name": "id"}]},))
    db = Mock()
    service = ChatService(settings, db)
    service.scope_table = "employees"
    proposal = service.propose("SELECT * FROM employees", schema)
    db.discover_schema.return_value = replace(schema, tables=({"name": "employees", "columns": []},))
    with pytest.raises(PolicyError, match="schema changed"):
        service.execute(proposal)
    db.execute.assert_not_called()


@pytest.mark.parametrize("sql", [
    "SELECT e.name, d.name FROM employees e JOIN departments d ON e.department_id = d.id",
    "SELECT * FROM hr_demo.employees e LEFT JOIN hr_demo.orders o ON e.id = o.employee_id",
    "WITH totals AS (SELECT employee_id, SUM(amount) AS total FROM orders GROUP BY employee_id) "
    "SELECT e.name, t.total FROM employees e JOIN totals t ON e.id = t.employee_id",
    "SELECT * FROM employees WHERE id IN (SELECT employee_id FROM orders)",
    "SELECT * FROM employees a JOIN employees b ON a.manager_id = b.id",
])
def test_selected_schema_joins(sql):
    query = validate_sql(sql, "hr_demo", "employees_messy", allowed_tables=TABLES)
    assert query.read_only
    assert set(query.target_tables) <= set(TABLES)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM employees JOIN other_schema.departments ON 1 = 1",
    "SELECT * FROM employees WHERE id IN (SELECT id FROM secret_table)",
    "WITH departments AS (SELECT * FROM other_schema.departments) SELECT * FROM departments",
    "SELECT * FROM employees JOIN secret_view ON 1 = 1",
    "SELECT SLEEP(1) FROM employees JOIN departments ON 1 = 1",
    "DELETE e FROM employees e JOIN departments d ON e.department_id = d.id",
    "UPDATE employees e JOIN departments d ON e.department_id = d.id SET e.name = 'x'",
    "DROP TABLE employees, departments",
])
def test_schema_joins_do_not_bypass_policy(sql):
    with pytest.raises(PolicyError):
        validate_sql(sql, "hr_demo", "employees_messy", allowed_tables=TABLES)


def test_confirmation_names_actual_table_and_consumes_once():
    settings = Settings("localhost", 3306, "user", "secret", "hr_demo", allow_mutations=True, schema_scope=True)
    schema = Schema("hr_demo", "employees_messy", (), (), (), "8", tables=(
        {"name": "employees", "columns": [{"name": "id"}]},
        {"name": "departments", "columns": [{"name": "id"}]},))
    db = Mock()
    db.discover_schema.return_value = schema
    service = ChatService(settings, db)
    proposal = service.propose("DELETE FROM departments", schema)
    assert service.confirmation_phrase == "CONFIRM departments"
    with pytest.raises(PolicyError):
        service.execute(proposal, confirmed=True, phrase="CONFIRM employees_messy")
    service.execute(proposal, confirmed=True, phrase="CONFIRM departments")
    with pytest.raises(PolicyError):
        service.execute(proposal, confirmed=True, phrase="CONFIRM departments")
    db.execute.assert_called_once()


def test_other_table_schema_change_invalidates_proposal():
    settings = Settings("localhost", 3306, "user", "secret", "hr_demo", allow_mutations=True)
    schema = Schema("hr_demo", "employees_messy", (), (), (), "8", tables=(
        {"name": "employees", "columns": [{"name": "id"}]},))
    db = Mock()
    db.discover_schema.return_value = replace(schema, tables=(*schema.tables, {"name": "orders", "columns": []}))
    service = ChatService(settings, db)
    proposal = service.propose("DELETE FROM employees", schema)
    with pytest.raises(PolicyError, match="schema changed"):
        service.execute(proposal, confirmed=True, phrase="CONFIRM employees")
    db.execute.assert_not_called()


def test_list_databases_only_metadata():
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.exec_driver_sql.return_value = [("sales",), ("hr_demo",)]
    settings = Settings("localhost", 3306, "user", "secret", "hr_demo")
    assert Database(settings, engine).list_databases() == ["hr_demo", "sales"]
    connection.exec_driver_sql.assert_called_once_with("SHOW DATABASES")


def test_system_schema_cannot_mutate():
    settings = Settings("localhost", 3306, "root", "secret", "mysql", allow_mutations=True)
    schema = Schema("mysql", "user", ({"name": "User"},), (), (), "8", tables=({"name": "user"},))
    with pytest.raises(PolicyError, match="read-only"):
        ChatService(settings, Mock()).propose("DELETE FROM user", schema)


def test_discover_all_base_tables_and_relationships(monkeypatch):
    settings = Settings("localhost", 3306, "user", "secret", "hr_demo", schema_scope=True)
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.exec_driver_sql.return_value.scalar_one.return_value = "8.0.40"
    inspector = Mock()
    inspector.get_table_names.return_value = ["orders", "employees"]
    inspector.get_columns.return_value = [{"name": "id", "type": "INT", "nullable": False}]
    inspector.get_pk_constraint.return_value = {"constrained_columns": ["id"]}
    inspector.get_indexes.return_value = [{"name": "ix_id", "column_names": ["id"], "unique": True}]
    inspector.get_foreign_keys.return_value = [{"constrained_columns": ["employee_id"],
        "referred_table": "employees", "referred_columns": ["id"]}]
    monkeypatch.setattr("chatbot.db.inspect", lambda _: inspector)
    schema = Database(settings, engine).discover_schema()
    assert schema.exists
    assert schema.table_names == ("employees", "orders")
    assert schema.tables[1]["foreign_keys"][0]["referred_table"] == "employees"
    assert schema.tables[0]["indexes"][0]["name"] == "ix_id"
    inspector.get_view_names.assert_not_called()