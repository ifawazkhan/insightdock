"""Pure/security UI contracts. No provider, DNS, database or live auth calls."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from chatbot import cloud_access as access, cloud_ui as ui
from chatbot.config import Settings


ISSUER = "https://accounts.google.com"
SUBJECT = "123456789"
OWNER = access.owner_key(ISSUER, SUBJECT)


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class Stop(BaseException):
    """Streamlit control flow must not be swallowed by Exception handlers."""


class Rerun(BaseException):
    pass


class User(dict):
    is_logged_in = True


class State(dict):
    pass


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", Mock(side_effect=AssertionError("No live DNS")))
    monkeypatch.setattr("socket.create_connection", Mock(side_effect=AssertionError("No live sockets")))
    monkeypatch.setattr(ui, "create_cloud_database", Mock(side_effect=AssertionError("No live database")))


@pytest.fixture
def fake_st(monkeypatch):
    monkeypatch.setenv("CHATBOT_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("CHATBOT_CLOUD_ALLOWED_SUBJECTS", SUBJECT)
    fake = MagicMock()
    fake.session_state = State()
    fake.user = User(iss=ISSUER, sub=SUBJECT, name="Invited User")
    fake.secrets = {"auth": {
        "redirect_uri": "https://app.example.com/oauth2callback",
        "cookie_secret": "not-a-live-cookie-secret",
        "client_id": "configured", "client_secret": "configured",
        "server_metadata_url": ISSUER + "/.well-known/openid-configuration",
    }}
    fake.stop.side_effect = Stop
    fake.rerun.side_effect = Rerun
    fake.button.return_value = False
    fake.form_submit_button.return_value = False
    fake.text_input.side_effect = ["db.example.com", "tenant", "", "reader", "private-password"]
    fake.checkbox.side_effect = [True, True, False]
    monkeypatch.setattr(ui, "st", fake)
    monkeypatch.setattr(ui, "_QUOTAS", access.QuotaLimiter())
    return fake


@pytest.fixture
def settings():
    return Settings("", 3306, "", "", "", "", cloud_mode=True,
                    api_key="operator-ai-secret", model="configured-model")


def establish(fake, *, clock=None):
    lease = access.SessionLease(OWNER, clock=clock or Clock())
    fake.session_state.update(_cloud_owner=OWNER, _cloud_lease=lease,
                              _cloud_display_name="Invited User")
    return lease


def mock_database(monkeypatch, settings, *, exists=True):
    database = Mock()
    database.settings = replace(settings, host="db.example.com", user="reader",
                                password="private-password", database="tenant")

    def discover():
        database.action_guard()
        return SimpleNamespace(exists=exists)

    database.discover_schema.side_effect = discover
    monkeypatch.setattr(ui, "create_cloud_database", Mock(return_value=database))
    return database


def test_owner_key_is_stable_issuer_scoped_and_unambiguous():
    assert OWNER == hashlib.sha256((ISSUER + "\0" + SUBJECT).encode()).hexdigest()
    assert access.owner_key("ab", "c") != access.owner_key("a", "bc")
    assert OWNER != access.owner_key("accounts.google.com", SUBJECT)
    assert OWNER != access.owner_key(ISSUER, "someone-else")


@pytest.mark.parametrize("value", ["", " ", "a\0b", "a\nb", " padded", None, 3, "x" * 2049])
def test_owner_key_rejects_invalid_identity_parts(value):
    with pytest.raises(access.CloudAccessError):
        access.owner_key(ISSUER, value)


@pytest.mark.parametrize("allowlist", [SUBJECT, f"{ISSUER}|{SUBJECT}", f"other, {SUBJECT}, "])
def test_invites_are_subject_based_not_email_or_name(allowlist):
    policy = access.AccessPolicy.from_config(ISSUER, allowlist)
    identity = policy.authenticate({"iss": ISSUER, "sub": SUBJECT, "name": "User", "email": "x@test"},
                                   is_logged_in=True)
    assert identity.owner_key == OWNER and identity.display_name == "User"
    with pytest.raises(access.CloudAccessError):
        policy.authenticate({"iss": ISSUER, "sub": "uninvited", "email": SUBJECT}, is_logged_in=True)


@pytest.mark.parametrize("issuer,allowlist", [
    ("", SUBJECT), ("https://evil.example", SUBJECT), (ISSUER + "/", SUBJECT),
    (ISSUER, ""), (ISSUER, " , "), (ISSUER, "*"),
    (ISSUER, "accounts.google.com|" + SUBJECT), (ISSUER, ISSUER + "|a|b"),
    (ISSUER, ISSUER + "|"),
])
def test_configuration_fails_closed(issuer, allowlist):
    with pytest.raises(access.CloudAccessError):
        access.AccessPolicy.from_config(issuer, allowlist)


@pytest.mark.parametrize("changes,logged_in", [
    ({}, False), ({}, 1), ({"iss": "accounts.google.com"}, True),
    ({"iss": None}, True), ({"sub": None}, True), ({"sub": "other"}, True),
])
def test_native_login_and_exact_issuer_required(changes, logged_in):
    policy = access.AccessPolicy.from_config(ISSUER, SUBJECT)
    with pytest.raises(access.CloudAccessError):
        policy.authenticate({"iss": ISSUER, "sub": SUBJECT, **changes}, is_logged_in=logged_in)


@pytest.mark.parametrize("exp", [None, "200", True, float("nan"), float("inf"), 99, 100])
def test_exp_claim_when_present_must_be_valid_and_future(exp):
    policy = access.AccessPolicy.from_config(ISSUER, SUBJECT)
    with pytest.raises(access.CloudAccessError):
        policy.authenticate({"iss": ISSUER, "sub": SUBJECT, "exp": exp}, is_logged_in=True, now=100)


def test_missing_exp_is_supported_and_name_is_bounded():
    policy = access.AccessPolicy.from_config(ISSUER, SUBJECT)
    claims = {"iss": ISSUER, "sub": SUBJECT, "name": "A" * 200}
    assert len(policy.authenticate(claims, is_logged_in=True).display_name) == 100
    assert policy.authenticate({**claims, "exp": 101}, is_logged_in=True, now=100).owner_key == OWNER
    assert policy.authenticate({**claims, "name": None}, is_logged_in=True).display_name == "User"


def test_idle_expiry_is_checked_before_touch_and_is_terminal():
    clock = Clock()
    lease = access.SessionLease(OWNER, clock=clock)
    clock.now = 899
    lease.check(OWNER, touch=False)
    clock.now = 900
    with pytest.raises(access.CloudAccessError):
        lease.check(OWNER)
    clock.now = 0
    with pytest.raises(access.CloudAccessError):
        lease.check(OWNER)


def test_activity_extends_idle_not_absolute_grant():
    clock = Clock()
    lease = access.SessionLease(OWNER, clock=clock)
    for current in [899, 1798, 2697, 3596, 3599]:
        clock.now = current
        lease.check(OWNER)
    clock.now = 3600
    with pytest.raises(access.CloudAccessError):
        lease.check(OWNER)


@pytest.mark.parametrize("current,owner", [(0, "other"), (-1, OWNER), (float("nan"), OWNER)])
def test_lease_owner_and_clock_fail_closed(current, owner):
    clock = Clock()
    lease = access.SessionLease(OWNER, clock=clock)
    clock.now = current
    with pytest.raises(access.CloudAccessError):
        lease.check(owner)


@pytest.mark.parametrize("kind,per_user,total", [("model", 20, 200), ("db", 120, 1000), ("connection", 5, 20)])
def test_default_per_user_and_global_hourly_budgets(kind, per_user, total):
    limiter = access.QuotaLimiter(clock=Clock())
    for _ in range(per_user):
        limiter.charge("first", kind)
    with pytest.raises(access.QuotaExceeded):
        limiter.charge("first", kind)
    for i in range(total - per_user):
        limiter.charge("user-" + str(i), kind)
    with pytest.raises(access.QuotaExceeded):
        limiter.charge("another", kind)


def test_budget_windows_reset_at_boundary_not_on_denials():
    clock = Clock()
    limiter = access.QuotaLimiter(clock=clock, limits={"model": (1, 2)})
    limiter.charge("a", "model")
    clock.now = 3599
    with pytest.raises(access.QuotaExceeded):
        limiter.charge("a", "model")
    clock.now = 3600
    limiter.charge("a", "model")


def test_caps_do_not_evict_active_budgets_or_partially_charge():
    clock = Clock()
    limiter = access.QuotaLimiter(clock=clock, max_principals=1, limits={"model": (2, 2)})
    limiter.charge("a", "model")
    with pytest.raises(access.QuotaExceeded, match="capacity"):
        limiter.charge("b", "model")
    limiter.charge("a", "model")
    with pytest.raises(access.QuotaExceeded):
        limiter.charge("a", "model")
    clock.now = 3600
    limiter.charge("b", "model")


@pytest.mark.parametrize("single_owner", [True, False])
def test_concurrent_charges_never_exceed_atomic_user_or_global_budget(single_owner):
    limiter = access.QuotaLimiter(clock=Clock(), limits={"model": (20, 30)})

    def attempt(i):
        try:
            limiter.charge("a" if single_owner else str(i), "model")
            return 1
        except access.QuotaExceeded:
            return 0

    with ThreadPoolExecutor(max_workers=20) as executor:
        assert sum(executor.map(attempt, range(200))) == (20 if single_owner else 30)


def test_unknown_actions_and_email_disabled_without_spending_budget():
    limiter = access.QuotaLimiter(clock=Clock(), max_principals=1)
    for kind in ["email", "report", "unknown"]:
        with pytest.raises(access.QuotaExceeded, match="disabled"):
            limiter.charge("a", kind)
    limiter.charge("b", "model")


def test_quota_clock_rollback_cannot_reset_active_budgets():
    clock = Clock(3600)
    limiter = access.QuotaLimiter(clock=clock)
    limiter.charge("a", "model")
    clock.now = 3599
    with pytest.raises(access.QuotaExceeded):
        limiter.charge("a", "model")


@pytest.mark.parametrize("dispose_raises", [True, False])
def test_cleanup_disposes_aliases_once_and_clears_all_sensitive_state(dispose_raises):
    database = Mock()
    if dispose_raises:
        database.engine.dispose.side_effect = RuntimeError("private credentials")
    state = State(dark_mode=True, _cloud_database=database,
                  service=SimpleNamespace(database=database), chats={"private": "history"},
                  profiles={"private": "profile"}, password="private", _cloud_lease=object(),
                  _cloud_owner=OWNER, _cloud_settings=object(), report_pdfs=b"private")
    access.clear_cloud_session(state)
    assert state == {"dark_mode": True}
    database.engine.dispose.assert_called_once_with()


def test_cleanup_disposes_distinct_service_db_and_does_not_retain_arbitrary_theme():
    first, second = Mock(), Mock()
    state = State(_cloud_database=first, service=SimpleNamespace(database=second), dark_mode="secret")
    access.clear_cloud_session(state)
    assert state == {}
    first.engine.dispose.assert_called_once()
    second.engine.dispose.assert_called_once()


@pytest.mark.parametrize("break_config", ["auth", "cookie", "issuer", "invites", "metadata"])
def test_missing_native_auth_or_operator_configuration_stops_before_form(fake_st, settings, monkeypatch, break_config):
    if break_config == "auth":
        fake_st.secrets = {}
    elif break_config == "cookie":
        del fake_st.secrets["auth"]["cookie_secret"]
    elif break_config == "metadata":
        fake_st.secrets["auth"]["server_metadata_url"] = "https://evil.example/oidc"
    else:
        monkeypatch.delenv("CHATBOT_OIDC_ISSUER" if break_config == "issuer" else "CHATBOT_CLOUD_ALLOWED_SUBJECTS")
    fake_st.session_state["chats"] = {"private": "history"}
    with pytest.raises(Stop):
        ui.prepare_cloud_session(settings)
    assert fake_st.session_state == {}
    fake_st.form.assert_not_called()
    ui.create_cloud_database.assert_not_called()


def test_named_google_native_provider_configuration(fake_st):
    auth = fake_st.secrets["auth"]
    auth["google"] = {key: auth.pop(key) for key in ["client_id", "client_secret", "server_metadata_url"]}
    assert ui._auth_configuration()[1] == "google"


def test_logged_out_user_sees_native_login_only_and_no_stale_history(fake_st, settings):
    fake_st.user.is_logged_in = False
    fake_st.session_state.update(chats={"private": "history"}, dark_mode=True)
    fake_st.button.return_value = True
    with pytest.raises(Stop):
        ui.prepare_cloud_session(settings)
    fake_st.login.assert_called_once_with()
    assert fake_st.session_state == {"dark_mode": True}
    fake_st.form.assert_not_called()


@pytest.mark.parametrize("failure", ["owner", "idle", "absolute", "exp", "invite", "logout"])
def test_each_sensitive_action_revalidates_auth_and_clears_before_history(fake_st, monkeypatch, failure):
    clock = Clock()
    lease = establish(fake_st, clock=clock)
    database = Mock()
    fake_st.session_state.update(_cloud_database=database, chats={"private": "history"})
    if failure == "owner":
        fake_st.user["sub"] = "another"
        monkeypatch.setenv("CHATBOT_CLOUD_ALLOWED_SUBJECTS", SUBJECT + ",another")
    elif failure == "idle":
        clock.now = 900
    elif failure == "absolute":
        for current in [800, 1600, 2400, 3200]:
            clock.now = current
            lease.check(OWNER)
        clock.now = 3600
    elif failure == "exp":
        fake_st.user["exp"] = 0
    elif failure == "invite":
        monkeypatch.setenv("CHATBOT_CLOUD_ALLOWED_SUBJECTS", "other")
    else:
        fake_st.user.is_logged_in = False
    with pytest.raises(access.CloudAccessError):
        access.check_cloud_action("model")
    assert fake_st.session_state == {}
    fake_st.logout.assert_called_once()
    database.engine.dispose.assert_called_once()


def test_expiry_during_prepare_never_returns_cached_history(fake_st, settings):
    clock = Clock()
    establish(fake_st, clock=clock)
    fake_st.session_state.update(_cloud_database=Mock(), chats={"secret": "history"})
    clock.now = 900
    with pytest.raises(Stop):
        ui.prepare_cloud_session(settings)
    assert fake_st.session_state == {}
    assert fake_st.logout.called
    fake_st.form.assert_not_called()


def test_quota_denials_dont_touch_idle_or_destroy_valid_sessions(fake_st, monkeypatch):
    clock = Clock()
    establish(fake_st, clock=clock)
    monkeypatch.setattr(ui, "_QUOTAS", access.QuotaLimiter(clock=Clock(), limits={"model": (1, 1)}))
    access.check_cloud_action("model")
    clock.now = 899
    with pytest.raises(access.QuotaExceeded):
        access.check_cloud_action("model")
    assert fake_st.session_state["_cloud_owner"] == OWNER
    clock.now = 900
    with pytest.raises(access.CloudAccessError):
        access.check_cloud_action("model")
    assert fake_st.session_state == {}


def test_ai_configuration_required_before_form(fake_st, settings):
    with pytest.raises(Stop):
        ui.prepare_cloud_session(replace(settings, api_key=""))
    fake_st.form.assert_not_called()
    ui.create_cloud_database.assert_not_called()


@pytest.mark.parametrize("permission,consent", [(False, False), (False, True), (True, False)])
def test_no_connection_without_both_explicit_consents(fake_st, settings, permission, consent):
    fake_st.form_submit_button.return_value = True
    fake_st.checkbox.side_effect = [permission, consent, True]
    with pytest.raises(Stop):
        ui.prepare_cloud_session(settings)
    ui.create_cloud_database.assert_not_called()
    fake_st.error.assert_called_once()


@pytest.mark.parametrize("table,summaries", [("", False), ("items", True)])
def test_onboarding_derived_settings_guard_discovery_and_form_secret_handling(fake_st, monkeypatch, settings, table, summaries):
    fake_st.form_submit_button.return_value = True
    fake_st.text_input.side_effect = ["db.example.com", "tenant", table, "reader", "private-password"]
    fake_st.checkbox.side_effect = [True, True, summaries]
    captured = {}
    database = mock_database(monkeypatch, settings)

    def factory(derived, request):
        captured.update(settings=derived, request=request)
        database.settings = replace(derived, host=request.host, database=request.database,
                                    user=request.username, password=request.password,
                                    table=request.table or "", schema_scope=request.table is None)
        return database

    ui.create_cloud_database.side_effect = factory
    with pytest.raises(Rerun):
        ui.prepare_cloud_session(replace(settings, allow_mutations=True, ai_summaries=not summaries))
    assert captured["request"].table == (table or None)
    derived = captured["settings"]
    assert derived.cloud_mode and derived.openai_consent and not derived.allow_mutations
    assert derived.ai_summaries is summaries
    assert derived.client_scope == ("table" if table else "schema")
    assert fake_st.session_state["_cloud_database"] is database
    database.discover_schema.assert_called_once()
    fake_st.form.assert_called_once_with("_cloud_connection_form", clear_on_submit=True)
    assert fake_st.text_input.call_args_list[-1].kwargs == {"type": "password"}
    assert all("key" not in call.kwargs for call in fake_st.text_input.call_args_list)
    assert "password" not in fake_st.session_state
    assert ui._QUOTAS._global == {"connection": 1, "db": 1}
    result = ui.prepare_cloud_session(settings)
    assert result == (database.settings, database, OWNER, "Invited User")
    database.action_guard()
    access.check_cloud_action("model")
    with pytest.raises(access.QuotaExceeded):
        access.check_cloud_action("email")
    assert ui._QUOTAS._global == {"connection": 1, "db": 2, "model": 1}


@pytest.mark.parametrize("failure", ["discovery", "empty", "create"])
def test_failed_onboarding_disposes_database_and_sanitizes_errors(fake_st, monkeypatch, settings, failure):
    fake_st.form_submit_button.return_value = True
    database = mock_database(monkeypatch, settings, exists=failure != "empty")
    if failure == "discovery":
        database.discover_schema.side_effect = RuntimeError("private-password db.example.com")
    elif failure == "create":
        ui.create_cloud_database.side_effect = RuntimeError("private-password db.example.com")
    with pytest.raises(Stop):
        ui.prepare_cloud_session(settings)
    assert "_cloud_database" not in fake_st.session_state
    assert "private-password" not in str(fake_st.error.call_args_list)
    assert "db.example.com" not in str(fake_st.error.call_args_list)
    if failure != "create":
        database.engine.dispose.assert_called_once()
    assert ui._QUOTAS._global["connection"] == 1


def test_connection_attempt_quota_precedes_factory_and_dns(fake_st, monkeypatch, settings):
    fake_st.form_submit_button.return_value = True
    establish(fake_st)
    limiter = access.QuotaLimiter(clock=Clock(), limits={"connection": (1, 1)})
    limiter.charge(OWNER, "connection")
    monkeypatch.setattr(ui, "_QUOTAS", limiter)
    with pytest.raises(Stop):
        ui.prepare_cloud_session(settings)
    ui.create_cloud_database.assert_not_called()


def test_old_database_guard_cannot_be_rebound_to_new_grant(fake_st, monkeypatch, settings):
    fake_st.form_submit_button.return_value = True
    database = mock_database(monkeypatch, settings)
    with pytest.raises(Rerun):
        ui.prepare_cloud_session(settings)
    old_guard = database.action_guard
    fake_st.session_state["_cloud_lease"] = access.SessionLease(OWNER)
    with pytest.raises(access.CloudAccessError, match="no longer authorized"):
        old_guard()
    assert ui._QUOTAS._global["db"] == 1


def test_disconnected_database_guard_cannot_reopen_disposed_engine(fake_st, monkeypatch, settings):
    fake_st.form_submit_button.return_value = True
    database = mock_database(monkeypatch, settings)
    with pytest.raises(Rerun):
        ui.prepare_cloud_session(settings)
    old_guard = database.action_guard
    with pytest.raises(Rerun):
        ui._disconnect()
    with pytest.raises(access.CloudAccessError, match="no longer authorized"):
        old_guard()
    assert ui._QUOTAS._global["db"] == 1
    database.engine.dispose.assert_called_once()


def test_disconnect_disposes_clears_history_but_does_not_renew_grant(fake_st):
    lease = establish(fake_st)
    database = Mock()
    fake_st.session_state.update(_cloud_database=database, chats={"secret": "history"}, dark_mode=False)
    with pytest.raises(Rerun):
        ui._disconnect()
    assert fake_st.session_state == {"dark_mode": False, "_cloud_owner": OWNER,
                                     "_cloud_lease": lease, "_cloud_display_name": "Invited User"}
    database.engine.dispose.assert_called_once()


def test_logout_cleans_before_native_logout_and_rerun(fake_st):
    establish(fake_st)
    database = Mock()
    fake_st.session_state.update(_cloud_database=database, chats={"private": "history"}, dark_mode=True)

    def logout():
        assert fake_st.session_state == {"dark_mode": True}
        database.engine.dispose.assert_called_once()

    fake_st.logout.side_effect = logout
    with pytest.raises(Rerun):
        ui._logout()


def test_account_popover_includes_database_disconnect_and_signout(fake_st, settings):
    establish(fake_st)
    fake_st.session_state["_cloud_database"] = SimpleNamespace(settings=replace(settings, database="tenant"))
    ui.render_cloud_account()
    fake_st.popover.assert_called_once_with("User / Invited User")
    assert any(call.args == ("Connected database: tenant",) for call in fake_st.text.call_args_list)
    assert [call.args[0] for call in fake_st.button.call_args_list] == ["Disconnect", "Sign out"]