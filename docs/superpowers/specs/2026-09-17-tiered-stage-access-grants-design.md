# Tiered Stage Access Grants — Architecture & Implementation Specification

## 0. Relationship to the original grants spec

This supersedes two foundational premises of `docs/superpowers/specs/2026-09-17-confidential-access-grants-design.md` (already implemented in Tasks 1-19 of `docs/superpowers/plans/2026-09-17-confidential-access-grants.md`):

1. **Every grant was always read-only confidential-doc access.** It now has three tiers: `viewer` (see/search public+internal docs in the stage), `contributor` (viewer + edit/upload in that stage), `contributor_confidential` (contributor + confidential docs in that stage).
2. **TTL was fixed by scope** (`GRANT_TTL_DAYS = 90`, `DOCUMENT_GRANT_TTL_HOURS = 72`). The approver now picks the duration per grant: 72 hours, 1 week, 1 month (30 days flat), or no expiration.

Everything this spec does NOT mention (document/team-scope requests, `resolve_effective_access()`'s document/team branches, the `DOCUMENT_AND_TEAM_SCOPE_REQUESTS_ENABLED` policy gate, `_derive_document_team`) is unchanged and stays exactly as implemented — those paths remain dormant behind the existing stage-only policy restriction. Tiers and approver-chosen duration apply **only** to stage-scope grants, since that is the only reachable request path today.

**Why not real temporary team membership** (the first mechanism considered): `UserTeamMembership` has a hard `UNIQUE(user_id, team_id, project_id)` constraint, and the only way to reach the stage-request flow (`_derive_stage_team()`) requires the requester to already hold a row on the approving team. Elevating that existing row's role would grant the bump across every stage the team already reaches — precisely the cross-stage leak the redesign exists to prevent. This spec uses a parallel, attribute-based grant instead, which is inherently stage-scoped by construction and needs no membership mutation or revert-on-expiry logic at all.

**Revision note:** this replaces the first draft of this spec after a direct pressure-test against the live `docflow-complete` code turned up one design contradiction, one live ordering/revocation bug, one missing enforcement site, and one under-specified integration surface (`AuthorizationContext`). All four are resolved below with file:line evidence; nothing here is asserted without having been checked against the actual current code.

## 1. Who can receive a stage grant, and what it actually unlocks (resolves the request-reachability contradiction)

### 1.1 Reachability — unchanged, confirmed non-contradictory

`_derive_stage_team()` (`access_requests_service.py:108-128`) still requires the requester to already belong to a team with `TeamStageAccess` to the target stage. The frontend trigger (`StageSection.jsx`'s `StageAccessLink`, rendered only when `documents.some(d => d.locked) && stageId`) only ever fires for a stage the requester's own team can already partially see. This part is unambiguous and unchanged.

### 1.2 What a grant unlocks — the DocumentTeamVisibility question

The contradiction was here: if the requester's team already has `TeamStageAccess` to the stage, `get_accessible_stages_for_user()` already lists that stage for them — so what would a `viewer` grant add?

Traced three independent call sites — `classify_document_visibility()` (`access_control.py:431-469`), `classify_documents_visibility()` (`access_control.py:472-565`), and `build_access_filter()` (`access_control.py:578-641`) — and all three gate on `DocumentTeamVisibility` (which team a *specific document* is visible to, set at upload time to the uploading team only, editable later by team_lead+/admin) **before** ever consulting sensitivity or grants. Stage-level `TeamStageAccess` and per-document `DocumentTeamVisibility` are two independent dimensions: a team can have full stage access and still only see the subset of that stage's documents its own members happened to upload.

That means the reachable, pre-existing "locked document" trigger only proves the requester's team has `DocumentTeamVisibility` for *at least one* confidential document in that stage (the one that's locked) — it says nothing about the other documents in that stage uploaded by other teams. Given your own tier definitions ("viewer: grant access to only see... the documents in the request stage with public or internal clearance level"), a `viewer` grant is meant to unlock **every** public/internal document in that stage, not just the requester's own team's uploads — otherwise `viewer`/`contributor` would have zero observable read effect beyond what native access already provides, which contradicts their definitions.

**Resolution:** a stage grant, at any tier, overrides the `DocumentTeamVisibility` check **within that one granted stage only**, capped by the tier's sensitivity ceiling (public+internal for `viewer`/`contributor`, +confidential for `contributor_confidential`). It does **not** change what `DocumentTeamVisibility` means anywhere else, for any other user, or for any other stage. This is an additive capability the tier system introduces — it does not modify the pre-existing "grants only affect sensitivity, never team visibility" behavior of the old document/team-scope grant types, which remain exactly as implemented.

## 2. Data model

### 2.1 `AccessRequestStatus` — new value

```python
class AccessRequestStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    denied = "denied"
    revoked = "revoked"   # NEW
```

`revoked` is a terminal state set by an approver on an already-`approved` request, distinct from `denied` (which means "never approved") purely for audit legibility.

### 2.2 `AccessRequest` — new columns

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
- `tier: Mapped[GrantTier | None]` — `NULL` while pending; set by the approver at decision time, never by the requester. Populated only when `scope == AccessRequestScope.stage`; stays `NULL` for document/team-scope rows.
- `duration: Mapped[GrantDuration | None]` — same population rule. Stored explicitly (rather than re-derived from `expires_at - decided_at`) so the UI can display "1 month" exactly rather than reverse-engineering it from a timestamp delta.
- `revoked_at: Mapped[datetime | None]`, `revoked_by: Mapped[UUID | None]` (FK `users.user_id`) — set together on revocation. Match the existing `decided_at`/`decided_by` column definitions verbatim (read them first).

`expires_at` becomes genuinely meaningful as `NULL` = no expiration. `is_expired()` (`access_requests_service.py:57-58`) already treats a falsy `expires_at` as not-expired — confirmed no change needed there.

### 2.3 Concurrency invariant — partial unique index

A plain "check no pending request exists, then insert" (§4.1 below) is race-prone: two simultaneous requests can both observe "no pending" and both insert. Add, in the same migration:

```sql
CREATE UNIQUE INDEX uq_access_requests_one_pending_stage_request
ON access_requests (user_id, stage_id)
WHERE scope = 'stage' AND status = 'pending';
```

This makes "at most one pending stage request per (user, stage)" an actual database invariant, not an application-timing assumption.

### 2.4 Resolver index

`resolve_stage_grant()` (§3) becomes a per-request authorization lookup — add a supporting index:

```sql
CREATE INDEX ix_access_requests_stage_grant_lookup
ON access_requests (user_id, stage_id, status, decided_at)
WHERE scope = 'stage';
```

### 2.5 Migration mechanics

New Alembic revision:
- `CREATE TYPE grant_tier AS ENUM (...)`, `CREATE TYPE grant_duration AS ENUM (...)` before `add_column` (per the Task-1 lesson already in the SDD ledger — `add_column` with `sa.Enum(...)` does not auto-create the backing Postgres type).
- `ALTER TYPE access_request_status ADD VALUE 'revoked'` — must run as its own statement, not batched into the same implicit transaction as any subsequent use of the new value (Postgres restriction on `ALTER TYPE ... ADD VALUE`). **Downgrade for this one statement is a documented no-op** — Postgres has no `DROP VALUE` for enums; removing one requires recreating the type, which is out of scope for a routine downgrade. State this explicitly in the migration's `downgrade()` docstring rather than silently omitting it.
- Add `tier`, `duration`, `revoked_at`, `revoked_by` as nullable columns; add the two indexes above. `downgrade()` drops them in reverse, then `DROP TYPE IF EXISTS grant_tier` / `grant_duration`.
- **No RLS changes needed**: `access_requests`' row-level policy (confirmed via the table's existing tenant-scoping) filters on tenant/project identity columns, not on the columns being added — plain nullable column additions don't interact with it.
- **Backfill**: confirmed directly against the live dev database (`SELECT count(*) FROM access_requests` — 0 rows, any status). No existing approved grant needs a tier/duration backfill decision in this environment. If this migration ever runs against an environment with pre-existing approved rows, `tier`/`duration` staying `NULL` on those old rows must be treated by every consumer as "an old-model grant, tier-less" — §3 below is written so that `resolve_stage_grant()` simply never returns a `NULL`-tier row as an active *stage* grant (only rows with a tier populated count), so an old untiered row is inert rather than ambiguous. This repo has none today, so nothing further is required now, but the invariant holds regardless.

## 3. The ABAC resolver

### 3.1 Canonical bulk function

```python
@dataclass
class StageGrant:
    tier: GrantTier
    team_id: UUID          # the grant's own routing team (from _derive_stage_team at request time)
    expires_at: datetime | None
    request_id: UUID

def get_active_stage_grants_for_user(db: Session, user_id: UUID) -> dict[UUID, StageGrant]:
    """
    stage_id -> StageGrant for every stage where user_id currently holds
    an active grant. The single source every consumer reads from —
    get_accessible_stages_for_user(), build_access_filter(),
    build_authorization_context(), and resolve_stage_grant() (a thin
    per-stage accessor over this). Written once so the "latest terminal
    event, revocation-aware, expiry-aware, deleted-stage-aware" reduction
    logic (below) exists in exactly one place.
    """
```

Query: all `AccessRequest` rows for `user_id` where `scope == stage`, `tier IS NOT NULL`, `status IN (approved, revoked)`, joined to `Stage` on `stage_id` with `Stage.deleted_at IS NULL` (a soft-deleted stage never has an active grant, regardless of the row's own status/expiry — the `TeamStageAccess` cleanup `delete_stage()` already does doesn't touch `AccessRequest`, so this join is the enforcement point, not a data cleanup). Group by `stage_id`; within each group, take the row with the latest `COALESCE(revoked_at, decided_at)` — **this is the fix for the revoke-then-fallback bug** (see below) — and include it in the result only if that latest-event row's `status == approved` and it is not expired.

**The bug this avoids:** an earlier draft of this resolver picked "the latest row with `status == approved`," which is wrong. Example: a `viewer` grant is approved in January, a `contributor` grant is approved in February (superseding it per §4.1), then the February grant is revoked in March. Querying "latest approved row" would find the January row (still `status == approved`, and now the *only* row matching that filter) and incorrectly reactivate it as `viewer` access. Querying "latest terminal event regardless of status, then checking whether that event was an approval" correctly finds March's revocation as the latest event and returns no grant at all — an explicitly revoked grant never falls back to an older one. **This must stay true even if the resolver is later refactored — it is a deliberate security property, not an implementation detail.**

`resolve_stage_grant(db, user_id, stage_id) -> StageGrant | None` is `get_active_stage_grants_for_user(db, user_id).get(stage_id)`.

## 4. Enforcement — every consumer

| Call site | Change |
|---|---|
| `get_accessible_stages_for_user()` (`access_control.py:191`) | Union in `get_active_stage_grants_for_user(db, user_id).keys()`. Feeds `build_access_filter`, `stages.py` (stage list), `workspace.py`, `project_intelligence.py`, `rag/retrieval.py`, both `query_tools.py` call sites, and `graph_tools.py` automatically. |
| `build_access_filter()` (`access_control.py:578-641`) | Per §1.2: the `Document.document_id.in_(visible_doc_ids_subquery)` condition gains an `or_(..., Document.stage_id.in_(grant_stage_ids))`, where `grant_stage_ids = get_active_stage_grants_for_user(db, user_id).keys()`. This bypasses the `DocumentTeamVisibility` requirement *only* for documents in a granted stage — sensitivity is still filtered downstream by `can_view_document()`, unaffected. |
| `classify_document_visibility()` (`access_control.py:431-469`, single-document) | Same override: if `visible_team_ids ∩ memberships` is empty, don't immediately return `not_visible` — first check `get_active_stage_grants_for_user(db, user_id).get(document.stage_id)`; if present, proceed to the sensitivity check using the grant tier as the effective clearance instead of falling through to `not_visible`. |
| `classify_documents_visibility()` (`access_control.py:472-565`, batch) | Same override, applied per-document using `auth_context.stage_grant_tiers` (see `AuthorizationContext` row below) in place of a fresh per-document DB call. |
| `AuthorizationContext` (`authorization_context.py`) | **Required, not optional** (this was under-specified in the first draft). Add `stage_grant_tiers: dict[uuid.UUID, GrantTier]`, populated in `build_authorization_context()` from `get_active_stage_grants_for_user(db, user_id)`. Step 3 (`accessible_stage_ids`) unions in `stage_grant_tiers.keys()` — it currently runs its own independent `TeamStageAccess` query and does **not** call `get_accessible_stages_for_user()`, so fixing that function alone does not fix this path; this dict must be threaded through explicitly. Step 4's `active_confidential_grant_stage_ids` (tier-blind today — any approved stage grant unlocks confidential) is replaced by a tier check: confidential unlock only when `stage_grant_tiers.get(document.stage_id) == GrantTier.contributor_confidential`. |
| `resolve_effective_access()` (`access_control.py:241-409`) | The stage-scope grant-query branch (`elif stage_id is not None`, around line 339) only contributes to the `active_grants` "granted" result when the matching row's `tier == contributor_confidential`. A `viewer`/`contributor`-tier stage grant must not make this resolver report "granted" for confidential purposes — it isn't one. (Document/team-scope branches are untouched; they have no tier concept and remain always-read-only as implemented.) |
| `is_grant_only_confidential_access()` (`access_control.py:125-137`) | Must become tier-aware, or it actively breaks the new tier: today it returns `True` (read-only) for *any* grant-derived confidential access. A `contributor_confidential` stage grant is explicitly write-capable — if this function is left as-is, it would wrongly block every write action for the one tier that's supposed to allow them. Fix: `EffectiveAccessResult` gains `scope: AccessRequestScope | None` and `tier: GrantTier | None` fields (set by `resolve_effective_access()`'s grant branch); `is_grant_only_confidential_access()` returns `False` when `result.scope == AccessRequestScope.stage and result.tier == GrantTier.contributor_confidential`, and its existing `True` otherwise (document/team-scope grants, or a stage grant with no tier — shouldn't occur but is treated as read-only defensively). |
| `document_persistence.py:108-132` (`_check_upload_access`) | Rewritten as an explicit OR, not a fallback-after-raise (the literal fallback pattern in the first draft doesn't work against this function's current shape, which raises immediately on either check's failure): `native_ok = has_permission(db, user_id, "upload", team_id, project_id) and has_stage_access(db, user_id, team_id, stage_id, project_id)`; `grant = resolve_stage_grant(db, user_id, stage_id)`; `grant_ok = grant is not None and grant.tier in (GrantTier.contributor, GrantTier.contributor_confidential) and grant.team_id == team_id`; raise only `if not (native_ok or grant_ok)`. Requiring `team_id == grant.team_id` means the grant holder can only upload attributed to the grant's own routing team, never an arbitrary one. |
| `document_review.py:435` (`review_message`) | Identical OR, keyed off `document.stage_id` / `document.uploaded_as_team_id` in place of the client-supplied `team_id`/`stage_id`. |
| `workflow.py:173` (`submit_for_review`) — **missing from the first draft, found by inventorying every `has_permission()` action string in the codebase** | Same bar as upload (`_ACTION_MIN_ROLE["submit"] == contributor`) — without the same OR fallback here, a contributor-grant holder could upload a document but never submit it for review, leaving it permanently stuck in `draft`. Identical OR pattern: `native_ok = has_permission(db, user_id, "submit", team_id, project_id)`; `grant_ok` via `resolve_stage_grant(db, user_id, document.stage_id)` (need the document's `stage_id` — `submit_for_review` receives `document_id`, so load it) with the same tier/`team_id` match. |
| `can_edit_document()` (`access_control.py:111-122`) | **No change.** `document.uploaded_by == user_id` already covers a grant holder editing their own upload; native contributors already can't edit others' finalized documents (team_lead+ only) — a contributor grant gets identical parity for free. Verified by reading the function: it never checks role rank below team_lead for the "not the uploader" branch, so there's no room for a grant-derived role to leak extra edit rights even if one were added — leaving it untouched is the correct, minimal fix. |
| `can_delete_version()` (`document_delete.py:76-87`) | **No change, but documented explicitly**: `is_admin or version.uploaded_by == user_id` is already identity-based, not role-based — a plain native viewer who somehow uploaded a version could already delete it today. A contributor-grant holder deleting their own non-live version is this same pre-existing rule, not a new capability the grant introduces. `viewer`-tier grants never reach this at all, since they can never satisfy `uploaded_by == user_id` (they can't upload). |
| `has_stage_access()` (`access_control.py:169-188`) | **Unchanged.** Stays the narrow "does this team have `TeamStageAccess`" check; the grant fallback lives in the call sites above, not inside it, so its other caller (`query_tools.py:435`, asking "can this team see this stage," a different question) is undisturbed. |
| `_derive_stage_team()` (`access_requests_service.py:108-128`) | **Unchanged** — see §1.1. |
| `approve`/`reject`/`manage_team_members`/`approve_access_request` (`workflow.py:263`, `document_upload_review.py:134`, `admin.py`, `access_control.py:122`) | **Unchanged.** All team_lead-only; no tier confers any of these — matches the tier definitions verbatim (none mention approval or team management). |

### 4.1 Capability matrix (explicit, per your request)

| Capability | viewer | contributor | contributor_confidential |
|---|---|---|---|
| See public/internal docs in the granted stage (via §1.2 override) | ✓ | ✓ | ✓ |
| See confidential docs in the granted stage | — | — | ✓ |
| Create document in the granted stage | — | ✓ (attributed to the grant's routing team) | ✓ |
| Edit own uploaded content | — | ✓ (via existing `uploaded_by` rule, no new code) | ✓ |
| Edit others' existing content | — | — (matches native contributor — team_lead+ only) | — |
| Upload new version of own document | — | ✓ | ✓ |
| Submit own document for review | — | ✓ | ✓ |
| Delete own non-live version | — (can never upload) | ✓ (via existing identity-based rule, no new code) | ✓ |
| Approve/reject workflow | — | — | — |
| Manage team members / approve access requests | — | — | — |

## 5. Request/approval lifecycle changes

### 5.1 Re-requesting for a higher tier

`request_confidential_access()`'s existing dedup check (`access_requests_service.py:219-223`) currently blocks any second request once *any* grant exists for the target. For stage scope specifically: use `resolve_stage_grant()` for the dedup check instead of `resolve_effective_access()`'s status — block only when a `pending` request already exists for `(user_id, stage_id)` (enforced at the DB level too, via §2.3's partial unique index). An active lower-tier grant no longer blocks requesting a higher one. Document/team-scope dedup semantics are untouched.

### 5.2 Approval — `_decide()` (`access_requests.py`)

For `scope == stage`, the endpoint requires `tier: GrantTier` and `duration: GrantDuration` in the body (both mandatory). `expires_at` computed from `duration`: `hours_72` → `+72h`, `week_1` → `+7d`, `month_1` → `+30d` flat, `unlimited` → `None`. Document/team-scope approvals keep today's fixed-TTL behavior (`tier`/`duration` stay `NULL`).

Per §3's "latest terminal event" resolution, approving a new stage request when an older grant is already active needs no special handling — the new row's `decided_at` is simply the latest, so `get_active_stage_grants_for_user()` picks it up automatically. The older row is left as-is (still `status == approved` in the database, just no longer the one being consulted) — this is intentional per §3's bug fix, not an oversight: it must stay inert even after a later grant covering the same stage is revoked (§3's "no reactivation" property).

### 5.3 New endpoint — revoke

`POST /access-requests/{request_id}/revoke`. Auth: same `has_permission(..., "approve_access_request", ...)` gate as `_decide()`, checked against the request's own `team_id`. Loads the row with `SELECT ... FOR UPDATE` (`db.execute(select(AccessRequest).where(...).with_for_update())`) before checking `status == approved`, so two concurrent revoke calls can't both pass the check and double-write the audit trail. 404/409 if not currently `approved`. Sets `status = revoked`, `revoked_at = now()`, `revoked_by = identity.user_id`; `record_audit(action="REVOKE_ACCESS_GRANT", ...)`; commits.

### 5.4 Approver visibility of requester's team + role

`pending_requests_for_reviewer()` / `_serialize()` (`access_requests.py`) add `requester_team_name: str` and `requester_role: str` to `AccessRequestOut`, resolved via the existing `_get_team_membership()` helper joined with `Team.name`.

## 6. Frontend changes

- **`AdminPage.jsx`** (pending rows): show requester's team + role (§5.4); the approve action becomes a form requiring a 3-way tier selector and a 4-way duration selector before Approve is enabled. Document/team-scope rows keep today's plain Approve/Deny.
- **`AdminPage.jsx`** (granted rows): a "Revoke" button on each active stage grant, with a confirmation step.
- **`MyAccessRequestsPanel.jsx`**: displays the granted tier and either the countdown to `expires_at` or "No expiration."
- **`frontend/src/lib/api.js`**: `accessRequestsApi` gains `revoke(requestId)`; the approve call gains `tier`/`duration` in its body for stage-scope requests.

## 7. Testing / verification plan

1. Migration: apply, confirm single alembic head, confirm `grant_tier`/`grant_duration` enum types exist, confirm `access_request_status` has 4 values, confirm both new indexes exist.
2. `get_active_stage_grants_for_user()` / `resolve_stage_grant()`: no grant → empty/`None`; approved+unexpired → returned; approved+expired → not returned; two approved rows for the same (user, stage) → only the one with the latest `decided_at` (not `requested_at`) is returned; **the revoke-then-fallback case** — approve grant A, approve grant B (superseding A), revoke B, confirm the result is `None`, not a reactivated A; a grant on a soft-deleted stage → not returned.
3. Concurrency: two simultaneous requests for the same (user, stage) with no existing pending request — confirm exactly one succeeds and the other gets a clean 409/integrity error, not two pending rows.
4. Visibility override (§1.2): a user whose team has `TeamStageAccess` to a stage but no `DocumentTeamVisibility` for most of its documents, granted `viewer` tier, now sees *every* public/internal document in that stage (not just their own team's uploads) via `classify_document_visibility`, `classify_documents_visibility` (batch/RAG path), and `build_access_filter` (list) — all three agree. Confidential documents in that stage still render as locked stubs for this user.
5. Upload/submit path: same user granted `contributor` creates a document attributed to the grant's routing `team_id`, then submits it for review — both succeed; a direct API call with a *different* `team_id` is rejected even with an active contributor grant.
6. Confidential unlock and write-capability interaction: a user granted `contributor_confidential` can both view a confidential document in that stage AND submit/edit their own upload of one — confirming `is_grant_only_confidential_access()` does not wrongly block writes for this specific tier (this was a live bug in the first draft of this spec).
7. Duration: approve with each of the four durations, confirm `expires_at` lands within a few seconds of the expected offset, or is `None` for `unlimited`.
8. Re-request/upgrade: user with an active `viewer` grant requests again for the same stage with no pending request outstanding — succeeds; requesting again while already pending — 409.
9. Revoke: approver revokes an active grant under `FOR UPDATE`; confirm `get_active_stage_grants_for_user()` immediately excludes it; confirm `status`/`revoked_at`/`revoked_by`; confirm the audit log entry.
10. AuthorizationContext / RAG parity: grant a `viewer`-tier stage grant, confirm a direct document read, the document list endpoint, and a RAG retrieval/search call all agree on what's now visible (this specifically exercises the `AuthorizationContext.stage_grant_tiers` wiring, not just `get_accessible_stages_for_user()`).
11. Live browser walkthrough: approver sees team+role and the tier/duration selectors on a real pending stage request; approves as `contributor`, 1 week; requester sees upload+submit ability and the countdown in "My requests"; approver revokes; requester's next write attempt correctly loses access, and confirms via a second browser/session that RAG search for that user no longer surfaces the stage's documents either.
