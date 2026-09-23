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
//              notesApi, adminApi, password reset, super-admin). Every stub
//              rejects with a clear "not connected yet" ApiError so the
//              calling screen shows a message in that section instead of
//              failing silently. notificationsApi is REAL (see below).

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
    // payload.detail can be a plain string (most HTTPExceptions), an array of
    // Pydantic validation errors (422 — {type,loc,msg,input,ctx} objects), or
    // an app-defined object (e.g. workflow.py's {message, scan}). Whatever
    // its shape, `detail` here (and therefore err.message) must always end
    // up a plain string — an uncaught object/array reaching a React child
    // crashes the whole page (this is exactly what broke accept-invite:
    // a short password produced a 422 array, rendered raw).
    let detail = response.statusText || 'Request failed';
    if (payload && typeof payload.detail === 'string') detail = payload.detail;
    else if (payload && Array.isArray(payload.detail) && typeof payload.detail[0]?.msg === 'string') {
      detail = payload.detail[0].msg;
    } else if (payload && payload.detail && typeof payload.detail.message === 'string') {
      detail = payload.detail.message;
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
  // creates a brand-new Tenant + its first user (org_admin)
  registerOrg: (data) =>
    request('/auth/register-org', {
      method: 'POST', auth: false,
      body: {
        org_name: data.org_name,
        email: data.email,
        password: data.password,
        full_name: data.full_name || null,
      },
    }),
  // redeem an admin-issued invite token
  acceptInvite: (data) =>
    request('/auth/accept-invite', {
      method: 'POST', auth: false,
      body: { token: data.token, password: data.password, full_name: data.full_name || null },
    }),
  me: () => request('/auth/me'),
  googleAuthorize: () =>
    request('/auth/google/authorize', { auth: false, credentials: 'include' }),
  exchangeOAuth: (code) =>
    request('/auth/exchange', { method: 'POST', body: { code }, auth: false }),
  // change own password -> bumps token_version, invalidating every other session
  changePassword: (currentPassword, newPassword) =>
    request('/auth/password', {
      method: 'PUT',
      body: { current_password: currentPassword, new_password: newPassword },
    }),
  // set a FIRST password for an account that only ever signed in via Google
  // (no current password to verify) -- 409s if one already exists.
  setPassword: (newPassword) =>
    request('/auth/set-password', { method: 'POST', body: { new_password: newPassword } }),
  // update own display name -> { full_name }
  updateProfile: (fullName) =>
    request('/auth/profile', { method: 'PUT', body: { full_name: fullName } }),
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
    uploaded_by: d.uploaded_by,
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
  // includeDrafts is only honoured server-side for org_admin/project_admin —
  // a regular contributor always gets just their own drafts either way.
  documents: async (projectId, { includeDrafts = false } = {}) => {
    const ws = await workspace();
    const project = ws.projects.find((p) => p.project_id === projectId);
    const rows = await request(
      `/documents?project_id=${encodeURIComponent(projectId)}&include_drafts=${includeDrafts}`,
    );
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
  // Required-document checklist for a stage — what R001 evaluates for
  // mandatory evidence, and what "X/Y requirements satisfied" counts.
  // satisfied/satisfied_by come from the audit engine's RequirementSatisfaction
  // table and are ABAC-filtered server-side: satisfied can be true while
  // satisfied_by is null when the caller can't see the satisfying document.
  // -> [{ requirement_id, stage_id, name, description, is_mandatory, source,
  //       created_at, satisfied, satisfied_by: {document_id, filename} | null }]
  listRequirements: (projectId, stageId) =>
    request(`/projects/${encodeURIComponent(projectId)}/stages/${encodeURIComponent(stageId)}/requirements`),
  // body: { name, description?, is_mandatory? }
  createRequirement: async (projectId, stageId, body) => {
    const r = await request(
      `/projects/${encodeURIComponent(projectId)}/stages/${encodeURIComponent(stageId)}/requirements`,
      { method: 'POST', body },
    );
    clearWorkspaceCache();
    return r;
  },
  // body: { name?, description?, is_mandatory? }
  updateRequirement: async (projectId, stageId, requirementId, body) => {
    const r = await request(
      `/projects/${encodeURIComponent(projectId)}/stages/${encodeURIComponent(stageId)}/requirements/${encodeURIComponent(requirementId)}`,
      { method: 'PATCH', body },
    );
    clearWorkspaceCache();
    return r;
  },
  removeRequirement: async (projectId, stageId, requirementId) => {
    const r = await request(
      `/projects/${encodeURIComponent(projectId)}/stages/${encodeURIComponent(stageId)}/requirements/${encodeURIComponent(requirementId)}`,
      { method: 'DELETE' },
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
  // -> [{ user_id, name, role }]  (project/org admin only)
  members: (projectId, teamId) =>
    request(`/projects/${encodeURIComponent(projectId)}/teams/${encodeURIComponent(teamId)}/members`),
};

// ---------- Documents ----------
export const documentsApi = {
  list: (projectId, opts) => projectsApi.documents(projectId, opts),
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
  // -> [{ version_id, version_number, approval_outcome, uploaded_by,
  //        is_latest, is_live, can_delete, status, file_size_bytes, created_at }]
  versions: (documentId) => request(`/documents/${encodeURIComponent(documentId)}/versions`),
  // -> { version_id, version_number, content_markdown }
  versionContent: (documentId, versionId) =>
    request(`/documents/${encodeURIComponent(documentId)}/versions/${encodeURIComponent(versionId)}/content`),
  // Per-version delete: your own draft/rejected version, only if it sits
  // after the document's current live version. 204 No Content on success.
  deleteVersion: (documentId, versionId) =>
    request(`/documents/${encodeURIComponent(documentId)}/versions/${encodeURIComponent(versionId)}`, {
      method: 'DELETE',
    }),
  // Line-level diff between any two of this document's versions (not tied
  // to a review/chat session — a plain read).
  // -> { added_lines, removed_lines, has_changes, unified_diff }
  versionsDiff: (documentId, fromVersionId, toVersionId) =>
    request(
      `/documents/${encodeURIComponent(documentId)}/versions/diff?from_version_id=${encodeURIComponent(fromVersionId)}&to_version_id=${encodeURIComponent(toVersionId)}`
    ),
  // Master Plan v2, item 12: re-uploading an existing document. Never
  // silently replaces the current version — returns a real diff + initial
  // scan + a session_id to continue the review via versionReviewMessage.
  // -> { document_id, session_id, diff, scan, scan_error, reformed_content,
  //      injection_flagged, injection_findings, failed_criteria, reply }
  uploadVersion: async (documentId, file) => {
    const form = new FormData();
    form.append('file', file);
    const token = getToken();
    const res = await fetch(`${API_BASE}/documents/${encodeURIComponent(documentId)}/upload-version`, {
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
  // UI_FIXES_2026-09-15.md: edit the CURRENT document via chat, no file
  // re-upload — seeds a version-review session from the current version's
  // own content. Continue with versionReviewMessage exactly like uploadVersion.
  // -> { document_id, session_id, reply }
  startEdit: (documentId) =>
    request(`/documents/${encodeURIComponent(documentId)}/start-edit`, { method: 'POST' }),
  // One turn of the version-diff-review conversation (revise further, or
  // finalize) — -> { reply, revised, finalized, diff, version_id,
  // version_number, status, scan, scan_error, ..., workflow_reset }
  versionReviewMessage: (documentId, sessionId, message) =>
    request('/documents/review/version-message', {
      method: 'POST',
      body: { document_id: documentId, session_id: sessionId, message: message || '' },
    }),
  // Whole-document delete — project_admin/org_admin only. confirmText must
  // be the literal word "delete" (also re-checked server-side). 204 on success.
  remove: (documentId, confirmText) =>
    request(`/documents/${encodeURIComponent(documentId)}`, {
      method: 'DELETE', body: { confirm_text: confirmText },
    }),
  // -> { document_id, filename, version_id, version_number, version_status,
  //      content_markdown, sensitivity_level, workflow_state, created_at,
  //      scan_overall_score, scan_criteria, scan_passed, injection_flagged }
  view: (documentId) => request(`/documents/${encodeURIComponent(documentId)}/view`),

  submit: (documentId) =>
    request(`/documents/${encodeURIComponent(documentId)}/submit`, { method: 'POST' }),
  // override=true only takes effect server-side for org_admin/project_admin —
  // approve_document() re-checks the role itself, this flag alone grants nothing.
  approve: (documentId, { override = false } = {}) =>
    request(`/documents/${encodeURIComponent(documentId)}/approve`, {
      method: 'POST', body: { override },
    }),
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
  // -> { status, email, role, project_id, team_id, expires_at, invite_link } —
  // email sending deferred, the admin copies/hands out invite_link themselves.
  // projectId/teamId optional (Master Plan v2, item 16): when set, accepting
  // the invite auto-assigns that team role (or project_admin scope).
  invite: (email, role = 'contributor', { projectId, teamId } = {}) =>
    request('/admin/invite', {
      method: 'POST',
      body: { email, role, project_id: projectId || null, team_id: teamId || null },
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
  create: (teamId, reason) => request('/access-requests', { method: 'POST', body: { team_id: teamId, reason } }),
  createForDocument: (documentId, reason) =>
    request('/access-requests', { method: 'POST', body: { document_id: documentId, reason } }),
  createForStage: (stageId, reason) =>
    request('/access-requests', { method: 'POST', body: { stage_id: stageId, reason } }),
  // Effective-state lookup for one exact target — the server resolves
  // native/grant/pending/terminal precedence; the caller only renders the
  // returned verdict, never re-derives it.
  status: ({ documentId, stageId, teamId } = {}) => {
    const params = new URLSearchParams();
    if (documentId) params.set('document_id', documentId);
    if (stageId) params.set('stage_id', stageId);
    if (teamId) params.set('team_id', teamId);
    return request(`/access-requests/status?${params.toString()}`);
  },
  // requests the caller may decide (team_lead on that team / project_admin / org_admin)
  pending: () => request('/access-requests/pending'),
  // currently-active stage-scope grants the caller may revoke — the Revoke
  // counterpart to pending(): an approved request drops out of pending()
  // the moment it's decided, so this is the only place a lead can look one
  // up again to revoke it.
  activeGrants: () => request('/access-requests/active-grants'),
  // tier/duration are REQUIRED by the backend for a stage-scope request
  // (422 otherwise) — omit both for document/team-scope approvals, which
  // still use the old fixed-TTL, always-read-only behavior.
  approve: (id, { tier, duration } = {}) =>
    request(`/access-requests/${encodeURIComponent(id)}/approve`, {
      method: 'POST',
      body: tier && duration ? { tier, duration } : {},
    }),
  deny: (id) => request(`/access-requests/${encodeURIComponent(id)}/deny`, { method: 'POST' }),
  // approved -> revoked, immediately and permanently — no undo besides the
  // requester asking again.
  revoke: (id) => request(`/access-requests/${encodeURIComponent(id)}/revoke`, { method: 'POST' }),
};

// ---------- Search Agent: REAL — the Search tab ----------
// POST /agents/search/message runs one turn of the merged Search agent
// (grounded content Q&A + read-only metadata Q&A in one conversation —
// replaces the former separate RAG and Query agents/tabs). Conversation
// ownership is per (user, project); pass session_id back to continue, or omit
// it (null) to start fresh.
export const searchApi = {
  // -> { reply, tools_called: [...], session_id }
  message: (projectId, sessionId, message) =>
    request('/agents/search/message', {
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
  // layout (Master Plan v2, item 14) is only applied on a fresh session's
  // first turn — pass it once, omit on every later message.
  draftMessage: (sessionId, message, projectId, layout) =>
    request('/agents/draft/message', {
      method: 'POST',
      body: {
        session_id: sessionId, message: message || '', project_id: projectId || null,
        layout: layout || null,
      },
    }),
  // Item 14 — built-in standard section layouts for the template picker.
  // -> [{ id, label, document_type, sections: [{name, purpose}] }]
  draftLayouts: () => request('/agents/draft/layouts'),
  // Item 14 — "Upload template": a real reference document in, a
  // content-stripped section outline out. Nothing persisted server-side;
  // the caller holds the returned sections and passes them as `layout`.
  extractOutline: async (file) => {
    const form = new FormData();
    form.append('file', file);
    const token = getToken();
    const res = await fetch(`${API_BASE}/agents/draft/extract-outline`, {
      method: 'POST',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: form,
    });
    const text = await res.text();
    let payload = null;
    if (text) { try { payload = JSON.parse(text); } catch { payload = text; } }
    if (!res.ok) {
      let detail = res.statusText || 'Could not extract a template from this file';
      if (payload && typeof payload.detail === 'string') detail = payload.detail;
      throw new ApiError(detail, res.status, payload);
    }
    return payload; // { sections: [{name, purpose}] }
  },
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

export const notesApi = stub('Notes');

export const notificationsApi = {
  list: () => request('/notifications'),
  markRead: (notificationId) =>
    request(`/notifications/${encodeURIComponent(notificationId)}/read`, { method: 'POST' }),
  markAllRead: () => request('/notifications/read-all', { method: 'POST' }),
};

// ---------- Project Intelligence (real backend) ----------
export const intelligenceApi = {
  metrics: (projectId) =>
    request(`/projects/${encodeURIComponent(projectId)}/intelligence/metrics`),
  gaps: (projectId, stageId) => {
    const q = stageId ? `?stage_id=${encodeURIComponent(stageId)}` : '';
    return request(`/projects/${encodeURIComponent(projectId)}/intelligence/gaps${q}`);
  },
  findings: (projectId, { severity, ruleCode, isBlocker, stageId, status } = {}) => {
    const params = new URLSearchParams();
    if (severity) params.append('severity', severity);
    if (ruleCode) params.append('rule_code', ruleCode);
    if (isBlocker !== undefined && isBlocker !== null) params.append('is_blocker', isBlocker);
    if (stageId) params.append('stage_id', stageId);
    if (status) params.append('status', status);
    const qs = params.toString();
    return request(`/projects/${encodeURIComponent(projectId)}/intelligence/findings${qs ? `?${qs}` : ''}`);
  },
  dismissFinding: (projectId, findingId, reason) =>
    request(`/projects/${encodeURIComponent(projectId)}/intelligence/findings/${encodeURIComponent(findingId)}/dismiss`, {
      method: 'POST',
      body: { reason },
    }),
  restoreFinding: (projectId, findingId) =>
    request(`/projects/${encodeURIComponent(projectId)}/intelligence/findings/${encodeURIComponent(findingId)}/restore`, {
      method: 'POST',
    }),
  neighborhood: (projectId, { nodeId, sourceTable, sourceId, depth = 1 } = {}) => {
    const params = new URLSearchParams();
    if (nodeId) params.append('node_id', nodeId);
    if (sourceTable) params.append('source_table', sourceTable);
    if (sourceId) params.append('source_id', sourceId);
    if (depth) params.append('depth', depth);
    const qs = params.toString();
    return request(`/projects/${encodeURIComponent(projectId)}/intelligence/neighborhood${qs ? `?${qs}` : ''}`);
  },
  progressHistory: (projectId, limit = 50) =>
    request(`/projects/${encodeURIComponent(projectId)}/intelligence/progress-history?limit=${encodeURIComponent(limit)}`),
  timeline: (projectId, limit = 100) =>
    request(`/projects/${encodeURIComponent(projectId)}/intelligence/timeline?limit=${encodeURIComponent(limit)}`),
  triggerAudit: (projectId, targetStageId = null) =>
    request(`/projects/${encodeURIComponent(projectId)}/intelligence/audit`, {
      method: 'POST',
      body: { target_stage_id: targetStageId || null },
    }),
  markComplete: (projectId) =>
    request(`/projects/${encodeURIComponent(projectId)}/intelligence/mark-complete`, {
      method: 'POST',
    }),
  reopen: (projectId) =>
    request(`/projects/${encodeURIComponent(projectId)}/intelligence/reopen`, {
      method: 'POST',
    }),
};

export { ApiError };

