"""Cloud reports never use disk recipient preferences or SMTP."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from chatbot import report_ui as ui
from chatbot.cloud_access import CloudAccessError
from chatbot.config import Settings
from chatbot.db import QueryResult, Schema
from chatbot.reports import ReportError, ReportEvidence, build_report, utc_now


@pytest.fixture
def cloud(monkeypatch):
    settings = Settings("localhost", 3306, "user", "secret", "demo", table="employees", cloud_mode=True)
    schema = Schema("demo", "employees", ({"name": "department", "type": "VARCHAR"},), (), (), "8")
    evidence = ReportEvidence("q1", "Department headcount",
                              "SELECT department, COUNT(*) AS employee_count FROM employees GROUP BY department",
                              QueryResult(["department", "employee_count"], [["IT", 2]], 0, False, .1),
                              utc_now(), "snapshot")
    report = build_report("Department report", (evidence,), scope=("demo", "employees"), source="snapshot")
    runner = Mock(scope=report.scope, scope_label="demo.employees")
    runner.snapshot.return_value = report
    runner.live.return_value = report
    service = SimpleNamespace(settings=settings, scope_table="employees", database=Mock(), cancel=Mock())
    screen = Mock()
    # Deliberately no local profiles/active_profile: cloud must never need them.
    screen.session_state = {"_cloud_owner": "owner", "_cloud_settings": settings}
    screen.container.side_effect = lambda **kw: nullcontext()
    screen.expander.side_effect = lambda *args, **kw: nullcontext()
    screen.spinner.side_effect = lambda *args, **kw: nullcontext()
    screen.button.return_value = True  # Even malicious/stale Send clicks cannot send.
    monkeypatch.setattr(ui, "st", screen)
    check = Mock()
    monkeypatch.setattr("chatbot.cloud_ui._check_session", check)
    store = Mock(side_effect=AssertionError("No disk preferences in cloud"))
    email_config = Mock(side_effect=AssertionError("No SMTP config in cloud"))
    sender = Mock(side_effect=AssertionError("No SMTP in cloud"))
    export = Mock(return_value=b"%PDF-cloud")
    monkeypatch.setattr(ui, "RecipientStore", store)
    monkeypatch.setattr(ui.EmailSettings, "from_env", email_config)
    monkeypatch.setattr(ui, "send_report_pdf", sender)
    monkeypatch.setattr(ui, "report_pdf", export)
    monkeypatch.setattr(ui, "client_table", Mock())
    chat = {"report_runner": runner, "reports": [report], "messages": []}
    return SimpleNamespace(settings=settings, schema=schema, report=report, runner=runner, service=service,
                           screen=screen, check=check, store=store, sender=sender,
                           email_config=email_config, export=export, chat=chat)


def render(cloud, **kwargs):
    ui.render_reports(cloud.chat, cloud.service, cloud.schema, Mock(),
                      consent=False, ai_summary=False, **kwargs)


@pytest.mark.parametrize("targeted", [False, True])
def test_cloud_download_without_local_preferences_or_email(cloud, targeted):
    render(cloud, **({"report_id": cloud.report.id} if targeted else {}))
    cloud.export.assert_called_once_with(cloud.report)
    cloud.screen.download_button.assert_called_once()
    cloud.screen.caption.assert_any_call(
        "Email delivery is unavailable in this cloud pilot. Download the PDF to share it.")
    assert not any(call.args[0] == "Send to email" for call in cloud.screen.button.call_args_list)
    cloud.screen.text_input.assert_not_called()
    cloud.store.assert_not_called()
    cloud.email_config.assert_not_called()
    cloud.sender.assert_not_called()
    assert cloud.check.call_count >= 3


def test_stale_local_recipient_callback_does_not_write_in_cloud(cloud):
    cloud.screen.session_state["recipient"] = "private@example.com"
    ui._save_recipient("Local user", "recipient")
    cloud.store.assert_not_called()


@pytest.mark.parametrize("snapshot", [False, True])
def test_direct_build_guard_precedes_snapshot_or_live_io(cloud, snapshot):
    if snapshot:
        item = cloud.report.evidence[0]
        cloud.chat["messages"] = [{"query_id": item.id, "question": item.question, "read_only": True,
                                  "sql": item.sql, "result": item.result, "scope": cloud.runner.scope,
                                  "executed_at": item.executed_at}]
    cloud.check.side_effect = CloudAccessError("SECRET")
    with pytest.raises(ReportError) as error:
        ui.build_report(cloud.chat, cloud.service, cloud.runner, cloud.schema,
                        "Department report", consent=False)
    assert error.value.__cause__ is None and "SECRET" not in str(error.value)
    cloud.runner.snapshot.assert_not_called()
    cloud.runner.live.assert_not_called()


@pytest.mark.parametrize("cached_pdf", [False, True])
def test_expired_report_session_never_renders_or_exports(cloud, cached_pdf):
    cloud.chat["report_open"] = True
    if cached_pdf:
        cloud.chat["report_pdfs"] = {cloud.report.id: b"%PDF-cached"}
    cloud.check.side_effect = CloudAccessError("SECRET")
    render(cloud)
    cloud.screen.container.assert_not_called()
    cloud.screen.download_button.assert_not_called()
    cloud.export.assert_not_called()
    cloud.runner.snapshot.assert_not_called()
    cloud.runner.live.assert_not_called()
    assert not cloud.chat["report_open"]
    assert "SECRET" not in str(cloud.screen.warning.call_args_list)


def test_expiry_immediately_before_pdf_build_blocks_io(cloud):
    cloud.check.side_effect = [None, None, CloudAccessError("SECRET")]
    render(cloud)
    cloud.export.assert_not_called()
    cloud.screen.download_button.assert_not_called()


def test_expiry_after_pdf_build_blocks_download(cloud):
    cloud.check.side_effect = [None, None, None, CloudAccessError("SECRET")]
    render(cloud)
    cloud.export.assert_called_once()
    cloud.screen.download_button.assert_not_called()


def test_cached_pdf_rechecks_lease_before_download(cloud):
    cloud.chat["report_pdfs"] = {cloud.report.id: b"%PDF-cached"}
    cloud.check.side_effect = [None, None, CloudAccessError("SECRET")]
    render(cloud)
    cloud.export.assert_not_called()
    cloud.screen.download_button.assert_not_called()