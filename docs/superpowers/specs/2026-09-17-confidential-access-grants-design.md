# Confidential Access Grants

## Architecture & Implementation Specification

### Status

**Design phase — Sections 1–2 finalized, Section 3 pending**

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

# 4. Grant-Only Detection Helper

A new shared helper will be added to `access_control.py`:

```python
def is_grant_only_confidential_access(
    db: Session,
    user_id: UUID,
    document: Document,
) -> bool:
    ...
```

Its meaning is:

> Return `True` exactly when the document is confidential and the user's access to that document is coming from an approved confidential-access grant rather than native role-based access.

---

## 4.1 Helper evaluation order

The helper must first establish whether the user already has native access to confidential documents through their normal role.

Conceptually:

```text
Is document confidential?
    NO → False

Is user an org admin?
    YES → False

Is user a project admin for this document?
    YES → False

Does user have team-lead+ role on a team
that normally gives native access to this document?
    YES → False

Does user have an active confidential grant?
    YES → True

Otherwise
    → False
```

The native-access checks include:

* organization admin;
* project admin;
* team-lead-or-higher membership on an applicable visible team.

The visible teams are again determined through `DocumentTeamVisibility`.

This ordering is intentional.

If a user has both:

```text
native role-based access
+
confidential grant
```

the user retains their normal capabilities. The grant does not downgrade a user's existing role.

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

The following remains unresolved:

* Exact behavior when `delete_stage()` encounters one or more confidential documents for which the actor has grant-only access: **hard-fail the stage deletion vs. partially execute while excluding/reporting those documents**.

---

# 13. Next Section

Before proceeding to implementation, the next design section should define the **UI/request flow**, including:

* how a user requests access at document/stage/team scope;
* how the scope is selected or inferred;
* what information is shown to the requester;
* how pending requests are represented;
* how approvers see and act on scoped requests;
* how granted access is communicated;
* how expired grants are represented;
* how the UI distinguishes ordinary visibility from grant-based read access;
* and how the existing RAG/search experience interacts with the new grant types.

Implementation should not begin beyond the already-agreed design until these remaining architectural decisions are documented.
