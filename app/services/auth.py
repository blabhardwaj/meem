"""
Phase 2 authentication primitives.

Adapted from the teammate's `backend/app/auth.py` (per MERGE_DECISIONS §2):
  - password hashing / verification — PBKDF2-HMAC-SHA256, 310k iterations,
    constant-time comparison. Same primitive as his; the one change is a
    per-user random salt stored with the hash instead of a single global
    salt derived from the app secret.
  - session tokens — HMAC-SHA256-signed, base64url payload, expiry. Adopted
    as-is, minus his dev/admin bypass path (`payload["sub"] == auth_username`)
    — real auth only here.
  - Google OAuth2 — CSRF state (signed + short-lived), auth-code exchange,
    ID-token verification via Google's tokeninfo endpoint. Adopted as-is.

The resolved-identity object (`ResolvedIdentity`) is NOT a port of his
`UserContext`: it is built from our per-team role model
(UserTeamMembership / ProjectAdmin / User.is_org_admin) and deliberately
does not collapse into a single role string.

NOTHING here is wired into access_control.py or session_context.py — that
integration is Phase 3.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from datetime import datetime, timedelta, timezone

from app.config import (
    FRONTEND_URL,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GOOGLE_OAUTH_REDIRECT_URI,
    PASSWORD_HASH_ITERATIONS,
    SESSION_TOKEN_SECRET,
    SESSION_TOKEN_TTL_SECONDS,
)
from app.models.team import ProjectAdmin, TeamRole, UserTeamMembership
from app.models.user import User

_PBKDF2_ALGO = "sha256"
_PBKDF2_PREFIX = "pbkdf2_sha256"

# --- login lockout ------------------------------------------------------------

MAX_FAILED_LOGIN_ATTEMPTS = 10
LOCKOUT_DURATION_MINUTES = 15


def is_locked_out(user: User) -> bool:
    return bool(user.locked_until and user.locked_until > datetime.now(timezone.utc))


def record_failed_login(db: Session, user: User) -> None:
    """Bump the failure counter; lock the account once the threshold is hit."""
    user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
    if user.failed_login_attempts >= MAX_FAILED_LOGIN_ATTEMPTS:
        user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_DURATION_MINUTES)
    db.add(user)


def clear_failed_logins(db: Session, user: User) -> None:
    if user.failed_login_attempts or user.locked_until:
        user.failed_login_attempts = 0
        user.locked_until = None
        db.add(user)


# --- password hashing --------------------------------------------------------

def hash_password(password: str) -> str:
    """
    PBKDF2-HMAC-SHA256 with a fresh 16-byte random salt. Returns a
    self-describing string: ``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>``.
    """
    salt = secrets.token_bytes(16)
    iterations = PASSWORD_HASH_ITERATIONS
    digest = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode(), salt, iterations)
    return f"{_PBKDF2_PREFIX}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of ``password`` against a hash_password() string."""
    try:
        prefix, iter_str, salt_hex, digest_hex = stored.split("$")
        if prefix != _PBKDF2_PREFIX:
            return False
        iterations = int(iter_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (AttributeError, ValueError):
        return False
    candidate = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode(), salt, iterations)
    return hmac.compare_digest(candidate, expected)


# --- session tokens ---------------------------------------------------------

def _require_secret() -> str:
    if not SESSION_TOKEN_SECRET:
        raise RuntimeError(
            "SESSION_TOKEN_SECRET is not set — cannot sign or verify auth tokens"
        )
    return SESSION_TOKEN_SECRET


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _encode(payload: dict) -> str:
    return _b64e(json.dumps(payload, separators=(",", ":")).encode())


def _decode(value: str) -> str:
    return _b64d(value).decode()


def _sign(value: str) -> str:
    mac = hmac.new(_require_secret().encode(), value.encode(), hashlib.sha256).digest()
    return _b64e(mac)


def create_session_token(user_id: str, token_version: int = 0) -> str:
    """HMAC-signed token carrying the user id, token_version, and an absolute expiry."""
    payload = {
        "uid": str(user_id),
        "tv": int(token_version),
        "exp": int(time.time()) + SESSION_TOKEN_TTL_SECONDS,
    }
    encoded = _encode(payload)
    return f"{encoded}.{_sign(encoded)}"


def verify_session_token(token: str) -> tuple[str, int]:
    """Return (user_id, token_version) from a valid token; raise ValueError otherwise."""
    try:
        encoded, signature = token.split(".", 1)
    except (AttributeError, ValueError) as exc:
        raise ValueError("Malformed token") from exc
    if not hmac.compare_digest(signature, _sign(encoded)):
        raise ValueError("Bad token signature")
    try:
        payload = json.loads(_decode(encoded))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Malformed token payload") from exc
    if not payload.get("uid") or int(payload.get("exp", 0)) < int(time.time()):
        raise ValueError("Expired or invalid token")
    return str(payload["uid"]), int(payload.get("tv", 0))


# --- Google OAuth2 --------------------------------------------------------------

def google_is_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def create_oauth_state() -> str:
    """Signed, single-use, 10-minute CSRF state token."""
    payload = {"nonce": secrets.token_urlsafe(32), "exp": int(time.time()) + 600}
    encoded = _encode(payload)
    return f"{encoded}.{_sign(encoded)}"


def validate_oauth_state(state: str) -> bool:
    try:
        encoded, signature = state.split(".", 1)
        if not hmac.compare_digest(signature, _sign(encoded)):
            return False
        payload = json.loads(_decode(encoded))
        return bool(payload.get("nonce")) and int(payload.get("exp", 0)) >= int(time.time())
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False


def oauth_state_cookie_kwargs(request) -> dict:
    """
    Cookie kwargs for the OAuth state cookie. `secure` is only True on an
    actual HTTPS request — browsers silently drop Secure cookies over plain
    HTTP, which breaks the flow when testing on http://localhost.
    """
    return {
        "httponly": True,
        "secure": request.url.scheme == "https",
        "samesite": "lax",
        "max_age": 600,
        "path": "/",
    }


def google_authorization_url(state: str) -> str:
    if not google_is_configured():
        raise ValueError("Google sign-in is not configured")
    query = urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "state": state,
        "prompt": "select_account",
    })
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"


def exchange_google_code(code: str) -> dict:
    """
    Trade an auth code for tokens, then verify the ID token against Google's
    tokeninfo endpoint. Returns {"subject", "email", "name"} on success;
    raises ValueError otherwise.
    """
    if not google_is_configured():
        raise ValueError("Google sign-in is not configured")
    payload = urlencode({
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "grant_type": "authorization_code",
    }).encode()
    token_request = Request(
        "https://oauth2.googleapis.com/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(token_request, timeout=15) as response:
            token_data = json.loads(response.read())
    except Exception as exc:  # noqa: BLE001 — network/parse failures all map to one error
        raise ValueError("Google token exchange failed") from exc

    identity_token = token_data.get("id_token")
    if not identity_token:
        raise ValueError("Google did not return an identity token")
    try:
        with urlopen(
            f"https://oauth2.googleapis.com/tokeninfo?id_token={identity_token}", timeout=15
        ) as response:
            identity = json.loads(response.read())
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Google identity verification failed") from exc

    if identity.get("aud") != GOOGLE_CLIENT_ID or identity.get("email_verified") != "true":
        raise ValueError("Google account could not be verified")
    return {
        "subject": identity["sub"],
        "email": identity["email"].lower(),
        "name": identity.get("name"),
    }


_exchange_store: dict[str, tuple[str, float]] = {}  # {code: (token, expiry_timestamp)}


def create_oauth_exchange_code(token: str) -> str:
    code = secrets.token_urlsafe(16)
    _exchange_store[code] = (token, time.time() + 30)  # 30-second TTL
    return code


def consume_exchange_code(code: str) -> str:
    entry = _exchange_store.pop(code, None)
    if not entry or entry[1] < time.time():
        raise ValueError("Invalid or expired exchange code")
    return entry[0]


def post_login_redirect_url(code: str, *, is_new_user: bool = False) -> str:
    """Where the Google callback sends the browser once a session token exists."""
    params = {"oauth_code": code}
    if is_new_user:
        params["is_new_user"] = "true"
    return f"{FRONTEND_URL.rstrip('/')}/?{urlencode(params)}"



# --- resolved identity (our per-team model, NOT his UserContext) ---------------

@dataclass(frozen=True)
class TeamMembership:
    team_id: uuid.UUID
    project_id: uuid.UUID
    role: TeamRole


@dataclass(frozen=True)
class ResolvedIdentity:
    """
    Everything auth knows about the caller after verifying their token.
    Roles are kept per-(team, project); admin scopes are listed separately.
    Consumed by Phase 3 when this replaces the CLI session_context path.
    """
    user_id: uuid.UUID
    email: str
    tenant_id: uuid.UUID
    is_org_admin: bool
    team_memberships: list[TeamMembership]
    project_admin_project_ids: list[uuid.UUID]

    def role_on_team(self, team_id: uuid.UUID, project_id: uuid.UUID) -> str:
        """
        The effective role string for permission / sensitivity decisions, in the
        same precedence has_permission() uses:
          org_admin > project_admin > per-team role > (no membership -> viewer)
        """
        if self.is_org_admin:
            return "org_admin"
        if project_id in self.project_admin_project_ids:
            return "project_admin"
        membership = next(
            (m for m in self.team_memberships if m.team_id == team_id), None
        )
        return membership.role.value if membership else "viewer"


def resolve_identity(
    db: Session, user_id: uuid.UUID, token_version: int | None = None
) -> ResolvedIdentity:
    """
    Build the full picture: every UserTeamMembership row (per-team role),
    every ProjectAdmin scope, plus the org-level flag. No collapsing.
    Raises ValueError if the user id doesn't exist, or if `token_version` is
    given and doesn't match the user's current one (invalidated by a
    password change).
    """
    user = db.get(User, user_id)
    if user is None:
        raise ValueError("User not found")
    if token_version is not None and token_version != user.token_version:
        raise ValueError("Session has been invalidated")

    db.execute(text("SET LOCAL app.current_tenant_id = :tid"), {"tid": str(user.tenant_id)})

    memberships = db.execute(
        select(UserTeamMembership).where(UserTeamMembership.user_id == user.user_id)
    ).scalars().all()
    admin_rows = db.execute(
        select(ProjectAdmin).where(ProjectAdmin.user_id == user.user_id)
    ).scalars().all()

    return ResolvedIdentity(
        user_id=user.user_id,
        email=user.email,
        tenant_id=user.tenant_id,
        is_org_admin=bool(user.is_org_admin),
        team_memberships=[
            TeamMembership(team_id=m.team_id, project_id=m.project_id, role=m.role)
            for m in memberships
        ],
        project_admin_project_ids=[row.project_id for row in admin_rows],
    )
