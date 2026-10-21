"""Native Streamlit cloud auth/onboarding; no chat_app import or global DB cache.

Call prepare_cloud_session(settings) BEFORE main initializes profiles/history.
Keep _cloud_* session keys intact during main initialization. The returned DB is
already guarded; do not replace it with Database(settings). Main must separately
guard every model request with cloud_access.check_cloud_action("model"), disable
cloud email UI, and call render_cloud_account instead of the local profile UI.

Required Streamlit secrets: [auth] redirect_uri, cookie_secret and the native
default provider's client_id, client_secret, server_metadata_url; alternatively
put the provider fields in [auth.google]. Configure Google's OIDC metadata URL.
Environment: CHATBOT_MODE=cloud, CHATBOT_OIDC_ISSUER=https://accounts.google.com,
CHATBOT_CLOUD_ALLOWED_SUBJECTS=<sub> or <issuer>|<sub>, plus the AI provider
settings documented by Settings. No .env is read here. No raw credentials are
stored outside the session-local database/settings and transient form values.
"""

from collections.abc import Mapping
from dataclasses import replace
import os

import streamlit as st

from chatbot.cloud_access import (
    AccessPolicy, CloudAccessError, CloudIdentity, SessionLease, _QUOTAS,
    clear_cloud_session,
)
from chatbot.cloud_connection import ConnectionRequest, create_cloud_database
from chatbot.config import Settings
from chatbot.db import Database


def _auth_configuration() -> tuple[AccessPolicy, str | None]:
    """Validate configuration on every sensitive action, including invite changes."""
    policy = AccessPolicy.from_config(
        os.environ.get("CHATBOT_OIDC_ISSUER", ""),
        os.environ.get("CHATBOT_CLOUD_ALLOWED_SUBJECTS", ""),
    )
    try:
        auth = st.secrets["auth"]
        if not isinstance(auth, Mapping):
            raise ValueError
        provider_name = "google" if "google" in auth else None
        provider = auth["google"] if provider_name else auth
        if not isinstance(provider, Mapping):
            raise ValueError
        for source, keys in ((auth, ("redirect_uri", "cookie_secret")),
                             (provider, ("client_id", "client_secret", "server_metadata_url"))):
            if any(not isinstance(source.get(key), str) or not source[key].strip() for key in keys):
                raise ValueError
        if provider["server_metadata_url"] != "https://accounts.google.com/.well-known/openid-configuration":
            raise ValueError
    except Exception:
        raise CloudAccessError("Configure Streamlit native Google authentication secrets.") from None
    return policy, provider_name


def _current_identity(policy: AccessPolicy) -> CloudIdentity:
    try:
        user = st.user
        return policy.authenticate(dict(user), is_logged_in=user.is_logged_in)
    except CloudAccessError:
        raise
    except Exception:
        raise CloudAccessError("Native authentication is unavailable. Sign in again.") from None


def _check_session() -> CloudIdentity:
    """Check identity and lease without extending idle time or exposing history."""
    try:
        policy, _ = _auth_configuration()
        identity = _current_identity(policy)
        state = st.session_state
        lease = state.get("_cloud_lease")
        if state.get("_cloud_owner") != identity.owner_key or not isinstance(lease, SessionLease):
            raise CloudAccessError("Your cloud session is no longer authorized. Sign in again.")
        lease.check(identity.owner_key, touch=False)
        return identity
    except CloudAccessError:
        clear_cloud_session(st.session_state)
        # Clearing the lease alone would let the same native cookie obtain a
        # fresh one on the next rerun, bypassing the absolute app grant TTL.
        st.logout()
        raise


def _check_cloud_action(kind: str) -> None:
    identity = _check_session()
    _QUOTAS.charge(identity.owner_key, kind)
    st.session_state["_cloud_lease"].check(identity.owner_key)


def _logout() -> None:
    clear_cloud_session(st.session_state)
    st.logout()
    st.rerun()
    st.stop()


def _stop_denied(message: str, *, sign_out: bool = False) -> None:
    clear_cloud_session(st.session_state)
    st.error(message)
    if sign_out:
        st.logout()
    st.stop()


def _ai_configured(settings: Settings) -> bool:
    if not settings.api_key.strip() or not settings.model.strip():
        return False
    if settings.provider == "azure":
        return bool(settings.azure_endpoint.strip() and settings.azure_api_version.strip())
    return settings.provider == "openai"


def _disconnect() -> None:
    # A disconnect is not a new auth grant: retain only the validated lease and
    # identity, otherwise repeated disconnects could bypass the absolute TTL.
    identity = _check_session()
    lease = st.session_state["_cloud_lease"]
    name = st.session_state.get("_cloud_display_name", identity.display_name)
    clear_cloud_session(st.session_state)
    st.session_state.update(_cloud_owner=identity.owner_key, _cloud_lease=lease,
                            _cloud_display_name=name)
    st.rerun()
    st.stop()


def render_cloud_account() -> None:
    """Render authenticated sidebar account controls, not local profile controls."""
    try:
        identity = _check_session()
    except CloudAccessError as exc:
        _stop_denied(str(exc), sign_out=True)
        return
    with st.sidebar:
        with st.popover("User / " + identity.display_name):
            st.text(identity.display_name)
            database = st.session_state.get("_cloud_database")
            if database is not None:
                st.text("Connected database: " + database.settings.database)
                if st.button("Disconnect", key="_cloud_disconnect"):
                    _disconnect()
            else:
                st.caption("No database connected")
            if st.button("Sign out", key="_cloud_logout"):
                _logout()


def prepare_cloud_session(settings: Settings) -> tuple[Settings, Database, str, str]:
    """Return connected settings/DB/owner/name or stop before any private UI.

    A fresh native login creates an app lease only after preexisting sensitive
    state is cleared. An expired/mismatched lease never silently creates a new
    grant: sign out and back in. Initial discovery is charged through the same
    DB action guard as later discovery/query calls; main may discover again.
    """
    if not settings.cloud_mode:
        _stop_denied("Cloud onboarding requires cloud mode.")
    try:
        policy, provider_name = _auth_configuration()
    except CloudAccessError as exc:
        _stop_denied(str(exc))
        raise  # st.stop never returns
    try:
        logged_in = st.user.is_logged_in is True
    except Exception:
        _stop_denied("Native authentication is unavailable.")
        raise
    if not logged_in:
        clear_cloud_session(st.session_state)
        st.title("Sign in to your database assistant")
        if st.button("Sign in with Google", key="_cloud_login"):
            st.login(provider_name) if provider_name else st.login()
        st.stop()
    try:
        identity = _current_identity(policy)
        state = st.session_state
        if "_cloud_owner" not in state:
            # Reject incomplete cached grants instead of rebinding their data.
            if "_cloud_lease" in state or "_cloud_database" in state:
                raise CloudAccessError("Your cloud session is invalid. Sign in again.")
            clear_cloud_session(state)
            state.update(_cloud_owner=identity.owner_key,
                         _cloud_display_name=identity.display_name,
                         _cloud_lease=SessionLease(identity.owner_key))
        identity = _check_session()
    except CloudAccessError as exc:
        _stop_denied(str(exc), sign_out=True)
        raise
    if not _ai_configured(settings):
        _stop_denied("The operator must configure the AI provider before cloud onboarding.", sign_out=True)
    database = state.get("_cloud_database")
    if database is not None:
        return database.settings, database, identity.owner_key, identity.display_name

    render_cloud_account()
    st.title("Connect your MySQL database")
    st.caption("Use a dedicated read-only account on a public MySQL host with verified TLS (port 3306).")
    with st.form("_cloud_connection_form", clear_on_submit=True):
        # No explicit widget keys: submitted secrets must not become reusable
        # application session keys. clear_on_submit clears frontend form values.
        host = st.text_input("Host")
        database_name = st.text_input("Database")
        table = st.text_input("Table (optional; leave blank to query the database schema)")
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        permission = st.checkbox("I have permission to access this database using this read-only account.")
        consent = st.checkbox("I consent to sending database schema and my questions to the configured AI provider.")
        summaries = st.checkbox("Also allow query results to be sent to AI for summaries (optional).")
        submitted = st.form_submit_button("Connect")
    if submitted:
        if not permission or not consent:
            st.error("Database permission and schema/questions AI consent are required.")
        else:
            connected = None
            try:
                # Charge all submitted connection attempts, including invalid
                # identifiers/hosts, before validation, DNS or engine creation.
                _check_cloud_action("connection")
                request = ConnectionRequest(host.strip(), database_name.strip(), username.strip(),
                                            password, table.strip() or None)
                derived = replace(settings, cloud_mode=True, allow_mutations=False,
                                  openai_consent=True, ai_summaries=summaries,
                                  client_scope="table" if request.table else "schema")
                connected = create_cloud_database(derived, request)
                expected_owner = identity.owner_key
                expected_lease = state["_cloud_lease"]
                active = False

                def guard() -> None:
                    # A retained old Database must never become usable by a new
                    # owner, renewed grant, or after disconnect (disposing an
                    # SQLAlchemy engine alone does not prevent future connects).
                    current = _check_session()
                    if (current.owner_key != expected_owner
                            or st.session_state.get("_cloud_lease") is not expected_lease
                            or (active and st.session_state.get("_cloud_database") is not connected)):
                        raise CloudAccessError("This database connection is no longer authorized.")
                    _check_cloud_action("db")

                connected.action_guard = guard
                schema = connected.discover_schema()
                if not schema.exists:
                    raise CloudAccessError("No queryable tables were found.")
                # Discovery can take time: do not store a connection if the
                # grant or native authentication expired during that call.
                _check_session()
            except Exception:
                if connected is not None:
                    try:
                        connected.engine.dispose()
                    except Exception:
                        pass
                st.error("Cannot connect. Check access, consent, session validity and cloud quotas.")
            else:
                state["_cloud_database"] = connected
                state["_cloud_settings"] = connected.settings
                state["_cloud_schema"] = schema
                active = True
                st.rerun()
    st.stop()