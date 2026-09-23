"""
Phase 2 authentication endpoints.

  POST /auth/signup            email + password -> new User (hashed pw) + token
  POST /auth/login             email + password -> session token
  GET  /auth/google/authorize  -> Google OAuth authorization URL (+ state cookie)
  GET  /auth/google/callback   OAuth callback -> find/create User -> 302 to SPA
  GET  /auth/me                current token -> resolved per-team identity

Phase 11 additions:
  POST /auth/register-org      create a brand-new Tenant + its first org_admin User
  POST /auth/accept-invite     redeem an admin-issued invite token -> new User + token
  PUT  /auth/password          change own password -> bumps token_version (logs out
                               every other session)

Not yet wired into access_control.py / session_context.py — that is Phase 3.
"""

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, get_db_with_tenant
from app.config import DEFAULT_SIGNUP_TENANT_ID
from app.database import get_db
from app.limiter import limiter
from app.models.invitation import Invitation
from app.models.team import ProjectAdmin, TeamRole, UserTeamMembership
from app.models.tenant import Tenant

from app.models.user import User
from app.services.audit import record_audit
from app.services.auth import (
    ResolvedIdentity,
    clear_failed_logins,
    consume_exchange_code,
    create_oauth_exchange_code,
    create_oauth_state,
    create_session_token,
    exchange_google_code,
    google_authorization_url,
    google_is_configured,
    hash_password,
    is_locked_out,
    LOCKOUT_DURATION_MINUTES,
    oauth_state_cookie_kwargs,
    post_login_redirect_url,
    record_failed_login,
    validate_oauth_state,
    verify_password,
)

INVITE_TTL_HOURS = 72


router = APIRouter(prefix="/auth", tags=["auth"])

_OAUTH_STATE_COOKIE = "google_oauth_state"


# --- request / response models ----------------------------------------------

class SignupRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=8, max_length=256)


class LoginRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class RegisterOrgRequest(BaseModel):
    org_name: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=8, max_length=256)
    full_name: str | None = Field(default=None, max_length=255)


class AcceptInviteRequest(BaseModel):
    token: str = Field(min_length=1)
    password: str = Field(min_length=8, max_length=256)
    full_name: str | None = Field(default=None, max_length=255)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    is_new_user: bool = False


class GoogleAuthorizeResponse(BaseModel):
    authorization_url: str


class TeamMembershipOut(BaseModel):
    team_id: str
    project_id: str
    role: str


class MeResponse(BaseModel):
    user_id: str
    email: str
    full_name: str | None = None
    tenant_id: str
    tenant_name: str
    is_org_admin: bool
    team_memberships: list[TeamMembershipOut]
    project_admin_project_ids: list[str]


# --- helpers ----------------------------------------------------------------

def _normalize_email(raw: str) -> str:
    email = raw.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(status_code=422, detail="Enter a valid email address")
    return email


def _default_signup_tenant(db: Session) -> uuid.UUID:
    if not DEFAULT_SIGNUP_TENANT_ID:
        raise HTTPException(
            status_code=500,
            detail="DEFAULT_SIGNUP_TENANT_ID is not configured — cannot create accounts",
        )
    try:
        tenant_id = uuid.UUID(DEFAULT_SIGNUP_TENANT_ID)
    except ValueError:
        raise HTTPException(
            status_code=500, detail="DEFAULT_SIGNUP_TENANT_ID is not a valid UUID"
        ) from None
    if db.get(Tenant, tenant_id) is None:
        raise HTTPException(
            status_code=500,
            detail="DEFAULT_SIGNUP_TENANT_ID does not match any tenant",
        )
    return tenant_id


# --- endpoints --------------------------------------------------------------

@router.post("/signup", response_model=TokenResponse, status_code=201)
@limiter.limit("3/minute")
def signup(request: Request, body: SignupRequest, db: Session = Depends(get_db)):
    email = _normalize_email(body.email)
    tenant_id = _default_signup_tenant(db)

    if db.execute(select(User).where(User.email == email)).scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="An account with that email already exists")

    user = User(email=email, tenant_id=tenant_id, password_hash=hash_password(body.password))
    db.add(user)
    db.flush()
    record_audit(db, actor_id=user.user_id, action="SIGNUP", resource_type="user",
                 resource_id=user.user_id, details={"method": "password"})
    db.commit()
    db.refresh(user)

    return TokenResponse(
        access_token=create_session_token(str(user.user_id), user.token_version),
        is_new_user=True,
    )


@router.post("/login", response_model=TokenResponse)
@limiter.limit("60/minute")  # per-IP flood guard; per-account brute-force is handled by lockout
def login(request: Request, body: LoginRequest, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None or not user.password_hash:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if is_locked_out(user):
        raise HTTPException(
            status_code=423,
            detail=f"Account locked due to repeated failed logins. Try again in "
                   f"{LOCKOUT_DURATION_MINUTES} minutes.",
        )

    if not verify_password(body.password, user.password_hash):
        record_failed_login(db, user)
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid email or password")

    clear_failed_logins(db, user)
    db.commit()
    # LOGIN events are deliberately not audited (finalized Audit Log design —
    # the log carries account/permission-management actions only).
    return TokenResponse(
        access_token=create_session_token(str(user.user_id), user.token_version)
    )



@router.get("/google/authorize", response_model=GoogleAuthorizeResponse)
def google_authorize(request: Request, response: Response):
    if not google_is_configured():
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")
    state = create_oauth_state()
    response.set_cookie(_OAUTH_STATE_COOKIE, state, **oauth_state_cookie_kwargs(request))
    return GoogleAuthorizeResponse(authorization_url=google_authorization_url(state))


@router.get("/google/callback")
def google_callback(code: str, state: str, request: Request, db: Session = Depends(get_db)):
    cookie_state = request.cookies.get(_OAUTH_STATE_COOKIE)
    if (
        not cookie_state
        or not validate_oauth_state(state)
        or not hmac.compare_digest(state, cookie_state)
    ):
        raise HTTPException(status_code=400, detail="Invalid Google sign-in state")

    try:
        identity = exchange_google_code(code)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    email = identity["email"].strip().lower()
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    is_new_user = user is None
    if user is None:
        tenant_id = _default_signup_tenant(db)
        user = User(email=email, tenant_id=tenant_id, password_hash=None)
        db.add(user)
        db.flush()
        record_audit(db, actor_id=user.user_id, action="SIGNUP", resource_type="user",
                     resource_id=user.user_id, details={"method": "google"})
    # LOGIN events are deliberately not audited (see POST /auth/login).
    db.commit()
    db.refresh(user)

    token = create_session_token(str(user.user_id), user.token_version)
    exchange_code = create_oauth_exchange_code(token)
    redirect = RedirectResponse(
        post_login_redirect_url(exchange_code, is_new_user=is_new_user), status_code=302
    )
    redirect.delete_cookie(_OAUTH_STATE_COOKIE, path="/")
    return redirect


class ExchangeRequest(BaseModel):
    code: str = Field(min_length=1)


@router.post("/exchange", response_model=TokenResponse)
def exchange_code(req: ExchangeRequest):
    try:
        token = consume_exchange_code(req.code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid or expired exchange code") from exc
    return TokenResponse(access_token=token)



@router.get("/me", response_model=MeResponse)
def me(
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    tenant = db.get(Tenant, identity.tenant_id)
    user = db.get(User, identity.user_id)
    return MeResponse(
        user_id=str(identity.user_id),
        email=identity.email,
        full_name=user.full_name if user else None,
        tenant_id=str(identity.tenant_id),
        tenant_name=tenant.name if tenant else "",
        is_org_admin=identity.is_org_admin,
        team_memberships=[
            TeamMembershipOut(
                team_id=str(m.team_id),
                project_id=str(m.project_id),
                role=m.role.value,
            )
            for m in identity.team_memberships
        ],
        project_admin_project_ids=[str(pid) for pid in identity.project_admin_project_ids],
    )


@router.post("/register-org", response_model=TokenResponse, status_code=201)
@limiter.limit("3/minute")
def register_org(request: Request, body: RegisterOrgRequest, db: Session = Depends(get_db)):
    """Create a brand-new Tenant plus its first user, who becomes org_admin."""
    email = _normalize_email(body.email)
    if db.execute(select(User).where(User.email == email)).scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="An account with that email already exists")

    tenant = Tenant(name=body.org_name.strip())
    db.add(tenant)
    db.flush()  # tenant.tenant_id populated, not yet committed

    user = User(
        email=email,
        tenant_id=tenant.tenant_id,
        full_name=body.full_name,
        is_org_admin=True,
        password_hash=hash_password(body.password),
    )
    db.add(user)
    db.flush()
    record_audit(db, actor_id=user.user_id, action="SIGNUP", resource_type="user",
                 resource_id=user.user_id, details={"method": "register_org", "tenant_id": str(tenant.tenant_id)})
    db.commit()
    db.refresh(user)

    return TokenResponse(
        access_token=create_session_token(str(user.user_id), user.token_version),
        is_new_user=True,
    )


@router.post("/accept-invite", response_model=TokenResponse, status_code=201)
@limiter.limit("10/minute")
def accept_invite(request: Request, body: AcceptInviteRequest, db: Session = Depends(get_db)):
    token_hash = hashlib.sha256(body.token.encode()).hexdigest()
    invitation = db.execute(
        select(Invitation).where(Invitation.token_hash == token_hash)
    ).scalar_one_or_none()

    if invitation is None:
        raise HTTPException(status_code=400, detail="Invalid or unknown invite token")
    if invitation.accepted_at is not None:
        raise HTTPException(status_code=400, detail="This invite has already been used")
    if invitation.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="This invite has expired")

    # invitations carries no RLS policy (looked up pre-auth by token hash,
    # tenant-agnostic by necessity), so the query above ran with no tenant
    # context. Every table touched from here on IS RLS-scoped — some
    # (user_team_memberships, project_admins) with FORCE ROW LEVEL SECURITY,
    # which applies even to docflow_app as table owner, unlike `users`
    # (ENABLE only, owner bypasses by default). db.info["tenant_id"] only
    # takes effect on the session's NEXT transaction (after_begin,
    # app/database.py) — rollback the still-open first transaction (nothing
    # was written yet) before setting it.
    invitation_tenant_id = invitation.tenant_id
    db.rollback()
    db.info["tenant_id"] = str(invitation_tenant_id)

    email = invitation.email
    if db.execute(select(User).where(User.email == email)).scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="An account with that email already exists")

    user = User(
        email=email,
        tenant_id=invitation.tenant_id,
        full_name=body.full_name,
        is_org_admin=(invitation.role == "org_admin"),
        password_hash=hash_password(body.password),
    )
    db.add(user)
    db.flush()

    # Master Plan v2, item 16: an invite carrying a project/team scope
    # assigns that access automatically on accept, instead of leaving the
    # new user with a bare org account and no project access.
    if invitation.team_id is not None:
        db.add(UserTeamMembership(
            user_id=user.user_id, team_id=invitation.team_id,
            project_id=invitation.project_id, role=TeamRole(invitation.role),
        ))
    elif invitation.project_id is not None and invitation.role == "project_admin":
        db.add(ProjectAdmin(user_id=user.user_id, project_id=invitation.project_id))

    invitation.accepted_at = datetime.now(timezone.utc)
    db.add(invitation)
    record_audit(db, actor_id=user.user_id, action="SIGNUP", resource_type="user",
                 resource_id=user.user_id,
                 details={"method": "accept_invite", "invitation_id": str(invitation.invitation_id)})
    db.commit()
    db.refresh(user)

    return TokenResponse(
        access_token=create_session_token(str(user.user_id), user.token_version),
        is_new_user=True,
    )


@router.put("/password")
def change_password(
    body: ChangePasswordRequest,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Change own password. Bumps token_version, invalidating every other session."""
    user = db.get(User, identity.user_id)
    if user is None or not user.password_hash or not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    user.password_hash = hash_password(body.new_password)
    user.token_version = (user.token_version or 0) + 1
    db.add(user)
    db.commit()
    db.refresh(user)

    return TokenResponse(
        access_token=create_session_token(str(user.user_id), user.token_version)
    )
