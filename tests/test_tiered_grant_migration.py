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
