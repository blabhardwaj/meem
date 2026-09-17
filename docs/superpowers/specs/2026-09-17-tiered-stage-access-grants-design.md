# Tiered Stage Access Grants — Architecture & Implementation Specification

## 0. Relationship to the original grants spec

This supersedes two foundational premises of `docs/superpowers/specs/2026-09-17-confidential-access-grants-design.md` (already implemented in Tasks 1-19 of `docs/superpowers/plans/2026-09-17-confidential-access-grants.md`):

1. **Every grant was always read-only confidential-doc access.** It now has three tiers: `viewer` (see/search public+internal docs in the stage), `contributor` (viewer + edit/upload in that stage), `contributor_confidential` (contributor + confidential docs in that stage).
2. **TTL was fixed by scope** (`GRANT_TTL_DAYS = 90`, `DOCUMENT_GRANT_TTL_HOURS = 72`). The approver now picks the duration per grant: 72 hours, 1 week, 1 month (30 days flat), or no expiration.

Everything this spec does NOT mention (document/team-scope requests, `resolve_effective_access()`'s document/team branches, the `DOCUMENT_AND_TEAM_SCOPE_REQUESTS_ENABLED` policy gate, `_derive_document_team`) is unchanged and stays exactly as implemented — those paths remain dormant behind the existing stage-only policy restriction (`docs/superpowers/specs/2026-09-17-confidential-access-grants-design.md` §13.A / the mid-session scope-change note in the SDD ledger). Tiers and approver-chosen duration apply **only** to stage-scope grants, since that is the only reachable request path today.

**Why not real temporary team membership** (the first mechanism considered): `UserTeamMembership` has a hard `UNIQUE(user_id, team_id, project_id)` constraint, and the only way to reach the stage-request flow (`_derive_stage_team()`) requires the requester to already hold a row on the approving team. Elevating that existing row's role would grant the bump across every stage the team already reaches — precisely the cross-stage leak the redesign exists to prevent. This spec uses a parallel, attribute-based grant instead (an extension of the already-shipped `resolve_effective_access()` pattern), which is inherently stage-scoped by construction and needs no membership mutation or revert-on-expiry logic at all.

## 1. Data model

### 1.1 `AccessRequestStatus` — new value

```python
class AccessRequestStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    denied = "denied"
    revoked = "revoked"   # NEW
```

`revoked` is a terminal state set by an approver on an already-`approved` request, distinct from `denied` (which means "never approved") purely for audit legibility. Every resolver that checks "is this grant currently active" treats `revoked` identically to `denied`/expired — not granted.

### 1.2 `AccessRequest` — two new nullable columns

```python
class GrantTier(str, enum.Enum):
    viewer = "viewer"
    contributor = "contributor"
    contributor_confidential = "contributor_confidential"


class GrantDuration(str, enum.Enum):
    hours_72 = "hours_72"
    week_1 = "week_1"
    month_1 = "month_1"
    unlimited = "unlimited"
```

On `AccessRequest`:
- `tier: Mapped[GrantTier | None]` — `NULL` while pending; set by the approver at decision time (`_decide()`), never by the requester. Populated only when `scope == AccessRequestScope.stage`; stays `NULL` for document/team-scope rows (those keep the old fixed-TTL behavior, unaffected by this spec).
- `duration: Mapped[GrantDuration | None]` — same population rule as `tier`. Stored (rather than re-derived from `expires_at - decided_at`) so the UI can display "1 month" exactly rather than reverse-engineering it from a timestamp delta, which is lossy at daylight-saving/leap-second boundaries.
- `revoked_at: Mapped[datetime | None]` and `revoked_by: Mapped[UUID | None]` (FK `users.user_id`) — set together when status transitions to `revoked`. Mirrors the existing `decided_at`/`decided_by`-style pair already on this model (read the current columns before writing the migration to match naming exactly).

`expires_at` (already a column) becomes genuinely meaningful as `NULL` = no expiration, rather than always being set to a fixed TTL. `is_expired()` (`access_requests_service.py:57-58`) already treats a falsy `expires_at` as "not expired" (`bool(r.expires_at and r.expires_at < now())`) — confirmed this needs no change, it was written generically enough to already handle `NULL` correctly.

### 1.3 Migration

New Alembic revision: `CREATE TYPE grant_tier AS ENUM (...)`, `CREATE TYPE grant_duration AS ENUM (...)` (bootstrap the enum types explicitly before `add_column`, per the Task-1 lesson already recorded in the SDD ledger — `op.add_column()` with `sa.Enum(...)` does not auto-create the backing Postgres type). Add `tier`, `duration`, `revoked_at`, `revoked_by` as nullable columns; extend the existing `access_request_status` Postgres enum with `revoked` via `ALTER TYPE access_request_status ADD VALUE 'revoked'` (must run outside the same transaction as any subsequent use of the new value, per Postgres's `ALTER TYPE ... ADD VALUE` transaction rule — confirm Alembic's `op.execute()` here doesn't get batched with the column-add statements in a way that violates this).

## 2. The ABAC resolver

New function in `app/services/access_control.py`, alongside `resolve_effective_access()`:

```python
@dataclass
class StageGrant:
    tier: GrantTier
    team_id: UUID          # the grant's own routing team (from _derive_stage_team at request time)
    expires_at: datetime | None
    request_id: UUID

def resolve_stage_grant(db: Session, user_id: UUID, stage_id: UUID) -> StageGrant | None:
    """
    The latest approved, non-revoked, non-expired stage-scope grant for
    (user_id, stage_id) — or None. "Latest" = highest decided_at among
    approved rows (a newer APPROVAL supersedes an older one for the same
    (user, stage), regardless of which was originally requested first;
    only ever one active grant per (user, stage) at a time). Pure
    attribute lookup — never touches UserTeamMembership. Expiry needs no
    cleanup: once expires_at passes, this simply stops returning it.
    """
```

Query shape: `AccessRequest` rows where `scope == stage`, `stage_id == stage_id`, `user_id == user_id`, `status == approved`, ordered by `decided_at DESC` (not `requested_at` — an earlier-submitted request can be decided later than a subsequently-submitted one, and it's the decision that supersedes, not the submission order), `LIMIT 1` — then check `not is_expired(row)` before returning (an expired latest-approved row means no active grant, even if an older-but-still-approved row beneath it happens to still be unexpired; per the "supersedes" decision, only the latest counts, so this correctly returns `None` in that case rather than falling back to the older row).

## 3. Enforcement — every consumer, and why each is or isn't affected

| Call site | Change |
|---|---|
| `get_accessible_stages_for_user()` (`access_control.py:191`) | Union in `stage_id`s where `resolve_stage_grant(db, user_id, stage_id) is not None` (any tier — viewer included), for active stages. This is the single choke point already feeding `build_access_filter`, `stages.py` (stage list), `workspace.py`, `project_intelligence.py`, `rag/retrieval.py`, and both `query_tools.py` call sites, plus `graph_tools.py` — none of those eight need individual changes. |
| `resolve_effective_access()` / `is_grant_only_confidential_access()` (`access_control.py`) | The stage-scope branch of `resolve_effective_access()` only reports `"granted"` (confidential unlock) when `resolve_stage_grant(...).tier == contributor_confidential`. Viewer/contributor grants make the stage visible (via the row above) but leave confidential docs there as locked stubs — Task 13's `classify_document_visibility()` needs zero changes, it already calls through this resolver. |
| `document_persistence.py:108-132` (`_check_upload_access`, new-document upload) | After the existing `has_permission(..., "upload", team_id, ...)` + `has_stage_access(...)` check fails, fall back to: `grant = resolve_stage_grant(db, user_id, stage_id); if grant and grant.tier in (contributor, contributor_confidential) and grant.team_id == team_id: allow`. Requiring `team_id == grant.team_id` means a grant holder can only upload attributed to the team the grant itself is routed through (the team that already has `TeamStageAccess` to this stage) — never an arbitrary team they name. |
| `document_review.py:435` (`review_message`, revising an existing document) | Identical fallback, keyed off `document.stage_id` and `document.uploaded_as_team_id` in place of the client-supplied `team_id`/`stage_id` above. |
| `has_stage_access()` (`access_control.py:169`) | **Unchanged.** It stays the narrow "does this team have `TeamStageAccess` to this stage" check. The grant fallback lives in the two call sites above, not inside this function, so `has_stage_access()`'s existing semantics (and its other caller, `query_tools.py:435`, which is asking a different question — "can this specific team see this stage," not "can this user via any means") aren't disturbed. |
| `_derive_stage_team()` (`access_requests_service.py:108-128`) | **Unchanged** (per explicit decision) — still requires the requester to already belong to a team with `TeamStageAccess` to the target stage. Requesting access to a fully-invisible stage stays impossible; no new "browse all stages" UI is introduced. |
| Workflow actions — `approve`/`reject`/`submit_for_review`/finalize (`workflow.py:263`, `document_upload_review.py:134`, `document_finalize.py`) | **Unchanged.** All gated on `"approve"` (team_lead-only) or already covered by the existing `GrantOnlyAccessError`/confidential-read checks from Tasks 7-9. No tier confers approval rights — matches the spec's own tier definitions (none of the three mention approval). |
| RAG retrieval (`rag/retrieval.py`) and RAG tools (`rag_tools.py`, `query_tools.py`, `graph_tools.py`) | Inherit correct behavior automatically via `get_accessible_stages_for_user()`. Confidential-doc filtering within a now-visible stage still runs through the same `resolve_effective_access()`/`is_grant_only_confidential_access()` calls those paths already make per document — no separate change needed. (The open item from mid-session scope-change #2 — auditing exact RAG *messaging* for zero-hit vs. hidden-count leakage — is tracked separately and resumes after this spec ships, per the SDD ledger.) |

## 4. Request/approval lifecycle changes

### 4.1 Re-requesting for a higher tier

`request_confidential_access()`'s existing dedup check (`access_requests_service.py:219-223`, `if effective.status in ("granted", "pending"): raise 409`) currently blocks any second request once *any* grant exists for the target. This changes for stage scope: reuse `resolve_stage_grant()` directly instead of `resolve_effective_access()`'s status for the dedup check on stage-scope requests — block only when a `pending` request already exists for `(user_id, stage_id)`; a `granted` (active) lower-tier grant no longer blocks a fresh request. (`resolve_effective_access()`'s own `"granted"`/`"pending"` semantics for document/team scope are untouched — this change is scoped to the stage branch only.)

### 4.2 Approval — `_decide()` (`access_requests.py`)

For a `scope == stage` request being approved, the endpoint now requires `tier: GrantTier` and `duration: GrantDuration` in the request body (both mandatory — no default tier/duration; the approver must choose). `expires_at` is computed from `duration`: `hours_72` → `+72h`, `week_1` → `+7d`, `month_1` → `+30d` (flat, not calendar-month), `unlimited` → `None`. Document/team-scope approvals keep today's fixed-TTL behavior unchanged (`tier`/`duration` stay `NULL` for those rows) — this endpoint branches on `scope` to decide which path applies.

Because of §4.1's "latest approved wins" rule, approving a new stage request for a user who already has an older active grant on that same stage doesn't need to touch the old row at all — `resolve_stage_grant()`'s `ORDER BY requested_at DESC LIMIT 1` naturally picks the new one.

### 4.3 New endpoint — revoke

`POST /access-requests/{request_id}/revoke`. Auth: same `has_permission(..., "approve_access_request", ...)` gate as `_decide()`, checked against the request's own `team_id`. Only legal on a request whose current `status == approved` (404/409 otherwise — mirror the existing status-guard style already in `_decide()`). Sets `status = revoked`, `revoked_at = now()`, `revoked_by = identity.user_id`; `record_audit(action="REVOKE_ACCESS_GRANT", ...)`; commits. `resolve_stage_grant()` immediately stops returning this row (its `status` filter excludes anything but `approved`) — no other code needs to know revocation happened.

### 4.4 Approver visibility of requester's team + role

`pending_requests_for_reviewer()` / `_serialize()` (`access_requests.py`) add `requester_team_name: str` and `requester_role: str` to `AccessRequestOut` — resolved via the existing `_get_team_membership(db, r.user_id, r.team_id)` helper (already imported in `access_requests_service.py`; reuse it here) joined with `Team.name`. This is read-only enrichment, no new query pattern needed beyond what Task 18 already established for scope-specific copy.

## 5. Frontend changes

- **`AdminPage.jsx`** (Pending Approvals): each stage-scope pending row shows the requester's team + role (new fields from §4.4) alongside the existing scope copy from Task 18. The approve action becomes a small form: a 3-way tier selector (viewer / contributor / contributor + confidential, with the one-line descriptions from the original directive) and a 4-way duration selector (72 hours / 1 week / 1 month / no expiration), both required before the Approve button is enabled. Document/team-scope rows keep today's plain Approve/Deny (no selectors — those paths don't use tiers).
- **`AdminPage.jsx`** (granted rows): each currently-`approved` stage-scope grant gets a "Revoke" button calling the new endpoint, with a confirmation step (revocation is immediate and has no undo other than the user re-requesting).
- **`MyAccessRequestsPanel.jsx`**: displays the granted tier (plain label) and either the countdown-to-`expires_at` (existing `formatCountdown`, unchanged) or "No expiration" when `expires_at` is `null`.
- **`frontend/src/lib/api.js`**: `accessRequestsApi` gains `revoke(requestId)`; the existing `decide`-style approve call gains `tier`/`duration` in its body for stage-scope requests.

## 6. Testing / verification plan

1. Migration: apply, confirm single alembic head, confirm `grant_tier`/`grant_duration` Postgres enum types exist, confirm `access_request_status` has 4 values including `revoked`.
2. `resolve_stage_grant()`: unit tests — no grant → `None`; approved+unexpired → returns tier/team_id; approved+expired → `None`; two approved rows for the same (user, stage) → returns only the latest by `requested_at`; revoked row → `None` even though `expires_at` hasn't passed.
3. Visibility: a user with zero team relationship to a stage, granted `viewer` tier only, sees that stage's public/internal documents (via `get_accessible_stages_for_user()`) but confidential documents there remain locked stubs.
4. Upload path: same user granted `contributor` tier can create a document attributed to the grant's own routing `team_id` in that stage; a direct API call attempting to upload with a *different* `team_id` is rejected even with an active contributor grant.
5. Confidential unlock: same user granted `contributor` (not `contributor_confidential`) still cannot view a confidential document in that stage; upgrading the grant to `contributor_confidential` unlocks it.
6. Duration: approve with each of the four durations, confirm `expires_at` lands within a few seconds of the expected offset (or is `None` for `unlimited`).
7. Re-request/upgrade: user with an active `viewer` grant requests again for the same stage while no pending request exists — succeeds (no 409); requesting again while a request is already `pending` — 409 as today.
8. Revoke: approver revokes an active grant; `resolve_stage_grant()` immediately returns `None`; the revoked row's `status`/`revoked_at`/`revoked_by` are correct; audit log has the `REVOKE_ACCESS_GRANT` entry.
9. Live browser walkthrough: approver sees team+role and the tier/duration selectors on a real pending stage request; approves as `contributor`, 1 week; requester sees the upload ability and the countdown in "My requests"; approver revokes; requester's next action correctly loses access.
