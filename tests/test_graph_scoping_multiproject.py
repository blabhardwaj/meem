import unittest
import uuid
from app.database import SessionLocal
from app.models.graph import Edge, Node
from app.models.project import Project
from app.models.stage import Stage
from app.models.team import Team, ProjectAdmin, UserTeamMembership, TeamRole
from app.models.tenant import Tenant
from app.models.user import User
from app.services.graph.sync import sync_project_graph
from app.tools.graph_tools import query_entity_neighborhood


class TestGraphScopingMultiProject(unittest.TestCase):
    def setUp(self):
        self.db = SessionLocal()
        # Tenant 1
        self.tenant1 = self.db.query(Tenant).first()
        self.assertIsNotNone(self.tenant1, "Tenant 1 required")
        self.user1 = self.db.query(User).filter(User.tenant_id == self.tenant1.tenant_id).first()
        self.assertIsNotNone(self.user1, "User 1 required")

        # Project A in Tenant 1
        self.project_a = Project(
            tenant_id=self.tenant1.tenant_id,
            name=f"MultiProject A {uuid.uuid4().hex[:8]}",
        )
        # Project B in Tenant 1
        self.project_b = Project(
            tenant_id=self.tenant1.tenant_id,
            name=f"MultiProject B {uuid.uuid4().hex[:8]}",
        )
        self.db.add_all([self.project_a, self.project_b])
        self.db.commit()

        # Teams in Project A & B
        self.team_a = Team(project_id=self.project_a.project_id, name="Team Project A")
        self.team_b = Team(project_id=self.project_b.project_id, name="Team Project B")
        self.db.add_all([self.team_a, self.team_b])
        self.db.commit()

        # User is admin of Project A and team member of Project B
        self.admin_a = ProjectAdmin(user_id=self.user1.user_id, project_id=self.project_a.project_id)
        self.membership_b = UserTeamMembership(
            user_id=self.user1.user_id,
            team_id=self.team_b.team_id,
            project_id=self.project_b.project_id,
            role=TeamRole.contributor,
        )
        self.db.add_all([self.admin_a, self.membership_b])
        self.db.commit()

        # Stages in Project A and B
        self.stage_a = Stage(project_id=self.project_a.project_id, name="Stage A1", order_index=1, requires_approval=False)
        self.stage_b = Stage(project_id=self.project_b.project_id, name="Stage B1", order_index=1, requires_approval=False)
        self.db.add_all([self.stage_a, self.stage_b])
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        # Clean graph
        for pid in [self.project_a.project_id, self.project_b.project_id]:
            self.db.query(Edge).filter(Edge.project_id == pid).delete()
            self.db.query(Node).filter(Node.project_id == pid).delete()

        self.db.query(ProjectAdmin).filter(ProjectAdmin.project_id.in_([self.project_a.project_id, self.project_b.project_id])).delete()
        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id.in_([self.project_a.project_id, self.project_b.project_id])).delete()
        self.db.query(Stage).filter(Stage.project_id.in_([self.project_a.project_id, self.project_b.project_id])).delete()
        self.db.query(Team).filter(Team.project_id.in_([self.project_a.project_id, self.project_b.project_id])).delete()
        self.db.query(Project).filter(Project.project_id.in_([self.project_a.project_id, self.project_b.project_id])).delete()
        self.db.commit()
        self.db.close()

    def test_multiproject_user_scoping_and_isolation(self):
        """
        Tests multi-project user and team invariants:
        1. User in one project
        2. Same user in two projects
        3. Syncing Project A then Project B does NOT overwrite Project A's user node
        4. Querying Project A after Project B was synced preserves Project A context
        5. Project-specific neighborhood does not expose user's unrelated project context
        6. Two tenants can have equivalent source IDs without collision
        """
        # 1. Sync Project A
        res_a = sync_project_graph(self.db, self.project_a.project_id)
        self.assertGreater(res_a.nodes_synced, 0)

        # Verify User node exists in Project A
        node_a = (
            self.db.query(Node)
            .filter(
                Node.tenant_id == self.tenant1.tenant_id,
                Node.project_id == self.project_a.project_id,
                Node.source_table == "users",
                Node.source_id == self.user1.user_id,
            )
            .first()
        )
        self.assertIsNotNone(node_a)
        self.assertEqual(node_a.project_id, self.project_a.project_id)

        # 2. Sync Project B
        res_b = sync_project_graph(self.db, self.project_b.project_id)
        self.assertGreater(res_b.nodes_synced, 0)

        # Verify User node exists in Project B
        node_b = (
            self.db.query(Node)
            .filter(
                Node.tenant_id == self.tenant1.tenant_id,
                Node.project_id == self.project_b.project_id,
                Node.source_table == "users",
                Node.source_id == self.user1.user_id,
            )
            .first()
        )
        self.assertIsNotNone(node_b)
        self.assertEqual(node_b.project_id, self.project_b.project_id)

        # 3. Verify Project A's node STILL exists and was NOT overwritten
        node_a_refreshed = (
            self.db.query(Node)
            .filter(
                Node.tenant_id == self.tenant1.tenant_id,
                Node.project_id == self.project_a.project_id,
                Node.source_table == "users",
                Node.source_id == self.user1.user_id,
            )
            .first()
        )
        self.assertIsNotNone(node_a_refreshed)
        self.assertEqual(node_a_refreshed.node_id, node_a.node_id)
        self.assertNotEqual(node_a.node_id, node_b.node_id)

        # 4. Invariant B: Project-specific graph query never inherits another project's context
        # Check Project A edges for User A
        edges_a = (
            self.db.query(Edge)
            .filter(
                Edge.project_id == self.project_a.project_id,
                Edge.source_node_id == node_a.node_id,
            )
            .all()
        )
        edge_types_a = [e.edge_type for e in edges_a]
        self.assertIn("MANAGES_PROJECT", edge_types_a)
        self.assertNotIn("MEMBER_OF", edge_types_a)

        # Check Project B edges for User B
        edges_b = (
            self.db.query(Edge)
            .filter(
                Edge.project_id == self.project_b.project_id,
                Edge.source_node_id == node_b.node_id,
            )
            .all()
        )
        edge_types_b = [e.edge_type for e in edges_b]
        self.assertIn("MEMBER_OF", edge_types_b)
        self.assertNotIn("MANAGES_PROJECT", edge_types_b)

        # 5. Project-specific neighborhood does not expose User's unrelated project context
        import json
        nh_a_str = query_entity_neighborhood(
            project_id=str(self.project_a.project_id),
            entity_type="user",
            entity_id=str(self.user1.user_id),
            user_id=str(self.user1.user_id),
        )
        nh_a = json.loads(nh_a_str)
        self.assertNotIn("error", nh_a)
        connected_labels = [c["related_entity"] for c in nh_a.get("connections", [])]
        # Connected to Project A, NOT Team Project B
        self.assertIn(self.project_a.name, connected_labels)
        self.assertNotIn("Team Project B", connected_labels)

        # 6. Two tenants can have equivalent source IDs without collision
        tenant2 = Tenant(name=f"Tenant 2 {uuid.uuid4().hex[:8]}")
        self.db.add(tenant2)
        self.db.commit()

        project2 = Project(tenant_id=tenant2.tenant_id, name="Tenant 2 Project")
        self.db.add(project2)
        self.db.commit()

        shared_source_id = uuid.uuid4()
        # Create node in Tenant 1
        node_t1 = Node(
            tenant_id=self.tenant1.tenant_id,
            project_id=self.project_a.project_id,
            entity_type="user",
            source_table="users",
            source_id=shared_source_id,
            label="User Shared ID",
        )
        # Create node in Tenant 2 with identical source_id
        node_t2 = Node(
            tenant_id=tenant2.tenant_id,
            project_id=project2.project_id,
            entity_type="user",
            source_table="users",
            source_id=shared_source_id,
            label="User Shared ID in Tenant 2",
        )
        self.db.add_all([node_t1, node_t2])
        self.db.commit()

        self.assertNotEqual(node_t1.node_id, node_t2.node_id)
        self.assertEqual(node_t1.source_id, node_t2.source_id)

        # Clean up tenant 2
        self.db.query(Node).filter(Node.project_id == project2.project_id).delete()
        self.db.query(Project).filter(Project.project_id == project2.project_id).delete()
        self.db.query(Tenant).filter(Tenant.tenant_id == tenant2.tenant_id).delete()
        self.db.commit()


if __name__ == "__main__":
    unittest.main()
