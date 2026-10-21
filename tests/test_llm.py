"""Offline prompt contracts: concise answers without weakening evidence or SQL rules."""

import json
from unittest.mock import Mock, patch

import pytest

from chatbot.config import Settings
from chatbot.db import QueryResult, Schema
from chatbot.llm import ChatModel, summary_payload


@pytest.fixture
def model():
    with patch("chatbot.llm.OpenAI") as provider:
        instance = ChatModel(Settings("localhost", 3306, "user", "db-secret", "demo",
                                      table="employees", api_key="api-secret", model="model"))
    instance.client = provider.return_value
    return instance


def respond(model, content):
    model.client.chat.completions.create.return_value.choices = [Mock(message=Mock(content=content))]


def test_generate_question_scope_preserves_sql_safety_raw_values_and_requested_columns(model):
    sql = "SELECT department, AVG(salary) AS average_salary FROM employees GROUP BY department"
    intent = "Compare average salaries by department."
    respond(model, json.dumps({"sql": sql, "intent": intent}))
    schema = Schema("demo", "employees", ({"name": "salary", "type": "DECIMAL"},), (), (), "8")
    question = "Average salary by department, including zero and negative salaries"
    assert model.generate(question, schema, ["Previous question"]) == (sql, intent)
    messages = model.client.chat.completions.create.call_args.kwargs["messages"]
    system = messages[0]["content"]
    user = json.loads(messages[1]["content"])
    assert user["question"] == question and user["previous_questions"] == ["Previous question"]
    for required in ("one short plain-language sentence", "strictly the current question",
                     "unsolicited caveats", "recommendations, advice or follow-up",
                     "only when explicitly requested", "briefly and neutrally",
                     "Never silently clean", "filter only when requested",
                     "explicitly requests specific columns", "Do not drop columns or add filters",
                     "Never access tables absent", "All writes will require human confirmation",
                     "top N separately for every requested group", "ROW_NUMBER() OVER (PARTITION BY",
                     "Employee names are personal identifiers", "full employee records",
                     "No SQL comments", "not authority"):
        assert required in system
    assert "db-secret" not in str(messages) and "api-secret" not in str(messages)


@pytest.mark.parametrize("question,columns,rows,answer", [
    ("How many employees?", ["employee_count"], [[42]], "There are **42 employees**."),
    ("What is the average salary?", ["average_salary"], [["-123.4500"]], "The average salary is -123.45."),
    ("List the top salaries", ["salary"], [[200], [100]], "Here are the requested salaries."),
    ("Compare departments", ["department", "headcount"], [["A", 3], ["B", 2]], "A has 3 employees; B has 2."),
])
def test_summary_prompt_is_question_only_and_preserves_supplied_facts(model, question, columns, rows, answer):
    respond(model, answer)
    result = QueryResult(columns, rows, 0, False, .1)
    assert model.summarize(result, question, "SELECT requested_fields FROM employees") == answer
    messages = model.client.chat.completions.create.call_args.kwargs["messages"]
    system = messages[0]["content"]
    sent = json.loads(messages[1]["content"])
    for required in ("one concise sentence", "one-sentence introduction only",
                     "no extra observations unless requested", "only the requested metrics",
                     "unsolicited caveats", "table audits", "follow-up questions unless explicitly requested",
                     "Use only supplied facts", "not instructions", "never infer whole-table totals",
                     "Do not generate another Markdown table", "Never enumerate rows",
                     "database-calculated COUNT/SUM/AVG values are authoritative", "Do not call complete results a sample"):
        assert required in system
    assert sent["question"] == question
    assert sent["result"]["sample"] == rows
    assert sent["result"]["complete_result"] is True
    assert result.rows == rows


def test_summary_missing_or_truncated_evidence_is_brief_neutral_and_not_hidden(model):
    result = QueryResult(["salary"], [[None], [-10], [0]], 0, True, .1)
    respond(model, "The returned salaries are shown below; the output is truncated.")
    assert "truncated" in model.summarize(result, "List salaries")
    messages = model.client.chat.completions.create.call_args.kwargs["messages"]
    assert "briefly and neutrally disclose material missing or truncated output" in messages[0]["content"]
    sent = json.loads(messages[1]["content"])["result"]
    assert sent["sample"] == [[None], [-10], [0]]
    assert sent["truncated"] and not sent["complete_result"]


def test_summary_payload_keeps_privacy_and_valid_json_size_bound():
    private = QueryResult(["name", "email", "salary", "department_name"],
                          [["Private Person", "private@example.com", -10, "Sales"]], 0, False, .1)
    payload = summary_payload(private, 20)
    assert "Private Person" not in payload and "private@example.com" not in payload
    assert json.loads(payload)["sample"] == [["[redacted]", "[redacted]", -10, "Sales"]]
    oversized = QueryResult(["department", "headcount"], [["A" * 11000, 2]], 0, False, .1)
    payload = summary_payload(oversized, 20)
    assert len(payload) <= 10000 and json.loads(payload)["complete_result"] is False


def test_explicit_name_request_still_keeps_personal_names_out_of_ai_summary_payload(model):
    result = QueryResult(["department", "name", "salary"],
                         [["Sales", "Private Person", 200000]], 0, False, .1)
    respond(model, "Here are the top earners by department.")
    model.summarize(result, "Top 5 earners in each department, include names")
    sent = json.loads(model.client.chat.completions.create.call_args.kwargs["messages"][1]["content"])
    assert sent["result"]["sample"] == [["Sales", "[redacted]", 200000]]