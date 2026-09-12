// Central client for the DocFlow AI backend (FastAPI on :8000).
//
// Phase 6: the teammate's full frontend is kept as-is. This client wires the
// calls it makes to OUR backend:
//   - REAL   : auth (login/signup/me/google), /workspace, /documents (list +
//              upload), the approval workflow (submit/approve/reject), and a
//              real-data mapping for projectsApi.list / .documents /
//              .pendingApprovals so his Projects + Project Workspace screens
//              show live data in his own layout.
//   - STUBBED: everything with no backend yet (projectsApi.create/activity,
//              document versions/delete, ragApi, chatApi, agentsApi, studioApi,
//              notesApi, adminApi, notificationsApi, password reset,
//              super-admin). Every stub rejects with a clear
//              "not connected yet" ApiError so the calling screen shows a
//              message in that section instead of failing silently.

export const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000';

const TOKEN_KEY = 'docflow_token';

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}
export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

class ApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

export const NOT_CONNECTED_MESSAGE =
  "This feature isn't connected to the backend yet.";

function notConnected(feature) {
  return Promise.reject(
    new ApiError(
      `${NOT_CONNECTED_MESSAGE}${feature ? ` (${feature})` : ''}`,
      501,
      { not_connected: true, feature },
    ),
  );
}

async function request(path, { method = 'GET', body, auth = true, credentials } = {}) {
  const headers = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (auth) {
    const token = getToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
  }

  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers,
    credentials,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  let payload = null;
  const text = await response.text();
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = text; }
  }
  if (!response.ok) {
    let detail = response.statusText || 'Request failed';
    if (payload && typeof payload.detail === 'string') detail = payload.detail;
    else if (payload && Array.isArray(payload.detail) && payload.detail[0]?.msg) {
      detail = payload.detail[0].msg;
    }
    throw new ApiError(detail, response.status, payload);
  }
  return payload;
}

const SENSITIVITY_TO_INT = { public: 0, internal: 1, confidential: 2 };

// ---------- Auth ----------
export const authApi = {
  login: (email, password) =>
    request('/auth/login', { method: 'POST', body: { email, password }, auth: false }),
  // his LoginPage calls signup(formObject); AuthContext narrows it to email+password
  signup: (data) =>
    request('/auth/signup', {
      method: 'POST', auth: false,
      body: { email: data.email, password: data.password },
    }),
  me: () => request('/auth/me'),
  googleAuthorize: () =>
    request('/auth/google/authorize', { auth: false, credentials: 'include' }),

  // no backend for these:
  forgotPassword: () => notConnected('password reset'),
  resetPassword: () => notConnected('password reset'),
  changePassword: () => notConnected('change password'),
  superAdmin: () => notConnected('super-admin login'),
  demo: () => notConnected('demo login'),
};

// ---------- Workspace helper (project/team/stage names + ids) ----------
export const workspaceApi = {
  get: () => request('/workspace'),
};

let _workspaceCache = null;
async function workspace() {
  if (!_workspaceCache) _workspaceCache = await workspaceApi.get();
  return _workspaceCache;
}
export function clearWorkspaceCache() { _workspaceCache = null; }

// Map our GET /documents rows into the shape his components expect.
function mapDocs(rows, project) {
  const stageName = (id) => project?.stages.find((s) => s.stage_id === id)?.name || 'Unspecified';
  const teamName = (id) => project?.teams?.find((t) => t.team_id === id)?.name || null;
  return rows.map((d) => ({
    document_id: d.document_id,
    filename: d.original_filename,
    doc_type: (d.original_filename || '').replace(/\.md$/i, '') || 'Document',
    stage: stageName(d.stage_id),
    stage_id: d.stage_id,
    sensitivity_level: SENSITIVITY_TO_INT[d.sensitivity_level] ?? 1,
    workflow_state: d.workflow_state,
    uploaded_as_team_id: d.uploaded_as_team_id,
    team_name: teamName(d.uploaded_as_team_id),
    project_id: project?.project_id || null,
    created_at: null,
    members: undefined,
  }));
}

// ---------- Projects (his shape, real backend) ----------
export const projectsApi = {
  // GET /projects returns his ProjectSummary shape directly.
  list: () => request('/projects'),
  // his ProjectsPage sends { project_id (slug), project_name, description };
  // our backend takes { name, description } and mints its own UUID.
  create: async (data) => {
    const created = await request('/projects', {
      method: 'POST',
      body: { name: data.project_name || data.name, description: data.description || null },
    });
    clearWorkspaceCache();
    return created;
  },
  documents: async (projectId) => {
    const ws = await workspace();
    const project = ws.projects.find((p) => p.project_id === projectId);
    const rows = await request(`/documents?project_id=${encodeURIComponent(projectId)}`);
    return mapDocs(rows, project);
  },
  pendingApprovals: async (projectId) => {
    const docs = await projectsApi.documents(projectId);
    return docs.filter((d) => d.workflow_state === 'pending_review');
  },
};

// ---------- Project Activity (real backend) ----------
// Finalized design: project cards -> single team -> flat, sensitivity-filtered
// activity feed for that team. See app/services/activity.py.
export const activityApi = {
  // -> [{ project_id, project_name, admin_here, teams: [{ team_id, name }] }]
  projects: () => request('/activity/projects'),
  // -> [{ log_id, actor_name, action, filename, stage, sensitivity_level,
  //       status, current_state, rejection_reason, details, timestamp }]
  feed: (projectId, teamId) =>
    request(`/activity?project_id=${encodeURIComponent(projectId)}&team_id=${encodeURIComponent(teamId)}`),
};

// ---------- Stages (real backend) ----------
// Create / edit / soft-delete project stages. Mutations clear the workspace
// cache so ProjectWorkspace picks up the new stage list on reload.
export const stagesApi = {
  list: (projectId) => request(`/projects/${encodeURIComponent(projectId)}/stages`),
  create: async (projectId, body) => {
    const r = await request(`/projects/${encodeURIComponent(projectId)}/stages`, { method: 'POST', body });
    clearWorkspaceCache();
    return r;
  },
  // body: { name?, order_index?, requires_approval? }
  update: async (projectId, stageId, body) => {
    const r = await request(
      `/projects/${encodeURIComponent(projectId)}/stages/${encodeURIComponent(stageId)}`,
      { method: 'PATCH', body },
    );
    clearWorkspaceCache();
    return r;
  },
  remove: async (projectId, stageId, reassignTo) => {
    const q = reassignTo ? `?reassign_to=${encodeURIComponent(reassignTo)}` : '';
    const r = await request(
      `/projects/${encodeURIComponent(projectId)}/stages/${encodeURIComponent(stageId)}${q}`,
      { method: 'DELETE' },
    );
    clearWorkspaceCache();
    return r;
  },
  // Replace this stage's full set of outbound references (stage_ids, same project).
  setReferences: async (projectId, stageId, references) => {
    const r = await request(
      `/projects/${encodeURIComponent(projectId)}/stages/${encodeURIComponent(stageId)}/references`,
      { method: 'PUT', body: { references } },
    );
    clearWorkspaceCache();
    return r;
  },
  // Replace the full set of teams granted access to this stage (team_ids, same project).
  setTeamAccess: async (projectId, stageId, teamIds) => {
    const r = await request(
      `/projects/${encodeURIComponent(projectId)}/stages/${encodeURIComponent(stageId)}/team-access`,
      { method: 'PUT', body: { team_ids: teamIds } },
    );
    clearWorkspaceCache();
    return r;
  },
};

// ---------- Teams (real backend) ----------
// Create the teams themselves within a project (NOT user->team assignment —
// that's adminApi.assignRoles). Creating clears the workspace cache so the
// "Assign Roles" team dropdown (fed by /workspace) picks up the new team.
export const teamsApi = {
  // -> [{ team_id, project_id, name, member_count }]
  list: (projectId) => request(`/projects/${encodeURIComponent(projectId)}/teams`),
  create: async (projectId, name) => {
    const r = await request(`/projects/${encodeURIComponent(projectId)}/teams`, {
      method: 'POST', body: { name },
    });
    clearWorkspaceCache();
    return r;
  },
};

// ---------- Documents ----------
export const documentsApi = {
  list: (projectId) => projectsApi.documents(projectId),
  listAll: () => notConnected('list all documents'),
  // body: { document_type, stage_id, content, team_id, sensitivity_level }
  upload: (data) => request('/documents/upload', { method: 'POST', body: data }),
  // Real file upload (PDF/DOCX/TXT/MD) -> auto-scan -> seeds a review chat
  // session. multipart/form-data, so it bypasses request()'s JSON body.
  // -> { document_id, version_id, stage_id, stage_name, sensitivity_level,
  //      uploaded_as_team_id, status, session_id, scan, scan_error,
  //      scan_skipped, reformed_content, injection_flagged,
  //      injection_findings, reply }
  uploadFile: async ({ file, stageId, teamId, sensitivityLevel = 'internal' }) => {
    const form = new FormData();
    form.append('file', file);
    form.append('stage_id', stageId);
    form.append('team_id', teamId);
    form.append('sensitivity_level', sensitivityLevel);

    const token = getToken();
    const res = await fetch(`${API_BASE}/documents/upload-file`, {
      method: 'POST',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: form,
    });
    const text = await res.text();
    let payload = null;
    if (text) { try { payload = JSON.parse(text); } catch { payload = text; } }
    if (!res.ok) {
      let detail = res.statusText || 'Upload failed';
      if (payload && typeof payload.detail === 'string') detail = payload.detail;
      throw new ApiError(detail, res.status, payload);
    }
    return payload;
  },
  uploadBatch: () => notConnected('batch upload'),
  versions: () => notConnected('document versions'),
  uploadVersion: () => notConnected('upload new version'),
  remove: () => notConnected('delete document'),
  openUrl: () => '#', // no download endpoint yet

  submit: (documentId) =>
    request(`/documents/${encodeURIComponent(documentId)}/submit`, { method: 'POST' }),
  approve: (documentId) =>
    request(`/documents/${encodeURIComponent(documentId)}/approve`, { method: 'POST' }),
  reject: (documentId, reason) =>
    request(`/documents/${encodeURIComponent(documentId)}/reject`, {
      method: 'POST', body: { reason },
    }),
  status: (documentId) => request(`/documents/${encodeURIComponent(documentId)}/status`),
};

// ---------- Document review chat (upload + scan + revise + index) ----------
// One turn of the post-upload review conversation — reuses the SAME
// drafting_agent as chat-drafting, but finalize writes a new DocumentVersion
// on the uploaded document instead of a downloadable file.
export const documentReviewApi = {
  message: (documentId, sessionId, message) =>
    request('/documents/review/message', {
      method: 'POST',
      body: { document_id: documentId, session_id: sessionId, message: message || '' },
    }),
};

// ---------- Not connected yet ----------
const stub = (feature) => new Proxy({}, {
  get: () => () => notConnected(feature),
});

function stubMethods(feature, names) {
  return Object.fromEntries(names.map((n) => [n, () => notConnected(feature)]));
}

// ---------- Admin: real for the directory / invite / audit surface ----------
export const adminApi = {
  // -> [{ user_id, username, full_name, team_name, is_org_admin, roles:[{project_id, role}] }]
  listUsers: () => request('/admin/users'),
  // his form collects full_name/team_name too; our backend only needs the email.
  createUser: (data) => request('/admin/users', {
    method: 'POST',
    body: { email: data.email, full_name: data.full_name || null },
  }),
  // his "Promote to org admin" button (role is always 'admin' from his UI)
  assignAccess: (data) => request('/admin/access', { method: 'POST', body: { email: data.email } }),
  // invite / assign a TeamRole on a specific team. Accepts his
  // { email, project_id, role, team_name } — backend resolves team_name -> team_id
  // and maps role 'member' -> contributor (rejects 'admin' with a clear message).
  assignProjectAccess: (data) => request('/admin/project-access', {
    method: 'POST',
    body: {
      email: data.email,
      role: data.role,
      project_id: data.project_id,
      team_name: data.team_name,
    },
  }),
  // Assign Roles modal, Section B ("Add new access"). One of:
  //   { email, mode: 'team_member', team_assignments: [{ team_id, role }] }
  //   { email, mode: 'project_admin', project_ids: [...], all_projects: bool }
  //   { email, mode: 'org_admin' }
  // See app/routers/admin.py::assign_roles.
  assignRoles: (data) => request('/admin/assign-roles', { method: 'POST', body: data }),

  // Assign Roles modal, Section A ("Current access").
  updateTeamRole: (membershipId, role) =>
    request(`/admin/team-membership/${encodeURIComponent(membershipId)}`, {
      method: 'PATCH', body: { role },
    }),
  removeTeamMembership: (membershipId) =>
    request(`/admin/team-membership/${encodeURIComponent(membershipId)}`, { method: 'DELETE' }),
  removeProjectAdmin: (scopeId) =>
    request(`/admin/project-admin/${encodeURIComponent(scopeId)}`, { method: 'DELETE' }),
  revokeOrgAdmin: (userId) =>
    request('/admin/revoke-org-admin', { method: 'POST', body: { user_id: userId } }),

  auditLog: (limit = 100) => request(`/admin/audit-log?limit=${encodeURIComponent(limit)}`),

  // no backend yet:
  ...stubMethods('Admin', ['removeUser', 'assignProjectAccessRemove', 'setRole']),
};

// ---------- Confidential-access requests (real backend) ----------
export const accessRequestsApi = {
  // the caller's own requests, newest first — { status, active, team_name, ... }
  mine: () => request('/access-requests/mine'),
  // create a pending request for one team
  create: (teamId) => request('/access-requests', { method: 'POST', body: { team_id: teamId } }),
  // requests the caller may decide (team_lead on that team / project_admin / org_admin)
  pending: () => request('/access-requests/pending'),
  approve: (id) => request(`/access-requests/${encodeURIComponent(id)}/approve`, { method: 'POST' }),
  deny: (id) => request(`/access-requests/${encodeURIComponent(id)}/deny`, { method: 'POST' }),
};

// ---------- RAG Agent: REAL (Phase C) — the Search tab ----------
// POST /agents/rag/message runs one grounded-retrieval turn. Conversation
// ownership is per (user, project); pass session_id back to continue, or omit
// it (null) to start fresh.
export const ragApi = {
  // -> { reply, tools_called: [...], session_id }
  message: (projectId, sessionId, message) =>
    request('/agents/rag/message', {
      method: 'POST',
      body: { project_id: projectId, session_id: sessionId || null, message },
    }),
};

// ---------- Query Agent: REAL — the Query tab (read-only metadata Q&A) ----------
// POST /agents/query/message runs one read-only metadata lookup turn. Same
// per-(user, project) conversation ownership as ragApi; pass session_id back to
// continue, or omit it (null) to start fresh.
export const queryApi = {
  // -> { reply, tools_called: [...], session_id }
  message: (projectId, sessionId, message) =>
    request('/agents/query/message', {
      method: 'POST',
      body: { project_id: projectId, session_id: sessionId || null, message },
    }),
};

// ---------- Chat history: REAL — the Search tab's history sidebar / reload ----------
// Read-only view of chat_sessions / chat_messages (written server-side by the
// agent runners). Starting a new conversation is just "send with no session_id".
export const chatApi = {
  // -> [{ session_id, title, started_at }]
  sessions: (projectId, mode) =>
    request(
      `/chat/sessions?project_id=${encodeURIComponent(projectId)}` +
        (mode ? `&mode=${encodeURIComponent(mode)}` : ''),
    ),
  // -> [{ message_id, role, content, created_at, sources }]
  messages: (sessionId) =>
    request(`/chat/sessions/${encodeURIComponent(sessionId)}/messages`),
  removeSession: (sessionId) =>
    request(`/chat/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' }),
};

// ---------- Agents: Drafting + standalone Scan flows are REAL (decoupled from persistence) ----------
// POST /agents/draft/message runs one drafting turn; the finalized draft is a
// standalone local file fetched via GET /agents/draft/download/{filename}.
export const agentsApi = {
  // -> { reply, drafted, finalized, scan, scan_error, final_content,
  //      download_url, filename }
  draftMessage: (sessionId, message, projectId) =>
    request('/agents/draft/message', {
      method: 'POST',
      body: { session_id: sessionId, message: message || '', project_id: projectId || null },
    }),
  // Standalone Structure Scanner chat (DEFERRED_ITEMS.md #4). Auth only, no
  // project/persistence. -> { reply, tools_called: [...] }
  scanMessage: (sessionId, message) =>
    request('/agents/scan/message', {
      method: 'POST',
      body: { session_id: sessionId, message },
    }),
  // Auth'd blob fetch — a plain <a href download> can't send the Bearer token.
  draftDownload: async (downloadUrl) => {
    const token = getToken();
    const res = await fetch(`${API_BASE}${downloadUrl}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!res.ok) throw new ApiError('Could not download the draft', res.status);
    return res.blob();
  },
  // Upload a finalized draft directly to a project/stage/team without re-downloading or re-scanning
  uploadDraftToProject: ({ draftId, projectId, stageId, teamId, sensitivityLevel = 'internal' }) =>
    request('/agents/draft/upload-to-project', {
      method: 'POST',
      body: {
        draft_id: draftId,
        project_id: projectId,
        stage_id: stageId,
        team_id: teamId,
        sensitivity_level: sensitivityLevel,
      },
    }),
  // still no backend:
  analyzeGaps: () => notConnected('gap analysis'),
  followups: () => notConnected('follow-up questions'),
};

export const studioApi = stub('Studio');
export const notesApi = stub('Notes');
export const notificationsApi = stub('Notifications');

export { ApiError };
