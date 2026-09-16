import unittest
import uuid
from datetime import datetime, timezone
from app.database import SessionLocal
from app.models.graph import (
    AuditFinding,
    AuditRun,
    Claim,
    Edge,
    Node,
    ProjectMetricSnapshot,
    StageMetricSnapshot,
)
from app.models.project import Project
from app.models.stage import Stage
from app.models.team import Team
from app.models.tenant import Tenant
from app.models.user import User
from app.models.audit import AuditLog
from app.services.graph.metrics_service import (
    get_latest_project_health,
    get_project_progress_history,
    get_project_timeline,
    get_stage_health_history,
)


class TestMetricsService(unittest.TestCase):
    def setUp(self):
        self.db = SessionLocal()
        self.tenant = self.db.query(Tenant).first()
        self.assertIsNotNone(self.tenant, "Tenant required")
        self.user = self.db.query(User).filter(User.tenant_id == self.tenant.tenant_id).first()
        self.assertIsNotNone(self.user, "User required")

        self.project = Project(
            tenant_id=self.tenant.tenant_id,
            name=f"Metrics Test Project {uuid.uuid4().hex[:8]}",
        )
        self.db.add(self.project)
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        self.db.query(StageMetricSnapshot).filter(StageMetricSnapshot.project_id == self.project.project_id).delete()
        self.db.query(ProjectMetricSnapshot).filter(ProjectMetricSnapshot.project_id == self.project.project_id).delete()
        self.db.query(AuditFinding).filter(AuditFinding.project_id == self.project.project_id).delete()
        self.db.query(AuditRun).filter(AuditRun.project_id == self.project.project_id).delete()
        self.db.query(AuditLog).filter(AuditLog.resource_id == self.project.project_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def test_project_progress_history(self):
        # Insert 2 historical project snapshots
        snap1 = ProjectMetricSnapshot(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            completeness_score=45.0,
            readiness_status="NOT_READY",
            mandatory_requirement_coverage=50.0,
            approval_health=80.0,
            dependency_health=100.0,
            document_health=90.0,
            version_reference_health=100.0,
            conflict_health=100.0,
            open_findings_by_severity={"HIGH": 2, "MEDIUM": 1},
            blockers_count=2,
            snapshot_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        )
        snap2 = ProjectMetricSnapshot(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            completeness_score=95.0,
            readiness_status="READY",
            mandatory_requirement_coverage=100.0,
            approval_health=100.0,
            dependency_health=100.0,
            document_health=100.0,
            version_reference_health=100.0,
            conflict_health=100.0,
            open_findings_by_severity={},
            blockers_count=0,
            snapshot_at=datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc),
        )
        self.db.add_all([snap1, snap2])
        self.db.commit()

        history = get_project_progress_history(self.db, self.project.project_id)
        self.assertEqual(len(history), 2)
        # Verify chronological order
        self.assertEqual(history[0]["completeness_score"], 45.0)
        self.assertEqual(history[1]["completeness_score"], 95.0)
        self.assertEqual(history[1]["readiness_status"], "READY")

    def test_stage_health_history_survives_stage_deletion(self):
        """
        Verify historical retention requirement:
        Historical metric snapshots remain queryable even after the live stage is deleted.
        """
        archived_stage_id = uuid.uuid4()
        stage_snap = StageMetricSnapshot(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            stage_id=archived_stage_id,
            stage_name="Decommissioned Stage X",
            completeness_score=80.0,
            readiness_status="READY",
            mandatory_requirements_total=5,
            mandatory_requirements_satisfied=4,
            mandatory_requirements_missing=1,
            mandatory_requirements_partial=0,
            mandatory_requirements_blocked=0,
            upstream_requirements_applicable=2,
            upstream_requirements_satisfied=2,
            evidence_coverage=80.0,
            document_health=100.0,
            approvals_satisfied=True,
            blockers_count=0,
            snapshot_at=datetime(2026, 2, 1, 10, 0, tzinfo=timezone.utc),
        )
        self.db.add(stage_snap)
        self.db.commit()

        # Query history for the non-existent live stage
        history = get_stage_health_history(self.db, self.project.project_id, stage_id=archived_stage_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["stage_name"], "Decommissioned Stage X")
        self.assertEqual(history[0]["completeness_score"], 80.0)

    def test_project_timeline_synthesis(self):
        # Add AuditRun
        run = AuditRun(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            lifecycle_snapshot={},
            rules_version="1.0.0",
            status="success",
            readiness_status="READY",
            completeness_score=100.0,
            rules_evaluated=7,
            started_at=datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc),
            completed_at=datetime(2026, 3, 1, 10, 1, tzinfo=timezone.utc),
        )
        self.db.add(run)

        # Add AuditLog
        log = AuditLog(
            user_id=self.user.user_id,
            action="stage_gate_approved",
            resource_type="project",
            resource_id=self.project.project_id,
            details={"stage_name": "Design"},
            created_at=datetime(2026, 3, 1, 10, 5, tzinfo=timezone.utc),
        )
        self.db.add(log)
        self.db.commit()

        timeline = get_project_timeline(self.db, self.project.project_id)
        self.assertGreaterEqual(len(timeline), 2)
        event_types = [item["event_type"] for item in timeline]
        self.assertIn("AUDIT_RUN", event_types)
        self.assertIn("USER_ACTION", event_types)


if __name__ == "__main__":
    unittest.main()
