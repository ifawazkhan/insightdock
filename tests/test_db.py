from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

from chatbot.config import Settings
from chatbot.db import Database, DatabaseError, Schema
from chatbot.sql_policy import validate_sql


@pytest.fixture
def setup_db():
    settings = Settings("localhost", 3306, "chatbot", "secret", "hr_demo", max_rows=2)
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.exec_driver_sql("CREATE TABLE employees_messy(id INTEGER PRIMARY KEY, salary INTEGER)")
        c.exec_driver_sql("INSERT INTO employees_messy VALUES (1, 10), (2, 20), (3, 30)")
    db = Database(settings, engine)
    schema = Schema("hr_demo", "employees_messy", ({"name": "salary"},), (), (), "0.0.0")
    yield db, schema
    engine.dispose()


def test_fetch_cap_and_empty(setup_db):
    db, schema = setup_db
    r = db.execute(validate_sql("SELECT * FROM employees_messy ORDER BY id", "hr_demo", "employees_messy"), schema)
    assert r.columns == ["id", "salary"]
    assert r.rows == [[1, 10], [2, 20]]
    assert r.truncated
    r = db.execute(validate_sql("SELECT * FROM employees_messy WHERE id=99", "hr_demo", "employees_messy"), schema)
    assert not r.rows and not r.truncated


def test_commit_and_rollback(setup_db):
    db, schema = setup_db
    q = validate_sql("UPDATE employees_messy SET salary=42 WHERE id=1", "hr_demo", "employees_messy")
    assert db.execute(q, schema).affected_rows == 1
    with db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT salary FROM employees_messy WHERE id=1").scalar_one() == 42
    q = validate_sql("INSERT INTO employees_messy VALUES (1, 0)", "hr_demo", "employees_messy")
    with pytest.raises(DatabaseError):
        db.execute(q, schema)
    with db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT COUNT(*) FROM employees_messy").scalar_one() == 3


def test_connection_loss_unknown_outcome():
    settings = Settings("localhost", 3306, "chatbot", "secret", "hr_demo")
    engine = Mock()
    # Use a concrete context-manager mock for the managed connection.
    from unittest.mock import MagicMock
    managed = MagicMock()
    engine.connect.return_value = managed
    managed.__enter__.return_value.execution_options.return_value.exec_driver_sql.side_effect = OperationalError(
        "sensitive SQL", {}, Exception(2013, "sensitive connection"), connection_invalidated=True)
    db = Database(settings, engine)
    schema = Schema("hr_demo", "employees_messy", (), (), (), "8.0.40")
    with pytest.raises(DatabaseError, match="UNKNOWN") as error:
        db.execute(validate_sql("DELETE FROM employees_messy", "hr_demo", "employees_messy"), schema)
    assert "sensitive" not in str(error.value)


def test_timeout_only_when_supported():
    from unittest.mock import MagicMock
    settings = Settings("localhost", 3306, "chatbot", "secret", "hr_demo")
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    result = connection.execution_options.return_value.exec_driver_sql.return_value
    result.returns_rows = False
    result.rowcount = 0
    db = Database(settings, engine)
    schema = Schema("hr_demo", "employees_messy", (), (), (), "26.7.0", True)
    db.execute(validate_sql("SELECT * FROM employees_messy", "hr_demo", "employees_messy"), schema)
    connection.exec_driver_sql.assert_called_once_with("SET SESSION MAX_EXECUTION_TIME = 10000")