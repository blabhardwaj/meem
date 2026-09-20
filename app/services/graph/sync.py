import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import and_, select, delete
from sqlalchemy.orm import Session

from app.models.graph import Edge, Node
from app.models.project import Project
from app.models.stage import Stage, StageReference, TeamStageAccess
from app.models.document import Document, DocumentVersion
from app.models.required_document import RequiredDocument
from app.models.team import Team, ProjectAdmin, UserTeamMembership
from app.models.user import User


class SyncResult:
    def __init__(self, project_id: uuid.UUID):
        self.project_id = project_id
        self.nodes_synced: int = 0
        self.edges_synced: int = 0
        self.errors: List[str] = []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": str(self.project_id),
            "nodes_synced": self.nodes_synced,
            "edges_synced": self.edges_synced,
            "errors": self.errors,
        }


def _upsert_node(
    db: Session,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    entity_type: str,
    source_table: str,
    source_id: uuid.UUID,
    label: str,
    properties: Optional[Dict[str, Any]] = None,
) -> Node:
    """Idempotently insert or update a graph node based on (tenant_id, project_id, source_table, source_id)."""
    node = db.query(Node).filter(
        Node.tenant_id == tenant_id,
        Node.project_id == project_id,
        Node.source_table == source_table,
        Node.source_id == source_id,
    ).first()

    props = properties or {}
    if node:
        node.label = label
        node.project_id = project_id
        node.properties = props
        node.updated_at = datetime.now(timezone.utc)
    else:
        node = Node(
            tenant_id=tenant_id,
            project_id=project_id,
            entity_type=entity_type,
            source_table=source_table,
            source_id=source_id,
            label=label,
            properties=props,
        )
        db.add(node)
        db.flush()

    return node


def _upsert_edge(
    db: Session,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    source_node_id: uuid.UUID,
    target_node_id: uuid.UUID,
    edge_type: str,
    properties: Optional[Dict[str, Any]] = None,
    confidence: float = 1.0,
    provenance: Optional[Dict[str, Any]] = None,
) -> Edge:
    """Idempotently insert or update a graph edge based on (source_node_id, target_node_id, edge_type)."""
    edge = db.query(Edge).filter(
        Edge.source_node_id == source_node_id,
        Edge.target_node_id == target_node_id,
        Edge.edge_type == edge_type,
    ).first()

    props = properties or {}
    if edge:
        edge.properties = props
        edge.confidence = confidence
        edge.provenance = provenance
    else:
        edge = Edge(
            tenant_id=tenant_id,
            project_id=project_id,
            source_node_id=source_node_id,
            target_node_id=target_node_id,
            edge_type=edge_type,
            properties=props,
            confidence=confidence,
            provenance=provenance,
        )
        db.add(edge)
        db.flush()

    return edge


def sync_project_graph(db: Session, project_id: uuid.UUID) -> SyncResult:
    """
    Synchronizes all authoritative relational entities and dynamic stage topology
    for a given project into the 'knowledge.*' graph tables.

    Master Plan v2, item 5: also removes STALE derived facts, not just
    upserting current ones. Previously only PRECEDES edges were cleaned up;
    every other edge type (ALLOWED_REFERENCE, REQUIRES/ORIGINATED_IN/
    APPLIES_TO, ASSIGNED_TEAM, MANAGES_PROJECT, MEMBER_OF, BELONGS_TO_STAGE,
    OWNED_BY) could accumulate forever once its underlying row was removed
    or reassigned. Node.source_table in (required_documents, teams, users)
    is cleaned up too — Edge rows have ON DELETE CASCADE to Node, so deleting
    a stale node also removes every edge touching it for free; the explicit
    edge cleanup below only covers edges where BOTH endpoint nodes still
    exist but the relationship between them (e.g. a revoked team_stage_access
    grant) does not.
    """
    result = SyncResult(project_id)
    project = db.query(Project).filter(Project.project_id == project_id).first()
    if not project:
        result.errors.append(f"Project {project_id} not found")
        return result

    tenant_id = project.tenant_id
    node_map: Dict[Tuple[str, uuid.UUID], Node] = {}

    # Active (source_node_id, target_node_id) pairs per edge type, populated
    # as the sync below creates/refreshes each edge — used for the stale-edge
    # cleanup pass at the end. PRECEDES already tracked its own pairs inline;
    # kept that variable name for the rest.
    active_allowed_reference_pairs: Set[Tuple[uuid.UUID, uuid.UUID]] = set()
    active_requires_pairs: Set[Tuple[uuid.UUID, uuid.UUID]] = set()
    active_originated_in_pairs: Set[Tuple[uuid.UUID, uuid.UUID]] = set()
    active_applies_to_pairs: Set[Tuple[uuid.UUID, uuid.UUID]] = set()
    active_assigned_team_pairs: Set[Tuple[uuid.UUID, uuid.UUID]] = set()
    active_manages_project_pairs: Set[Tuple[uuid.UUID, uuid.UUID]] = set()
    active_member_of_pairs: Set[Tuple[uuid.UUID, uuid.UUID]] = set()
    active_belongs_to_stage_pairs: Set[Tuple[uuid.UUID, uuid.UUID]] = set()
    active_owned_by_pairs: Set[Tuple[uuid.UUID, uuid.UUID]] = set()
    active_has_version_pairs: Set[Tuple[uuid.UUID, uuid.UUID]] = set()
    active_uploaded_pairs: Set[Tuple[uuid.UUID, uuid.UUID]] = set()
    active_approved_by_pairs: Set[Tuple[uuid.UUID, uuid.UUID]] = set()

    # Active source_ids per node source_table that can genuinely disappear
    # from this project (documents are never hard-deleted today, so their
    # node type is excluded — see docstring above).
    active_requirement_ids: Set[uuid.UUID] = set()
    active_team_ids: Set[uuid.UUID] = set()
    active_user_ids: Set[uuid.UUID] = set()
    active_version_ids: Set[uuid.UUID] = set()

    # 1. Sync Project Node
    proj_node = _upsert_node(
        db=db,
        tenant_id=tenant_id,
        project_id=project_id,
        entity_type="project",
        source_table="projects",
        source_id=project.project_id,
        label=project.name,
        properties={"status": getattr(project, "status", "active")},
    )
    node_map[("projects", project.project_id)] = proj_node
    result.nodes_synced += 1

    # 2. Sync Active Stages & Dynamic Topology
    # Non-deleted stages ordered by (order_index ASC, created_at ASC)
    active_stages = (
        db.query(Stage)
        .filter(Stage.project_id == project_id, Stage.deleted_at.is_(None))
        .order_by(Stage.order_index.asc(), Stage.created_at.asc())
        .all()
    )

    stage_nodes: List[Node] = []
    for stage in active_stages:
        s_node = _upsert_node(
            db=db,
            tenant_id=tenant_id,
            project_id=project_id,
            entity_type="stage",
            source_table="stages",
            source_id=stage.stage_id,
            label=stage.name,
            properties={
                "order_index": stage.order_index,
                "requires_approval": stage.requires_approval,
            },
        )
        stage_nodes.append(s_node)
        node_map[("stages", stage.stage_id)] = s_node
        result.nodes_synced += 1

        # Project -> CONTAINS_STAGE -> Stage
        _upsert_edge(
            db=db,
            tenant_id=tenant_id,
            project_id=project_id,
            source_node_id=proj_node.node_id,
            target_node_id=s_node.node_id,
            edge_type="CONTAINS_STAGE",
        )
        result.edges_synced += 1

    # Synchronize Dynamic PRECEDES Edges (Immediate adjacency only: Stage_i -> Stage_{i+1})
    # First, collect valid new PRECEDES pairs
    active_precedes_pairs: Set[Tuple[uuid.UUID, uuid.UUID]] = set()
    for i in range(len(stage_nodes) - 1):
        prev_node = stage_nodes[i]
        next_node = stage_nodes[i + 1]
        active_precedes_pairs.add((prev_node.node_id, next_node.node_id))
        _upsert_edge(
            db=db,
            tenant_id=tenant_id,
            project_id=project_id,
            source_node_id=prev_node.node_id,
            target_node_id=next_node.node_id,
            edge_type="PRECEDES",
            properties={"immediate": True},
        )
        result.edges_synced += 1

    # Remove stale PRECEDES edges for this project (e.g. from prior stage ordering or archived stages)
    current_precedes_edges = (
        db.query(Edge)
        .filter(Edge.project_id == project_id, Edge.edge_type == "PRECEDES")
        .all()
    )
    for edge in current_precedes_edges:
        if (edge.source_node_id, edge.target_node_id) not in active_precedes_pairs:
            db.delete(edge)

    # 3. Sync Permitted Cross-Stage References (ALLOWED_REFERENCE)
    # stage_references(stage_id, references_stage_id) -> (Stage: stage_id) -[ALLOWED_REFERENCE]-> (Stage: references_stage_id)
    stage_ids = [s.stage_id for s in active_stages]
    if stage_ids:
        stage_refs = (
            db.query(StageReference)
            .filter(StageReference.stage_id.in_(stage_ids))
            .all()
        )
        for sref in stage_refs:
            source_node = node_map.get(("stages", sref.stage_id))
            target_node = node_map.get(("stages", sref.references_stage_id))
            if source_node and target_node:
                active_allowed_reference_pairs.add((source_node.node_id, target_node.node_id))
                _upsert_edge(
                    db=db,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    source_node_id=source_node.node_id,
                    target_node_id=target_node.node_id,
                    edge_type="ALLOWED_REFERENCE",
                )
                result.edges_synced += 1

    # 4. Sync Requirements (required_documents)
    reqs = (
        db.query(RequiredDocument)
        .filter(RequiredDocument.stage_id.in_(stage_ids))
        .all()
    )
    for req in reqs:
        r_node = _upsert_node(
            db=db,
            tenant_id=tenant_id,
            project_id=project_id,
            entity_type="requirement",
            source_table="required_documents",
            source_id=req.requirement_id,
            label=req.name,
            properties={
                "is_mandatory": req.is_mandatory,
                "description": req.description,
                "source": str(req.source) if req.source else "custom",
                "stage_id": str(req.stage_id) if req.stage_id else None,
            },
        )
        node_map[("required_documents", req.requirement_id)] = r_node
        result.nodes_synced += 1
        active_requirement_ids.add(req.requirement_id)

        # Stage -> REQUIRES -> Requirement
        stage_node = node_map.get(("stages", req.stage_id))
        if stage_node:
            active_requires_pairs.add((stage_node.node_id, r_node.node_id))
            _upsert_edge(
                db=db,
                tenant_id=tenant_id,
                project_id=project_id,
                source_node_id=stage_node.node_id,
                target_node_id=r_node.node_id,
                edge_type="REQUIRES",
                properties={"is_mandatory": req.is_mandatory},
            )
            result.edges_synced += 1

            # Requirement -> ORIGINATED_IN -> Stage
            active_originated_in_pairs.add((r_node.node_id, stage_node.node_id))
            _upsert_edge(
                db=db,
                tenant_id=tenant_id,
                project_id=project_id,
                source_node_id=r_node.node_id,
                target_node_id=stage_node.node_id,
                edge_type="ORIGINATED_IN",
            )
            result.edges_synced += 1

            # Deterministic default: Requirement -> APPLIES_TO -> Stage (originating stage)
            active_applies_to_pairs.add((r_node.node_id, stage_node.node_id))
            _upsert_edge(
                db=db,
                tenant_id=tenant_id,
                project_id=project_id,
                source_node_id=r_node.node_id,
                target_node_id=stage_node.node_id,
                edge_type="APPLIES_TO",
                properties={"rule": "originating_default"},
            )
            result.edges_synced += 1

    # 5. Sync Teams & Team Stage Access
    teams = db.query(Team).filter(Team.project_id == project_id).all()
    for team in teams:
        t_node = _upsert_node(
            db=db,
            tenant_id=tenant_id,
            project_id=project_id,
            entity_type="team",
            source_table="teams",
            source_id=team.team_id,
            label=team.name,
        )
        node_map[("teams", team.team_id)] = t_node
        result.nodes_synced += 1
        active_team_ids.add(team.team_id)

    # Team -> ASSIGNED_TEAM -> Stage
    team_stage_accesses = (
        db.query(TeamStageAccess)
        .filter(TeamStageAccess.stage_id.in_(stage_ids))
        .all()
    )
    for tsa in team_stage_accesses:
        t_node = node_map.get(("teams", tsa.team_id))
        s_node = node_map.get(("stages", tsa.stage_id))
        if t_node and s_node:
            active_assigned_team_pairs.add((t_node.node_id, s_node.node_id))
            _upsert_edge(
                db=db,
                tenant_id=tenant_id,
                project_id=project_id,
                source_node_id=t_node.node_id,
                target_node_id=s_node.node_id,
                edge_type="ASSIGNED_TEAM",
                properties={"access_granted": True},
            )
            result.edges_synced += 1

    # 6. Sync Users & Project Admins
    admins = db.query(ProjectAdmin).filter(ProjectAdmin.project_id == project_id).all()
    for pa in admins:
        user = db.query(User).filter(User.user_id == pa.user_id).first()
        if user:
            u_node = _upsert_node(
                db=db,
                tenant_id=tenant_id,
                project_id=project_id,
                entity_type="user",
                source_table="users",
                source_id=user.user_id,
                label=user.full_name or user.email,
            )
            node_map[("users", user.user_id)] = u_node
            result.nodes_synced += 1
            active_user_ids.add(user.user_id)

            active_manages_project_pairs.add((u_node.node_id, proj_node.node_id))
            _upsert_edge(
                db=db,
                tenant_id=tenant_id,
                project_id=project_id,
                source_node_id=u_node.node_id,
                target_node_id=proj_node.node_id,
                edge_type="MANAGES_PROJECT",
                properties={"role": "admin"},
            )
            result.edges_synced += 1

    # Sync project team members
    memberships = db.query(UserTeamMembership).filter(UserTeamMembership.project_id == project_id).all()
    for m in memberships:
        user = db.query(User).filter(User.user_id == m.user_id).first()
        if user:
            u_node = _upsert_node(
                db=db,
                tenant_id=tenant_id,
                project_id=project_id,
                entity_type="user",
                source_table="users",
                source_id=user.user_id,
                label=user.full_name or user.email,
            )
            node_map[("users", user.user_id)] = u_node
            result.nodes_synced += 1
            active_user_ids.add(user.user_id)

            t_node = node_map.get(("teams", m.team_id))
            if t_node:
                active_member_of_pairs.add((u_node.node_id, t_node.node_id))
                _upsert_edge(
                    db=db,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    source_node_id=u_node.node_id,
                    target_node_id=t_node.node_id,
                    edge_type="MEMBER_OF",
                    properties={"role": m.role.value if hasattr(m.role, "value") else str(m.role)},
                )
                result.edges_synced += 1


    # 7. Sync Documents & Document Versions
    docs = (
        db.query(Document)
        .filter(Document.project_id == project_id)
        .all()
    )
    for doc in docs:
        versions = (
            db.query(DocumentVersion)
            .filter(DocumentVersion.document_id == doc.document_id)
            .order_by(DocumentVersion.version_number.asc())
            .all()
        )
        current_ver = (
            next((v for v in versions if v.version_id == doc.current_version_id), None)
            or (versions[-1] if versions else None)
        )

        latest_uploader = None
        current_approver = None
        current_approval_status = "pending"
        if current_ver:
            if current_ver.uploaded_by:
                u_row = db.query(User).filter(User.user_id == current_ver.uploaded_by).first()
                if u_row:
                    latest_uploader = u_row.full_name or u_row.email
            if current_ver.approved_by:
                a_row = db.query(User).filter(User.user_id == current_ver.approved_by).first()
                if a_row:
                    current_approver = a_row.full_name or a_row.email
                    current_approval_status = "approved"

        d_node = _upsert_node(
            db=db,
            tenant_id=tenant_id,
            project_id=project_id,
            entity_type="document",
            source_table="documents",
            source_id=doc.document_id,
            label=doc.original_filename or "Untitled Document",
            properties={
                "current_version_id": str(doc.current_version_id) if doc.current_version_id else None,
                "sensitivity": getattr(doc, "sensitivity_level", None),
                "original_filename": doc.original_filename,
                # project_intelligence.py's get_neighborhood() redacts document
                # nodes by checking properties["stage_id"] against the caller's
                # accessible stages — it was never actually set here, silently
                # weakening that redaction for every document node. Fixed.
                "stage_id": str(doc.stage_id) if doc.stage_id else None,
                "latest_uploader": latest_uploader,
                "current_approver": current_approver,
                "current_approval_status": current_approval_status,
            },
        )
        node_map[("documents", doc.document_id)] = d_node
        result.nodes_synced += 1

        # Document -> BELONGS_TO_STAGE -> Stage
        if doc.stage_id:
            s_node = node_map.get(("stages", doc.stage_id))
            if s_node:
                active_belongs_to_stage_pairs.add((d_node.node_id, s_node.node_id))
                _upsert_edge(
                    db=db,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    source_node_id=d_node.node_id,
                    target_node_id=s_node.node_id,
                    edge_type="BELONGS_TO_STAGE",
                )
                result.edges_synced += 1

        # Document -> OWNED_BY -> Team
        if doc.uploaded_as_team_id:
            t_node = node_map.get(("teams", doc.uploaded_as_team_id))
            if t_node:
                active_owned_by_pairs.add((d_node.node_id, t_node.node_id))
                _upsert_edge(
                    db=db,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    source_node_id=d_node.node_id,
                    target_node_id=t_node.node_id,
                    edge_type="OWNED_BY",
                )
                result.edges_synced += 1

        # Sync DocumentVersion nodes and governance edges
        for v in versions:
            v_node = _upsert_node(
                db=db,
                tenant_id=tenant_id,
                project_id=project_id,
                entity_type="document_version",
                source_table="document_versions",
                source_id=v.version_id,
                label=f"{doc.original_filename or 'Document'} v{v.version_number}",
                properties={
                    "document_id": str(doc.document_id),
                    "version_number": v.version_number,
                    "stage_id": str(doc.stage_id) if doc.stage_id else None,
                    "created_at": v.created_at.isoformat() if v.created_at else None,
                    "approved_at": v.approved_at.isoformat() if v.approved_at else None,
                    "provenance_event_id": str(v.provenance_event_id) if v.provenance_event_id else None,
                },
            )
            node_map[("document_versions", v.version_id)] = v_node
            result.nodes_synced += 1
            active_version_ids.add(v.version_id)

            # Document -> HAS_VERSION -> DocumentVersion
            active_has_version_pairs.add((d_node.node_id, v_node.node_id))
            _upsert_edge(
                db=db,
                tenant_id=tenant_id,
                project_id=project_id,
                source_node_id=d_node.node_id,
                target_node_id=v_node.node_id,
                edge_type="HAS_VERSION",
                properties={"version_number": v.version_number},
            )
            result.edges_synced += 1

            # User -> UPLOADED -> DocumentVersion
            if v.uploaded_by:
                u_node = node_map.get(("users", v.uploaded_by))
                if not u_node:
                    u_inst = db.query(User).filter(User.user_id == v.uploaded_by).first()
                    if u_inst:
                        u_node = _upsert_node(
                            db=db,
                            tenant_id=tenant_id,
                            project_id=project_id,
                            entity_type="user",
                            source_table="users",
                            source_id=u_inst.user_id,
                            label=u_inst.full_name or u_inst.email,
                        )
                        node_map[("users", u_inst.user_id)] = u_node
                        result.nodes_synced += 1
                        active_user_ids.add(u_inst.user_id)
                if u_node:
                    active_uploaded_pairs.add((u_node.node_id, v_node.node_id))
                    _upsert_edge(
                        db=db,
                        tenant_id=tenant_id,
                        project_id=project_id,
                        source_node_id=u_node.node_id,
                        target_node_id=v_node.node_id,
                        edge_type="UPLOADED",
                        properties={
                            "timestamp": v.created_at.isoformat() if v.created_at else None,
                            "provenance_event_id": str(v.provenance_event_id) if v.provenance_event_id else None,
                        },
                    )
                    result.edges_synced += 1

            # User -> APPROVED_BY -> DocumentVersion
            if v.approved_by:
                app_node = node_map.get(("users", v.approved_by))
                if not app_node:
                    app_inst = db.query(User).filter(User.user_id == v.approved_by).first()
                    if app_inst:
                        app_node = _upsert_node(
                            db=db,
                            tenant_id=tenant_id,
                            project_id=project_id,
                            entity_type="user",
                            source_table="users",
                            source_id=app_inst.user_id,
                            label=app_inst.full_name or app_inst.email,
                        )
                        node_map[("users", app_inst.user_id)] = app_node
                        result.nodes_synced += 1
                        active_user_ids.add(app_inst.user_id)
                if app_node:
                    active_approved_by_pairs.add((app_node.node_id, v_node.node_id))
                    _upsert_edge(
                        db=db,
                        tenant_id=tenant_id,
                        project_id=project_id,
                        source_node_id=app_node.node_id,
                        target_node_id=v_node.node_id,
                        edge_type="APPROVED_BY",
                        properties={
                            "timestamp": v.approved_at.isoformat() if v.approved_at else None,
                            "provenance_event_id": str(v.provenance_event_id) if v.provenance_event_id else None,
                        },
                    )
                    result.edges_synced += 1

    # 8. Stale-edge cleanup — same pattern as the existing PRECEDES cleanup
    # above, extended to every other edge type this function owns (verified
    # each is written ONLY here, never by relationship_extractor.py/
    # claims_analyzer.py, so this can't delete another subsystem's edges).
    edge_types_and_active_pairs = (
        ("ALLOWED_REFERENCE", active_allowed_reference_pairs),
        ("REQUIRES", active_requires_pairs),
        ("ORIGINATED_IN", active_originated_in_pairs),
        ("APPLIES_TO", active_applies_to_pairs),
        ("ASSIGNED_TEAM", active_assigned_team_pairs),
        ("MANAGES_PROJECT", active_manages_project_pairs),
        ("MEMBER_OF", active_member_of_pairs),
        ("BELONGS_TO_STAGE", active_belongs_to_stage_pairs),
        ("OWNED_BY", active_owned_by_pairs),
        ("HAS_VERSION", active_has_version_pairs),
        ("UPLOADED", active_uploaded_pairs),
        ("APPROVED_BY", active_approved_by_pairs),
    )
    for edge_type, active_pairs in edge_types_and_active_pairs:
        current_edges = (
            db.query(Edge)
            .filter(Edge.project_id == project_id, Edge.edge_type == edge_type)
            .all()
        )
        for edge in current_edges:
            if (edge.source_node_id, edge.target_node_id) not in active_pairs:
                db.delete(edge)

    # 9. Stale-node cleanup for source_tables that can genuinely disappear
    # from this project (a removed team/requirement, or a user with no
    # remaining membership/admin role here). Edge rows have ON DELETE CASCADE
    # to Node, so this also removes every edge still touching a stale node —
    # covers cases the pair-based cleanup above can't (e.g. BOTH endpoints
    # gone at once). Documents are excluded: never hard-deleted today.
    node_types_and_active_ids = (
        ("required_documents", active_requirement_ids),
        ("teams", active_team_ids),
        ("users", active_user_ids),
        ("document_versions", active_version_ids),
    )
    for source_table, active_ids in node_types_and_active_ids:
        current_nodes = (
            db.query(Node)
            .filter(Node.project_id == project_id, Node.source_table == source_table)
            .all()
        )
        for node in current_nodes:
            if node.source_id not in active_ids:
                db.delete(node)

    db.commit()
    return result
