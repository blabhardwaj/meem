"""enable rls tenant isolation and supporting fk indexes

Revision ID: a1b2c3d4e5f6
Revises: f1a2b3c4d5e6
Create Date: 2026-09-14 04:35:00.000000
"""
from typing import Sequence, Union
from alembic import op

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
    -- 0. Supporting Indexes for Foreign Key lookups in RLS subqueries
    CREATE INDEX IF NOT EXISTS idx_doc_versions_document_id ON document_versions (document_id);
    CREATE INDEX IF NOT EXISTS idx_stages_project_id ON stages (project_id);
    CREATE INDEX IF NOT EXISTS idx_teams_project_id ON teams (project_id);
    CREATE INDEX IF NOT EXISTS idx_chat_sessions_project_id ON chat_sessions (project_id);
    CREATE INDEX IF NOT EXISTS idx_chat_messages_session_id ON chat_messages (session_id);
    CREATE INDEX IF NOT EXISTS idx_document_scans_version_id ON document_scans (version_id);
    CREATE INDEX IF NOT EXISTS idx_stage_references_stage_id ON stage_references (stage_id);
    CREATE INDEX IF NOT EXISTS idx_project_admins_project_id ON project_admins (project_id);
    CREATE INDEX IF NOT EXISTS idx_utm_project_id ON user_team_memberships (project_id);
    CREATE INDEX IF NOT EXISTS idx_tsa_team_id ON team_stage_access (team_id);
    CREATE INDEX IF NOT EXISTS idx_access_requests_user_id ON access_requests (user_id);

    -- 1. Direct Tables (0 Hops): Direct tenant_id
    ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
    ALTER TABLE documents FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON documents;
    CREATE POLICY tenant_isolation ON documents
        USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);

    ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
    ALTER TABLE projects FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON projects;
    CREATE POLICY tenant_isolation ON projects
        USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);

    ALTER TABLE users ENABLE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON users;
    CREATE POLICY tenant_isolation ON users
        USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);

    ALTER TABLE knowledge.nodes ENABLE ROW LEVEL SECURITY;
    ALTER TABLE knowledge.nodes FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON knowledge.nodes;
    CREATE POLICY tenant_isolation ON knowledge.nodes
        USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);

    ALTER TABLE knowledge.edges ENABLE ROW LEVEL SECURITY;
    ALTER TABLE knowledge.edges FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON knowledge.edges;
    CREATE POLICY tenant_isolation ON knowledge.edges
        USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);

    ALTER TABLE knowledge.claims ENABLE ROW LEVEL SECURITY;
    ALTER TABLE knowledge.claims FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON knowledge.claims;
    CREATE POLICY tenant_isolation ON knowledge.claims
        USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);

    -- 2. 1-Hop Dependent Tables
    ALTER TABLE document_versions ENABLE ROW LEVEL SECURITY;
    ALTER TABLE document_versions FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON document_versions;
    CREATE POLICY tenant_isolation ON document_versions
        USING (EXISTS (
            SELECT 1 FROM documents d
            WHERE d.document_id = document_versions.document_id
              AND d.tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        ));

    ALTER TABLE stages ENABLE ROW LEVEL SECURITY;
    ALTER TABLE stages FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON stages;
    CREATE POLICY tenant_isolation ON stages
        USING (EXISTS (
            SELECT 1 FROM projects p
            WHERE p.project_id = stages.project_id
              AND p.tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        ));

    ALTER TABLE teams ENABLE ROW LEVEL SECURITY;
    ALTER TABLE teams FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON teams;
    CREATE POLICY tenant_isolation ON teams
        USING (EXISTS (
            SELECT 1 FROM projects p
            WHERE p.project_id = teams.project_id
              AND p.tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        ));

    ALTER TABLE chat_sessions ENABLE ROW LEVEL SECURITY;
    ALTER TABLE chat_sessions FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON chat_sessions;
    CREATE POLICY tenant_isolation ON chat_sessions
        USING (EXISTS (
            SELECT 1 FROM projects p
            WHERE p.project_id = chat_sessions.project_id
              AND p.tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        ));

    ALTER TABLE workflow_state ENABLE ROW LEVEL SECURITY;
    ALTER TABLE workflow_state FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON workflow_state;
    CREATE POLICY tenant_isolation ON workflow_state
        USING (EXISTS (
            SELECT 1 FROM documents d
            WHERE d.document_id = workflow_state.document_id
              AND d.tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        ));

    ALTER TABLE document_team_visibility ENABLE ROW LEVEL SECURITY;
    ALTER TABLE document_team_visibility FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON document_team_visibility;
    CREATE POLICY tenant_isolation ON document_team_visibility
        USING (EXISTS (
            SELECT 1 FROM documents d
            WHERE d.document_id = document_team_visibility.document_id
              AND d.tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        ));

    ALTER TABLE user_team_memberships ENABLE ROW LEVEL SECURITY;
    ALTER TABLE user_team_memberships FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON user_team_memberships;
    CREATE POLICY tenant_isolation ON user_team_memberships
        USING (EXISTS (
            SELECT 1 FROM projects p
            WHERE p.project_id = user_team_memberships.project_id
              AND p.tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        ));

    ALTER TABLE project_admins ENABLE ROW LEVEL SECURITY;
    ALTER TABLE project_admins FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON project_admins;
    CREATE POLICY tenant_isolation ON project_admins
        USING (EXISTS (
            SELECT 1 FROM projects p
            WHERE p.project_id = project_admins.project_id
              AND p.tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        ));

    ALTER TABLE access_requests ENABLE ROW LEVEL SECURITY;
    ALTER TABLE access_requests FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON access_requests;
    CREATE POLICY tenant_isolation ON access_requests
        USING (EXISTS (
            SELECT 1 FROM users u
            WHERE u.user_id = access_requests.user_id
              AND u.tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        ));

    -- 3. 2-Hop Dependent Tables
    ALTER TABLE team_stage_access ENABLE ROW LEVEL SECURITY;
    ALTER TABLE team_stage_access FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON team_stage_access;
    CREATE POLICY tenant_isolation ON team_stage_access
        USING (EXISTS (
            SELECT 1 FROM teams t
            JOIN projects p ON t.project_id = p.project_id
            WHERE t.team_id = team_stage_access.team_id
              AND p.tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        ));

    ALTER TABLE stage_references ENABLE ROW LEVEL SECURITY;
    ALTER TABLE stage_references FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON stage_references;
    CREATE POLICY tenant_isolation ON stage_references
        USING (EXISTS (
            SELECT 1 FROM stages s
            JOIN projects p ON s.project_id = p.project_id
            WHERE s.stage_id = stage_references.stage_id
              AND p.tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        ));

    ALTER TABLE required_documents ENABLE ROW LEVEL SECURITY;
    ALTER TABLE required_documents FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON required_documents;
    CREATE POLICY tenant_isolation ON required_documents
        USING (EXISTS (
            SELECT 1 FROM stages s
            JOIN projects p ON s.project_id = p.project_id
            WHERE s.stage_id = required_documents.stage_id
              AND p.tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        ));

    ALTER TABLE chat_messages ENABLE ROW LEVEL SECURITY;
    ALTER TABLE chat_messages FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON chat_messages;
    CREATE POLICY tenant_isolation ON chat_messages
        USING (EXISTS (
            SELECT 1 FROM chat_sessions cs
            JOIN projects p ON cs.project_id = p.project_id
            WHERE cs.session_id = chat_messages.session_id
              AND p.tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        ));

    ALTER TABLE document_scans ENABLE ROW LEVEL SECURITY;
    ALTER TABLE document_scans FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON document_scans;
    CREATE POLICY tenant_isolation ON document_scans
        USING (EXISTS (
            SELECT 1 FROM document_versions dv
            JOIN documents d ON dv.document_id = d.document_id
            WHERE dv.version_id = document_scans.version_id
              AND d.tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        ));
    """)


def downgrade() -> None:
    op.execute("""
    DROP POLICY IF EXISTS tenant_isolation ON document_scans;
    ALTER TABLE document_scans NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE document_scans DISABLE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS tenant_isolation ON chat_messages;
    ALTER TABLE chat_messages NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE chat_messages DISABLE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS tenant_isolation ON required_documents;
    ALTER TABLE required_documents NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE required_documents DISABLE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS tenant_isolation ON stage_references;
    ALTER TABLE stage_references NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE stage_references DISABLE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS tenant_isolation ON team_stage_access;
    ALTER TABLE team_stage_access NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE team_stage_access DISABLE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS tenant_isolation ON access_requests;
    ALTER TABLE access_requests NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE access_requests DISABLE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS tenant_isolation ON project_admins;
    ALTER TABLE project_admins NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE project_admins DISABLE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS tenant_isolation ON user_team_memberships;
    ALTER TABLE user_team_memberships NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE user_team_memberships DISABLE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS tenant_isolation ON document_team_visibility;
    ALTER TABLE document_team_visibility NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE document_team_visibility DISABLE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS tenant_isolation ON workflow_state;
    ALTER TABLE workflow_state NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE workflow_state DISABLE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS tenant_isolation ON chat_sessions;
    ALTER TABLE chat_sessions NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE chat_sessions DISABLE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS tenant_isolation ON teams;
    ALTER TABLE teams NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE teams DISABLE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS tenant_isolation ON stages;
    ALTER TABLE stages NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE stages DISABLE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS tenant_isolation ON document_versions;
    ALTER TABLE document_versions NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE document_versions DISABLE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS tenant_isolation ON knowledge.claims;
    ALTER TABLE knowledge.claims NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE knowledge.claims DISABLE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS tenant_isolation ON knowledge.edges;
    ALTER TABLE knowledge.edges NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE knowledge.edges DISABLE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS tenant_isolation ON knowledge.nodes;
    ALTER TABLE knowledge.nodes NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE knowledge.nodes DISABLE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS tenant_isolation ON users;
    ALTER TABLE users NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE users DISABLE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS tenant_isolation ON projects;
    ALTER TABLE projects NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE projects DISABLE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS tenant_isolation ON documents;
    ALTER TABLE documents NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE documents DISABLE ROW LEVEL SECURITY;

    DROP INDEX IF EXISTS idx_access_requests_user_id;
    DROP INDEX IF EXISTS idx_tsa_team_id;
    DROP INDEX IF EXISTS idx_utm_project_id;
    DROP INDEX IF EXISTS idx_project_admins_project_id;
    DROP INDEX IF EXISTS idx_stage_references_stage_id;
    DROP INDEX IF EXISTS idx_document_scans_version_id;
    DROP INDEX IF EXISTS idx_chat_messages_session_id;
    DROP INDEX IF EXISTS idx_chat_sessions_project_id;
    DROP INDEX IF EXISTS idx_teams_project_id;
    DROP INDEX IF EXISTS idx_stages_project_id;
    DROP INDEX IF EXISTS idx_doc_versions_document_id;
    """)
