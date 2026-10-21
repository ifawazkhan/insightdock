"""Entrypoint cloud contracts, with no live OIDC, AI, DNS or database calls."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from chatbot import cloud_access as access, cloud_ui
from chatbot.config import Settings
from chatbot.db import QueryResult, Schema
from chatbot.llm import ModelError
from chatbot.service import ChatService
from chatbot.sql_policy import PolicyError

# Reuse the local AppTest isolation fixtures (offline model, mail and preferences).
from test_app import email_delivery, fake_model, recipient_store  # noqa: F401


APP = Path(__file__).resolve().parents[1] / "chat_app.py"
ISSUER = "https://accounts.google.com"
SUBJECT = "invited-subject"
OWNER = access.owner_key(ISSUER, SUBJECT)


class User(dict):
    is_logged_in = True


def database(settings):
    db = Mock()
    db.settings = settings
    db.list_databases.side_effect = AssertionError("Cloud must not enumerate operator databases")
    db.discover_schema.return_value = Schema(settings.database, settings.table, (
        {"name": "salary", "type": "INT", "nullable": True, "default": "None"},), (), (), "8.0.40")
    db.execute.return_value = QueryResult(["employee_count"], [[7]], 0, False, 0.01)
    return db


def test_cloud_proposals_reject_writes_even_with_manual_mutation_flag():
    settings = Settings(host="tenant.example.test", port=3306, user="reader",
                        password="secret", database="tenant_data", table="employees",
                        cloud_mode=True, allow_mutations=True)
    db = database(settings)
    service = ChatService(settings, db)
    with pytest.raises(PolicyError, match="read-only"):
        service.propose("DELETE FROM employees", db.discover_schema())
    assert service.pending is None
    db.execute.assert_not_called()


@pytest.fixture(autouse=True)
def cloud_environment(monkeypatch):
    monkeypatch.setenv("CHATBOT_MODE", "cloud")
    monkeypatch.setenv("OPENAI_API_KEY", "operator-ai-key")
    monkeypatch.setenv("OPENAI_MODEL", "offline-model")
    monkeypatch.setenv("CHATBOT_AI_PROVIDER", "openai")
    monkeypatch.setenv("CHATBOT_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("CHATBOT_CLOUD_ALLOWED_SUBJECTS", SUBJECT)
    monkeypatch.setattr("chatbot.config.load_dotenv", Mock(side_effect=AssertionError("Cloud must not load .env")))
    constructor = Mock(side_effect=AssertionError("Entrypoint must use the returned guarded database"))
    monkeypatch.setattr("chatbot.db.Database", constructor)
    monkeypatch.setattr("socket.getaddrinfo", Mock(side_effect=AssertionError("No live DNS")))
    monkeypatch.setattr(cloud_ui, "create_cloud_database", Mock(side_effect=AssertionError("No live MySQL")))
    monkeypatch.setattr(cloud_ui, "_QUOTAS", access.QuotaLimiter())
    return constructor


@pytest.fixture
def native_auth(monkeypatch):
    user = User(iss=ISSUER, sub=SUBJECT, name="Invited Analyst")
    monkeypatch.setattr(st, "user", user)
    monkeypatch.setattr(cloud_ui, "_auth_configuration", lambda: (
        access.AccessPolicy.from_config(ISSUER, SUBJECT), None))
    monkeypatch.setattr(st, "logout", Mock())
    monkeypatch.setattr(st, "login", Mock())
    return user


@pytest.fixture
def cloud_app(monkeypatch, cloud_environment, fake_model):
    operator = Settings.from_env()
    connected = replace(operator, host="tenant.example.test", user="reader", password="tenant-secret",
                        database="tenant_data", table="employees_messy", client_scope="table",
                        openai_consent=True, allow_mutations=False)
    db = database(connected)
    current = {"database": db, "owner": OWNER, "name": "Invited Analyst"}

    def prepare(settings):
        assert settings == operator
        # Do not clear app state here: main itself must bind the exact DB object.
        st.session_state["_cloud_owner"] = current["owner"]
        st.session_state["_cloud_database"] = current["database"]
        return current["database"].settings, current["database"], current["owner"], current["name"]

    prepare_mock = Mock(side_effect=prepare)
    monkeypatch.setattr(cloud_ui, "prepare_cloud_session", prepare_mock)
    account = Mock(side_effect=lambda: st.sidebar.caption("Authenticated cloud account"))
    monkeypatch.setattr(cloud_ui, "render_cloud_account", account)
    # Report authentication remains separate from our entrypoint preparation fake.
    monkeypatch.setattr(cloud_ui, "_check_session", Mock())
    at = AppTest.from_file(str(APP), default_timeout=15).run()
    assert not at.exception
    return at, db, current, prepare_mock, account


@pytest.mark.parametrize("failure", ["issuer", "invites", "auth"])
def test_missing_cloud_configuration_fails_closed(monkeypatch, cloud_environment, failure):
    if failure == "auth":
        monkeypatch.setattr(st, "secrets", {})
    else:
        monkeypatch.delenv("CHATBOT_OIDC_ISSUER" if failure == "issuer" else "CHATBOT_CLOUD_ALLOWED_SUBJECTS")
    at = AppTest.from_file(str(APP), default_timeout=15)
    at.session_state["chats"] = {"private": {"title": "PRIVATE HISTORY"}}
    at.session_state["profiles"] = {"operator": {"name": "PRIVATE OPERATOR"}}
    at.run()
    assert not at.exception
    assert at.error
    assert "service" not in at.session_state
    assert "chats" not in at.session_state and "profiles" not in at.session_state
    assert not at.chat_input and not at.chat_message
    assert not at.sidebar.button and not at.selectbox
    cloud_environment.assert_not_called()


@pytest.mark.parametrize("logged_in,subject", [(False, SUBJECT), (True, "uninvited")])
def test_unauthorized_cloud_entrypoint_has_no_database_or_history(
        native_auth, cloud_environment, logged_in, subject):
    native_auth.is_logged_in = logged_in
    native_auth["sub"] = subject
    at = AppTest.from_file(str(APP), default_timeout=15)
    at.session_state["chats"] = {"private": {"title": "PRIVATE HISTORY"}}
    at.run()
    assert not at.exception
    assert "service" not in at.session_state and "chats" not in at.session_state
    assert not at.chat_input and not at.chat_message
    assert not at.text_input
    cloud_environment.assert_not_called()
    cloud_ui.create_cloud_database.assert_not_called()


def test_native_oidc_onboarding_uses_submitted_database_not_operator(
        monkeypatch, native_auth, cloud_environment):
    created = []

    def connect(settings, request):
        connected = replace(settings, host=request.host, user=request.username,
                            password=request.password, database=request.database,
                            table=request.table or "", client_scope="table" if request.table else "schema")
        db = database(connected)
        created.append(db)
        return db

    factory = Mock(side_effect=connect)
    monkeypatch.setattr(cloud_ui, "create_cloud_database", factory)
    at = AppTest.from_file(str(APP), default_timeout=15).run()
    assert not at.exception
    assert "service" not in at.session_state and "chats" not in at.session_state
    assert at.title[0].value == "Connect your MySQL database"
    values = {"Host": "tenant.example.test", "Database": "tenant_data", "Username": "reader",
              "Password": "tenant-secret", "Table (optional; leave blank to query the database schema)": "employees_messy"}
    for widget in at.text_input:
        widget.set_value(values[widget.label])
    at.checkbox[0].check()
    at.checkbox[1].check()
    next(b for b in at.button if b.label == "Connect").click().run()
    assert not at.exception
    factory.assert_called_once()
    db = created[0]
    service = at.session_state["service"]
    assert service.database is db is at.session_state["_cloud_database"]
    assert service.settings is db.settings
    assert service.settings.database == "tenant_data"
    assert service.settings.user == "reader" and not service.settings.allow_mutations
    assert service.scope_table == "employees_messy"
    assert at.session_state["active_profile"] == OWNER
    assert list(at.session_state["profiles"]) == [OWNER]
    assert at.session_state["profiles"][OWNER]["name"] == "Invited Analyst"
    assert not any(s.label == "User" for s in at.selectbox)
    assert not any(b.label == "Add user" for b in at.button)
    cloud_environment.assert_not_called()


def test_cloud_prepare_precedes_history_and_binds_service(cloud_app, cloud_environment):
    at, db, _, prepare, account = cloud_app
    assert at.session_state["service"].database is db
    assert at.session_state["service"].settings is db.settings
    assert at.session_state["service"].scope_table == "employees_messy"
    assert list(at.session_state["profiles"]) == [OWNER]
    assert at.session_state["active_profile"] == OWNER
    assert not at.session_state["schema_histories"]
    assert not any(s.label in {"User", "Database", "Table"} for s in at.selectbox)
    assert not any(b.label == "Add user" for b in at.button)
    assert not at.chat_input[0].disabled
    at.toggle(key="dark_mode").set_value(True).run()
    assert not at.exception
    assert at.session_state["service"].database is db
    assert prepare.call_count == account.call_count == 2
    db.engine.dispose.assert_not_called()
    cloud_environment.assert_not_called()


def test_cloud_same_settings_new_database_generation_drops_old_history(cloud_app):
    at, old_db, current, _, _ = cloud_app
    at.chat_input[0].set_value("How many employees?").run()
    assert not at.exception
    old_service = at.session_state["service"]
    old_chat = at.session_state["active_chat"]
    at.session_state["edited_prompt"] = "OLD PRIVATE PROMPT"
    at.session_state["pending_prompt"] = "OLD PRIVATE WRITE"
    replacement = database(old_db.settings)
    current["database"] = replacement
    at.run()
    assert not at.exception
    assert at.session_state["service"] is not old_service
    assert at.session_state["service"].database is replacement
    assert old_chat not in at.session_state["chats"]
    assert not at.session_state["messages"]
    assert "pending_prompt" not in at.session_state and "edited_prompt" not in at.session_state
    assert not at.session_state["schema_histories"]
    old_db.engine.dispose.assert_called_once()
    replacement.execute.assert_not_called()


def test_cloud_different_owner_never_inherits_history(cloud_app):
    at, db, current, _, _ = cloud_app
    at.chat_input[0].set_value("PRIVATE QUESTION").run()
    old_chat = at.session_state["active_chat"]
    current.update(owner=access.owner_key(ISSUER, "second-subject"), name="Second Analyst",
                   database=database(replace(db.settings, database="second_tenant")))
    at.run()
    assert not at.exception
    assert list(at.session_state["profiles"]) == [current["owner"]]
    assert at.session_state["active_profile"] == current["owner"]
    assert old_chat not in at.session_state["chats"]
    assert not at.session_state["messages"]
    assert "PRIVATE QUESTION" not in " ".join(m.value for m in at.markdown)


def test_cloud_schema_scope_comes_from_returned_settings(cloud_app):
    at, db, current, _, _ = cloud_app
    scoped = database(replace(db.settings, table="", client_scope="schema"))
    scoped.discover_schema.return_value = Schema("tenant_data", "", (), (), (), "8.0.40", tables=(
        {"name": "employees_messy", "columns": (
            {"name": "salary", "type": "INT", "nullable": True, "default": "None"},),
         "primary_key": (), "foreign_keys": ()},))
    current["database"] = scoped
    at.run()
    assert not at.exception
    assert at.session_state["service"].database is scoped
    assert at.session_state["service"].scope_table is None
    assert "schema" in at.chat_input[0].placeholder
    assert not at.selectbox
    scoped.execute.assert_not_called()


def test_cloud_cross_database_read_is_blocked(cloud_app):
    at, db, _, _, _ = cloud_app
    at.chat_input[0].set_value("SELECT * FROM operator_private.employees_messy").run()
    assert not at.exception
    assert at.error
    assert at.session_state["service"].pending is None
    db.execute.assert_not_called()


def test_report_access_cleanup_stops_before_reading_cleared_app_state(cloud_app, monkeypatch):
    at, _, _, _, _ = cloud_app

    def deny():
        access.clear_cloud_session(st.session_state)
        raise access.CloudAccessError("Session expired.")

    monkeypatch.setattr(cloud_ui, "_check_session", deny)
    at.chat_input[0].set_value("Create a workforce report").run()
    assert not at.exception
    assert at.warning
    assert "service" not in at.session_state and "chats" not in at.session_state
    assert "messages" not in at.session_state


@pytest.mark.parametrize("sql", ["DELETE FROM employees_messy", "UPDATE employees_messy SET salary = 1",
                                 "DROP TABLE employees_messy"])
def test_cloud_writes_are_rejected_without_confirmation_or_execution(cloud_app, sql):
    at, db, _, _, _ = cloud_app
    at.chat_input[0].set_value(sql).run()
    assert not at.exception
    assert any("read-only queries only" in e.value for e in at.error)
    assert at.session_state["service"].pending is None
    assert "pending_prompt" not in at.session_state
    assert not any(b.label == "Confirm & execute once" for b in at.button)
    db.execute.assert_not_called()


@pytest.mark.parametrize("stage", ["discovery", "fresh_discovery", "execute", "model", "report"])
@pytest.mark.parametrize("clear_session", [False, True])
def test_cloud_guard_denials_do_not_crash_or_restore_cleared_history(
        cloud_app, fake_model, monkeypatch, stage, clear_session):
    at, db, _, _, _ = cloud_app

    def deny(*args, **kwargs):
        if clear_session:
            access.clear_cloud_session(st.session_state)
        raise access.CloudAccessError("Cloud action denied safely.")

    if stage == "discovery":
        db.discover_schema.side_effect = deny
        at.run()
    else:
        if stage == "fresh_discovery":
            schema = db.discover_schema.return_value
            calls = iter([schema])

            def discover():
                try:
                    return next(calls)
                except StopIteration:
                    return deny()

            db.discover_schema.side_effect = discover
        elif stage == "execute":
            db.execute.side_effect = deny
        elif stage == "model":
            fake_model.generate.side_effect = deny
        else:
            monkeypatch.setattr("chatbot.report_ui.build_report", deny)
        at.chat_input[0].set_value("Create a workforce report" if stage == "report" else "How many employees?").run()
    assert not at.exception
    assert at.error or at.warning
    if clear_session:
        assert "service" not in at.session_state and "chats" not in at.session_state
        assert "messages" not in at.session_state and "profiles" not in at.session_state
    else:
        assert at.session_state["service"].pending is None
        assert "pending_prompt" not in at.session_state
        chat = at.session_state["chats"][at.session_state["active_chat"]]
        assert not chat.get("report_open")
    if stage not in {"execute", "report"}:
        db.execute.assert_not_called()


@pytest.mark.parametrize("stage", ["generate", "summary", "query", "execute", "report"])
@pytest.mark.parametrize("clear_session", [False, True])
def test_wrapped_model_denials_preserve_only_valid_cloud_state(
        cloud_app, fake_model, monkeypatch, stage, clear_session):
    at, db, _, _, _ = cloud_app
    if stage == "summary":
        db.settings = replace(db.settings, ai_summaries=True)
        at.run()
        assert not at.exception
    service = at.session_state["service"]

    def deny(*args, **kwargs):
        if clear_session:
            access.clear_cloud_session(st.session_state)
        raise ModelError("Cloud model action denied safely.") from access.CloudAccessError("Session unavailable.")

    if stage == "generate":
        fake_model.generate.side_effect = deny
    elif stage == "summary":
        fake_model.summarize.side_effect = deny
    elif stage == "query":
        monkeypatch.setattr(service, "propose", deny)
    elif stage == "execute":
        db.execute.side_effect = deny
    else:
        monkeypatch.setattr("chatbot.report_ui.build_report", deny)
    at.chat_input[0].set_value("Create a workforce report" if stage == "report" else "How many employees?").run()
    assert not at.exception
    if clear_session:
        assert at.error or at.warning
        for key in ("service", "chats", "messages", "profiles", "pending_prompt", "edited_prompt"):
            assert key not in at.session_state
    else:
        assert at.session_state["service"] is service
        chat = at.session_state["chats"][at.session_state["active_chat"]]
        assert chat["messages"][0]["role"] == "user"
        assert not chat.get("report_open")
        if stage != "report":
            assert chat["messages"][-1]["role"] == "assistant"
            assert ("notice" if stage == "summary" else "error") in chat["messages"][-1]
    if stage not in {"summary", "execute"}:
        db.execute.assert_not_called()


@pytest.mark.parametrize("stage", ["generate", "summary"])
@pytest.mark.parametrize("invalid_state", ["service", "messages", "chats"])
def test_model_return_cannot_append_to_invalid_cloud_state(
        cloud_app, fake_model, stage, invalid_state):
    at, db, _, _, _ = cloud_app
    if stage == "summary":
        db.settings = replace(db.settings, ai_summaries=True)
        at.run()
        assert not at.exception
    messages = at.session_state["messages"]

    def invalidate(*args, **kwargs):
        st.session_state.pop(invalid_state)
        return ("", "Do not append this answer") if stage == "generate" else "Do not append this summary"

    getattr(fake_model, "generate" if stage == "generate" else "summarize").side_effect = invalidate
    at.chat_input[0].set_value("How many employees?").run()
    assert not at.exception
    assert at.error
    assert invalid_state not in at.session_state
    assert [message["role"] for message in messages] == ["user"]
    if stage == "generate":
        db.execute.assert_not_called()