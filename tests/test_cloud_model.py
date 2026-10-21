"""Cloud provider boundaries; no authentication or external network calls."""
from dataclasses import replace
import json
import traceback
from unittest.mock import Mock, patch

import pytest

from chatbot.cloud_access import CloudAccessError
from chatbot.config import Settings
from chatbot.db import QueryResult, Schema
from chatbot.llm import ChatModel, ModelError
from chatbot.reports import ReportEvidence, build_report, utc_now


@pytest.fixture
def model():
    settings = Settings("localhost", 3306, "user", "db-secret", "demo", table="employees",
                        api_key="api-secret", model="model", cloud_mode=True,
                        openai_consent=True, ai_summaries=True)
    with patch("chatbot.llm.OpenAI"):
        instance = ChatModel(settings)
    instance.client = Mock()
    return instance


@pytest.fixture
def guard(monkeypatch):
    check = Mock()
    monkeypatch.setattr("chatbot.llm.check_cloud_action", check)
    return check


def invoke(model, action):
    schema = Schema("demo", "employees", ({"name": "salary", "type": "INT"},), (), (), "8")
    result = QueryResult(["record_count"], [[2]], 0, False, .1)
    evidence = ReportEvidence("q1", "Count", "SELECT COUNT(*) AS record_count FROM employees",
                              result, utc_now(), "snapshot")
    report = build_report("Count", (evidence,), scope=("demo", "employees"), source="snapshot")
    responses = {
        "generate": {"sql": "SELECT salary FROM employees", "intent": "List salaries."},
        "plan": {"queries": [{"id": "q1", "question": "Count",
                              "sql": evidence.sql}], "clarification": ""},
        "summary": "There are two records.",
        "narrative": {"summary": "Review the supplied evidence [q1].",
                      "recommendations": [], "limitations": []},
    }
    content = responses[action]
    model.client.chat.completions.create.return_value.choices = [
        Mock(message=Mock(content=content if isinstance(content, str) else json.dumps(content)))]
    if action == "generate":
        return model.generate("List salaries", schema, ["Earlier question"])
    if action == "plan":
        return model.plan_report("Count", schema, ["Earlier question"])
    if action == "summary":
        return model.summarize(result, "Count", evidence.sql)
    return model.report_narrative(report, consent=True)


@pytest.mark.parametrize("action", ["generate", "plan", "summary", "narrative"])
def test_every_cloud_request_guarded_and_token_capped(model, guard, action):
    model.client.chat.completions.create.side_effect = lambda **kw: (
        guard.assert_called_once_with("model") or model.client.chat.completions.create.return_value)
    invoke(model, action)
    guard.assert_called_once_with("model")
    kwargs = model.client.chat.completions.create.call_args.kwargs
    assert kwargs["max_completion_tokens"] == 4096
    assert "_result_sharing" not in kwargs
    if action == "generate":
        system = kwargs["messages"][0]["content"]
        assert "READ-ONLY" in system and "SELECT" in system
        assert "INSERT, UPDATE, DELETE" not in system
        assert "All writes will require human confirmation" not in system


@pytest.mark.parametrize("action", ["generate", "plan", "summary", "narrative"])
def test_no_schema_question_consent_means_no_provider_calls(model, guard, action):
    model.settings = replace(model.settings, openai_consent=False)
    with pytest.raises(ModelError, match="consent"):
        invoke(model, action)
    guard.assert_not_called()
    model.client.chat.completions.create.assert_not_called()


@pytest.mark.parametrize("action", ["summary", "narrative"])
def test_results_need_separate_settings_consent(model, guard, action):
    model.settings = replace(model.settings, ai_summaries=False)
    with pytest.raises(ModelError, match="consent"):
        invoke(model, action)
    guard.assert_not_called()
    model.client.chat.completions.create.assert_not_called()


@pytest.mark.parametrize("action", ["generate", "plan", "summary", "narrative"])
@pytest.mark.parametrize("failure", ["guard", "provider", "output"])
def test_cloud_errors_have_no_secret_exception_chain(model, guard, action, failure):
    if failure == "guard":
        guard.side_effect = CloudAccessError("api-secret db-secret")
    elif failure == "provider":
        model.client.chat.completions.create.side_effect = RuntimeError("api-secret db-secret")
    else:
        if action == "summary":
            model.client.chat.completions.create.side_effect = IndexError("api-secret db-secret")
        else:
            model.client.chat.completions.create.side_effect = lambda **kw: Mock(
                choices=[Mock(message=Mock(content="api-secret db-secret"))])
    with pytest.raises(ModelError) as error:
        invoke(model, action)
    assert error.value.__cause__ is None
    assert "api-secret" not in "".join(traceback.format_exception(error.value))
    assert "db-secret" not in "".join(traceback.format_exception(error.value))
    if failure == "guard":
        model.client.chat.completions.create.assert_not_called()


def test_cloud_input_limit_counts_utf8_and_includes_schema(model, guard):
    schema = Schema("demo", "employees", ({"name": "界" * 51000, "type": "INT"},), (), (), "8")
    with pytest.raises(ModelError, match="size limit"):
        model.generate("List salaries", schema, ["Earlier question"])
    guard.assert_not_called()
    model.client.chat.completions.create.assert_not_called()


@pytest.mark.parametrize("action", ["generate", "plan", "summary", "narrative"])
def test_local_requests_keep_legacy_kwargs_and_consent_behavior(model, guard, action):
    model.settings = replace(model.settings, cloud_mode=False, openai_consent=False, ai_summaries=False)
    invoke(model, action)
    guard.assert_not_called()
    kwargs = model.client.chat.completions.create.call_args.kwargs
    expected = {"model", "messages"} | (set() if action == "summary" else {"response_format"})
    assert set(kwargs) == expected


def test_azure_uses_same_guard_and_modern_token_parameter(model, guard):
    with patch("chatbot.llm.AzureOpenAI") as azure:
        instance = ChatModel(replace(model.settings, provider="azure",
                                     azure_endpoint="https://example.test", azure_api_version="2024-10-21"))
    instance.client = azure.return_value
    invoke(instance, "generate")
    guard.assert_called_once_with("model")
    assert instance.client.chat.completions.create.call_args.kwargs["max_completion_tokens"] == 4096