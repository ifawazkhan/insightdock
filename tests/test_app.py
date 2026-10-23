from pathlib import Path
from dataclasses import replace
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
import sqlglot
import streamlit as st
from streamlit.testing.v1 import AppTest

from chatbot.config import Settings
from chatbot.db import QueryResult, Schema
from chatbot.output import display_columns, safe_csv
from chatbot.report_automation import automatic_queries
from chatbot.report_document import build_document
from chatbot.report_email import DeliveryUncertain, EmailSettings, RecipientStore
from chatbot.sql_policy import validate_sql

APP = Path(__file__).resolve().parents[1] / "chat_app.py"


@pytest.fixture(autouse=True)
def fake_model(monkeypatch):
    # All AppTests stay offline, including optional report planning/summaries.
    model = Mock()

    def generate(question, schema, context):
        sql = question if question.lstrip().upper().startswith(
            ("SELECT", "WITH", "DELETE", "UPDATE", "INSERT", "DROP", "ALTER", "TRUNCATE")
        ) else "SELECT COUNT(*) AS employee_count FROM employees_messy"
        return sql, "Answer the requested question"

    model.generate.side_effect = generate
    model.summarize.return_value = "There are 895 employees."
    model.answer_general.return_value = "Photosynthesis is how plants use sunlight to make sugars from water and carbon dioxide."
    model.plan_report.side_effect = AssertionError("Unexpected model report planning")
    monkeypatch.setattr("chatbot.llm.ChatModel", lambda _: model)
    monkeypatch.setattr("chatbot.report_ui.ChatModel", lambda _: model)
    return model


@pytest.fixture
def app_settings(monkeypatch):
    settings = Settings("localhost", 3306, "chatbot", "secret", "hr_demo",
                        api_key="fake", model="test", openai_consent=True,
                        ai_summaries=False, allow_mutations=True,
                        schema_scope=True, client_scope="table")
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
    return settings


@pytest.fixture(autouse=True)
def recipient_store(monkeypatch, tmp_path):
    # Exercise real read/save callbacks without touching persistent profile PII.
    store = RecipientStore(tmp_path.resolve() / "preferences" / "recipients.json")
    monkeypatch.setattr("chatbot.report_ui.RecipientStore", lambda: store)
    return store


@pytest.fixture(autouse=True)
def email_delivery(monkeypatch):
    # Never read local SMTP secrets or contact a mail server in any AppTest.
    monkeypatch.setattr(EmailSettings, "from_env", Mock(return_value=EmailSettings()))
    sender = Mock()
    monkeypatch.setattr("chatbot.report_ui.send_report_pdf", sender)
    for transport in ("SMTP", "SMTP_SSL"):
        monkeypatch.setattr(f"chatbot.report_email.smtplib.{transport}",
                            Mock(side_effect=AssertionError("AppTests must not use SMTP")))
    return sender


@pytest.fixture
def configured_email(monkeypatch):
    settings = EmailSettings(host="smtp.example.test", from_address="reports@example.test")
    monkeypatch.setattr(EmailSettings, "from_env", Mock(return_value=settings))
    assert settings.configured
    return settings


@pytest.fixture
def app(monkeypatch, app_settings, fake_model):
    db = Mock()
    db.list_databases.side_effect = AssertionError("Client UI must not enumerate databases")
    db.discover_schema.return_value = Schema("hr_demo", "employees_messy", (
        {"name": "salary", "type": "INT", "nullable": True, "default": "None"},), (), (), "8.0.40")
    db.execute.return_value = QueryResult(["employee_count"], [[895]], 0, False, 0.01)
    monkeypatch.setattr("chatbot.db.Database", lambda _: db)
    at = AppTest.from_file(str(APP), default_timeout=15).run()
    return at, db


def button(app, label):
    return next(b for b in app.button if b.label == label)


def queue_prompt(at, prompt):
    """Stop at the app's rerun boundary after recording, before processing, a prompt."""
    with patch("chat_app.st.rerun", st.stop):
        at.chat_input[0].set_value(prompt).run()


def submit_prompt(at, prompt):
    """Queue a prompt, then run the queued model/database work explicitly."""
    queue_prompt(at, prompt)
    at.run()


def test_natural_chat_uses_model_without_direct_sql_controls(app, fake_model):
    at, db = app
    assert not at.exception
    assert not at.radio
    assert not at.chat_input[0].disabled
    queue_prompt(at, "How many employees are there?")
    assert fake_model.generate.call_count == 0
    db.execute.assert_not_called()
    assert at.session_state["messages"][-1]["text"] == "How many employees are there?"
    original_generate = fake_model.generate.side_effect
    rendered_text = []
    original_text = st.text

    def verify_user_turn_precedes_model(question, schema, context):
        assert at.session_state["messages"][-1] == {
            "role": "user", "text": question, "id": at.session_state["messages"][-1]["id"]}
        assert question in rendered_text
        db.execute.assert_not_called()
        return original_generate(question, schema, context)

    fake_model.generate.side_effect = verify_user_turn_precedes_model
    with patch("chat_app.st.text", side_effect=lambda value, *args, **kwargs: (
            rendered_text.append(value), original_text(value, *args, **kwargs))[1]):
        at.run()
    assert not at.exception
    fake_model.generate.assert_called_once()
    fake_model.summarize.assert_not_called()
    db.execute.assert_called_once()
    assert not at.code
    assert not at.metric
    assert "895" in " ".join(m.value for m in at.markdown)
    assert not any(exp.label == "View data" for exp in at.expander)
    assert not at.get("download_button")


@pytest.mark.parametrize("question", ["hey", "hey hello", "good morning"])
def test_greetings_get_a_natural_reply_without_sql_or_model(app, fake_model, question):
    at, db = app
    submit_prompt(at, question)
    assert not at.exception
    assert fake_model.generate.call_count == 0
    fake_model.answer_general.assert_not_called()
    fake_model.summarize.assert_not_called()
    db.execute.assert_not_called()
    assert any("Hi! I can chat" in element.value for element in at.markdown)


def test_live_weather_request_is_honest_and_does_not_generate_sql(app, fake_model):
    at, db = app
    responses = [
        {"results": [{"name": "Hyderabad", "admin1": "Telangana", "country": "India",
                      "latitude": 17.385, "longitude": 78.4867}]},
        {"current": {"temperature_2m": 29, "time": "12:15"},
         "current_units": {"temperature_2m": "°C"}},
    ]
    with patch("chatbot.weather._get_json", side_effect=responses) as weather:
        submit_prompt(at, "what is the temperature in Hyderabad")
    assert not at.exception
    assert fake_model.generate.call_count == 0
    fake_model.answer_general.assert_not_called()
    db.execute.assert_not_called()
    assert weather.call_count == 2
    assert any("current temperature in Hyderabad" in element.value for element in at.markdown)


def test_regular_truncated_result_has_no_incomplete_output_warning(app):
    at, db = app
    db.execute.return_value = QueryResult(["employee_count"], [[895]], 0, True, .01)
    submit_prompt(at, "How many employees are there?")
    visible = " ".join(element.value for element in at.markdown)
    assert not at.warning
    assert "incomplete" not in visible.lower() and "truncated" not in visible.lower()


def test_weather_prompt_without_city_asks_a_clarifying_question(app, fake_model):
    at, db = app
    submit_prompt(at, "what is the weather")
    assert not at.exception
    fake_model.generate.assert_not_called()
    fake_model.answer_general.assert_not_called()
    db.execute.assert_not_called()
    assert any("Which city" in element.value for element in at.markdown)


def test_general_knowledge_uses_text_only_model_without_database_query(app, fake_model):
    at, db = app
    submit_prompt(at, "Explain photosynthesis in simple terms")
    assert not at.exception
    fake_model.generate.assert_not_called()
    fake_model.answer_general.assert_called_once_with("Explain photosynthesis in simple terms")
    db.execute.assert_not_called()
    assert any("plants use sunlight" in element.value for element in at.markdown)


def test_report_is_explicit_and_snapshot_never_requeries(app, monkeypatch):
    at, db = app
    from chatbot.report_export import report_pdf
    export = Mock(wraps=report_pdf)
    monkeypatch.setattr("chatbot.report_ui.report_pdf", export)
    submit_prompt(at, "SELECT COUNT(*) AS employee_count FROM employees_messy")
    assert not any(t.label == "Report focus (optional)" for t in at.text_input)
    submit_prompt(at, "Create a workforce report")
    assert not at.exception
    assert db.execute.call_count == 1
    chat = at.session_state["chats"][at.session_state["active_chat"]]
    assert len(chat["reports"]) == 1
    assert chat["reports"][0].source == "snapshot"
    assert not chat["report_open"]
    assert "895" in " ".join(chat["reports"][0].findings)
    report = chat["reports"][0]
    pdf = chat["report_pdfs"][report.id]
    assert pdf.startswith(b"%PDF")
    assert at.download_button[0].label == "Download PDF"
    assert at.download_button[0].proto.ignore_rerun
    assert not any(b.label == "Prepare PDF" for b in at.button)
    for _ in range(3):
        at.run()
        at.download_button[0].click().run()
        assert not at.exception
        assert db.execute.call_count == 1
        current = at.session_state["chats"][at.session_state["active_chat"]]
        assert current["reports"] == [report]
        assert current["report_pdfs"][report.id] is pdf
    export.assert_called_once_with(report)


def test_report_chat_request_builds_immediately_without_composer(app, fake_model):
    at, db = app
    request = "Create a workforce report based on live data"
    queue_prompt(at, request)
    assert db.execute.call_count == 0
    assert at.session_state["messages"][0]["text"] == request
    original_execute = db.execute.side_effect

    def verify_user_turn_precedes_report_query(query, schema):
        assert at.session_state["messages"][0]["role"] == "user"
        assert at.session_state["messages"][0]["text"] == request
        if original_execute:
            return original_execute(query, schema)
        return db.execute.return_value

    db.execute.side_effect = verify_user_turn_precedes_report_query
    at.run()
    assert not at.exception
    chat = at.session_state["chats"][at.session_state["active_chat"]]
    assert len(chat["reports"]) == 1
    assert chat["reports"][0].question == request
    assert chat["messages"][0]["role"] == "user"
    assert chat["messages"][0]["text"] == request
    assert not chat["report_open"]
    assert not any(t.label == "Report focus (optional)" for t in at.text_input)
    assert not any(b.label in {"Build business report", "Build report"} for b in at.button)
    assert not at.session_state["service"].pending
    assert db.execute.call_count == len(chat["reports"][0].evidence)
    fake_model.generate.assert_not_called()
    fake_model.plan_report.assert_not_called()


def test_reports_stay_at_request_turns_with_distinct_email_widgets(
        app, monkeypatch, recipient_store, email_delivery, configured_email):
    at, db = app
    from chatbot.report_export import report_pdf
    export = Mock(wraps=report_pdf)
    monkeypatch.setattr("chatbot.report_ui.report_pdf", export)
    profile_name = at.session_state["profiles"]["owner"]["name"]
    recipient_store.save(profile_name, "saved@example.test")
    request = "Create a workforce report based on live data"
    submit_prompt(at, request)
    submit_prompt(at, "How many employees are there?")
    submit_prompt(at, request)
    assert not at.exception
    chat = at.session_state["chats"][at.session_state["active_chat"]]
    reports = list(chat["reports"])
    assert len(reports) == 2
    assert [message.get("report_id") for message in chat["messages"]] == [
        None, reports[0].id, None, None, None, reports[1].id]
    assert chat["messages"] == at.session_state["messages"]
    assert chat["title"] == request[:48]
    turns = at.chat_message
    assert len(turns) == 6
    assert turns[0].text[0].value == request
    assert turns[1].subheader[0].value == reports[0].title
    assert turns[2].text[0].value == "How many employees are there?"
    assert turns[3].markdown[0].value == "Employee count: 895."
    assert turns[5].subheader[0].value == reports[1].title
    assert not turns[1].get("iframe") and not turns[5].get("iframe")
    keys = [f"report_recipient_owner_{report.id}" for report in reports]
    assert [at.text_input(key=key).value for key in keys] == ["saved@example.test"] * 2
    at.text_input(key=keys[0]).set_value("analyst@example.test").run()
    assert recipient_store.read(profile_name) == "analyst@example.test"
    at.button(key=f"report_email_send_{reports[0].id}").click().run()
    query_count = db.execute.call_count
    for dark in (True, False):
        at.toggle(key="dark_mode").set_value(dark).run()
        at.run()
        assert not at.exception
        assert len(at.chat_message) == 6
        assert len(at.download_button) == 2
        assert db.execute.call_count == query_count
        assert chat["reports"] == reports
    assert export.call_count == 2
    email_delivery.assert_called_once_with(
        chat["report_pdfs"][reports[0].id], reports[0].title,
        "analyst@example.test", configured_email)


def test_expired_report_markers_are_skipped_and_legacy_reports_still_render(app):
    at, db = app
    for _ in range(4):
        submit_prompt(at, "Create a workforce report based on live data")
        assert not at.exception
    chat = at.session_state["chats"][at.session_state["active_chat"]]
    assert len(chat["reports"]) == len(at.download_button) == 3
    assert len(at.chat_message) == 7  # Four user turns, three retained reports.
    assert sum("report_id" in message for message in chat["messages"]) == 4
    query_count = db.execute.call_count
    messages = [message for message in chat["messages"] if "report_id" not in message]
    chat["messages"] = messages
    at.session_state["messages"] = list(messages)
    at.run()
    assert not at.exception
    assert len(at.download_button) == 3
    assert db.execute.call_count == query_count


def test_live_report_without_history_builds_once_from_chat(app, monkeypatch):
    at, db = app
    model = Mock(side_effect=AssertionError("A local workforce report must not need a model"))
    monkeypatch.setattr("chatbot.report_ui.ChatModel", model)
    assert not at.session_state["messages"]
    submit_prompt(at, "Create a workforce report")
    assert not at.exception
    chat = at.session_state["chats"][at.session_state["active_chat"]]
    assert len(chat["reports"]) == 1
    report = chat["reports"][0]
    assert report.source == "live" and report.title == "Workforce Overview"
    assert not chat["report_open"]
    assert "report_plan" not in chat
    assert len(report.evidence) == 2  # Count and typed salary summary in this schema.
    assert db.execute.call_count == len(report.evidence)
    assert all(call.args[0].read_only for call in db.execute.call_args_list)
    assert [m["text"] for m in at.session_state["messages"] if m["role"] == "user"] == ["Create a workforce report"]
    model.assert_not_called()
    pdf = chat["report_pdfs"][report.id]
    assert pdf.startswith(b"%PDF")
    assert at.download_button[0].label == "Download PDF"
    assert not any(b.label == "Prepare PDF" for b in at.button)
    for _ in range(3):
        at.run()
        at.download_button[0].click().run()
        assert not at.exception
        assert db.execute.call_count == len(report.evidence)
        current = at.session_state["chats"][at.session_state["active_chat"]]
        assert current["reports"] == [report]
        assert current["report_pdfs"][report.id] is pdf
    model.assert_not_called()


def test_report_has_no_composer_or_build_controls(app):
    at, db = app
    submit_prompt(at, "Create a workforce report")
    assert not at.exception
    assert [t.label for t in at.main.text_input] == ["Recipient email"]
    assert not at.main.selectbox
    assert not at.main.multiselect
    assert not any(b.label in {"Build report", "Build business report", "Run plan"} for b in at.button)
    assert not any(t.label == "Report focus (optional)" for t in at.text_input)
    assert all(call.args[0].read_only for call in db.execute.call_args_list)


@pytest.mark.parametrize("source", ["snapshot", "live"])
def test_report_main_display_has_no_uuid_ids(app, source):
    at, _ = app
    if source == "snapshot":
        submit_prompt(at, "SELECT COUNT(*) AS employee_count FROM employees_messy")
    submit_prompt(at, "Create a workforce report")
    assert not at.exception
    chat = at.session_state["chats"][at.session_state["active_chat"]]
    report = chat["reports"][0]
    assert report.source == source
    visible = " ".join(str(element.value) for kind in ("text", "caption", "markdown", "subheader")
                       for element in at.main.get(kind))
    identifiers = [at.session_state["active_chat"], report.id, *(e.id for e in report.evidence)]
    identifiers.extend(m["id"] for m in chat["messages"] if "id" in m)
    identifiers.extend(m["query_id"] for m in chat["messages"] if "query_id" in m)
    for identifier in identifiers:
        if len(identifier) == 32 or (identifier.startswith("q") and len(identifier) == 33):
            assert identifier not in visible
    assert any(c.value.startswith("Source 1 ·") for c in at.main.caption)


def test_report_generation_cancels_pending_write(app):
    at, db = app
    submit_prompt(at, "SELECT COUNT(*) FROM employees_messy")
    submit_prompt(at, "DELETE FROM employees_messy")
    assert at.session_state["service"].pending is not None
    submit_prompt(at, "Create a workforce report")
    assert not at.exception
    assert at.session_state["service"].pending is None
    assert "pending_prompt" not in at.session_state
    assert not any(b.label == "Confirm & execute once" for b in at.button)
    assert db.execute.call_count == 1
    at.run()
    assert db.execute.call_count == 1


def test_report_history_profile_isolation_and_edit_invalidation(app):
    at, db = app
    submit_prompt(at, "SELECT COUNT(*) FROM employees_messy")
    submit_prompt(at, "Create a workforce report")
    assert at.download_button[0].label == "Download PDF"
    assert not any(b.label == "Prepare PDF" for b in at.button)
    original_id = at.session_state["active_chat"]
    add_user(at, "Guest")
    chat = at.session_state["chats"][at.session_state["active_chat"]]
    assert not chat.get("reports")
    next(s for s in at.selectbox if s.label == "User").set_value("owner").run()
    assert at.session_state["active_chat"] == original_id
    assert at.session_state["chats"][original_id]["reports"]
    at.text_area[0].set_value("SELECT MAX(salary) FROM employees_messy")
    button(at, "Save & resend").click().run()
    assert not at.exception
    chat = at.session_state["chats"][original_id]
    assert not chat["reports"]
    assert not chat.get("report_pdfs")
    assert not chat["report_open"]
    assert len(chat["query_history"]) == 1
    assert db.execute.call_count == 2


def build_snapshot_report(at):
    submit_prompt(at, "SELECT COUNT(*) AS employee_count FROM employees_messy")
    submit_prompt(at, "Create a workforce report")
    assert not at.exception
    chat = at.session_state["chats"][at.session_state["active_chat"]]
    assert chat["reports"][-1].source == "snapshot"
    assert at.download_button[0].label == "Download PDF"
    return chat


def recipient_input(at):
    return next(t for t in at.text_input if t.label == "Recipient email")


def open_email_dropdown(at, report_id):
    # AppTest exposes popover children even while the browser popover is closed.
    key = f"report_recipient_{at.session_state['active_profile']}_{report_id}"
    assert any(field.key == key for field in at.text_input)


def test_report_recipient_autosaves_and_restores_across_same_profile_chats(app, recipient_store, email_delivery):
    at, db = app
    profile_name = at.session_state["profiles"]["owner"]["name"]
    recipient_store.save(profile_name, "previous@example.test")
    original = at.session_state["active_chat"]
    build_snapshot_report(at)
    original_report_id = at.session_state["chats"][original]["reports"][0].id
    report_id = original_report_id
    assert any(b.label == "Send" and b.key == f"report_email_send_{report_id}" for b in at.button)
    open_email_dropdown(at, report_id)
    assert recipient_input(at).value == "previous@example.test"
    recipient_input(at).set_value("Analyst@EXAMPLE.TEST").run()
    assert not at.exception
    assert recipient_store.read(profile_name) == "Analyst@example.test"
    assert any("Email preference saved" in c.value for c in at.caption)
    button(at, "New chat").click().run()
    # The widget is absent on this run; the next chat must reload the store.
    assert not any(t.label == "Recipient email" for t in at.text_input)
    build_snapshot_report(at)
    report_id = at.session_state["chats"][at.session_state["active_chat"]]["reports"][0].id
    open_email_dropdown(at, report_id)
    assert recipient_input(at).value == "Analyst@example.test"
    at.button(key=f"history_{original}").click().run()
    assert at.session_state["active_chat"] == original
    open_email_dropdown(at, original_report_id)
    assert recipient_input(at).value == "Analyst@example.test"
    assert db.execute.call_count == 2
    email_delivery.assert_not_called()


def test_report_recipient_clear_forgets_saved_address(app, recipient_store, email_delivery):
    at, _ = app
    build_snapshot_report(at)
    report_id = at.session_state["chats"][at.session_state["active_chat"]]["reports"][0].id
    open_email_dropdown(at, report_id)
    profile_name = at.session_state["profiles"]["owner"]["name"]
    recipient_input(at).set_value("forget@example.test").run()
    assert recipient_store.read(profile_name) == "forget@example.test"
    recipient_input(at).set_value("").run()
    assert not at.exception
    assert recipient_store.read(profile_name) == ""
    assert at.button(key=f"report_email_send_{report_id}").disabled
    button(at, "New chat").click().run()
    build_snapshot_report(at)
    assert recipient_input(at).value == ""
    email_delivery.assert_not_called()


def test_report_recipient_is_isolated_by_profile(app, recipient_store, email_delivery):
    at, db = app
    build_snapshot_report(at)
    owner_name = at.session_state["profiles"]["owner"]["name"]
    report_id = at.session_state["chats"][at.session_state["active_chat"]]["reports"][0].id
    open_email_dropdown(at, report_id)
    recipient_input(at).set_value("owner@example.test").run()
    add_user(at, "Report guest")
    guest = at.session_state["active_profile"]
    build_snapshot_report(at)
    report_id = at.session_state["chats"][at.session_state["active_chat"]]["reports"][0].id
    open_email_dropdown(at, report_id)
    assert recipient_input(at).value == ""
    recipient_input(at).set_value("guest@example.test").run()
    assert recipient_store.read(owner_name) == "owner@example.test"
    assert recipient_store.read("Report guest") == "guest@example.test"
    next(s for s in at.selectbox if s.label == "User").set_value("owner").run()
    report_id = at.session_state["chats"][at.session_state["active_chat"]]["reports"][0].id
    open_email_dropdown(at, report_id)
    assert recipient_input(at).value == "owner@example.test"
    next(s for s in at.selectbox if s.label == "User").set_value(guest).run()
    assert not at.exception
    report_id = at.session_state["chats"][at.session_state["active_chat"]]["reports"][0].id
    open_email_dropdown(at, report_id)
    assert recipient_input(at).value == "guest@example.test"
    assert db.execute.call_count == 2
    email_delivery.assert_not_called()


@pytest.mark.parametrize("outcome", ["accepted", "uncertain"])
def test_report_email_deliberate_cached_send_never_repeats(
        app, monkeypatch, recipient_store, email_delivery, configured_email, outcome):
    at, db = app
    from chatbot.report_export import report_pdf
    export = Mock(wraps=report_pdf)
    monkeypatch.setattr("chatbot.report_ui.report_pdf", export)
    profile_name = at.session_state["profiles"]["owner"]["name"]
    recipient_store.save(profile_name, "saved@example.test")
    if outcome == "uncertain":
        email_delivery.side_effect = DeliveryUncertain("Mock ambiguous SMTP result")
    chat = build_snapshot_report(at)
    report = chat["reports"][0]
    pdf = chat["report_pdfs"][report.id]
    assert pdf.startswith(b"%PDF")
    open_email_dropdown(at, report.id)
    assert recipient_input(at).value == "saved@example.test"
    assert not at.button(key=f"report_email_send_{report.id}").disabled
    for _ in range(2):
        at.run()
        at.download_button[0].click().run()
    email_delivery.assert_not_called()
    at.button(key=f"report_email_send_{report.id}").click().run()
    assert not at.exception
    email_delivery.assert_called_once_with(pdf, report.title, "saved@example.test", configured_email)
    assert email_delivery.call_args.args[0] is pdf
    assert chat["report_email_attempts"][(report.id, "saved@example.test")] == outcome
    assert recipient_store.read(profile_name) == "saved@example.test"
    assert at.button(key=f"report_email_send_{report.id}").disabled
    if outcome == "accepted":
        assert any("accepted by the mail server" in s.value and
                   "Inbox delivery is not guaranteed" in s.value for s in at.success)
    else:
        assert any("Delivery outcome is uncertain" in w.value and
                   "automatic retry is blocked" in w.value for w in at.warning)
    for _ in range(3):
        at.run()
        at.download_button[0].click().run()
        assert not at.exception
        open_email_dropdown(at, report.id)
        assert at.button(key=f"report_email_send_{report.id}").disabled
        assert chat["report_pdfs"][report.id] is pdf
    email_delivery.assert_called_once()
    export.assert_called_once_with(report)
    db.execute.assert_called_once()


@pytest.mark.parametrize("address", ["", "invalid", "one@example.test,two@example.test"])
def test_report_invalid_recipient_disables_email_not_download(
        app, recipient_store, email_delivery, configured_email, address):
    at, db = app
    build_snapshot_report(at)
    report_id = at.session_state["chats"][at.session_state["active_chat"]]["reports"][0].id
    open_email_dropdown(at, report_id)
    recipient_input(at).set_value(address).run()
    assert not at.exception
    assert at.button(key=f"report_email_send_{report_id}").disabled
    assert recipient_store.read(at.session_state["profiles"]["owner"]["name"]) == ""
    assert not at.download_button[0].disabled
    at.download_button[0].click().run()
    assert not at.exception
    email_delivery.assert_not_called()
    db.execute.assert_called_once()


def test_report_missing_smtp_keeps_download_available(app, recipient_store, email_delivery):
    at, db = app
    build_snapshot_report(at)
    report_id = at.session_state["chats"][at.session_state["active_chat"]]["reports"][0].id
    open_email_dropdown(at, report_id)
    recipient_input(at).set_value("recipient@example.test").run()
    assert recipient_store.read(at.session_state["profiles"]["owner"]["name"]) == "recipient@example.test"
    assert any("SMTP settings" in w.value for w in at.warning)
    assert at.button(key=f"report_email_send_{report_id}").disabled
    assert not at.download_button[0].disabled
    at.download_button[0].click().run()
    at.run()
    assert not at.exception
    email_delivery.assert_not_called()
    db.execute.assert_called_once()


def test_chart_choice_does_not_requery(app):
    at, db = app
    db.execute.return_value = QueryResult(["department", "employee_count"], [["IT", 4], ["Sales", 6]], 0, False, .01)
    submit_prompt(at, "SELECT department, COUNT(*) AS employee_count FROM employees_messy GROUP BY department")
    chooser = next(s for s in at.selectbox if s.label == "Chart type")
    assert "pie" in chooser.options or "Pie" in chooser.options
    chooser.set_value("pie").run()
    assert not at.exception
    db.execute.assert_called_once()


def test_destructive_confirmation_and_rerun(app):
    at, db = app
    submit_prompt(at, "DELETE FROM employees_messy")
    db.execute.assert_not_called()
    assert button(at, "Confirm & execute once").disabled
    next(t for t in at.text_input if t.label.startswith("Type CONFIRM")).set_value("CONFIRM employees_messy").run()
    button(at, "Confirm & execute once").click().run()
    assert not at.exception
    db.execute.assert_called_once()
    at.run()
    db.execute.assert_called_once()


def test_cancel_and_policy_error(app):
    at, db = app
    submit_prompt(at, "DROP TABLE employees_messy")
    button(at, "Cancel change").click().run()
    db.execute.assert_not_called()
    submit_prompt(at, "SELECT * FROM other")
    assert any("Only hr_demo.employees_messy" in e.value for e in at.error)
    db.execute.assert_not_called()


@pytest.mark.parametrize("overrides", [
    {"api_key": ""}, {"model": ""}, {"openai_consent": False},
])
def test_natural_chat_requires_model_and_consent(app, app_settings, monkeypatch, fake_model, overrides):
    at, _ = app
    settings = replace(app_settings, **overrides)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
    at.run()
    assert not at.exception
    assert not at.chat_input[0].disabled
    assert any("Chat setup is incomplete" in i.value for i in at.info)
    assert not at.radio
    fake_model.generate.assert_not_called()
    submit_prompt(at, "hey")
    assert not at.exception
    fake_model.generate.assert_not_called()
    assert any("Hi! I can chat" in element.value for element in at.markdown)


def test_missing_configuration(monkeypatch):
    def missing(cls):
        raise ValueError("Set MYSQL_PASSWORD in your local .env before connecting.")
    monkeypatch.setattr(Settings, "from_env", classmethod(missing))
    at = AppTest.from_file(str(APP)).run()
    assert not at.exception
    assert any("MYSQL_PASSWORD" in e.value for e in at.error)


def test_export_safety_and_duplicate_labels():
    result = QueryResult(["x", "x", "x (2)"], [["=cmd()", "  +formula", -42]], 0, False, 1)
    assert len(set(display_columns(result.columns))) == 3
    csv = safe_csv(result)
    assert "'=cmd()" in csv
    assert "'  +formula" in csv
    assert "-42" in csv


def test_natural_questions_and_concise_answers_enabled(app, app_settings, monkeypatch, fake_model):
    at, db = app
    settings = replace(app_settings, ai_summaries=True)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
    at.run()
    assert not at.chat_input[0].disabled
    submit_prompt(at, "How many employees are there?")
    assert not at.exception
    fake_model.generate.assert_called_once()
    fake_model.summarize.assert_called_once()
    db.execute.assert_called_once()
    assert any("There are 895 employees" in m.value for m in at.markdown)
    assert not at.code
    assert not at.metric
    assert "895" in " ".join(m.value for m in at.markdown)
    assert any("InsightDock" in m.value for m in at.markdown)


def test_new_chat_starts_empty_and_cancels_pending_write(app):
    at, db = app
    submit_prompt(at, "How many employees are there?")
    submit_prompt(at, "DELETE FROM employees_messy")
    assert at.session_state["service"].pending is not None
    button(at, "New chat").click().run()
    assert not at.exception
    db.execute.assert_called_once()
    assert not at.chat_message
    assert not at.session_state["messages"]
    assert at.session_state["service"].pending is None
    assert "pending_prompt" not in at.session_state


def test_new_chat_cancels_a_queued_unprocessed_question(app, fake_model):
    at, db = app
    queue_prompt(at, "How many employees are there?")
    assert at.session_state.get("queued_chat_prompt") == "How many employees are there?"
    button(at, "New chat").click().run()
    assert not at.exception
    assert "queued_chat_prompt" not in at.session_state
    at.run()
    fake_model.generate.assert_not_called()
    db.execute.assert_not_called()
    assert not at.session_state.messages


def test_inline_table_all_columns(app):
    at, db = app
    columns = ["emp_id", "name", "department", "salary", "join_date_raw", "join_date",
               "city", "email", "phone", "rating", "status"]
    rows = [[1, "Alice", "Engineering", 200000, None, None, "Delhi", "a@example.com", None, 5, "Active"],
            [2, "Bob", "Sales", 190000, None, None, "Pune", "b@example.com", None, 4, "Active"]]
    db.execute.return_value = QueryResult(columns, rows, 0, False, 0.01)
    submit_prompt(at, "SELECT * FROM employees_messy ORDER BY salary DESC LIMIT 5")
    assert not at.exception
    assert not any(e.label == "View data" for e in at.expander)
    result = at.session_state["messages"][-1]["result"]
    assert result.columns == columns
    assert result.rows == rows
    html = " ".join(element.value for element in at.get("html"))
    assert all(column in html for column in columns)
    assert "Alice" in html and "Bob" in html
    assert not at.dataframe
    assert not at.code
    assert [tab.label for tab in at.tabs] == ["Chart", "Details"]
    assert not at.get("download_button")


def test_top_earners_in_each_department_shows_names_and_pay_locally(app, fake_model):
    at, db = app
    question = "top 5 top earners in each department"
    sql = ("WITH ranked AS (SELECT name, department, salary, ROW_NUMBER() OVER "
           "(PARTITION BY department ORDER BY salary DESC, emp_id ASC) AS rank_in_department "
           "FROM employees_messy) SELECT name, department, salary FROM ranked "
           "WHERE rank_in_department <= 5 ORDER BY department, salary DESC")
    fake_model.generate.return_value = (sql, "Here are the highest earners by department.")
    fake_model.generate.side_effect = None
    rows = [[name, department, salary] for department, names in
            (("Engineering", [("Alice Nguyen", 250000), ("Beth Jones", 225000)]),
             ("Sales", [("Carlos Diaz", 215000), ("Diana Chen", 205000)]))
            for name, salary in names]
    db.execute.return_value = QueryResult(["name", "department", "salary"], rows, 0, False, .01)
    submit_prompt(at, question)
    assert not at.exception
    assert "Top 5 earners by department:" in " ".join(element.value for element in at.text)
    answer = next(message for message in at.session_state.messages if message.get("plain_text"))
    assert all(name in answer["text"] for name in ("Alice Nguyen", "Beth Jones", "Carlos Diaz", "Diana Chen"))
    assert "Engineering:" in answer["text"] and "Sales:" in answer["text"]
    assert "250,000" in answer["text"] and "215,000" in answer["text"]
    fake_model.summarize.assert_not_called()
    assert db.execute.call_count == 1
    assert db.execute.call_args.args[0].sql == sql


def test_top_earners_per_department_refuses_global_limit(app, fake_model):
    at, db = app
    fake_model.generate.return_value = (
        "SELECT name, department, salary FROM employees_messy ORDER BY salary DESC LIMIT 5",
        "Here are the highest earners.",
    )
    submit_prompt(at, "top 5 earners in each department")
    assert not at.exception
    assert db.execute.call_count == 0
    assert "nothing was executed" in " ".join(element.value for element in at.markdown).lower()


def test_history_restores_without_reexecuting(app):
    at, db = app
    first = "SELECT COUNT(*) FROM employees_messy"
    submit_prompt(at, first)
    button(at, "New chat").click().run()
    assert not at.chat_message
    second = "SELECT AVG(salary) FROM employees_messy"
    submit_prompt(at, second)
    assert db.execute.call_count == 2
    button(at, first).click().run()
    assert not at.exception
    assert any(t.value == first for t in at.text)
    assert not any(t.value == second for t in at.text)
    assert db.execute.call_count == 2
    button(at, second).click().run()
    assert any(t.value == second for t in at.text)
    assert db.execute.call_count == 2


def test_switch_cancels_pending_mutation(app):
    at, db = app
    first = "SELECT COUNT(*) FROM employees_messy"
    submit_prompt(at, first)
    button(at, "New chat").click().run()
    submit_prompt(at, "DROP TABLE employees_messy")
    assert button(at, "Confirm & execute once").disabled
    button(at, first).click().run()
    assert not any(b.label == "Confirm & execute once" for b in at.button)
    assert db.execute.call_count == 1


def test_new_chat_preserves_history_without_reexecuting(app):
    at, db = app
    first = "SELECT COUNT(*) FROM employees_messy"
    submit_prompt(at, first)
    original = at.session_state["active_chat"]
    button(at, "New chat").click().run()
    assert not at.chat_message
    assert len(at.session_state["chats"]) == 2
    at.button(key=f"history_{original}").click().run()
    assert not at.exception
    assert at.session_state["messages"][0]["text"] == first
    db.execute.assert_called_once()


def test_edit_question_replaces_turn_without_replaying_history(app):
    at, db = app
    original = "SELECT COUNT(*) FROM employees_messy"
    submit_prompt(at, original)
    submit_prompt(at, "SELECT AVG(salary) FROM employees_messy")
    assert db.execute.call_count == 2
    at.text_area[0].set_value("SELECT MAX(salary) FROM employees_messy")
    next(b for b in at.button if b.label == "Save & resend").click().run()
    assert not at.exception
    assert db.execute.call_count == 3
    messages = at.session_state["messages"]
    assert len(messages) == 2
    assert messages[0]["text"] == "SELECT MAX(salary) FROM employees_messy"
    assert any(e.type == "iframe" for e in at.get("iframe"))


def test_edit_mutation_still_needs_new_confirmation(app):
    at, db = app
    submit_prompt(at, "SELECT COUNT(*) FROM employees_messy")
    at.text_area[0].set_value("DELETE FROM employees_messy")
    next(b for b in at.button if b.label == "Save & resend").click().run()
    assert not at.exception
    assert db.execute.call_count == 1
    assert button(at, "Confirm & execute once").disabled


def test_configured_scope_fails_closed_and_cancels_pending_write(app):
    at, db = app
    assert at.session_state["service"].scope_table == "employees_messy"
    submit_prompt(at, "DELETE FROM employees_messy")
    assert button(at, "Confirm & execute once").disabled
    # Another accessible table must not become a fallback for a missing configured table.
    db.discover_schema.return_value = Schema("hr_demo", "employees_messy", (), (), (), "8",
        tables=({"name": "other", "columns": [{"name": "id", "type": "INT"}]},))
    at.run()
    assert not at.exception
    assert at.chat_input[0].disabled
    assert any("Configured table 'employees_messy' is unavailable" in e.value for e in at.error)
    assert at.session_state["service"].pending is None
    assert "pending_prompt" not in at.session_state
    assert not any(b.label == "Confirm & execute once" for b in at.button)
    assert at.session_state["service"].scope_table == "employees_messy"
    db.execute.assert_not_called()
    db.list_databases.assert_not_called()
    at.run()
    db.execute.assert_not_called()


def test_lookup_has_no_chart_tabs(app):
    at, db = app
    db.execute.return_value = QueryResult(["name", "city", "salary"], [["Alex", "Delhi", 100]], 0, False, .01)
    submit_prompt(at, "SELECT * FROM employees_messy WHERE phone = '123'")
    assert not at.exception
    assert not at.tabs
    assert at.session_state["messages"][-1]["result"].rows == [["Alex", "Delhi", 100]]
    assert "Alex" in " ".join(element.value for element in at.get("html"))
    assert not at.dataframe


def test_no_unexpected_sidebar_controls(app):
    at, db = app
    captions = " ".join(c.value for c in at.caption)
    assert "Appearance:" not in captions
    assert "tables · Joins" not in captions
    assert "History stays" not in captions
    assert [s.label for s in at.sidebar.selectbox] == ["User"]
    assert at.sidebar.selectbox[0].value == "owner"
    assert [t.label for t in at.sidebar.text_input] == ["User name"]
    assert [b.label for b in at.sidebar.button] == ["Add user", "New chat", "New chat"]
    assert [m.value for m in at.sidebar.markdown] == ["**Recents**"]
    for kind in ("radio", "checkbox", "toggle", "multiselect", "text_area", "expander"):
        assert not at.sidebar.get(kind)
    assert [t.label for t in at.toggle] == ["Dark mode"]
    assert not at.radio
    db.list_databases.assert_not_called()
    db.execute.assert_not_called()


def test_insightdock_empty_state_shows_brand_and_examples_without_running_queries(app):
    at, db = app
    assert not at.exception
    body = " ".join(element.value for element in at.markdown)
    assert "InsightDock" in body
    assert "MySQL analytics assistant" in body
    assert "Meet your data" in body
    assert "A clearer view of your data" in body
    assert {"How many employees are there?", "Compare average salary by department",
            "Show hiring trends by month"}.issubset({button.label for button in at.button})
    db.execute.assert_not_called()
    at.session_state.messages.append({"role": "user", "text": "Existing question", "id": "turn-1"})
    at.run()
    assert "Meet your data" not in " ".join(element.value for element in at.markdown)


def test_example_prompt_uses_the_existing_chat_submission_flow(app):
    at, db = app
    with patch("chat_app.st.rerun", st.stop):
        button(at, "Compare average salary by department").click().run()
    assert not at.exception
    assert at.session_state.get("edited_prompt") == "Compare average salary by department"
    db.execute.assert_not_called()
    with patch("chat_app.st.rerun", st.stop):
        at.run()
    assert at.session_state.messages[-1]["role"] == "user"
    assert at.session_state.messages[-1]["text"] == "Compare average salary by department"
    assert at.session_state.get("queued_chat_prompt") == "Compare average salary by department"
    db.execute.assert_not_called()


def add_user(at, name):
    next(t for t in at.text_input if t.label == "User name").set_value(name)
    button(at, "Add user").click().run()


def test_user_profiles_separate_history_and_cancel_approval(app):
    at, db = app
    question = "SELECT COUNT(*) FROM employees_messy"
    submit_prompt(at, question)
    submit_prompt(at, "DELETE FROM employees_messy")
    assert button(at, "Confirm & execute once").disabled
    add_user(at, "Professor")
    assert not at.exception
    assert not at.chat_message
    assert at.session_state["service"].pending is None
    professor_id = at.session_state["active_profile"]
    assert professor_id != "owner"
    assert next(s for s in at.selectbox if s.label == "User").value == professor_id
    submit_prompt(at, "SELECT AVG(salary) FROM employees_messy")
    next(s for s in at.selectbox if s.label == "User").set_value("owner").run()
    assert not at.exception
    assert any(t.value == question for t in at.text)
    assert not any(b.label == "Confirm & execute once" for b in at.button)
    assert db.execute.call_count == 2
    next(s for s in at.selectbox if s.label == "User").set_value(professor_id).run()
    assert any(t.value == "SELECT AVG(salary) FROM employees_messy" for t in at.text)
    assert db.execute.call_count == 2


def test_duplicate_and_blank_users_rejected(app):
    at, _ = app
    add_user(at, "   ")
    assert any("Enter a name" in w.value for w in at.warning)
    add_user(at, "fawaz khan")
    assert any("already exists" in w.value for w in at.warning)
    assert len(at.session_state["profiles"]) == 1


def test_theme_toggle_preserves_chat_and_approval_without_io(app, fake_model, email_delivery):
    at, db = app
    build_snapshot_report(at)
    submit_prompt(at, "DELETE FROM employees_messy")
    chat = at.session_state["chats"][at.session_state["active_chat"]]
    messages = list(at.session_state["messages"])
    pending = at.session_state["service"].pending
    report = chat["reports"][0]
    pdf = chat["report_pdfs"][report.id]
    generate_calls = fake_model.generate.call_count
    for dark in (True, False):
        next(t for t in at.toggle if t.label == "Dark mode").set_value(dark).run()
        assert not at.exception
        assert at.session_state["dark_mode"] is dark
        assert at.session_state["messages"] == messages
        assert at.session_state["service"].pending is pending
        assert chat["reports"] == [report]
        assert chat["report_pdfs"][report.id] is pdf
        assert button(at, "Confirm & execute once").disabled
    db.execute.assert_called_once()
    assert fake_model.generate.call_count == generate_calls
    fake_model.summarize.assert_not_called()
    fake_model.plan_report.assert_not_called()
    email_delivery.assert_not_called()


@pytest.fixture
def pay_app(app, monkeypatch):
    at, db = app
    schema = replace(db.discover_schema.return_value, columns=(
        {"name": "department", "type": "VARCHAR(80)"},
        {"name": "salary", "type": "DECIMAL(12,2)"},
        {"name": "status", "type": "VARCHAR(20)"},
        {"name": "city", "type": "VARCHAR(80)"},
        {"name": "join_date", "type": "DATE"},
    ))
    db.discover_schema.return_value = schema
    question = "Which departments and roles have the highest and lowest salaries?"
    plan = automatic_queries(schema, question)
    results = {
        ("employee_count",): [[10]],
        ("department", "employee_count"): [["Sales", 5], ["Engineering", 3], [None, 2]],
        ("department", "employee_count", "salary_count", "average_salary"): [
            ["Sales", 5, 2, Decimal("200")], ["Engineering", 3, 1, Decimal("300")], [None, 2, 0, None]],
        ("employee_count", "salary_count", "average_positive_salary", "total_positive_salary",
         "missing_salary_count", "nonpositive_salary_count"): [[10, 3, Decimal("233.333333"), 700, 4, 3]],
    }
    approved = {}
    for query in plan:
        checked = validate_sql(query.sql, schema.database, schema.table)
        aliases = tuple(p.alias_or_name for p in sqlglot.parse_one(query.sql, read="mysql").expressions)
        approved[checked.sql] = (aliases, results[aliases])

    def execute(query, fresh_schema):
        assert query.read_only
        assert fresh_schema.fingerprint == schema.fingerprint
        assert query.sql in approved, "Mock results must match an approved query, not a generic count"
        aliases, rows = approved[query.sql]
        return QueryResult(list(aliases), [list(row) for row in rows], 0, False, .01)

    db.execute.side_effect = execute
    model = Mock(side_effect=AssertionError("Pay reports must not call an external model"))
    monkeypatch.setattr("chatbot.report_ui.ChatModel", model)
    at.run()
    assert not at.exception
    yield at, db, plan, question
    model.assert_not_called()


@pytest.mark.parametrize("history,source,extra_reads", [
    ("absent", "live", 4), ("headcount", "live", 4), ("salary", "snapshot", 0),
])
def test_question_led_pay_report_tables_live_fallback_and_snapshot_reuse(pay_app, history, source, extra_reads):
    at, db, plan, question = pay_app
    if history != "absent":
        query = next(q for q in plan if "GROUP BY `department`" in q.sql
                     and ("AS average_salary" in q.sql) == (history == "salary"))
        submit_prompt(at, query.sql)
        assert not at.exception
    else:
        assert not at.session_state["messages"]
    initial_reads = db.execute.call_count
    submit_prompt(at, "Create a report: " + question)
    assert not at.exception
    chat = at.session_state["chats"][at.session_state["active_chat"]]
    assert len(chat["reports"]) == 1
    report = chat["reports"][0]
    assert report.title == "Department Pay Report"
    assert report.question == "Create a report: " + question and report.source == source
    assert not chat["report_open"] and "report_plan" not in chat
    assert db.execute.call_count == initial_reads + extra_reads
    assert len(report.evidence) == (1 if source == "snapshot" else 4)
    assert not any("`status`" in e.sql or "`city`" in e.sql or "DATE_FORMAT" in e.sql for e in report.evidence)
    assert at.session_state["messages"][-2]["text"] == "Create a report: " + question
    assert at.session_state["messages"][-1] == {
        "role": "assistant", "report_id": report.id, "text": ""}
    assert "Department Pay Report" in [s.value for s in at.subheader]
    assert "Department Salary Profile" in [s.value for s in at.subheader]
    assert "Question: Create a report: " + question in [c.value for c in at.caption]
    profile_tables = [d.value for d in at.get("html") if "Positive salary observations" in d.value
                      and "<th>Department</th>" in d.value]
    assert len(profile_tables) == 1
    table = profile_tables[0]
    for label in ("Employee count", "Average salary", "Percentage of returned total"):
        assert f"<th>{label}</th>" in table
    assert "<td>Sales</td><td>5</td><td>2</td><td>200</td><td>50%</td>" in table
    assert "<td>Engineering</td><td>3</td><td>1</td><td>300</td><td>30%</td>" in table
    assert "<td>Missing</td><td>2</td><td>0</td><td>Missing</td><td>20%</td>" in table
    visible = " ".join(t.value for t in at.text)
    assert "Engineering has the highest average salary (300)" in visible
    assert "Sales has the lowest (200)" in visible
    assert "Salary averages exclude missing and nonpositive salaries" not in visible
    assert "No job-title or role evidence is available" not in visible
    assert "Partial report" not in visible and "truncated" not in visible.lower()
    assert build_document(report).findings[0].startswith("Among returned groups, Engineering")
    # The primary report chart's Details table is the average-only projection.
    primary_chart = next(s for s in build_document(report).sections if s.title == "Department Salary Profile")
    assert primary_chart.chart_result.columns == ["department", "average_salary"]
    assert [row[1] for row in primary_chart.chart_result.rows[:2]] == ["200", "300"]
    from html.parser import HTMLParser

    class TableCells(HTMLParser):
        def __init__(self):
            super().__init__()
            self.cells = []

        def handle_data(self, data):
            self.cells.append(data)

    tables = []
    for element in at.get("html"):
        parser = TableCells()
        parser.feed(element.value)
        tables.append(parser.cells)
    assert any(cells[:2] == ["department", "average_salary"] and
               cells[2:6] == ["Sales", "200", "Engineering", "300"] for cells in tables)
    at.run()
    assert not at.exception
    assert db.execute.call_count == initial_reads + extra_reads
    assert at.session_state["chats"][at.session_state["active_chat"]]["reports"] == [report]