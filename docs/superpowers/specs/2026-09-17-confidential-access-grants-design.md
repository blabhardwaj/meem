# Confidential Access Grants

## Architecture & Implementation Specification

### Status

**Design phase — Sections 1–13 finalized. Two implementation-time decisions deliberately deferred (Section 14). Ready for implementation planning.**

This specification defines scoped confidential-access grants that allow users to read confidential documents without granting them additional capabilities such as editing, submitting, approving, rejecting, deleting, or managing workflow state.

---

# 1. Data Model & Grant Resolution

## 1.1 AccessRequest model

`AccessRequest` in `app/models/team.py` will be extended with scoped grant information.

### New fields

```python
scope: Mapped[AccessRequestScope]
```

`AccessRequestScope` is a new enum with:

```text
document
stage
team
```

Two nullable foreign keys are added:

```python
document_id: Mapped[uuid.UUID | None]
stage_id: Mapped[uuid.UUID | None]
```

`document_id` references `documents`.

`stage_id` references `stages`.

### Scope invariant

Exactly one target is associated with `document` and `stage` scopes:

| Scope      | `document_id` | `stage_id` | `team_id` |
| ---------- | ------------- | ---------- | --------- |
| `document` | required      | NULL       | required  |
| `stage`    | NULL          | required   | required  |
| `team`     | NULL          | NULL       | required  |

The invariant is enforced in two places:

1. `request_confidential_access()` validates the request before creation.
2. A database `CHECK` constraint in the migration provides defense in depth.

`team_id` remains required for **every** access-request row.

For document/stage-scoped requests, the requester does not choose `team_id`. It is derived server-side from the target:

* document → the document's `uploaded_as_team_id`
* stage → the stage's team access

The `team_id` identifies the team responsible for approving the request and is also used for the existing "already a member" precondition.

---

## 1.2 Foreign-key deletion behavior

The existing `team_id` foreign key behavior is preserved exactly as-is.

The two new target foreign keys use:

```text
ON DELETE CASCADE
```

A document-scoped or stage-scoped request has no meaningful target once that target is deleted. `SET NULL` is inappropriate because it would leave the request violating the scope invariant.

This does not alter the existing deletion behavior of `team_id`.

---

## 1.3 Grant TTL

Grant expiration depends on scope.

### Document scope

```text
DOCUMENT_GRANT_TTL_HOURS = 72
```

A document-scoped approved grant expires:

```text
now + 72 hours
```

### Stage and team scope

The existing:

```text
GRANT_TTL_DAYS
```

continues to apply, currently representing the 90-day default.

Therefore:

```text
document grant → 72 hours
stage grant    → existing 90-day TTL
team grant     → existing 90-day TTL
```

---

# 2. Grant Resolution

## 2.1 Core helper

The existing confidential-grant lookup:

```text
_has_active_confidential_grant
```

will be replaced/extended with three-scope resolution logic.

It is called by:

```text
classify_document_visibility()
classify_documents_visibility()
```

The helper evaluates a **specific document** and determines whether the requesting user has an approved, unexpired confidential-access grant that covers that document.

A grant satisfies the document if **any one** of the following is true:

### Document-scoped grant

```text
grant.scope == document
AND
grant.document_id == document.document_id
```

### Stage-scoped grant

```text
grant.scope == stage
AND
grant.stage_id == document.stage_id
```

### Team-scoped grant

```text
grant.scope == team
AND
grant.team_id ∈ document's visible teams
```

The team-scoped behavior remains semantically identical to the existing implementation.

---

## 2.2 Definition of "document's visible teams"

The applicable teams are exactly the teams represented by:

```python
DocumentTeamVisibility
```

rows for the document.

Conceptually:

```python
visible_team_ids = {
    row.team_id
    for row in db.execute(
        select(DocumentTeamVisibility).where(
            DocumentTeamVisibility.document_id == document.document_id
        )
    ).scalars()
}
```

This must **not** be replaced with:

* all teams belonging to the uploader;
* the document's `uploaded_as_team_id` alone;
* or another approximation of document visibility.

The team-scoped grant check must intersect against the same `DocumentTeamVisibility` set used by the existing visibility system.

---

## 2.3 Query shape

The three grant types should be resolved with one database query rather than separate lookups.

Conceptually:

```sql
WHERE
    user_id = ?
    AND status = approved
    AND expires_at > now()
    AND (
        document_id = ?
        OR stage_id = ?
        OR team_id IN (...)
    )
```

This preserves the existing characteristic of performing a single additional grant lookup while extending it to document- and stage-scoped grants.

---

# 3. Enforcement: Grant Access Is Read-Only

## 3.1 Core principle

A confidential-access grant provides **read access only**.

It does not constitute a role and must never become a capability upgrade.

The resulting model is:

```text
Normal role-based access
    → existing capabilities remain unchanged

Grant-only confidential access
    → READ ONLY

No applicable access
    → existing denial behavior
```

In particular, an access grant must never confer:

* edit capability;
* new-version upload capability;
* submit capability;
* approve capability;
* reject capability;
* document deletion capability;
* workflow-state mutation capability;
* stage-management capability;
* team-management capability;
* any other permission normally derived from a user's role/rank.

---

# 4. Effective Access Resolution & the Grant-Only Detection Helper

## 4.0 `resolve_effective_access()` — the single source of truth

Both the read-only enforcement helper (`is_grant_only_confidential_access()`) and the requester-facing status endpoint (Section 13.C) need to answer variants of the same underlying question: *how, if at all, does this user currently have access to this specific target?*

To prevent these two consumers from drifting into two independent implementations of that question, a single shared resolver is introduced in `access_control.py`:

```python
def resolve_effective_access(
    db: Session,
    user_id: UUID,
    *,
    document_id: UUID | None = None,
    stage_id: UUID | None = None,
    team_id: UUID | None = None,
) -> EffectiveAccessResult:
    ...
```

Exactly one of `document_id` / `stage_id` / `team_id` is provided per call — the caller is asking about one specific target.

`EffectiveAccessResult` carries:

```text
status: "granted" | "pending" | "denied" | "expired" | "none"
via_grant: bool          # only meaningful when status == "granted"
expires_at: datetime | None
request_id: UUID | None  # the request this verdict was derived from, if any
```

### 4.0.1 Resolution order

```text
resolve_effective_access()
    │
    ├─ native role access (org admin / project admin / team-lead+
    │  on an applicable visible team)?
    │      → granted, via_grant=False, expires_at=None
    │
    ├─ active document/stage/team grant covers this target?
    │      → granted, via_grant=True, expires_at=<matching grant's expiry>
    │
    ├─ live pending request for this exact target?
    │      → pending, expires_at=None
    │
    ├─ latest denied/expired request for this exact target?
    │      → denied / expired (accordingly)
    │
    └─ nothing found
           → none
```

Native role access is checked **first** and short-circuits the rest — a user with both native access and an active grant is reported as natively granted (`via_grant=False`), never as grant-only. This is the same precedence already established for document visibility (Section 2) and is what keeps a team_lead who also happens to hold a grant from ever being treated as read-only.

Only when native access is absent does an active grant get consulted, and only when neither is present does request history (pending, then terminal) get consulted. A stale denied/expired request for a target the user now has an active grant for must never suppress the "granted" verdict — the resolver checks effective current access before it ever looks at request history.

### 4.0.2 Open question: deterministic grant selection on overlap

A target can, in principle, be covered by more than one active grant simultaneously — e.g. a document covered by both a document-scoped grant and a stage-scoped grant on the same stage. The resolved `status` is unambiguous (`granted`), but `expires_at`/`request_id` must come from exactly one of the overlapping grants, and the selection rule is not yet decided here.

**This is left open, to be settled in the resolver's implementation plan** — not invented in this design document. The document > stage > team scope hierarchy already established elsewhere in this spec is the natural candidate for a narrowest-scope-wins rule, but the plan should decide and document this explicitly rather than leaving it to whichever query happens to return first.

---

## 4.1 `is_grant_only_confidential_access()` — thin consumer

The read-only enforcement helper from the original design becomes a thin wrapper over the shared resolver rather than its own independent implementation:

```python
def is_grant_only_confidential_access(
    db: Session,
    user_id: UUID,
    document: Document,
) -> bool:
    if document.sensitivity_level != SensitivityLevel.confidential:
        return False
    result = resolve_effective_access(db, user_id, document_id=document.document_id)
    return result.status == "granted" and result.via_grant
```

Its meaning is unchanged from the original design:

> Return `True` exactly when the document is confidential and the user's access to that document is coming from an approved confidential-access grant rather than native role-based access.

What changes is that this meaning is now expressed as a direct consequence of `resolve_effective_access()`'s `via_grant` field, rather than as a second, hand-written traversal of org admin → project admin → team-lead+ → grant. The evaluation order described conceptually in the original design (native access checked before grant) is now enforced structurally by the resolver itself, not re-derived here.

If a user has both:

```text
native role-based access
+
confidential grant
```

the user retains their normal capabilities. The grant does not downgrade a user's existing role. This guarantee now lives in one place (`resolve_effective_access()`) rather than being a property each consumer must independently get right.

---

# 5. Authorization Boundary

`has_permission()` remains completely **grant-blind**.

No grant-awareness should be threaded into its rank comparison or capability resolution.

This preserves the architectural distinction:

```text
has_permission()
    → "What can this role normally do?"

is_grant_only_confidential_access()
    → "Is this particular document accessible only because of a read-only grant?"
```

Mutation paths explicitly opt into the second check.

---

# 6. Mutation Enforcement

The grant-only check is applied at shared mutation choke points wherever possible rather than duplicated across routers.

The standard pattern is:

```python
if not has_permission(...):
    raise 403

if is_grant_only_confidential_access(db, user_id, document):
    raise 403
```

The exact exception should use the existing permission/workflow exception mechanism understood by the relevant caller.

---

## 6.1 `finalize_document_revision()`

Location:

```text
app/services/document_finalize.py:144
```

This is a critical shared choke point.

The audit identified two different call chains reaching this function:

```text
stricter can_edit_document() path
looser has_permission("upload", ...) path
```

Because both ultimately finalize a new version of an existing document, the grant-only restriction belongs **inside `finalize_document_revision()` itself**.

This prevents the two call chains from developing inconsistent enforcement.

The check should therefore be implemented once at this shared boundary rather than duplicated at both routers.

The initial upload of a brand-new document is not covered by this restriction because there is no pre-existing document whose confidential access is being exercised.

---

## 6.2 Document deletion

The following service functions require enforcement:

```text
app/services/document_delete.py:150
delete_document()
```

and:

```text
app/services/document_delete.py:98
delete_version()
```

Both operate on existing documents/versions and must reject operations when the actor's access is grant-only.

The check should be performed alongside the existing authorization logic, including the existing `is_admin` handling.

---

## 6.3 Approval promotion

Location:

```text
app/services/workflow.py:196
promote_version_on_approval()
```

This is another shared mutation choke point.

It is reached by:

* normal document approval;
* auto-approve;
* auto-index behavior.

The grant-only restriction belongs inside this shared function rather than being independently duplicated across each caller.

---

## 6.4 Submit

```text
submit_for_review()
```

The existing permission check remains.

After it succeeds, the grant-only confidential-access check is applied to the existing document.

Grant-only users must receive a 403 rather than being allowed to submit a confidential document for review.

---

## 6.5 Approve

```text
approve_document()
```

The existing permission check remains.

A grant-only user must not be able to approve the document.

---

## 6.6 Reject

```text
reject_document()
```

The existing permission check remains.

A grant-only user must not be able to reject the document.

---

## 6.7 Reset-to-draft

Location:

```text
app/services/workflow.py:316
reset_to_draft_if_approved()
```

Although this is not a conventional document-edit operation, it mutates workflow state on an existing document.

It therefore falls within the read-only restriction and must reject grant-only access.

---

# 7. Bulk Stage Deletion / Reassignment

`delete_stage()` introduces a structurally different enforcement problem.

The current implementation performs a bulk SQL update that reassigns matching documents' `Document.stage_id` values.

Conceptually:

```text
delete stage
    ↓
bulk UPDATE documents
    ↓
reassign affected documents
```

A single document-level check at the router boundary is insufficient because multiple documents may be affected by the operation and each may have different grant-only access status.

Therefore, enforcement must operate at the **per-document level**.

The current design proposes changing the bulk operation so that affected documents are examined individually and:

```text
grant-only confidential document
    → cannot be reassigned by that actor
```

The operation must not silently allow the grant holder to move a confidential document merely because the operation happens to be expressed as a stage-level bulk update.

### Behavior still to be decided

Two possible behaviors remain under consideration:

**Hard failure**

```text
Any grant-only document found
    → fail the entire stage deletion
```

or:

**Partial operation**

```text
Grant-only documents
    → excluded from reassignment

Other documents
    → reassigned

Result
    → clear report of excluded documents
```

This is intentionally left as an unresolved design decision before implementation.

---

# 8. New-Document Upload Exception

`upload_and_scan()` has an auto-approve path that operates on the first version of a brand-new document.

This remains out of scope.

Reason:

```text
new document
    → no existing confidential document state
    → no existing grant can be exercised against it
```

Therefore, the grant-only read restriction does not need to be applied to this initial creation path.

The restriction begins when an existing document is being mutated.

---

# 9. RAG & Search

No grant-specific enforcement changes are required in the RAG/search layer for this section.

The reason is architectural:

```text
classify_document_visibility()
    ↓
confidential grant establishes read visibility
    ↓
RAG/search can retrieve the document
```

RAG/search performs read operations and therefore inherits the corrected visibility behavior.

Mutation authorization remains separately enforced at the document/workflow service boundaries.

This preserves the separation between:

```text
READ VISIBILITY
```

and:

```text
WRITE / WORKFLOW CAPABILITY
```

---

# 10. Enforcement Architecture Summary

The resulting authorization flow is:

```text
                         ┌─────────────────────────┐
                         │ classify_document_      │
                         │ visibility()            │
                         └────────────┬────────────┘
                                      │
                     confidential document?
                                      │
                                      ▼
                         active grant resolution
                           /       |       \
                          /        |        \
                   document      stage      team
                      grant      grant      grant
                          \        |        /
                           \       |       /
                            └──────┴──────┘
                                   │
                                   ▼
                              READ ACCESS
                                   │
                    ┌──────────────┴──────────────┐
                    │                             │
                  RAG/search                 normal viewing
```

Mutation flow:

```text
Existing document mutation
          │
          ▼
   has_permission()
          │
          │ normal role capability
          ▼
       allowed?
       /     \
     no       yes
     │         │
    403        ▼
       is_grant_only_confidential_access()
                 │
          ┌──────┴──────┐
        False          True
          │              │
          ▼              ▼
     continue           403
```

Critically, the grant does **not** enter the capability system.

---

# 11. Shared-Choke-Point Principle

Where multiple routes can perform the same mutation, enforcement should occur at the deepest shared service boundary that represents the actual mutation.

Examples:

```text
multiple version-upload paths
        ↓
finalize_document_revision()
        ↓
single grant-only check
```

and:

```text
approve / auto-approve / auto-index
        ↓
promote_version_on_approval()
        ↓
single grant-only check
```

This prevents authorization drift between callers.

---

# 12. Current Design Decisions

The following are considered settled:

* `AccessRequest` supports `document`, `stage`, and `team` scopes.
* `team_id` remains mandatory for all requests.
* Document/stage target teams are derived server-side.
* Scope/target consistency is enforced both application-side and with a DB `CHECK`.
* Document grants expire after 72 hours.
* Stage/team grants retain the existing 90-day TTL.
* Grant resolution checks document, stage, and visible-team scope in one query.
* Team scope uses exactly the existing `DocumentTeamVisibility` semantics.
* Grant access is read-only.
* `has_permission()` remains grant-blind.
* Native role-based access always takes precedence over grant-only restrictions.
* Shared mutation choke points should contain the enforcement rather than duplicating checks across routers.
* RAG/search requires no special grant enforcement beyond corrected document visibility.
* Brand-new document creation is outside grant-only mutation enforcement.
* `document_id` and `stage_id` use `ON DELETE CASCADE`.
* Existing `team_id` FK behavior remains unchanged.
* `resolve_effective_access()` is the single shared resolver for target-level effective access, consumed by both `is_grant_only_confidential_access()` (mutation enforcement) and `GET /access-requests/status` (UI state) — neither independently re-derives grant precedence.
* Native role access is checked before grant coverage, which is checked before request history (pending, then terminal) — this precedence lives in the resolver, not in each consumer.
* The UI never reconstructs scope/target/grant-precedence logic; every trigger and status surface renders a server-resolved verdict.
* Document/stage escalation in the request UI is presented as a deliberate, explicit broadening — never implied or automatic.
* Team-scope requests keep their existing membership precondition, enforced server-side only.
* `expires_at` is server-authoritative; any UI countdown is presentation-only, never separately stored state.
* Notification routing (approver vs. requester) is driven by a structured `audience` field set at creation time, never inferred from notification title text.

See **Section 14** for the complete, current list of deliberately deferred items — do not treat this section's earlier "unresolved" note as the final word; it has been superseded and folded into Section 14.

---

# 13. UI & Request-Flow Design

## 13.0 Method

This section was designed by first auditing the existing request/approval UI and backend flow as it stands today (not by assuming a design), then defining how the three scopes (document/stage/team) map onto that existing UX, then tracing the full lifecycle (request → pending → approval/rejection → grant → expiration) before deciding what UI changes are actually necessary.

### 13.0.1 Audit findings — current state before this design

* **Requester side**: `ProjectWorkspace.jsx`'s "Request confidential access" modal is the only entry point in the direct-UI path. It lists every team where the caller is `viewer`/`contributor`, and calls `POST /access-requests` with body `{team_id}` only — no other field is sent. Status per team is derived from `GET /access-requests/mine`, inspecting only the caller's own, latest-per-team request.
* **Approver side**: the "Pending Approvals" tab (`AdminPage.jsx`) lists every pending `AccessRequest` grouped by team, showing only requester identity + request date. There is no scope information today because no scope field exists yet; every request is implicitly team-wide, and approval unconditionally sets a flat 90-day expiry.
* **Status visibility**: there is no dedicated "my requests" surface — status is visible only by reopening the exact modal that created the request. Nothing shows time-to-expiry; expiration is discovered reactively, after the fact, by process of elimination (`status == approved && !active` renders as "expired").
* **Second entry point**: `app/tools/rag_tools.py`'s `request_confidential_access` tool is a parallel, agent-initiated path into the identical `request_confidential_access()` service function, triggered when the LLM decides the user has agreed to a scripted access-request offer. It is one-shot/fire-and-forget with no follow-up status mechanism of its own.
* **Notifications**: `notify_access_request_created`/`notify_access_request_decided` fire real per-recipient notification rows with `resource_type="access_request"`/`resource_id` already attached, but are title-only (no body) and the `TopNav.jsx` bell UI renders them as inert text — no click-through to the Pending Approvals tab or to any status view exists today.
* **Document-row visibility change**: once a grant is approved, a previously-hidden document simply appears in the list on the next fetch, indistinguishable from a document the user could always see — there is no "you can see this because of a grant" indicator anywhere.

These gaps — no per-target status lookup, no scope-aware approver UI, no expiry countdown, inert notifications — are what the rest of this section addresses, in addition to wiring in the new document/stage scopes.

---

## 13.A Request triggers per scope

### Document scope (new)

The locked placeholder document row (Section 13.D's redacted stub for a `blocked_by_sensitivity` document) gets a "Request access" action. It calls a new client method:

```text
accessRequestsApi.createForDocument(documentId)
    → POST /access-requests
      { scope: "document", document_id: documentId }
```

**No `team_id` crosses the client/server boundary.** The server derives it from the document's `uploaded_as_team_id`, per Section 1.1's invariant. This is a direct extension of the existing trust boundary (today's `POST /access-requests` already only accepts `team_id` from the client for team-scope requests) — document/stage requests extend the same pattern with `document_id`/`stage_id` instead, never `team_id`.

### Stage scope (new)

Surfaced as an explicit escalation from the document context, worded to make the broadening deliberate rather than implied:

> "Need access to more documents in this stage? Request stage access."

```text
accessRequestsApi.createForStage(stageId)
    → POST /access-requests
      { scope: "stage", stage_id: stageId }
```

The UI must not make it seem as though requesting stage access is the same action as, or an automatic consequence of, requesting document access — it is presented as a distinct, broader choice the user makes deliberately. The escalation reads as:

```text
this document
     ↓ broader
this stage
     ↓ broader
this team
```

### Team scope (existing, repurposed)

The existing `ProjectWorkspace` modal is kept, not replaced, and becomes the explicit entry point for the broadest tier:

> **Team access** — request access to confidential documents shared with this team.

Its existing membership precondition (`viewer`/`contributor` already on that team, checked in `request_confidential_access()`) is unchanged and remains enforced **server-side only** — the UI may explain why the option isn't available to a given user, but never becomes the authority deciding eligibility.

### Trigger state machine (applies to all three scopes)

Because there are now three independent entry points into request creation, every trigger must be state-aware rather than unconditionally rendering "Request access" — otherwise a user could accumulate multiple redundant pending requests for the same effective target. Each trigger queries its exact scope+target's current state (Section 13.C) before rendering:

```text
none            → "Request access"
pending         → "Pending review" (inert, not clickable)
granted         → "Granted" (+ expiry countdown if via_grant; see 13.C)
denied/expired  → "Request access" (retry)
```

This state is qualified by **exact scope + target** — an active grant for document A must not suppress the request trigger for stage X even though document A belongs to stage X, and conversely a team-wide grant must not make every individual document trigger read "Granted" unless that specific document actually resolves as visible through the grant. The underlying resolution (Section 4.0's `resolve_effective_access()`) is authoritative; the frontend renders its verdict and performs no grant-matching logic of its own.

---

## 13.B Approver-side scope display & requester status visibility

### Approver side (Pending Approvals tab)

Each request row's copy becomes scope-specific, so the approver knows the object being granted without needing to infer it from the requester's team membership:

```text
document → "{requester} requests access to {filename}"
stage    → "{requester} requests access to the {stage name} stage"
team     → "{requester} requests team-wide access"
```

The row also displays the grant duration that **will** apply if approved (72 hours for document scope, 90 days for stage/team scope, per Section 1.3) — computed and supplied by the server, not hard-coded as frontend policy. Approve/Deny remain exactly as they are mechanically: `POST .../approve` / `POST .../deny` with no body — the stored `AccessRequest` row (including its scope) remains the sole authority; the client supplies no scope or TTL at decision time.

### Requester status visibility — "My Access Requests"

A dedicated status surface is introduced (exact placement — dedicated page vs. panel/drawer off the notification bell — is a UI-placement detail for the implementation plan, not an architectural decision). It lists every request the caller has made, each row showing: scope, target name, status, and an expiry countdown when applicable.

Per-row expiry follows one rule: **`expires_at` is server-authoritative; any countdown shown ("expires in 11 hours") is a presentation-layer computation over it, never a separately stored or trusted piece of state.** A `pending` row has no `expires_at` yet and shows no countdown — only once a request resolves to `granted` does an expiry exist to display.

---

## 13.C API shape — effective-state resolution, not raw row lookups

The governing rule for this whole section:

> `/access-requests/status` is an **effective-state endpoint**, not a raw request-row lookup. The server resolves current grant coverage and pending/terminal request state for the exact target, using the same authoritative access-resolution semantics as document visibility (`resolve_effective_access()`, Section 4.0). The frontend only renders the returned verdict — it performs no scope/target matching, grant-precedence, or expiry logic of its own.

### `GET /access-requests/status?document_id=... | stage_id=... | team_id=...`

New endpoint. Given exactly one target, calls `resolve_effective_access()` (Section 4.0) for the caller and returns its `EffectiveAccessResult` directly:

```text
{
  status: "none" | "pending" | "granted" | "denied" | "expired",
  via_grant: bool,           // only meaningful when status == "granted"
  expires_at: string | null,
  request_id: string | null
}
```

This is what every Section 13.A trigger calls to render its state, and what closes the "frontend must not reconstruct grant logic" requirement — the two questions "is there a request for this target" and "does this user currently have effective read access to this target" are both resolved server-side, by the same function that governs actual document visibility and read-only enforcement, and are never conflated or re-derived in the browser.

### `GET /access-requests/mine` — kept, distinct purpose

Retained as-is in spirit (extended with `scope`/`target_name` for display), but now has a clearly distinct role from `/status`:

```text
/mine              → lifecycle/history surface (every request the caller ever made)
/status?target=...  → point-in-time effective state for one exact target
```

`/mine` feeds the "My Access Requests" surface (13.B); `/status` feeds the per-target trigger state machine (13.A). Neither reimplements the other.

### `GET /access-requests/pending` — enriched

Each `AccessRequestOut` row gains server-resolved `scope`, `target_id`, `target_name`, and `grant_duration` — the approver-side copy and duration-preview in 13.B render directly off these fields; no scope-to-duration business logic (e.g. "documents get 72 hours") is duplicated in the frontend.

### Notification payload — structured routing

`notify_access_request_created`/`notify_access_request_decided` already record `resource_type="access_request"`/`resource_id`. This section adds one field: an explicit `audience` (or `role_in_event`) value — `"approver"` for the created notification, `"requester"` for the decided notification — set at creation time. `TopNav.jsx`'s notification click-through (routing to the Pending Approvals tab or to "My Access Requests") is then a deterministic function of this structured field, never an inference from notification title wording.

---

## 13.D Document list redaction (locked rows)

This decision predates the rest of Section 13 (it was settled during earlier, pre-architectural-pivot design work on the same underlying problem — "how does a user discover a confidential document exists at all") but was never transcribed into this document. It is recorded here now because Section 13.A's document-scope trigger depends on it existing somewhere concrete, and a plan task must not reference an undefined UI element.

### The problem

`GET /documents` (`app/routers/documents.py`) currently filters using `can_view_document()` — a plain boolean. A document that resolves to `blocked_by_sensitivity` is **silently dropped from the response entirely**, indistinguishable from a document that doesn't exist or that the user has no team relationship to at all (`not_visible`). This means a viewer/contributor has no way to discover that a confidential document exists on their own team before requesting access — the request is necessarily blind ("maybe there's something confidential here").

### The decision

`list_documents` is changed to use `classify_document_visibility()` (the three-way outcome, already defined in Section 2) instead of the boolean wrapper:

```text
fully_allowed          → serializes normally, exactly as today
blocked_by_sensitivity → serializes as a redacted stub (see below)
not_visible             → still dropped entirely, unchanged
```

### Redacted stub shape

A `blocked_by_sensitivity` document is returned with a `locked: true` flag and only the following populated:

```text
document_id
original_filename
stage_id
sensitivity_level
uploaded_as_team_id   # needed so the client can pass it to Section 13.A's request-trigger UI
```

`workflow_state` and `uploaded_by` are omitted (`None`) — approval-lifecycle and authorship detail are not shown for content the user cannot open. No other field (content, version history, scan results) is ever included for a locked stub.

### Frontend rendering

`DocumentItem` renders a distinct locked variant when `doc.locked` is true: a dimmed row showing a lock icon, the filename, and the sensitivity badge, with the Section 13.A document-scope "Request access" trigger as its only action. The existing static banner in `SourcePanel.jsx` ("Some documents may be confidential — request access") is removed — it was compensating for content being entirely invisible, which is no longer the case once locked stubs render as real rows with a grounded, specific action.

### Post-grant behavior

No additional plumbing is required for a locked stub to become a full row once access is granted: `classify_document_visibility()` re-evaluates `resolve_effective_access()`-backed grant coverage on every call, so the very next `list_documents` fetch after an approval naturally returns the document as `fully_allowed` instead of `blocked_by_sensitivity`. The frontend does not need to special-case this transition — it is a consequence of the list simply being re-fetched (e.g. on the next page load, or a explicit refresh after the requester sees their request move to `granted` in "My Access Requests").

---

# 14. Status & Remaining Open Items

Sections 1–13 are now finalized as the checkpoint design. The following are the only items deliberately left open, each explicitly flagged at its point of origin above rather than silently assumed:

* **`delete_stage()` bulk-reassignment behavior** (Section 7): hard-fail the whole stage deletion vs. partially execute while excluding/reporting grant-only-inaccessible documents. Does not block UI design and can be resolved independently.
* **Deterministic grant selection on multi-grant overlap** (Section 4.0.2): when a target is covered by more than one active grant simultaneously, which one's `expires_at`/`request_id` is surfaced. A narrowest-scope-wins rule is the natural candidate given the document > stage > team hierarchy, but the exact rule is deferred to the resolver's implementation plan.
* **"My Access Requests" UI placement** (Section 13.B): dedicated page vs. panel/drawer off the notification bell — an implementation-time UI decision, not an architectural one.

No other section carries an open decision. The next step is the implementation plan (via the writing-plans skill), which should address the two deferred rules above explicitly before or during implementation rather than leaving them to be decided ad hoc in code.
