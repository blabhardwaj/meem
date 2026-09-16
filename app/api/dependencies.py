"""
Shared FastAPI dependencies.

get_current_user — the single auth entry point for HTTP routes: turns a
Bearer token into a fully resolved per-team identity (the same
ResolvedIdentity that GET /auth/me returns), or a 401.

Connection-pool note: FastAPI caches a Depends() result per request, keyed
by the dependency callable — so get_current_user and get_db_with_tenant both
depending on _request_db() below means a request that uses BOTH (nearly
every real endpoint: identity + a tenant-scoped session) opens exactly ONE
SessionLocal(), not two. This used to open two separate sessions per
request (get_current_user via get_db, get_db_with_tenant via its own
SessionLocal()) — with Supabase's pooler capped at 15 session-mode
connections total, that doubling was enough on its own to exhaust the pool
under ordinary concurrent traffic (several browser tabs/panels polling at
once), surfacing as "max clients reached in session mode" on whatever
request happened to be unlucky enough to ask for a connection last.
resolve_identity() already does the one SET LOCAL app.current_tenant_id
this session needs (it must, to read the caller's own team/admin rows,
which FORCE RLS) — get_db_with_tenant now simply reuses that same
already-scoped session instead of re-deriving and re-scoping a second one.
"""

import uuid

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.database import SessionLocal

from app.services.auth import ResolvedIdentity, resolve_identity, verify_session_token

_bearer_scheme = HTTPBearer(auto_error=False)


def _request_db():
    """
    The one DB session for this request. Depended on by both
    get_current_user and get_db_with_tenant — FastAPI's per-request
    dependency cache guarantees this body runs (and SessionLocal() is
    called) at most once per request no matter how many endpoint
    parameters/dependencies ask for it.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(_request_db),
) -> ResolvedIdentity:
    """
    Verify the ``Authorization: Bearer <token>`` header and return the
    caller's resolved identity. Raises 401 when the header is missing or the
    token is malformed / expired / points at a user that no longer exists.
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        raw_uid, token_version = verify_session_token(credentials.credentials)
        user_id = uuid.UUID(raw_uid)
        return resolve_identity(db, user_id, token_version=token_version)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def require_org_admin(
    identity: ResolvedIdentity = Depends(get_current_user),
) -> ResolvedIdentity:
    if not identity.is_org_admin:
        raise HTTPException(status_code=403, detail="Organization admin access required")
    return identity


def get_db_with_tenant(
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(_request_db),
):
    # resolve_identity() (inside get_current_user, on this SAME session —
    # see _request_db above) already ran `SET LOCAL app.current_tenant_id`
    # for identity.tenant_id, so this session is already correctly scoped.
    # db.info["tenant_id"] is still set for completeness/consistency with
    # code that inspects it directly, and in case this transaction ends and
    # a new one begins within the same request (after_begin in
    # app/database.py re-applies it from db.info on every new transaction).
    db.info["tenant_id"] = identity.tenant_id
    return db


