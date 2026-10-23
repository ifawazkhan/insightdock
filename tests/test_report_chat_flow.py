"""Chat-only report UI regressions; no provider, database or SMTP connections."""
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from chatbot import report_ui as ui
from chatbot.config import Settings
from chatbot.db import DatabaseError, QueryResult, Schema
from chatbot.llm import ModelError
from chatbot.report_document import build_document
from chatbot.report_email import DeliveryUncertain, EmailError, EmailSettings, RecipientStore
from chatbot.report_service import ReportService
from chatbot.reports import ReportError, ReportEvidence, build_report, utc_now
from chatbot.sql_policy import PolicyError


PDF = b"%PDF-1.4\nreport\n%%EOF"


class State(dict):
    def __getattr__(self, name):
        return self[name]


class FakeUI:
    def __init__(self):
        self.session_state = State(active_chat="chat", active_profile="profile",
                                   profiles={"profile": {"name": "Local user"}})
        self.events = []
        self.panels = []
        self.clicks = set()
        self.rerun = Mock()

    @contextmanager
    def container(self, **kwargs):
        self.events.append(("container_start", kwargs["key"], tuple(self.panels), kwargs))
        try:
            yield
        finally:
            self.events.append(("container_end", kwargs["key"], tuple(self.panels), kwargs))

    @contextmanager
    def spinner(self, text):
        yield

    @contextmanager
    def expander(self, label, **kwargs):
        self.events.append(("expander", label, tuple(self.panels), kwargs))
        self.panels.append(label)
        try:
            yield
        finally:
            self.panels.pop()

    @contextmanager
    def popover(self, label, **kwargs):
        self.events.append(("popover", label, tuple(self.panels), kwargs))
        self.panels.append(label)
        try:
            yield
        finally:
            self.panels.pop()

    def text_input(self, label, *, key, **kwargs):
        self.events.append(("input", label, tuple(self.panels), {"key": key, **kwargs}))
        return self.session_state.get(key, "")

    def button(self, label, *, key, disabled=False, **kwargs):
        self.events.append(("button", label, tuple(self.panels), {"disabled": disabled}))
        clicked = key in self.clicks
        self.clicks.discard(key)
        return clicked and not disabled

    def __getattr__(self, kind):
        def record(value, **kwargs):
            self.events.append((kind, value, tuple(self.panels), kwargs))
        return record


@pytest.fixture
def flow(monkeypatch, tmp_path):
    monkeypatch.setattr("socket.create_connection", Mock(side_effect=AssertionError("No network")))
    screen = FakeUI()
    monkeypatch.setattr(ui, "st", screen)
    settings = Settings("localhost", 3306, "test", "secret", "demo")
    schema = Schema("demo", settings.table, (
        {"name": "department", "type": "VARCHAR(80)"},
        {"name": "salary", "type": "INT"},), (), (), "8")
    database = Mock()
    database.discover_schema.side_effect = AssertionError("No DB discovery")
    database.execute.side_effect = AssertionError("No DB execution")
    service = SimpleNamespace(settings=settings, database=database, scope_table=settings.table,
                              cancel=Mock())
    real_runner = ReportService(settings, database)
    runner = Mock(scope=real_runner.scope, scope_label=real_runner.scope_label)
    runner.snapshot.side_effect = real_runner.snapshot
    evidence = ReportEvidence("q1", "Department headcount",
                              f"SELECT department, COUNT(*) AS employee_count FROM {settings.table} GROUP BY department",
                              QueryResult(["department", "employee_count"], [["Sales", 4], ["IT", 2]], 0, False, .1),
                              utc_now(), "snapshot")
    report = build_report("Department report", (evidence,), scope=runner.scope, source="snapshot")
    runner.live.return_value = replace(report, source="live")
    chat = {"messages": [], "report_runner": runner}
    store = RecipientStore(tmp_path / "recipients.json")
    monkeypatch.setattr(ui, "RecipientStore", lambda: store)
    monkeypatch.setattr(ui.EmailSettings, "from_env", Mock(return_value=EmailSettings()))
    model = Mock(side_effect=AssertionError("No provider"))
    monkeypatch.setattr(ui, "ChatModel", model)
    export = Mock(return_value=PDF)
    sender = Mock()
    monkeypatch.setattr(ui, "report_pdf", export)
    monkeypatch.setattr(ui, "send_report_pdf", sender)
    monkeypatch.setattr(ui, "client_table", screen.dataframe)
    chart = Mock()

    def render(consent=False):
        ui.render_reports(chat, service, schema, chart, consent=consent, ai_summary=False)

    return SimpleNamespace(screen=screen, service=service, schema=schema, runner=runner,
                           chat=chat, store=store, report=report, export=export,
                           sender=sender, model=model, chart=chart, render=render)


def retained(flow, **changes):
    item = flow.report.evidence[0]
    return {"query_id": item.id, "question": item.question, "read_only": True,
            "sql": item.sql, "result": item.result, "scope": flow.runner.scope,
            "executed_at": item.executed_at, **changes}


def test_open_queues_one_bounded_chat_job_without_io(flow):
    flow.chat.update(report_plan="old", report_notice="old")
    flow.chat["messages"] = [retained(flow), {"role": "user", "question": "Build report"}]
    ui.open_report(flow.chat)
    assert flow.chat["report_open"] and flow.chat["report_topic"] == "Department headcount"
    assert "report_plan" not in flow.chat and "report_notice" not in flow.chat
    ui.open_report(flow.chat, "x" * 220)
    assert flow.chat["report_topic"] == "x" * 200
    flow.runner.live.assert_not_called()
    flow.runner.snapshot.assert_not_called()
    flow.service.cancel.assert_not_called()


def test_snapshot_builds_immediately_once_and_full_report_stays_visible(flow):
    flow.chat["messages"] = [retained(flow), {"role": "user", "question": "Department report"}]
    ui.open_report(flow.chat, "Department report")
    flow.screen.session_state.update(pending_prompt="write", edited_prompt="old")
    flow.service.cancel.side_effect = lambda: (
        pytest.fail("Job not consumed before I/O") if flow.chat["report_open"] else None)
    flow.render()
    report = flow.chat["reports"][0]
    assert report.source == "snapshot" and report.question == "Department report"
    assert not flow.chat["report_open"] and "report_topic" not in flow.chat
    assert "pending_prompt" not in flow.screen.session_state
    assert "edited_prompt" not in flow.screen.session_state
    for _ in range(3):
        flow.render()
    flow.runner.snapshot.assert_called_once()
    flow.runner.live.assert_not_called()
    flow.model.assert_not_called()
    flow.export.assert_called_once_with(report)
    flow.sender.assert_not_called()
    assert len(flow.chat["reports"]) == 1
    assert flow.chart.call_count == 4
    assert any(e[0] == "dataframe" for e in flow.screen.events)
    downloads = [e for e in flow.screen.events if e[0] == "download_button"]
    assert len(downloads) == 4 and all(e[3]["data"] is PDF for e in downloads)
    assert all(e[3]["on_click"] == "ignore" for e in downloads)
    assert not any(e[1] in {"Report focus (optional)", "Build report", "Build a business report"}
                   for e in flow.screen.events if isinstance(e[1], str))
    flow.screen.rerun.assert_not_called()


def test_live_job_is_consumed_before_execution_and_uses_new_run_ids(flow):
    def live(title, queries, run_id):
        assert not flow.chat["report_open"] and "report_topic" not in flow.chat
        assert queries and len(run_id) == 32
        return replace(flow.report, id=run_id, source="live")
    flow.runner.live.side_effect = live
    for _ in range(2):
        ui.open_report(flow.chat, "Latest department report")
        flow.render()
        flow.render()
    assert flow.runner.live.call_count == 2
    assert len({c.args[2] for c in flow.runner.live.call_args_list}) == 2
    flow.runner.snapshot.assert_not_called()


@pytest.mark.parametrize("error", [ReportError, PolicyError, ModelError, DatabaseError])
def test_failed_job_closes_without_retry_or_secret_details(flow, error):
    flow.runner.live.side_effect = error("SECRET")
    ui.open_report(flow.chat, "Latest workforce report")
    flow.render()
    flow.render()
    flow.runner.live.assert_called_once()
    assert not flow.chat["report_open"] and not flow.chat.get("reports")
    warnings = [e[1] for e in flow.screen.events if e[0] == "warning"]
    assert len(warnings) == 1 and "SECRET" not in warnings[0]


def test_constraints_without_consent_never_run_a_broad_replacement(flow):
    ui.open_report(flow.chat, "Department salary report excluding IT")
    flow.render()
    flow.render()
    flow.runner.live.assert_not_called()
    flow.model.assert_not_called()
    assert not flow.chat["report_open"]


def test_pay_request_does_not_reuse_department_counts(flow):
    flow.chat["messages"] = [retained(flow)]
    ui.open_report(flow.chat, "Department salary report")
    flow.render()
    flow.runner.snapshot.assert_called_once()
    flow.runner.live.assert_called_once()


def test_build_helper_is_pure_ui_free_and_preserves_snapshot_safety(flow):
    flow.chat["messages"] = [retained(flow)]
    report = ui.build_report(flow.chat, flow.service, flow.runner, flow.schema,
                             "Department report", consent=False)
    assert report.title == "Department Composition Report"
    assert report.question == "Department report"
    assert not flow.screen.events and "reports" not in flow.chat
    flow.chat["messages"] = [retained(flow, scope=("other", flow.service.settings.table))]
    ui.build_report(flow.chat, flow.service, flow.runner, flow.schema,
                    "Department report", consent=False)
    flow.runner.live.assert_called_once()


def test_report_interface_omits_partial_and_limitations_commentary(flow, monkeypatch):
    flow.chat["reports"] = [flow.report]
    document = replace(build_document(flow.report), recommendations=(), follow_up="",
                       summary="Partial report: some requested evidence is unavailable.",
                       limitations=("Detailed limitation",))
    monkeypatch.setattr(ui, "build_document", lambda _: document)
    flow.render()
    headings = [e[1] for e in flow.screen.events if e[0] == "subheader"]
    assert not {"Recommendations", "Suggested follow-up", "Data limitations"} & set(headings)
    limits = [e for e in flow.screen.events if e[0] == "text" and "Detailed limitation" in e[1]]
    assert not limits
    assert any(e[0] == "text" and e[1] == "Here are the available results." and not e[2]
               for e in flow.screen.events)


def test_email_dropdown_saved_recipient_and_no_smtp_download(flow):
    flow.store.save("Local user", "saved@example.com")
    flow.chat["reports"] = [flow.report]
    flow.render()
    panels = [e for e in flow.screen.events if e[0] == "popover" and e[1] == "Send to email"]
    assert len(panels) == 1
    key = f"report_recipient_profile_{flow.report.id}"
    assert flow.screen.session_state[key] == "saved@example.com"
    field = next(e for e in flow.screen.events if e[0] == "input")
    assert field[2] == ("Send to email",) and field[3]["on_change"] is ui._save_recipient
    assert any(e[0] == "warning" and "SMTP settings" in e[1] and e[2] == ("Send to email",)
               for e in flow.screen.events)
    assert any(e[0] == "download_button" for e in flow.screen.events)
    assert next(e for e in flow.screen.events if e[0] == "button" and e[1] == "Send")[3]["disabled"]
    flow.screen.session_state[key] = "new@example.com"
    ui._save_recipient("Local user", key)
    assert flow.store.read("Local user") == "new@example.com"
    flow.screen.session_state[key] = ""
    ui._save_recipient("Local user", key)
    assert flow.store.read("Local user") == ""
    flow.sender.assert_not_called()


@pytest.mark.parametrize("targeted", [False, True])
def test_email_settings_follow_full_report_inside_each_container(flow, monkeypatch, targeted):
    reports = [replace(flow.report, id=f"report-{index}") for index in range(2)]
    flow.chat["reports"] = reports
    flow.store.save("Local user", "saved@example.com")
    read = Mock(wraps=flow.store.read)
    monkeypatch.setattr(flow.store, "read", read)

    def render():
        if targeted:
            for report in reports:
                ui.render_reports(flow.chat, flow.service, flow.schema, flow.chart,
                                  consent=False, ai_summary=False, report_id=report.id)
        else:
            flow.render()

    render()
    events = flow.screen.events
    keys = []
    for report in reports:
        container_key = f"business_report_{report.id}"
        start = next(i for i, e in enumerate(events) if e[:2] == ("container_start", container_key))
        end = next(i for i, e in enumerate(events) if e[:2] == ("container_end", container_key))
        content = events[start + 1:end]
        title = next(i for i, e in enumerate(content) if e[:2] == ("subheader", report.title))
        findings = next(i for i, e in enumerate(content) if e[:2] == ("subheader", "Key findings"))
        sources = next(i for i, e in enumerate(content) if e[:2] == ("expander", "Sources & details"))
        download = next(i for i, e in enumerate(content) if e[0] == "download_button")
        email = next(i for i, e in enumerate(content) if e[:2] == ("popover", "Send to email"))
        send = next(i for i, e in enumerate(content) if e[:2] == ("button", "Send"))
        assert title < findings < sources < download < email < send
        assert content[email][2:] == ((), {"key": f"report_email_{report.id}", "width": "content"})
        field = next(e for e in content if e[0] == "input")
        key = f"report_recipient_profile_{report.id}"
        keys.append(field[3]["key"])
        assert field[3]["key"] == key
        assert field[3]["args"] == ("Local user", key)
        assert flow.screen.session_state[key] == "saved@example.com"
    assert len(set(keys)) == 2
    assert read.call_count == 2
    assert ui.EmailSettings.from_env.call_count == (2 if targeted else 1)
    flow.screen.session_state[keys[0]] = "changed@example.com"
    ui._save_recipient("Local user", keys[0])
    render()
    assert read.call_count == 2
    assert flow.screen.session_state[keys[1]] == "saved@example.com"
    assert flow.export.call_count == 2
    flow.sender.assert_not_called()
    flow.runner.live.assert_not_called()
    flow.runner.snapshot.assert_not_called()


@pytest.mark.parametrize("outcome", ["accepted", "uncertain"])
def test_explicit_send_consumed_before_io_and_never_replayed(flow, monkeypatch, outcome):
    flow.chat["reports"] = [flow.report]
    settings = EmailSettings(host="smtp.example.test", from_address="sender@example.test")
    monkeypatch.setattr(ui.EmailSettings, "from_env", Mock(return_value=settings))
    flow.screen.session_state[f"report_recipient_profile_{flow.report.id}"] = "new@EXAMPLE.COM"
    attempt = (flow.report.id, "new@example.com")

    def send(*args):
        assert flow.chat["report_email_attempts"][attempt] == "sending"
        if outcome == "uncertain":
            raise DeliveryUncertain("Unknown outcome")

    flow.sender.side_effect = send
    flow.render()
    flow.sender.assert_not_called()
    for _ in range(3):
        flow.screen.clicks.add(f"report_email_send_{flow.report.id}")
        flow.render()
    flow.sender.assert_called_once_with(PDF, flow.report.title, "new@example.com", settings)
    assert flow.chat["report_email_attempts"][attempt] == outcome
    assert flow.store.read("Local user") == "new@example.com"
    flow.export.assert_called_once()


def test_failed_send_is_not_retried_on_rerender(flow, monkeypatch):
    flow.chat["reports"] = [flow.report]
    monkeypatch.setattr(ui.EmailSettings, "from_env", Mock(return_value=EmailSettings(
        host="smtp.example.test", from_address="sender@example.test")))
    flow.screen.session_state[f"report_recipient_profile_{flow.report.id}"] = "new@example.com"
    flow.sender.side_effect = EmailError("Sending failed")
    flow.screen.clicks.add(f"report_email_send_{flow.report.id}")
    flow.render()
    flow.render()
    flow.sender.assert_called_once()
    assert flow.chat["report_email_attempts"][(flow.report.id, "new@example.com")] == "failed"


def test_report_history_prunes_cached_artifacts(flow):
    old = [replace(flow.report, id=str(i)) for i in range(3)]
    flow.chat.update(reports=old, report_pdfs={r.id: PDF for r in old},
                     report_pdf_errors={"0"}, report_email_attempts={("0", "a@example.com"): "accepted"})
    ui.open_report(flow.chat, "Latest department report")
    flow.render()
    assert [r.id for r in flow.chat["reports"]] == ["1", "2", flow.report.id]
    assert "0" not in flow.chat["report_pdfs"] and "0" not in flow.chat["report_pdf_errors"]
    assert not flow.chat["report_email_attempts"]