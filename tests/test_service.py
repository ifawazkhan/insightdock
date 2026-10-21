from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
from unittest.mock import Mock

import pytest

from chatbot.config import Settings
from chatbot.db import DatabaseError, QueryResult, Schema, normalize
from chatbot.llm import ChatModel, ModelError, summary_payload
from chatbot.service import ChatService
from chatbot.sql_policy import PolicyError


@pytest.fixture
def settings():
    return Settings("localhost", 3306, "chatbot", "secret", "hr_demo", allow_mutations=True)


@pytest.fixture
def schema():
    return Schema("hr_demo", "employees_messy", ({"name": "salary", "type": "INT"},), (), (), "8.0.40")


@pytest.fixture
def service(settings, schema):
    database = Mock()
    database.discover_schema.return_value = schema
    database.execute.return_value = QueryResult([], [], 1, False, 0.01)
    return ChatService(settings, database)


def test_confirmation_and_one_use(service, schema):
    p = service.propose("DELETE FROM employees_messy", schema)
    with pytest.raises(PolicyError):
        service.execute(p)
    with pytest.raises(PolicyError):
        service.execute(p, confirmed=True, phrase="wrong")
    service.database.execute.assert_not_called()
    service.execute(p, confirmed=True, phrase="CONFIRM employees_messy")
    with pytest.raises(PolicyError):
        service.execute(p, confirmed=True, phrase="CONFIRM employees_messy")
    service.database.execute.assert_called_once()


def test_regular_dml_confirmation(service, schema):
    p = service.propose("DELETE FROM employees_messy WHERE salary < 0", schema)
    with pytest.raises(PolicyError):
        service.execute(p)
    service.execute(p, confirmed=True)


def test_cancel_and_new_prompt(service, schema):
    old = service.propose("DROP TABLE employees_messy", schema)
    service.propose("SELECT salary FROM employees_messy", schema)
    with pytest.raises(PolicyError):
        service.execute(old, confirmed=True, phrase="CONFIRM employees_messy")
    service.cancel()
    assert service.pending is None


def test_changed_schema(service, schema):
    p = service.propose("SELECT * FROM employees_messy", schema)
    service.database.discover_schema.return_value = replace(schema, columns=({"name": "new"},))
    with pytest.raises(PolicyError, match="schema changed"):
        service.execute(p)
    service.database.execute.assert_not_called()
    assert service.pending is None


def test_failed_mutation_never_retries(service, schema):
    p = service.propose("INSERT INTO employees_messy(salary) VALUES (42)", schema)
    service.database.execute.side_effect = DatabaseError("unknown outcome")
    with pytest.raises(DatabaseError):
        service.execute(p, confirmed=True)
    with pytest.raises(PolicyError):
        service.execute(p, confirmed=True)
    service.database.execute.assert_called_once()


def test_readonly_setting(settings, schema):
    service = ChatService(replace(settings, allow_mutations=False), Mock())
    with pytest.raises(PolicyError, match="disabled"):
        service.propose("DROP TABLE employees_messy", schema)


def test_missing_table_recovery(service, schema):
    absent = replace(schema, columns=())
    with pytest.raises(PolicyError, match="missing"):
        service.propose("SELECT * FROM employees_messy", absent)
    p = service.propose("CREATE TABLE employees_messy(id INT)", absent)
    assert not p.query.read_only


def test_serialization_and_status():
    assert normalize(Decimal("123.450")) == "123.450"
    assert normalize(date(2026, 1, 1)) == "2026-01-01"
    assert normalize(b"abc") == "616263"
    assert normalize(None) is None
    assert "truncated" in QueryResult(["x"], [[1]], 0, True, 0.1).status
    assert "0 rows" in QueryResult(["x"], [], 0, False, 0.1).status


def test_sample_privacy_and_bounds():
    result = QueryResult(["email", "salary", "emp_id"], [["private@example.com", 42, 123456]] * 100, 0, True, 1)
    payload = summary_payload(result, 2)
    assert "private@example.com" not in payload
    assert "123456" not in payload
    assert len(json.loads(payload)["sample"]) == 2
    assert not json.loads(payload)["complete_result"]
    huge = QueryResult(["value"], [["a" * 20000]], 0, False, 1)
    assert len(summary_payload(huge, 20)) <= 10000


def test_complete_aggregate_not_mislabeled_as_sample():
    result = QueryResult(["total_employees"], [[895]], 0, False, 0.01)
    payload = json.loads(summary_payload(result, 20))
    assert payload["complete_result"]
    assert payload["sample"] == [[895]]
    assert "Complete query output" in payload["sampling_note"]


def test_secrets_not_in_settings_repr(settings):
    assert "secret" not in repr(settings)


def test_model_missing_configuration(settings):
    with pytest.raises(ModelError, match="OPENAI_API_KEY"):
        ChatModel(settings)


def test_model_generation_uses_schema_not_results(settings, schema):
    model = ChatModel(replace(settings, api_key="test-key", model="test-model"))
    model.client = Mock()
    model.client.chat.completions.create.return_value.choices = [Mock()]
    model.client.chat.completions.create.return_value.choices[0].message.content = json.dumps({
        "sql": "SELECT AVG(salary) FROM employees_messy", "intent": "Average salary"})
    sql, intent = model.generate("average salary", schema, ["count employees"])
    assert sql.startswith("SELECT")
    messages = model.client.chat.completions.create.call_args.kwargs["messages"]
    assert "secret" not in str(messages)
    assert "test-key" not in str(messages)
    assert "salary" in str(messages)
    assert intent == "Average salary"


@pytest.mark.parametrize("content", ["not json", "[]", '{"sql": 1, "intent": "bad"}', "{}"])
def test_malformed_model_output(settings, schema, content):
    model = ChatModel(replace(settings, api_key="test-key", model="test-model"))
    model.client = Mock()
    model.client.chat.completions.create.return_value.choices = [Mock()]
    model.client.chat.completions.create.return_value.choices[0].message.content = content
    with pytest.raises(ModelError):
        model.generate("question", schema, [])