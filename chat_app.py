"""Run with Streamlit. State is session-local; no employee data is persisted."""

import base64
import re

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import json
import sqlglot
from sqlglot import exp
from uuid import uuid4

from chatbot.config import ROOT, Settings
from chatbot.db import Database, DatabaseError, QueryResult
from chatbot.llm import ChatModel, ModelError
from chatbot.output import display_columns
from chatbot.visualization import choose_visualization, compatible_visualizations
from chatbot.reports import is_report_request, utc_now
from chatbot.report_ui import open_report, render_reports as render_report_ui
from chatbot.service import ChatService, Proposal
from chatbot.sql_policy import PolicyError
from chatbot.speech import speech_controls_html
from chatbot.client_theme import theme_css, themed_spec, client_table
from chatbot.report_presentation import display_number, human_label
from chatbot.cloud_access import CloudAccessError
from chatbot.cloud_ui import prepare_cloud_session, render_cloud_account


def copy_button(text: str) -> None:
    """Copy locally in the browser; never interpolate message HTML into the page."""
    payload = json.dumps(text).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    components.html('''<style>body{margin:0;background:transparent}button{background:transparent;
                color:light-dark(#475569,#94a3b8);border:0;cursor:pointer;padding:6px;width:34px;height:34px;
                border-radius:8px;display:grid;place-items:center}
                button:hover{color:light-dark(#4f46e5,#818cf8);background:light-dark(#eef2ff,#252b4a)}button:focus-visible{outline:2px solid light-dark(#4f46e5,#818cf8)}
                svg{width:21px;height:21px}</style><button aria-label="Copy message" title="Copy message">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                <rect x="4" y="7" width="13" height="14" rx="3"/><path d="M9 7V5a3 3 0 0 1 3-3h6a3 3 0 0 1 3 3v8a3 3 0 0 1-3 3h-1"/></svg></button>
        <script>const text=''' + payload + ''';
        // Observe native appearance only; never render message HTML or alter parent state.
        const syncTheme=()=>{document.documentElement.style.colorScheme=
            getComputedStyle(parent.document.querySelector('.stApp')).colorScheme;};
        syncTheme();const observer=new MutationObserver(syncTheme);
        observer.observe(parent.document.querySelector('.stApp'),{attributes:true,attributeFilter:['class','style']});
        window.addEventListener('pagehide',()=>observer.disconnect());
                const success=()=>{button.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m5 12 4 4L19 6"/></svg>';button.title='Copied';button.setAttribute('aria-label','Copied');};
        const button=document.querySelector('button');button.onclick=async()=>{
                    try {await navigator.clipboard.writeText(text);success();}
          catch(error){const field=document.createElement('textarea');field.value=text;
            document.body.appendChild(field);field.select();const ok=document.execCommand('copy');
                        field.remove();if(ok)success();else button.title='Select text to copy';}
                };</script>''', height=36)


def edit_question(index: int, text: str, service: ChatService) -> None:
    """Resubmit an edited question from that turn, without replaying completed writes."""
    service.cancel()
    st.session_state.pop("pending_prompt", None)
    st.session_state.messages = st.session_state.messages[:index]
    chat = st.session_state.chats[st.session_state.active_chat]
    retained_ids = {m.get("query_id") for m in st.session_state.messages}
    chat["query_history"] = [q for q in chat.get("query_history", []) if q["query_id"] in retained_ids]
    chat["reports"] = []
    chat.pop("report_pdfs", None)
    chat.pop("report_plan", None)
    chat["report_open"] = False
    st.session_state.edited_prompt = text.strip()
    save_chat()


def switch_profile(profile_id: str, service: ChatService) -> None:
    """Separate local conversation workspaces, not authentication or permissions."""
    save_chat()
    current = st.session_state.chats[st.session_state.active_chat]
    current.pop("report_plan", None)
    current["report_open"] = False
    st.session_state.schema_histories[service.history_key] = {
        "chats": st.session_state.chats, "active_chat": st.session_state.active_chat}
    st.session_state.profiles[st.session_state.active_profile]["histories"] = dict(st.session_state.schema_histories)
    service.cancel()
    st.session_state.pop("pending_prompt", None)
    st.session_state.pop("queued_chat_prompt", None)
    st.session_state.pop("edited_prompt", None)
    st.session_state.active_profile = profile_id
    st.session_state.schema_histories = dict(st.session_state.profiles[profile_id]["histories"])
    previous = st.session_state.schema_histories.get(service.history_key)
    if previous:
        st.session_state.chats = previous["chats"]
        st.session_state.active_chat = previous["active_chat"]
        st.session_state.messages = list(st.session_state.chats[st.session_state.active_chat]["messages"])
    else:
        st.session_state.pop("chats", None)
        new_chat(service)


def render_result(result: QueryResult, key: str, question: str = "", *, sql: str = "") -> None:
    visual = choose_visualization(result, question, sql=sql)

    def table() -> None:
        frame = pd.DataFrame(result.rows, columns=display_columns(result.columns))
        client_table(frame)

    if visual.kind == "metric":
        st.metric(visual.title, visual.value, border=False)
    elif visual.spec:
        st.caption(visual.title)
        chart_tab, details_tab = st.tabs(["Chart", "Details"])
        with chart_tab:
            alternatives = compatible_visualizations(result, question, sql=sql)
            if len(alternatives) > 1:
                selected_chart = st.selectbox("Chart type", ["Auto", *alternatives],
                    format_func=lambda kind: kind.replace("_", " ").title(), key=f"visual_kind_{key}")
                if selected_chart != "Auto":
                    visual = alternatives[selected_chart]
            st.vega_lite_chart(spec=themed_spec(visual.spec, st.session_state.get("dark_mode", False)), width="stretch", theme=None, key=f"chart_{key}")
        with details_tab:
            table()
    elif result.columns and result.rows:
        table()
    if visual.note:
        st.caption(visual.note)
    if result.truncated:
        st.warning("These results are incomplete. Refine your question to see a smaller selection.")


def save_chat() -> None:
    """Keep history local to this browser session; never store credentials or confirmations."""
    chat = st.session_state.chats[st.session_state.active_chat]
    chat["messages"] = st.session_state.messages[-20:]
    first_question = next((m["text"] for m in chat["messages"] if m["role"] == "user"), "New chat")
    chat["title"] = " ".join(first_question.split())[:48] or "New chat"


def _requested_top_earners_answer(result: QueryResult, question: str) -> str | None:
    """Build a local, plain-text answer for explicitly requested per-group earners."""
    if not re.search(r"\b(?:top|highest|best[- ]paid|top[- ]paid)\b", question, re.I) or not re.search(
            r"\b(?:each|per|within each|in each)\b", question, re.I):
        return None
    columns = {column.casefold(): index for index, column in enumerate(result.columns)}
    department_index = next((i for name, i in columns.items()
                             if re.search(r"\bdepartment\b", name)), None)
    salary_index = next((i for name, i in columns.items()
                         if re.search(r"salary|earnings|pay", name)), None)
    name_index = next((i for name, i in columns.items()
                       if name in {"name", "employee_name", "full_name", "employee_full_name"}), None)
    first_index, last_index = columns.get("first_name"), columns.get("last_name")
    if department_index is None or salary_index is None or (name_index is None and first_index is None):
        return None

    groups: dict[str, list[str]] = {}
    for row in result.rows:
        if len(row) != len(result.columns):
            continue
        raw_name = row[name_index] if name_index is not None else " ".join(
            str(row[index]).strip() for index in (first_index, last_index)
            if index is not None and row[index] is not None and str(row[index]).strip())
        name = " ".join(str(raw_name or "").split())
        if not name:
            continue
        department = " ".join(str(row[department_index] or "Missing").split())
        salary = display_number(row[salary_index])
        groups.setdefault(department, []).append(f"{name} ({salary})")
    if not groups:
        return None
    requested_count = re.search(r"\b(?:top\s+)?(\d{1,2})\b", question, re.I)
    requested_limit = int(requested_count.group(1)) if requested_count else None
    heading = f"Top {requested_limit} earners by department:" if requested_limit else "Top earners by department:"
    lines = [f"{department}: {', '.join(earners[:requested_limit] if requested_limit else earners)}"
             for department, earners in groups.items()]
    return heading + "\n" + "\n".join(lines)


def _requires_department_ranking(question: str) -> bool:
    return bool(re.search(r"\b(?:top|highest|best[- ]paid|top[- ]paid)\b", question, re.I)
                and re.search(r"\b(?:each|per)\s+department\b", question, re.I))


def _has_requested_department_rank(sql: str, question: str) -> bool:
    """Fail closed unless SQL uses a department-partitioned row number capped at N."""
    count_match = re.search(r"\btop\s+(\d{1,2})\b", question, re.I)
    if not count_match:
        return False
    expected = int(count_match.group(1))
    try:
        statement = sqlglot.parse_one(sql, read="mysql")
    except (sqlglot.errors.ParseError, ValueError, TypeError):
        return False
    if not isinstance(statement, exp.Select):
        return False
    outputs = [projection.alias_or_name.casefold() for projection in statement.expressions]
    if (not any(re.search(r"name", name) for name in outputs)
            or not any(re.search(r"department", name) for name in outputs)
            or not any(re.search(r"salary|earnings|pay", name) for name in outputs)):
        return False
    for window in statement.find_all(exp.Window):
        if not isinstance(window.this, exp.RowNumber):
            continue
        partitions = window.args.get("partition_by", [])
        if not any(re.search(r"department", partition_column.name, re.I)
                   for partition in partitions for partition_column in partition.find_all(exp.Column)):
            continue
        ranking_order = window.args.get("order")
        ranking_terms = ranking_order.expressions if ranking_order else []
        if (not ranking_terms or not isinstance(ranking_terms[0].this, exp.Column)
                or not re.search(r"salary|earnings|pay", ranking_terms[0].this.name, re.I)
                or not ranking_terms[0].args.get("desc")):
            continue
        alias = window.parent.alias if isinstance(window.parent, exp.Alias) else ""
        if not alias:
            continue
        for where in statement.find_all(exp.Where):
            for comparison in where.find_all(exp.LTE):
                left, right = comparison.this, comparison.expression
                if (isinstance(left, exp.Column) and left.name.casefold() == alias.casefold()
                        and isinstance(right, exp.Literal) and right.is_int
                    and int(right.this) == expected):
                    return True
    return False


def new_chat(service: ChatService) -> None:
    if "chats" not in st.session_state:
        st.session_state.chats = {}
    else:
        save_chat()
    service.cancel()
    for chat in st.session_state.chats.values():
        chat.pop("report_plan", None)
        chat["report_open"] = False
    st.session_state.pop("pending_prompt", None)
    st.session_state.pop("queued_chat_prompt", None)
    st.session_state.pop("edited_prompt", None)
    chat_id = uuid4().hex
    st.session_state.chats[chat_id] = {"title": "New chat", "messages": []}
    # Bound in-memory history: 20 chats × 20 messages, including their capped result sets.
    while len(st.session_state.chats) > 20:
        del st.session_state.chats[next(iter(st.session_state.chats))]
    st.session_state.active_chat = chat_id
    st.session_state.messages = []


def switch_chat(chat_id: str, service: ChatService) -> None:
    save_chat()
    current = st.session_state.chats[st.session_state.active_chat]
    current.pop("report_plan", None)
    current["report_open"] = False
    service.cancel()
    st.session_state.pop("pending_prompt", None)
    st.session_state.pop("queued_chat_prompt", None)
    st.session_state.pop("edited_prompt", None)
    st.session_state.active_chat = chat_id
    st.session_state.messages = list(st.session_state.chats[chat_id]["messages"])


def record(proposal: Proposal, prompt: str, service: ChatService, *, confirmed: bool = False,
           phrase: str = "", ai_summary: bool = False) -> None:
    message = {"role": "assistant", "sql": proposal.query.sql, "text": proposal.intent, "question": prompt}
    # Clear UI pending state before any database/model call. Never repeat a change on a rerun.
    st.session_state.pop("pending_prompt", None)
    try:
        result = service.execute(proposal, confirmed=confirmed, phrase=phrase)
        ensure_cloud_state(service)
        message["result"] = result
        message["read_only"] = proposal.query.read_only
        if proposal.query.read_only:
            message.update(query_id="q" + uuid4().hex, executed_at=utc_now(),
                scope=(service.settings.database, service.scope_table if service.settings.schema_scope else service.settings.table))
            history = st.session_state.chats[st.session_state.active_chat].setdefault("query_history", [])
            history.append({k: message[k] for k in ("query_id", "question", "sql", "executed_at", "scope", "read_only")})
            del history[:-50]
        if proposal.query.read_only:
            message["text"] = "I found no matching records." if not result.rows else (
                "Here are the matching records."
            )
            if len(result.rows) == 1 and len(result.columns) == 1:
                message["text"] = f"{human_label(result.columns[0])}: {display_number(result.rows[0][0])}."
        else:
            message["text"] = f"Your requested change is complete. {result.affected_rows} rows were affected."
        local_ranking_answer = _requested_top_earners_answer(result, prompt) if proposal.query.read_only else None
        if local_ranking_answer is not None:
            # Names were explicitly requested. Render the locally fetched names safely as plain text;
            # never send them to an AI summary or expose the internal privacy placeholder.
            message["text"] = local_ranking_answer
            message["plain_text"] = True
        elif ai_summary and proposal.query.read_only:
            try:
                message["text"] = ChatModel(service.settings).summarize(result, prompt, proposal.query.sql)
            except ModelError as exc:
                message["notice"] = str(exc)
    except CloudAccessError as exc:
        handle_cloud_denial(service, exc)
        message["error"] = str(exc)
    except (PolicyError, DatabaseError) as exc:
        message["error"] = str(exc)
    ensure_cloud_state(service)
    st.session_state.messages.append(message)
    st.session_state.messages = st.session_state.messages[-20:]
    save_chat()


def ensure_cloud_state(service: ChatService, *, error: str = "Cloud session is unavailable. Sign in again.") -> None:
    """Stop stale cloud work without recreating or clearing conversation state."""
    if service.settings.cloud_mode and (
            st.session_state.get("service") is not service
            or "messages" not in st.session_state
            or st.session_state.get("active_chat") not in st.session_state.get("chats", {})):
        st.error(error)
        st.stop()


def handle_cloud_denial(service: ChatService, error: CloudAccessError) -> None:
    """Never recreate history after an authorization guard cleared the session."""
    service.cancel()
    st.session_state.pop("pending_prompt", None)
    st.session_state.pop("edited_prompt", None)
    ensure_cloud_state(service, error=str(error))
    chat = st.session_state.chats[st.session_state.active_chat]
    chat["report_open"] = False
    chat.pop("report_topic", None)
    chat.pop("report_plan", None)


def render_reports(chat, service, schema, render_result, **options) -> None:
    """Report UI can consume a denial after its guard has cleared app state."""
    render_report_ui(chat, service, schema, render_result, **options)
    ensure_cloud_state(service)


def main() -> None:
    st.set_page_config(page_title="InsightDock · MySQL assistant", page_icon=str(ROOT / "assets" / "insightdock.svg"), layout="wide", initial_sidebar_state="expanded")
    st.markdown(f"<style>{(ROOT / 'assets' / 'chat.css').read_text()}</style>", unsafe_allow_html=True)
    st.markdown(f"<style>{theme_css(st.session_state.get('dark_mode', False))}</style>", unsafe_allow_html=True)
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        st.error(str(exc))
        st.info("Configure the database values in your local .env using .env.example. Never paste credentials into chat.")
        st.stop()

    cloud_database = None
    owner, display_name = "owner", "Fawaz Khan"
    if settings.cloud_mode:
        try:
            settings, cloud_database, owner, display_name = prepare_cloud_session(settings)
        except CloudAccessError as exc:
            st.error(str(exc))
            st.stop()

    if ("service" not in st.session_state or st.session_state.get("base_settings") != settings
            or (settings.cloud_mode and (st.session_state.service.database is not cloud_database
                                         or st.session_state.get("active_profile") != owner))):
        if "service" in st.session_state:
            st.session_state.service.database.engine.dispose()
        st.session_state.service = ChatService(settings, cloud_database if settings.cloud_mode else Database(settings))
        st.session_state.service.scope_table = settings.table if settings.client_scope == "table" else None
        st.session_state.base_settings = settings
        st.session_state.pop("selected_schema", None)
        st.session_state.schema_histories = {}
        st.session_state.profiles = {owner: {"name": display_name, "histories": {}}}
        st.session_state.active_profile = owner
        st.session_state.pop("profile_picker", None)
        st.session_state.pop("next_profile_picker", None)
        st.session_state.pop("chats", None)
        st.session_state.messages = []
        st.session_state.pop("pending_prompt", None)
        st.session_state.pop("edited_prompt", None)
        st.session_state.pop("queued_chat_prompt", None)
    service = st.session_state.service
    if "chats" not in st.session_state:
        new_chat(service)
    if settings.cloud_mode:
        render_cloud_account()
    with st.sidebar:
        if not settings.cloud_mode:
            with st.container(key="profile_controls"):
                render_local_profiles(service)
            profile_id = st.session_state.profile_picker
            if profile_id != st.session_state.active_profile:
                switch_profile(profile_id, service)
                st.rerun()
        if st.button("New chat", key="new_chat_button", width="stretch", icon=":material/add:"):
            new_chat(service)
            st.rerun()
        st.markdown("**Recents**")
        for chat_id, chat in reversed(list(st.session_state.chats.items())):
            active = chat_id == st.session_state.active_chat
            if st.button(chat["title"], key=f"history_{chat_id}", width="stretch",
                         type="primary" if active else "secondary", icon=":material/chat_bubble_outline:"):
                if not active:
                    switch_chat(chat_id, service)
                    st.rerun()

    try:
        render_chat(service)
    except CloudAccessError as exc:
        handle_cloud_denial(service, exc)
        st.error(str(exc))


def render_local_profiles(service: ChatService) -> None:
    """Local convenience profiles are deliberately unavailable in cloud mode."""
    profile_column, add_column = st.columns([5, 1], gap="small")
    profiles = st.session_state.profiles
    with profile_column:
        if "next_profile_picker" in st.session_state:
            st.session_state.profile_picker = st.session_state.pop("next_profile_picker")
        st.selectbox("User", list(profiles),
            index=list(profiles).index(st.session_state.active_profile),
            format_func=lambda value: profiles[value]["name"], key="profile_picker",
            help="Local session profiles, not authenticated accounts.")
    with add_column:
        with st.popover("", icon=":material/person_add:", help="Add user"):
            with st.form("add_profile", clear_on_submit=True):
                name = st.text_input("User name", max_chars=40)
                if st.form_submit_button("Add user"):
                    clean_name = " ".join(name.split())
                    if not clean_name:
                        st.warning("Enter a name.")
                    elif any(p["name"].casefold() == clean_name.casefold() for p in profiles.values()):
                        st.warning("That user already exists.")
                    elif len(profiles) >= 10:
                        st.warning("Up to 10 local users can be added per session.")
                    else:
                        new_id = uuid4().hex
                        profiles[new_id] = {"name": clean_name, "histories": {}}
                        switch_profile(new_id, service)
                        st.session_state.next_profile_picker = new_id
                        st.rerun()


def render_chat(service: ChatService) -> None:
    logo_data = base64.b64encode((ROOT / "assets" / "insightdock.svg").read_bytes()).decode("ascii")
    logo_uri = f"data:image/svg+xml;base64,{logo_data}"
    title_column, theme_column = st.columns([5, 1])
    with title_column:
        st.markdown(f'''<div class="topbar"><div class="brand-lockup">
          <img class="brand-logo" src="{logo_uri}" alt="" />
          <div><div class="brand-name">InsightDock</div>
          <div class="brand-note">MySQL analytics assistant</div></div>
          </div><span class="topbar-tagline">Your data, in focus.</span></div>''', unsafe_allow_html=True)
    with theme_column:
        dark_mode = st.toggle("Dark mode", key="dark_mode")
    st.markdown(f"<style>{theme_css(dark_mode)}</style>", unsafe_allow_html=True)

    settings = service.settings
    model_ready = bool(settings.api_key and settings.model)
    consent = settings.openai_consent
    ai_summary = settings.ai_summaries and consent
    try:
        schema = service.scoped_schema(service.database.discover_schema())
        if service.scope_table and not schema.exists:
            raise PolicyError("The configured table is unavailable.")
    except (DatabaseError, PolicyError, CloudAccessError) as exc:
        if isinstance(exc, CloudAccessError):
            handle_cloud_denial(service, exc)
        service.cancel()
        st.session_state.pop("pending_prompt", None)
        if service.scope_table:
            st.error(f"Configured table '{settings.table}' is unavailable. Check the database configuration and access.")
        else:
            st.error(str(exc))
        st.info("Chat is unavailable until the configured data is accessible.")
        st.chat_input("Ask a question…", disabled=True)
        st.stop()
    disabled = not (model_ready and consent and schema.exists)
    if disabled:
        st.info("Chat setup is incomplete. Configure your AI model, enable AI consent, and check the configured data.")

    chat = st.session_state.chats[st.session_state.active_chat]
    if chat.get("report_open"):
        render_reports(chat, service, schema, render_result,
                       consent=consent, ai_summary=ai_summary, build_only=True)

    if not st.session_state.messages:
        st.markdown(f'''<section class="hero" aria-labelledby="welcome-title">
          <div class="hero-mark"><img src="{logo_uri}" alt="" /></div>
          <div class="hero-eyebrow">A clearer view of your data</div>
          <h1 id="welcome-title">Meet your data<br/><em>where insight begins.</em></h1>
          <p>Ask a question in plain English. InsightDock turns your MySQL data into a clear answer.</p>
          </section>''', unsafe_allow_html=True)
        st.markdown('<div class="suggestion-label">Try asking</div>', unsafe_allow_html=True)
        examples = ["How many employees are there?", "Compare average salary by department", "Show hiring trends by month"]
        example_columns = st.columns(3, gap="small")
        for index, (column, example) in enumerate(zip(example_columns, examples)):
            with column:
                if st.button(example, key=f"example_prompt_{index}", width="stretch", icon=":material/arrow_outward:", disabled=disabled):
                    st.session_state.edited_prompt = example
                    st.rerun()

    for i, message in enumerate(st.session_state.messages):
        if "report_id" in message:
            if any(report.id == message["report_id"] for report in chat.get("reports", [])):
                with st.chat_message("assistant", avatar=":material/auto_awesome:"):
                    render_reports(chat, service, schema, render_result,
                                   consent=consent, ai_summary=ai_summary, report_id=message["report_id"])
            continue
        with st.chat_message(message["role"], avatar=":material/auto_awesome:" if message["role"] == "assistant" else None):
            # Markdown formatting is supported, but model/user HTML is never executed.
            if message["role"] == "user":
                st.text(message.get("text", ""))
            elif message.get("plain_text"):
                st.text(message.get("text", ""))
            else:
                st.markdown(message.get("text", ""), unsafe_allow_html=False)
            if "error" in message:
                st.error(message["error"])
            if "notice" in message:
                with st.expander("Answer details"):
                    st.caption(message["notice"])
            if "result" in message:
                if message["result"].columns:
                    result = message["result"]
                    key = f"{st.session_state.active_chat}_{i}"
                    if not (len(result.rows) == 1 and len(result.columns) == 1 and message.get("text")):
                        render_result(result, key, message.get("question", ""), sql=message.get("sql", ""))
            if message["role"] == "assistant":
                with st.container(key=f"answer_actions_{st.session_state.active_chat}_{i}"):
                    copy_column, speech_column = st.columns(2, gap="small")
                    with copy_column:
                        copy_button(message.get("text", ""))
                    with speech_column:
                        spoken = "\n".join(str(message[field]) for field in ("text", "notice", "error") if message.get(field))
                        components.html(speech_controls_html(spoken), height=58)
        if message["role"] == "user":
            message_key = f"{st.session_state.active_chat}_{message.get('id', i)}"
            with st.container(key=f"message_actions_{message_key}"):
                actions = st.columns(2, gap="small")
                with actions[0]:
                    copy_button(message.get("text", ""))
                with actions[1]:
                    with st.popover("", icon=":material/edit:", help="Edit question"):
                        with st.form(f"edit_form_{message_key}"):
                            updated = st.text_area("Edit question", value=message.get("text", ""),
                                                   key=f"edit_text_{message_key}")
                            st.caption("Resending removes later messages in this chat. Completed database changes are not undone; new changes still need approval.")
                            if st.form_submit_button("Save & resend"):
                                if updated.strip():
                                    edit_question(i, updated, service)
                                    st.rerun()
                                st.warning("Enter a question before resending.")

    if chat.get("reports") and not any("report_id" in message for message in st.session_state.messages):
        render_reports(chat, service, schema, render_result,
                       consent=consent, ai_summary=ai_summary)

    if service.pending is not None:
        pending = service.pending
        if pending.schema_fingerprint != schema.fingerprint:
            service.cancel()
            st.warning("Schema changed: pending SQL was cancelled. Submit your request again.")
        else:
            st.subheader("Your approval is needed")
            st.text(pending.intent)
            # Reads never expose SQL. For writes it remains available for informed approval.
            with st.expander("Review exact change", expanded=False):
                st.code(pending.query.sql, language="sql")
            if pending.query.warning:
                st.warning(pending.query.warning)
            phrase = ""
            if pending.query.destructive:
                phrase = st.text_input(f"Type {service.confirmation_phrase} to proceed", key=f"phrase_{pending.token}")
            st.caption("Confirmation applies to this exact SQL once. A new prompt cancels it. Back up employee data before destructive changes.")
            col1, col2 = st.columns(2)
            if col1.button("Confirm & execute once", type="primary", key=f"confirm_{pending.token}",
                           disabled=pending.query.destructive and phrase != service.confirmation_phrase):
                with st.spinner("Applying your approved change…"):
                    record(pending, st.session_state.get("pending_prompt", ""), service,
                           confirmed=True, phrase=phrase)
                st.rerun()
            if col2.button("Cancel change", key=f"cancel_{pending.token}"):
                service.cancel()
                st.session_state.pop("pending_prompt", None)
                st.rerun()

    prompt = st.chat_input("Ask me anything about this table…" if service.scope_table else "Ask me anything about this schema…", disabled=disabled)
    prompt = prompt or st.session_state.pop("edited_prompt", None)
    if prompt and disabled:
        st.warning("Complete chat setup before resending your question.")
        prompt = None
    if prompt and is_report_request(prompt):
        st.session_state.messages.append({"role": "user", "text": prompt, "id": uuid4().hex})
        save_chat()
        service.cancel()
        st.session_state.pop("pending_prompt", None)
        open_report(st.session_state.chats[st.session_state.active_chat], prompt)
        st.rerun()
    if prompt:
        service.cancel()
        st.session_state.pop("pending_prompt", None)
        st.session_state.messages.append({"role": "user", "text": prompt, "id": uuid4().hex})
        save_chat()
        # Commit the user turn in a separate Streamlit run before doing any
        # provider/database work. Otherwise the browser keeps showing the old
        # page while the synchronous spinner is running.
        st.session_state.queued_chat_prompt = prompt
        st.rerun()

    prompt = st.session_state.pop("queued_chat_prompt", None)
    if prompt:
        try:
            with st.spinner("Thinking…"):
                context = [m["text"] for m in st.session_state.messages[:-1] if m["role"] == "user"]
                sql, intent = ChatModel(settings).generate(prompt, schema, context)
                ensure_cloud_state(service)
                if not sql.strip():
                    st.session_state.messages.append({"role": "assistant", "text": intent})
                    save_chat()
                    st.rerun()
                if _requires_department_ranking(prompt) and not _has_requested_department_rank(sql, prompt):
                    st.session_state.messages.append({
                        "role": "assistant",
                        "text": "I couldn't safely verify a top-per-department ranking query, so nothing was executed. Please try again or ask for the SQL to be reviewed.",
                    })
                    st.session_state.messages = st.session_state.messages[-20:]
                    save_chat()
                    st.rerun()
                proposal = service.propose(sql, schema, intent)
                ensure_cloud_state(service)
            if proposal.query.read_only:
                with st.spinner("Finding your answer…"):
                    record(proposal, prompt, service, ai_summary=consent and ai_summary)
            else:
                st.session_state.pending_prompt = prompt
        except (PolicyError, DatabaseError, ModelError, CloudAccessError) as exc:
            if isinstance(exc, CloudAccessError):
                handle_cloud_denial(service, exc)
            ensure_cloud_state(service, error=str(exc))
            st.session_state.messages.append({"role": "assistant", "text": "Nothing was executed.", "error": str(exc)})
        st.session_state.messages = st.session_state.messages[-20:]
        save_chat()
        st.rerun()


if __name__ == "__main__":
    main()