# Tiered Stage Access Grants Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fixed, always-read-only confidential-access grant with an approver-chosen tier (`viewer` / `contributor` / `contributor_confidential`) and approver-chosen duration (72 hours / 1 week / 1 month / no expiration) for stage-scope grants, without mutating `UserTeamMembership` or `has_permission()`.

**Architecture:** A new attribute-based resolver, `get_active_stage_grants_for_user()`, is the single source every consumer reads from — stage visibility, document-team-visibility override, confidential unlock, and mutation (upload/submit) all branch off it. `has_permission()` stays role-only and untouched; every mutation check becomes `native_permission OR (active_grant AND grant.team_id == acting_team_id)`, never a bare grant check alone.

**Tech Stack:** FastAPI + SQLAlchemy + Alembic (Postgres), Python 3.12 (use `C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe`, NOT the system `python`, which is 3.14 and lacks pytest); React frontend (Vite).

**Spec:** `docs/superpowers/specs/2026-09-17-tiered-stage-access-grants-design.md` — read this in full before starting; every task below argues from a specific section of it. This plan also extends the already-shipped `docs/superpowers/plans/2026-09-17-confidential-access-grants.md` (Tasks 1-19 complete, ledger at `.superpowers/sdd/2026-09-17-confidential-access-grants/progress.md`).

## Global Constraints

- No `Co-Authored-By: Claude` trailer on any commit — the user's explicit standing instruction for this repo (git author is already correctly the user; just omit the trailer line).
- Working directory: `D:\Ra\DocFlowAI\meem_salvage`. Branch: `docflow-complete`. Nothing gets pushed to origin without asking again.
- `alembic heads` will show **two** heads at all times during this plan — `d0e1f2a3b4c5` (the real, git-tracked tip this plan builds on) and `e2f3a4b5c6d7` (an untracked, uncommitted stray file that predates this plan entirely, confirmed via `git status --porcelain` showing `??` for it and its `down_revision` pointing at an unrelated, much earlier revision `e1f2a3b4c5d6`). **Do not touch, merge, or delete that file** — it isn't yours to resolve; verification steps below check `alembic heads` for exactly the ONE new head this plan's own migration adds on top of `d0e1f2a3b4c5`, not for a global single-head state.
- `tests/test_access_requests_scoped.py` currently has 3 pre-existing failures (`test_document_scope_derives_team_id_from_document`, `test_duplicate_document_request_is_rejected`, `test_team_scope_call_shape_unchanged_for_existing_caller`) — confirmed via a direct run before this plan started. These fail because of the existing (unrelated, already-shipped) policy gate in `request_confidential_access()` that rejects document/team-scope requests outright (`app/services/access_requests_service.py:171-175`) — not something this plan changes or fixes. Every task's regression check below runs `test_access_requests_scoped.py` and expects exactly these same 3 failures, no more, no fewer — a NEW failure in this file is this plan's problem; these 3 are not.
- Every DB-backed test in this repo follows the same fixture shape: real Postgres (no mocking), `SessionLocal()` with `self.db.info["tenant_id"] = str(tenant_id)` set explicitly, and teardown that never hard-deletes a user/row that `record_audit()` may have referenced (append-only `audit_log` FK trigger forbids it) — leave such rows in place as harmless orphans rather than fighting the trigger.
- SQLAlchemy flush-ordering: any code that sets `document.current_version_id = version_id` as a bare scalar assignment needs an explicit `db.flush()` between adding the version and committing — this repo's circular FK between `documents` and `document_versions` gives the ORM no automatic ordering otherwise. This has bitten every task in the original plan that touched fixtures this way; expect it here too if a task's test creates a document+version pair.

---

### Task 1: Migration + model — `GrantTier`/`GrantDuration` enums, `revoked` status, new `AccessRequest` columns

**Files:**
- Modify: `app/models/team.py:87-144` (`AccessRequestStatus`, new enums, `AccessRequest` class)
- Create: `alembic/versions/f9a8b7c6d5e4_tiered_stage_access_grants.py`
- Test: `tests/test_tiered_grant_migration.py`

**Interfaces:**
- Produces: `GrantTier` (str enum: `viewer`, `contributor`, `contributor_confidential`), `GrantDuration` (str enum: `hours_72`, `week_1`, `month_1`, `unlimited`), both in `app.models.team`. `AccessRequestStatus.revoked`. `AccessRequest.tier: GrantTier | None`, `AccessRequest.duration: GrantDuration | None`, `AccessRequest.revoked_at: datetime | None`, `AccessRequest.revoked_by: UUID | None`. These are consumed by every later task in this plan.

Current `app/models/team.py:87-97`:
```python
class AccessRequestStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    denied = "denied"


class AccessRequestScope(str, enum.Enum):
    document = "document"
    stage = "stage"
    team = "team"
```

- [ ] **Step 1: Confirm the pre-existing two-alembic-heads state, so it isn't mistaken for something this task broke**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m alembic heads`
Expected output (order may vary): `d0e1f2a3b4c5 (head)` and `e2f3a4b5c6d7 (head)` — exactly these two, matching the Global Constraints note above. If you see a different second head, stop and ask — something else has changed.

- [ ] **Step 2: Write the model changes**

Replace `app/models/team.py:87-97` with:

```python
class AccessRequestStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    denied = "denied"
    revoked = "revoked"


class AccessRequestScope(str, enum.Enum):
    document = "document"
    stage = "stage"
    team = "team"


class GrantTier(str, enum.Enum):
    """
    Approver-chosen capability tier for a STAGE-scope grant only.
    viewer: see/search public+internal docs in the granted stage.
    contributor: viewer + create/upload/submit in the granted stage,
      attributed to the grant's own routing team (AccessRequest.team_id).
    contributor_confidential: contributor + confidential docs in the
      granted stage. Document/team-scope grants never set this — it
      stays None for those, and they remain always-read-only as before.
    """
    viewer = "viewer"
    contributor = "contributor"
    contributor_confidential = "contributor_confidential"


class GrantDuration(str, enum.Enum):
    """
    Approver-chosen duration label for a STAGE-scope grant, stored
    verbatim (not re-derived from expires_at - decided_at) so the UI can
    display it exactly. hours_72/week_1/month_1 are flat offsets (30 days
    for month_1, not calendar-month arithmetic); unlimited means
    expires_at stays None.
    """
    hours_72 = "hours_72"
    week_1 = "week_1"
    month_1 = "month_1"
    unlimited = "unlimited"
```

Then add four new columns to the `AccessRequest` class, immediately after the existing `expires_at` column (currently the last column, `app/models/team.py:142-144`):

```python
    tier: Mapped["GrantTier | None"] = mapped_column(
        Enum(GrantTier, name="grant_tier"), nullable=True
    )
    duration: Mapped["GrantDuration | None"] = mapped_column(
        Enum(GrantDuration, name="grant_duration"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=True
    )
```

- [ ] **Step 3: Write the migration**

Create `alembic/versions/f9a8b7c6d5e4_tiered_stage_access_grants.py`:

```python
"""tiered stage access grants

Revision ID: f9a8b7c6d5e4
Revises: d0e1f2a3b4c5
Create Date: 2026-09-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f9a8b7c6d5e4"
down_revision: Union[str, None] = "d0e1f2a3b4c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # New enum types must exist before add_column can reference them —
    # op.add_column() with sa.Enum(...) does not auto-create the backing
    # Postgres type (same lesson as the c9d0e1f2a3b4 migration).
    op.execute("CREATE TYPE grant_tier AS ENUM ('viewer', 'contributor', 'contributor_confidential')")
    op.execute("CREATE TYPE grant_duration AS ENUM ('hours_72', 'week_1', 'month_1', 'unlimited')")

    # ALTER TYPE ... ADD VALUE must run as its own statement, not batched
    # with anything that uses the new value in the same implicit
    # transaction (Postgres restriction).
    op.execute("ALTER TYPE access_request_status ADD VALUE 'revoked'")

    op.add_column(
        "access_requests",
        sa.Column("tier", sa.Enum("viewer", "contributor", "contributor_confidential", name="grant_tier"), nullable=True),
    )
    op.add_column(
        "access_requests",
        sa.Column("duration", sa.Enum("hours_72", "week_1", "month_1", "unlimited", name="grant_duration"), nullable=True),
    )
    op.add_column("access_requests", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("access_requests", sa.Column("revoked_by", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_access_requests_revoked_by", "access_requests", "users",
        ["revoked_by"], ["user_id"],
    )

    # Concurrency invariant (spec §2.3): at most one pending stage request
    # per (user, stage) — enforced as a real DB constraint, not just an
    # application-level check-then-insert.
    op.execute(
        "CREATE UNIQUE INDEX uq_access_requests_one_pending_stage_request "
        "ON access_requests (user_id, stage_id) "
        "WHERE scope = 'stage' AND status = 'pending'"
    )

    # Supporting index for the new hot resolver query (spec §2.4).
    op.execute(
        "CREATE INDEX ix_access_requests_stage_grant_lookup "
        "ON access_requests (user_id, stage_id, status, decided_at) "
        "WHERE scope = 'stage'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_access_requests_stage_grant_lookup")
    op.execute("DROP INDEX IF EXISTS uq_access_requests_one_pending_stage_request")
    op.drop_constraint("fk_access_requests_revoked_by", "access_requests", type_="foreignkey")
    op.drop_column("access_requests", "revoked_by")
    op.drop_column("access_requests", "revoked_at")
    op.drop_column("access_requests", "duration")
    op.drop_column("access_requests", "tier")
    op.execute("DROP TYPE IF EXISTS grant_duration")
    op.execute("DROP TYPE IF EXISTS grant_tier")
    # NOTE: Postgres has no `ALTER TYPE ... DROP VALUE` — removing 'revoked'
    # from access_request_status would require recreating the whole enum
    # type and every column/index that depends on it. Deliberately left
    # as a no-op here; this is a documented, permanent limitation of this
    # downgrade, not an oversight.
```

- [ ] **Step 4: Apply the migration**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m alembic upgrade f9a8b7c6d5e4`
(Not `alembic upgrade head` — with two pre-existing heads, per Global Constraints, the bare `head` keyword is ambiguous and alembic refuses it outright with "Multiple head revisions are present." Target this migration's own revision ID explicitly.)
Expected: applies cleanly, no error. (If it errors on the `ALTER TYPE ... ADD VALUE` line being combined with later statements in one transaction, split the migration into two separate `op.execute()` calls further apart, or consult Alembic's `transactional_ddl` setting for this project — check `alembic/env.py` first for how transactions are configured before changing anything.)

- [ ] **Step 5: Confirm the new head and schema**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m alembic heads`
Expected: `f9a8b7c6d5e4 (head)` and `e2f3a4b5c6d7 (head)` — the pre-existing stray head is still there (untouched, expected), and `f9a8b7c6d5e4` has replaced `d0e1f2a3b4c5` as the tip of the real chain.

- [ ] **Step 6: Write the schema-verification test**

Create `tests/test_tiered_grant_migration.py`:

```python
import unittest

from sqlalchemy import inspect, text

from app.database import SessionLocal
from app.models.team import AccessRequestStatus, GrantDuration, GrantTier


class TestTieredGrantMigration(unittest.TestCase):
    def setUp(self):
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()

    def test_new_enum_values_exist(self):
        self.assertEqual(
            {e.value for e in GrantTier}, {"viewer", "contributor", "contributor_confidential"}
        )
        self.assertEqual(
            {e.value for e in GrantDuration}, {"hours_72", "week_1", "month_1", "unlimited"}
        )
        self.assertIn("revoked", {e.value for e in AccessRequestStatus})

    def test_access_requests_has_new_columns(self):
        inspector = inspect(self.db.get_bind())
        columns = {c["name"] for c in inspector.get_columns("access_requests")}
        self.assertIn("tier", columns)
        self.assertIn("duration", columns)
        self.assertIn("revoked_at", columns)
        self.assertIn("revoked_by", columns)

    def test_partial_unique_index_exists(self):
        rows = self.db.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'access_requests'")
        ).fetchall()
        names = {r[0] for r in rows}
        self.assertIn("uq_access_requests_one_pending_stage_request", names)
        self.assertIn("ix_access_requests_stage_grant_lookup", names)
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_tiered_grant_migration.py -v`
Expected: 3 passed.

- [ ] **Step 8: Confirm no existing test broke**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_access_control_resolver.py tests/test_access_requests_scoped.py -q`
Expected: `test_access_control_resolver.py` fully passes (18 passed); `test_access_requests_scoped.py` shows exactly the 3 pre-existing failures named in Global Constraints, no new ones — a plain nullable-column migration shouldn't change either file's behavior yet.

- [ ] **Step 9: Commit**

```bash
git add app/models/team.py alembic/versions/f9a8b7c6d5e4_tiered_stage_access_grants.py tests/test_tiered_grant_migration.py
git commit -m "feat(access): add tier/duration columns and revoked status for stage grants"
```

---

### Task 2: `get_active_stage_grants_for_user()` / `resolve_stage_grant()` — the ABAC resolver

**Files:**
- Modify: `app/services/access_control.py:32` (imports), append new code after `get_accessible_stages_for_user()` (currently ends `app/services/access_control.py:223`)
- Test: `tests/test_stage_grant_resolver.py`

**Interfaces:**
- Consumes: `AccessRequest`, `AccessRequestScope`, `AccessRequestStatus`, `GrantTier` (Task 1), `Stage` (already imported).
- Produces: `StageGrant` dataclass (`tier: GrantTier`, `team_id: UUID`, `expires_at: datetime | None`, `request_id: UUID`); `get_active_stage_grants_for_user(db: Session, user_id: UUID) -> dict[UUID, StageGrant]`; `resolve_stage_grant(db: Session, user_id: UUID, stage_id: UUID) -> StageGrant | None`. Every later task in this plan calls one of these two functions — no task re-derives this resolution logic independently.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_stage_grant_resolver.py`:

```python
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models.project import Project
from app.models.stage import Stage
from app.models.team import (
    AccessRequest, AccessRequestScope, AccessRequestStatus, GrantTier,
    Team, TeamRole, UserTeamMembership,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.services.access_control import get_active_stage_grants_for_user, resolve_stage_grant


class TestStageGrantResolver(unittest.TestCase):
    def setUp(self):
        with SessionLocal() as lookup_db:
            self.tenant = lookup_db.query(Tenant).first()
            tenant_id = self.tenant.tenant_id

        self.db = SessionLocal()
        self.db.info["tenant_id"] = str(tenant_id)

        self.project = Project(tenant_id=tenant_id, name=f"StageGrant Test {uuid.uuid4().hex[:8]}")
        self.db.add(self.project)
        self.db.commit()

        self.team = Team(project_id=self.project.project_id, name="Grant Team")
        self.db.add(self.team)
        self.db.commit()

        self.stage = Stage(project_id=self.project.project_id, name="Grant Stage", order_index=1)
        self.db.add(self.stage)
        self.db.commit()

        self.viewer = User(email=f"v-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.db.add(self.viewer)
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        self.db.query(AccessRequest).filter(AccessRequest.team_id == self.team.team_id).delete()
        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id == self.project.project_id).delete()
        self.db.query(User).filter(User.user_id == self.viewer.user_id).delete()
        self.db.query(Stage).filter(Stage.project_id == self.project.project_id).delete()
        self.db.query(Team).filter(Team.project_id == self.project.project_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def _grant(self, *, tier, status=AccessRequestStatus.approved, decided_at=None, revoked_at=None, expires_at=None, request_id=None):
        return AccessRequest(
            request_id=request_id or uuid.uuid4(),
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            tier=tier, status=status, decided_at=decided_at, revoked_at=revoked_at,
            expires_at=expires_at,
        )

    def test_no_grant_returns_none(self):
        self.assertEqual(get_active_stage_grants_for_user(self.db, self.viewer.user_id), {})
        self.assertIsNone(resolve_stage_grant(self.db, self.viewer.user_id, self.stage.stage_id))

    def test_approved_unexpired_grant_is_returned(self):
        now = datetime.now(timezone.utc)
        self.db.add(self._grant(tier=GrantTier.viewer, decided_at=now, expires_at=now + timedelta(days=7)))
        self.db.commit()
        grant = resolve_stage_grant(self.db, self.viewer.user_id, self.stage.stage_id)
        self.assertIsNotNone(grant)
        self.assertEqual(grant.tier, GrantTier.viewer)
        self.assertEqual(grant.team_id, self.team.team_id)

    def test_approved_expired_grant_is_not_returned(self):
        now = datetime.now(timezone.utc)
        self.db.add(self._grant(tier=GrantTier.viewer, decided_at=now - timedelta(days=10), expires_at=now - timedelta(days=1)))
        self.db.commit()
        self.assertIsNone(resolve_stage_grant(self.db, self.viewer.user_id, self.stage.stage_id))

    def test_latest_decided_at_wins_not_latest_requested_at(self):
        now = datetime.now(timezone.utc)
        older_decision_newer_request = self._grant(
            tier=GrantTier.viewer, decided_at=now - timedelta(days=5), expires_at=now + timedelta(days=90),
        )
        newer_decision_older_request = self._grant(
            tier=GrantTier.contributor, decided_at=now - timedelta(days=1), expires_at=now + timedelta(days=90),
        )
        self.db.add(older_decision_newer_request)
        self.db.commit()
        self.db.add(newer_decision_older_request)
        self.db.commit()
        grant = resolve_stage_grant(self.db, self.viewer.user_id, self.stage.stage_id)
        self.assertEqual(grant.tier, GrantTier.contributor)

    def test_revoke_does_not_reactivate_an_older_approved_grant(self):
        now = datetime.now(timezone.utc)
        january_viewer = self._grant(tier=GrantTier.viewer, decided_at=now - timedelta(days=60), expires_at=None)
        february_contributor = self._grant(
            tier=GrantTier.contributor, status=AccessRequestStatus.revoked,
            decided_at=now - timedelta(days=30), revoked_at=now - timedelta(days=1), expires_at=None,
        )
        self.db.add_all([january_viewer, february_contributor])
        self.db.commit()
        self.assertIsNone(resolve_stage_grant(self.db, self.viewer.user_id, self.stage.stage_id))

    def test_tie_breaker_uses_request_id_when_timestamps_are_identical(self):
        same_ts = datetime.now(timezone.utc)
        lower_id = uuid.UUID(int=1)
        higher_id = uuid.UUID(int=2)
        self.db.add(self._grant(tier=GrantTier.viewer, decided_at=same_ts, expires_at=same_ts + timedelta(days=90), request_id=lower_id))
        self.db.add(self._grant(tier=GrantTier.contributor, decided_at=same_ts, expires_at=same_ts + timedelta(days=90), request_id=higher_id))
        self.db.commit()
        grant = resolve_stage_grant(self.db, self.viewer.user_id, self.stage.stage_id)
        self.assertEqual(grant.request_id, higher_id)
        self.assertEqual(grant.tier, GrantTier.contributor)

    def test_grant_on_soft_deleted_stage_is_not_returned(self):
        now = datetime.now(timezone.utc)
        self.db.add(self._grant(tier=GrantTier.viewer, decided_at=now, expires_at=None))
        self.db.commit()
        self.stage.deleted_at = now
        self.db.commit()
        self.assertIsNone(resolve_stage_grant(self.db, self.viewer.user_id, self.stage.stage_id))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_stage_grant_resolver.py -v`
Expected: FAIL with `ImportError: cannot import name 'get_active_stage_grants_for_user'`.

- [ ] **Step 3: Implement**

In `app/services/access_control.py`, change the top-level import line 32 from:
```python
from sqlalchemy import select
```
to:
```python
from sqlalchemy import func, select
```

Then, immediately after `get_accessible_stages_for_user()` (which currently ends at line 223, right before the `@dataclass class EffectiveAccessResult` block), insert:

```python
@dataclass
class StageGrant:
    """
    tier + team_id together are the grant's full entitlement (spec §3.2):
    the tier is only ever exercised AS this specific team, never any
    other team the grant holder might separately belong to. Any code
    checking `tier` for a mutation must also check `team_id` against
    the team the mutation is being attributed to — never one without
    the other.
    """
    tier: GrantTier
    team_id: UUID
    expires_at: datetime | None
    request_id: UUID


def get_active_stage_grants_for_user(db: Session, user_id: UUID) -> dict[UUID, "StageGrant"]:
    """
    stage_id -> StageGrant for every stage where user_id currently holds
    an active grant. The single source every consumer reads from.

    "Active" means: the LATEST terminal event (approval or revocation,
    ranked by COALESCE(revoked_at, decided_at) DESC, request_id DESC as
    a deterministic tie-breaker) for this (user, stage) is itself an
    approval, and that approval is not expired, and the stage itself is
    not soft-deleted.

    Deliberately does NOT simply filter to status == approved and take
    the newest such row — an older approved row must not "reactivate"
    after a newer grant covering the same stage is revoked. Ranking by
    the latest EVENT (regardless of whether that event was an approval
    or a revocation) and only then checking whether it was an approval
    is what makes revocation permanent instead of a fallback to
    whatever was approved before it.
    """
    rows = db.execute(
        select(AccessRequest)
        .join(Stage, Stage.stage_id == AccessRequest.stage_id)
        .where(
            AccessRequest.user_id == user_id,
            AccessRequest.scope == AccessRequestScope.stage,
            AccessRequest.tier.is_not(None),
            AccessRequest.status.in_([AccessRequestStatus.approved, AccessRequestStatus.revoked]),
            Stage.deleted_at.is_(None),
        )
        .order_by(
            func.coalesce(AccessRequest.revoked_at, AccessRequest.decided_at).desc(),
            AccessRequest.request_id.desc(),
        )
    ).scalars().all()

    now = datetime.now(timezone.utc)
    result: dict[UUID, StageGrant] = {}
    seen_stage_ids: set[UUID] = set()
    for r in rows:
        if r.stage_id in seen_stage_ids:
            continue  # a later (in this ordering) row already settled this stage
        seen_stage_ids.add(r.stage_id)
        if r.status != AccessRequestStatus.approved:
            continue  # the latest terminal event for this stage was a revocation
        if r.expires_at is not None:
            exp = r.expires_at if r.expires_at.tzinfo else r.expires_at.replace(tzinfo=timezone.utc)
            if exp < now:
                continue
        result[r.stage_id] = StageGrant(
            tier=r.tier, team_id=r.team_id, expires_at=r.expires_at, request_id=r.request_id,
        )
    return result


def resolve_stage_grant(db: Session, user_id: UUID, stage_id: UUID) -> "StageGrant | None":
    return get_active_stage_grants_for_user(db, user_id).get(stage_id)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_stage_grant_resolver.py -v`
Expected: 7 passed.

- [ ] **Step 5: Regression check**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_access_control_resolver.py tests/test_access_requests_scoped.py -q`
Expected: same as Task 1 Step 8 — `test_access_control_resolver.py` fully green, `test_access_requests_scoped.py` shows exactly the 3 known pre-existing failures. This task only ADDS new functions; nothing existing calls them yet, so nothing else should move.

- [ ] **Step 6: Commit**

```bash
git add app/services/access_control.py tests/test_stage_grant_resolver.py
git commit -m "feat(access): add get_active_stage_grants_for_user, the stage-grant ABAC resolver"
```

---

### Task 3: `get_accessible_stages_for_user()` — union in grant stages

**Files:**
- Modify: `app/services/access_control.py:191-223`
- Test: `tests/test_stage_grant_resolver.py` (append to the file from Task 2)

**Interfaces:**
- Consumes: `get_active_stage_grants_for_user()` (Task 2).
- Produces: no signature change to `get_accessible_stages_for_user(db, user_id, project_id) -> list[UUID]` — same callers (`build_access_filter`, `stages.py`, `workspace.py`, `project_intelligence.py`, `rag/retrieval.py`, both `query_tools.py` call sites, `graph_tools.py`) get the fix automatically with no changes on their end.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_stage_grant_resolver.py` (same file, same class — it already has the fixtures this needs):

```python
    def test_stage_with_no_native_team_access_becomes_visible_via_grant(self):
        from app.services.access_control import get_accessible_stages_for_user

        outsider = User(email=f"o-{uuid.uuid4().hex[:8]}@test.com", tenant_id=self.tenant.tenant_id, password_hash="x")
        self.db.add(outsider)
        self.db.commit()
        other_team = Team(project_id=self.project.project_id, name="Outsider's Team")
        self.db.add(other_team)
        self.db.commit()
        self.db.add(UserTeamMembership(
            user_id=outsider.user_id, team_id=other_team.team_id,
            project_id=self.project.project_id, role=TeamRole.viewer,
        ))
        self.db.commit()
        # outsider's own team has NO TeamStageAccess to self.stage at all.
        self.assertEqual(get_accessible_stages_for_user(self.db, outsider.user_id, self.project.project_id), [])

        grant = AccessRequest(
            user_id=outsider.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            tier=GrantTier.viewer, status=AccessRequestStatus.approved,
            decided_at=datetime.now(timezone.utc), expires_at=None,
        )
        self.db.add(grant)
        self.db.commit()
        try:
            self.assertEqual(
                get_accessible_stages_for_user(self.db, outsider.user_id, self.project.project_id),
                [self.stage.stage_id],
            )
        finally:
            self.db.query(AccessRequest).filter(AccessRequest.request_id == grant.request_id).delete()
            self.db.query(UserTeamMembership).filter(UserTeamMembership.user_id == outsider.user_id).delete()
            self.db.query(Team).filter(Team.team_id == other_team.team_id).delete()
            self.db.query(User).filter(User.user_id == outsider.user_id).delete()
            self.db.commit()

    def test_grant_for_a_stage_in_a_different_project_is_not_leaked_in(self):
        from app.services.access_control import get_accessible_stages_for_user

        other_project = Project(tenant_id=self.tenant.tenant_id, name=f"Other Project {uuid.uuid4().hex[:8]}")
        self.db.add(other_project)
        self.db.commit()
        other_stage = Stage(project_id=other_project.project_id, name="Other Stage", order_index=1)
        self.db.add(other_stage)
        self.db.commit()

        grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.stage, stage_id=other_stage.stage_id,
            tier=GrantTier.viewer, status=AccessRequestStatus.approved,
            decided_at=datetime.now(timezone.utc), expires_at=None,
        )
        self.db.add(grant)
        self.db.commit()
        try:
            self.assertEqual(
                get_accessible_stages_for_user(self.db, self.viewer.user_id, self.project.project_id),
                [],
            )
        finally:
            self.db.query(AccessRequest).filter(AccessRequest.request_id == grant.request_id).delete()
            self.db.query(Stage).filter(Stage.stage_id == other_stage.stage_id).delete()
            self.db.query(Project).filter(Project.project_id == other_project.project_id).delete()
            self.db.commit()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_stage_grant_resolver.py -v -k "different_project or via_grant"`
Expected: `test_stage_with_no_native_team_access_becomes_visible_via_grant` FAILS (asserts `[self.stage.stage_id]`, gets `[]`); `test_grant_for_a_stage_in_a_different_project_is_not_leaked_in` PASSES already (nothing unions grants in yet, so it's trivially `[]`) — that's fine, it becomes a real regression guard once Step 3 lands.

- [ ] **Step 3: Implement**

Replace `app/services/access_control.py:191-223` (the full body of `get_accessible_stages_for_user`, from `if _is_org_admin` through the final `return list(...)`) with:

```python
    if _is_org_admin(db, user_id) or _is_project_admin(db, user_id, project_id):
        return list(db.execute(
            select(Stage.stage_id).where(
                Stage.project_id == project_id, Stage.deleted_at.is_(None)
            )
        ).scalars())

    team_ids = list(db.execute(
        select(UserTeamMembership.team_id).where(
            UserTeamMembership.user_id == user_id,
            UserTeamMembership.project_id == project_id,
        )
    ).scalars())

    native_stage_ids: set[UUID] = set()
    if team_ids:
        native_stage_ids = set(db.execute(
            select(TeamStageAccess.stage_id)
            .join(Stage, Stage.stage_id == TeamStageAccess.stage_id)
            .where(TeamStageAccess.team_id.in_(team_ids), Stage.deleted_at.is_(None))
            .distinct()
        ).scalars())

    # Stage grants (spec §1.2/§4): a viewer/contributor/contributor_confidential
    # grant makes its stage visible even with zero native TeamStageAccess.
    # get_active_stage_grants_for_user() is not project-scoped (it's a
    # per-user lookup), so its result is re-filtered to THIS project here.
    grant_stage_ids = set(get_active_stage_grants_for_user(db, user_id).keys())
    if grant_stage_ids:
        grant_stage_ids = set(db.execute(
            select(Stage.stage_id).where(
                Stage.stage_id.in_(grant_stage_ids),
                Stage.project_id == project_id,
                Stage.deleted_at.is_(None),
            )
        ).scalars())

    return list(native_stage_ids | grant_stage_ids)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_stage_grant_resolver.py -v`
Expected: 9 passed.

- [ ] **Step 5: Regression check**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_access_control_resolver.py tests/test_access_requests_scoped.py tests/test_stage_grant_resolver.py -q`
Expected: same known-good baseline as before (18 + 9 passed in the first/third files, exactly the 3 known pre-existing failures in the second).

- [ ] **Step 6: Commit**

```bash
git add app/services/access_control.py tests/test_stage_grant_resolver.py
git commit -m "feat(access): stage grants make their stage visible via get_accessible_stages_for_user"
```

---

### Task 4: Tier-gate `resolve_effective_access()` / `is_grant_only_confidential_access()`, and fix `GET /access-requests/status`

**Files:**
- Modify: `app/services/access_control.py:226-238` (`EffectiveAccessResult`), `:359-382` (grant-winner selection inside `resolve_effective_access`), `:125-137` (`is_grant_only_confidential_access`)
- Modify: `app/routers/access_requests.py:78-115` (`EffectiveAccessOut`, `access_status()`)
- Modify: `tests/test_access_control_resolver.py` (fix one existing fixture — see Step 1)
- Test: `tests/test_access_requests_router.py` (new)

**Interfaces:**
- Consumes: `resolve_stage_grant()` (Task 2).
- Produces: `EffectiveAccessResult` gains `scope: AccessRequestScope | None = None` and `tier: GrantTier | None = None` fields, consumed by Task 6 and by anything downstream that needs to know *why* a result was granted.

**Why this task exists (traced against the live code, not assumed):** `resolve_effective_access()`'s stage-scope branch currently treats *any* approved, unexpired stage grant as unlocking confidential access — with no concept of tier, every stage grant behaved like today's single-purpose confidential grant. Once tiers exist, a `viewer`/`contributor` grant must NOT unlock confidential documents (only `contributor_confidential` should), or the whole point of having tiers is defeated. Separately, `is_grant_only_confidential_access()` currently returns `True` (read-only) for *any* grant-derived confidential access — left as-is, it would wrongly block writes for `contributor_confidential`, the one tier explicitly meant to allow them. And `GET /access-requests/status` (which `StageSection.jsx`'s `StageAccessLink` and `DocumentItem.jsx`'s `LockedDocumentRow` poll) calls `resolve_effective_access()` directly — once that function stops reporting "granted" for a mere `viewer`/`contributor` stage grant (correctly, for confidential-access purposes), the same endpoint would start telling the UI "you have no grant at all" for someone who very much does — a real regression this task closes by having the endpoint also consult `resolve_stage_grant()`.

- [ ] **Step 1: Fix the one existing test this task's change breaks**

In `tests/test_access_control_resolver.py`, the import line near the top currently reads:
```python
from app.models.team import AccessRequest, AccessRequestScope, AccessRequestStatus, Team, TeamRole, UserTeamMembership
```
Change it to:
```python
from app.models.team import AccessRequest, AccessRequestScope, AccessRequestStatus, GrantTier, Team, TeamRole, UserTeamMembership
```

Then in `test_stale_denied_request_does_not_suppress_active_stage_grant`, the `stage_grant` fixture currently reads:
```python
        stage_grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        )
```
Add `tier=GrantTier.contributor_confidential,` right after `status=AccessRequestStatus.approved,` — this test's whole point is proving an active stage grant unlocks confidential document access, so it needs to be the one tier that actually does that under the new model; without this the test would start failing for a reason unrelated to a real regression, just because a tier-less stage grant no longer means anything.

(`test_document_grant_takes_precedence_over_overlapping_stage_grant`, a few lines below, also builds a tier-less `stage_grant` — leave it untouched. Traced through: it wins on the `document` scope bucket before the `stage` bucket is ever consulted, so this task's tier filter never affects its outcome. `test_stage_scoped_grant_is_honored_in_batch_path`, further down in `TestBatchVisibilityGrants`, DOES need the same fix — but it exercises the batch/`AuthorizationContext` path, not this function directly, so it's fixed in Task 6, not here.)

- [ ] **Step 2: Run the resolver tests to confirm the one fixed test still passes, and see the tier-gating tests fail**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_access_control_resolver.py -v`
Expected: all 18 still pass right now (the fixture edit alone doesn't change behavior yet — `tier` isn't read by `resolve_effective_access()` until Step 4). This step is just confirming the test file edits compile and don't themselves break anything before the real implementation change.

- [ ] **Step 3: Add two new tests proving the tier gate**

Append to `tests/test_access_control_resolver.py`, inside `TestIsGrantOnlyConfidentialAccess`:

```python
    def test_viewer_tier_stage_grant_does_not_unlock_confidential(self):
        grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            tier=GrantTier.viewer, status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        )
        self.db.add(grant)
        self.db.commit()
        result = resolve_effective_access(self.db, self.viewer.user_id, document_id=self.document.document_id)
        self.assertEqual(result.status, "none")
        self.assertFalse(is_grant_only_confidential_access(self.db, self.viewer.user_id, self.document))

    def test_contributor_confidential_stage_grant_is_not_read_only(self):
        grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            tier=GrantTier.contributor_confidential, status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        )
        self.db.add(grant)
        self.db.commit()
        result = resolve_effective_access(self.db, self.viewer.user_id, document_id=self.document.document_id)
        self.assertEqual(result.status, "granted")
        self.assertEqual(result.tier, GrantTier.contributor_confidential)
        # The whole point of this tier: it must NOT be read-only, unlike
        # every other kind of grant-derived confidential access.
        self.assertFalse(is_grant_only_confidential_access(self.db, self.viewer.user_id, self.document))
```

- [ ] **Step 4: Run to verify they fail**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_access_control_resolver.py -v -k "viewer_tier_stage_grant or contributor_confidential_stage_grant"`
Expected: both FAIL — today ANY active stage grant (any tier) reports `status == "granted"`, so `test_viewer_tier_stage_grant_does_not_unlock_confidential` fails on `assertEqual(result.status, "none")`, and `test_contributor_confidential_stage_grant_is_not_read_only` fails on the final `assertFalse` (today it's unconditionally `True`).

- [ ] **Step 5: Implement — `EffectiveAccessResult` and the grant-winner selection**

In `app/services/access_control.py`, add `GrantTier` to the `app.models.team` import block (currently `app/services/access_control.py:36-44`):
```python
from app.models.team import (
    UserTeamMembership,
    ProjectAdmin,
    Team,
    TeamRole,
    AccessRequest,
    AccessRequestScope,
    AccessRequestStatus,
    GrantTier,
)
```

Replace `EffectiveAccessResult` (`access_control.py:226-238`):
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
    status: str  # "granted" | "pending" | "denied" | "none"
    via_grant: bool = False  # only meaningful when status == "granted"
    expires_at: datetime | None = None
    request_id: UUID | None = None
    scope: "AccessRequestScope | None" = None  # the winning grant's scope, when via_grant
    tier: "GrantTier | None" = None  # the winning grant's tier, when it's a stage-scope grant
```

Replace the grant-winner selection block (`access_control.py:367-382`, currently):
```python
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
```
with:
```python
    active_grants = [r for r in rows if _is_active_grant(r)]
    if active_grants:
        # Narrowest-scope-wins tie-break (spec §4.0.2): document, then
        # stage, then team. Only relevant when resolving a document target
        # that could be covered by an overlapping stage/team grant too —
        # a stage or team target query only ever matches its own scope.
        by_scope = {AccessRequestScope.document: [], AccessRequestScope.stage: [], AccessRequestScope.team: []}
        for r in active_grants:
            by_scope[r.scope].append(r)
        # A stage-scope grant only confers CONFIDENTIAL access — this
        # resolver's only concern — when its approver chose the
        # contributor_confidential tier. viewer/contributor stage grants
        # make the stage itself visible (get_accessible_stages_for_user)
        # but never unlock confidential documents through this path.
        # Document/team-scope grants have no tier concept and remain
        # always-confidential-read as originally implemented.
        by_scope[AccessRequestScope.stage] = [
            r for r in by_scope[AccessRequestScope.stage] if r.tier == GrantTier.contributor_confidential
        ]
        for scope in (AccessRequestScope.document, AccessRequestScope.stage, AccessRequestScope.team):
            if by_scope[scope]:
                winner = by_scope[scope][0]
                return EffectiveAccessResult(
                    status="granted", via_grant=True,
                    expires_at=winner.expires_at, request_id=winner.request_id,
                    scope=winner.scope, tier=winner.tier,
                )
```

- [ ] **Step 6: Implement — `is_grant_only_confidential_access()`**

Replace `access_control.py:125-137`:
```python
def is_grant_only_confidential_access(db: Session, user_id: UUID, document) -> bool:
    """
    True iff `document` is confidential-tier AND user_id's access to it is
    coming from an approved confidential-access grant rather than native
    role-based access — i.e. this document must be READ-ONLY for this user
    regardless of their normal team role. Thin wrapper over
    resolve_effective_access() (the single shared resolver) — never
    re-derives native-vs-grant precedence independently.

    The one exception: a stage-scope grant at the contributor_confidential
    tier is explicitly NOT read-only (that tier's entire point is to allow
    writes) — every other kind of grant-derived confidential access
    (document/team-scope, or a stage grant at any other tier reaching this
    function some other way) stays read-only, exactly as before.
    """
    if document.sensitivity_level != SensitivityLevel.confidential:
        return False
    result = resolve_effective_access(db, user_id, document_id=document.document_id)
    if not (result.status == "granted" and result.via_grant):
        return False
    if result.scope == AccessRequestScope.stage and result.tier == GrantTier.contributor_confidential:
        return False
    return True
```

- [ ] **Step 7: Run to verify the new tests pass**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_access_control_resolver.py -v`
Expected: 20 passed (18 original + 2 new).

- [ ] **Step 8: Fix `GET /access-requests/status` — write the failing test first**

Create `tests/test_access_requests_router.py`:

```python
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from starlette.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.api.dependencies import get_current_user
from app.models.project import Project
from app.models.stage import Stage, TeamStageAccess
from app.models.team import AccessRequest, AccessRequestScope, AccessRequestStatus, GrantTier, Team, TeamRole, UserTeamMembership
from app.models.tenant import Tenant
from app.models.user import User
from app.services.auth import resolve_identity


class TestAccessStatusEndpoint(unittest.TestCase):
    def setUp(self):
        self.db = SessionLocal()
        self.tenant = self.db.query(Tenant).first()
        self.assertIsNotNone(self.tenant, "Tenant required")
        tenant_id = self.tenant.tenant_id

        self.project = Project(tenant_id=tenant_id, name=f"Status Endpoint Test {uuid.uuid4().hex[:8]}")
        self.db.add(self.project)
        self.db.commit()

        self.team = Team(project_id=self.project.project_id, name="Status Team")
        self.db.add(self.team)
        self.db.commit()

        self.stage = Stage(project_id=self.project.project_id, name="Status Stage", order_index=1)
        self.db.add(self.stage)
        self.db.commit()
        self.db.add(TeamStageAccess(team_id=self.team.team_id, stage_id=self.stage.stage_id))
        self.db.commit()

        self.viewer = User(email=f"status-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.db.add(self.viewer)
        self.db.commit()
        self.db.add(UserTeamMembership(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            project_id=self.project.project_id, role=TeamRole.viewer,
        ))
        self.db.commit()

        self.identity = resolve_identity(self.db, self.viewer.user_id)
        app.dependency_overrides[get_current_user] = lambda: self.identity
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.rollback()
        self.db.query(AccessRequest).filter(AccessRequest.team_id == self.team.team_id).delete()
        self.db.query(TeamStageAccess).filter(TeamStageAccess.stage_id == self.stage.stage_id).delete()
        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id == self.project.project_id).delete()
        self.db.query(User).filter(User.user_id == self.viewer.user_id).delete()
        self.db.query(Stage).filter(Stage.stage_id == self.stage.stage_id).delete()
        self.db.query(Team).filter(Team.team_id == self.team.team_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def test_active_viewer_tier_grant_reports_granted_not_none(self):
        grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            tier=GrantTier.viewer, status=AccessRequestStatus.approved,
            decided_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        self.db.add(grant)
        self.db.commit()
        resp = self.client.get(f"/access-requests/status?stage_id={self.stage.stage_id}")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # Before this task's fix, this would report "none" — the ordinary
        # confidential-access resolver correctly doesn't count a viewer-tier
        # grant as "granted" for CONFIDENTIAL purposes, but the UI polling
        # this endpoint (StageAccessLink, LockedDocumentRow) needs to know
        # the grant exists at all, regardless of tier.
        self.assertEqual(body["status"], "granted")
        self.assertTrue(body["via_grant"])

    def test_no_grant_still_reports_none(self):
        resp = self.client.get(f"/access-requests/status?stage_id={self.stage.stage_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "none")
```

- [ ] **Step 9: Run to verify it fails**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_access_requests_router.py -v`
Expected: `test_active_viewer_tier_grant_reports_granted_not_none` FAILS (`assertEqual(body["status"], "granted")` gets `"none"`); `test_no_grant_still_reports_none` passes already.

- [ ] **Step 10: Implement the endpoint fix**

In `app/routers/access_requests.py`, replace the body of `access_status()` (`app/routers/access_requests.py:85-115`, from the docstring through the `return EffectiveAccessOut(...)`):

```python
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

    For a stage target specifically: resolve_effective_access() only
    reports "granted" when the active grant is confidential-unlocking
    (contributor_confidential tier) — correct for its own purpose, but this
    endpoint also needs to reflect a plain viewer/contributor grant, which
    IS an active grant even though it confers no confidential access. When
    resolve_stage_grant() finds one and resolve_effective_access() didn't
    already report "granted" via some other path, this reports "granted"
    too, using that grant's own expires_at/request_id.
    """
    provided = [x for x in (document_id, stage_id, team_id) if x is not None]
    if len(provided) != 1:
        raise HTTPException(
            status_code=422, detail="Exactly one of document_id, stage_id, team_id is required."
        )
    from app.services.access_control import resolve_effective_access, resolve_stage_grant

    result = resolve_effective_access(
        db, identity.user_id, document_id=document_id, stage_id=stage_id, team_id=team_id,
    )
    if result.status != "granted" and stage_id is not None:
        stage_grant = resolve_stage_grant(db, identity.user_id, stage_id)
        if stage_grant is not None:
            return EffectiveAccessOut(
                status="granted", via_grant=True,
                expires_at=stage_grant.expires_at.isoformat() if stage_grant.expires_at else None,
                request_id=str(stage_grant.request_id),
            )
    return EffectiveAccessOut(
        status=result.status,
        via_grant=result.via_grant,
        expires_at=result.expires_at.isoformat() if result.expires_at else None,
        request_id=str(result.request_id) if result.request_id else None,
    )
```

- [ ] **Step 11: Run to verify both tests pass**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_access_requests_router.py -v`
Expected: 2 passed.

- [ ] **Step 12: Full regression check**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_access_control_resolver.py tests/test_access_requests_scoped.py tests/test_stage_grant_resolver.py tests/test_access_requests_router.py tests/test_grant_enforcement.py -q`
Expected: 20 + 9 + 2 + (whatever `test_grant_enforcement.py` already had, unaffected) passed; `test_access_requests_scoped.py` still exactly the 3 known pre-existing failures.

- [ ] **Step 13: Commit**

```bash
git add app/services/access_control.py app/routers/access_requests.py tests/test_access_control_resolver.py tests/test_access_requests_router.py
git commit -m "feat(access): tier-gate confidential unlock; fix status endpoint for non-confidential-tier grants"
```

---

### Task 5: `DocumentTeamVisibility` override in `classify_document_visibility()` and `build_access_filter()`

**Files:**
- Modify: `app/services/access_control.py:431-469` (`classify_document_visibility`), `:578-641` (`build_access_filter`)
- Test: `tests/test_stage_grant_document_visibility.py` (new)

**Interfaces:**
- Consumes: `resolve_stage_grant()`, `get_active_stage_grants_for_user()`, `get_accessible_stages_for_user()` (Tasks 2-3).
- Produces: no signature changes — both functions keep their existing callers untouched.

**Why (spec §1.2):** `DocumentTeamVisibility` (which team a *specific document* is visible to, defaulting to only its uploading team) is independent of stage-level `TeamStageAccess`. Traced directly: `classify_document_visibility()` returns `not_visible` immediately whenever the user has no membership on any team the document is visible to — before sensitivity or grants are even consulted. A stage grant must override this check *within its own stage only*, or `viewer`/`contributor` tiers would have zero observable read effect for anyone whose team already has `TeamStageAccess` to the stage (which is required to reach the request flow at all) — they'd already see every document their own team uploaded regardless of any grant.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_stage_grant_document_visibility.py`:

```python
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models.document import Document, DocumentTeamVisibility, SensitivityLevel
from app.models.project import Project
from app.models.stage import Stage, TeamStageAccess
from app.models.team import AccessRequest, AccessRequestScope, AccessRequestStatus, GrantTier, Team, TeamRole, UserTeamMembership
from app.models.tenant import Tenant
from app.models.user import User
from app.services.access_control import build_access_filter, classify_document_visibility, DocumentVisibility


class TestStageGrantDocumentVisibility(unittest.TestCase):
    def setUp(self):
        with SessionLocal() as lookup_db:
            self.tenant = lookup_db.query(Tenant).first()
            tenant_id = self.tenant.tenant_id

        self.db = SessionLocal()
        self.db.info["tenant_id"] = str(tenant_id)

        self.project = Project(tenant_id=tenant_id, name=f"DocVis Test {uuid.uuid4().hex[:8]}")
        self.db.add(self.project)
        self.db.commit()

        # Two teams share the stage; the viewer belongs to "own_team" only,
        # but the document in question was uploaded AS "other_team" — so
        # DocumentTeamVisibility does not include own_team for it, even
        # though own_team has full TeamStageAccess to the stage.
        self.own_team = Team(project_id=self.project.project_id, name="Own Team")
        self.other_team = Team(project_id=self.project.project_id, name="Other Team")
        self.db.add_all([self.own_team, self.other_team])
        self.db.commit()

        self.stage = Stage(project_id=self.project.project_id, name="Shared Stage", order_index=1)
        self.db.add(self.stage)
        self.db.commit()
        self.db.add(TeamStageAccess(team_id=self.own_team.team_id, stage_id=self.stage.stage_id))
        self.db.commit()

        self.viewer = User(email=f"dv-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.other_uploader = User(email=f"ou-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.db.add_all([self.viewer, self.other_uploader])
        self.db.commit()
        self.db.add(UserTeamMembership(
            user_id=self.viewer.user_id, team_id=self.own_team.team_id,
            project_id=self.project.project_id, role=TeamRole.viewer,
        ))
        self.db.commit()

        self.public_doc = Document(
            tenant_id=tenant_id, project_id=self.project.project_id, stage_id=self.stage.stage_id,
            uploaded_by=self.other_uploader.user_id, uploaded_as_team_id=self.other_team.team_id,
            sensitivity_level=SensitivityLevel.public,
            original_filename="public.md", mime_type="text/markdown",
        )
        self.confidential_doc = Document(
            tenant_id=tenant_id, project_id=self.project.project_id, stage_id=self.stage.stage_id,
            uploaded_by=self.other_uploader.user_id, uploaded_as_team_id=self.other_team.team_id,
            sensitivity_level=SensitivityLevel.confidential,
            original_filename="confidential.md", mime_type="text/markdown",
        )
        self.db.add_all([self.public_doc, self.confidential_doc])
        self.db.commit()
        self.db.add(DocumentTeamVisibility(document_id=self.public_doc.document_id, team_id=self.other_team.team_id))
        self.db.add(DocumentTeamVisibility(document_id=self.confidential_doc.document_id, team_id=self.other_team.team_id))
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        self.db.query(AccessRequest).filter(AccessRequest.stage_id == self.stage.stage_id).delete()
        for doc in (self.public_doc, self.confidential_doc):
            self.db.query(DocumentTeamVisibility).filter(DocumentTeamVisibility.document_id == doc.document_id).delete()
            self.db.query(Document).filter(Document.document_id == doc.document_id).update({"current_version_id": None})
            self.db.query(Document).filter(Document.document_id == doc.document_id).delete()
        self.db.query(TeamStageAccess).filter(TeamStageAccess.stage_id == self.stage.stage_id).delete()
        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id == self.project.project_id).delete()
        self.db.query(User).filter(User.user_id.in_([self.viewer.user_id, self.other_uploader.user_id])).delete(synchronize_session=False)
        self.db.query(Stage).filter(Stage.stage_id == self.stage.stage_id).delete()
        self.db.query(Team).filter(Team.project_id == self.project.project_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def _grant(self, tier):
        return AccessRequest(
            user_id=self.viewer.user_id, team_id=self.own_team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            tier=tier, status=AccessRequestStatus.approved,
            decided_at=datetime.now(timezone.utc), expires_at=None,
        )

    def test_without_grant_other_teams_public_doc_is_not_visible(self):
        self.assertEqual(
            classify_document_visibility(self.db, self.viewer.user_id, self.public_doc),
            DocumentVisibility.not_visible,
        )

    def test_viewer_tier_grant_unlocks_other_teams_public_doc(self):
        self.db.add(self._grant(GrantTier.viewer))
        self.db.commit()
        self.assertEqual(
            classify_document_visibility(self.db, self.viewer.user_id, self.public_doc),
            DocumentVisibility.fully_allowed,
        )

    def test_viewer_tier_grant_does_not_unlock_other_teams_confidential_doc(self):
        self.db.add(self._grant(GrantTier.viewer))
        self.db.commit()
        self.assertEqual(
            classify_document_visibility(self.db, self.viewer.user_id, self.confidential_doc),
            DocumentVisibility.blocked_by_sensitivity,
        )

    def test_contributor_confidential_tier_grant_unlocks_other_teams_confidential_doc(self):
        self.db.add(self._grant(GrantTier.contributor_confidential))
        self.db.commit()
        self.assertEqual(
            classify_document_visibility(self.db, self.viewer.user_id, self.confidential_doc),
            DocumentVisibility.fully_allowed,
        )

    def test_build_access_filter_includes_other_teams_doc_only_with_grant(self):
        filt = build_access_filter(self.db, self.viewer.user_id, self.project.project_id)
        visible_ids = {
            d.document_id for d in self.db.query(Document).filter(filt).all()
        }
        self.assertNotIn(self.public_doc.document_id, visible_ids)

        self.db.add(self._grant(GrantTier.viewer))
        self.db.commit()
        filt = build_access_filter(self.db, self.viewer.user_id, self.project.project_id)
        visible_ids = {
            d.document_id for d in self.db.query(Document).filter(filt).all()
        }
        self.assertIn(self.public_doc.document_id, visible_ids)
```

- [ ] **Step 2: Run to verify failures**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_stage_grant_document_visibility.py -v`
Expected: `test_without_grant_other_teams_public_doc_is_not_visible` passes already (nothing changed yet); the other 4 FAIL.

- [ ] **Step 3: Implement — `classify_document_visibility()`**

Replace `access_control.py:431-469`:
```python
def classify_document_visibility(db: Session, user_id: UUID, document: Document) -> DocumentVisibility:
    """
    The full ABAC decision for viewing a specific document — bypass, team
    visibility, and sensitivity clearance — as a three-way outcome rather
    than can_view_document()'s bool.

    A stage grant (any tier) overrides the team-visibility check for
    documents in its own stage only (spec §1.2) — it does NOT change what
    DocumentTeamVisibility means for any other stage or any other user.
    """
    if _is_org_admin(db, user_id):
        return DocumentVisibility.fully_allowed
    if _is_project_admin(db, user_id, document.project_id):
        return DocumentVisibility.fully_allowed

    # Team visibility — document must be visible to a team the user belongs to
    visible_team_ids = {
        row.team_id for row in db.execute(
            select(DocumentTeamVisibility).where(DocumentTeamVisibility.document_id == document.document_id)
        ).scalars()
    }
    memberships = [
        m for m in (_get_team_membership(db, user_id, team_id) for team_id in visible_team_ids)
        if m is not None
    ]

    stage_grant = resolve_stage_grant(db, user_id, document.stage_id) if document.stage_id else None

    if not memberships and stage_grant is None:
        return DocumentVisibility.not_visible  # not on any team this document is visible to, and no stage grant either

    # Sensitivity clearance
    if document.sensitivity_level in (SensitivityLevel.public, SensitivityLevel.internal):
        return DocumentVisibility.fully_allowed  # viewer+ (native or grant-derived) can always see these

    # confidential tier — delegate to the single shared resolver rather than
    # re-deriving native-vs-grant precedence here. resolve_effective_access()
    # independently matches this document's stage_id against any stage-scope
    # grant (Task 4's tier gate already restricts that to
    # contributor_confidential), so no separate grant check is needed here.
    result = resolve_effective_access(db, user_id, document_id=document.document_id)
    if result.status == "granted":
        return DocumentVisibility.fully_allowed
    return DocumentVisibility.blocked_by_sensitivity
```

- [ ] **Step 4: Implement — `build_access_filter()`**

Replace `access_control.py:578-641`:
```python
def build_access_filter(db: Session, user_id: UUID, project_id: UUID):
    """
    Returns a SQLAlchemy filter condition for querying documents within a
    project — the COARSE narrowing only: tenant, project, team-visibility
    (or an overriding stage grant), and stage access. Sensitivity is
    deliberately NOT filtered here; that decision belongs entirely to
    can_view_document()'s per-row check. Callers must still run each
    returned row through can_view_document().

    For org_admin/project_admin, returns a filter scoped only to
    tenant/project (full visibility within that scope).

    A stage grant (any tier) overrides the DocumentTeamVisibility
    requirement for documents in its own stage only (spec §1.2) — the
    document must still fall within accessible_stage_ids, which already
    includes grant stages via get_accessible_stages_for_user() (Task 3).
    """
    from sqlalchemy import and_, or_

    user = db.get(User, user_id)

    if user.is_org_admin:
        return Document.tenant_id == user.tenant_id

    if _is_project_admin(db, user_id, project_id):
        return and_(Document.tenant_id == user.tenant_id, Document.project_id == project_id)

    # Regular user: must be on a team the document is visible to, OR the
    # document's stage must be one this user holds an active grant for.
    user_team_ids = [
        row.team_id for row in db.execute(
            select(UserTeamMembership).where(
                UserTeamMembership.user_id == user_id,
                UserTeamMembership.project_id == project_id,
            )
        ).scalars()
    ]
    grant_stage_ids = list(get_active_stage_grants_for_user(db, user_id).keys())
    if not user_team_ids and not grant_stage_ids:
        return Document.document_id == None  # no access — matches nothing

    accessible_stage_ids = get_accessible_stages_for_user(db, user_id, project_id)
    if not accessible_stage_ids:
        return Document.document_id == None  # no access — matches nothing

    visible_doc_ids_subquery = (
        select(DocumentTeamVisibility.document_id)
        .where(DocumentTeamVisibility.team_id.in_(user_team_ids))
    )

    return and_(
        Document.tenant_id == user.tenant_id,
        Document.project_id == project_id,
        or_(
            Document.document_id.in_(visible_doc_ids_subquery),
            Document.stage_id.in_(grant_stage_ids),
        ),
        Document.stage_id.in_(accessible_stage_ids),
        # NOTE: no sensitivity condition — can_view_document() makes the
        # final per-row call, exactly as before.
    )
```

- [ ] **Step 5: Run to verify they pass**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_stage_grant_document_visibility.py -v`
Expected: 5 passed.

- [ ] **Step 6: Full regression check**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_access_control_resolver.py tests/test_access_requests_scoped.py tests/test_stage_grant_resolver.py tests/test_access_requests_router.py tests/test_stage_grant_document_visibility.py tests/test_grant_enforcement.py -q`
Expected: all green except the same 3 known pre-existing failures in `test_access_requests_scoped.py`.

- [ ] **Step 7: Commit**

```bash
git add app/services/access_control.py tests/test_stage_grant_document_visibility.py
git commit -m "feat(access): stage grants override DocumentTeamVisibility within their own stage"
```

---

### Task 6: `AuthorizationContext.stage_grant_tiers` + batch `classify_documents_visibility()`

**Files:**
- Modify: `app/services/authorization_context.py` (whole file — `AuthorizationContext` dataclass and `build_authorization_context()`)
- Modify: `app/services/access_control.py:472-565` (`classify_documents_visibility`)
- Modify: `tests/test_access_control_resolver.py` (fix the one batch-path test that needs a tier — see Step 1)
- Test: `tests/test_stage_grant_batch_visibility.py` (new)

**Interfaces:**
- Consumes: `get_active_stage_grants_for_user()` (Task 2).
- Produces: `AuthorizationContext.stage_grant_tiers: dict[uuid.UUID, GrantTier]`, replacing the removed `active_confidential_grant_stage_ids: set[uuid.UUID]` field — consumed by `classify_documents_visibility()` in this same task, and available to any future batch consumer that needs per-stage tier info.

**Why this task exists (traced against the live code, not assumed):** `AuthorizationContext` builds its own independent `accessible_stage_ids` via a raw `TeamStageAccess` query — it never calls `get_accessible_stages_for_user()`, so Task 3's fix does not reach this path at all. Its confidential-grant set (`active_confidential_grant_stage_ids`) is also tier-blind today: any approved stage grant, regardless of tier, unlocks confidential documents in the batch/RAG path. Both must be fixed explicitly here, or RAG retrieval and the batch document-list path would disagree with the single-document reads Tasks 4-5 just fixed.

- [ ] **Step 1: Fix the one existing test this task's change breaks**

In `tests/test_access_control_resolver.py`, `TestBatchVisibilityGrants.test_stage_scoped_grant_is_honored_in_batch_path` currently builds its `grant` with no `tier`. Add `tier=GrantTier.contributor_confidential,` right after `status=AccessRequestStatus.approved,` in that fixture — same reasoning as Task 4 Step 1: this test's whole point is proving an active stage grant unlocks confidential access in the batch path, so it needs the one tier that does that.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_stage_grant_batch_visibility.py` (mirrors Task 5's fixtures, but drives `classify_documents_visibility()` via a real `AuthorizationContext`):

```python
import unittest
import uuid
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models.document import Document, DocumentTeamVisibility, SensitivityLevel
from app.models.project import Project
from app.models.stage import Stage, TeamStageAccess
from app.models.team import AccessRequest, AccessRequestScope, AccessRequestStatus, GrantTier, Team, TeamRole, UserTeamMembership
from app.models.tenant import Tenant
from app.models.user import User
from app.services.access_control import classify_documents_visibility, DocumentVisibility
from app.services.authorization_context import build_authorization_context


class TestStageGrantBatchVisibility(unittest.TestCase):
    def setUp(self):
        with SessionLocal() as lookup_db:
            self.tenant = lookup_db.query(Tenant).first()
            tenant_id = self.tenant.tenant_id

        self.db = SessionLocal()
        self.db.info["tenant_id"] = str(tenant_id)

        self.project = Project(tenant_id=tenant_id, name=f"BatchVis Test {uuid.uuid4().hex[:8]}")
        self.db.add(self.project)
        self.db.commit()

        self.own_team = Team(project_id=self.project.project_id, name="Own Team")
        self.other_team = Team(project_id=self.project.project_id, name="Other Team")
        self.db.add_all([self.own_team, self.other_team])
        self.db.commit()

        self.stage = Stage(project_id=self.project.project_id, name="Batch Stage", order_index=1)
        self.db.add(self.stage)
        self.db.commit()
        self.db.add(TeamStageAccess(team_id=self.own_team.team_id, stage_id=self.stage.stage_id))
        self.db.commit()

        self.viewer = User(email=f"bv-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.other_uploader = User(email=f"bo-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.db.add_all([self.viewer, self.other_uploader])
        self.db.commit()
        self.db.add(UserTeamMembership(
            user_id=self.viewer.user_id, team_id=self.own_team.team_id,
            project_id=self.project.project_id, role=TeamRole.viewer,
        ))
        self.db.commit()

        self.public_doc = Document(
            tenant_id=tenant_id, project_id=self.project.project_id, stage_id=self.stage.stage_id,
            uploaded_by=self.other_uploader.user_id, uploaded_as_team_id=self.other_team.team_id,
            sensitivity_level=SensitivityLevel.public,
            original_filename="batch-public.md", mime_type="text/markdown",
        )
        self.confidential_doc = Document(
            tenant_id=tenant_id, project_id=self.project.project_id, stage_id=self.stage.stage_id,
            uploaded_by=self.other_uploader.user_id, uploaded_as_team_id=self.other_team.team_id,
            sensitivity_level=SensitivityLevel.confidential,
            original_filename="batch-confidential.md", mime_type="text/markdown",
        )
        self.db.add_all([self.public_doc, self.confidential_doc])
        self.db.commit()
        self.db.add(DocumentTeamVisibility(document_id=self.public_doc.document_id, team_id=self.other_team.team_id))
        self.db.add(DocumentTeamVisibility(document_id=self.confidential_doc.document_id, team_id=self.other_team.team_id))
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        self.db.query(AccessRequest).filter(AccessRequest.stage_id == self.stage.stage_id).delete()
        for doc in (self.public_doc, self.confidential_doc):
            self.db.query(DocumentTeamVisibility).filter(DocumentTeamVisibility.document_id == doc.document_id).delete()
            self.db.query(Document).filter(Document.document_id == doc.document_id).update({"current_version_id": None})
            self.db.query(Document).filter(Document.document_id == doc.document_id).delete()
        self.db.query(TeamStageAccess).filter(TeamStageAccess.stage_id == self.stage.stage_id).delete()
        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id == self.project.project_id).delete()
        self.db.query(User).filter(User.user_id.in_([self.viewer.user_id, self.other_uploader.user_id])).delete(synchronize_session=False)
        self.db.query(Stage).filter(Stage.stage_id == self.stage.stage_id).delete()
        self.db.query(Team).filter(Team.project_id == self.project.project_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def test_authorization_context_carries_stage_grant_tier(self):
        grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.own_team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            tier=GrantTier.contributor, status=AccessRequestStatus.approved,
            decided_at=datetime.now(timezone.utc), expires_at=None,
        )
        self.db.add(grant)
        self.db.commit()
        ctx = build_authorization_context(self.db, self.viewer.user_id, self.project.project_id)
        self.assertEqual(ctx.stage_grant_tiers.get(self.stage.stage_id), GrantTier.contributor)
        self.assertIn(self.stage.stage_id, ctx.accessible_stage_ids)

    def test_batch_visibility_without_grant_excludes_other_teams_doc(self):
        ctx = build_authorization_context(self.db, self.viewer.user_id, self.project.project_id)
        result = classify_documents_visibility(self.db, ctx, [self.public_doc.document_id])
        self.assertEqual(result[self.public_doc.document_id], DocumentVisibility.not_visible)

    def test_batch_visibility_viewer_tier_unlocks_public_not_confidential(self):
        grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.own_team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            tier=GrantTier.viewer, status=AccessRequestStatus.approved,
            decided_at=datetime.now(timezone.utc), expires_at=None,
        )
        self.db.add(grant)
        self.db.commit()
        ctx = build_authorization_context(self.db, self.viewer.user_id, self.project.project_id)
        result = classify_documents_visibility(self.db, ctx, [self.public_doc.document_id, self.confidential_doc.document_id])
        self.assertEqual(result[self.public_doc.document_id], DocumentVisibility.fully_allowed)
        self.assertEqual(result[self.confidential_doc.document_id], DocumentVisibility.blocked_by_sensitivity)

    def test_batch_visibility_contributor_confidential_tier_unlocks_both(self):
        grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.own_team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            tier=GrantTier.contributor_confidential, status=AccessRequestStatus.approved,
            decided_at=datetime.now(timezone.utc), expires_at=None,
        )
        self.db.add(grant)
        self.db.commit()
        ctx = build_authorization_context(self.db, self.viewer.user_id, self.project.project_id)
        result = classify_documents_visibility(self.db, ctx, [self.public_doc.document_id, self.confidential_doc.document_id])
        self.assertEqual(result[self.public_doc.document_id], DocumentVisibility.fully_allowed)
        self.assertEqual(result[self.confidential_doc.document_id], DocumentVisibility.fully_allowed)
```

- [ ] **Step 3: Run to verify failures**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_stage_grant_batch_visibility.py -v`
Expected: `test_batch_visibility_without_grant_excludes_other_teams_doc` passes already; the other 3 FAIL (`stage_grant_tiers` doesn't exist yet on `AuthorizationContext` — `AttributeError`).

- [ ] **Step 4: Implement — `AuthorizationContext`**

Rewrite `app/services/authorization_context.py` in full:

```python
"""
Request-scoped authorization context for DocFlow AI.

Eliminates N+1 query loops across ABAC and RAG retrieval by pre-loading:
  - User identity, tenant_id, clearance, org-admin status
  - Project-admin status
  - All team memberships and roles for (user_id, project_id)
  - All accessible stage IDs (native TeamStageAccess + active stage grants)
  - Active confidential access grants for the user's teams/documents
  - Active stage grant tiers, for confidential unlock within a stage grant

Authoritative decisions remain in PostgreSQL. This context is strictly internal
and NEVER exposed as an argument to LLMs.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import SensitivityLevel
from app.models.project import Project
from app.models.stage import Stage, TeamStageAccess
from app.models.team import (
    AccessRequest,
    AccessRequestScope,
    AccessRequestStatus,
    GrantTier,
    ProjectAdmin,
    TeamRole,
    UserTeamMembership,
)
from app.models.user import User


@dataclass
class AuthorizationContext:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    project_id: uuid.UUID

    is_org_admin: bool
    is_project_admin: bool

    team_ids: set[uuid.UUID] = field(default_factory=set)
    team_roles: dict[uuid.UUID, TeamRole] = field(default_factory=dict)

    accessible_stage_ids: set[uuid.UUID] = field(default_factory=set)
    clearance_level: SensitivityLevel | str | None = None
    active_confidential_grant_team_ids: set[uuid.UUID] = field(default_factory=set)
    active_confidential_grant_document_ids: set[uuid.UUID] = field(default_factory=set)
    # stage_id -> GrantTier for every active stage grant this user holds in
    # this project. Replaces the old tier-blind active_confidential_grant_stage_ids
    # set — confidential unlock now requires tier == contributor_confidential,
    # checked by the caller (classify_documents_visibility), not here.
    stage_grant_tiers: dict[uuid.UUID, GrantTier] = field(default_factory=dict)

    @property
    def is_admin(self) -> bool:
        return self.is_org_admin or self.is_project_admin


def build_authorization_context(
    db: Session,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
) -> AuthorizationContext:
    """
    Constructs a request-scoped AuthorizationContext for (user_id, project_id)
    in a small, bounded number of queries (no N+1 over documents).
    """
    user = db.get(User, user_id)
    if not user:
        raise ValueError(f"User {user_id} does not exist.")

    tenant_id = user.tenant_id
    is_org_admin = bool(user.is_org_admin)
    clearance = getattr(user, "clearance_level", None)

    # 1. Project Admin check
    is_project_admin = db.execute(
        select(ProjectAdmin.id).where(
            ProjectAdmin.user_id == user_id,
            ProjectAdmin.project_id == project_id,
        ).limit(1)
    ).scalar_one_or_none() is not None

    # 2. Team memberships within this project
    memberships = db.execute(
        select(UserTeamMembership).where(
            UserTeamMembership.user_id == user_id,
            UserTeamMembership.project_id == project_id,
        )
    ).scalars().all()

    team_ids = {m.team_id for m in memberships}
    team_roles = {m.team_id: m.role for m in memberships}

    # 3. Accessible stages — native TeamStageAccess, plus stage grants of
    # any tier (imported lazily to avoid a circular import with
    # access_control.py, which imports AuthorizationContext from here).
    from app.services.access_control import get_active_stage_grants_for_user

    stage_grant_tiers: dict[uuid.UUID, GrantTier] = {}
    if is_org_admin or is_project_admin:
        stage_rows = db.execute(
            select(Stage.stage_id).where(
                Stage.project_id == project_id,
                Stage.deleted_at.is_(None),
            )
        ).scalars().all()
        accessible_stage_ids = set(stage_rows)
    else:
        accessible_stage_ids = set()
        if team_ids:
            stage_rows = db.execute(
                select(TeamStageAccess.stage_id)
                .join(Stage, Stage.stage_id == TeamStageAccess.stage_id)
                .where(
                    TeamStageAccess.team_id.in_(team_ids),
                    Stage.deleted_at.is_(None),
                )
                .distinct()
            ).scalars().all()
            accessible_stage_ids = set(stage_rows)

        stage_grants = get_active_stage_grants_for_user(db, user_id)
        if stage_grants:
            grant_stage_ids_in_project = set(db.execute(
                select(Stage.stage_id).where(
                    Stage.stage_id.in_(stage_grants.keys()),
                    Stage.project_id == project_id,
                    Stage.deleted_at.is_(None),
                )
            ).scalars())
            accessible_stage_ids |= grant_stage_ids_in_project
            stage_grant_tiers = {
                sid: grant.tier for sid, grant in stage_grants.items()
                if sid in grant_stage_ids_in_project
            }

    # 4. Active non-expired confidential grants of document/team scope for
    # this user — stage-scope grants are handled entirely above via
    # stage_grant_tiers, which is tier-aware and revocation-safe in a way
    # this simpler per-row scan is not (see get_active_stage_grants_for_user's
    # docstring for why a plain "status == approved" scan is insufficient).
    active_confidential_grant_team_ids = set()
    active_confidential_grant_document_ids = set()
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

    return AuthorizationContext(
        user_id=user_id,
        tenant_id=tenant_id,
        project_id=project_id,
        is_org_admin=is_org_admin,
        is_project_admin=is_project_admin,
        team_ids=team_ids,
        team_roles=team_roles,
        accessible_stage_ids=accessible_stage_ids,
        clearance_level=clearance,
        active_confidential_grant_team_ids=active_confidential_grant_team_ids,
        active_confidential_grant_document_ids=active_confidential_grant_document_ids,
        stage_grant_tiers=stage_grant_tiers,
    )
```

- [ ] **Step 5: Implement — `classify_documents_visibility()`**

In `app/services/access_control.py`, replace the per-document evaluation loop (`access_control.py:527-563`, from `for doc_id, document in doc_map.items():` through the closing of that loop, just before `return out`):

```python
    for doc_id, document in doc_map.items():
        visible_teams = doc_visible_teams.get(doc_id, set())
        overlapping_teams = visible_teams & auth_context.team_ids
        stage_tier = auth_context.stage_grant_tiers.get(document.stage_id)

        if not overlapping_teams and stage_tier is None:
            out[doc_id] = DocumentVisibility.not_visible
            continue

        # Sensitivity check: Public & Internal
        if document.sensitivity_level in (SensitivityLevel.public, SensitivityLevel.internal):
            out[doc_id] = DocumentVisibility.fully_allowed
            continue

        # Sensitivity check: Confidential
        if document.sensitivity_level == SensitivityLevel.confidential:
            # Check highest role on any overlapping team
            roles = [auth_context.team_roles.get(tid) for tid in overlapping_teams]
            max_rank = max((_TEAM_ROLE_RANK[r] for r in roles if r in _TEAM_ROLE_RANK), default=-1)

            if max_rank >= _TEAM_ROLE_RANK[TeamRole.team_lead]:
                out[doc_id] = DocumentVisibility.fully_allowed
                continue

            # Active approved grant: document/team scope (tier-less, always
            # confidential-read), or a stage grant specifically at the
            # contributor_confidential tier.
            if (
                doc_id in auth_context.active_confidential_grant_document_ids
                or overlapping_teams & auth_context.active_confidential_grant_team_ids
                or stage_tier == GrantTier.contributor_confidential
            ):
                out[doc_id] = DocumentVisibility.fully_allowed
                continue

            out[doc_id] = DocumentVisibility.blocked_by_sensitivity
            continue

        # Restricted or unhandled sensitivity
        out[doc_id] = DocumentVisibility.blocked_by_sensitivity

    return out
```

- [ ] **Step 6: Run to verify all pass**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_stage_grant_batch_visibility.py tests/test_access_control_resolver.py -v`
Expected: 4 passed (new file) + 20 passed (resolver file, including the fixed batch test from Step 1).

- [ ] **Step 7: Full regression check**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/ -q --ignore=tests/test_access_requests_scoped.py`
Then separately: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_access_requests_scoped.py -q`
Expected: everything else in the suite green (the pre-existing 29 RLS/graph failures the original plan's ledger already documented as unrelated to this whole area are the only other expected non-passes), and the second command shows exactly the same 3 known pre-existing failures.

- [ ] **Step 8: Commit**

```bash
git add app/services/authorization_context.py app/services/access_control.py tests/test_access_control_resolver.py tests/test_stage_grant_batch_visibility.py
git commit -m "feat(access): thread stage grant tiers through AuthorizationContext for the batch/RAG path"
```

---

### Task 7: Upload enforcement — `document_persistence.py:_check_upload_access`

**Files:**
- Modify: `app/services/document_persistence.py:22-45` (imports), `:108-132` (`_check_upload_access`)
- Test: `tests/test_stage_grant_upload.py` (new)

**Interfaces:**
- Consumes: `resolve_stage_grant()` (Task 2).
- Produces: no signature change to `_check_upload_access(db, *, user_id, team_id, project_id, stage_id) -> Stage`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_stage_grant_upload.py`:

```python
import unittest
import uuid
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models.project import Project
from app.models.stage import Stage, TeamStageAccess
from app.models.team import AccessRequest, AccessRequestScope, AccessRequestStatus, GrantTier, Team, TeamRole, UserTeamMembership
from app.models.tenant import Tenant
from app.models.user import User
from app.services.document_persistence import PermissionDeniedError, StageNotFoundError, _check_upload_access


class TestStageGrantUploadAccess(unittest.TestCase):
    def setUp(self):
        with SessionLocal() as lookup_db:
            self.tenant = lookup_db.query(Tenant).first()
            tenant_id = self.tenant.tenant_id

        self.db = SessionLocal()
        self.db.info["tenant_id"] = str(tenant_id)

        self.project = Project(tenant_id=tenant_id, name=f"Upload Grant Test {uuid.uuid4().hex[:8]}")
        self.db.add(self.project)
        self.db.commit()

        self.routing_team = Team(project_id=self.project.project_id, name="Routing Team")
        self.other_team = Team(project_id=self.project.project_id, name="Other Team")
        self.db.add_all([self.routing_team, self.other_team])
        self.db.commit()

        self.stage = Stage(project_id=self.project.project_id, name="Upload Stage", order_index=1)
        self.db.add(self.stage)
        self.db.commit()
        self.db.add(TeamStageAccess(team_id=self.routing_team.team_id, stage_id=self.stage.stage_id))
        self.db.commit()

        # The grant holder has ZERO membership on either team — proving the
        # grant path works without any native role at all.
        self.grant_holder = User(email=f"gh-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.db.add(self.grant_holder)
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        self.db.query(AccessRequest).filter(AccessRequest.stage_id == self.stage.stage_id).delete()
        self.db.query(TeamStageAccess).filter(TeamStageAccess.stage_id == self.stage.stage_id).delete()
        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id == self.project.project_id).delete()
        self.db.query(User).filter(User.user_id == self.grant_holder.user_id).delete()
        self.db.query(Stage).filter(Stage.stage_id == self.stage.stage_id).delete()
        self.db.query(Team).filter(Team.project_id == self.project.project_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def _grant(self, tier):
        return AccessRequest(
            user_id=self.grant_holder.user_id, team_id=self.routing_team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            tier=tier, status=AccessRequestStatus.approved,
            decided_at=datetime.now(timezone.utc), expires_at=None,
        )

    def test_no_grant_no_membership_is_denied(self):
        with self.assertRaises(PermissionDeniedError):
            _check_upload_access(
                self.db, user_id=self.grant_holder.user_id, team_id=self.routing_team.team_id,
                project_id=self.project.project_id, stage_id=self.stage.stage_id,
            )

    def test_viewer_tier_grant_cannot_upload(self):
        self.db.add(self._grant(GrantTier.viewer))
        self.db.commit()
        with self.assertRaises(PermissionDeniedError):
            _check_upload_access(
                self.db, user_id=self.grant_holder.user_id, team_id=self.routing_team.team_id,
                project_id=self.project.project_id, stage_id=self.stage.stage_id,
            )

    def test_contributor_tier_grant_can_upload_as_its_own_routing_team(self):
        self.db.add(self._grant(GrantTier.contributor))
        self.db.commit()
        stage = _check_upload_access(
            self.db, user_id=self.grant_holder.user_id, team_id=self.routing_team.team_id,
            project_id=self.project.project_id, stage_id=self.stage.stage_id,
        )
        self.assertEqual(stage.stage_id, self.stage.stage_id)

    def test_contributor_tier_grant_cannot_upload_as_a_different_team(self):
        self.db.add(self._grant(GrantTier.contributor))
        self.db.commit()
        with self.assertRaises(PermissionDeniedError):
            _check_upload_access(
                self.db, user_id=self.grant_holder.user_id, team_id=self.other_team.team_id,
                project_id=self.project.project_id, stage_id=self.stage.stage_id,
            )
```

- [ ] **Step 2: Run to verify failures**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_stage_grant_upload.py -v`
Expected: `test_no_grant_no_membership_is_denied` passes already; `test_viewer_tier_grant_cannot_upload` passes already (both currently deny for the right reason, just not via the new logic yet); `test_contributor_tier_grant_can_upload_as_its_own_routing_team` FAILS (raises `PermissionDeniedError` instead of returning); `test_contributor_tier_grant_cannot_upload_as_a_different_team` passes already.

- [ ] **Step 3: Implement**

In `app/services/document_persistence.py`, change the import line (currently `app/services/document_persistence.py:38`):
```python
from app.services.access_control import has_permission, has_stage_access, resolve_sensitivity
```
to:
```python
from app.models.team import GrantTier
from app.services.access_control import has_permission, has_stage_access, resolve_sensitivity, resolve_stage_grant
```

Replace `_check_upload_access()` (`document_persistence.py:108-132`):
```python
def _check_upload_access(
    db: Session, *, user_id: uuid.UUID, team_id: uuid.UUID, project_id: uuid.UUID, stage_id: uuid.UUID,
) -> Stage:
    """
    Shared gate for both creation paths: has_permission + has_stage_access
    (native team-role path), OR an active contributor/contributor_confidential
    stage grant whose OWN routing team_id matches the team_id being uploaded
    as (spec §3.2 — tier and team_id are one entitlement, checked together;
    a grant never authorizes uploading as an arbitrary team).
    """
    stage = db.get(Stage, stage_id)
    if stage is None or stage.project_id != project_id or stage.deleted_at is not None:
        raise StageNotFoundError(
            f"Stage {stage_id} does not exist in this project (or has been deleted)."
        )

    native_ok = (
        has_permission(db, user_id, "upload", team_id, project_id)
        and has_stage_access(db, user_id, team_id, stage_id, project_id)
    )
    if not native_ok:
        grant = resolve_stage_grant(db, user_id, stage_id)
        grant_ok = (
            grant is not None
            and grant.tier in (GrantTier.contributor, GrantTier.contributor_confidential)
            and grant.team_id == team_id
        )
        if not grant_ok:
            raise PermissionDeniedError(
                f"You do not have permission to upload to the '{stage.name}' stage as this team."
            )

    return stage
```

- [ ] **Step 4: Run to verify all pass**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_stage_grant_upload.py -v`
Expected: 4 passed.

- [ ] **Step 5: Regression check**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_access_control_resolver.py tests/test_stage_grant_resolver.py tests/test_stage_grant_document_visibility.py tests/test_stage_grant_batch_visibility.py tests/test_grant_enforcement.py -q`
Expected: all green (the upload change touches a different function than anything these files exercise, but confirms nothing else broke).

- [ ] **Step 6: Commit**

```bash
git add app/services/document_persistence.py tests/test_stage_grant_upload.py
git commit -m "feat(access): allow contributor-tier stage grants to upload as their own routing team"
```

---

### Task 8: Revision enforcement — `document_review.py:review_message`

**Files:**
- Modify: `app/routers/document_review.py:26-40` (imports), `:419-448` (`review_message`) — extract a new `_check_revision_permission()` helper
- Test: `tests/test_stage_grant_revision_permission.py` (new)

**Interfaces:**
- Consumes: `resolve_stage_grant()` (Task 2).
- Produces: `_check_revision_permission(db: Session, user_id: uuid.UUID, document: Document) -> None` (raises `HTTPException(403)` on denial), extracted so it's unit-testable without invoking the LLM-backed `run_review_turn()` the rest of `review_message` depends on.

- [ ] **Step 1: Write the failing test**

Create `tests/test_stage_grant_revision_permission.py`:

```python
import unittest
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from app.database import SessionLocal
from app.models.document import Document, SensitivityLevel
from app.models.project import Project
from app.models.stage import Stage, TeamStageAccess
from app.models.team import AccessRequest, AccessRequestScope, AccessRequestStatus, GrantTier, Team, UserTeamMembership
from app.models.tenant import Tenant
from app.models.user import User
from app.routers.document_review import _check_revision_permission


class TestStageGrantRevisionPermission(unittest.TestCase):
    def setUp(self):
        with SessionLocal() as lookup_db:
            self.tenant = lookup_db.query(Tenant).first()
            tenant_id = self.tenant.tenant_id

        self.db = SessionLocal()
        self.db.info["tenant_id"] = str(tenant_id)

        self.project = Project(tenant_id=tenant_id, name=f"Revision Grant Test {uuid.uuid4().hex[:8]}")
        self.db.add(self.project)
        self.db.commit()

        self.routing_team = Team(project_id=self.project.project_id, name="Routing Team")
        self.db.add(self.routing_team)
        self.db.commit()

        self.stage = Stage(project_id=self.project.project_id, name="Revision Stage", order_index=1)
        self.db.add(self.stage)
        self.db.commit()
        self.db.add(TeamStageAccess(team_id=self.routing_team.team_id, stage_id=self.stage.stage_id))
        self.db.commit()

        self.grant_holder = User(email=f"rgh-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.db.add(self.grant_holder)
        self.db.commit()

        self.document = Document(
            tenant_id=tenant_id, project_id=self.project.project_id, stage_id=self.stage.stage_id,
            uploaded_by=self.grant_holder.user_id, uploaded_as_team_id=self.routing_team.team_id,
            sensitivity_level=SensitivityLevel.internal,
            original_filename="revise-me.md", mime_type="text/markdown",
        )
        self.db.add(self.document)
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        self.db.query(AccessRequest).filter(AccessRequest.stage_id == self.stage.stage_id).delete()
        self.db.query(Document).filter(Document.document_id == self.document.document_id).update({"current_version_id": None})
        self.db.query(Document).filter(Document.document_id == self.document.document_id).delete()
        self.db.query(TeamStageAccess).filter(TeamStageAccess.stage_id == self.stage.stage_id).delete()
        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id == self.project.project_id).delete()
        self.db.query(User).filter(User.user_id == self.grant_holder.user_id).delete()
        self.db.query(Stage).filter(Stage.stage_id == self.stage.stage_id).delete()
        self.db.query(Team).filter(Team.project_id == self.project.project_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def test_no_grant_is_denied(self):
        with self.assertRaises(HTTPException) as ctx:
            _check_revision_permission(self.db, self.grant_holder.user_id, self.document)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_contributor_tier_grant_via_own_routing_team_is_allowed(self):
        grant = AccessRequest(
            user_id=self.grant_holder.user_id, team_id=self.routing_team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            tier=GrantTier.contributor, status=AccessRequestStatus.approved,
            decided_at=datetime.now(timezone.utc), expires_at=None,
        )
        self.db.add(grant)
        self.db.commit()
        _check_revision_permission(self.db, self.grant_holder.user_id, self.document)  # does not raise
```

- [ ] **Step 2: Run to verify failures**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_stage_grant_revision_permission.py -v`
Expected: FAIL with `ImportError: cannot import name '_check_revision_permission'`.

- [ ] **Step 3: Implement**

In `app/routers/document_review.py`, change the import lines (currently `app/routers/document_review.py:38-39`):
```python
from app.models.team import Team
from app.services.access_control import can_edit_document, can_view_document, has_permission
```
to:
```python
from app.models.team import GrantTier, Team
from app.services.access_control import can_edit_document, can_view_document, has_permission, resolve_stage_grant
```

Add this new function directly above `review_message` (`app/routers/document_review.py:419`):
```python
def _check_revision_permission(db: Session, user_id: uuid.UUID, document: Document) -> None:
    """
    Same bar as uploading: the acting user must have "upload" rights on
    this document's team (org_admin/project_admin bypass, as everywhere
    else), OR an active contributor/contributor_confidential stage grant
    routed through that exact team (spec §3.2 — tier and team_id checked
    together). Deliberately not scoped to "only the original uploader" —
    any teammate (native or grant-derived) with upload rights on this
    stage/team can continue the review. Raises HTTPException(403) if
    neither applies.
    """
    if has_permission(db, user_id, "upload", document.uploaded_as_team_id, document.project_id):
        return
    grant = resolve_stage_grant(db, user_id, document.stage_id)
    grant_ok = (
        grant is not None
        and grant.tier in (GrantTier.contributor, GrantTier.contributor_confidential)
        and grant.team_id == document.uploaded_as_team_id
    )
    if not grant_ok:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to revise documents for this team.",
        )
```

Then replace the inline check inside `review_message` (`app/routers/document_review.py:431-439`, currently):
```python
    # Same bar as uploading: the acting user must still have "upload" rights
    # on this document's team (org_admin/project_admin bypass, as everywhere
    # else). Deliberately not scoped to "only the original uploader" — any
    # teammate with upload rights on this stage/team can continue the review.
    if not has_permission(db, identity.user_id, "upload", document.uploaded_as_team_id, document.project_id):
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to revise documents for this team.",
        )
```
with:
```python
    _check_revision_permission(db, identity.user_id, document)
```

- [ ] **Step 4: Run to verify tests pass**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_stage_grant_revision_permission.py -v`
Expected: 2 passed.

- [ ] **Step 5: Regression check**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_access_control_resolver.py tests/test_stage_grant_resolver.py tests/test_stage_grant_document_visibility.py tests/test_stage_grant_batch_visibility.py tests/test_stage_grant_upload.py tests/test_grant_enforcement.py -q`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add app/routers/document_review.py tests/test_stage_grant_revision_permission.py
git commit -m "feat(access): allow contributor-tier stage grants to revise documents via review_message"
```

---

### Task 9: Submit enforcement — `workflow.py:submit_for_review` (the gap missing from the original spec draft)

**Files:**
- Modify: `app/services/workflow.py:41-46` (imports), `:159-183` (`submit_for_review`)
- Test: `tests/test_stage_grant_submit.py` (new)

**Interfaces:**
- Consumes: `resolve_stage_grant()` (Task 2).
- Produces: no signature change to `submit_for_review(db, document_id, user_id, team_id, project_id, role) -> WorkflowState`.

**Why this task exists:** found by inventorying every `has_permission()` action string in the codebase — `"submit"` shares the exact same `contributor` minimum role as `"upload"` (`access_control.py:64`, comment: "submit is the same bar as upload"). Without this fix, a contributor-tier grant holder could upload a document into their granted stage but never submit it for review, leaving it stuck in `draft` forever.

- [ ] **Step 1: Write the failing test**

Create `tests/test_stage_grant_submit.py`:

```python
import unittest
import uuid
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models.document import Document, SensitivityLevel
from app.models.project import Project
from app.models.stage import Stage, TeamStageAccess
from app.models.team import AccessRequest, AccessRequestScope, AccessRequestStatus, GrantTier, Team, UserTeamMembership
from app.models.tenant import Tenant
from app.models.user import User
from app.models.workflow import WorkflowState, WorkflowStatus
from app.services.workflow import WorkflowPermissionError, submit_for_review


class TestStageGrantSubmit(unittest.TestCase):
    def setUp(self):
        with SessionLocal() as lookup_db:
            self.tenant = lookup_db.query(Tenant).first()
            tenant_id = self.tenant.tenant_id

        self.db = SessionLocal()
        self.db.info["tenant_id"] = str(tenant_id)

        self.project = Project(tenant_id=tenant_id, name=f"Submit Grant Test {uuid.uuid4().hex[:8]}")
        self.db.add(self.project)
        self.db.commit()

        self.routing_team = Team(project_id=self.project.project_id, name="Routing Team")
        self.db.add(self.routing_team)
        self.db.commit()

        self.stage = Stage(project_id=self.project.project_id, name="Submit Stage", order_index=1, requires_approval=True)
        self.db.add(self.stage)
        self.db.commit()
        self.db.add(TeamStageAccess(team_id=self.routing_team.team_id, stage_id=self.stage.stage_id))
        self.db.commit()

        self.grant_holder = User(email=f"sgh-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.db.add(self.grant_holder)
        self.db.commit()

        self.document = Document(
            tenant_id=tenant_id, project_id=self.project.project_id, stage_id=self.stage.stage_id,
            uploaded_by=self.grant_holder.user_id, uploaded_as_team_id=self.routing_team.team_id,
            sensitivity_level=SensitivityLevel.internal,
            original_filename="submit-me.md", mime_type="text/markdown",
        )
        self.db.add(self.document)
        self.db.commit()
        self.db.add(WorkflowState(document_id=self.document.document_id, state=WorkflowStatus.draft))
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        self.db.query(AccessRequest).filter(AccessRequest.stage_id == self.stage.stage_id).delete()
        self.db.query(WorkflowState).filter(WorkflowState.document_id == self.document.document_id).delete()
        self.db.query(Document).filter(Document.document_id == self.document.document_id).update({"current_version_id": None})
        self.db.query(Document).filter(Document.document_id == self.document.document_id).delete()
        self.db.query(TeamStageAccess).filter(TeamStageAccess.stage_id == self.stage.stage_id).delete()
        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id == self.project.project_id).delete()
        self.db.query(User).filter(User.user_id == self.grant_holder.user_id).delete()
        self.db.query(Stage).filter(Stage.stage_id == self.stage.stage_id).delete()
        self.db.query(Team).filter(Team.project_id == self.project.project_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def test_no_grant_is_denied(self):
        with self.assertRaises(WorkflowPermissionError):
            submit_for_review(
                self.db, self.document.document_id, self.grant_holder.user_id,
                self.routing_team.team_id, self.project.project_id, "viewer",
            )

    def test_contributor_tier_grant_can_submit(self):
        grant = AccessRequest(
            user_id=self.grant_holder.user_id, team_id=self.routing_team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            tier=GrantTier.contributor, status=AccessRequestStatus.approved,
            decided_at=datetime.now(timezone.utc), expires_at=None,
        )
        self.db.add(grant)
        self.db.commit()
        state = submit_for_review(
            self.db, self.document.document_id, self.grant_holder.user_id,
            self.routing_team.team_id, self.project.project_id, "viewer",
        )
        self.assertEqual(state.state, WorkflowStatus.pending_review)
```

- [ ] **Step 2: Run to verify failures**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_stage_grant_submit.py -v`
Expected: `test_no_grant_is_denied` passes already; `test_contributor_tier_grant_can_submit` FAILS with `WorkflowPermissionError`.

- [ ] **Step 3: Implement**

In `app/services/workflow.py`, change the import line (currently `app/services/workflow.py:43`):
```python
from app.services.access_control import has_permission, is_grant_only_confidential_access
```
to:
```python
from app.models.team import GrantTier
from app.services.access_control import has_permission, is_grant_only_confidential_access, resolve_stage_grant
```

Replace `submit_for_review()`'s body (`app/services/workflow.py:159-183`, from the `def submit_for_review(` line through the `if state.state not in (...)` check — leave everything from that `if` onward untouched):

```python
def submit_for_review(
    db: Session,
    document_id: uuid.UUID,
    user_id: uuid.UUID,
    team_id: uuid.UUID,
    project_id: uuid.UUID,
    role: str,
) -> WorkflowState:
    """
    draft -> pending_review, or rejected -> pending_review (resubmission after
    addressing the feedback). Requires 'submit' (contributor+), OR an active
    contributor/contributor_confidential stage grant routed through team_id
    (spec §3.2 — same OR pattern as upload/revision). On resubmission from
    'rejected', the stale rejection_reason is cleared.
    """
    state = _require_state(db, document_id)
    document = db.get(Document, document_id)

    native_ok = has_permission(db, user_id, "submit", team_id, project_id)
    if not native_ok:
        grant = resolve_stage_grant(db, user_id, document.stage_id) if document is not None else None
        grant_ok = (
            grant is not None
            and grant.tier in (GrantTier.contributor, GrantTier.contributor_confidential)
            and grant.team_id == team_id
        )
        if not grant_ok:
            raise WorkflowPermissionError(
                "You do not have permission to submit documents for review on this team."
            )

    if document is not None and is_grant_only_confidential_access(db, user_id, document):
        raise WorkflowPermissionError(
            "Your access to this document is read-only (granted via a confidential-access "
            "request, not your team role) — you cannot submit it for review."
        )
    if state.state not in (WorkflowStatus.draft, WorkflowStatus.rejected):
```

(The line above, `if state.state not in (WorkflowStatus.draft, WorkflowStatus.rejected):`, is where the pre-existing code continues unchanged — this replacement only touches the two blocks before it.)

- [ ] **Step 4: Run to verify tests pass**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_stage_grant_submit.py -v`
Expected: 2 passed.

- [ ] **Step 5: Full regression check**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_access_control_resolver.py tests/test_stage_grant_resolver.py tests/test_stage_grant_document_visibility.py tests/test_stage_grant_batch_visibility.py tests/test_stage_grant_upload.py tests/test_stage_grant_revision_permission.py tests/test_grant_enforcement.py -q`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add app/services/workflow.py tests/test_stage_grant_submit.py
git commit -m "feat(access): allow contributor-tier stage grants to submit documents for review"
```

---

### Task 10: Re-request dedup — `access_requests_service.py` stage-scope path

**Files:**
- Modify: `app/services/access_requests_service.py:212-223`
- Test: `tests/test_access_requests_scoped.py` (append)

**Interfaces:**
- Consumes: `AccessRequestStatus`, `AccessRequestScope` (existing).
- Produces: no signature change to `request_confidential_access()`.

**Why (spec §5.1):** the existing dedup check blocks a second request for a target once *any* grant (`"granted"` or `"pending"`) exists. Under the tiered model this must loosen for stage scope specifically — someone with an active `viewer` grant needs to be able to request `contributor` next, without waiting for the old grant to expire. Only a duplicate *pending* request should still be blocked (backed by Task 1's DB-level partial unique index). Document/team-scope dedup is untouched.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_access_requests_scoped.py` (it already has `self.engineering`, `self.stage`, `self.contributor` fixtures set up in `setUp`):

```python
    def test_stage_scope_active_grant_does_not_block_a_new_request(self):
        from datetime import timedelta
        from app.models.team import AccessRequestStatus, GrantTier

        self.db.add(UserTeamMembership(
            user_id=self.contributor.user_id, team_id=self.engineering.team_id,
            project_id=self.project.project_id, role=TeamRole.contributor,
        ))
        self.db.add(TeamStageAccess(team_id=self.engineering.team_id, stage_id=self.stage.stage_id))
        self.db.commit()

        existing_grant = AccessRequest(
            user_id=self.contributor.user_id, team_id=self.engineering.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            tier=GrantTier.viewer, status=AccessRequestStatus.approved,
            decided_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        self.db.add(existing_grant)
        self.db.commit()

        req = request_confidential_access(
            self.db, user_id=self.contributor.user_id, stage_id=self.stage.stage_id,
            expected_tenant_id=self.tenant.tenant_id,
        )
        self.assertEqual(req.scope, AccessRequestScope.stage)
        self.assertEqual(req.status, AccessRequestStatus.pending)

    def test_stage_scope_pending_request_still_blocks_a_duplicate(self):
        self.db.add(UserTeamMembership(
            user_id=self.contributor.user_id, team_id=self.engineering.team_id,
            project_id=self.project.project_id, role=TeamRole.contributor,
        ))
        self.db.add(TeamStageAccess(team_id=self.engineering.team_id, stage_id=self.stage.stage_id))
        self.db.commit()

        request_confidential_access(
            self.db, user_id=self.contributor.user_id, stage_id=self.stage.stage_id,
            expected_tenant_id=self.tenant.tenant_id,
        )
        with self.assertRaises(AccessRequestError) as ctx:
            request_confidential_access(
                self.db, user_id=self.contributor.user_id, stage_id=self.stage.stage_id,
                expected_tenant_id=self.tenant.tenant_id,
            )
        self.assertEqual(ctx.exception.status_code, 409)
```

(This file's imports already include `datetime, timezone` and `AccessRequest, AccessRequestScope, AccessRequestStatus, Team, TeamRole, UserTeamMembership` — `timedelta` and `GrantTier` are imported locally inside the first test above since the file-level import doesn't have them yet.)

- [ ] **Step 2: Run to verify the first test fails**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_access_requests_scoped.py -v -k stage_scope_active_grant`
Expected: FAIL — today's code raises `AccessRequestError` ("already have a granted confidential-access request") instead of returning a new pending request.

- [ ] **Step 3: Implement**

In `app/services/access_requests_service.py`, replace lines 212-223 (from `from app.services.access_control import resolve_effective_access` through the closing of the `if effective.status in (...)` block):

```python
    if scope == AccessRequestScope.stage:
        # Stage-scope dedup (spec §5.1): only a duplicate PENDING request is
        # blocked here — an existing ACTIVE grant (any tier) no longer blocks
        # a fresh request, since the whole point of tiers is requesting an
        # upgrade without waiting for the current grant to expire. Task 1's
        # partial unique index (uq_access_requests_one_pending_stage_request)
        # backs this up against a race between two simultaneous requests.
        existing_pending = db.execute(
            select(AccessRequest).where(
                AccessRequest.user_id == user_id,
                AccessRequest.scope == AccessRequestScope.stage,
                AccessRequest.stage_id == stage_id,
                AccessRequest.status == AccessRequestStatus.pending,
            )
        ).scalar_one_or_none()
        if existing_pending is not None:
            raise AccessRequestError(
                "You already have a pending confidential-access request for this target.",
                status_code=409,
            )
    else:
        from app.services.access_control import resolve_effective_access
        effective = resolve_effective_access(
            db, user_id,
            document_id=document_id if scope == AccessRequestScope.document else None,
            team_id=resolved_team_id if scope == AccessRequestScope.team else None,
        )
        if effective.status in ("granted", "pending"):
            raise AccessRequestError(
                f"You already have a {effective.status} confidential-access request for this target.",
                status_code=409,
            )
```

- [ ] **Step 4: Run to verify both new tests pass**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_access_requests_scoped.py -v -k "stage_scope_active_grant or stage_scope_pending_request"`
Expected: 2 passed.

- [ ] **Step 5: Full regression check**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_access_requests_scoped.py -q`
Expected: exactly the same 3 known pre-existing failures (document/team-scope, untouched by this task) — every stage-scope test, old and new, passes.

- [ ] **Step 6: Commit**

```bash
git add app/services/access_requests_service.py tests/test_access_requests_scoped.py
git commit -m "feat(access): let stage-scope requests upgrade an active grant instead of blocking on it"
```

---

### Task 11: `access_requests.py` router — approver tier/duration, requester role, revoke endpoint

**Files:**
- Modify: `app/routers/access_requests.py` (whole file — see steps below for exact regions)
- Test: `tests/test_access_requests_router.py` (append — this is the file Task 4 created)

**Interfaces:**
- Consumes: `GrantTier`, `GrantDuration` (Task 1).
- Produces: `AccessRequestOut` gains `requester_role: str | None` and `tier: str | None`; new `POST /access-requests/{request_id}/revoke` endpoint; `approve_access_request` now accepts an optional JSON body `{tier, duration}`, required (422 if missing) when the target request is stage-scope.

**Design note:** `team_name` (already on `AccessRequestOut`) IS the requester's own team for every scope — `_derive_stage_team()` only ever returns a team the requester already belongs to (Task 1.1 of the spec), and document/team-scope requests use the requester's own team directly. So the "approver sees requester's team + role" requirement only needs ONE new field, `requester_role` — `team_name` already covers the team half.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_access_requests_router.py` (new class in the same file). It reuses this file's existing top-level imports as-is (`Stage`, `TeamStageAccess`, `TeamRole`, `GrantTier`, `datetime`, `timedelta`, `timezone` are all already imported by Task 4's version of this file) — the only new import needed is `GrantDuration`, added to the existing `from app.models.team import ...` line at the top of the file:

```python
# app/models/team import line at the top of the file becomes:
# from app.models.team import AccessRequest, AccessRequestScope, AccessRequestStatus, GrantDuration, GrantTier, Team, TeamRole, UserTeamMembership


class TestApproveAndRevokeStageGrant(unittest.TestCase):
    def setUp(self):
        self.db = SessionLocal()
        self.tenant = self.db.query(Tenant).first()
        tenant_id = self.tenant.tenant_id

        self.project = Project(tenant_id=tenant_id, name=f"Approve Revoke Test {uuid.uuid4().hex[:8]}")
        self.db.add(self.project)
        self.db.commit()

        self.team = Team(project_id=self.project.project_id, name="Approve Team")
        self.db.add(self.team)
        self.db.commit()

        self.stage = Stage(project_id=self.project.project_id, name="Approve Stage", order_index=1)
        self.db.add(self.stage)
        self.db.commit()
        self.db.add(TeamStageAccess(team_id=self.team.team_id, stage_id=self.stage.stage_id))
        self.db.commit()

        self.team_lead = User(email=f"lead-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.requester = User(email=f"req-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.db.add_all([self.team_lead, self.requester])
        self.db.commit()
        self.db.add(UserTeamMembership(
            user_id=self.team_lead.user_id, team_id=self.team.team_id,
            project_id=self.project.project_id, role=TeamRole.team_lead,
        ))
        self.db.add(UserTeamMembership(
            user_id=self.requester.user_id, team_id=self.team.team_id,
            project_id=self.project.project_id, role=TeamRole.viewer,
        ))
        self.db.commit()

        self.request = AccessRequest(
            user_id=self.requester.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            status=AccessRequestStatus.pending,
        )
        self.db.add(self.request)
        self.db.commit()

        self.lead_identity = resolve_identity(self.db, self.team_lead.user_id)
        app.dependency_overrides[get_current_user] = lambda: self.lead_identity
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.rollback()
        self.db.query(AccessRequest).filter(AccessRequest.team_id == self.team.team_id).delete()
        self.db.query(TeamStageAccess).filter(TeamStageAccess.stage_id == self.stage.stage_id).delete()
        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id == self.project.project_id).delete()
        self.db.query(User).filter(User.user_id.in_([self.team_lead.user_id, self.requester.user_id])).delete(synchronize_session=False)
        self.db.query(Stage).filter(Stage.stage_id == self.stage.stage_id).delete()
        self.db.query(Team).filter(Team.team_id == self.team.team_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def test_approving_stage_request_without_tier_and_duration_is_422(self):
        resp = self.client.post(f"/access-requests/{self.request.request_id}/approve", json={})
        self.assertEqual(resp.status_code, 422)

    def test_approving_with_tier_and_duration_sets_expires_at_and_serializes_both(self):
        resp = self.client.post(
            f"/access-requests/{self.request.request_id}/approve",
            json={"tier": "contributor", "duration": "week_1"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["tier"], "contributor")
        self.assertEqual(body["requester_role"], "viewer")
        self.assertEqual(body["status"], "approved")
        expires_at = datetime.fromisoformat(body["expires_at"])
        expected = datetime.now(timezone.utc) + timedelta(days=7)
        self.assertLess(abs((expires_at - expected).total_seconds()), 30)

    def test_revoke_transitions_approved_to_revoked(self):
        self.client.post(
            f"/access-requests/{self.request.request_id}/approve",
            json={"tier": "contributor", "duration": "unlimited"},
        )
        resp = self.client.post(f"/access-requests/{self.request.request_id}/revoke")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "revoked")

        from app.services.access_control import resolve_stage_grant
        self.assertIsNone(resolve_stage_grant(self.db, self.requester.user_id, self.stage.stage_id))

    def test_revoke_on_a_pending_request_is_409(self):
        resp = self.client.post(f"/access-requests/{self.request.request_id}/revoke")
        self.assertEqual(resp.status_code, 409)
```

- [ ] **Step 2: Run to verify failures**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_access_requests_router.py -v -k TestApproveAndRevokeStageGrant`
Expected: `test_approving_stage_request_without_tier_and_duration_is_422` FAILS (currently approves unconditionally, 200 not 422); `test_approving_with_tier_and_duration_...` FAILS (`tier`/`requester_role` keys don't exist in the response yet — `KeyError` or `None`); `test_revoke_transitions_approved_to_revoked` and `test_revoke_on_a_pending_request_is_409` both FAIL with 404 (`/revoke` doesn't exist yet).

- [ ] **Step 3: Implement — models and imports**

In `app/routers/access_requests.py`, change the import block (currently `app/routers/access_requests.py:31-36`):
```python
from app.models.team import (
    AccessRequest,
    AccessRequestScope,
    AccessRequestStatus,
    Team,
)
```
to:
```python
from app.models.team import (
    AccessRequest,
    AccessRequestScope,
    AccessRequestStatus,
    GrantDuration,
    GrantTier,
    Team,
)
```
and change (currently `app/routers/access_requests.py:38`):
```python
from app.services.access_control import has_permission
```
to:
```python
from app.services.access_control import _get_team_membership, has_permission
```

Add, near the top of the file (after the imports, before `router = APIRouter(...)`):
```python
_DURATION_LABELS: dict[GrantDuration, str] = {
    GrantDuration.hours_72: "72 hours",
    GrantDuration.week_1: "1 week",
    GrantDuration.month_1: "1 month",
    GrantDuration.unlimited: "no expiration",
}
_EXPIRES_AT_FOR_DURATION = {
    GrantDuration.hours_72: lambda now: now + timedelta(hours=72),
    GrantDuration.week_1: lambda now: now + timedelta(days=7),
    GrantDuration.month_1: lambda now: now + timedelta(days=30),
    GrantDuration.unlimited: lambda now: None,
}
```

Replace `AccessRequestOut` (`app/routers/access_requests.py:59-76`):
```python
class AccessRequestOut(BaseModel):
    request_id: str
    user_id: str
    requester_email: str | None
    requester_role: str | None  # the requester's TeamRole on `team_id` — approver visibility
    team_id: str
    team_name: str
    project_id: str
    project_name: str
    scope: str  # "document" | "stage" | "team"
    target_id: str  # document_id, stage_id, or team_id, matching `scope`
    target_name: str  # filename, stage name, or team name, matching `scope`
    tier: str | None  # GrantTier value — set only on a decided stage-scope request
    grant_duration_label: str  # human-readable duration, or a prompt to choose one if still pending
    status: str
    requested_at: str | None
    decided_at: str | None
    expires_at: str | None
    active: bool  # approved and not expired — i.e. currently grants clearance
```

Add a new request body model, right after `CreateAccessRequest`:
```python
class ApproveAccessRequestBody(BaseModel):
    tier: GrantTier | None = None
    duration: GrantDuration | None = None
```

- [ ] **Step 4: Implement — `_serialize()`**

Replace `_serialize()` (`app/routers/access_requests.py:118-159`):
```python
def _serialize(db: Session, r: AccessRequest, *, team=None, project=None, requester=None) -> AccessRequestOut:
    from app.models.document import Document
    from app.models.stage import Stage
    from app.services.access_requests_service import DOCUMENT_GRANT_TTL_HOURS, GRANT_TTL_DAYS

    team = team or db.get(Team, r.team_id)
    project = project or (db.get(Project, team.project_id) if team else None)
    requester = requester or db.get(User, r.user_id)
    requester_membership = _get_team_membership(db, r.user_id, r.team_id)

    if r.scope == AccessRequestScope.document:
        target_id = str(r.document_id)
        doc = db.get(Document, r.document_id)
        target_name = doc.original_filename if doc else "(deleted document)"
        grant_duration_label = f"{DOCUMENT_GRANT_TTL_HOURS} hours"
    elif r.scope == AccessRequestScope.stage:
        target_id = str(r.stage_id)
        stage = db.get(Stage, r.stage_id)
        target_name = stage.name if stage else "(deleted stage)"
        grant_duration_label = (
            _DURATION_LABELS[r.duration] if r.duration is not None
            else "Approver chooses tier & duration"
        )
    else:
        target_id = str(r.team_id)
        target_name = team.name if team else "(unknown)"
        grant_duration_label = f"{GRANT_TTL_DAYS} days"

    return AccessRequestOut(
        request_id=str(r.request_id),
        user_id=str(r.user_id),
        requester_email=requester.email if requester else None,
        requester_role=requester_membership.role.value if requester_membership else None,
        team_id=str(r.team_id),
        team_name=team.name if team else "(unknown)",
        project_id=str(team.project_id) if team else "",
        project_name=project.name if project else "(unknown)",
        scope=r.scope.value,
        target_id=target_id,
        target_name=target_name,
        tier=r.tier.value if r.tier else None,
        grant_duration_label=grant_duration_label,
        status=r.status.value,
        requested_at=r.requested_at.isoformat() if r.requested_at else None,
        decided_at=r.decided_at.isoformat() if r.decided_at else None,
        expires_at=r.expires_at.isoformat() if r.expires_at else None,
        active=(r.status == AccessRequestStatus.approved and not _is_expired(r)),
    )
```

- [ ] **Step 5: Implement — `_decide()` and the approve/deny/revoke endpoints**

Replace `_decide()` and the two endpoints below it (`app/routers/access_requests.py:216-274`, from `def _decide(` through the end of `deny_access_request`):
```python
def _decide(
    db: Session, identity: ResolvedIdentity, request_id: uuid.UUID, *,
    approve: bool, tier: GrantTier | None = None, duration: GrantDuration | None = None,
) -> AccessRequestOut:
    r = db.get(AccessRequest, request_id)
    team = db.get(Team, r.team_id) if r else None
    project = db.get(Project, team.project_id) if team else None
    if r is None or team is None or project is None or project.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Request not found")
    if not has_permission(
        db, identity.user_id, "approve_access_request", team.team_id, team.project_id
    ):
        raise HTTPException(
            status_code=403, detail=f"You cannot review access requests for team '{team.name}'"
        )
    if r.status != AccessRequestStatus.pending:
        raise HTTPException(status_code=409, detail=f"This request has already been {r.status.value}.")

    if approve and r.scope == AccessRequestScope.stage and (tier is None or duration is None):
        raise HTTPException(
            status_code=422,
            detail="Approving a stage-scope request requires both tier and duration.",
        )

    from app.services.access_requests_service import DOCUMENT_GRANT_TTL_HOURS

    now = datetime.now(timezone.utc)
    r.status = AccessRequestStatus.approved if approve else AccessRequestStatus.denied
    r.decided_by = identity.user_id
    r.decided_at = now
    if approve:
        if r.scope == AccessRequestScope.stage:
            r.tier = tier
            r.duration = duration
            r.expires_at = _EXPIRES_AT_FOR_DURATION[duration](now)
        else:
            r.expires_at = (
                now + timedelta(hours=DOCUMENT_GRANT_TTL_HOURS) if r.scope == AccessRequestScope.document
                else now + timedelta(days=_GRANT_TTL_DAYS)
            )
    else:
        r.expires_at = None
    record_audit(
        db, actor_id=identity.user_id,
        action="APPROVE_ACCESS_REQUEST" if approve else "DENY_ACCESS_REQUEST",
        resource_type="access_request", resource_id=r.request_id,
        details={"team": team.name, "requester_id": str(r.user_id)},
    )
    notify_access_request_decided(
        db, requester_id=r.user_id, team_name=team.name, project_id=project.project_id,
        request_id=r.request_id, approved=approve,
    )
    db.commit()
    db.refresh(r)
    return _serialize(db, r, team=team, project=project)


@router.post("/{request_id}/approve", response_model=AccessRequestOut)
def approve_access_request(
    request_id: uuid.UUID,
    body: ApproveAccessRequestBody = ApproveAccessRequestBody(),
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    return _decide(db, identity, request_id, approve=True, tier=body.tier, duration=body.duration)


@router.post("/{request_id}/deny", response_model=AccessRequestOut)
def deny_access_request(
    request_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    return _decide(db, identity, request_id, approve=False)


@router.post("/{request_id}/revoke", response_model=AccessRequestOut)
def revoke_access_request(
    request_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    """
    approved -> revoked, immediately and permanently (spec §5.3 — a later
    older grant for the same stage does NOT reactivate; see
    get_active_stage_grants_for_user()'s docstring). Row-locked via
    SELECT ... FOR UPDATE so two concurrent revoke calls can't both pass
    the status check and double-write the audit trail.
    """
    r = db.execute(
        select(AccessRequest).where(AccessRequest.request_id == request_id).with_for_update()
    ).scalar_one_or_none()
    team = db.get(Team, r.team_id) if r else None
    project = db.get(Project, team.project_id) if team else None
    if r is None or team is None or project is None or project.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Request not found")
    if not has_permission(
        db, identity.user_id, "approve_access_request", team.team_id, team.project_id
    ):
        raise HTTPException(
            status_code=403, detail=f"You cannot review access requests for team '{team.name}'"
        )
    if r.status != AccessRequestStatus.approved:
        raise HTTPException(
            status_code=409, detail=f"This request is {r.status.value}, not approved — nothing to revoke."
        )

    r.status = AccessRequestStatus.revoked
    r.revoked_at = datetime.now(timezone.utc)
    r.revoked_by = identity.user_id
    record_audit(
        db, actor_id=identity.user_id, action="REVOKE_ACCESS_GRANT",
        resource_type="access_request", resource_id=r.request_id,
        details={"team": team.name, "requester_id": str(r.user_id)},
    )
    db.commit()
    db.refresh(r)
    return _serialize(db, r, team=team, project=project)
```

- [ ] **Step 6: Run to verify all pass**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_access_requests_router.py -v`
Expected: 6 passed (2 from Task 4 + 4 new).

- [ ] **Step 7: Full regression check**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/ -q`
Expected: everything from Tasks 1-10 still green; `test_access_requests_scoped.py` still exactly the 3 known pre-existing failures; the pre-existing ~29 RLS/graph failures unrelated to this whole area (same ones the original plan's ledger already documented).

- [ ] **Step 8: Commit**

```bash
git add app/routers/access_requests.py tests/test_access_requests_router.py
git commit -m "feat(access): approver-chosen tier/duration on approval, requester role visibility, revoke endpoint"
```

---

### Task 12: Frontend — `api.js` additions

**Files:**
- Modify: `frontend/src/lib/api.js:480-504` (`accessRequestsApi`)

**Interfaces:**
- Consumes: `POST /access-requests/{id}/approve` (now accepting an optional `{tier, duration}` body), `POST /access-requests/{id}/revoke` (Task 11).
- Produces: `accessRequestsApi.approve(id, { tier, duration })`, `accessRequestsApi.revoke(id)` — consumed by Task 13.

No backend test needed for this task (pure frontend, backend already covered in Task 11) — verified by a manual build check and consumed directly by Task 13's browser walkthrough.

- [ ] **Step 1: Implement**

Replace the `accessRequestsApi` block (`frontend/src/lib/api.js:480-504`):
```js
// ---------- Confidential-access requests (real backend) ----------
export const accessRequestsApi = {
  // the caller's own requests, newest first — { status, active, team_name, ... }
  mine: () => request('/access-requests/mine'),
  // create a pending request for one team
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
  // requests the caller may decide (team_lead on that team / project_admin / org_admin)
  pending: () => request('/access-requests/pending'),
  // tier/duration are REQUIRED by the backend for a stage-scope request
  // (422 otherwise) — omit both for document/team-scope approvals, which
  // still use the old fixed-TTL, always-read-only behavior.
  approve: (id, { tier, duration } = {}) =>
    request(`/access-requests/${encodeURIComponent(id)}/approve`, {
      method: 'POST',
      body: tier && duration ? { tier, duration } : {},
    }),
  deny: (id) => request(`/access-requests/${encodeURIComponent(id)}/deny`, { method: 'POST' }),
  // approved -> revoked, immediately and permanently — no undo besides the
  // requester asking again.
  revoke: (id) => request(`/access-requests/${encodeURIComponent(id)}/revoke`, { method: 'POST' }),
};
```

- [ ] **Step 2: Confirm the build is still clean**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage\frontend" && npx vite build`
Expected: clean build, no new warnings/errors (only pre-existing bundle-size warnings, matching every prior frontend task in this whole feature's history).

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/api.js
git commit -m "feat(access): wire tier/duration approval and revoke into the API client"
```

---

### Task 13: Frontend — `AdminPage.jsx` — requester role, tier/duration selectors, Revoke

**Files:**
- Modify: `frontend/src/pages/AdminPage.jsx:1035-1065` (the `team.requests.map` block inside `ApprovalsTab`)

**Interfaces:**
- Consumes: `accessRequestsApi.approve(id, {tier, duration})`, `accessRequestsApi.revoke(id)` (Task 12); `AccessRequestOut.requester_role`, `.tier`, `.active` (Task 11).

- [ ] **Step 1: Implement**

Add two option arrays near the top of `AdminPage.jsx`, alongside the existing `TEAM_ROLE_CHOICES` (`frontend/src/pages/AdminPage.jsx:22-30`):
```js
const GRANT_TIER_OPTIONS = [
  { label: 'Viewer — public/internal docs only', value: 'viewer' },
  { label: 'Contributor — viewer + edit/upload', value: 'contributor' },
  { label: 'Contributor + confidential', value: 'contributor_confidential' },
];
const GRANT_DURATION_OPTIONS = [
  { label: '72 hours', value: 'hours_72' },
  { label: '1 week', value: 'week_1' },
  { label: '1 month', value: 'month_1' },
  { label: 'No expiration', value: 'unlimited' },
];
```

Inside `ApprovalsTab`, add per-row tier/duration selection state right after the existing `busyKey` state (`frontend/src/pages/AdminPage.jsx:896`):
```js
  const [grantChoices, setGrantChoices] = useState({}); // request_id -> { tier, duration }
```

Replace the `team.requests.map` block (`frontend/src/pages/AdminPage.jsx:1035-1065`):
```jsx
                        {team.requests.length > 0 && (
                          <div className="space-y-2">
                            <p className="text-xs uppercase tracking-wide text-gray-500">Confidential-access requests</p>
                            {team.requests.map((r) => {
                              const choice = grantChoices[r.request_id] || {};
                              const isPending = r.status === 'pending';
                              const isActiveGrant = r.status === 'approved' && r.active;
                              const canApprove = r.scope !== 'stage' || (choice.tier && choice.duration);
                              return (
                                <div key={r.request_id} className="rounded-md border border-border bg-surface px-3 py-2 space-y-2">
                                  <div className="flex items-center justify-between gap-3">
                                    <div className="min-w-0">
                                      <p className="text-sm text-gray-200 truncate">
                                        {r.requester_email || r.user_id}
                                        {r.requester_role && <span className="text-gray-500"> ({r.requester_role})</span>}{' '}
                                        {r.scope === 'document' && <>requests access to <span className="font-medium">{r.target_name}</span></>}
                                        {r.scope === 'stage' && <>requests access to the <span className="font-medium">{r.target_name}</span> stage</>}
                                        {r.scope === 'team' && <>requests team-wide access</>}
                                      </p>
                                      <p className="text-xs text-gray-500">
                                        Requested {r.requested_at ? new Date(r.requested_at).toLocaleDateString() : '—'}
                                        {' · '}
                                        {r.tier ? `${r.tier} · ` : ''}{r.grant_duration_label}
                                      </p>
                                    </div>
                                    <div className="flex gap-2 shrink-0">
                                      {isPending && (
                                        <>
                                          <Button size="sm" icon={Check}
                                            disabled={!canApprove}
                                            loading={busyKey === `req-approve-${r.request_id}`}
                                            onClick={() => act(
                                              `req-approve-${r.request_id}`,
                                              () => accessRequestsApi.approve(r.request_id, choice),
                                              'Access request approved.',
                                            )}
                                          >Approve</Button>
                                          <Button size="sm" variant="danger" icon={X}
                                            loading={busyKey === `req-deny-${r.request_id}`}
                                            onClick={() => act(`req-deny-${r.request_id}`, () => accessRequestsApi.deny(r.request_id), 'Access request denied.')}
                                          >Deny</Button>
                                        </>
                                      )}
                                      {isActiveGrant && (
                                        <Button size="sm" variant="danger" icon={X}
                                          loading={busyKey === `req-revoke-${r.request_id}`}
                                          onClick={() => {
                                            if (!window.confirm('Revoke this access grant immediately? This cannot be undone.')) return;
                                            act(`req-revoke-${r.request_id}`, () => accessRequestsApi.revoke(r.request_id), 'Access grant revoked.');
                                          }}
                                        >Revoke</Button>
                                      )}
                                    </div>
                                  </div>
                                  {isPending && r.scope === 'stage' && (
                                    <div className="flex gap-2">
                                      <Dropdown
                                        label="Tier"
                                        options={GRANT_TIER_OPTIONS}
                                        value={choice.tier || ''}
                                        onChange={(value) => setGrantChoices((cur) => ({ ...cur, [r.request_id]: { ...cur[r.request_id], tier: value } }))}
                                        placeholder="Choose a tier"
                                      />
                                      <Dropdown
                                        label="Duration"
                                        options={GRANT_DURATION_OPTIONS}
                                        value={choice.duration || ''}
                                        onChange={(value) => setGrantChoices((cur) => ({ ...cur, [r.request_id]: { ...cur[r.request_id], duration: value } }))}
                                        placeholder="Choose a duration"
                                      />
                                    </div>
                                  )}
                                </div>
                              );
                            })}
                          </div>
                        )}
```

- [ ] **Step 2: Confirm the build is clean**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage\frontend" && npx vite build`
Expected: clean build.

- [ ] **Step 3: Live browser walkthrough**

Start both dev servers if not already running (`uvicorn` on :8000, `vite` on :5173 — check first via the chrome-devtools MCP tools or a port check before starting new ones, matching the established convention from the original plan's Tasks 15-19). Using real seeded demo data (or a temporary stage/team/request created the same way earlier tasks in the original plan did, cleaned up afterward):

1. Log in as a team_lead for some team with `TeamStageAccess` to a stage.
2. Have a second account request stage access (via the existing "Request stage access" link, or by creating the `AccessRequest` row directly if no seeded pending one exists).
3. Open Admin → Pending Approvals, confirm the pending row shows the requester's email AND role (e.g. "arjun.verma@... (viewer) requests access to the Planning stage").
4. Confirm the Approve button is disabled until BOTH tier and duration dropdowns have a value selected.
5. Choose `Contributor`, `1 week`, click Approve — confirm success, and that the row's copy now shows "contributor · 1 week".
6. Confirm a Revoke button now appears next to the same row (still expanded/visible after refresh) and clicking it (after confirming the browser `confirm()` dialog) removes the grant — re-check via `GET /access-requests/status` or the requester's own "My requests" panel that it now reads "none"/gone.
7. Check the browser console — zero errors throughout.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/AdminPage.jsx
git commit -m "feat(access): tier/duration selectors, requester role display, and revoke in Pending Approvals"
```

---

### Task 14: Frontend — `MyAccessRequestsPanel.jsx` — display tier and "No expiration"

**Files:**
- Modify: `frontend/src/components/access/MyAccessRequestsPanel.jsx`

**Interfaces:**
- Consumes: `AccessRequestOut.tier` (Task 11).

- [ ] **Step 1: Implement**

In `frontend/src/components/access/MyAccessRequestsPanel.jsx`, replace the row-rendering block inside the `requests.map` (currently lines 51-70, from `return (` through its closing `);`):
```jsx
          return (
            <div key={r.request_id} className="flex items-center justify-between gap-3 rounded-lg border border-border bg-background px-3 py-2.5">
              <div className="min-w-0">
                <p className="text-sm text-gray-200 truncate">{r.target_name}</p>
                <p className="text-xs text-gray-500 capitalize">
                  {r.scope} access · {r.team_name}
                  {r.tier && <> · {r.tier.replace(/_/g, ' ')}</>}
                </p>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                {isExpiredApproved ? (
                  <Badge variant="neutral">Expired</Badge>
                ) : (
                  <Badge variant={badge.variant}>{badge.label}</Badge>
                )}
                {r.active && (
                  <span className="text-xs text-gray-500 flex items-center gap-1">
                    <Clock size={11} /> {r.expires_at ? formatCountdown(r.expires_at) : 'no expiration'}
                  </span>
                )}
              </div>
            </div>
          );
```

(Only two things changed from the current version: the tier is appended to the scope/team line when present, and the countdown line now falls back to "no expiration" instead of only rendering `formatCountdown` when `r.expires_at` truthy — under the old model `expires_at` was always set, so this branch never mattered before; under the new model `unlimited`-duration grants leave it `null`.)

- [ ] **Step 2: Confirm the build is clean**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage\frontend" && npx vite build`
Expected: clean build.

- [ ] **Step 3: Live browser walkthrough**

As the requester account from Task 13's walkthrough (or a fresh one), open "My requests" and confirm: a `contributor`/`1 week` grant shows "stage access · <team> · contributor" with a countdown; approve a separate request with `unlimited` duration and confirm that row shows "no expiration" instead of a countdown. Console clean throughout.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/access/MyAccessRequestsPanel.jsx
git commit -m "feat(access): show granted tier and handle no-expiration grants in My Access Requests"
```

---

### Task 15: End-to-end verification and post-implementation checklist

**Files:** none modified — this task only runs checks.

- [ ] **Step 0: Confirm the deliberately-untouched functions stayed untouched**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && git diff <this-plan's-first-commit>..HEAD -- app/services/access_control.py | grep -E "^\+.*def (can_edit_document|can_delete_version|has_stage_access)\(|^\-.*def (can_edit_document|can_delete_version|has_stage_access)\("` and the equivalent for `app/services/document_delete.py` (`can_delete_version`). Expected: no output — per the spec's capability matrix, `can_edit_document()` and `can_delete_version()` already produce the correct result for grant holders via their existing identity-based rules (`uploaded_by == user_id`), and `has_stage_access()` stays the narrow native-team-only check with the grant fallback living in its callers instead (Tasks 7-9). If this diff shows any of these three functions touched, something drifted from the spec during implementation — investigate before proceeding.

- [ ] **Step 1: Full backend regression suite**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/ -q`
Expected failures, and ONLY these: the 3 pre-existing `test_access_requests_scoped.py` failures named in Global Constraints, and the ~29 pre-existing RLS/graph failures already documented in `.superpowers/sdd/2026-09-17-confidential-access-grants/progress.md` (Task 7's entry) as unrelated to this whole feature area. Anything else failing is this plan's problem — stop and fix before proceeding.

- [ ] **Step 2: Single new alembic head**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage" && "C:\Users\blabh\AppData\Local\Programs\Python\Python312\python.exe" -m alembic heads`
Expected: `f9a8b7c6d5e4 (head)` and `e2f3a4b5c6d7 (head)` — exactly the two named in Global Constraints, nothing else.

- [ ] **Step 3: Clean frontend build**

Run: `cd "D:\Ra\DocFlowAI\meem_salvage\frontend" && npx vite build`
Expected: clean, only pre-existing bundle-size warnings.

- [ ] **Step 4: Live end-to-end grant lifecycle walkthrough, including expiry**

Using the running dev servers and real (or temporary, cleaned-up-after) seeded data:

1. As a plain viewer with no confidential access, on a stage their team already partially sees (some locked confidential doc), request stage access.
2. As that team's team_lead, open Pending Approvals, confirm requester email + role shown, approve as `viewer` / `72 hours`.
3. As the requester: confirm they now see every public/internal document in that stage (not just their own team's uploads, if the stage has documents from other teams — set this up if the seeded data doesn't already have it), confirm confidential documents there are still locked stubs, confirm they cannot upload (no upload UI/action succeeds).
4. Directly in the database, set that grant's `expires_at` to a past timestamp (matching this session's established convention for testing expiry without waiting — e.g. `UPDATE access_requests SET expires_at = now() - interval '1 hour' WHERE request_id = '...'`). Reload as the requester and confirm the stage/documents are no longer visible, and `GET /access-requests/status` reports `"none"` again.
5. Repeat steps 1-2 but approve as `contributor` / `1 week` instead. Confirm the requester can now upload a document into that stage (attributed to the grant's routing team) and submit it for review, but still cannot see confidential documents there.
6. Repeat once more, approving as `contributor_confidential` / `no expiration`. Confirm the requester can now see, upload to, and submit within that stage, including confidential documents, and that "My requests" shows "no expiration" with no countdown.
7. As the team_lead, revoke the `contributor_confidential` grant from step 6. Confirm the requester immediately loses all of it — reload and confirm they're back to their pre-grant state (stage may still show as locked-confidential if their own team independently has access, or disappear entirely if not — whichever matches their native access).
8. Confirm `rag_tools.py` is untouched by this whole plan (`git diff <plan-start-commit>..HEAD -- app/tools/rag_tools.py` is empty) — RAG's behavior changes only because it consumes `get_accessible_stages_for_user()`/`AuthorizationContext`, never because this plan edited it directly.
9. Check the browser console throughout — zero errors.

- [ ] **Step 5: Update the SDD ledger**

Append a new section to `.superpowers/sdd/2026-09-17-confidential-access-grants/progress.md` recording that this plan (Tasks 1-15) is complete, referencing this plan's file path and the tiered-grant spec, and noting that the mid-session scope-change #2 audit (stage-level document/count visibility hidden from users without access; RAG must not reveal these details either — Sources panel stage-name/count display, Project Intelligence/audit/metrics endpoints, `rag/retrieval.py`, `rag_tools.py`'s response messaging) is STILL the one deferred item from before this plan started, now that the tiered-grant mechanism it needs to account for (`get_active_stage_grants_for_user`, `stage_grant_tiers`) actually exists to audit against.

