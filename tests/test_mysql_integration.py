"""Explicitly opt-in integration against an empty disposable test database only."""

from dataclasses import replace
import os

import pytest
from sqlalchemy import inspect

from chatbot.config import Settings, identifier
from chatbot.db import Database
from chatbot.service import ChatService


@pytest.fixture
def sandbox():
    if os.getenv("CHATBOT_RUN_INTEGRATION") != "1":
        pytest.skip("Live destructive integration tests are opt-in.")
    settings = Settings.from_env()
    name = identifier(os.getenv("CHATBOT_TEST_DATABASE", ""))
    if not name.startswith("chatbot_test_") or name == settings.database:
        pytest.fail("Use a different disposable database starting with chatbot_test_.")
    settings = replace(settings, database=name, table="employees_messy", allow_mutations=True)
    db = Database(settings)
    with db.engine.connect() as c:
        if inspect(c).has_table(settings.table):
            db.engine.dispose()
            pytest.fail("Refusing to overwrite an existing sandbox employees_messy table.")
    service = ChatService(settings, db)
    created = False
    try:
        p = service.propose("CREATE TABLE employees_messy(emp_id INT PRIMARY KEY, salary INT)", db.discover_schema())
        service.execute(p, confirmed=True)
        created = True
        yield service, db
    finally:
        if created:
            with db.engine.begin() as c:
                c.exec_driver_sql("DROP TABLE IF EXISTS employees_messy")
        db.engine.dispose()


def test_live_dml_ddl_and_reads(sandbox):
    service, db = sandbox
    def execute(sql, confirmed=False, phrase=""):
        p = service.propose(sql, db.discover_schema())
        return service.execute(p, confirmed=confirmed, phrase=phrase)
    execute("INSERT INTO employees_messy VALUES (1, 100), (2, -1)", True)
    assert execute("SELECT COUNT(*) AS n FROM employees_messy").rows == [[2]]
    execute("UPDATE employees_messy SET salary=200 WHERE emp_id=1", True)
    execute("DELETE FROM employees_messy WHERE salary < 0", True)
    execute("ALTER TABLE employees_messy ADD COLUMN test_col INT", True, service.confirmation_phrase)
    assert "test_col" in [c["name"] for c in db.discover_schema().columns]
    execute("CREATE INDEX ix_salary ON employees_messy(salary)", True)
    execute("DROP INDEX ix_salary ON employees_messy", True, service.confirmation_phrase)
    execute("TRUNCATE TABLE employees_messy", True, service.confirmation_phrase)
    assert execute("SELECT COUNT(*) FROM employees_messy").rows == [[0]]
    execute("DROP TABLE employees_messy", True, service.confirmation_phrase)
    assert not db.discover_schema().exists
    execute("CREATE TABLE employees_messy(emp_id INT PRIMARY KEY, salary INT)", True)