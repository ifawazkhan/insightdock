"""Cloud security primitives, independent of Streamlit and external services.

Operator configuration (environment only):
* CHATBOT_OIDC_ISSUER: exact Google issuer, normally https://accounts.google.com.
* CHATBOT_CLOUD_ALLOWED_SUBJECTS: nonempty comma-separated OIDC subjects or
  issuer|subject pairs. Bare subjects are bound to the configured issuer; email
  addresses, names, wildcard patterns and email_verified are not identity proofs.

Integration: call cloud_ui.prepare_cloud_session before reading any history and
cloud_ui.render_cloud_account for the sidebar. Call check_cloud_action("model")
immediately before EVERY provider request (including summaries/report narratives).
Database guards charge discovery and queries independently. Reports must use the
guarded DB/model paths; cloud email is deliberately disabled. This module does not
wire those callers or import chat_app. Quotas are process-local: multiple workers
need a shared atomic quota backend before deployment. Restarts reset budgets.
"""

from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
import hashlib
import math
from threading import Lock
from time import monotonic, time


class CloudAccessError(RuntimeError):
    """A sanitized access denial safe to display to the user."""


class QuotaExceeded(CloudAccessError):
    """The action was not authorized or charged."""


def _identity_part(value: object) -> str:
    if (not isinstance(value, str) or not value or len(value) > 2048
            or value != value.strip() or any(ord(c) < 32 for c in value)):
        raise CloudAccessError("A valid OIDC identity is required.")
    return value


def owner_key(issuer: str, subject: str) -> str:
    """Stable tenant key: SHA256(issuer + NUL + subject), never an email hash."""
    issuer, subject = _identity_part(issuer), _identity_part(subject)
    return hashlib.sha256(f"{issuer}\0{subject}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CloudIdentity:
    owner_key: str
    display_name: str


@dataclass(frozen=True)
class AccessPolicy:
    issuer: str
    allowed_subjects: frozenset[str]

    @classmethod
    def from_config(cls, issuer: str, allowed_subjects: str) -> "AccessPolicy":
        # No issuer alias normalization: a token must match the operator's exact
        # configured issuer, even though Google documents two issuer spellings.
        if issuer not in {"https://accounts.google.com", "accounts.google.com"}:
            raise CloudAccessError("Configure CHATBOT_OIDC_ISSUER with a Google issuer.")
        tokens = frozenset(token.strip() for token in allowed_subjects.split(",") if token.strip())
        if not tokens or len(tokens) > 4096:
            raise CloudAccessError("Configure a nonempty cloud subject invite allowlist.")
        subjects = set()
        for token in tokens:
            if "|" in token:
                token_issuer, subject = token.split("|", 1)
                if token_issuer != issuer or "|" in subject:
                    raise CloudAccessError("Invite subjects must use the configured issuer.")
            else:
                subject = token
            _identity_part(subject)
            if subject == "*":
                raise CloudAccessError("Wildcard cloud invitations are not supported.")
            subjects.add(subject)
        return cls(issuer, frozenset(subjects))

    def authenticate(self, claims: Mapping, *, is_logged_in: bool,
                     now: float | None = None) -> CloudIdentity:
        """Trust only claims supplied by Streamlit's native authenticated user.

        This is claim authorization, NOT a JWT verifier. Never pass client input.
        Google/Streamlit may omit exp; when present it must be finite and future.
        """
        if is_logged_in is not True or claims.get("iss") != self.issuer:
            raise CloudAccessError("Sign in with an invited Google account.")
        subject = _identity_part(claims.get("sub"))
        if subject not in self.allowed_subjects:
            raise CloudAccessError("This account is not invited to this application.")
        if "exp" in claims:
            expires = claims["exp"]
            current = time() if now is None else now
            if (isinstance(expires, bool) or not isinstance(expires, (float, int))
                    or not math.isfinite(expires) or not math.isfinite(current)
                    or current >= expires):
                raise CloudAccessError("Your authentication has expired. Sign in again.")
        name = claims.get("name")
        # Display-only, bounded plain text. Never authorize by display name/email.
        name = name.strip()[:100] if isinstance(name, str) else ""
        name = "".join(c for c in name if c.isprintable())
        return CloudIdentity(owner_key(self.issuer, subject), name or "User")


class SessionLease:
    """Absolute 1h / idle 15min app grant; check BEFORE refreshing idle time.

    A lease never renews itself on a rerun or after expiration. Use a monotonic
    clock; OIDC exp is checked separately with wall time on every operation.
    """

    def __init__(self, owner: str, *, clock: Callable[[], float] = monotonic,
                 absolute_ttl: float = 3600, idle_ttl: float = 900):
        self.owner_key = _identity_part(owner)
        if any(not math.isfinite(v) or v <= 0 for v in (absolute_ttl, idle_ttl)):
            raise ValueError("Lease durations must be finite and positive.")
        self._clock = clock
        self._absolute_ttl = absolute_ttl
        self._idle_ttl = idle_ttl
        self._created = self._last_activity = clock()
        if not math.isfinite(self._created):
            raise ValueError("Lease clock must be finite.")
        self._expired = False

    def check(self, owner: str, *, touch: bool = True) -> None:
        current = self._clock()
        if (self._expired or owner != self.owner_key or not math.isfinite(current)
                or current < self._last_activity
                or current - self._created >= self._absolute_ttl
                or current - self._last_activity >= self._idle_ttl):
            self._expired = True
            raise CloudAccessError("Your cloud session expired. Sign in again.")
        if touch:
            self._last_activity = current


class QuotaLimiter:
    """Threadsafe, atomic per-principal AND global fixed hourly budgets.

    At most max_principals entries are retained for the current window. Unknown
    principals fail closed at capacity: active budgets are never evicted. Failed
    admissions do not partially charge another budget. Attempted admitted actions
    are charged even if the subsequent provider/DB operation fails.
    """

    DEFAULT_LIMITS = {"model": (20, 200), "db": (120, 1000), "connection": (5, 20)}

    def __init__(self, *, clock: Callable[[], float] = monotonic,
                 window_seconds: float = 3600, max_principals: int = 4096,
                 limits: Mapping[str, tuple[int, int]] | None = None):
        if not math.isfinite(window_seconds) or window_seconds <= 0 or max_principals < 1:
            raise ValueError("Quota window and principal capacity must be positive.")
        self._limits = dict(self.DEFAULT_LIMITS if limits is None else limits)
        if not self._limits or any(
            not isinstance(n, int) or isinstance(n, bool) or n <= 0
            for pair in self._limits.values() for n in pair
        ) or any(len(pair) != 2 for pair in self._limits.values()):
            raise ValueError("Quota limits must be positive per-user/global pairs.")
        self._clock = clock
        self._window_seconds = window_seconds
        self._max_principals = max_principals
        self._lock = Lock()
        self._window: int | None = None
        self._last_time = -math.inf
        self._principals: dict[str, dict[str, int]] = {}
        self._global: dict[str, int] = {}

    def charge(self, owner: str, kind: str) -> None:
        if not isinstance(owner, str) or not owner or len(owner) > 256:
            raise QuotaExceeded("A valid quota principal is required.")
        if kind not in self._limits:
            raise QuotaExceeded("This cloud action is disabled.")
        with self._lock:
            current = self._clock()
            if not math.isfinite(current) or current < self._last_time:
                raise QuotaExceeded("Cloud quota clock is unavailable.")
            self._last_time = current
            window = math.floor(current / self._window_seconds)
            if self._window != window:
                self._principals.clear()
                self._global.clear()
                self._window = window
            counts = self._principals.get(owner)
            if counts is None and len(self._principals) >= self._max_principals:
                raise QuotaExceeded("Cloud quota capacity reached. Try again next hour.")
            per_user, total = self._limits[kind]
            used = counts.get(kind, 0) if counts is not None else 0
            global_used = self._global.get(kind, 0)
            if used >= per_user or global_used >= total:
                raise QuotaExceeded("Cloud hourly action quota reached. Try again next hour.")
            if counts is None:
                counts = self._principals[owner] = {}
            counts[kind] = used + 1
            self._global[kind] = global_used + 1


# Process shared counters ONLY: no credentials, database objects or user claims.
_QUOTAS = QuotaLimiter()


def clear_cloud_session(state: MutableMapping) -> None:
    """Dispose owned DB(s), then remove ALL state except the boolean theme.

    Covers main's service alias as well as _cloud_database. Disposal failures are
    suppressed so stale credentials/history can never survive failed cleanup.
    """
    databases = [state.get("_cloud_database")]
    service = state.get("service")
    databases.append(getattr(service, "database", None))
    seen = set()
    try:
        for database in databases:
            if database is None or id(database) in seen:
                continue
            seen.add(id(database))
            try:
                database.engine.dispose()
            except Exception:
                pass
    finally:
        theme = state.get("dark_mode")
        for key in list(state):
            del state[key]
        if isinstance(theme, bool):
            state["dark_mode"] = theme


def check_cloud_action(kind: str) -> None:
    """Streamlit operation hook, lazily imported to keep primitives pure.

    Raises CloudAccessError before I/O. Invalid auth/lease clears sensitive state;
    quota exhaustion does not destroy a still-valid session. Email (and unknown
    action kinds) is always denied. Reports consume normal model and DB budgets.
    """
    from chatbot.cloud_ui import _check_cloud_action

    _check_cloud_action(kind)