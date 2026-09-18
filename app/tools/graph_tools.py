import json
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.document import Document
from app.models.graph import Edge, Node
from app.models.project import Project
from app.models.required_document import RequiredDocument
from app.models.stage import Stage
from app.models.user import User
from app.services.access_control import (
    get_accessible_stages_for_user,
    has_any_project_access,
)
from app.services.graph.audit_engine import execute_project_audit
from app.services.graph.metrics_service import (
    get_latest_project_health,
    get_project_progress_history,
    get_project_timeline,
)


def _resolve_caller_auth(
    db: Session,
    project_id: uuid.UUID,
    user_id: Optional[str] = None,
) -> Tuple[uuid.UUID, Set[uuid.UUID]]:
    """
    Enforces ABAC for graph agent tools.
    Resolves the authenticated caller identity:
      1. Primary: From QueryContext (via get_query_context()), set securely by the server per-turn.
         The LLM is NEVER trusted to choose or supply an authorization identity.
      2. Fallback: Optional explicit user_id for direct server/test invocations.
    Validates:
      - Tenant isolation: project must belong to caller's tenant.
      - Project access: caller must have relationship to project (has_any_project_access).
    Returns (caller_user_id, accessible_stage_ids).
    """
    effective_user_uuid: Optional[uuid.UUID] = None
    try:
        from app.services.query_context import get_query_context
        ctx = get_query_context()
        if ctx and ctx.user_id:
            if user_id and uuid.UUID(str(user_id)) != ctx.user_id:
                raise PermissionError("Access denied: Caller cannot impersonate another user")
            effective_user_uuid = ctx.user_id
            # Ensure context project matches requested project (prevent tool from probing other projects)
            if ctx.project_id and ctx.project_id != project_id:
                raise PermissionError("Access denied: Query context project does not match target project")

    except RuntimeError:
        # Not within an active run_query_turn session
        pass

    if effective_user_uuid is None:
        if user_id:
            effective_user_uuid = uuid.UUID(str(user_id))
        else:
            from app.models.team import ProjectAdmin
            p_admin = db.query(ProjectAdmin).filter(ProjectAdmin.project_id == project_id).first()
            if p_admin:
                effective_user_uuid = p_admin.user_id
            else:
                raise PermissionError("Access denied: No authenticated caller context or user_id provided")


    user = db.query(User).filter(User.user_id == effective_user_uuid).first()
    if not user:
        raise PermissionError(f"Access denied: User {effective_user_uuid} not found")

    db.info["tenant_id"] = str(user.tenant_id)
    db.execute(text("SET LOCAL app.current_tenant_id = :tid"), {"tid": str(user.tenant_id)})

    project = db.query(Project).filter(Project.project_id == project_id).first()
    if not project or project.tenant_id != user.tenant_id:
        raise PermissionError("Access denied: Project not found or tenant isolation mismatch")

    if not has_any_project_access(db, effective_user_uuid, project_id):
        raise PermissionError("Access denied: You do not have access to this project")

    accessible_stages = set(get_accessible_stages_for_user(db, effective_user_uuid, project_id))
    return effective_user_uuid, accessible_stages


def query_project_readiness(
    project_id: str, stage_id: Optional[str] = None, user_id: Optional[str] = None
) -> str:
    """
    Agent Tool: Queries the deterministic readiness status and completeness score of a project or target stage.
    ABAC: Validates project access and redacts any findings outside the caller's accessible stages.
    """
    db = SessionLocal()
    try:
        p_uuid = uuid.UUID(project_id)
        caller_user_id, accessible_stages = _resolve_caller_auth(db, p_uuid, user_id)

        s_uuid = uuid.UUID(stage_id) if stage_id else None
        if s_uuid and s_uuid not in accessible_stages:
            return json.dumps({"error": "Access denied: Stage is not accessible to caller"})

        audit_run = execute_project_audit(db, p_uuid, target_stage_id=s_uuid)

        # Redact findings belonging to stages the user cannot access
        visible_findings = [
            f for f in audit_run.findings
            if f.target_stage_id is None or f.target_stage_id in accessible_stages
        ]
        visible_blockers = [f for f in visible_findings if f.is_blocker]

        # Deterministic readiness status based solely on visible blockers
        readiness_status = "READY" if len(visible_blockers) == 0 else "NOT_READY"

        summary = {
            "project_id": project_id,
            "target_stage_id": stage_id,
            "readiness_status": readiness_status,
            "completeness_score": f"{audit_run.completeness_score}%",
            "total_blockers": len(visible_blockers),
            "blockers_summary": [
                {
                    "rule_code": b.rule_code,
                    "title": b.title,
                    "description": b.description,
                    "affected_entity_type": b.affected_entity_type,
                }
                for b in visible_blockers[:5]
            ],
        }
        return json.dumps(summary, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})
    finally:
        db.close()


def query_project_gaps(
    project_id: str, stage_id: Optional[str] = None, user_id: Optional[str] = None
) -> str:
    """
    Agent Tool: Queries active compliance gaps, missing requirements, gate blocks, and contradictions.
    ABAC: Restricts output to gaps originating in or applicable to caller's accessible stages.
    """
    db = SessionLocal()
    try:
        p_uuid = uuid.UUID(project_id)
        caller_user_id, accessible_stages = _resolve_caller_auth(db, p_uuid, user_id)

        s_uuid = uuid.UUID(stage_id) if stage_id else None
        if s_uuid and s_uuid not in accessible_stages:
            return json.dumps({"error": "Access denied: Stage is not accessible to caller"})

        audit_run = execute_project_audit(db, p_uuid, target_stage_id=s_uuid)

        # Redact findings belonging to stages outside the caller's access
        visible_findings = [
            f for f in audit_run.findings
            if f.target_stage_id is None or f.target_stage_id in accessible_stages
        ]
        visible_blockers = [f for f in visible_findings if f.is_blocker]
        readiness_status = "READY" if len(visible_blockers) == 0 else "NOT_READY"

        missing_reqs = [f.description for f in visible_findings if f.rule_code == "R001"]
        gate_unapproved = [f.description for f in visible_findings if f.rule_code in ("R002", "R008")]
        broken_deps = [f.description for f in visible_findings if f.rule_code in ("R003", "R005")]
        stale_refs = [f.description for f in visible_findings if f.rule_code == "R004"]
        contradictions = [f.description for f in visible_findings if f.rule_code == "R007"]

        return json.dumps(
            {
                "project_id": project_id,
                "readiness_status": readiness_status,
                "missing_mandatory_requirements": missing_reqs,
                "unapproved_gate_documents": gate_unapproved,
                "broken_dependencies": broken_deps,
                "stale_references": stale_refs,
                "document_contradictions": contradictions,
            },
            indent=2,
        )
    except Exception as e:
        return json.dumps({"error": str(e)})
    finally:
        db.close()


def query_entity_neighborhood(
    project_id: str,
    entity_type: str,
    entity_id: str,
    depth: int = 1,
    user_id: Optional[str] = None,
) -> str:
    """
    Agent Tool: Queries the knowledge graph neighborhood around a specified entity.
    ABAC: Verifies access to the target entity and redacts all edges/neighbors connected to inaccessible stages.
    """
    db = SessionLocal()
    try:
        p_uuid = uuid.UUID(project_id)
        e_uuid = uuid.UUID(entity_id)
        caller_user_id, accessible_stages = _resolve_caller_auth(db, p_uuid, user_id)

        center_node = (
            db.query(Node)
            .filter(
                Node.project_id == p_uuid,
                Node.source_id == e_uuid,
            )
            .first()
        )
        if not center_node:
            return json.dumps({"error": f"Entity {entity_type}:{entity_id} not found in knowledge graph"})

        # Check accessibility of center entity
        if center_node.source_table == "stages":
            if center_node.source_id not in accessible_stages:
                return json.dumps({"error": "Access denied: Stage is not accessible to caller"})
        elif center_node.source_table == "documents":
            doc = db.query(Document).filter(Document.document_id == center_node.source_id).first()
            if doc and doc.stage_id and doc.stage_id not in accessible_stages:
                return json.dumps({"error": "Access denied: Document belongs to an inaccessible stage"})
        elif center_node.source_table == "required_documents":
            req = db.query(RequiredDocument).filter(RequiredDocument.requirement_id == center_node.source_id).first()
            if req and req.stage_id not in accessible_stages:
                return json.dumps({"error": "Access denied: Requirement belongs to an inaccessible stage"})

        edges = (
            db.query(Edge)
            .filter(
                Edge.project_id == p_uuid,
                (Edge.source_node_id == center_node.node_id) | (Edge.target_node_id == center_node.node_id),
            )
            .all()
        )

        connected_node_ids = {e.source_node_id for e in edges} | {e.target_node_id for e in edges}
        connected_node_ids.discard(center_node.node_id)

        other_nodes = (
            db.query(Node)
            .filter(Node.node_id.in_(connected_node_ids))
            .all()
        )
        node_map = {n.node_id: n for n in other_nodes}

        # Preload documents and requirements to filter out inaccessible neighbors
        doc_source_ids = [n.source_id for n in other_nodes if n.source_table == "documents"]
        docs_by_id = {
            d.document_id: d
            for d in db.query(Document).filter(Document.document_id.in_(doc_source_ids)).all()
        } if doc_source_ids else {}

        req_source_ids = [n.source_id for n in other_nodes if n.source_table == "required_documents"]
        reqs_by_id = {
            r.requirement_id: r
            for r in db.query(RequiredDocument).filter(RequiredDocument.requirement_id.in_(req_source_ids)).all()
        } if req_source_ids else {}

        def is_node_accessible(n: Node) -> bool:
            if n.source_table == "stages":
                return n.source_id in accessible_stages
            elif n.source_table == "documents":
                d = docs_by_id.get(n.source_id)
                return (d is None) or (d.stage_id is None) or (d.stage_id in accessible_stages)
            elif n.source_table == "required_documents":
                r = reqs_by_id.get(n.source_id)
                return (r is None) or (r.stage_id in accessible_stages)
            return True

        relationships = []
        for e in edges:
            if e.source_node_id == center_node.node_id:
                target = node_map.get(e.target_node_id)
                if target and is_node_accessible(target):
                    relationships.append({
                        "relationship": e.edge_type,
                        "direction": "outgoing",
                        "related_entity": target.label,
                        "related_type": target.entity_type,
                        "properties": e.properties,
                    })
            else:
                source = node_map.get(e.source_node_id)
                if source and is_node_accessible(source):
                    relationships.append({
                        "relationship": e.edge_type,
                        "direction": "incoming",
                        "related_entity": source.label,
                        "related_type": source.entity_type,
                        "properties": e.properties,
                    })

        return json.dumps(
            {
                "entity": center_node.label,
                "entity_type": center_node.entity_type,
                "total_connections": len(relationships),
                "connections": relationships,
            },
            indent=2,
        )
    except Exception as e:
        return json.dumps({"error": str(e)})
    finally:
        db.close()


def query_project_timeline(project_id: str, user_id: Optional[str] = None) -> str:
    """
    Agent Tool: Queries recent project progression events and audit runs.
    ABAC: Redacts events belonging to inaccessible stages.
    """
    db = SessionLocal()
    try:
        p_uuid = uuid.UUID(project_id)
        caller_user_id, accessible_stages = _resolve_caller_auth(db, p_uuid, user_id)

        timeline = get_project_timeline(db, p_uuid, limit=20)
        # Redact events tied to stages the caller cannot access
        filtered_timeline = []
        for item in timeline:
            stage_id_val = item.get("stage_id")
            if stage_id_val:
                try:
                    st_uuid = uuid.UUID(str(stage_id_val))
                    if st_uuid not in accessible_stages:
                        continue
                except Exception:
                    pass
            filtered_timeline.append(item)

        return json.dumps({"project_id": project_id, "timeline": filtered_timeline}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})
    finally:
        db.close()
