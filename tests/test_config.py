from dataclasses import replace
from unittest.mock import Mock

import pytest

from chatbot.config import Settings
from chatbot.llm import ChatModel, ModelError


def test_azure_environment(monkeypatch):
    monkeypatch.setattr("chatbot.config.load_dotenv", lambda _: None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CHATBOT_AI_PROVIDER", raising=False)
    monkeypatch.setenv("MYSQL_PASSWORD", "private")
    monkeypatch.setenv("MYSQL_DATABASE", "hr_demo")
    monkeypatch.setenv("CHATBOT_DATABASE", "employees")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "private-azure-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_MINI", "mini-deployment")
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)
    monkeypatch.setenv("CHATBOT_OPENAI_CONSENT", "true")
    monkeypatch.setenv("CHATBOT_AI_SUMMARIES", "true")
    settings = Settings.from_env()
    assert settings.provider == "azure"
    assert settings.model == "mini-deployment"
    assert settings.database == "employees"
    assert settings.openai_consent and settings.ai_summaries
    assert "private" not in repr(settings)
    client = Mock()
    monkeypatch.setattr("chatbot.llm.AzureOpenAI", client)
    ChatModel(settings)
    client.assert_called_once_with(api_key="private-azure-key", azure_endpoint=settings.azure_endpoint,
                                   api_version=settings.azure_api_version, timeout=30, max_retries=0)
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "selected-deployment")
    assert Settings.from_env().model == "selected-deployment"


def test_azure_missing_endpoint():
    settings = Settings("localhost", 3306, "u", "secret", "db", api_key="secret", model="mini", provider="azure")
    with pytest.raises(ModelError, match="AZURE_OPENAI_ENDPOINT"):
        ChatModel(settings)


@pytest.mark.parametrize("name", ["CHATBOT_OPENAI_CONSENT", "CHATBOT_AI_SUMMARIES", "CHATBOT_ALLOW_MUTATIONS"])
def test_invalid_boolean(monkeypatch, name):
    monkeypatch.setattr("chatbot.config.load_dotenv", lambda _: None)
    monkeypatch.setenv("MYSQL_PASSWORD", "secret")
    monkeypatch.setenv(name, "yes")
    with pytest.raises(ValueError, match=name):
        Settings.from_env()