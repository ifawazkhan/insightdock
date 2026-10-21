from datetime import datetime, timezone
import json
from unittest.mock import Mock

import pytest

from chatbot.config import Settings
from chatbot.db import QueryResult, Schema
from chatbot.llm import ChatModel, ModelError, report_payload
from chatbot.reports import ReportEvidence, build_report


@pytest.fixture
def model():
    instance = ChatModel(Settings("localhost", 3306, "user", "db-secret", "demo", table="employees",
                                  api_key="api-secret", model="model", sample_rows=20))
    instance.client = Mock()
    return instance


@pytest.fixture
def schema():
    return Schema("demo", "employees", ({"name": "salary", "type": "DECIMAL"},), (), (), "8")


def respond(model, content):
    model.client.chat.completions.create.return_value.choices = [Mock()]
    model.client.chat.completions.create.return_value.choices[0].message.content = content


def report(raw=False, large=False):
    items = []
    for i in range(6 if large else 1):
        result = (QueryResult(["name", "salary"], [["Private Person", 123]], 0, False, .1) if raw else
                  QueryResult(["department", "record_count"], [["A" * 100, n] for n in range(50 if large else 2)], 0, False, .1))
        sql = ("SELECT name,salary FROM employees" if raw else
               "SELECT department,COUNT(*) AS record_count FROM employees GROUP BY department")
        items.append(ReportEvidence(f"q{i+1}", "Question contains private@example.com", sql, result,
                                    datetime.now(timezone.utc), "snapshot"))
    return build_report("Private title", tuple(items), scope=("demo", None), source="snapshot")


def test_plan_structure_canonical_scope_and_no_execution(model, schema):
    respond(model, json.dumps({"queries": [{"id": "q1", "question": "Average salary field",
                                           "sql": "select avg(salary) as salary_average from employees"}], "clarification": ""}))
    plan = model.plan_report("salary summary", schema, ["earlier question"])
    assert plan[0].sql.startswith("SELECT AVG")
    sent = str(model.client.chat.completions.create.call_args.kwargs["messages"])
    assert "earlier question" in sent
    assert "db-secret" not in sent and "api-secret" not in sent
    assert "read-only" in sent


@pytest.mark.parametrize("content", ["not JSON", "[]", "{}",
    json.dumps({"queries": [], "clarification": "Define revenue"}),
    json.dumps({"queries": [{"id": "q1", "question": "bad", "sql": "DELETE FROM employees"}], "clarification": ""}),
    json.dumps({"queries": [{"id": "q1", "question": "bad", "sql": "SELECT * FROM other.employees"}], "clarification": ""}),
    json.dumps({"queries": [{"id": "q1", "question": "bad", "sql": "SELECT SLEEP(1) FROM employees"}], "clarification": ""}),
    json.dumps({"queries": [{"id": "q1", "question": "bad", "sql": "SELECT * FROM employees", "execute": True}], "clarification": ""}),
    json.dumps({"queries": [{"id": "q1", "question": 1, "sql": "SELECT * FROM employees"}], "clarification": ""}),
    json.dumps({"queries": [{"id": f"q{i}", "question": "count", "sql": "SELECT COUNT(*) FROM employees"} for i in range(7)], "clarification": ""}),
])
def test_plan_rejects_invalid_unsafe_and_missing_metrics(model, schema, content):
    respond(model, content)
    with pytest.raises(ModelError):
        model.plan_report("Report", schema, [])


def test_payload_privacy_and_bounds():
    encoded = report_payload(report(raw=True))
    assert "Private Person" not in encoded and "private@example.com" not in encoded
    assert "SELECT" not in encoded and "Private title" not in encoded
    assert not json.loads(encoded)["evidence"][0]["available"]
    encoded = report_payload(report(large=True), 50)
    assert len(encoded) <= 30000
    for section in json.loads(encoded)["evidence"]:
        assert len(json.dumps(section)) <= 10000
    encoded = report_payload(report(), 1)
    section = json.loads(encoded)["evidence"][0]
    assert len(section["rows"]) == 1 and not section["complete_query_output"]


def test_consent_denial_zero_provider_calls(model):
    with pytest.raises(ModelError, match="consent"):
        model.report_narrative(report())
    model.client.chat.completions.create.assert_not_called()
    with pytest.raises(ModelError, match="aggregate-safe"):
        model.report_narrative(report(raw=True), consent=True)
    model.client.chat.completions.create.assert_not_called()


def test_narrative_qualitative_cited_preserves_local_numbers(model):
    original = report()
    respond(model, json.dumps({"summary": "Department labels are represented in the supplied evidence [q1].",
                               "recommendations": ["Verify department definitions before decisions [q1]."],
                               "limitations": ["Business context remains unspecified [q1]."]}))
    narrated = model.report_narrative(original, consent=True)
    assert narrated.findings == original.findings
    assert narrated.evidence is original.evidence
    assert original.summary in narrated.summary
    assert "AI commentary" in narrated.summary


@pytest.mark.parametrize("text", ["There are 42 employees [q1].", "There are forty employees [q1].",
                                  "There are many [missing].", "Uncited qualitative commentary.",
                                  "Revenue is $100 [q1].", "Salary is higher [q1].",
                                  "<b>Commentary</b> [q1].", "Share is half [q1]."])
def test_narrative_rejects_numeric_ungrounded_refs_markup(model, text):
    respond(model, json.dumps({"summary": text, "recommendations": [], "limitations": []}))
    with pytest.raises(ModelError):
        model.report_narrative(report(), consent=True)


@pytest.mark.parametrize("content", ["[]", "{}", "not json",
    json.dumps({"summary": "Commentary [q1].", "recommendations": "bad", "limitations": []}),
    json.dumps({"summary": "Commentary [q1].", "recommendations": ["Commentary [q1]."] * 5, "limitations": []})])
def test_narrative_structured_bounds(model, content):
    respond(model, content)
    with pytest.raises(ModelError):
        model.report_narrative(report(), consent=True)