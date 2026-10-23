"""One-click, scope-bound business reports; rerendering never refreshes data."""
from uuid import uuid4
from dataclasses import replace
import re

import streamlit as st
import pandas as pd
from chatbot.client_theme import client_table

from chatbot.cloud_access import CloudAccessError
from chatbot.db import DatabaseError
from chatbot.llm import ChatModel, ModelError
from chatbot.report_automation import automatic_queries, relevant_messages, report_title
from chatbot.report_document import build_document
from chatbot.report_export import report_pdf
from chatbot.report_email import (DeliveryUncertain, EmailError, EmailSettings,
                                  RecipientStore, normalize_email, send_report_pdf)
from chatbot.report_service import ReportService
from chatbot.reports import ReportError, aggregate_result
from chatbot.sql_policy import PolicyError


def open_report(chat: dict, topic: str = "") -> None:
    """Queue one chat-bound job; opening a report performs no I/O."""
    if not topic:
        # A report opened after a conversation should inherit the actual
        # business question rather than silently become a generic overview.
        topic = next((m.get("question", "") for m in reversed(chat.get("messages", []))
                      if m.get("read_only") is True and m.get("question")
                      and not re.match(r"\s*(SELECT|WITH)\b", m["question"], re.I)), "")
    chat["report_topic"] = topic[:200]
    chat["report_open"] = True
    chat.pop("report_plan", None)
    chat.pop("report_notice", None)


def _save_recipient(profile_name: str, widget_key: str) -> None:
    # A stale local widget callback must not persist cloud-session preferences.
    if "_cloud_owner" in st.session_state or getattr(st.session_state.get("_cloud_settings"), "cloud_mode", False):
        return
    try:
        RecipientStore().save(profile_name, st.session_state[widget_key])
        st.session_state[widget_key + "_notice"] = "Email preference saved on this device."
    except EmailError as exc:
        st.session_state[widget_key + "_notice"] = str(exc)


def _check_report_access(settings) -> None:
    """Validate snapshots/exports too, without charging a nonexistent report quota."""
    if settings.cloud_mode:
        from chatbot.cloud_ui import _check_session
        try:
            _check_session()
        except CloudAccessError:
            raise ReportError("Report access is unavailable. Sign in again.") from None


def build_report(chat: dict, service, runner, schema, title: str, *, consent: bool):
    """Build once using the existing scoped snapshot/live safety gates."""
    _check_report_access(service.settings)
    fresh_requested = bool(re.search(r"\b(fresh|refresh|live|current|latest)\b", title, re.I))
    custom_constraints = bool(re.search(r"(?:[<>]=?|\b(?:20\d{2}|only|excluding|exclude|between|since|before|after|last|past|where|filter|above|below|greater|less|more than|fewer than|at least|at most)\b)", title, re.I))
    selected = [] if fresh_requested or custom_constraints else relevant_messages(chat.get("messages", []), runner.scope, title)
    pay_requested = bool(re.search(r"\b(pay|paid|salary|salaries|compensation)\b", title, re.I))
    if selected and pay_requested:
        # Counts alone cannot answer pay questions; retain the metric coverage check.
        candidate = runner.snapshot(title, selected)
        sections = build_document(candidate).sections
        group_requested = bool(re.search(r"department|role|job", title, re.I))
        department_requested = bool(re.search(r"department", title, re.I))
        role_requested = bool(re.search(r"role|job", title, re.I))
        expected = "Department Salary Profile" if department_requested else "Job Title Salary Profile"
        sufficient = any(s.title == expected for s in sections) if group_requested else any(
            any("salary" in c.lower() for c in s.columns) for s in sections)
        if role_requested and any(c.get("name", "").lower() in {"role", "job_title", "job_role"} for c in schema.columns):
            sufficient &= any(s.title == "Job Title Salary Profile" for s in sections)
        if not sufficient:
            selected = []
        else:
            return replace(candidate, title=report_title(title), question=title)
    if selected:
        report = runner.snapshot(title, selected)
    else:
        queries = automatic_queries(schema, title)
        model_ready = bool(service.settings.api_key and service.settings.model)
        if custom_constraints and not (model_ready and consent):
            raise ReportError("Custom report filters need consented planning; no broad replacement was run.")
        if model_ready and consent and (custom_constraints or not re.search(r"workforce|business|overview|employee|department|role|job|pay|salary|salaries|compensation|hir|status|cit|geograph", title, re.I)):
            queries = ChatModel(service.settings).plan_report(title, schema, [])
        report = runner.live(title, queries, uuid4().hex)
    return replace(report, title=report_title(title), question=title)


def render_reports(chat: dict, service, schema, render_result, *, consent: bool, ai_summary: bool,
                   report_id: str | None = None, build_only: bool = False) -> None:
    try:
        _check_report_access(service.settings)
    except ReportError:
        chat["report_open"] = False
        st.warning("Report unavailable. Sign in again to access your reports.")
        return
    # Consume the job before cancellation, planning, or any other I/O. Even a
    # failure/interrupted run must not silently retry on the next render.
    pending = report_id is None and bool(chat.get("report_open"))
    topic = ""
    if pending:
        chat["report_open"] = False
        topic = chat.pop("report_topic", "")
        chat.pop("report_plan", None)
    scope_table = service.scope_table if service.settings.schema_scope else service.settings.table
    runner = chat.get("report_runner")
    if runner is None or runner.scope != (service.settings.database, scope_table):
        runner = ReportService(service.settings, service.database, scope_table)
        chat["report_runner"] = runner
    if pending:
        try:
            service.cancel()
            st.session_state.pop("pending_prompt", None)
            st.session_state.pop("edited_prompt", None)
            with st.spinner("Building your report…"):
                report = build_report(chat, service, runner, schema,
                                      topic.strip() or "Workforce overview", consent=consent)
                reports = chat.setdefault("reports", [])
                reports.append(report)
                del reports[:-3]
                ids = {r.id for r in reports}
                chat["report_pdfs"] = {rid: data for rid, data in chat.get("report_pdfs", {}).items() if rid in ids}
                chat["report_pdf_errors"] = chat.get("report_pdf_errors", set()) & ids
                chat["report_email_attempts"] = {attempt: status for attempt, status in chat.get("report_email_attempts", {}).items() if attempt[0] in ids}
                messages = chat.get("messages", [])
                if any(message.get("role") == "user" for message in messages):
                    messages = [*messages, {"role": "assistant", "report_id": report.id, "text": ""}][-20:]
                    chat["messages"] = messages
                    # Keep the active conversation in sync without importing the app.
                    active_chat = st.session_state.get("active_chat")
                    if st.session_state.get("chats", {}).get(active_chat) is chat:
                        st.session_state["messages"] = list(messages)
        except (ReportError, PolicyError, ModelError, DatabaseError):
            st.warning("Report unavailable. Check the request or selected data and ask again. No data was changed.")
    if build_only:
        return
    reports = [report for report in chat.get("reports", [])
               if report_id is None or report.id == report_id]
    if not reports:
        return
    cloud = service.settings.cloud_mode
    profile = {} if cloud else st.session_state.profiles[st.session_state.active_profile]
    email_settings = EmailSettings()
    email_settings_error = ""
    try:
        if not cloud:
            email_settings = EmailSettings.from_env()
    except EmailError as exc:
        email_settings_error = str(exc)
    for report in reports:
        try:
            _check_report_access(service.settings)
        except ReportError:
            st.warning("Report unavailable. Sign in again to access your reports.")
            return
        with st.container(border=False, key=f"business_report_{report.id}"):
            document = build_document(report)
            st.subheader(document.title)
            source = "Saved chat answers" if report.source == "snapshot" else "Selected-table analysis"
            st.caption(f"{source} · {runner.scope_label} · {report.created_at:%Y-%m-%d %H:%M UTC}")
            if report.question:
                st.caption("Question: " + report.question)
            st.markdown("**Executive summary**")
            summary = document.summary
            if "Partial report:" in summary:
                summary = "Here are the available results."
            st.text(summary)
            for section in document.sections:
                st.subheader(section.title)
                client_table(pd.DataFrame(section.rows[:30], columns=section.columns))
                if len(section.rows) > 30:
                    st.caption(f"Showing 30 of {len(section.rows)} returned groups. Additional source values remain in Sources & details.")
                st.text(section.commentary)
            charts = [section for section in document.sections if section.chart_result is not None and len(section.rows) > 1]
            if charts:
                st.subheader("Workforce charts")
                # Pay comparisons are primary for pay reports, not incidental
                # headcount/status charts. All selection remains local.
                charts.sort(key=lambda section: "Salary Profile" not in section.title)
                for index, section in enumerate(charts[:3]):
                    render_result(section.chart_result, f"report_{report.id}_{index}", section.chart_question, sql=section.chart_sql)
            st.subheader("Key findings")
            for finding in document.findings:
                st.text("• " + finding)
            if document.recommendations:
                st.subheader("Recommendations")
                for action in document.recommendations:
                    st.text("• " + action)
            if document.follow_up:
                st.subheader("Suggested follow-up")
                st.text(document.follow_up)
            with st.expander("Sources & details"):
                st.caption("Display values are rounded to two decimals. Exact source values are retained below.")
                for index, evidence in enumerate(report.evidence, 1):
                    st.caption(f"Source {index} · {evidence.executed_at:%Y-%m-%d %H:%M UTC}")
                    safe = aggregate_result(evidence)
                    if safe is not None:
                        client_table(pd.DataFrame(safe.rows, columns=safe.columns))
                with st.expander("SQL provenance"):
                    for index, evidence in enumerate(report.evidence, 1):
                        st.caption(f"Source {index}")
                        st.code(evidence.sql, language="sql")
            if cloud:
                st.caption("Email delivery is unavailable in this cloud pilot. Download the PDF to share it.")
            pdfs = chat.setdefault("report_pdfs", {})
            errors = chat.setdefault("report_pdf_errors", set())
            retry = report.id in errors and st.button("Retry PDF", key=f"report_pdf_retry_{report.id}")
            if report.id not in pdfs and (report.id not in errors or retry):
                try:
                    _check_report_access(service.settings)
                    pdfs[report.id] = report_pdf(report)
                    errors.discard(report.id)
                except ReportError:
                    st.warning("Report unavailable. Sign in again to export your report.")
                    return
                except Exception:
                    errors.add(report.id)
            if report.id in errors:
                st.warning("PDF export failed. Your report remains available; use Retry PDF to try again.")
            data = pdfs.get(report.id)
            if data:
                try:
                    _check_report_access(service.settings)
                except ReportError:
                    st.warning("Report unavailable. Sign in again to download your report.")
                    return
                name = re.sub(r"[^A-Za-z0-9_-]+", "_", report.title)[:60] or "business_report"
                st.download_button("Download PDF", data=data, file_name=name + ".pdf", mime="application/pdf",
                                   key=f"report_download_{report.id}", on_click="ignore")
                if cloud:
                    continue
                email_key = f"report_recipient_{st.session_state.active_profile}_{report.id}"
                attempts = chat.setdefault("report_email_attempts", {})
                with st.popover("Send to email", key=f"report_email_{report.id}", width="content"):
                    if email_key not in st.session_state:
                        try:
                            st.session_state[email_key] = RecipientStore().read(profile["name"])
                        except EmailError as exc:
                            st.session_state[email_key] = ""
                            st.warning(str(exc))
                    recipient = st.text_input("Recipient email", key=email_key, max_chars=254,
                                              on_change=_save_recipient, args=(profile["name"], email_key))
                    st.caption("Saved on this device for this profile name; clear the field to forget it. Only Send shares the PDF with your mail provider and recipient.")
                    if notice := st.session_state.get(email_key + "_notice"):
                        st.caption(notice)
                    if email_settings_error:
                        st.warning(email_settings_error)
                    elif not email_settings.configured:
                        st.warning("Email sending is not configured. Add SMTP settings to your private .env file and restart the app. PDF download remains available.")
                    try:
                        address = normalize_email(recipient) if recipient else ""
                    except EmailError:
                        address = ""
                        if recipient:
                            st.caption("Enter one valid email address to send this report.")
                    attempt_key = (report.id, address)
                    state = attempts.get(attempt_key)
                    if st.button("Send", key=f"report_email_send_{report.id}",
                                 disabled=not address or not email_settings.configured or state in {"sending", "accepted", "uncertain"}
                                 or (attempt_key not in attempts and len(attempts) >= 64),
                                 type="primary", width="stretch"):
                        # Consume this deliberate action before I/O. Reruns/downloads
                        # never send; ambiguous outcomes are never retried.
                        attempts[attempt_key] = "sending"
                        try:
                            RecipientStore().save(profile["name"], address)
                            with st.spinner("Sending PDF…"):
                                send_report_pdf(data, report.title, address, email_settings)
                            attempts[attempt_key] = "accepted"
                        except DeliveryUncertain:
                            attempts[attempt_key] = "uncertain"
                        except EmailError as exc:
                            attempts[attempt_key] = "failed"
                            st.warning(str(exc))
                        st.rerun()
                if state == "accepted":
                    st.success("Email accepted by the mail server. Inbox delivery is not guaranteed; check your inbox or spam folder.")
                elif state in {"uncertain", "sending"}:
                    st.warning("Delivery outcome is uncertain. Check your inbox and mail provider before sending another copy; automatic retry is blocked.")
                elif state == "failed":
                    st.warning("Email was not confirmed sent. Check SMTP settings and recipient before trying again.")