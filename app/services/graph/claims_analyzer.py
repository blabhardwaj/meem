import re
import uuid
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy.orm import Session

from app.models.graph import Claim, Edge, Node
from app.models.document import Document, DocumentVersion
from app.services.graph.audit_rules import FindingSpec


# Extraction patterns for explicit semantic statements / claims
CLAIM_PATTERNS = [
    # 1. Dates / Deadlines / Timelines
    {
        "type": "timeline",
        "subject": "launch date",
        "predicate": "scheduled_for",
        "regex": re.compile(r"(?:launch|delivery|release|go-live|completion)\s+(?:date\s+)?(?:is\s+|set\s+to\s+|:\s*)([A-Za-z0-9\s,\-\/]+?)(?:\.|\n|$)", re.IGNORECASE),
    },
    {
        "type": "timeline",
        "subject": "project deadline",
        "predicate": "scheduled_for",
        "regex": re.compile(r"(?:project\s+deadline|target\s+completion)\s+(?:is\s+|:\s*)([A-Za-z0-9\s,\-\/]+?)(?:\.|\n|$)", re.IGNORECASE),
    },
    # 2. Budgets / Financials
    {
        "type": "financial",
        "subject": "project budget",
        "predicate": "allocated_amount",
        "regex": re.compile(r"(?:total\s+)?(?:project\s+)?(?:budget|cost|funding|allocation)\s+(?:is\s+|of\s+|:\s*)(\$?\s*[0-9,]+(?:\.[0-9]+)?\s*(?:USD|k|million|M|k|K|billion|B)?)(?:\.|\n|$)", re.IGNORECASE),
    },
    # 3. Architecture / Technology direct choices
    {
        "type": "architecture",
        "subject": "primary database",
        "predicate": "technology_choice",
        "regex": re.compile(r"(?:primary\s+)?(?:database|datastore|backend\s+db)\s+(?:is\s+|selected\s+is\s+|:\s*)([A-Za-z0-9_\-]+)(?:\.|\n|$)", re.IGNORECASE),
    },
    {
        "type": "architecture",
        "subject": "cloud provider",
        "predicate": "technology_choice",
        "regex": re.compile(r"(?:cloud\s+provider|hosting\s+platform)\s+(?:is\s+|:\s*)([A-Za-z0-9_\-]+)(?:\.|\n|$)", re.IGNORECASE),
    },
    # 4. Security & Compliance directives
    {
        "type": "security",
        "subject": "encryption at rest",
        "predicate": "requirement_status",
        "regex": re.compile(r"(?:encryption\s+at\s+rest|storage\s+encryption)\s+(?:is\s+|:\s*)(mandatory|required|optional|not\s+required|prohibited|AES-[0-9]+)(?:\.|\n|$)", re.IGNORECASE),
    },
    {
        "type": "security",
        "subject": "two-factor authentication",
        "predicate": "requirement_status",
        "regex": re.compile(r"(?:two-factor\s+authentication|2fa|mfa)\s+(?:is\s+|:\s*)(mandatory|required|optional|disabled|not\s+required)(?:\.|\n|$)", re.IGNORECASE),
    },
    # 5. Performance / Latency SLAs
    {
        "type": "performance",
        "subject": "p95 latency",
        "predicate": "latency_threshold",
        "regex": re.compile(
            r"(?:p95\s+latency|target\s+latency|latency\s+target|response\s+time|p95\s+response\s+time)"
            r"(?:[^\n.:;=]*?)"
            r"(?::|=|is|below|exceed|\*|\s)"
            r"[^0-9\n]*?"
            r"([0-9]+(?:\.[0-9]+)?\s*ms)",
            re.IGNORECASE,
        ),
    },
]


def extract_claims_from_text(
    text: str,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
) -> List[Dict[str, Any]]:
    """
    Extracts structured factual claims from raw text using pattern recognition.
    """
    claims_data: List[Dict[str, Any]] = []
    if not text:
        return claims_data

    req_pat = re.compile(r"\b([A-Z0-9]{2,}(?:-[A-Z0-9]+)+)\b")

    for pattern in CLAIM_PATTERNS:
        matches = pattern["regex"].finditer(text)
        for match in matches:
            val = match.group(1).strip()
            # Basic polarity detection
            polarity = True
            if val.lower() in ("optional", "not required", "disabled", "prohibited", "false"):
                polarity = False

            snippet = match.group(0).strip()

            # Contextual requirement identification:
            # 1. Check current line (from preceding newline to following newline)
            req_code = None

            line_start = text.rfind("\n", 0, match.start())
            line_start = 0 if line_start == -1 else line_start + 1
            line_end = text.find("\n", match.end())
            line_end = len(text) if line_end == -1 else line_end
            current_line = text[line_start:line_end]

            line_matches = list(req_pat.finditer(current_line))
            if line_matches:
                # Prefer match preceding match.start() on this line (e.g. bullet label)
                rel_pos = match.start() - line_start
                preceding = [m for m in line_matches if m.start() <= rel_pos]
                if preceding:
                    req_code = preceding[-1].group(1)
                else:
                    req_code = min(line_matches, key=lambda m: abs(m.start() - rel_pos)).group(1)
            else:
                # 2. Check surrounding window (+/- 250 chars)
                w_start = max(0, match.start() - 250)
                w_end = min(len(text), match.end() + 250)
                w_text = text[w_start:w_end]
                cands = list(req_pat.finditer(w_text))
                if cands:
                    # Prefer formal requirement identifiers over ticket numbers, then proximity
                    def cand_rank(m):
                        val_code = m.group(1)
                        is_req = bool(re.search(r"(?:REQ|CHK|SLA|SPEC|NFR|BR|CR|PERF|SEC|SYS)", val_code, re.I)) or len(val_code.split("-")) >= 3
                        dist = abs((w_start + m.start()) - match.start())
                        return (0 if is_req else 1, dist)

                    best = min(cands, key=cand_rank)
                    req_code = best.group(1)

            source_loc = {
                "start": match.start(),
                "end": match.end(),
                "claim_type": pattern["type"],
            }
            if req_code:
                source_loc["requirement_context"] = req_code

            claims_data.append({
                "tenant_id": tenant_id,
                "project_id": project_id,
                "document_id": document_id,
                "version_id": version_id,
                "subject": pattern["subject"],
                "predicate": pattern["predicate"],
                "object": val,
                "polarity": polarity,
                "snippet": snippet,
                "source_locator": source_loc,
                "confidence": 0.95,
            })

    return claims_data


def persist_claims(
    db: Session,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    claims_data: List[Dict[str, Any]],
    extraction_run_id: Optional[uuid.UUID] = None,
) -> List[Claim]:
    """
    Persists extracted claims into knowledge.claims idempotently.
    Overwrites previous claims for the same document version.
    """
    db.query(Claim).filter(Claim.version_id == version_id).delete()

    created_claims: List[Claim] = []
    for cd in claims_data:
        claim = Claim(
            tenant_id=tenant_id,
            project_id=project_id,
            document_id=document_id,
            version_id=version_id,
            subject=cd["subject"],
            predicate=cd["predicate"],
            object=cd["object"],
            polarity=cd.get("polarity", True),
            snippet=cd.get("snippet", ""),
            source_locator=cd.get("source_locator", {}),
            confidence=cd.get("confidence", 1.0),
            extraction_run_id=extraction_run_id,
        )
        db.add(claim)
        created_claims.append(claim)

    db.flush()
    return created_claims


def detect_project_contradictions(
    db: Session,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    evaluated_stage_ids: Optional[List[uuid.UUID]] = None,
) -> List[FindingSpec]:
    """
    Detects contradictions between claims across documents in scope.
    Generates CONFLICTS_WITH edges between conflicting document nodes and
    returns R009 blocker findings.
    """
    findings: List[FindingSpec] = []

    # Query claims joined with Document to check stage scope
    query = (
        db.query(Claim, Document)
        .join(Document, Claim.document_id == Document.document_id)
        .filter(Claim.tenant_id == tenant_id, Claim.project_id == project_id)
        .order_by(Document.created_at.asc(), Claim.created_at.asc())
    )

    if evaluated_stage_ids is not None:
        query = query.filter(Document.stage_id.in_(evaluated_stage_ids))

    results = query.all()
    if not results:
        return findings

    # Group claims by (subject.lower(), predicate.lower())
    groups: Dict[Tuple[str, str], List[Tuple[Claim, Document]]] = {}
    for claim, doc in results:
        key = (claim.subject.lower().strip(), claim.predicate.lower().strip())
        groups.setdefault(key, []).append((claim, doc))

    # Pre-fetch document graph nodes
    doc_ids = list({doc.document_id for _, doc in results})
    doc_nodes = (
        db.query(Node)
        .filter(
            Node.tenant_id == tenant_id,
            Node.project_id == project_id,
            Node.source_table == "documents",
            Node.source_id.in_(doc_ids),
        )
        .all()
    )
    node_map = {n.source_id: n for n in doc_nodes}

    def is_formal_req(code: Optional[str]) -> bool:
        if not code:
            return False
        return bool(re.search(r"(?:REQ|CHK|SLA|SPEC|NFR|BR|CR|PERF|SEC|SYS)", code, re.I)) or len(code.split("-")) >= 3

    def pair_rank(c_a: Claim, c_b: Claim) -> tuple[int, int]:
        loc_a = c_a.source_locator or {}
        loc_b = c_b.source_locator or {}
        req_a = loc_a.get("requirement_context")
        req_b = loc_b.get("requirement_context")
        if req_a and req_b and req_a == req_b:
            return (0, 0 if is_formal_req(req_a) else 1)
        if is_formal_req(req_a) or is_formal_req(req_b):
            return (1, 0)
        if req_a or req_b:
            return (2, 0)
        return (3, 0)

    seen_doc_pairs: set = set()

    for (subj, pred), claim_tuples in groups.items():
        if len(claim_tuples) < 2:
            continue

        candidate_pairs = []
        for i in range(len(claim_tuples)):
            for j in range(i + 1, len(claim_tuples)):
                c1, doc1 = claim_tuples[i]
                c2, doc2 = claim_tuples[j]

                # If same document, don't flag cross-document contradiction
                if doc1.document_id == doc2.document_id:
                    continue

                # Ensure upstream/earlier created document is doc1
                if doc1.created_at and doc2.created_at and doc1.created_at > doc2.created_at:
                    c1, doc1, c2, doc2 = c2, doc2, c1, doc1

                # Contradiction criteria:
                # 1. Different polarities (e.g. mandatory vs optional)
                # 2. Different values for same polarity
                val1_clean = c1.object.lower().strip().replace("$", "").replace(",", "")
                val2_clean = c2.object.lower().strip().replace("$", "").replace(",", "")

                is_contradiction = False
                if c1.polarity != c2.polarity:
                    is_contradiction = True
                elif val1_clean != val2_clean:
                    is_contradiction = True

                if is_contradiction:
                    candidate_pairs.append((c1, doc1, c2, doc2))

        # Sort so highest-fidelity requirement matches are processed first
        candidate_pairs.sort(key=lambda item: pair_rank(item[0], item[2]))

        for c1, doc1, c2, doc2 in candidate_pairs:
            doc_pair_key = (tuple(sorted([str(doc1.document_id), str(doc2.document_id)])), subj, pred)
            if doc_pair_key in seen_doc_pairs:
                continue
            seen_doc_pairs.add(doc_pair_key)

            loc1 = c1.source_locator or {}
            loc2 = c2.source_locator or {}
            req_ctx1 = loc1.get("requirement_context")
            req_ctx2 = loc2.get("requirement_context")

            if req_ctx1 and req_ctx2:
                if req_ctx1 == req_ctx2:
                    resolved_req = req_ctx1
                elif is_formal_req(req_ctx1) and not is_formal_req(req_ctx2):
                    resolved_req = req_ctx1
                elif is_formal_req(req_ctx2) and not is_formal_req(req_ctx1):
                    resolved_req = req_ctx2
                else:
                    resolved_req = req_ctx1
            elif req_ctx1:
                resolved_req = req_ctx1
            else:
                resolved_req = req_ctx2

            # Upsert CONFLICTS_WITH edge in graph
            node1 = node_map.get(doc1.document_id)
            node2 = node_map.get(doc2.document_id)
            if node1 and node2:
                existing_edge = (
                    db.query(Edge)
                    .filter(
                        Edge.source_node_id == node1.node_id,
                        Edge.target_node_id == node2.node_id,
                        Edge.edge_type == "CONFLICTS_WITH",
                    )
                    .first()
                )
                conflict_props = {
                    "claim1_id": str(c1.claim_id),
                    "claim2_id": str(c2.claim_id),
                    "subject": c1.subject,
                    "predicate": c1.predicate,
                    "value1": c1.object,
                    "value2": c2.object,
                    "snippet1": c1.snippet,
                    "snippet2": c2.snippet,
                }
                if resolved_req:
                    conflict_props["requirement_context"] = resolved_req

                if existing_edge:
                    existing_edge.properties = conflict_props
                else:
                    db.add(
                        Edge(
                            tenant_id=tenant_id,
                            project_id=project_id,
                            source_node_id=node1.node_id,
                            target_node_id=node2.node_id,
                            edge_type="CONFLICTS_WITH",
                            confidence=1.0,
                            properties=conflict_props,
                        )
                    )
                db.flush()

            # Emit R009 blocker finding
            finding_details = {
                "conflicting_document_id": str(doc2.document_id),
                "conflicting_document_name": doc2.original_filename,
                "subject": c1.subject,
                "predicate": c1.predicate,
                "value_a": c1.object,
                "value_b": c2.object,
                "snippet_a": c1.snippet,
                "snippet_b": c2.snippet,
                "claim1_id": str(c1.claim_id),
                "claim2_id": str(c2.claim_id),
            }
            if resolved_req:
                finding_details["requirement_context"] = resolved_req
                desc = (
                    f"Direct contradiction detected between '{doc1.original_filename}' and '{doc2.original_filename}' "
                    f"regarding '{c1.subject}' ({resolved_req}): '{c1.object}' vs '{c2.object}'."
                )
            else:
                desc = (
                    f"Direct contradiction detected between '{doc1.original_filename}' and '{doc2.original_filename}' "
                    f"regarding '{c1.subject}': '{c1.object}' vs '{c2.object}'."
                )

            findings.append(
                FindingSpec(
                    rule_code="R009",
                    severity="HIGH",
                    is_blocker=True,
                    title="Document Contradiction Detected",
                    description=desc,
                    affected_entity_type="document",
                    affected_entity_id=doc1.document_id,
                    target_stage_id=doc1.stage_id,
                    evidence_sources=[
                        {
                            "document_id": str(doc1.document_id),
                            "version_id": str(c1.version_id),
                            "snippet": c1.snippet,
                            "claim_id": str(c1.claim_id),
                        },
                        {
                            "document_id": str(doc2.document_id),
                            "version_id": str(c2.version_id),
                            "snippet": c2.snippet,
                            "claim_id": str(c2.claim_id),
                        },
                    ],
                    details=finding_details,
                )
            )

    return findings
