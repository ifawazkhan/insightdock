"""Configuration; never print credentials or alter the dataset loader's settings."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import re

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("Database/table names must be simple SQL identifiers.")
    return value


def bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, str(default)).lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false.")
    return value == "true"


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    database: str
    table: str = "employees_messy"
    api_key: str = field(default="", repr=False)
    model: str = ""
    allow_mutations: bool = False
    max_rows: int = 500
    sample_rows: int = 20
    timeout_seconds: int = 15
    select_timeout_ms: int = 10000
    provider: str = "openai"
    azure_endpoint: str = field(default="", repr=False)
    azure_api_version: str = ""
    openai_consent: bool = False
    ai_summaries: bool = False
    schema_scope: bool = False
    client_scope: str = "table"
    cloud_mode: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        # Decide before loading any local secrets. Cloud deployments use only
        # operator-provided environment variables and per-session DB credentials.
        mode = os.getenv("CHATBOT_MODE", "local").lower()
        if mode not in {"local", "cloud"}:
            raise ValueError("CHATBOT_MODE must be local or cloud.")
        cloud = mode == "cloud"
        if not cloud:
            load_dotenv(ROOT / ".env")
        password = "" if cloud else os.getenv("MYSQL_PASSWORD")
        if password is None:
            raise ValueError("Set MYSQL_PASSWORD in your local .env before connecting.")
        provider = os.getenv("CHATBOT_AI_PROVIDER") or (
            "azure" if os.getenv("AZURE_OPENAI_API_KEY") and not os.getenv("OPENAI_API_KEY") else "openai"
        )
        provider = provider.lower()
        if provider not in {"openai", "azure"}:
            raise ValueError("CHATBOT_AI_PROVIDER must be openai or azure.")
        azure = provider == "azure"
        client_scope = os.getenv("CHATBOT_SCOPE", "table").lower()
        if client_scope not in {"table", "schema"}:
            raise ValueError("CHATBOT_SCOPE must be table or schema.")
        return cls(
            host="" if cloud else os.getenv("MYSQL_HOST", "127.0.0.1"),
            port=3306 if cloud else bounded_int("MYSQL_PORT", 3306, 1, 65535),
            user="" if cloud else os.getenv("MYSQL_USER", "root"),
            password=password,
            database="" if cloud else identifier(os.getenv("CHATBOT_DATABASE") or os.getenv("MYSQL_DATABASE", "hr_demo")),
            table="" if cloud else identifier(os.getenv("CHATBOT_TABLE", "employees_messy")),
            api_key=os.getenv("AZURE_OPENAI_API_KEY" if azure else "OPENAI_API_KEY", ""),
            model=(os.getenv("AZURE_OPENAI_DEPLOYMENT") or os.getenv("AZURE_OPENAI_DEPLOYMENT_MINI", ""))
                if azure else os.getenv("OPENAI_MODEL", ""),
            allow_mutations=False if cloud else env_bool("CHATBOT_ALLOW_MUTATIONS"),
            max_rows=bounded_int("CHATBOT_MAX_ROWS", 500, 1, 5000),
            sample_rows=bounded_int("CHATBOT_SAMPLE_ROWS", 20, 1, 50),
            timeout_seconds=bounded_int("CHATBOT_TIMEOUT_SECONDS", 15, 1, 120),
            select_timeout_ms=bounded_int("CHATBOT_SELECT_TIMEOUT_MS", 10000, 100, 120000),
            provider=provider,
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT", ""),
            azure_api_version=os.getenv("AZURE_OPENAI_API_VERSION", ""),
            openai_consent=env_bool("CHATBOT_OPENAI_CONSENT"),
            ai_summaries=env_bool("CHATBOT_AI_SUMMARIES"),
            schema_scope=True,
            client_scope=client_scope,
            cloud_mode=cloud,
        )