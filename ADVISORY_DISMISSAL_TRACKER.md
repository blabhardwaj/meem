# Advisory Dismissal Implementation Tracker & Task Checklist

## Overview
Tracks the implementation of the **Advisory Finding Dismissal (`AdvisoryDismissal`)** system for Team Leads and above in DocFlowAI.

### Architectural Invariants
1. **`is_blocker == False` is the sole dismissal boundary**: Any finding where `is_blocker == True` cannot be dismissed (returns HTTP 400).
2. **Durable semantic identity**: Persistence is keyed to `(project_id, finding_fingerprint)`, while `initial_finding_id` is historical provenance only.
3. **Deterministic fingerprinting**: Excludes volatile scores, timestamps, and LLM text; hashes only stable identity attributes.
4. **Readiness unmutated**: Dismissals govern presentation and actionable filtering only (`is_dismissed`); gate blockers and readiness status remain untouched.
5. **Dismissal Safety Invariant**:
   > An `AdvisoryDismissal` may only affect a finding while the current `AuditFinding` has `is_blocker == False`. If a later audit classifies the same fingerprint as a gate blocker, the dismissal is ignored for that run and the blocker remains fully active.

---

## Task Checklist

- [x] **Phase 1: Database Model & Schema Integration**
  - [x] Create `app/models/advisory_dismissal.py` with `AdvisoryDismissal` table in schema `knowledge`.
  - [x] Export `AdvisoryDismissal` in `app/models/graph.py` and `app/models/__init__.py`.
  - [x] Execute DDL to create `knowledge.advisory_dismissals` table in PostgreSQL with unique index `(project_id, finding_fingerprint)`.
- [x] **Phase 2: Deterministic Semantic Fingerprinting & Service Layer**
  - [x] Create `app/services/graph/advisory_dismissal_service.py`.
  - [x] Implement `compute_finding_fingerprint()` strictly using invariant identity attributes (rule_code + affected_entity_id + normalized target evidence/discriminator) — completely excluding timestamps, scores, or volatile text.
  - [x] Implement `dismiss_advisory_finding()` following the strict authorization pipeline:
    - Finding exists
    - Project scope matches
    - `reviews_project(db, user_id, project_id)` (Team Lead+)
    - `finding.is_blocker == False` (Strict 400 rejection if `is_blocker == True`)
    - Document ABAC clearance `can_view_document()`
    - Upsert `AdvisoryDismissal(is_active=True)`
    - Immutable `record_audit(action="DISMISS_ADVISORY_FINDING")`
  - [x] Implement `restore_advisory_finding()` with authorization, un-dismissing and logging `record_audit(action="RESTORE_ADVISORY_FINDING")`.
  - [x] Implement `get_active_dismissals_map()` for fast lookup across findings.
- [x] **Phase 3: DTOs, Audit Actions & Router Integration**
  - [x] Update `app/schemas/graph.py` with `DismissFindingRequest`, `AdvisoryDismissalDTO`, and add `finding_fingerprint`, `is_dismissed`, `dismissal` fields to `FindingDTO`.
  - [x] Add `"DISMISS_ADVISORY_FINDING"` and `"RESTORE_ADVISORY_FINDING"` to `AUDIT_LOG_ACTIONS` in `app/services/audit.py`, updating `_row_scope` and `_visible` for project and team lead scoping.
  - [x] Update `_build_and_filter_finding_dtos()` in `app/routers/project_intelligence.py` to annotate findings with deterministic fingerprints and active dismissal state.
  - [x] Enforce **Dismissal Safety Invariant** in `_build_and_filter_finding_dtos()`: apply active dismissal map only when `not f.is_blocker`. If a finding becomes a blocker in a subsequent run, historical dismissals are ignored and `is_dismissed` remains `False`.
  - [x] Add `POST /projects/{project_id}/intelligence/findings/{finding_id}/dismiss` and `POST /projects/{project_id}/intelligence/findings/{finding_id}/restore` endpoints in `app/routers/project_intelligence.py`.
  - [x] Update `get_findings` to support `status` query filter (`all`, `active`, `dismissed`).
  - [x] Ensure `get_gaps` preserves non-mutated readiness gate evaluation based purely on `is_blocker`.
- [x] **Phase 4: Frontend UI / UX Enhancements**
  - [x] Update `frontend/src/lib/api.js` with `dismissFinding` and `restoreFinding`, and support `status` parameter on `findings()`.
  - [x] Update `frontend/src/components/intelligence/AuditFindingDrawer.jsx` with dismissal controls, rationale dialog, and dismissed info banner.
  - [x] Update `frontend/src/pages/ProjectIntelligence.jsx` with active/dismissed filtering and badges.
- [x] **Phase 5: Automated Verification & Regression Testing**
  - [x] Write and run test suite in `tests/test_advisory_dismissal.py`:
    - Blocker protection (`is_blocker == True` -> 400 rejection across all rules)
    - Advisory eligibility (`is_blocker == False` -> 200)
    - Substantive reason validation (min 5 chars)
    - Role gating (viewer/contributor -> 403, team lead+ -> 200)
    - Stage & Document ABAC clearance
    - Durable persistence across audit runs via deterministic fingerprint (Run 1 -> Finding A, Run 2 -> Finding B with new ID automatically marked dismissed)
    - Dismissal Safety Invariant (`test_dismissal_safety_invariant_blocker_never_overridden`): A historical dismissal never overrides a finding if a later audit classifies the same fingerprint as a blocker (`is_blocker=True`)
    - Fingerprint determinism and insensitivity to volatile fields (scores, timestamps, generated text)
    - Restore advisory flow (`RESTORE_ADVISORY_FINDING`)
    - Append-only immutable audit trail logging (`DISMISS_ADVISORY_FINDING`)
  - [x] Verify frontend build (`npm run build` passed in 2.60s with 0 errors).
  - [x] Executed `python -m unittest tests.test_advisory_dismissal.TestAdvisoryDismissal` -> Ran 6 tests in 72.730s, **OK**.

---

## Change Log & Reasoning
- **`app/models/advisory_dismissal.py`** [NEW, 53 lines]:
  - Created `AdvisoryDismissal` in `knowledge` schema.
  - Fields: `dismissal_id` (PK), `tenant_id`, `project_id`, `finding_fingerprint`, `rule_code`, `affected_entity_type`, `affected_entity_id`, `initial_finding_id`, `is_active`, `reason`, `dismissed_by`, `dismissed_at`, `restored_by`, `restored_at`.
  - Keyed to `(project_id, finding_fingerprint)` via unique constraint `uq_project_finding_fingerprint` so dismissals persist across re-audits.
- **`app/models/graph.py`** [MODIFY, +5 lines]:
  - Exported `AdvisoryDismissal` alongside graph models.
- **`app/models/__init__.py`** [MODIFY, +2 lines]:
  - Re-exported `AdvisoryDismissal` in top-level models package.
- **PostgreSQL Database**:
  - DDL executed: `knowledge.advisory_dismissals` table created with unique index on `(project_id, finding_fingerprint)`.
- **`app/services/graph/advisory_dismissal_service.py`** [NEW, 321 lines]:
  - Implemented `compute_finding_fingerprint()`: strictly deterministic SHA256 hash over invariant identity fields (`rule_code`, `affected_entity_id`, `normalized issue_type`, `normalized conflicting/reference evidence doc_id or section`). Excludes timestamps, similarity scores, or generated LLM text.
  - Implemented `dismiss_advisory_finding()`: 8-step authorization pipeline enforcing minimum 5-char reason, project scope, `reviews_project` (Team Lead, Project Admin, Org Admin), `is_blocker == False` (HTTP 400 if blocker), ABAC document clearance, upserting `AdvisoryDismissal`, and append-only audit logging via `record_audit("DISMISS_ADVISORY_FINDING")`.
  - Implemented `restore_advisory_finding()`: reverses dismissal (`is_active=False`), records audit action `"RESTORE_ADVISORY_FINDING"`.
  - Implemented `get_active_dismissals_map()`: fast dictionary lookup `(fingerprint -> AdvisoryDismissal)` for batch finding hydration.
- **`app/services/audit.py`** [MODIFY, +10 lines]:
  - Registered `"DISMISS_ADVISORY_FINDING"` and `"RESTORE_ADVISORY_FINDING"` in `AUDIT_LOG_ACTIONS`.
  - Updated `_row_scope()` and `_visible()` to extract `project_id` and grant visibility to org admins, project admins of the project, and team leads leading teams in the project.
- **`app/schemas/graph.py`** [MODIFY, +25 lines]:
  - Added `finding_fingerprint`, `is_dismissed`, and `dismissal` metadata fields to `FindingDTO`.
  - Added `DismissFindingRequest` schema requiring substantive `reason` (min length 5).
  - Added `AdvisoryDismissalDTO` schema.
- **`app/routers/project_intelligence.py`** [MODIFY, +104 lines]:
  - In `_build_and_filter_finding_dtos()`: preloaded active dismissals, computed deterministic fingerprint for every finding, and attached `is_dismissed`, `finding_fingerprint`, and `dismissal` summary.
  - Implemented **Dismissal Safety Invariant** in `_build_and_filter_finding_dtos()`: `if not f.is_blocker:` applies `active_dismissals.get(fingerprint)`. If a later audit classifies the same fingerprint as a blocker (`f.is_blocker == True`), historical dismissals are ignored, setting `is_dismissed = False` and `dismissal = None`.
  - In `get_findings()`: added `status` query parameter supporting `all`, `active`, `dismissed`.
  - In `get_gaps()`: untouched readiness status & total_blockers logic, preserving the core principle that dismissals govern presentation only and do not mutate underlying findings or readiness score.
  - Added `POST /projects/{project_id}/intelligence/findings/{finding_id}/dismiss` and `POST /projects/{project_id}/intelligence/findings/{finding_id}/restore`.
- **`frontend/src/lib/api.js`** [MODIFY, +12 lines]:
  - Updated `intelligenceApi.findings()` to accept `status` query parameter.
  - Added `intelligenceApi.dismissFinding(projectId, findingId, reason)` and `intelligenceApi.restoreFinding(projectId, findingId)`.
- **`frontend/src/components/intelligence/AuditFindingDrawer.jsx`** [MODIFY, +98 lines]:
  - Added ABAC role detection (`canDismiss = is_org_admin || ['team_lead', 'project_admin'].includes(role)`).
  - Added Dismissed banner displaying author timestamp, substantive rationale, and "Restore finding" action button.
  - Added Dismiss Finding button for active advisory findings opening a dedicated modal requiring at least 5 characters of rationale.
  - Attached finding fingerprint in drawer footer.
  - Triggered `onFindingUpdated` callback to synchronize parent state.
- **`frontend/src/pages/ProjectIntelligence.jsx`** [MODIFY, +25 lines]:
  - Added `statusFilter` state (`'active'`, `'dismissed'`, `'all'`) to Filter Bar.
  - Updated finding cards to display `DISMISSED ADVISORY` badge.
  - Passed `onFindingUpdated={() => loadData({ silent: true })}` to `AuditFindingDrawer`.
- **Frontend Verification**:
  - `npm run build` executed and passed cleanly (`0` errors, built in 2.60s).
- **`tests/test_advisory_dismissal.py`** [NEW, 597 lines]:
  - `test_fingerprint_determinism_and_insensitivity_to_volatile_data`: Asserts that variations in similarity score, timestamps, or LLM explanations produce exact identical fingerprints, while changes in invariant rule discriminators (e.g. `issue_type`) produce distinct fingerprints.
  - `test_blocker_protection_invariant`: Asserts that any finding with `is_blocker=True` returns HTTP 400 with message `"Gating blockers cannot be dismissed"`.
  - `test_advisory_eligibility_and_durable_persistence`: Asserts that advisory dismissal succeeds for team leads, validates minimum 5-char reason, writes append-only audit log, and persists across fresh audit runs (Run 1 -> Finding A, Run 2 -> Finding B with new ID) where Finding B automatically inherits dismissal via matching fingerprint.
  - `test_dismissal_safety_invariant_blocker_never_overridden`: Asserts that when a finding is dismissed during Run 1 as an advisory, but is later elevated to a gating blocker (`is_blocker=True`) in Run 2 with the identical fingerprint, the dismissal is ignored for Run 2, `is_dismissed` is False, and the blocker remains fully active.
  - `test_restore_advisory_finding`: Asserts that un-dismissing sets `is_active=False` and appends `RESTORE_ADVISORY_FINDING` to audit log.
  - `test_role_authorization_contributor_forbidden`: Asserts contributors without review role receive HTTP 403.
  - Executed test suite: `Ran 6 tests in 72.730s -> OK`.
