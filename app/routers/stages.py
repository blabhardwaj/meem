"""
Stage management (create / edit / soft-delete), per the Stage model's design
intent — until now stages only ever existed via raw SQL.

  GET    /projects/{project_id}/stages              list active stages (+ doc counts)
  POST   /projects/{project_id}/stages              create a stage           (project_admin+)
  PATCH  /projects/{project_id}/stages/{stage_id}   rename / reorder / toggle requires_approval
                                                                             (project_admin+)
  DELETE /projects/{project_id}/stages/{stage_id}   soft-delete (deleted_at)  (project_admin+)
                                                    blocked while the stage still
                                                    owns documents unless
                                                    ?reassign_to=<stage_id> is given

"project_admin+" = org_admin (tenant-wide) or a ProjectAdmin scope on this
project. Team leads do NOT manage stages (stages are project-wide, not
team-scoped).

order_index is kept dense (0..n-1) after every mutation.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, get_db_with_tenant
from app.models.document import Document
from app.models.project import Project
from app.models.required_document import RequiredDocument, RequirementSource
from app.models.stage import Stage, StageReference, TeamStageAccess
from app.models.team import Team
from app.services.access_control import get_accessible_stages_for_user
from app.services.audit import record_audit
from app.services.auth import ResolvedIdentity
from app.services.graph.audit_engine import sync_and_audit_project

router = APIRouter(prefix="/projects", tags=["stages"])


# --- request / response models ----------------------------------------------

class CreateStageRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    # Where to insert it. Omitted / out of range -> appended to the end.
    order_index: int | None = Field(default=None, ge=0)


class UpdateStageRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    order_index: int | None = Field(default=None, ge=0)
    requires_approval: bool | None = None


class SetStageReferencesRequest(BaseModel):
    # The FULL desired set of stages this stage references (replace, not append).
    references: list[uuid.UUID] = Field(default_factory=list)


class SetTeamAccessRequest(BaseModel):
    # The FULL desired set of teams granted access to this stage (replace, not append).
    team_ids: list[uuid.UUID] = Field(default_factory=list)


class StageOut(BaseModel):
    stage_id: str
    project_id: str
    name: str
    order_index: int
    requires_approval: bool
    document_count: int
    # Stage IDs this stage references (one-way, same project). Drives RAG scope.
    references: list[str] = Field(default_factory=list)
    # Team IDs granted access to this stage (upload + visibility gate).
    team_access: list[str] = Field(default_factory=list)


class RequiredDocumentOut(BaseModel):
    requirement_id: str
    stage_id: str
    name: str
    description: str | None
    is_mandatory: bool
    source: str
    created_at: str


class CreateRequiredDocumentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    is_mandatory: bool = True


class UpdateRequiredDocumentRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    is_mandatory: bool | None = None


# --- helpers ---------------------------------------------------------------

def _load_project(db: Session, identity: ResolvedIdentity, project_id: uuid.UUID) -> Project:
    project = db.get(Project, project_id)
    if project is None or project.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _can_see_project(db: Session, identity: ResolvedIdentity, project_id: uuid.UUID) -> bool:
    if identity.is_org_admin or project_id in identity.project_admin_project_ids:
        return True
    return any(m.project_id == project_id for m in identity.team_memberships)


def _require_project_admin(identity: ResolvedIdentity, project_id: uuid.UUID) -> None:
    if identity.is_org_admin or project_id in identity.project_admin_project_ids:
        return
    raise HTTPException(
        status_code=403,
        detail="Only project admins (or organization admins) can manage stages.",
    )


def _active_stages(db: Session, project_id: uuid.UUID) -> list[Stage]:
    return db.execute(
        select(Stage)
        .where(Stage.project_id == project_id, Stage.deleted_at.is_(None))
        .order_by(Stage.order_index, Stage.created_at)
    ).scalars().all()


def _renumber(stages: list[Stage]) -> None:
    """Assign dense 0..n-1 order_index in list order."""
    for i, stage in enumerate(stages):
        if stage.order_index != i:
            stage.order_index = i


def _doc_counts(db: Session, project_id: uuid.UUID) -> dict[uuid.UUID, int]:
    rows = db.execute(
        select(Document.stage_id, func.count())
        .where(Document.project_id == project_id)
        .group_by(Document.stage_id)
    ).all()
    return {sid: n for sid, n in rows}


def _refs_by_stage(db: Session, stage_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
    """{stage_id: [referenced stage_id, ...]} for the given stages."""
    out: dict[uuid.UUID, list[uuid.UUID]] = {sid: [] for sid in stage_ids}
    if not stage_ids:
        return out
    for r in db.execute(
        select(StageReference).where(StageReference.stage_id.in_(stage_ids))
    ).scalars():
        out.setdefault(r.stage_id, []).append(r.references_stage_id)
    return out


def _team_access_by_stage(db: Session, stage_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
    """{stage_id: [team_id with access, ...]} for the given stages."""
    out: dict[uuid.UUID, list[uuid.UUID]] = {sid: [] for sid in stage_ids}
    if not stage_ids:
        return out
    for r in db.execute(
        select(TeamStageAccess).where(TeamStageAccess.stage_id.in_(stage_ids))
    ).scalars():
        out.setdefault(r.stage_id, []).append(r.team_id)
    return out


def _serialize(
    stage: Stage,
    doc_count: int,
    refs: list[uuid.UUID] | None = None,
    team_access: list[uuid.UUID] | None = None,
) -> StageOut:
    return StageOut(
        stage_id=str(stage.stage_id),
        project_id=str(stage.project_id),
        name=stage.name,
        order_index=stage.order_index,
        requires_approval=stage.requires_approval,
        document_count=doc_count,
        references=[str(x) for x in (refs or [])],
        team_access=[str(x) for x in (team_access or [])],
    )


# --- endpoints -----------------------------------------------------------

@router.get("/{project_id}/stages", response_model=list[StageOut])
def list_stages(
    project_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    _load_project(db, identity, project_id)
    if not _can_see_project(db, identity, project_id):
        raise HTTPException(status_code=403, detail="You do not have access to this project")

    # ENFORCEMENT POINT B: a regular user only sees stages accessible via ANY
    # of their team memberships in this project (team_stage_access union).
    # org_admin/project_admin bypass — get_accessible_stages_for_user()
    # returns every active stage for them, unfiltered.
    accessible_ids = set(get_accessible_stages_for_user(db, identity.user_id, project_id))

    counts = _doc_counts(db, project_id)
    stages = [s for s in _active_stages(db, project_id) if s.stage_id in accessible_ids]
    refs = _refs_by_stage(db, [s.stage_id for s in stages])
    team_access = _team_access_by_stage(db, [s.stage_id for s in stages])
    return [
        _serialize(s, counts.get(s.stage_id, 0), refs.get(s.stage_id, []), team_access.get(s.stage_id, []))
        for s in stages
    ]


@router.post("/{project_id}/stages", response_model=StageOut, status_code=201)
def create_stage(
    project_id: uuid.UUID,
    body: CreateStageRequest,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    _load_project(db, identity, project_id)
    _require_project_admin(identity, project_id)

    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Stage name cannot be empty")

    stages = _active_stages(db, project_id)
    if any(s.name.lower() == name.lower() for s in stages):
        raise HTTPException(status_code=409, detail=f"A stage named '{name}' already exists")

    pos = body.order_index if body.order_index is not None else len(stages)
    pos = max(0, min(pos, len(stages)))

    stage = Stage(project_id=project_id, name=name, order_index=pos,
                  requires_approval=False)
    db.add(stage)
    stages.insert(pos, stage)
    _renumber(stages)
    db.flush()

    record_audit(
        db, actor_id=identity.user_id, action="CREATE_STAGE", resource_type="stage",
        resource_id=stage.stage_id,
        details={"project_id": str(project_id), "name": name, "order_index": stage.order_index},
    )
    db.commit()
    db.refresh(stage)
    return _serialize(stage, 0, [])


@router.patch("/{project_id}/stages/{stage_id}", response_model=StageOut)
def update_stage(
    project_id: uuid.UUID,
    stage_id: uuid.UUID,
    body: UpdateStageRequest,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    _load_project(db, identity, project_id)
    _require_project_admin(identity, project_id)

    stages = _active_stages(db, project_id)
    stage = next((s for s in stages if s.stage_id == stage_id), None)
    if stage is None:
        raise HTTPException(status_code=404, detail="Stage not found")

    changed: dict = {}

    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="Stage name cannot be empty")
        if any(s.stage_id != stage_id and s.name.lower() == name.lower() for s in stages):
            raise HTTPException(status_code=409, detail=f"A stage named '{name}' already exists")
        if name != stage.name:
            changed["name"] = name
            stage.name = name

    if body.requires_approval is not None and body.requires_approval != stage.requires_approval:
        stage.requires_approval = body.requires_approval
        changed["requires_approval"] = body.requires_approval

    if body.order_index is not None:
        target = max(0, min(body.order_index, len(stages) - 1))
        current = stages.index(stage)
        if target != current:
            stages.pop(current)
            stages.insert(target, stage)
            _renumber(stages)
            changed["order_index"] = stage.order_index

    if not changed:
        counts = _doc_counts(db, project_id)
        return _serialize(stage, counts.get(stage_id, 0),
                          _refs_by_stage(db, [stage_id])[stage_id],
                          _team_access_by_stage(db, [stage_id])[stage_id])

    record_audit(
        db, actor_id=identity.user_id, action="UPDATE_STAGE", resource_type="stage",
        resource_id=stage.stage_id, details={"project_id": str(project_id), **{
            k: str(v) for k, v in changed.items()
        }},
    )
    db.commit()
    db.refresh(stage)
    counts = _doc_counts(db, project_id)
    return _serialize(stage, counts.get(stage_id, 0),
                      _refs_by_stage(db, [stage_id])[stage_id],
                      _team_access_by_stage(db, [stage_id])[stage_id])


@router.delete("/{project_id}/stages/{stage_id}")
def delete_stage(
    project_id: uuid.UUID,
    stage_id: uuid.UUID,
    reassign_to: uuid.UUID | None = Query(
        default=None,
        description="Move this stage's documents to this stage before deleting.",
    ),
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    _load_project(db, identity, project_id)
    _require_project_admin(identity, project_id)

    # No grant-only enforcement is needed on the bulk document reassignment
    # below: this endpoint is already gated to org_admin/project_admin only
    # (the check above), and is_grant_only_confidential_access() always
    # returns False for both — native access, unconditional first branch.
    # The actor here can never be grant-only for any document, so the
    # "grant-only actor bulk-reassigning documents they can't fully see"
    # concern doesn't apply through this endpoint as it exists today.

    stages = _active_stages(db, project_id)
    stage = next((s for s in stages if s.stage_id == stage_id), None)
    if stage is None:
        raise HTTPException(status_code=404, detail="Stage not found")
    if len(stages) <= 1:
        raise HTTPException(status_code=409, detail="A project must keep at least one stage.")

    doc_count = db.execute(
        select(func.count()).select_from(Document).where(Document.stage_id == stage_id)
    ).scalar_one()

    reassigned = 0
    if doc_count > 0:
        if reassign_to is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"'{stage.name}' still has {doc_count} document(s). Reassign them "
                    "to another stage before deleting it."
                ),
            )
        if reassign_to == stage_id:
            raise HTTPException(status_code=422, detail="Cannot reassign a stage's documents to itself")
        target = next((s for s in stages if s.stage_id == reassign_to), None)
        if target is None:
            raise HTTPException(status_code=404, detail="Reassignment target stage not found")
        db.execute(
            Document.__table__.update()
            .where(Document.stage_id == stage_id)
            .values(stage_id=reassign_to)
        )
        reassigned = doc_count

    stage.deleted_at = datetime.now(timezone.utc)
    remaining = [s for s in stages if s.stage_id != stage_id]
    _renumber(remaining)

    # Drop any stage-reference links this stage was on either side of — a
    # deleted stage should not linger in another stage's RAG scope, and its
    # own outbound references are meaningless now.
    db.query(StageReference).filter(
        (StageReference.stage_id == stage_id)
        | (StageReference.references_stage_id == stage_id)
    ).delete(synchronize_session=False)

    # Same for team_stage_access — a deleted stage shouldn't leave dangling
    # access grants around.
    db.query(TeamStageAccess).filter(TeamStageAccess.stage_id == stage_id).delete(
        synchronize_session=False
    )

    # And for required_documents — a deleted stage's checklist is meaningless
    # now (no reassignment concept makes sense here, unlike documents: these
    # are stage-specific requirement definitions, not references to move).
    deleted_requirements = db.query(RequiredDocument).filter(
        RequiredDocument.stage_id == stage_id
    ).delete(synchronize_session=False)

    record_audit(
        db, actor_id=identity.user_id, action="DELETE_STAGE", resource_type="stage",
        resource_id=stage.stage_id,
        details={
            "project_id": str(project_id), "name": stage.name,
            "reassigned_documents": reassigned,
            "reassigned_to": str(reassign_to) if reassigned else None,
            "deleted_requirements": deleted_requirements,
        },
    )
    db.commit()
    return {"status": "deleted", "stage_id": str(stage_id), "reassigned_documents": reassigned}


@router.put("/{project_id}/stages/{stage_id}/references", response_model=StageOut)
def set_stage_references(
    project_id: uuid.UUID,
    stage_id: uuid.UUID,
    body: SetStageReferencesRequest,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    """
    Replace this stage's full set of outbound references. One-way (A->B does not
    imply B->A). Every referenced stage must be another ACTIVE stage in the SAME
    project; a stage cannot reference itself.
    """
    _load_project(db, identity, project_id)
    _require_project_admin(identity, project_id)

    stages = _active_stages(db, project_id)
    stage = next((s for s in stages if s.stage_id == stage_id), None)
    if stage is None:
        raise HTTPException(status_code=404, detail="Stage not found")

    active_ids = {s.stage_id for s in stages}
    wanted: list[uuid.UUID] = []
    for ref_id in body.references:
        if ref_id in wanted:
            continue  # dedupe silently
        if ref_id == stage_id:
            raise HTTPException(status_code=422, detail="A stage cannot reference itself")
        if ref_id not in active_ids:
            # missing, soft-deleted, or in another project — all rejected the same
            raise HTTPException(
                status_code=422,
                detail="A referenced stage must be another active stage in this project",
            )
        wanted.append(ref_id)

    current = set(_refs_by_stage(db, [stage_id])[stage_id])
    if current != set(wanted):
        db.query(StageReference).filter(StageReference.stage_id == stage_id).delete(
            synchronize_session=False
        )
        for ref_id in wanted:
            db.add(StageReference(stage_id=stage_id, references_stage_id=ref_id))
        record_audit(
            db, actor_id=identity.user_id, action="UPDATE_STAGE_REFERENCES",
            resource_type="stage", resource_id=stage_id,
            details={"project_id": str(project_id),
                     "references": ",".join(str(x) for x in wanted) or "(none)"},
        )
        db.commit()

    counts = _doc_counts(db, project_id)
    return _serialize(stage, counts.get(stage_id, 0), wanted,
                      _team_access_by_stage(db, [stage_id])[stage_id])


@router.put("/{project_id}/stages/{stage_id}/team-access", response_model=StageOut)
def set_team_access(
    project_id: uuid.UUID,
    stage_id: uuid.UUID,
    body: SetTeamAccessRequest,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    """
    Replace the full set of teams granted access to this stage. Every team_id
    must be a team in the SAME project. Gates ENFORCEMENT POINT A (upload)
    and ENFORCEMENT POINT B (stage visibility) for regular users — org_admin
    / project_admin bypass both regardless of this list.
    """
    _load_project(db, identity, project_id)
    _require_project_admin(identity, project_id)

    stage = db.get(Stage, stage_id)
    if stage is None or stage.project_id != project_id or stage.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Stage not found")

    project_team_ids = {
        t.team_id for t in db.execute(
            select(Team).where(Team.project_id == project_id)
        ).scalars()
    }
    wanted: list[uuid.UUID] = []
    for team_id in body.team_ids:
        if team_id in wanted:
            continue  # dedupe silently
        if team_id not in project_team_ids:
            raise HTTPException(
                status_code=422,
                detail="A granted team must be a team in this project",
            )
        wanted.append(team_id)

    current = set(_team_access_by_stage(db, [stage_id])[stage_id])
    if current != set(wanted):
        db.query(TeamStageAccess).filter(TeamStageAccess.stage_id == stage_id).delete(
            synchronize_session=False
        )
        for team_id in wanted:
            db.add(TeamStageAccess(team_id=team_id, stage_id=stage_id))
        record_audit(
            db, actor_id=identity.user_id, action="UPDATE_STAGE_TEAM_ACCESS",
            resource_type="stage", resource_id=stage_id,
            details={"project_id": str(project_id),
                     "team_access": ",".join(str(x) for x in wanted) or "(none)"},
        )
        db.commit()

    counts = _doc_counts(db, project_id)
    return _serialize(stage, counts.get(stage_id, 0),
                      _refs_by_stage(db, [stage_id])[stage_id], wanted)


# --- required documents (stage completion checklist) -----------------------

def _serialize_requirement(r: RequiredDocument) -> RequiredDocumentOut:
    return RequiredDocumentOut(
        requirement_id=str(r.requirement_id),
        stage_id=str(r.stage_id),
        name=r.name,
        description=r.description,
        is_mandatory=r.is_mandatory,
        source=r.source.value,
        created_at=r.created_at.isoformat(),
    )


def _load_active_stage(db: Session, project_id: uuid.UUID, stage_id: uuid.UUID) -> Stage:
    stage = db.get(Stage, stage_id)
    if stage is None or stage.project_id != project_id or stage.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Stage not found")
    return stage


@router.get("/{project_id}/stages/{stage_id}/requirements", response_model=list[RequiredDocumentOut])
def list_requirements(
    project_id: uuid.UUID,
    stage_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    """
    A stage's required-document checklist — what R001 (app/services/graph/
    audit_rules.py) evaluates for mandatory evidence, and what the
    Intelligence page's "X/Y requirements satisfied" counts. Read-only, so
    visibility parity with GET /stages: any project member, not admin-only.
    """
    _load_project(db, identity, project_id)
    if not _can_see_project(db, identity, project_id):
        raise HTTPException(status_code=403, detail="You do not have access to this project")
    _load_active_stage(db, project_id, stage_id)

    requirements = db.execute(
        select(RequiredDocument)
        .where(RequiredDocument.stage_id == stage_id)
        .order_by(RequiredDocument.created_at.asc())
    ).scalars().all()
    return [_serialize_requirement(r) for r in requirements]


@router.post(
    "/{project_id}/stages/{stage_id}/requirements",
    response_model=RequiredDocumentOut, status_code=201,
)
def create_requirement(
    project_id: uuid.UUID,
    stage_id: uuid.UUID,
    body: CreateRequiredDocumentRequest,
    background_tasks: BackgroundTasks,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    """
    Defines a new required-document checklist item for a stage — the piece
    that was previously entirely missing: nothing in the app ever created a
    RequiredDocument row, so every stage's completeness was vacuously 100%.
    Schedules a graph re-sync so R001 can evaluate this requirement (it only
    becomes evaluable once it has a matching knowledge-graph Node, created by
    sync_project_graph() — see sync_and_audit_project()'s own docstring).
    """
    project = _load_project(db, identity, project_id)
    _require_project_admin(identity, project_id)
    _load_active_stage(db, project_id, stage_id)

    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Requirement name cannot be empty")

    requirement = RequiredDocument(
        stage_id=stage_id, name=name, description=body.description,
        is_mandatory=body.is_mandatory, source=RequirementSource.custom,
    )
    db.add(requirement)
    db.flush()

    record_audit(
        db, actor_id=identity.user_id, action="CREATE_REQUIRED_DOCUMENT",
        resource_type="required_document", resource_id=requirement.requirement_id,
        details={"project_id": str(project_id), "stage_id": str(stage_id), "name": name},
    )
    db.commit()
    db.refresh(requirement)

    background_tasks.add_task(sync_and_audit_project, project_id, project.tenant_id, identity.user_id)
    return _serialize_requirement(requirement)


@router.patch(
    "/{project_id}/stages/{stage_id}/requirements/{requirement_id}",
    response_model=RequiredDocumentOut,
)
def update_requirement(
    project_id: uuid.UUID,
    stage_id: uuid.UUID,
    requirement_id: uuid.UUID,
    body: UpdateRequiredDocumentRequest,
    background_tasks: BackgroundTasks,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    project = _load_project(db, identity, project_id)
    _require_project_admin(identity, project_id)
    _load_active_stage(db, project_id, stage_id)

    requirement = db.get(RequiredDocument, requirement_id)
    if requirement is None or requirement.stage_id != stage_id:
        raise HTTPException(status_code=404, detail="Requirement not found")

    changed: dict = {}

    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="Requirement name cannot be empty")
        if name != requirement.name:
            changed["name"] = name
            requirement.name = name

    if body.description is not None and body.description != requirement.description:
        changed["description"] = body.description
        requirement.description = body.description

    if body.is_mandatory is not None and body.is_mandatory != requirement.is_mandatory:
        changed["is_mandatory"] = body.is_mandatory
        requirement.is_mandatory = body.is_mandatory

    if not changed:
        return _serialize_requirement(requirement)

    record_audit(
        db, actor_id=identity.user_id, action="UPDATE_REQUIRED_DOCUMENT",
        resource_type="required_document", resource_id=requirement.requirement_id,
        details={"project_id": str(project_id), "stage_id": str(stage_id),
                  **{k: str(v) for k, v in changed.items()}},
    )
    db.commit()
    db.refresh(requirement)

    background_tasks.add_task(sync_and_audit_project, project_id, project.tenant_id, identity.user_id)
    return _serialize_requirement(requirement)


@router.delete("/{project_id}/stages/{stage_id}/requirements/{requirement_id}")
def delete_requirement(
    project_id: uuid.UUID,
    stage_id: uuid.UUID,
    requirement_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    project = _load_project(db, identity, project_id)
    _require_project_admin(identity, project_id)
    _load_active_stage(db, project_id, stage_id)

    requirement = db.get(RequiredDocument, requirement_id)
    if requirement is None or requirement.stage_id != stage_id:
        raise HTTPException(status_code=404, detail="Requirement not found")

    record_audit(
        db, actor_id=identity.user_id, action="DELETE_REQUIRED_DOCUMENT",
        resource_type="required_document", resource_id=requirement.requirement_id,
        details={"project_id": str(project_id), "stage_id": str(stage_id), "name": requirement.name},
    )
    db.delete(requirement)
    db.commit()

    background_tasks.add_task(sync_and_audit_project, project_id, project.tenant_id, identity.user_id)
    return {"status": "deleted", "requirement_id": str(requirement_id)}
