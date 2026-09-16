# Confidential Access Grants Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a viewer/contributor request read-only, time-boxed access to a specific confidential document, an entire stage's confidential documents, or a whole team's confidential documents — approved by that team's lead — without ever gaining edit/upload/submit/approve/reject/delete capability, and without the frontend ever having to re-derive grant logic itself.

**Architecture:** A single new resolver, `resolve_effective_access()`, becomes the one place that answers "how does this user currently have access to this target" (native role vs. grant vs. pending/denied/expired request). Both the mutation-enforcement helper (`is_grant_only_confidential_access()`) and the new status API consume it — neither re-derives the precedence rules. `AccessRequest` gains a `scope` (document/stage/team) plus two nullable target FKs; `team_id` stays mandatory on every row as approval-routing metadata, derived server-side, never client-supplied for document/stage scope.

**Tech Stack:** FastAPI + SQLAlchemy + Alembic (Postgres) backend; React frontend; `unittest.TestCase` against a real Postgres instance (no mocking — this codebase's established convention, confirmed in `tests/test_audit_rules_expanded.py`).

**Spec:** `docs/superpowers/specs/2026-09-17-confidential-access-grants-design.md` — this plan implements Sections 1–14 of that spec. Read it alongside this plan; task descriptions below reference its section numbers rather than repeating their rationale.

## Global Constraints

- Document-scope grants expire after `DOCUMENT_GRANT_TTL_HOURS = 72` (hours); stage/team-scope grants keep the existing `GRANT_TTL_DAYS = 90` (spec §1.3).
- `team_id` is required on every `AccessRequest` row regardless of scope; for document/stage scope it is derived server-side and never accepted from the client (spec §1.1, §13.A).
- New FKs `document_id`/`stage_id` on `AccessRequest` use `ON DELETE CASCADE`; the existing `team_id` FK is untouched (spec §1.2).
- `has_permission()` is never modified and never becomes grant-aware (spec §5). Every enforcement check is `if not has_permission(...): raise ...` followed by a **separate**, explicit `is_grant_only_confidential_access(...)` check.
- `resolve_effective_access()` is the only place native-vs-grant-vs-request precedence is decided (spec §4.0). No other function re-implements that precedence.
- Deterministic multi-grant selection (spec §4.0.2, deliberately deferred to this plan): when a target is covered by more than one active grant, `resolve_effective_access()` checks **document-scope, then stage-scope, then team-scope, in that order**, and returns the first match's `expires_at`/`request_id` — narrowest scope wins. This is decided and documented here, not left ambiguous.
- `delete_stage()` (spec §7, deliberately deferred): **no enforcement code is added.** `app/routers/stages.py`'s `_require_project_admin()` gate means only org_admin/project_admin can ever call this endpoint, and `is_grant_only_confidential_access()` always returns `False` for both (native access, unconditional first branch) — so the actor can never be grant-only. Task 9 adds a one-line comment recording this reasoning; no behavior changes.
- Every new backend test runs against a real Postgres session (`SessionLocal()`), following `tests/test_audit_rules_expanded.py`'s exact `setUp`/`tearDown` convention (tenant looked up on a short-lived session first, then `self.db.info["tenant_id"]` set before the real session's first query, per `app/database.py`'s `after_begin` listener).

---

## File Structure

**Backend — new files:**
- `app/models/team.py` — modified (not new): add `AccessRequestScope` enum and three new `AccessRequest` columns.
- `alembic/versions/<new>_confidential_access_grant_scopes.py` — new migration.
- `alembic/versions/<new>_notification_audience.py` — new migration (adds `Notification.audience`).
- `tests/test_access_control_resolver.py` — new: `resolve_effective_access()` + `is_grant_only_confidential_access()` unit tests.
- `tests/test_access_requests_scoped.py` — new: `request_confidential_access()` multi-scope tests.
- `tests/test_grant_enforcement.py` — new: read-only enforcement tests across every mutation path from spec §6.

**Backend — modified files:**
- `app/models/notification.py` — add `NotificationAudience` enum + `Notification.audience` column.
- `app/services/access_control.py` — add `EffectiveAccessResult`, `resolve_effective_access()`; rewrite `is_grant_only_confidential_access()` and the confidential branch of `classify_document_visibility()`.
- `app/services/authorization_context.py` — add `active_confidential_grant_document_ids`/`active_confidential_grant_stage_ids` to `AuthorizationContext`; extend the bulk grant query.
- `app/services/access_requests_service.py` — multi-scope `request_confidential_access()`, stage team-derivation, retire `live_request()` in favor of `resolve_effective_access()`.
- `app/routers/access_requests.py` — `CreateAccessRequest` gains optional `document_id`/`stage_id`; new `GET /access-requests/status`; enrich `/pending` and `/mine` output.
- `app/services/document_finalize.py` — new `GrantOnlyAccessError`; enforcement in `finalize_document_revision()`.
- `app/services/document_delete.py` — enforcement in `delete_document()`/`delete_version()` (reuses existing `PermissionDeniedError`).
- `app/services/workflow.py` — enforcement in `submit_for_review()`, `approve_document()`, `reject_document()`, `reset_to_draft_if_approved()` (reuses existing `WorkflowPermissionError`).
- `app/routers/document_review.py` — catch `GrantOnlyAccessError` in `review_message`/`version_review_message`; catch `WorkflowPermissionError` in `version_review_message`.
- `app/routers/stages.py` — one-line comment on `delete_stage()`, no logic change.
- `app/routers/documents.py` — `list_documents` redaction (spec §13.D).
- `app/services/notifications.py` — `audience` param on `notify_access_request_created`/`notify_access_request_decided`.

**Frontend — new files:**
- `frontend/src/components/access/MyAccessRequestsPanel.jsx` — new: the "My Access Requests" surface (spec §13.B).

**Frontend — modified files:**
- `frontend/src/lib/api.js` — `mapDocs` gains `locked`; `accessRequestsApi` gains `createForDocument`/`createForStage`/`status`.
- `frontend/src/components/sources/DocumentItem.jsx` — locked-row rendering.
- `frontend/src/components/sources/SourcePanel.jsx` — remove the static banner; document/stage trigger wiring.
- `frontend/src/pages/ProjectWorkspace.jsx` — team-scope modal reframing; stage-scope trigger handler; "My Access Requests" panel mount.
- `frontend/src/pages/AdminPage.jsx` — scope-aware Pending Approvals row copy + duration display.
- `frontend/src/components/layout/TopNav.jsx` — notification click-through routing.

---

### Task 1: `AccessRequestScope` model + migration

**Files:**
- Modify: `app/models/team.py:87-127`
- Create: `alembic/versions/<rev>_confidential_access_grant_scopes.py`
- Test: `tests/test_access_control_resolver.py` (created in Task 3; this task only needs the migration to apply cleanly — verified via `alembic upgrade head`)

**Interfaces:**
- Produces: `AccessRequestScope` enum (`document`/`stage`/`team`), `AccessRequest.scope`, `AccessRequest.document_id`, `AccessRequest.stage_id` — consumed by every later task.

- [ ] **Step 1: Add the scope enum and new columns to the model**

Edit `app/models/team.py`. Add the enum right after `AccessRequestStatus` (after line 90), and add three fields to `AccessRequest`:

```python
class AccessRequestScope(str, enum.Enum):
    document = "document"
    stage = "stage"
    team = "team"
```

Then in `AccessRequest` (after the existing `team_id` column, before `status`):

```python
    scope: Mapped[AccessRequestScope] = mapped_column(
        Enum(AccessRequestScope, name="access_request_scope"),
        default=AccessRequestScope.team,
        nullable=False,
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.document_id", ondelete="CASCADE"), nullable=True
    )
    stage_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stages.stage_id", ondelete="CASCADE"), nullable=True
    )
```

Update the class docstring to mention the three scopes (it currently says "elevated access on a specific team" only).

- [ ] **Step 2: Write the migration**

Find the current head first:

Run: `cd D:\Ra\DocFlowAI\meem_salvage && python -m alembic heads`
Expected: `b8c9d0e1f2a3 (head)`

Create `alembic/versions/<new_rev>_confidential_access_grant_scopes.py` (generate a real revision id the same way existing migrations do — 12 lowercase hex characters):

```python
"""add confidential access grant scopes

Revision ID: <new_rev>
Revises: b8c9d0e1f2a3
Create Date: 2026-09-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "<new_rev>"
down_revision: Union[str, None] = "b8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "access_requests",
        sa.Column(
            "scope",
            sa.Enum("document", "stage", "team", name="access_request_scope"),
            nullable=False,
            server_default="team",
        ),
    )
    op.add_column("access_requests", sa.Column("document_id", sa.UUID(), nullable=True))
    op.add_column("access_requests", sa.Column("stage_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_access_requests_document_id", "access_requests", "documents",
        ["document_id"], ["document_id"], ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_access_requests_stage_id", "access_requests", "stages",
        ["stage_id"], ["stage_id"], ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_access_request_scope_target",
        "access_requests",
        "(scope = 'document' AND document_id IS NOT NULL AND stage_id IS NULL) OR "
        "(scope = 'stage' AND stage_id IS NOT NULL AND document_id IS NULL) OR "
        "(scope = 'team' AND document_id IS NULL AND stage_id IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_access_request_scope_target", "access_requests", type_="check")
    op.drop_constraint("fk_access_requests_stage_id", "access_requests", type_="foreignkey")
    op.drop_constraint("fk_access_requests_document_id", "access_requests", type_="foreignkey")
    op.drop_column("access_requests", "stage_id")
    op.drop_column("access_requests", "document_id")
    op.drop_column("access_requests", "scope")
    op.execute("DROP TYPE IF EXISTS access_request_scope")
```

- [ ] **Step 3: Apply and verify**

Run: `cd D:\Ra\DocFlowAI\meem_salvage && python -m alembic upgrade head`
Expected: migration applies with no error; `python -m alembic heads` now shows the new revision.

Run: `python -c "import app.main"` (using the project's Python 3.12 interpreter, per this repo's established convention — not the system default)
Expected: no import error (confirms the model change is syntactically and structurally sound).

- [ ] **Step 4: Commit**

```bash
git add app/models/team.py alembic/versions/<new_rev>_confidential_access_grant_scopes.py
git commit -m "feat(access): add document/stage/team scope to AccessRequest"
```

---

### Task 2: `Notification.audience` field + migration

**Files:**
- Modify: `app/models/notification.py`
- Create: `alembic/versions/<rev>_notification_audience.py`

**Interfaces:**
- Produces: `NotificationAudience` enum (`approver`/`requester`), `Notification.audience` — consumed by Task 13 (notification wiring) and Task 20 (frontend click-through).

- [ ] **Step 1: Add the enum and column**

Edit `app/models/notification.py`. Add after `NotificationType` (after line 27):

```python
class NotificationAudience(str, enum.Enum):
    """
    Which side of an event's lifecycle this notification is for — drives
    frontend click-through routing deterministically, never inferred from
    title text. Nullable: only access-request notifications set this today;
    other notification types have no two-sided lifecycle to distinguish.
    """
    approver = "approver"
    requester = "requester"
```

Add to `Notification` (after `notification_type`, before `title`):

```python
    audience: Mapped[NotificationAudience | None] = mapped_column(
        Enum(NotificationAudience, name="notification_audience"), nullable=True
    )
```

- [ ] **Step 2: Write the migration**

Run: `python -m alembic heads` to get the current head (the one from Task 1).

Create `alembic/versions/<new_rev>_notification_audience.py` with `down_revision` set to Task 1's revision id:

```python
"""add notification audience

Revision ID: <new_rev>
Revises: <task_1_rev>
Create Date: 2026-09-17 00:00:01.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "<new_rev>"
down_revision: Union[str, None] = "<task_1_rev>"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "notifications",
        sa.Column(
            "audience",
            sa.Enum("approver", "requester", name="notification_audience"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("notifications", "audience")
    op.execute("DROP TYPE IF EXISTS notification_audience")
```

- [ ] **Step 3: Apply and verify**

Run: `python -m alembic upgrade head`
Expected: applies cleanly; `python -m alembic heads` shows this new revision as the sole head.

- [ ] **Step 4: Commit**

```bash
git add app/models/notification.py alembic/versions/<new_rev>_notification_audience.py
git commit -m "feat(notifications): add audience field for deterministic click-through routing"
```

---

### Task 3: `resolve_effective_access()` — the shared resolver

**Files:**
- Modify: `app/services/access_control.py`
- Test: `tests/test_access_control_resolver.py` (new)

**Interfaces:**
- Consumes: `AccessRequest`, `AccessRequestScope`, `AccessRequestStatus` (Task 1); `_is_org_admin`, `_is_project_admin`, `_get_team_membership`, `_TEAM_ROLE_RANK`, `TeamRole` (existing, `app/services/access_control.py`).
- Produces: `EffectiveAccessResult` dataclass, `resolve_effective_access(db, user_id, *, document_id=None, stage_id=None, team_id=None) -> EffectiveAccessResult` — consumed by Task 4 (`is_grant_only_confidential_access`), Task 6 (`request_confidential_access` dedup), Task 11 (`GET /access-requests/status`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_access_control_resolver.py`:

```python
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models.document import Document, DocumentTeamVisibility, SensitivityLevel
from app.models.project import Project
from app.models.stage import Stage, TeamStageAccess
from app.models.team import AccessRequest, AccessRequestScope, AccessRequestStatus, Team, TeamRole, UserTeamMembership
from app.models.tenant import Tenant
from app.models.user import User
from app.services.access_control import resolve_effective_access


class TestResolveEffectiveAccess(unittest.TestCase):
    def setUp(self):
        with SessionLocal() as lookup_db:
            self.tenant = lookup_db.query(Tenant).first()
            self.assertIsNotNone(self.tenant, "Tenant required")
            tenant_id = self.tenant.tenant_id

        self.db = SessionLocal()
        self.db.info["tenant_id"] = str(tenant_id)
        self.tenant = self.db.get(Tenant, tenant_id)

        self.project = Project(tenant_id=tenant_id, name=f"Resolver Test {uuid.uuid4().hex[:8]}")
        self.db.add(self.project)
        self.db.commit()

        self.team = Team(project_id=self.project.project_id, name="Resolver Team")
        self.db.add(self.team)
        self.db.commit()

        self.stage = Stage(project_id=self.project.project_id, name="Resolver Stage", order_index=1)
        self.db.add(self.stage)
        self.db.commit()

        self.viewer = User(email=f"viewer-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.team_lead = User(email=f"lead-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.db.add_all([self.viewer, self.team_lead])
        self.db.commit()

        self.db.add(UserTeamMembership(user_id=self.viewer.user_id, team_id=self.team.team_id, project_id=self.project.project_id, role=TeamRole.viewer))
        self.db.add(UserTeamMembership(user_id=self.team_lead.user_id, team_id=self.team.team_id, project_id=self.project.project_id, role=TeamRole.team_lead))
        self.db.commit()

        self.document = Document(
            tenant_id=tenant_id, project_id=self.project.project_id, stage_id=self.stage.stage_id,
            uploaded_by=self.team_lead.user_id, uploaded_as_team_id=self.team.team_id,
            sensitivity_level=SensitivityLevel.confidential,
            original_filename="secret.md", mime_type="text/markdown",
        )
        self.db.add(self.document)
        self.db.commit()
        self.db.add(DocumentTeamVisibility(document_id=self.document.document_id, team_id=self.team.team_id))
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        self.db.query(AccessRequest).filter(AccessRequest.team_id == self.team.team_id).delete()
        self.db.query(DocumentTeamVisibility).filter(DocumentTeamVisibility.document_id == self.document.document_id).delete()
        self.db.query(Document).filter(Document.document_id == self.document.document_id).update({"current_version_id": None})
        self.db.query(Document).filter(Document.document_id == self.document.document_id).delete()
        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id == self.project.project_id).delete()
        self.db.query(User).filter(User.user_id.in_([self.viewer.user_id, self.team_lead.user_id])).delete(synchronize_session=False)
        self.db.query(Stage).filter(Stage.stage_id == self.stage.stage_id).delete()
        self.db.query(Team).filter(Team.team_id == self.team.team_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def test_native_team_lead_is_granted_never_via_grant(self):
        result = resolve_effective_access(self.db, self.team_lead.user_id, document_id=self.document.document_id)
        self.assertEqual(result.status, "granted")
        self.assertFalse(result.via_grant)
        self.assertIsNone(result.expires_at)

    def test_viewer_with_no_grant_or_request_is_none(self):
        result = resolve_effective_access(self.db, self.viewer.user_id, document_id=self.document.document_id)
        self.assertEqual(result.status, "none")

    def test_viewer_with_pending_request_is_pending(self):
        req = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.pending,
        )
        self.db.add(req)
        self.db.commit()
        result = resolve_effective_access(self.db, self.viewer.user_id, document_id=self.document.document_id)
        self.assertEqual(result.status, "pending")
        self.assertIsNone(result.expires_at)

    def test_viewer_with_active_document_grant_is_granted_via_grant(self):
        req = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
        )
        self.db.add(req)
        self.db.commit()
        result = resolve_effective_access(self.db, self.viewer.user_id, document_id=self.document.document_id)
        self.assertEqual(result.status, "granted")
        self.assertTrue(result.via_grant)
        self.assertIsNotNone(result.expires_at)

    def test_stale_denied_request_does_not_suppress_active_stage_grant(self):
        denied = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.denied, decided_at=datetime.now(timezone.utc),
        )
        stage_grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        )
        self.db.add_all([denied, stage_grant])
        self.db.commit()
        result = resolve_effective_access(self.db, self.viewer.user_id, document_id=self.document.document_id)
        self.assertEqual(result.status, "granted")
        self.assertTrue(result.via_grant)

    def test_document_grant_takes_precedence_over_overlapping_stage_grant(self):
        stage_grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        )
        doc_grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
        )
        self.db.add_all([stage_grant, doc_grant])
        self.db.commit()
        result = resolve_effective_access(self.db, self.viewer.user_id, document_id=self.document.document_id)
        self.assertEqual(result.status, "granted")
        self.assertEqual(result.request_id, doc_grant.request_id)

    def test_expired_grant_falls_through_to_none(self):
        expired = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        self.db.add(expired)
        self.db.commit()
        result = resolve_effective_access(self.db, self.viewer.user_id, document_id=self.document.document_id)
        self.assertEqual(result.status, "none")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd D:\Ra\DocFlowAI\meem_salvage && python -m pytest tests/test_access_control_resolver.py -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_effective_access'`.

- [ ] **Step 3: Implement `EffectiveAccessResult` and `resolve_effective_access()`**

Edit `app/services/access_control.py`. Add near the top, after the imports (after line 37), a dataclass import and the result type:

```python
from dataclasses import dataclass
```

Add after `_has_active_confidential_grant` (after line 212, before `class DocumentVisibility`):

```python
@dataclass
class EffectiveAccessResult:
    """
    The resolved answer to "how, if at all, does this user currently have
    access to this specific target" — the single shared verdict consumed by
    both is_grant_only_confidential_access() (mutation enforcement) and
    GET /access-requests/status (UI state). Neither re-derives this
    precedence independently.
    """
    status: str  # "granted" | "pending" | "denied" | "expired" | "none"
    via_grant: bool = False  # only meaningful when status == "granted"
    expires_at: datetime | None = None
    request_id: UUID | None = None


def resolve_effective_access(
    db: Session,
    user_id: UUID,
    *,
    document_id: UUID | None = None,
    stage_id: UUID | None = None,
    team_id: UUID | None = None,
) -> EffectiveAccessResult:
    """
    Exactly one of document_id/stage_id/team_id is provided — the caller is
    asking about one specific target. Resolution order (deliberate, and the
    only place this precedence is decided):

      1. Native role access (org admin / project admin / team-lead+ on an
         applicable team) -> granted, via_grant=False. Checked FIRST so a
         user with both native access and an active grant is never treated
         as grant-only (the grant does not downgrade an existing role).
      2. An active (approved, unexpired) grant covering this target ->
         granted, via_grant=True. Checked document-scope, then stage-scope,
         then team-scope — narrowest scope wins when more than one grant
         could apply (e.g. a document covered by both a document grant and
         an overlapping stage grant).
      3. A live pending request for this exact target -> pending.
      4. The latest denied/expired request for this exact target ->
         denied / expired accordingly.
      5. Nothing found -> none.

    A stale denied/expired request never suppresses a currently-active grant
    or a currently-pending request — steps 1-2 are checked before request
    history is ever consulted.
    """
    if sum(x is not None for x in (document_id, stage_id, team_id)) != 1:
        raise ValueError("Exactly one of document_id, stage_id, team_id must be given")

    if _is_org_admin(db, user_id):
        return EffectiveAccessResult(status="granted", via_grant=False)

    # Resolve the project_id needed for the project_admin check, and the set
    # of teams this target is "native-visible" through (team-lead+ on any of
    # these means native access), per target kind.
    if document_id is not None:
        document = db.get(Document, document_id)
        if document is None:
            return EffectiveAccessResult(status="none")
        if _is_project_admin(db, user_id, document.project_id):
            return EffectiveAccessResult(status="granted", via_grant=False)
        native_team_ids = {
            row.team_id for row in db.execute(
                select(DocumentTeamVisibility).where(DocumentTeamVisibility.document_id == document_id)
            ).scalars()
        }
    elif stage_id is not None:
        stage = db.get(Stage, stage_id)
        if stage is None:
            return EffectiveAccessResult(status="none")
        if _is_project_admin(db, user_id, stage.project_id):
            return EffectiveAccessResult(status="granted", via_grant=False)
        native_team_ids = {
            row.team_id for row in db.execute(
                select(TeamStageAccess).where(TeamStageAccess.stage_id == stage_id)
            ).scalars()
        }
    else:
        team = db.get(Team, team_id)
        if team is None:
            return EffectiveAccessResult(status="none")
        if _is_project_admin(db, user_id, team.project_id):
            return EffectiveAccessResult(status="granted", via_grant=False)
        native_team_ids = {team_id}

    memberships = [
        m for m in (_get_team_membership(db, user_id, tid) for tid in native_team_ids) if m is not None
    ]
    if any(_TEAM_ROLE_RANK[m.role] >= _TEAM_ROLE_RANK[TeamRole.team_lead] for m in memberships):
        return EffectiveAccessResult(status="granted", via_grant=False)

    now = datetime.now(timezone.utc)

    # A target can be covered by a grant of ANY of the three scopes — a
    # document by its own document_id, its stage_id, or any of its native
    # team_ids; a stage by its own stage_id or any of its native team_ids;
    # a team only by its own team_id. Fetch every row that could possibly
    # cover this target in one query rather than one round trip per scope.
    if document_id is not None:
        candidate_team_ids = list(native_team_ids) or [uuid.UUID(int=0)]  # never-matching sentinel if empty
        rows = db.execute(
            select(AccessRequest).where(
                AccessRequest.user_id == user_id,
                (AccessRequest.document_id == document_id)
                | (AccessRequest.stage_id == document.stage_id)
                | (AccessRequest.team_id.in_(candidate_team_ids)),
            )
        ).scalars().all()
    elif stage_id is not None:
        candidate_team_ids = list(native_team_ids) or [uuid.UUID(int=0)]
        rows = db.execute(
            select(AccessRequest).where(
                AccessRequest.user_id == user_id,
                (AccessRequest.stage_id == stage_id) | (AccessRequest.team_id.in_(candidate_team_ids)),
            )
        ).scalars().all()
    else:
        rows = db.execute(
            select(AccessRequest).where(
                AccessRequest.user_id == user_id, AccessRequest.team_id == team_id,
                AccessRequest.scope == AccessRequestScope.team,
            )
        ).scalars().all()

    def _is_active_grant(r: AccessRequest) -> bool:
        if r.status != AccessRequestStatus.approved:
            return False
        if r.expires_at is None:
            return True
        exp = r.expires_at if r.expires_at.tzinfo else r.expires_at.replace(tzinfo=timezone.utc)
        return exp >= now

    active_grants = [r for r in rows if _is_active_grant(r)]
    if active_grants:
        # Narrowest-scope-wins tie-break (spec §4.0.2): document, then
        # stage, then team. Only relevant when resolving a document target
        # that could be covered by an overlapping stage/team grant too —
        # a stage or team target query only ever matches its own scope.
        by_scope = {AccessRequestScope.document: [], AccessRequestScope.stage: [], AccessRequestScope.team: []}
        for r in active_grants:
            by_scope[r.scope].append(r)
        for scope in (AccessRequestScope.document, AccessRequestScope.stage, AccessRequestScope.team):
            if by_scope[scope]:
                winner = by_scope[scope][0]
                return EffectiveAccessResult(
                    status="granted", via_grant=True,
                    expires_at=winner.expires_at, request_id=winner.request_id,
                )

    pending = [r for r in rows if r.status == AccessRequestStatus.pending]
    if pending:
        latest_pending = max(pending, key=lambda r: r.requested_at)
        return EffectiveAccessResult(status="pending", request_id=latest_pending.request_id)

    terminal = [r for r in rows if r.status in (AccessRequestStatus.denied,) or (r.status == AccessRequestStatus.approved and not _is_active_grant(r))]
    if terminal:
        latest = max(terminal, key=lambda r: (r.decided_at or r.requested_at))
        status = "denied" if latest.status == AccessRequestStatus.denied else "expired"
        return EffectiveAccessResult(status=status, request_id=latest.request_id)

    return EffectiveAccessResult(status="none")
```

Add `import uuid` to `access_control.py`'s existing import block (it currently only has `from uuid import UUID`, not the bare `uuid` module needed for the `uuid.UUID(int=0)` sentinel above).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_access_control_resolver.py -v`
Expected: all 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/access_control.py tests/test_access_control_resolver.py
git commit -m "feat(access): add resolve_effective_access, the shared target-resolution function"
```

---

### Task 4: `is_grant_only_confidential_access()` + `classify_document_visibility()` simplification

**Files:**
- Modify: `app/services/access_control.py:101-372` (approx — `can_edit_document` through `can_view_document`)
- Test: `tests/test_access_control_resolver.py` (extend)

**Interfaces:**
- Consumes: `resolve_effective_access()` (Task 3).
- Produces: `is_grant_only_confidential_access(db, user_id, document) -> bool` — consumed by every enforcement task (7–9).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_access_control_resolver.py`:

```python
from app.services.access_control import is_grant_only_confidential_access, classify_document_visibility, DocumentVisibility

class TestIsGrantOnlyConfidentialAccess(TestResolveEffectiveAccess):
    def test_internal_document_is_never_grant_only(self):
        self.document.sensitivity_level = SensitivityLevel.internal
        self.db.commit()
        self.assertFalse(is_grant_only_confidential_access(self.db, self.viewer.user_id, self.document))

    def test_team_lead_is_never_grant_only_even_with_a_grant(self):
        grant = AccessRequest(
            user_id=self.team_lead.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
        )
        self.db.add(grant)
        self.db.commit()
        self.assertFalse(is_grant_only_confidential_access(self.db, self.team_lead.user_id, self.document))

    def test_viewer_with_active_grant_is_grant_only(self):
        grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
        )
        self.db.add(grant)
        self.db.commit()
        self.assertTrue(is_grant_only_confidential_access(self.db, self.viewer.user_id, self.document))

    def test_viewer_with_no_grant_is_not_grant_only(self):
        self.assertFalse(is_grant_only_confidential_access(self.db, self.viewer.user_id, self.document))

    def test_classify_document_visibility_still_blocks_ungranted_viewer(self):
        self.assertEqual(
            classify_document_visibility(self.db, self.viewer.user_id, self.document),
            DocumentVisibility.blocked_by_sensitivity,
        )

    def test_classify_document_visibility_allows_granted_viewer(self):
        grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
        )
        self.db.add(grant)
        self.db.commit()
        self.assertEqual(
            classify_document_visibility(self.db, self.viewer.user_id, self.document),
            DocumentVisibility.fully_allowed,
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_access_control_resolver.py -v`
Expected: FAIL — `ImportError: cannot import name 'is_grant_only_confidential_access'`.

- [ ] **Step 3: Implement**

Replace `is_grant_only_confidential_access` — it does not exist yet as real code (it was only spec'd); add it in `app/services/access_control.py` right after `can_edit_document` (after line 112):

```python
def is_grant_only_confidential_access(db: Session, user_id: UUID, document) -> bool:
    """
    True iff `document` is confidential-tier AND user_id's access to it is
    coming from an approved confidential-access grant rather than native
    role-based access — i.e. this document must be READ-ONLY for this user
    regardless of their normal team role. Thin wrapper over
    resolve_effective_access() (the single shared resolver) — never
    re-derives native-vs-grant precedence independently.
    """
    if document.sensitivity_level != SensitivityLevel.confidential:
        return False
    result = resolve_effective_access(db, user_id, document_id=document.document_id)
    return result.status == "granted" and result.via_grant
```

Then replace the confidential-tier branch of `classify_document_visibility` (lines 263-270) — remove the now-redundant team_lead+ check (folded into the resolver) and the direct `_has_active_confidential_grant` call:

```python
    # confidential tier — delegate to the single shared resolver rather than
    # re-deriving native-vs-grant precedence here.
    result = resolve_effective_access(db, user_id, document_id=document.document_id)
    if result.status == "granted":
        return DocumentVisibility.fully_allowed
    return DocumentVisibility.blocked_by_sensitivity
```

`_has_active_confidential_grant` (lines 201-212) is now unused by `classify_document_visibility` but is still called from `authorization_context.py`'s docstring context — check: it is not actually called anywhere else (confirm via grep). If unused, delete it; if still referenced, leave it and note why in a comment.

Run: `grep -rn "_has_active_confidential_grant" app/ --include="*.py"` to confirm before deleting.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_access_control_resolver.py -v`
Expected: all tests (13 total across both test classes) PASS.

- [ ] **Step 5: Run the full existing access-control test suite to confirm no regression**

Run: `python -m pytest tests/ -k "access_control or audit_rules" -v`
Expected: all PASS — `classify_document_visibility`'s external behavior for team-scope grants must be unchanged for any existing test that exercises it.

- [ ] **Step 6: Commit**

```bash
git add app/services/access_control.py tests/test_access_control_resolver.py
git commit -m "feat(access): implement is_grant_only_confidential_access as a thin resolver consumer"
```

---

### Task 5: Extend `AuthorizationContext` for the batch (RAG/search) visibility path

**Files:**
- Modify: `app/services/authorization_context.py`
- Modify: `app/services/access_control.py:342-357` (`classify_documents_visibility`'s confidential branch)
- Test: `tests/test_access_control_resolver.py` (extend)

**Interfaces:**
- Consumes: `AccessRequest`, `AccessRequestScope` (Task 1).
- Produces: `AuthorizationContext.active_confidential_grant_document_ids`, `.active_confidential_grant_stage_ids` — consumed by RAG/retrieval (no other task touches this; it closes the gap found during plan research where the batch path only ever checked team-scope grants).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_access_control_resolver.py`:

```python
from app.services.authorization_context import build_authorization_context
from app.services.access_control import classify_documents_visibility

class TestBatchVisibilityGrants(TestResolveEffectiveAccess):
    def test_document_scoped_grant_is_honored_in_batch_path(self):
        grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
        )
        self.db.add(grant)
        self.db.commit()
        ctx = build_authorization_context(self.db, self.viewer.user_id, self.project.project_id)
        result = classify_documents_visibility(self.db, ctx, [self.document.document_id])
        self.assertEqual(result[self.document.document_id], DocumentVisibility.fully_allowed)

    def test_stage_scoped_grant_is_honored_in_batch_path(self):
        grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        )
        self.db.add(grant)
        self.db.commit()
        ctx = build_authorization_context(self.db, self.viewer.user_id, self.project.project_id)
        result = classify_documents_visibility(self.db, ctx, [self.document.document_id])
        self.assertEqual(result[self.document.document_id], DocumentVisibility.fully_allowed)

    def test_no_grant_is_blocked_in_batch_path(self):
        ctx = build_authorization_context(self.db, self.viewer.user_id, self.project.project_id)
        result = classify_documents_visibility(self.db, ctx, [self.document.document_id])
        self.assertEqual(result[self.document.document_id], DocumentVisibility.blocked_by_sensitivity)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_access_control_resolver.py::TestBatchVisibilityGrants -v`
Expected: `test_document_scoped_grant_is_honored_in_batch_path` and `test_stage_scoped_grant_is_honored_in_batch_path` FAIL (both currently resolve to `blocked_by_sensitivity` since the batch path only checks team-scope grants); `test_no_grant_is_blocked_in_batch_path` passes already (no change needed there, but keep it as a regression guard).

- [ ] **Step 3: Extend `AuthorizationContext`**

Edit `app/services/authorization_context.py`. Add two new fields to the dataclass (after `active_confidential_grant_team_ids` on line 49):

```python
    active_confidential_grant_document_ids: set[uuid.UUID] = field(default_factory=set)
    active_confidential_grant_stage_ids: set[uuid.UUID] = field(default_factory=set)
```

Replace the grant-fetching block (lines 115-135) to fetch grants of any scope in one query, then partition by scope:

```python
    # 4. Active non-expired confidential grants of ANY scope for this user —
    # one query, partitioned in memory by scope, mirroring
    # resolve_effective_access()'s single-target query shape but as a bulk
    # precompute for the N-document batch case (classify_documents_visibility
    # below) so this stays the "no N+1 queries" path its own docstring
    # promises. Team-scope grants are still narrowed to this project's teams;
    # document/stage-scope grants are fetched unfiltered by team since a
    # grant's own document_id/stage_id is the only thing that needs to match
    # later, per-document, in classify_documents_visibility.
    active_confidential_grant_team_ids = set()
    active_confidential_grant_document_ids = set()
    active_confidential_grant_stage_ids = set()
    if not (is_org_admin or is_project_admin):
        grants = db.execute(
            select(AccessRequest).where(
                AccessRequest.user_id == user_id,
                AccessRequest.status == AccessRequestStatus.approved,
            )
        ).scalars().all()

        now = datetime.now(timezone.utc)

        def _is_active(g: AccessRequest) -> bool:
            if g.expires_at is None:
                return True
            exp = g.expires_at if g.expires_at.tzinfo else g.expires_at.replace(tzinfo=timezone.utc)
            return exp >= now

        for g in grants:
            if not _is_active(g):
                continue
            if g.scope == AccessRequestScope.team and g.team_id in team_ids:
                active_confidential_grant_team_ids.add(g.team_id)
            elif g.scope == AccessRequestScope.document and g.document_id is not None:
                active_confidential_grant_document_ids.add(g.document_id)
            elif g.scope == AccessRequestScope.stage and g.stage_id is not None:
                active_confidential_grant_stage_ids.add(g.stage_id)
```

Update the import line (line 25-31) to include `AccessRequestScope`:

```python
from app.models.team import (
    AccessRequest,
    AccessRequestScope,
    AccessRequestStatus,
    ProjectAdmin,
    TeamRole,
    UserTeamMembership,
)
```

Update the final `return AuthorizationContext(...)` call (lines 137-148) to pass the two new fields:

```python
        active_confidential_grant_team_ids=active_confidential_grant_team_ids,
        active_confidential_grant_document_ids=active_confidential_grant_document_ids,
        active_confidential_grant_stage_ids=active_confidential_grant_stage_ids,
```

- [ ] **Step 4: Extend `classify_documents_visibility`'s confidential branch**

Edit `app/services/access_control.py`. In `classify_documents_visibility`, replace lines 351-354 (the team-only grant check):

```python
            # Active approved grant on any overlapping team (viewer or contributor)
            if overlapping_teams & auth_context.active_confidential_grant_team_ids:
                out[doc_id] = DocumentVisibility.fully_allowed
                continue
```

with a three-way check:

```python
            # Active approved grant of ANY scope — document, stage, or team.
            if (
                doc_id in auth_context.active_confidential_grant_document_ids
                or document.stage_id in auth_context.active_confidential_grant_stage_ids
                or overlapping_teams & auth_context.active_confidential_grant_team_ids
            ):
                out[doc_id] = DocumentVisibility.fully_allowed
                continue
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_access_control_resolver.py -v`
Expected: all tests PASS, including the three new `TestBatchVisibilityGrants` cases.

- [ ] **Step 6: Run the RAG/retrieval test suite to confirm no regression**

Run: `python -m pytest tests/ -k "retrieval or rag" -v`
Expected: all PASS (no test in this suite is expected to reference document/stage grants yet, so this is a pure regression check).

- [ ] **Step 7: Commit**

```bash
git add app/services/authorization_context.py app/services/access_control.py tests/test_access_control_resolver.py
git commit -m "fix(access): honor document/stage-scoped grants in the batch RAG visibility path"
```

---

### Task 6: Multi-scope `request_confidential_access()`

**Files:**
- Modify: `app/services/access_requests_service.py`
- Test: `tests/test_access_requests_scoped.py` (new)

**Interfaces:**
- Consumes: `resolve_effective_access()` (Task 3); `AccessRequestScope` (Task 1); `TeamStageAccess` (`app/models/stage.py`).
- Produces: `request_confidential_access(db, *, user_id, team_id=None, document_id=None, stage_id=None, expected_tenant_id=None) -> AccessRequest` — the `team_id`-only call signature is preserved for the existing caller in `app/tools/rag_tools.py:409` (zero changes needed there). Consumed by Task 11 (`POST /access-requests`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_access_requests_scoped.py`:

```python
import unittest
import uuid
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models.document import Document, DocumentTeamVisibility, SensitivityLevel
from app.models.project import Project
from app.models.stage import Stage, TeamStageAccess
from app.models.team import AccessRequest, AccessRequestScope, AccessRequestStatus, Team, TeamRole, UserTeamMembership
from app.models.tenant import Tenant
from app.models.user import User
from app.services.access_requests_service import AccessRequestError, request_confidential_access


class TestScopedAccessRequests(unittest.TestCase):
    def setUp(self):
        with SessionLocal() as lookup_db:
            self.tenant = lookup_db.query(Tenant).first()
            tenant_id = self.tenant.tenant_id

        self.db = SessionLocal()
        self.db.info["tenant_id"] = str(tenant_id)

        self.project = Project(tenant_id=tenant_id, name=f"ScopedReq Test {uuid.uuid4().hex[:8]}")
        self.db.add(self.project)
        self.db.commit()

        self.engineering = Team(project_id=self.project.project_id, name="Engineering")
        self.qa = Team(project_id=self.project.project_id, name="QA")
        self.db.add_all([self.engineering, self.qa])
        self.db.commit()

        self.stage = Stage(project_id=self.project.project_id, name="Testing", order_index=1)
        self.db.add(self.stage)
        self.db.commit()

        self.contributor = User(email=f"contrib-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.db.add(self.contributor)
        self.db.commit()

        self.document = Document(
            tenant_id=tenant_id, project_id=self.project.project_id, stage_id=self.stage.stage_id,
            uploaded_by=self.contributor.user_id, uploaded_as_team_id=self.engineering.team_id,
            sensitivity_level=SensitivityLevel.confidential,
            original_filename="doc.md", mime_type="text/markdown",
        )
        self.db.add(self.document)
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        self.db.query(AccessRequest).filter(AccessRequest.user_id == self.contributor.user_id).delete()
        self.db.query(TeamStageAccess).filter(TeamStageAccess.stage_id == self.stage.stage_id).delete()
        self.db.query(Document).filter(Document.document_id == self.document.document_id).update({"current_version_id": None})
        self.db.query(Document).filter(Document.document_id == self.document.document_id).delete()
        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id == self.project.project_id).delete()
        self.db.query(User).filter(User.user_id == self.contributor.user_id).delete()
        self.db.query(Stage).filter(Stage.stage_id == self.stage.stage_id).delete()
        self.db.query(Team).filter(Team.project_id == self.project.project_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def test_document_scope_derives_team_id_from_document(self):
        self.db.add(UserTeamMembership(
            user_id=self.contributor.user_id, team_id=self.engineering.team_id,
            project_id=self.project.project_id, role=TeamRole.contributor,
        ))
        self.db.commit()
        req = request_confidential_access(
            self.db, user_id=self.contributor.user_id, document_id=self.document.document_id,
            expected_tenant_id=self.tenant.tenant_id,
        )
        self.assertEqual(req.scope, AccessRequestScope.document)
        self.assertEqual(req.document_id, self.document.document_id)
        self.assertEqual(req.team_id, self.engineering.team_id)

    def test_document_scope_requires_membership_on_the_derived_team(self):
        with self.assertRaises(AccessRequestError) as ctx:
            request_confidential_access(
                self.db, user_id=self.contributor.user_id, document_id=self.document.document_id,
                expected_tenant_id=self.tenant.tenant_id,
            )
        self.assertEqual(ctx.exception.status_code, 403)

    def test_stage_scope_picks_earliest_team_stage_access_among_requesters_teams(self):
        self.db.add(UserTeamMembership(
            user_id=self.contributor.user_id, team_id=self.qa.team_id,
            project_id=self.project.project_id, role=TeamRole.contributor,
        ))
        self.db.commit()
        # Engineering gets access first, QA second — requester is only on QA.
        self.db.add(TeamStageAccess(team_id=self.engineering.team_id, stage_id=self.stage.stage_id))
        self.db.commit()
        self.db.add(TeamStageAccess(team_id=self.qa.team_id, stage_id=self.stage.stage_id))
        self.db.commit()

        req = request_confidential_access(
            self.db, user_id=self.contributor.user_id, stage_id=self.stage.stage_id,
            expected_tenant_id=self.tenant.tenant_id,
        )
        self.assertEqual(req.scope, AccessRequestScope.stage)
        self.assertEqual(req.stage_id, self.stage.stage_id)
        self.assertEqual(req.team_id, self.qa.team_id)  # the only team the requester belongs to

    def test_stage_scope_with_no_qualifying_team_membership_is_403(self):
        self.db.add(TeamStageAccess(team_id=self.engineering.team_id, stage_id=self.stage.stage_id))
        self.db.commit()
        with self.assertRaises(AccessRequestError) as ctx:
            request_confidential_access(
                self.db, user_id=self.contributor.user_id, stage_id=self.stage.stage_id,
                expected_tenant_id=self.tenant.tenant_id,
            )
        self.assertEqual(ctx.exception.status_code, 403)

    def test_duplicate_document_request_is_rejected(self):
        self.db.add(UserTeamMembership(
            user_id=self.contributor.user_id, team_id=self.engineering.team_id,
            project_id=self.project.project_id, role=TeamRole.contributor,
        ))
        self.db.commit()
        request_confidential_access(
            self.db, user_id=self.contributor.user_id, document_id=self.document.document_id,
            expected_tenant_id=self.tenant.tenant_id,
        )
        with self.assertRaises(AccessRequestError) as ctx:
            request_confidential_access(
                self.db, user_id=self.contributor.user_id, document_id=self.document.document_id,
                expected_tenant_id=self.tenant.tenant_id,
            )
        self.assertEqual(ctx.exception.status_code, 409)

    def test_team_scope_call_shape_unchanged_for_existing_caller(self):
        # This is exactly how app/tools/rag_tools.py's request_confidential_access
        # tool calls this function today — must keep working with zero changes.
        self.db.add(UserTeamMembership(
            user_id=self.contributor.user_id, team_id=self.engineering.team_id,
            project_id=self.project.project_id, role=TeamRole.contributor,
        ))
        self.db.commit()
        req = request_confidential_access(
            self.db, user_id=self.contributor.user_id, team_id=self.engineering.team_id,
            expected_tenant_id=self.tenant.tenant_id,
        )
        self.assertEqual(req.scope, AccessRequestScope.team)
        self.assertEqual(req.team_id, self.engineering.team_id)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_access_requests_scoped.py -v`
Expected: FAIL — `request_confidential_access()` doesn't yet accept `document_id`/`stage_id` kwargs (`TypeError`).

- [ ] **Step 3: Implement**

Replace `live_request` and `request_confidential_access` in `app/services/access_requests_service.py` (lines 60-183). Remove `live_request` entirely (its only caller is being rewritten to use `resolve_effective_access` instead — confirmed via `grep -rn "live_request" app/` showing zero other callers).

```python
def _derive_document_team(db: Session, document_id: uuid.UUID) -> uuid.UUID | None:
    from app.models.document import Document
    document = db.get(Document, document_id)
    return document.uploaded_as_team_id if document else None


def _derive_stage_team(db: Session, user_id: uuid.UUID, stage_id: uuid.UUID) -> uuid.UUID | None:
    """
    The team responsible for approving a stage-scoped request: among the
    teams with TeamStageAccess to this stage, the one the requester
    themselves already belongs to (viewer/contributor), earliest grant
    first if more than one qualifies. team_id here is approval-routing
    metadata only — it is never part of the stage request's own identity
    (that's stage_id alone; dedup/effective-state resolution is keyed by
    stage_id, not by this derived team_id).
    """
    from app.models.stage import TeamStageAccess

    access_rows = db.execute(
        select(TeamStageAccess)
        .where(TeamStageAccess.stage_id == stage_id)
        .order_by(TeamStageAccess.created_at.asc())
    ).scalars().all()
    for row in access_rows:
        if _get_team_membership(db, user_id, row.team_id) is not None:
            return row.team_id
    return None


def request_confidential_access(
    db: Session,
    *,
    user_id: uuid.UUID,
    team_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    stage_id: uuid.UUID | None = None,
    expected_tenant_id: uuid.UUID | None = None,
) -> AccessRequest:
    """
    Create a pending confidential-access request for `user_id`, scoped to
    exactly one of team_id/document_id/stage_id (the caller passes exactly
    one; this is the same shape app/tools/rag_tools.py's existing tool call
    already uses for team_id, so that call site needs no changes).

    For document/stage scope, `team_id` is NEVER accepted from the caller —
    it is derived server-side from the target, per the spec's trust
    boundary (a client can request access to a target, never nominate which
    team approves it).

    Commits on success and returns the fresh AccessRequest row.

    Raises:
        AccessRequestError — team/target unknown or cross-tenant (404), not
        a member of the resolved team (403), already cleared or a live
        request already exists for this exact target (409).
    """
    from app.models.team import AccessRequestScope

    provided = [x for x in (team_id, document_id, stage_id) if x is not None]
    if len(provided) != 1:
        raise ValueError("Exactly one of team_id, document_id, stage_id must be given")

    if document_id is not None:
        scope = AccessRequestScope.document
        resolved_team_id = _derive_document_team(db, document_id)
        if resolved_team_id is None:
            raise AccessRequestError("Document not found", status_code=404)
    elif stage_id is not None:
        scope = AccessRequestScope.stage
        resolved_team_id = _derive_stage_team(db, user_id, stage_id)
        if resolved_team_id is None:
            raise AccessRequestError("You are not a member of any team with access to this stage", status_code=403)
    else:
        scope = AccessRequestScope.team
        resolved_team_id = team_id

    team = db.get(Team, resolved_team_id)
    project = db.get(Project, team.project_id) if team else None
    if team is None or project is None:
        raise AccessRequestError("Team not found", status_code=404)
    if expected_tenant_id is not None and project.tenant_id != expected_tenant_id:
        raise AccessRequestError("Team not found", status_code=404)

    is_org_admin = _is_org_admin(db, user_id)
    is_proj_admin = _is_project_admin(db, user_id, project.project_id)
    membership: UserTeamMembership | None = _get_team_membership(db, user_id, resolved_team_id)

    if membership is None and not is_proj_admin and not is_org_admin:
        raise AccessRequestError("You are not a member of this team", status_code=403)

    if is_org_admin or is_proj_admin or (
        membership is not None and membership.role == TeamRole.team_lead
    ):
        raise AccessRequestError(
            "You already have confidential access on this team.", status_code=409
        )

    from app.services.access_control import resolve_effective_access
    effective = resolve_effective_access(
        db, user_id,
        document_id=document_id if scope == AccessRequestScope.document else None,
        stage_id=stage_id if scope == AccessRequestScope.stage else None,
        team_id=resolved_team_id if scope == AccessRequestScope.team else None,
    )
    if effective.status in ("granted", "pending"):
        raise AccessRequestError(
            f"You already have a {effective.status} confidential-access request for this target.",
            status_code=409,
        )

    req = AccessRequest(
        user_id=user_id, team_id=resolved_team_id, scope=scope,
        document_id=document_id, stage_id=stage_id,
        status=AccessRequestStatus.pending,
    )
    db.add(req)
    db.flush()
    record_audit(
        db,
        actor_id=user_id,
        action="REQUEST_CONFIDENTIAL_ACCESS",
        resource_type="team",
        resource_id=team.team_id,
        details={"team": team.name, "project": project.name, "scope": scope.value},
    )
    notify_access_request_created(
        db, team_id=team.team_id, team_name=team.name, project_id=project.project_id,
        requester_id=user_id, request_id=req.request_id,
    )
    db.commit()
    db.refresh(req)
    return req
```

Note: `is_expired` (lines 56-57) stays unchanged — still used by `app/routers/access_requests.py`'s `_serialize`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_access_requests_scoped.py -v`
Expected: all 6 tests PASS.

- [ ] **Step 5: Run the existing access-requests test suite (if any) to confirm no regression**

Run: `python -m pytest tests/ -k "access_request" -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/access_requests_service.py tests/test_access_requests_scoped.py
git commit -m "feat(access): support document/stage-scoped requests, deriving team_id server-side"
```

---

### Task 7: Enforcement — `finalize_document_revision()`

**Files:**
- Modify: `app/services/document_finalize.py`
- Modify: `app/routers/document_review.py` (two call sites)
- Test: `tests/test_grant_enforcement.py` (new)

**Interfaces:**
- Consumes: `is_grant_only_confidential_access()` (Task 4).
- Produces: `GrantOnlyAccessError` (new, `app/services/document_finalize.py`) — consumed only within this task (both its callers already propagate uncaught exceptions to the router, per plan research).

- [ ] **Step 1: Write the failing test**

Create `tests/test_grant_enforcement.py`:

```python
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models.document import Document, DocumentTeamVisibility, DocumentVersion, DocumentStatus, SensitivityLevel
from app.models.project import Project
from app.models.stage import Stage
from app.models.team import AccessRequest, AccessRequestScope, AccessRequestStatus, Team, TeamRole, UserTeamMembership
from app.models.tenant import Tenant
from app.models.user import User
from app.services import draft_workspace
from app.services.document_finalize import GrantOnlyAccessError, finalize_document_revision


class TestFinalizeGrantEnforcement(unittest.TestCase):
    def setUp(self):
        with SessionLocal() as lookup_db:
            self.tenant = lookup_db.query(Tenant).first()
            tenant_id = self.tenant.tenant_id

        self.db = SessionLocal()
        self.db.info["tenant_id"] = str(tenant_id)

        self.project = Project(tenant_id=tenant_id, name=f"Enforce Test {uuid.uuid4().hex[:8]}")
        self.db.add(self.project)
        self.db.commit()

        self.team = Team(project_id=self.project.project_id, name="Enforce Team")
        self.db.add(self.team)
        self.db.commit()

        self.stage = Stage(project_id=self.project.project_id, name="Enforce Stage", order_index=1)
        self.db.add(self.stage)
        self.db.commit()

        self.contributor = User(email=f"c-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.db.add(self.contributor)
        self.db.commit()
        self.db.add(UserTeamMembership(
            user_id=self.contributor.user_id, team_id=self.team.team_id,
            project_id=self.project.project_id, role=TeamRole.contributor,
        ))
        self.db.commit()

        version_id = uuid.uuid4()
        self.document = Document(
            tenant_id=tenant_id, project_id=self.project.project_id, stage_id=self.stage.stage_id,
            uploaded_by=self.contributor.user_id, uploaded_as_team_id=self.team.team_id,
            sensitivity_level=SensitivityLevel.confidential,
            original_filename="doc.md", mime_type="text/markdown",
            current_version_id=None,
        )
        self.db.add(self.document)
        self.db.commit()
        version = DocumentVersion(
            version_id=version_id, document_id=self.document.document_id,
            file_data=b"original", file_size_bytes=8, uploaded_by=self.contributor.user_id,
            status=DocumentStatus.pending_review,
        )
        self.db.add(version)
        self.document.current_version_id = version_id
        self.db.commit()
        self.db.add(DocumentTeamVisibility(document_id=self.document.document_id, team_id=self.team.team_id))
        self.db.commit()

        self.session_id = f"test-session-{uuid.uuid4().hex[:8]}"
        draft_workspace.write_working_draft(self.session_id, "# Revised content\n\nSome text.")

    def tearDown(self):
        draft_workspace.delete_working_draft(self.session_id)
        self.db.rollback()
        self.db.query(AccessRequest).filter(AccessRequest.team_id == self.team.team_id).delete()
        self.db.query(DocumentTeamVisibility).filter(DocumentTeamVisibility.document_id == self.document.document_id).delete()
        self.db.query(Document).filter(Document.document_id == self.document.document_id).update({"current_version_id": None})
        self.db.query(DocumentVersion).filter(DocumentVersion.document_id == self.document.document_id).delete()
        self.db.query(Document).filter(Document.document_id == self.document.document_id).delete()
        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id == self.project.project_id).delete()
        self.db.query(User).filter(User.user_id == self.contributor.user_id).delete()
        self.db.query(Stage).filter(Stage.stage_id == self.stage.stage_id).delete()
        self.db.query(Team).filter(Team.project_id == self.project.project_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def test_grant_only_contributor_cannot_finalize(self):
        grant = AccessRequest(
            user_id=self.contributor.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
        )
        self.db.add(grant)
        self.db.commit()

        with self.assertRaises(GrantOnlyAccessError):
            finalize_document_revision(
                self.db, document_id=self.document.document_id,
                user_id=self.contributor.user_id, session_id=self.session_id,
            )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_grant_enforcement.py -v`
Expected: FAIL — `ImportError: cannot import name 'GrantOnlyAccessError'`.

- [ ] **Step 3: Implement**

Edit `app/services/document_finalize.py`. Add after `class NoWorkingDraftError(Exception): pass` (after line 51):

```python
class GrantOnlyAccessError(Exception):
    """
    The caller's access to this document comes only from a read-only
    confidential-access grant (not native role-based access) — a grant
    never confers write/finalize capability, however the document was
    originally reached.
    """
```

Add the import at the top (with the other imports, after line 42):

```python
from app.services.access_control import is_grant_only_confidential_access
```

Insert the check in `finalize_document_revision` right after the document is loaded (after line 173, `if document is None: raise DocumentNotFoundError(...)`):

```python
    if is_grant_only_confidential_access(db, user_id, document):
        raise GrantOnlyAccessError(
            "Your access to this document is read-only (granted via a confidential-access "
            "request, not your team role) — you cannot finalize a revision."
        )
```

- [ ] **Step 4: Wire the two router call sites**

Edit `app/routers/document_review.py`. Add to the imports (near line 52):

```python
from app.services.document_finalize import GrantOnlyAccessError
```

In `review_message` (around line 439-448), add a specific catch **before** the bare `except Exception`:

```python
    try:
        turn = run_review_turn(
            db, session_id=body.session_id, document_id=document.document_id,
            user_id=identity.user_id, message=body.message,
        )
    except GrantOnlyAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — Groq / rate-limit / parse failures
        raise HTTPException(
            status_code=502,
            detail=f"The document-review agent could not complete this turn: {exc}",
        ) from exc
```

In `version_review_message` (around line 591-600), same addition:

```python
    try:
        turn = run_version_review_turn(
            db, session_id=body.session_id, document_id=document.document_id,
            user_id=identity.user_id, message=body.message,
        )
    except GrantOnlyAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — Groq / rate-limit / parse failures
        raise HTTPException(
            status_code=502,
            detail=f"The version-review agent could not complete this turn: {exc}",
        ) from exc
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_grant_enforcement.py -v`
Expected: PASS.

- [ ] **Step 6: Run the document-review test suite to confirm no regression**

Run: `python -m pytest tests/ -k "document_review or document_finalize or version_review" -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add app/services/document_finalize.py app/routers/document_review.py tests/test_grant_enforcement.py
git commit -m "feat(access): enforce read-only grants at the shared finalize_document_revision choke point"
```

---

### Task 8: Enforcement — `delete_document()` / `delete_version()`

**Files:**
- Modify: `app/services/document_delete.py`
- Test: `tests/test_grant_enforcement.py` (extend)

**Interfaces:**
- Consumes: `is_grant_only_confidential_access()` (Task 4).
- Produces: nothing new — reuses the module's existing `PermissionDeniedError`, already caught and translated to 403 at both router call sites (`app/routers/document_review.py:780`, `:813`) with zero router changes needed.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_grant_enforcement.py` (new test class, reusing `setUp`/`tearDown` from `TestFinalizeGrantEnforcement` via inheritance):

```python
from app.services.document_delete import PermissionDeniedError, delete_version


class TestDeleteVersionGrantEnforcement(TestFinalizeGrantEnforcement):
    def test_grant_only_contributor_cannot_delete_their_own_non_live_version(self):
        # A second, later version the contributor uploaded themselves — the
        # normal can_delete_version() rule (own version, after live) would
        # otherwise allow this.
        second_version_id = uuid.uuid4()
        second_version = DocumentVersion(
            version_id=second_version_id, document_id=self.document.document_id,
            file_data=b"newer draft", file_size_bytes=11, uploaded_by=self.contributor.user_id,
            status=DocumentStatus.pending_review,
        )
        self.db.add(second_version)
        self.db.commit()

        grant = AccessRequest(
            user_id=self.contributor.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
        )
        self.db.add(grant)
        self.db.commit()

        with self.assertRaises(PermissionDeniedError):
            delete_version(
                self.db, document_id=self.document.document_id, version_id=second_version_id,
                actor_id=self.contributor.user_id, is_admin=False,
            )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_grant_enforcement.py::TestDeleteVersionGrantEnforcement -v`
Expected: FAIL — the delete succeeds today (no `PermissionDeniedError` raised) because `can_delete_version` only checks ownership/admin, not grant status.

- [ ] **Step 3: Implement**

Edit `app/services/document_delete.py`. Add the import (with the others, after line 56):

```python
from app.services.access_control import is_grant_only_confidential_access
```

In `delete_version` (after the `document = db.get(Document, document_id)` / not-found check, after line 114):

```python
    if is_grant_only_confidential_access(db, actor_id, document):
        raise PermissionDeniedError(
            "Your access to this document is read-only (granted via a confidential-access "
            "request, not your team role) — you cannot delete a version."
        )
```

In `delete_document` (after the `document = db.get(Document, document_id)` / not-found check, after line 158). Note: this check is unreachable given today's role bars (the router already requires `is_admin` before calling `delete_document`, and `is_grant_only_confidential_access` always returns `False` for org_admin/project_admin) — included anyway for defense-in-depth consistency with the spec, and because relying on an upstream router-level gate to make a service-level check "provably unreachable" is exactly the kind of assumption that silently breaks if the router's own gate ever changes:

```python
    if is_grant_only_confidential_access(db, actor_id, document):
        raise PermissionDeniedError(
            "Your access to this document is read-only (granted via a confidential-access "
            "request, not your team role) — you cannot delete this document."
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_grant_enforcement.py -v`
Expected: both tests in the file PASS.

- [ ] **Step 5: Run the document-delete test suite to confirm no regression**

Run: `python -m pytest tests/ -k "document_delete" -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/document_delete.py tests/test_grant_enforcement.py
git commit -m "feat(access): enforce read-only grants in delete_document/delete_version"
```

---

### Task 9: Enforcement — workflow functions (`submit_for_review`, `approve_document`, `reject_document`, `reset_to_draft_if_approved`) + `delete_stage()` comment

**Files:**
- Modify: `app/services/workflow.py`
- Modify: `app/routers/document_review.py` (one new catch clause)
- Modify: `app/routers/stages.py` (comment only, no logic change)
- Test: `tests/test_grant_enforcement.py` (extend)

**Interfaces:**
- Consumes: `is_grant_only_confidential_access()` (Task 4).
- Produces: nothing new — reuses `WorkflowPermissionError`, already caught in `app/routers/workflow.py`'s `submit`/`approve`/`reject` endpoints. `version_review_message` needs one new catch clause for `reset_to_draft_if_approved`'s path.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_grant_enforcement.py`:

```python
from app.services.workflow import WorkflowPermissionError, submit_for_review


class TestSubmitGrantEnforcement(TestFinalizeGrantEnforcement):
    def test_grant_only_contributor_cannot_submit_for_review(self):
        from app.models.workflow import WorkflowState, WorkflowStatus
        self.db.add(WorkflowState(document_id=self.document.document_id, state=WorkflowStatus.draft))
        self.db.commit()

        grant = AccessRequest(
            user_id=self.contributor.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
        )
        self.db.add(grant)
        self.db.commit()

        with self.assertRaises(WorkflowPermissionError):
            submit_for_review(
                self.db, self.document.document_id, self.contributor.user_id,
                self.team.team_id, self.project.project_id, "contributor",
            )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_grant_enforcement.py::TestSubmitGrantEnforcement -v`
Expected: FAIL — `submit_for_review` succeeds today regardless of grant-only status.

- [ ] **Step 3: Implement**

Edit `app/services/workflow.py`. Add the import (with the others, after line 43):

```python
from app.services.access_control import is_grant_only_confidential_access
```

In `submit_for_review` (after the `has_permission` check, after line 176):

```python
    document = db.get(Document, document_id)
    if document is not None and is_grant_only_confidential_access(db, user_id, document):
        raise WorkflowPermissionError(
            "Your access to this document is read-only (granted via a confidential-access "
            "request, not your team role) — you cannot submit it for review."
        )
```

Note: `submit_for_review` doesn't currently load `document` until line 189 (later, for the notification call) — this adds an earlier load. Remove the later redundant `document = db.get(Document, document_id)` at line 189 and reuse this one instead (the variable is already in scope for the rest of the function).

In `approve_document` (after the `has_permission` check at line 258-261, **before** the `state.state != pending_review` check at line 262 — `document` is already loaded two lines later at line 267; move that load up to right after the permission check so the grant-only check can use it):

```python
    if not has_permission(db, approver_id, "approve", team_id, project_id):
        raise WorkflowPermissionError(
            "You do not have permission to approve documents on this team."
        )
    document = db.get(Document, document_id)
    # Currently unreachable in practice: 'approve' already requires
    # team_lead+ (has_permission above), and team_lead+ always has native —
    # never grant-only — access, so this can never actually trigger today.
    # Kept for defense in depth in case the required role for 'approve'
    # ever changes; the message itself must stay plain and user-facing.
    if document is not None and is_grant_only_confidential_access(db, approver_id, document):
        raise WorkflowPermissionError(
            "Your access to this document is read-only (granted via a confidential-access "
            "request, not your team role) — you cannot approve it."
        )
```

Remove the now-duplicate `document = db.get(Document, document_id)` at line 267 (the original location) since `document` is now loaded earlier.

In `reject_document` (after the `has_permission` check at line 379-382, before line 383's state check — load `document` here too, ahead of its current load at line 389):

```python
    if not has_permission(db, approver_id, "reject", team_id, project_id):
        raise WorkflowPermissionError(
            "You do not have permission to reject documents on this team."
        )
    document = db.get(Document, document_id)
    # Same defense-in-depth note as approve_document above: currently
    # unreachable since 'reject' also requires team_lead+.
    if document is not None and is_grant_only_confidential_access(db, approver_id, document):
        raise WorkflowPermissionError(
            "Your access to this document is read-only (granted via a confidential-access "
            "request, not your team role) — you cannot reject it."
        )
```

Remove the now-duplicate `document = db.get(Document, document_id)` at line 389.

In `reset_to_draft_if_approved` (after the `state is None or state.state != approved` early-return at line 344, before `previous_approver = state.approved_by`):

```python
    document = db.get(Document, document_id)
    if document is not None and is_grant_only_confidential_access(db, triggered_by, document):
        raise WorkflowPermissionError(
            "Your access to this document is read-only (granted via a confidential-access "
            "request, not your team role) — you cannot revise it in a way that resets its "
            "approval state."
        )
```

**`promote_version_on_approval()` deliberately gets no check of its own.** The spec (§6.3) lists it as a shared choke point reached by normal approval, auto-approve, and auto-index — but unlike `finalize_document_revision()` (Task 7, where centralizing the check was correct because ALL of its callers operate on an existing document), `promote_version_on_approval()`'s third caller (`document_upload_review.py`'s auto-approve-on-upload branch) is the brand-new-document-creation path spec §8 explicitly excludes. Putting the check inside `promote_version_on_approval()` itself would incorrectly apply it to that excluded caller too. Its two in-scope callers — `approve_document()` (this task, above) and `finalize_document_revision()`'s own internal call when `should_index()` is true on a non-approval-required stage (Task 7, already gated at that function's own top) — already have the check applied before they ever reach it. No further code is needed here; this paragraph is the plan's record of that reasoning, since the spec's own text (written before this plan resolved the tension) reads as if the check belongs inside `promote_version_on_approval()` directly.

- [ ] **Step 4: Wire the one new router catch clause**

`reset_to_draft_if_approved` is called only from `app/services/document_version_review.py`'s `run_version_review_turn` (not from `document_review_chat.py`'s `run_review_turn`), reached via `POST /documents/review/version-message`. That endpoint's bare `except Exception` (in `app/routers/document_review.py`'s `version_review_message`, edited in Task 7 Step 4) currently has no catch for `WorkflowPermissionError` at all. Add the import and a new catch clause, ordered before the bare `except Exception`:

```python
from app.services.workflow import WorkflowPermissionError
```

```python
    try:
        turn = run_version_review_turn(
            db, session_id=body.session_id, document_id=document.document_id,
            user_id=identity.user_id, message=body.message,
        )
    except GrantOnlyAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except WorkflowPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — Groq / rate-limit / parse failures
        raise HTTPException(
            status_code=502,
            detail=f"The version-review agent could not complete this turn: {exc}",
        ) from exc
```

(`review_message`, the other endpoint edited in Task 7, does not call `reset_to_draft_if_approved` at all — no change needed there for this specific function.)

- [ ] **Step 5: Add the `delete_stage()` comment**

Edit `app/routers/stages.py`. In `delete_stage`, right after the `_require_project_admin(identity, project_id)` call (after line 331):

```python
    # No grant-only enforcement is needed on the bulk document reassignment
    # below: this endpoint is already gated to org_admin/project_admin only
    # (the check above), and is_grant_only_confidential_access() always
    # returns False for both — native access, unconditional first branch.
    # The actor here can never be grant-only for any document, so the
    # "grant-only actor bulk-reassigning documents they can't fully see"
    # concern doesn't apply through this endpoint as it exists today.
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_grant_enforcement.py -v`
Expected: all tests across all classes in the file PASS.

- [ ] **Step 7: Run the full workflow + stages test suites to confirm no regression**

Run: `python -m pytest tests/ -k "workflow or stages" -v`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add app/services/workflow.py app/routers/document_review.py app/routers/stages.py tests/test_grant_enforcement.py
git commit -m "feat(access): enforce read-only grants across the approval workflow"
```

---

### Task 10: `GET /access-requests/status` — the effective-state endpoint

**Files:**
- Modify: `app/routers/access_requests.py`
- Modify: `frontend/src/lib/api.js` (add `status()` — done together since it's a thin pass-through, see Task 15 for the fuller frontend batch)
- Test: none new (covered by Task 3's resolver tests; this task is a thin HTTP wrapper — verified via manual curl/HTTPie check in Step 3)

**Interfaces:**
- Consumes: `resolve_effective_access()` (Task 3).
- Produces: `GET /access-requests/status?document_id=... | stage_id=... | team_id=...` — consumed by Task 16 (frontend trigger state machine).

- [ ] **Step 1: Implement the endpoint**

Edit `app/routers/access_requests.py`. Add a new response model and endpoint after `AccessRequestOut` (after line 68):

```python
class EffectiveAccessOut(BaseModel):
    status: str  # "none" | "pending" | "granted" | "denied" | "expired"
    via_grant: bool
    expires_at: str | None
    request_id: str | None


@router.get("/status", response_model=EffectiveAccessOut)
def access_status(
    document_id: uuid.UUID | None = None,
    stage_id: uuid.UUID | None = None,
    team_id: uuid.UUID | None = None,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    """
    Effective-state endpoint, not a raw request-row lookup: resolves current
    grant coverage AND pending/terminal request state for the exact target
    in one call, via the same resolver document visibility and mutation
    enforcement use. The frontend renders this verdict directly — it never
    reconstructs scope/target/grant-precedence logic itself.
    """
    provided = [x for x in (document_id, stage_id, team_id) if x is not None]
    if len(provided) != 1:
        raise HTTPException(
            status_code=422, detail="Exactly one of document_id, stage_id, team_id is required."
        )
    from app.services.access_control import resolve_effective_access

    result = resolve_effective_access(
        db, identity.user_id, document_id=document_id, stage_id=stage_id, team_id=team_id,
    )
    return EffectiveAccessOut(
        status=result.status,
        via_grant=result.via_grant,
        expires_at=result.expires_at.isoformat() if result.expires_at else None,
        request_id=str(result.request_id) if result.request_id else None,
    )
```

- [ ] **Step 2: Restart the backend and verify the route is registered**

Run: `cd D:\Ra\DocFlowAI\meem_salvage && python -m uvicorn app.main:app --port 8000` (per this repo's established convention: no hot-reload, kill+relaunch after every backend change, using the Python 3.12 interpreter, not the system default).

Run: `curl http://127.0.0.1:8000/openapi.json | grep -o '"/access-requests/status"'`
Expected: prints `"/access-requests/status"` — confirms the route registered.

- [ ] **Step 3: Manual verification against a real request**

Run (with a real bearer token and document_id from a running dev session):
```bash
curl -H "Authorization: Bearer <token>" "http://127.0.0.1:8000/access-requests/status?document_id=<uuid>"
```
Expected: a JSON body matching `EffectiveAccessOut`'s shape with a `status` of `none` (for a document the caller has never requested/been granted).

- [ ] **Step 4: Commit**

```bash
git add app/routers/access_requests.py
git commit -m "feat(access): add GET /access-requests/status, the effective-state endpoint"
```

---

### Task 11: Enrich `GET /access-requests/pending` and `GET /access-requests/mine`

**Files:**
- Modify: `app/routers/access_requests.py`

**Interfaces:**
- Consumes: `AccessRequestScope` (Task 1); `GRANT_TTL_DAYS` (existing, `app/services/access_requests_service.py:40`).
- Produces: `DOCUMENT_GRANT_TTL_HOURS` (new constant, `app/services/access_requests_service.py`); `AccessRequestOut` gains `scope`, `target_id`, `target_name`, `grant_duration_label` — consumed by Task 18 (AdminPage) and Task 19 (My Access Requests panel).

- [ ] **Step 1: Add `DOCUMENT_GRANT_TTL_HOURS`**

Edit `app/services/access_requests_service.py`, next to the existing `GRANT_TTL_DAYS = 90` (line 40):

```python
GRANT_TTL_DAYS = 90
DOCUMENT_GRANT_TTL_HOURS = 72
```

- [ ] **Step 2: Extend `AccessRequestOut` and `_serialize`**

Edit `app/routers/access_requests.py`. Extend `AccessRequestOut` (lines 56-68):

```python
class AccessRequestOut(BaseModel):
    request_id: str
    user_id: str
    requester_email: str | None
    team_id: str
    team_name: str
    project_id: str
    project_name: str
    scope: str  # "document" | "stage" | "team"
    target_id: str  # document_id, stage_id, or team_id, matching `scope`
    target_name: str  # filename, stage name, or team name, matching `scope`
    grant_duration_label: str  # "72 hours" | "90 days" — what approving THIS request grants
    status: str
    requested_at: str | None
    decided_at: str | None
    expires_at: str | None
    active: bool
```

Rewrite `_serialize` (lines 71-88) to resolve `target_name`/`grant_duration_label`:

```python
def _serialize(db: Session, r: AccessRequest, *, team=None, project=None, requester=None) -> AccessRequestOut:
    from app.models.document import Document
    from app.models.stage import Stage
    from app.services.access_requests_service import DOCUMENT_GRANT_TTL_HOURS, GRANT_TTL_DAYS

    team = team or db.get(Team, r.team_id)
    project = project or (db.get(Project, team.project_id) if team else None)
    requester = requester or db.get(User, r.user_id)

    if r.scope == AccessRequestScope.document:
        target_id = str(r.document_id)
        doc = db.get(Document, r.document_id)
        target_name = doc.original_filename if doc else "(deleted document)"
        grant_duration_label = f"{DOCUMENT_GRANT_TTL_HOURS} hours"
    elif r.scope == AccessRequestScope.stage:
        target_id = str(r.stage_id)
        stage = db.get(Stage, r.stage_id)
        target_name = stage.name if stage else "(deleted stage)"
        grant_duration_label = f"{GRANT_TTL_DAYS} days"
    else:
        target_id = str(r.team_id)
        target_name = team.name if team else "(unknown)"
        grant_duration_label = f"{GRANT_TTL_DAYS} days"

    return AccessRequestOut(
        request_id=str(r.request_id),
        user_id=str(r.user_id),
        requester_email=requester.email if requester else None,
        team_id=str(r.team_id),
        team_name=team.name if team else "(unknown)",
        project_id=str(team.project_id) if team else "",
        project_name=project.name if project else "(unknown)",
        scope=r.scope.value,
        target_id=target_id,
        target_name=target_name,
        grant_duration_label=grant_duration_label,
        status=r.status.value,
        requested_at=r.requested_at.isoformat() if r.requested_at else None,
        decided_at=r.decided_at.isoformat() if r.decided_at else None,
        expires_at=r.expires_at.isoformat() if r.expires_at else None,
        active=(r.status == AccessRequestStatus.approved and not _is_expired(r)),
    )
```

Add `AccessRequestScope` to the existing import (line 31-35):

```python
from app.models.team import (
    AccessRequest,
    AccessRequestScope,
    AccessRequestStatus,
    Team,
)
```

`_serialize` imports `DOCUMENT_GRANT_TTL_HOURS`/`GRANT_TTL_DAYS` locally (inside the function body, as written above) rather than adding them to the module-level import block — this avoids touching the existing `GRANT_TTL_DAYS as _GRANT_TTL_DAYS` alias at line 40, which `_decide` (Step 3) still uses under its original name.

- [ ] **Step 3: Also update `_decide` to use the scope-aware TTL**

Edit `_decide` (lines 141-173). Replace the flat `timedelta(days=_GRANT_TTL_DAYS)` at line 160 with a scope-aware duration:

```python
    from app.services.access_requests_service import DOCUMENT_GRANT_TTL_HOURS

    now = datetime.now(timezone.utc)
    r.status = AccessRequestStatus.approved if approve else AccessRequestStatus.denied
    r.decided_by = identity.user_id
    r.decided_at = now
    if approve:
        r.expires_at = (
            now + timedelta(hours=DOCUMENT_GRANT_TTL_HOURS) if r.scope == AccessRequestScope.document
            else now + timedelta(days=_GRANT_TTL_DAYS)
        )
    else:
        r.expires_at = None
```

This is the actual bug this task's real behavior depends on: today `_decide` always applies the flat 90-day TTL regardless of scope (spec §1.3 requires document grants to expire in 72 hours) — this fixes it.

- [ ] **Step 4: Restart backend and verify**

Run: `python -m uvicorn app.main:app --port 8000` (kill+relaunch, per convention).

Write a manual verification script (throwaway, not committed — this repo's `.gitignore` already excludes `scratch/`):

```python
# scratch/verify_scoped_ttl.py
import uuid
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models.project import Project
from app.models.tenant import Tenant
from app.models.team import AccessRequest, AccessRequestScope, AccessRequestStatus, Team
from app.models.user import User

db = SessionLocal()
tenant = db.query(Tenant).first()
db.info["tenant_id"] = str(tenant.tenant_id)
db = SessionLocal()
db.info["tenant_id"] = str(tenant.tenant_id)

project = Project(tenant_id=tenant.tenant_id, name=f"TTL Check {uuid.uuid4().hex[:8]}")
db.add(project)
db.commit()
team = Team(project_id=project.project_id, name="TTL Check Team")
db.add(team)
db.commit()
approver = User(email=f"approver-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant.tenant_id, password_hash="x")
requester = User(email=f"requester-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant.tenant_id, password_hash="x")
db.add_all([approver, requester])
db.commit()

doc_req = AccessRequest(
    user_id=requester.user_id, team_id=team.team_id, scope=AccessRequestScope.document,
    document_id=uuid.uuid4(), status=AccessRequestStatus.pending,
)
team_req = AccessRequest(
    user_id=requester.user_id, team_id=team.team_id, scope=AccessRequestScope.team,
    status=AccessRequestStatus.pending,
)
db.add_all([doc_req, team_req])
db.commit()

from app.services.access_requests_service import DOCUMENT_GRANT_TTL_HOURS, GRANT_TTL_DAYS
from datetime import timedelta

now = datetime.now(timezone.utc)
for r, expected in ((doc_req, timedelta(hours=DOCUMENT_GRANT_TTL_HOURS)), (team_req, timedelta(days=GRANT_TTL_DAYS))):
    r.status = AccessRequestStatus.approved
    r.decided_by = approver.user_id
    r.decided_at = now
    r.expires_at = now + (timedelta(hours=DOCUMENT_GRANT_TTL_HOURS) if r.scope == AccessRequestScope.document else timedelta(days=GRANT_TTL_DAYS))
    actual = r.expires_at - r.decided_at
    print(f"{r.scope.value}: expected {expected}, got {actual}, match={actual == expected}")

db.query(AccessRequest).filter(AccessRequest.team_id == team.team_id).delete()
db.query(User).filter(User.user_id.in_([approver.user_id, requester.user_id])).delete(synchronize_session=False)
db.query(Team).filter(Team.team_id == team.team_id).delete()
db.query(Project).filter(Project.project_id == project.project_id).delete()
db.commit()
db.close()
```

Run: `python scratch/verify_scoped_ttl.py`
Expected: prints `document: expected 3 days, 0:00:00, got 3 days, 0:00:00, match=True` (72 hours) and `team: expected 90 days, 0:00:00, got 90 days, 0:00:00, match=True`. Delete this scratch script when done — it is not part of the plan's deliverable, only a manual check.

Note: this script exercises the TTL constants and assignment logic directly rather than calling `_decide()` itself, since `_decide` requires a fully-authenticated `ResolvedIdentity` and a real HTTP-level permission check that's more naturally covered by Step 4's live UI/curl check than by a standalone script.

- [ ] **Step 5: Commit**

```bash
git add app/routers/access_requests.py app/services/access_requests_service.py
git commit -m "feat(access): enrich pending/mine with scope/target/duration; fix TTL to respect scope on approval"
```

---

### Task 12: Notification `audience` wiring

**Files:**
- Modify: `app/services/notifications.py`
- Modify: `app/services/access_requests_service.py` (one call site)
- Modify: `app/routers/access_requests.py` (one call site)

**Interfaces:**
- Consumes: `NotificationAudience` (Task 2).
- Produces: `Notification.audience` populated on every access-request notification — consumed by Task 20 (frontend click-through).

- [ ] **Step 1: Add `audience` param to both notification functions**

Edit `app/services/notifications.py`. Update `notify_access_request_created` (lines 111-133):

```python
from app.models.notification import Notification, NotificationType, NotificationAudience
```

```python
def notify_access_request_created(
    db: Session, *, team_id: uuid.UUID, team_name: str, project_id: uuid.UUID,
    requester_id: uuid.UUID, request_id: uuid.UUID,
) -> None:
    """Confidential access requested -> every team_lead on that team."""
    lead_user_ids = set(db.execute(
        select(UserTeamMembership.user_id).where(
            UserTeamMembership.team_id == team_id,
            UserTeamMembership.role == TeamRole.team_lead,
        )
    ).scalars())
    requester = db.get(User, requester_id)
    requester_label = requester.full_name or requester.email if requester else "Someone"
    for recipient_id in lead_user_ids:
        _create(
            db,
            recipient_user_id=recipient_id,
            notification_type=NotificationType.access_request_created,
            title=f"{requester_label} requested confidential access on {team_name}",
            resource_type="access_request",
            resource_id=request_id,
            project_id=project_id,
            audience=NotificationAudience.approver,
        )
```

Update `notify_access_request_decided` (lines 136-152) the same way, passing `audience=NotificationAudience.requester`.

Update `_create` (lines 35-54) to accept and pass through the new param:

```python
def _create(
    db: Session,
    *,
    recipient_user_id: uuid.UUID,
    notification_type: NotificationType,
    title: str,
    body: str | None = None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    audience: NotificationAudience | None = None,
) -> None:
    db.add(Notification(
        recipient_user_id=recipient_user_id,
        notification_type=notification_type,
        title=title,
        body=body,
        resource_type=resource_type,
        resource_id=resource_id,
        project_id=project_id,
        audience=audience,
    ))
```

- [ ] **Step 2: Include `audience` in the serialized notification payload**

Edit `_serialize` in `app/services/notifications.py` (lines 236-247), add:

```python
        "audience": n.audience.value if n.audience else None,
```

- [ ] **Step 3: Restart backend and verify**

Run: `python -m uvicorn app.main:app --port 8000` (kill+relaunch).

Trigger a real access request via the existing UI (or a curl `POST /access-requests`), then `GET /notifications` as the recipient team_lead, and confirm the response includes `"audience": "approver"`.

- [ ] **Step 4: Commit**

```bash
git add app/services/notifications.py
git commit -m "feat(notifications): populate audience on access-request notifications"
```

---

### Task 13: `list_documents` redaction — locked stubs (spec §13.D)

**Files:**
- Modify: `app/routers/documents.py`

**Interfaces:**
- Consumes: `classify_document_visibility()`, `DocumentVisibility` (existing, `app/services/access_control.py`).
- Produces: `DocumentListItem.locked` — consumed by Task 15 (`mapDocs`) and Task 16 (`DocumentItem` locked variant).

- [ ] **Step 1: Extend `DocumentListItem` and rewrite the filter**

Edit `app/routers/documents.py`. Add `locked: bool = False` to `DocumentListItem` (after line 67):

```python
class DocumentListItem(BaseModel):
    document_id: str
    original_filename: str
    project_id: str
    stage_id: str
    sensitivity_level: str
    uploaded_as_team_id: str
    workflow_state: str | None
    uploaded_by: str
    locked: bool = False
```

Replace the import (line 25) and the filtering logic (lines 134-175):

```python
from app.services.access_control import build_access_filter, classify_document_visibility, DocumentVisibility
```

```python
    access_filter = build_access_filter(db, identity.user_id, project_id)
    rows = db.execute(
        select(Document).where(access_filter, Document.project_id == project_id)
    ).scalars().all()

    # Three-way classification instead of the old boolean can_view_document()
    # filter: blocked_by_sensitivity documents are now surfaced as redacted
    # locked stubs (spec §13.D) instead of being silently dropped — a viewer
    # can otherwise never discover a confidential document exists before
    # requesting access to it. not_visible documents are still dropped
    # entirely; no trace of those should ever reach the client.
    visibility = {
        d.document_id: classify_document_visibility(db, identity.user_id, d) for d in rows
    }
    visible = [d for d in rows if visibility[d.document_id] != DocumentVisibility.not_visible]

    wf_by_doc = {
        w.document_id: w.state.value
        for w in db.execute(
            select(WorkflowState).where(
                WorkflowState.document_id.in_([d.document_id for d in visible])
            )
        ).scalars()
    } if visible else {}

    can_see_all_drafts = identity.is_org_admin or (project_id in identity.project_admin_project_ids)
    if not (include_drafts and can_see_all_drafts):
        visible = [
            d for d in visible
            if wf_by_doc.get(d.document_id) != "draft" or d.uploaded_by == identity.user_id
        ]

    out = []
    for d in visible:
        locked = visibility[d.document_id] == DocumentVisibility.blocked_by_sensitivity
        out.append(DocumentListItem(
            document_id=str(d.document_id),
            original_filename=d.original_filename,
            project_id=str(d.project_id),
            stage_id=str(d.stage_id),
            sensitivity_level=d.sensitivity_level.name,
            uploaded_as_team_id=str(d.uploaded_as_team_id),
            # Locked stubs omit lifecycle/authorship detail — a user who
            # can't open the document shouldn't see its approval state or
            # who uploaded it either.
            workflow_state=None if locked else wf_by_doc.get(d.document_id),
            uploaded_by="" if locked else str(d.uploaded_by),
            locked=locked,
        ))
    return out
```

Note: a locked document must still pass the earlier draft-visibility filter using its REAL `workflow_state` (from `wf_by_doc`, computed before redaction) — only the OUTPUT is redacted, not the filtering decision. The code above computes `wf_by_doc` and the draft filter before redacting, which is correct — confirm this ordering is preserved exactly as written.

- [ ] **Step 2: Restart backend and verify**

Run: `python -m uvicorn app.main:app --port 8000` (kill+relaunch).

Manually verify: as a viewer/contributor with no grant, `GET /documents?project_id=<id>` for a project containing a confidential document on their team returns that document with `"locked": true`, `"workflow_state": null`, `"uploaded_by": ""` — not omitted from the array.

- [ ] **Step 3: Commit**

```bash
git add app/routers/documents.py
git commit -m "feat(access): surface blocked-by-sensitivity documents as locked stubs instead of dropping them"
```

---

### Task 14: Frontend — `api.js` additions

**Files:**
- Modify: `frontend/src/lib/api.js`

**Interfaces:**
- Consumes: `GET /access-requests/status` (Task 10), `POST /access-requests` with `document_id`/`stage_id` bodies (Task 6/11), `locked` field on documents (Task 13).
- Produces: `accessRequestsApi.createForDocument`, `.createForStage`, `.status`; `documentsApi`/`mapDocs`'s `locked` field — consumed by Tasks 15–19.

- [ ] **Step 1: Extend `mapDocs`**

Edit `frontend/src/lib/api.js`. Add `locked` to the object returned by `mapDocs` (line 133-147):

```javascript
function mapDocs(rows, project) {
  const stageName = (id) => project?.stages.find((s) => s.stage_id === id)?.name || 'Unspecified';
  const teamName = (id) => project?.teams?.find((t) => t.team_id === id)?.name || null;
  return rows.map((d) => ({
    document_id: d.document_id,
    filename: d.original_filename,
    doc_type: (d.original_filename || '').replace(/\.md$/i, '') || 'Document',
    stage: stageName(d.stage_id),
    stage_id: d.stage_id,
    sensitivity_level: SENSITIVITY_TO_INT[d.sensitivity_level] ?? 1,
    workflow_state: d.workflow_state,
    uploaded_by: d.uploaded_by,
    uploaded_as_team_id: d.uploaded_as_team_id,
    team_name: teamName(d.uploaded_as_team_id),
    project_id: project?.project_id || null,
    created_at: null,
    members: undefined,
    locked: Boolean(d.locked),
  }));
}
```

- [ ] **Step 2: Extend `accessRequestsApi`**

Edit `frontend/src/lib/api.js` (lines 480-489):

```javascript
export const accessRequestsApi = {
  mine: () => request('/access-requests/mine'),
  create: (teamId) => request('/access-requests', { method: 'POST', body: { team_id: teamId } }),
  createForDocument: (documentId) =>
    request('/access-requests', { method: 'POST', body: { document_id: documentId } }),
  createForStage: (stageId) =>
    request('/access-requests', { method: 'POST', body: { stage_id: stageId } }),
  // Effective-state lookup for one exact target — the server resolves
  // native/grant/pending/terminal precedence; the caller only renders the
  // returned verdict, never re-derives it.
  status: ({ documentId, stageId, teamId } = {}) => {
    const params = new URLSearchParams();
    if (documentId) params.set('document_id', documentId);
    if (stageId) params.set('stage_id', stageId);
    if (teamId) params.set('team_id', teamId);
    return request(`/access-requests/status?${params.toString()}`);
  },
  pending: () => request('/access-requests/pending'),
  approve: (id) => request(`/access-requests/${encodeURIComponent(id)}/approve`, { method: 'POST' }),
  deny: (id) => request(`/access-requests/${encodeURIComponent(id)}/deny`, { method: 'POST' }),
};
```

- [ ] **Step 3: Add the backend `CreateAccessRequest` model change this depends on**

This step is a check, not new code: Task 6's `request_confidential_access()` already accepts `document_id`/`stage_id` kwargs, but the router's `CreateAccessRequest` Pydantic model (line 52-53) still only declares `team_id: uuid.UUID` (required). Edit `app/routers/access_requests.py`:

```python
class CreateAccessRequest(BaseModel):
    team_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None
    stage_id: uuid.UUID | None = None
```

And update `create_access_request` (lines 91-107) to pass all three through:

```python
@router.post("", response_model=AccessRequestOut, status_code=201)
def create_access_request(
    body: CreateAccessRequest,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    try:
        req = request_confidential_access(
            db,
            user_id=identity.user_id,
            team_id=body.team_id,
            document_id=body.document_id,
            stage_id=body.stage_id,
            expected_tenant_id=identity.tenant_id,
        )
    except AccessRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except ValueError as exc:  # zero or multiple targets given
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return _serialize(db, req, requester=db.get(User, identity.user_id))
```

- [ ] **Step 4: Restart backend, rebuild frontend, verify**

Run: `python -m uvicorn app.main:app --port 8000` (kill+relaunch backend).
Run: `cd frontend && npx vite build --logLevel warn`
Expected: clean build, no errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/api.js app/routers/access_requests.py
git commit -m "feat(access): wire document/stage-scoped request creation and status lookup into the API client"
```

---

### Task 15: Frontend — `DocumentItem.jsx` locked-row rendering

**Files:**
- Modify: `frontend/src/components/sources/DocumentItem.jsx`

**Interfaces:**
- Consumes: `document.locked`, `document.uploaded_as_team_id` (Task 14); `accessRequestsApi.createForDocument`, `.status` (Task 14).
- Produces: a locked-row rendering path — no new exports; `SourcePanel`/`StageSection` pass `document` through unchanged (already do, per plan research).

- [ ] **Step 1: Add the locked-row early return**

Edit `frontend/src/components/sources/DocumentItem.jsx`. Add a small new component above `DocumentItem` (after the existing helper functions, before line 82):

```jsx
import { Lock } from 'lucide-react';
import { accessRequestsApi } from '../../lib/api';

const LockedDocumentRow = ({ document }) => {
  const [state, setState] = useState('loading'); // loading | none | pending | granted | denied | expired
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    accessRequestsApi.status({ documentId: document.document_id })
      .then((r) => { if (!cancelled) setState(r.status); })
      .catch(() => { if (!cancelled) setState('none'); });
    return () => { cancelled = true; };
  }, [document.document_id]);

  const handleRequest = async () => {
    setBusy(true);
    setError('');
    try {
      await accessRequestsApi.createForDocument(document.document_id);
      setState('pending');
    } catch (err) {
      setError(err.message || 'Could not send the request.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex items-start gap-3 p-3 rounded-lg bg-background/40 border border-border/40">
      <div className="mt-0.5"><Lock size={16} className="text-gray-500" /></div>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-gray-400 truncate">{document.filename}</p>
        <p className="text-xs text-gray-500 mt-1">{sensitivityLabel(document.sensitivity_level)} — access required</p>
        {error && <p className="text-xs text-red-400 mt-1">{error}</p>}
      </div>
      <div className="shrink-0">
        {state === 'loading' && <span className="text-xs text-gray-600">…</span>}
        {state === 'none' || state === 'denied' || state === 'expired' ? (
          <Button size="sm" variant="ghost" className="h-6 px-2 text-xs" loading={busy} onClick={handleRequest}>
            Request access
          </Button>
        ) : null}
        {state === 'pending' && <Badge variant="warning">Pending review</Badge>}
        {state === 'granted' && <Badge variant="success">Granted</Badge>}
      </div>
    </div>
  );
};
```

Insert the early return at the top of `DocumentItem`'s body, right after the existing hook declarations and before `return (` (before line 263):

```jsx
  if (document.locked) {
    return <LockedDocumentRow document={document} />;
  }

```

Note: this early return must come **after** all the component's `useState`/`useRef` hook calls (lines 86-99+), never before them — React's rules of hooks require every hook to run unconditionally on every render. Verify by reading the full hook block before inserting.

- [ ] **Step 2: Verify no import collisions**

`Lock` and `accessRequestsApi` must not already be imported under different names in this file — check via `grep -n "^import\|from '../../lib/api'" frontend/src/components/sources/DocumentItem.jsx` before adding; if `documentsApi` is already imported from the same path (it is, line 10), add `accessRequestsApi` to that same import statement rather than a new line.

- [ ] **Step 3: Manual browser verification**

Start the frontend dev server (`npm run dev` in `frontend/`), open a project as a viewer/contributor with a confidential document on their team and no grant, and confirm:
- The document renders as a dimmed row with a lock icon, filename, and sensitivity label — not omitted, not a full document row.
- Clicking "Request access" shows "Pending review" without a page reload.
- Console stays clean (no errors).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/sources/DocumentItem.jsx
git commit -m "feat(access): render locked documents as a distinct row with a request-access action"
```

---

### Task 16: Frontend — remove the static banner, add stage-scope trigger

**Files:**
- Modify: `frontend/src/components/sources/SourcePanel.jsx`

**Interfaces:**
- Consumes: nothing new from other tasks — this is UI-only cleanup plus wiring an existing prop (`onRequestConfidentialAccess`, already passed from `ProjectWorkspace.jsx`) to a per-stage context instead of a single global banner.

- [ ] **Step 1: Remove the static banner**

Edit `frontend/src/components/sources/SourcePanel.jsx`. Delete the block at lines 361-370:

```jsx
        {hasRequestableTeams && (
          <button
            type="button"
            onClick={onRequestConfidentialAccess}
            className="mt-2 flex items-center gap-1.5 text-xs text-primary-light hover:text-primary transition-colors"
          >
            <Lock size={12} />
            Some documents may be confidential — request access
          </button>
        )}
```

This banner's job (compensating for confidential documents being entirely invisible) is superseded by Task 15's locked rows, which are now visible and carry their own grounded, per-document action.

- [ ] **Step 2: Add a stage-level "Request stage access" affordance**

Each `StageSection` needs a way to trigger a stage-scope request when the stage contains at least one locked document — surfaced as a small link under the stage's document list rather than a banner. Edit the `StageSection` call sites in `SourcePanel.jsx` (lines 380-395) to pass a new prop:

```jsx
            {orderedStages.map((stage) => (
              <StageSection
                key={stage.stage_id}
                stage={stage.name}
                stageId={stage.stage_id}
                documents={groupedDocs[stage.name] || []}
                canReview={canReview}
                canOverrideScan={canOverrideScan}
                canDelete={canDelete}
                onChanged={onChanged}
                requiresApproval={stage.requires_approval}
                onEdit={canManageStages ? () => openSettings(stage) : null}
                highlightDocumentId={highlightDocumentId}
                canEditAny={canEditAny}
                currentUserId={currentUserId}
              />
            ))}
```

(`stageId` is the only new prop — `StageSection` itself is modified in Step 3 to use it.)

- [ ] **Step 3: Render the stage-escalation link inside `StageSection`**

Edit `frontend/src/components/sources/StageSection.jsx`. Add the new prop to the destructured signature (line 8-22):

```jsx
const StageSection = ({
  stage,
  stageId = null,
  documents = [],
  canReview,
  canOverrideScan = false,
  canDelete,
  onChanged,
  requiresApproval = false,
  onEdit = null,
  menu = null,
  defaultExpanded = true,
  highlightDocumentId = null,
  canEditAny = false,
  currentUserId = null,
}) => {
```

After the documents list rendering (after line 99, the closing `)}` of the ternary), add — only when at least one document in this stage is locked:

```jsx
            {documents.some((d) => d.locked) && stageId && (
              <StageAccessLink stageId={stageId} />
            )}
```

Add a small new component at the top of the same file (after the imports, before `StageSection`):

```jsx
import { useState, useEffect } from 'react';
import { accessRequestsApi } from '../../lib/api';

const StageAccessLink = ({ stageId }) => {
  const [state, setState] = useState('loading');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    accessRequestsApi.status({ stageId })
      .then((r) => { if (!cancelled) setState(r.status); })
      .catch(() => { if (!cancelled) setState('none'); });
    return () => { cancelled = true; };
  }, [stageId]);

  if (state === 'loading' || state === 'granted') return null;

  const handleClick = async () => {
    setBusy(true);
    try {
      await accessRequestsApi.createForStage(stageId);
      setState('pending');
    } finally {
      setBusy(false);
    }
  };

  if (state === 'pending') {
    return <p className="text-xs text-gray-500 py-1 px-1">Stage access requested — pending review.</p>;
  }

  return (
    <button
      type="button"
      onClick={handleClick}
      disabled={busy}
      className="text-xs text-primary-light hover:text-primary transition-colors py-1 px-1"
    >
      Need access to more documents in this stage? Request stage access.
    </button>
  );
};
```

(Note: `React` and other hooks are already imported at the top of `StageSection.jsx` per line 1 — merge `useState`/`useEffect` into that existing import rather than duplicating it.)

- [ ] **Step 4: Manual browser verification**

With at least one locked document in a stage, confirm the "Need access to more documents in this stage?" link appears below that stage's document list (not above, not conflated with the document-level "Request access" button), and clicking it creates a stage-scope request distinct from any document-scope request already made.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/sources/SourcePanel.jsx frontend/src/components/sources/StageSection.jsx
git commit -m "feat(access): remove the static confidential-access banner, add a stage-scope request link"
```

---

### Task 17: Frontend — team-scope modal reframing

**Files:**
- Modify: `frontend/src/pages/ProjectWorkspace.jsx`

**Interfaces:**
- Consumes: nothing new — this is copy/framing only, per spec §13.A's "Team access — request access to confidential documents shared with this team" wording. The existing `hasRequestableTeams`/`onRequestConfidentialAccess` wiring (previously fed by `SourcePanel`'s now-removed banner) needs a new trigger point since that banner is gone.

- [ ] **Step 1: Add an explicit "Team access" entry point**

The banner removed in Task 16 was the only way to open this modal. Add a small, explicit button near the Teams toolbar chip instead. Edit `frontend/src/pages/ProjectWorkspace.jsx`, in the header row (after the Stages chip, around line 268, before the `<div className="flex-1" />`):

```jsx
        {requestableTeams.length > 0 && (
          <button
            type="button"
            onClick={() => { setReqNotice(''); setReqError(''); setAccessOpen(true); }}
            title="Request access to confidential documents shared with a team"
            className="inline-flex items-center gap-1.5 rounded-full border border-border bg-surface px-2.5 py-1 text-xs font-medium text-gray-300 hover:border-primary/50 hover:text-gray-100 transition-colors shrink-0"
          >
            Team access
          </button>
        )}
```

- [ ] **Step 2: Update the modal's copy**

Edit the modal (lines 366-402). Update the title/description to frame this explicitly as the broadest tier:

```jsx
      <Modal
        open={accessOpen}
        onClose={() => setAccessOpen(false)}
        title="Team access"
        description="Request access to every confidential document shared with a team — the broadest option. For a single document or one stage, use the Request access action on that document or stage instead."
      >
```

- [ ] **Step 3: Remove the now-unused `SourcePanel` banner props**

`hasRequestableTeams`/`onRequestConfidentialAccess` are no longer consumed by `SourcePanel` (the banner using them was removed in Task 16). Remove both props from the `<SourcePanel ... />` call (lines 292-293) and from `SourcePanel`'s own destructured signature (`app/components/sources/SourcePanel.jsx` lines 25-26) and its `PropTypes`/JSDoc if any exist. Verify via `grep -n "hasRequestableTeams\|onRequestConfidentialAccess" frontend/src/components/sources/SourcePanel.jsx` that no other reference remains in that file after removal.

- [ ] **Step 4: Manual browser verification**

Confirm the "Team access" chip appears in the header (only for a user with at least one requestable team), opens the modal with the new copy, and the existing per-team request/status list inside it still works unchanged.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/ProjectWorkspace.jsx frontend/src/components/sources/SourcePanel.jsx
git commit -m "feat(access): reframe the team-scope request modal as the broadest explicit tier"
```

---

### Task 18: Frontend — `AdminPage.jsx` scope-aware Pending Approvals copy

**Files:**
- Modify: `frontend/src/pages/AdminPage.jsx`

**Interfaces:**
- Consumes: `scope`, `target_name`, `grant_duration_label` on `AccessRequestOut` (Task 11).

- [ ] **Step 1: Replace the flat requester-only row copy**

Edit `frontend/src/pages/AdminPage.jsx`. Replace lines 1038-1057 (the access-request row rendering inside `groupByTeam`'s `team.requests.map`):

```jsx
                            {team.requests.map((r) => (
                              <div key={r.request_id} className="flex items-center justify-between gap-3 rounded-md border border-border bg-surface px-3 py-2">
                                <div className="min-w-0">
                                  <p className="text-sm text-gray-200 truncate">
                                    {r.requester_email || r.user_id}{' '}
                                    {r.scope === 'document' && <>requests access to <span className="font-medium">{r.target_name}</span></>}
                                    {r.scope === 'stage' && <>requests access to the <span className="font-medium">{r.target_name}</span> stage</>}
                                    {r.scope === 'team' && <>requests team-wide access</>}
                                  </p>
                                  <p className="text-xs text-gray-500">
                                    Requested {r.requested_at ? new Date(r.requested_at).toLocaleDateString() : '—'}
                                    {' · '}grants {r.grant_duration_label} if approved
                                  </p>
                                </div>
                                <div className="flex gap-2 shrink-0">
                                  <Button size="sm" icon={Check}
                                    loading={busyKey === `req-approve-${r.request_id}`}
                                    onClick={() => act(`req-approve-${r.request_id}`, () => accessRequestsApi.approve(r.request_id), 'Access request approved.')}
                                  >Approve</Button>
                                  <Button size="sm" variant="danger" icon={X}
                                    loading={busyKey === `req-deny-${r.request_id}`}
                                    onClick={() => act(`req-deny-${r.request_id}`, () => accessRequestsApi.deny(r.request_id), 'Access request denied.')}
                                  >Deny</Button>
                                </div>
                              </div>
                            ))}
```

- [ ] **Step 2: Manual browser verification**

As a team_lead, create one document-scope, one stage-scope, and one team-scope request (from three different accounts), then confirm the Pending Approvals tab shows all three with distinct, correct copy and the right duration label (72 hours for document, 90 days for stage/team) before approving.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/AdminPage.jsx
git commit -m "feat(access): show scope-specific copy and grant duration in Pending Approvals"
```

---

### Task 19: Frontend — "My Access Requests" panel

**Files:**
- Create: `frontend/src/components/access/MyAccessRequestsPanel.jsx`
- Modify: `frontend/src/pages/ProjectWorkspace.jsx` (mount point)

**Interfaces:**
- Consumes: `accessRequestsApi.mine()` (existing, enriched by Task 11).
- Produces: `<MyAccessRequestsPanel />` — a self-contained component taking no required props (fetches its own data).

- [ ] **Step 1: Write the component**

Create `frontend/src/components/access/MyAccessRequestsPanel.jsx`:

```jsx
import React, { useEffect, useState } from 'react';
import { Clock } from 'lucide-react';
import Modal from '../ui/Modal';
import Badge from '../ui/Badge';
import { accessRequestsApi } from '../../lib/api';

// expires_at is server-authoritative; this is a presentation-only countdown
// computed at render time — never stored, never trusted as state.
function formatCountdown(expiresAtIso) {
  if (!expiresAtIso) return null;
  const diffMs = new Date(expiresAtIso).getTime() - Date.now();
  if (diffMs <= 0) return 'expired';
  const hours = Math.round(diffMs / (1000 * 60 * 60));
  if (hours < 48) return `expires in ${hours}h`;
  return `expires in ${Math.round(hours / 24)}d`;
}

const statusBadge = {
  pending: { variant: 'warning', label: 'Pending review' },
  approved: { variant: 'success', label: 'Granted' },
  denied: { variant: 'danger', label: 'Denied' },
};

const MyAccessRequestsPanel = ({ open, onClose }) => {
  const [requests, setRequests] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    accessRequestsApi.mine()
      .then((rows) => { if (!cancelled) setRequests(Array.isArray(rows) ? rows : []); })
      .catch((err) => { if (!cancelled) setError(err.message || 'Could not load your requests.'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [open]);

  return (
    <Modal open={open} onClose={onClose} title="My Access Requests" description="Every confidential-access request you've made, across every scope.">
      {loading && <p className="text-sm text-gray-500">Loading…</p>}
      {error && <p className="text-sm text-red-400">{error}</p>}
      {!loading && requests.length === 0 && (
        <p className="text-sm text-gray-500">You haven't requested confidential access to anything yet.</p>
      )}
      <div className="space-y-2">
        {requests.map((r) => {
          const badge = statusBadge[r.status] || { variant: 'neutral', label: r.status };
          const isExpiredApproved = r.status === 'approved' && !r.active;
          return (
            <div key={r.request_id} className="flex items-center justify-between gap-3 rounded-lg border border-border bg-background px-3 py-2.5">
              <div className="min-w-0">
                <p className="text-sm text-gray-200 truncate">{r.target_name}</p>
                <p className="text-xs text-gray-500 capitalize">{r.scope} access · {r.team_name}</p>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                {isExpiredApproved ? (
                  <Badge variant="neutral">Expired</Badge>
                ) : (
                  <Badge variant={badge.variant}>{badge.label}</Badge>
                )}
                {r.active && r.expires_at && (
                  <span className="text-xs text-gray-500 flex items-center gap-1">
                    <Clock size={11} /> {formatCountdown(r.expires_at)}
                  </span>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </Modal>
  );
};

export default MyAccessRequestsPanel;
```

- [ ] **Step 2: Mount it from `ProjectWorkspace.jsx`**

Edit `frontend/src/pages/ProjectWorkspace.jsx`. Add the import (with the other component imports, near line 9):

```jsx
import MyAccessRequestsPanel from '../components/access/MyAccessRequestsPanel';
```

Add state (near the other access-request state, after line 55):

```jsx
  const [myRequestsPanelOpen, setMyRequestsPanelOpen] = useState(false);
```

Add a trigger button next to the "Team access" chip added in Task 17 (in the same header row):

```jsx
        <button
          type="button"
          onClick={() => setMyRequestsPanelOpen(true)}
          title="My access requests"
          className="inline-flex items-center gap-1.5 rounded-full border border-border bg-surface px-2.5 py-1 text-xs font-medium text-gray-300 hover:border-primary/50 hover:text-gray-100 transition-colors shrink-0"
        >
          My requests
        </button>
```

Mount the panel near the other modals (after the "Request confidential access" `<Modal>` closing tag, before the outer component's closing `</div>`):

```jsx
      <MyAccessRequestsPanel open={myRequestsPanelOpen} onClose={() => setMyRequestsPanelOpen(false)} />
```

This button is intentionally always visible (unlike "Team access", which is gated on `requestableTeams.length > 0`) — even a team_lead/admin with no requestable teams of their own might still want to check status on a request made before their role changed, and the panel itself shows "you haven't requested anything yet" cleanly for the common empty case.

- [ ] **Step 3: Manual browser verification**

Create a document-scope request, open "My requests," confirm it shows with "Pending review" and no countdown. Have it approved by a team_lead, reopen "My requests," confirm it now shows "Granted" with an "expires in ~72h" countdown.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/access/MyAccessRequestsPanel.jsx frontend/src/pages/ProjectWorkspace.jsx
git commit -m "feat(access): add the My Access Requests status panel"
```

---

### Task 20: Frontend — notification click-through routing

**Files:**
- Modify: `frontend/src/components/layout/TopNav.jsx`
- Modify: `frontend/src/pages/AdminPage.jsx` (read `?tab=` from the URL)

**Interfaces:**
- Consumes: `notification.audience` (Task 12).

- [ ] **Step 1: Make `AdminPage`'s tab state read the URL**

Edit `frontend/src/pages/AdminPage.jsx`. The `tab` state (line 96, `useState('users')`) currently ignores any `?tab=` URL parameter. Change it to read from the existing `searchParams` (already destructured at line 72-73):

```jsx
  const [tab, setTab] = useState(searchParams.get('tab') || 'users');
```

- [ ] **Step 2: Add click-through to notification rows**

Edit `frontend/src/components/layout/TopNav.jsx`. Wrap the notification title in a clickable element when `resource_type === 'access_request'`, routing by `audience`. Replace the notification row block (lines 242-266):

```jsx
                ) : notifications.map((notification) => {
                  const clickable = notification.resource_type === 'access_request' && notification.audience;
                  const handleRowClick = () => {
                    if (!clickable) return;
                    setNotificationOpen(false);
                    if (notification.audience === 'approver') {
                      navigate('/admin?tab=approvals');
                    } else {
                      navigate(`/projects/${notification.project_id}?myRequests=1`);
                    }
                  };
                  return (
                    <div
                      key={notification.notification_id}
                      onClick={handleRowClick}
                      className={`px-4 py-3 border-b border-border/30 last:border-0 ${notification.read ? '' : 'bg-primary/5'} ${clickable ? 'cursor-pointer hover:bg-surface-hover' : ''}`}
                    >
                      <div className="flex items-start justify-between gap-2">
                        <span className={`text-xs font-semibold ${notification.read ? 'text-gray-300' : 'text-primary-light'}`}>
                          {notification.title}
                        </span>
                        <div className="flex items-center gap-2 shrink-0">
                          <span className="text-[10px] text-gray-600">{new Date(notification.created_at).toLocaleString()}</span>
                          <button
                            type="button"
                            onClick={(e) => { e.stopPropagation(); dismissNotification(notification.notification_id); }}
                            className="text-gray-500 hover:text-gray-200 transition-colors"
                            title="Clear notification"
                            aria-label="Clear notification"
                          >
                            <X size={13} />
                          </button>
                        </div>
                      </div>
                      {notification.body && (
                        <p className="text-xs text-gray-400 mt-1">{notification.body}</p>
                      )}
                    </div>
                  );
                })}
```

Note the dismiss button's `onClick` now calls `e.stopPropagation()` — without it, clicking "dismiss" would also trigger the new row-level navigation, which is wrong (dismissing should never navigate).

`useNavigate` is already imported and instantiated in this file (`const navigate = useNavigate();`, confirmed at line 9) — no new import needed.

- [ ] **Step 2: Wire `ProjectWorkspace.jsx` to auto-open "My requests" from the URL**

The requester-facing routing target above uses `?myRequests=1`. Edit `frontend/src/pages/ProjectWorkspace.jsx` to honor it — add near the other `useSearchParams`-derived reads (after line 19):

```jsx
  const shouldOpenMyRequests = searchParams.get('myRequests') === '1';
```

Update the `myRequestsPanelOpen` state initializer from Task 19:

```jsx
  const [myRequestsPanelOpen, setMyRequestsPanelOpen] = useState(shouldOpenMyRequests);
```

- [ ] **Step 3: Manual browser verification**

As a team_lead, trigger a real access request from another account, confirm the resulting notification is clickable and navigates to `/admin?tab=approvals` with the Approvals tab already selected. As the requester, once approved, confirm their decision notification navigates to the project workspace with "My requests" already open. Confirm dismissing a notification never navigates.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/layout/TopNav.jsx frontend/src/pages/AdminPage.jsx frontend/src/pages/ProjectWorkspace.jsx
git commit -m "feat(access): route access-request notifications to the right screen deterministically"
```

---

## Post-implementation verification (run once all 20 tasks are complete)

- [ ] Run the full backend test suite: `python -m pytest tests/ -v` — expect zero failures, zero new errors relative to the pre-plan baseline.
- [ ] Run `python -m alembic heads` — expect exactly one head, confirming both new migrations chain cleanly.
- [ ] Run `cd frontend && npx vite build --logLevel warn` — expect a clean build.
- [ ] Live walkthrough (per this session's established E2E-testing convention, via Chrome DevTools MCP): as a contributor, discover a locked document, request document-scope access, get denied by the team_lead, request again, get approved, confirm the document becomes a full row on next reload, confirm the grant expires as read-only (attempt to edit/submit/delete and confirm each is rejected with a clear message), confirm the grant does not survive past its 72-hour TTL (verify via direct DB timestamp manipulation in a throwaway script, not by waiting 72 real hours).
- [ ] Confirm `app/tools/rag_tools.py`'s `request_confidential_access` tool still works unchanged (team-scope only, per its own docstring) — this plan deliberately does not extend the conversational agent tool to document/stage scope.
