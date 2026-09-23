import React, { useEffect, useState } from 'react';
import { Link, Navigate, useSearchParams } from 'react-router-dom';
import { ArrowLeft, ShieldCheck, Users, ClipboardList, ScrollText, Check, X, Clock3, Trash2, Search, ChevronDown, ChevronRight, FolderKanban } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import { adminApi, projectsApi, documentsApi, workspaceApi, accessRequestsApi, activityApi } from '../lib/api';
import Button from '../components/ui/Button';
import Input from '../components/ui/Input';
import Dropdown from '../components/ui/Dropdown';
import Badge from '../components/ui/Badge';
import Card from '../components/ui/Card';
import Modal from '../components/ui/Modal';
import BackButton from '../components/ui/BackButton';
import DocumentReviewModal from '../components/sources/DocumentReviewModal';

const TABS = [
  { id: 'users', label: 'Users & Access', icon: Users },
  { id: 'approvals', label: 'Pending Approvals', icon: ClipboardList },
  { id: 'audit', label: 'Audit Log', icon: ScrollText },
  { id: 'activity', label: 'Project Activity', icon: Clock3 },
];

const TEAM_ROLES = ['viewer', 'contributor', 'team_lead'];

const TEAM_ROLE_CHOICES = [
  { label: '— not assigned —', value: '' },
  { label: 'Viewer', value: 'viewer' },
  { label: 'Contributor', value: 'contributor' },
  { label: 'Team Lead', value: 'team_lead' },
];
const TEAM_ROLE_OPTIONS = TEAM_ROLE_CHOICES.slice(1);
const GRANT_TIER_OPTIONS = [
  { label: 'Viewer — public/internal docs only', value: 'viewer' },
  { label: 'Contributor — viewer + edit/upload', value: 'contributor' },
  { label: 'Contributor + confidential', value: 'contributor_confidential' },
];
const GRANT_DURATION_OPTIONS = [
  { label: '72 hours', value: 'hours_72' },
  { label: '1 week', value: 'week_1' },
  { label: '1 month', value: 'month_1' },
  { label: 'No expiration', value: 'unlimited' },
];
const MODE_OPTIONS = [
  { label: 'Team Member', value: 'team_member' },
  { label: 'Project Admin', value: 'project_admin' },
  { label: 'Org Admin', value: 'org_admin' },
];

// Small inline multi-select (the shared Dropdown is single-select only).
const CheckList = ({ label, options, selected, onToggle, allLabel, allSelected, onToggleAll, emptyText }) => (
  <div className="w-full flex flex-col gap-1.5">
    <label className="text-sm font-medium text-gray-300">{label}</label>
    <div className="rounded-md border border-border bg-background p-2 space-y-0.5 max-h-52 overflow-y-auto scrollbar-thin">
      {allLabel && (
        <label className="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-surface-hover cursor-pointer text-sm text-gray-200">
          <input type="checkbox" checked={allSelected} onChange={onToggleAll} className="accent-primary" />
          {allLabel}
        </label>
      )}
      {options.length === 0 && <p className="px-2 py-1.5 text-sm text-gray-600">{emptyText || 'Nothing to choose.'}</p>}
      {options.map((o) => (
        <label
          key={o.value}
          className={`flex items-center gap-2 px-2 py-1.5 rounded text-sm ${
            allSelected ? 'opacity-40' : 'hover:bg-surface-hover cursor-pointer text-gray-300'
          }`}
        >
          <input
            type="checkbox"
            disabled={allSelected}
            checked={selected.includes(o.value)}
            onChange={() => onToggle(o.value)}
            className="accent-primary"
          />
          {o.label}
        </label>
      ))}
    </div>
  </div>
);

const AdminPage = () => {
  const { user } = useAuth();
  const [searchParams] = useSearchParams();
  const requestedProjectId = searchParams.get('project_id') || '';
  const isOrgAdmin = Boolean(user?.is_org_admin);
  // team_lead / project_admin can review (approve documents, decide access requests)
  // within their own scope; the backend still enforces the per-team check.
  const REVIEW_ROLES = ['team_lead', 'project_admin', 'admin'];
  const roleValues = Object.values(user?.project_roles || {});
  const isProjectAdminAnywhere = roleValues.includes('project_admin');
  const isTeamLeadAnywhere = roleValues.includes('team_lead');
  const hasApprovalAccess = isOrgAdmin || roleValues.some((role) => REVIEW_ROLES.includes(role));
  // Finalized access design:
  //   Audit Log     -> org_admin, project_admin, team_lead   (contributor/viewer: none)
  //   Project Activity -> everyone EXCEPT a viewer-only user  (i.e. any contributor+ role)
  const canViewAudit = isOrgAdmin || isProjectAdminAnywhere || isTeamLeadAnywhere;
  const canViewActivity = isOrgAdmin
    || roleValues.some((role) => ['contributor', 'team_lead', 'project_admin'].includes(role));
  const canManageUsers = isOrgAdmin || isProjectAdminAnywhere || isTeamLeadAnywhere;
  const availableTabs = TABS.filter((tab) => {
    if (tab.id === 'users') return canManageUsers;
    if (tab.id === 'audit') return canViewAudit;
    if (tab.id === 'activity') return canViewActivity;
    if (tab.id === 'approvals') return hasApprovalAccess;
    return true;
  });
  const [tab, setTab] = useState('users');
  const [projects, setProjects] = useState([]);
  const visibleProjects = isOrgAdmin
    ? projects
    : projects.filter((project) => REVIEW_ROLES.includes(user?.project_roles?.[project.project_id]));

  useEffect(() => {
    projectsApi.list().then(setProjects).catch(() => {});
  }, []);

  useEffect(() => {
    if (!availableTabs.some((item) => item.id === tab)) setTab(availableTabs[0]?.id || 'users');
  }, [tab, availableTabs]);

  const canReachAdmin = canManageUsers || canViewAudit || canViewActivity || hasApprovalAccess;
  if (!canReachAdmin) {
    return <Navigate to="/" replace />;
  }

  return (
    <div className="flex-1 p-8 max-w-5xl mx-auto w-full">
      <div className="mb-6">
        <BackButton fallbackTo={requestedProjectId ? `/projects/${encodeURIComponent(requestedProjectId)}` : '/'} className="mb-4" />
        <h1 className="text-2xl font-bold text-gray-100 flex items-center gap-2">
          <ShieldCheck className="text-primary" size={22} />
          Access &amp; Governance
        </h1>
        <p className="text-gray-400 mt-1">User access, approvals, and access-control diagnostics.</p>
      </div>

      <div className="flex justify-center mb-6">
        <div className="inline-flex items-center gap-1 p-1 rounded-xl border border-border bg-surface/80 max-w-full overflow-x-auto">
          {availableTabs.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`flex items-center gap-2 px-3.5 py-2 text-sm font-medium rounded-lg transition-all whitespace-nowrap ${
                tab === t.id
                  ? 'bg-background text-gray-100 shadow-xs border border-border/60 font-semibold'
                  : 'text-gray-400 hover:text-gray-200 hover:bg-surface-hover/60 border border-transparent'
              }`}
            >
              <t.icon size={16} className={tab === t.id ? 'text-primary' : ''} />
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {tab === 'users' && canManageUsers && <UsersTab currentUser={user} initialProjectId={requestedProjectId} />}
      {tab === 'approvals' && hasApprovalAccess && <ApprovalsTab projects={visibleProjects} canOverrideScan={isOrgAdmin} />}
      {tab === 'audit' && canViewAudit && <AuditTab />}
      {tab === 'activity' && canViewActivity && <ProjectActivityTab />}
    </div>
  );
};

// Outcome/status -> Badge variant (shared by Project Activity + Audit Log).
const STATUS_VARIANT = {
  approved: 'success',
  rejected: 'danger',
  denied: 'danger',
  pending: 'warning',
  pending_review: 'warning',
  draft: 'neutral',
};
const statusVariant = (s) => STATUS_VARIANT[s] || 'neutral';

// Finalized Project Activity design:
//   project cards (only projects you're part of)
//     -> single-select team dropdown
//       -> flat activity list for that team, each entry showing its stage +
//          a workflow outcome. Entries tied to a confidential document only
//          appear if the backend's can_view_document() passes for you — team
//          membership alone does not bypass sensitivity clearance.
const ProjectActivityTab = () => {
  const [projects, setProjects] = useState(null);
  const [loadError, setLoadError] = useState('');
  const [openProject, setOpenProject] = useState(null);
  const [teamId, setTeamId] = useState('');
  const [feed, setFeed] = useState(null);
  const [feedLoading, setFeedLoading] = useState(false);
  const [feedError, setFeedError] = useState('');

  useEffect(() => {
    activityApi.projects()
      .then(setProjects)
      .catch((err) => setLoadError(err.message || 'Could not load projects.'));
  }, []);

  const enterProject = (project) => {
    setOpenProject(project);
    setFeed(null);
    setFeedError('');
    // Skip the team-picker step entirely when there's only one team to pick
    // — a dropdown with exactly one option is friction, not a choice.
    if (project.teams.length === 1) {
      pickTeam(project.teams[0].team_id, project);
    } else {
      setTeamId('');
    }
  };

  const pickTeam = async (nextTeamId, projectOverride) => {
    const project = projectOverride || openProject;
    setTeamId(nextTeamId);
    setFeed(null);
    setFeedError('');
    if (!nextTeamId || !project) return;
    setFeedLoading(true);
    try {
      setFeed(await activityApi.feed(project.project_id, nextTeamId));
    } catch (err) {
      setFeedError(err.message || 'Could not load activity for this team.');
    } finally {
      setFeedLoading(false);
    }
  };

  if (loadError) return <p className="text-sm text-red-400">{loadError}</p>;
  if (projects === null) {
    return (
      <div className="flex justify-center py-8">
        <div className="w-6 h-6 border-4 border-primary/30 border-t-primary rounded-full animate-spin" />
      </div>
    );
  }

  // ---- project card grid ----
  if (!openProject) {
    if (projects.length === 0) {
      return (
        <Card title="Project Activity">
          <p className="text-sm text-gray-500">
            You&rsquo;re not a contributing member of any project, so there&rsquo;s no activity to show.
          </p>
        </Card>
      );
    }
    return (
      <div className="space-y-4">
        <p className="text-sm text-gray-400">Pick a project to see its document activity.</p>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {projects.map((project) => (
            <button
              key={project.project_id}
              type="button"
              onClick={() => enterProject(project)}
              className="text-left rounded-xl border border-border bg-surface p-4 hover:border-primary/50 hover:bg-surface-hover transition-colors flex flex-col gap-3"
            >
              <div className="flex items-start justify-between gap-2">
                <span className="flex items-center gap-2 min-w-0">
                  <FolderKanban size={16} className="text-primary shrink-0" />
                  <span className="font-semibold text-gray-100 truncate">{project.project_name}</span>
                </span>
                <Badge variant={project.admin_here ? 'active' : 'neutral'}>
                  {project.admin_here ? 'Full access' : 'Your team'}
                </Badge>
              </div>
              <div className="flex items-center justify-between gap-2 mt-auto">
                <p className="text-xs text-gray-500">
                  {project.teams.length} team{project.teams.length === 1 ? '' : 's'} you can view
                </p>
                <ChevronRight size={14} className="text-gray-500 shrink-0" />
              </div>
            </button>
          ))}
        </div>
      </div>
    );
  }

  // ---- one project: team dropdown + flat activity list ----
  const teamOptions = openProject.teams.map((t) => ({ label: t.name, value: t.team_id }));
  return (
    <div className="space-y-5">
      <button
        type="button"
        onClick={() => { setOpenProject(null); setTeamId(''); setFeed(null); }}
        className="text-gray-400 hover:text-gray-200 transition-colors flex items-center gap-1 text-sm"
      >
        <ArrowLeft size={15} /> All projects
      </button>

      {openProject.teams.length > 1 ? (
        <Card
          title={openProject.project_name}
          description={openProject.admin_here
            ? 'You administer this project — you can view activity for any team.'
            : 'You can view activity for the team(s) you contribute to or lead.'}
        >
          <Dropdown
            label="Team"
            options={teamOptions}
            value={teamId}
            onChange={pickTeam}
            placeholder="Select a team"
          />
        </Card>
      ) : (
        <div>
          <h3 className="font-semibold text-gray-100">{openProject.project_name}</h3>
          <p className="text-sm text-gray-400 mt-1">Showing activity for {openProject.teams[0]?.name}.</p>
        </div>
      )}

      {feedError && <p className="text-sm text-red-400">{feedError}</p>}
      {feedLoading && <p className="text-sm text-gray-400">Loading activity…</p>}

      {feed && (
        <Card
          title="Team activity"
          description="Newest first. Each entry shows its stage and current workflow outcome. Activity for confidential documents only appears if you have clearance for them."
        >
          <div className="space-y-3">
            {feed.map((event) => (
              <div key={event.log_id} className="rounded-lg border border-border bg-background p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2 flex-wrap">
                    <Badge variant="active">{event.action.replace(/_/g, ' ').toLowerCase()}</Badge>
                    <span className="text-sm text-gray-200">{event.actor_name}</span>
                    {event.status && <Badge variant={statusVariant(event.status)}>{event.status.replace(/_/g, ' ')}</Badge>}
                  </div>
                  <span className="text-xs text-gray-500">{new Date(event.timestamp).toLocaleString()}</span>
                </div>
                <p className="text-xs text-gray-400 mt-2">
                  <span className="text-gray-300">{event.filename}</span>
                  {' · '}Stage: {event.stage}
                  {' · '}{event.sensitivity_level}
                </p>
                {event.rejection_reason && (
                  <p className="text-xs text-red-400 mt-1">Rejection reason: {event.rejection_reason}</p>
                )}
                {event.details && <p className="text-xs text-gray-500 mt-1">{event.details}</p>}
              </div>
            ))}
            {feed.length === 0 && (
              <p className="text-sm text-gray-500">No activity recorded for this team yet.</p>
            )}
          </div>
        </Card>
      )}
    </div>
  );
};

const UsersTab = ({ currentUser, initialProjectId = '' }) => {
  const [users, setUsers] = useState([]);
  const [workspace, setWorkspace] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [expandedUserId, setExpandedUserId] = useState(null);
  const [userSearch, setUserSearch] = useState('');
  // Which action is in flight, if any — a string key rather than a plain
  // boolean, so only the button actually clicked shows a spinner (a single
  // shared `busy` flag previously lit up "Invite by link", "Add user", and
  // "Assign Roles" together, whichever one was clicked).
  const [busyAction, setBusyAction] = useState(null);
  const busy = busyAction !== null;

  // "Add organization user"
  const [email, setEmail] = useState('');
  const [inviteRole, setInviteRole] = useState('contributor');

  // "Assign Roles" modal
  const [assignOpen, setAssignOpen] = useState(false);
  const [assignEmail, setAssignEmail] = useState('');
  // Section B — "Add new access"
  const [bMode, setBMode] = useState('team_member'); // team_member | project_admin | org_admin
  const [bProjectId, setBProjectId] = useState(initialProjectId);
  const [bTeamRoles, setBTeamRoles] = useState({}); // { [team_id]: 'viewer'|'contributor'|'team_lead' }
  const [bProjectIds, setBProjectIds] = useState([]);
  const [bAllProjects, setBAllProjects] = useState(false);

  const isOrgAdmin = Boolean(currentUser?.is_org_admin);
  const wsProjects = workspace?.projects || [];

  const load = async () => {
    setLoading(true);
    try {
      const [u, ws] = await Promise.all([
        adminApi.listUsers(),
        workspaceApi.get().catch(() => null),
      ]);
      setUsers(u);
      if (ws) setWorkspace(ws);
      return u;
    } catch (err) {
      setError(err.message || 'Could not load users.');
      return null;
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const resetAssign = () => {
    setBMode('team_member'); setBProjectId(''); setBTeamRoles({});
    setBProjectIds([]); setBAllProjects(false);
  };

  const openAssign = (targetEmail = '') => {
    setAssignEmail(targetEmail);
    resetAssign();
    setError(''); setNotice('');
    setAssignOpen(true);
  };

  // The directory row for whoever's email is in the modal (null for a brand-new invite).
  const assignTarget = users.find(
    (u) => u.username.toLowerCase() === assignEmail.trim().toLowerCase(),
  ) || null;
  const isSelfTarget = assignTarget && assignTarget.user_id === currentUser?.user_id;
  const orgAdminCount = users.filter((u) => u.is_org_admin).length;

  const handleInvite = async () => {
    if (!email.trim()) return;
    setBusyAction('invite'); setError(''); setNotice('');
    try {
      const res = await adminApi.invite(email.trim(), inviteRole);
      setNotice(`Invite link for ${res.email}: ${res.invite_link}`);
      setEmail('');
    } catch (err) {
      setError(err.message || 'Could not create the invite.');
    } finally {
      setBusyAction(null);
    }
  };

  const bProject = wsProjects.find((p) => p.project_id === bProjectId) || null;
  const bProjectTeams = bProject?.teams || [];
  const modalProjectOptions = wsProjects.map((p) => ({ label: p.name, value: p.project_id }));
  const bTeamAssignments = Object.entries(bTeamRoles)
    .filter(([, role]) => TEAM_ROLES.includes(role))
    .map(([team_id, role]) => ({ team_id, role }));
  const grantingAdminMode = bMode === 'project_admin' || bMode === 'org_admin';

  const canSubmitAssign = assignEmail.trim() && (
    (bMode === 'org_admin') ||
    (bMode === 'project_admin' && (bAllProjects || bProjectIds.length > 0)) ||
    (bMode === 'team_member' && bTeamAssignments.length > 0)
  ) && !(grantingAdminMode && !isOrgAdmin);

  const refreshTarget = async () => { await load(); };

  const handleAddAccess = async () => {
    if (!canSubmitAssign) return;
    let body;
    if (bMode === 'team_member') {
      body = { email: assignEmail.trim(), mode: 'team_member', team_assignments: bTeamAssignments };
    } else if (bMode === 'project_admin') {
      body = {
        email: assignEmail.trim(), mode: 'project_admin',
        all_projects: bAllProjects, project_ids: bAllProjects ? [] : bProjectIds,
      };
    } else {
      body = { email: assignEmail.trim(), mode: 'org_admin' };
    }
    setBusyAction('add-access'); setError(''); setNotice('');
    try {
      const res = await adminApi.assignRoles(body);
      setNotice(res.message || 'Access added.');
      resetAssign();
      await load();
    } catch (err) {
      setError(err.message || 'Could not add access.');
    } finally {
      setBusyAction(null);
    }
  };

  const handleChangeMembershipRole = async (membershipId, role) => {
    setBusyAction(`membership-role-${membershipId}`); setError(''); setNotice('');
    try {
      await adminApi.updateTeamRole(membershipId, role);
      setNotice('Role updated.');
      await refreshTarget();
    } catch (err) {
      setError(err.message || 'Could not update the role.');
    } finally {
      setBusyAction(null);
    }
  };

  const handleRemoveMembership = async (membershipId, label) => {
    if (!window.confirm(`Remove this access${label ? ` (${label})` : ''}?`)) return;
    setBusyAction(`membership-remove-${membershipId}`); setError(''); setNotice('');
    try {
      await adminApi.removeTeamMembership(membershipId);
      setNotice('Access removed.');
      await refreshTarget();
    } catch (err) {
      setError(err.message || 'Could not remove the access.');
    } finally {
      setBusyAction(null);
    }
  };

  const handleRemoveProjectAdmin = async (scopeId, projectName) => {
    if (!window.confirm(`Remove Project Admin on ${projectName}?`)) return;
    setBusyAction(`project-admin-remove-${scopeId}`); setError(''); setNotice('');
    try {
      await adminApi.removeProjectAdmin(scopeId);
      setNotice('Project Admin removed.');
      await refreshTarget();
    } catch (err) {
      setError(err.message || 'Could not remove Project Admin.');
    } finally {
      setBusyAction(null);
    }
  };

  const handleRevokeOrgAdmin = async (targetUser) => {
    if (isSelfTarget) {
      setError('You cannot revoke your own organization-admin status.');
      return;
    }
    if (!window.confirm(`Remove organization-admin from ${targetUser.username}?`)) return;
    setBusyAction('revoke-org-admin'); setError(''); setNotice('');
    try {
      await adminApi.revokeOrgAdmin(targetUser.user_id);
      setNotice(`${targetUser.username} is no longer an organization admin.`);
      await refreshTarget();
    } catch (err) {
      setError(err.message || 'Could not revoke organization-admin.');
    } finally {
      setBusyAction(null);
    }
  };

  return (
    <div className="space-y-6">
      <Modal
        open={assignOpen}
        onClose={() => setAssignOpen(false)}
        title="Assign Roles"
        description="Review and edit what this person can access, then add new access below."
      >
        <div className="space-y-6 max-h-[70vh] overflow-y-auto scrollbar-thin pr-1">
          <Input
            label="User email"
            value={assignEmail}
            onChange={(e) => setAssignEmail(e.target.value)}
            placeholder="teammate@company.com"
          />

          {(notice || error) && (
            <div className="space-y-1">
              {notice && <p className="text-emerald-400 text-sm">{notice}</p>}
              {error && <p className="text-red-400 text-sm">{error}</p>}
            </div>
          )}

          {/* ---------- Section A — Current access ---------- */}
          <section className="space-y-3">
            <h4 className="text-xs font-semibold uppercase tracking-wider text-gray-500">Current access</h4>

            {!assignTarget ? (
              <p className="text-sm text-gray-500">
                No account with that email yet — it will be created when you add access below.
              </p>
            ) : (
              <div className="space-y-4">
                {/* team memberships */}
                <div>
                  <p className="text-xs text-gray-500 mb-1.5">Team roles</p>
                  {assignTarget.team_memberships?.length ? (
                    <div className="space-y-2">
                      {assignTarget.team_memberships.map((m) => (
                        <div key={m.membership_id} className="flex items-center gap-2 rounded-md border border-border bg-background px-3 py-2">
                          <div className="min-w-0 flex-1">
                            <p className="text-sm text-gray-200 truncate">{m.team_name}</p>
                            <p className="text-xs text-gray-500 truncate">{m.project_name}</p>
                          </div>
                          <div className="w-36 shrink-0">
                            <Dropdown
                              options={TEAM_ROLE_OPTIONS}
                              value={m.role}
                              onChange={(value) => { if (value !== m.role) handleChangeMembershipRole(m.membership_id, value); }}
                            />
                          </div>
                          <button
                            type="button"
                            onClick={() => handleRemoveMembership(m.membership_id, `${m.role} on ${m.team_name}`)}
                            disabled={busy}
                            className="shrink-0 inline-flex items-center gap-1 rounded-md border border-red-500/30 px-2 py-1.5 text-xs text-red-400 hover:bg-red-500/10 disabled:opacity-50"
                          >
                            <Trash2 size={13} /> Remove
                          </button>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="text-sm text-gray-600">No team roles.</p>
                  )}
                </div>

                {/* project admin scopes */}
                <div>
                  <p className="text-xs text-gray-500 mb-1.5">Project Admin</p>
                  {assignTarget.project_admin?.length ? (
                    <div className="space-y-2">
                      {assignTarget.project_admin.map((pa) => (
                        <div key={pa.id} className="flex items-center gap-2 rounded-md border border-border bg-background px-3 py-2">
                          <p className="min-w-0 flex-1 text-sm text-gray-200 truncate">{pa.project_name}</p>
                          <button
                            type="button"
                            onClick={() => handleRemoveProjectAdmin(pa.id, pa.project_name)}
                            disabled={busy || !isOrgAdmin}
                            title={isOrgAdmin ? '' : 'Only organization admins can change this'}
                            className="shrink-0 inline-flex items-center gap-1 rounded-md border border-red-500/30 px-2 py-1.5 text-xs text-red-400 hover:bg-red-500/10 disabled:opacity-50"
                          >
                            <Trash2 size={13} /> Remove
                          </button>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="text-sm text-gray-600">Not a Project Admin anywhere.</p>
                  )}
                </div>

                {/* org admin */}
                <div>
                  <p className="text-xs text-gray-500 mb-1.5">Organization Admin</p>
                  {assignTarget.is_org_admin ? (
                    <div className="flex items-center gap-2 rounded-md border border-border bg-background px-3 py-2">
                      <Badge variant="active">Organization Admin</Badge>
                      <span className="flex-1" />
                      <button
                        type="button"
                        onClick={() => handleRevokeOrgAdmin(assignTarget)}
                        disabled={busy || !isOrgAdmin || isSelfTarget || orgAdminCount <= 1}
                        title={
                          isSelfTarget ? 'You cannot revoke your own organization-admin status'
                            : orgAdminCount <= 1 ? 'Cannot remove the last organization admin'
                            : !isOrgAdmin ? 'Only organization admins can change this' : ''
                        }
                        className="shrink-0 inline-flex items-center gap-1 rounded-md border border-red-500/30 px-2 py-1.5 text-xs text-red-400 hover:bg-red-500/10 disabled:opacity-50"
                      >
                        <Trash2 size={13} /> Revoke
                      </button>
                    </div>
                  ) : (
                    <p className="text-sm text-gray-600">Not an organization admin.</p>
                  )}
                  {assignTarget.is_org_admin && isSelfTarget && (
                    <p className="text-xs text-amber-400 mt-1">You cannot revoke your own organization-admin status.</p>
                  )}
                  {assignTarget.is_org_admin && !isSelfTarget && orgAdminCount <= 1 && (
                    <p className="text-xs text-amber-400 mt-1">This is the last organization admin — the role cannot be removed.</p>
                  )}
                </div>
              </div>
            )}
          </section>

          <div className="border-t border-border/60" />

          {/* ---------- Section B — Add new access ---------- */}
          <section className="space-y-3">
            <h4 className="text-xs font-semibold uppercase tracking-wider text-gray-500">Add new access</h4>

            <Dropdown
              label="Access type"
              options={MODE_OPTIONS}
              value={bMode}
              onChange={(value) => {
                setBMode(value);
                setBProjectId(''); setBTeamRoles({});
                setBProjectIds([]); setBAllProjects(false);
              }}
            />

            {bMode === 'team_member' && (
              <>
                <Dropdown
                  label="Project"
                  options={modalProjectOptions}
                  value={bProjectId}
                  onChange={(value) => { setBProjectId(value); setBTeamRoles({}); }}
                  placeholder={wsProjects.length ? 'Select a project' : 'No projects available'}
                />
                {bProjectId && (
                  <div className="space-y-2">
                    <p className="text-sm font-medium text-gray-300">
                      Teams in {bProject?.name || 'project'} — pick a role per team
                    </p>
                    {bProjectTeams.length === 0 && (
                      <p className="text-sm text-gray-600">This project has no teams yet.</p>
                    )}
                    {bProjectTeams.map((t) => (
                      <div key={t.team_id} className="flex items-center gap-2">
                        <span className="min-w-0 flex-1 text-sm text-gray-200 truncate">{t.name}</span>
                        <div className="w-44 shrink-0">
                          <Dropdown
                            options={TEAM_ROLE_CHOICES}
                            value={bTeamRoles[t.team_id] || ''}
                            onChange={(value) => setBTeamRoles((cur) => ({ ...cur, [t.team_id]: value }))}
                            placeholder="— not assigned —"
                          />
                        </div>
                      </div>
                    ))}
                    <p className="text-xs text-gray-500">
                      Only teams with a role selected are included. You can assign different roles to different teams in one action.
                    </p>
                  </div>
                )}
              </>
            )}

            {bMode === 'project_admin' && (
              <CheckList
                label="Projects"
                options={modalProjectOptions}
                selected={bProjectIds}
                onToggle={(id) => setBProjectIds((cur) => cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id])}
                allLabel="All projects"
                allSelected={bAllProjects}
                onToggleAll={() => setBAllProjects((v) => !v)}
                emptyText="No projects available."
              />
            )}

            {bMode === 'org_admin' && (
              <p className="text-sm text-gray-400">
                Organization Admin is a single account-wide flag — it grants full access to every
                project and team automatically. No project or team selection needed.
              </p>
            )}

            {grantingAdminMode && !isOrgAdmin && (
              <p className="text-xs text-amber-400">
                Only organization admins can grant Project Admin or Org Admin.
              </p>
            )}

            <Button onClick={handleAddAccess} loading={busyAction === 'add-access'} disabled={!canSubmitAssign || busy}>
              Add access
            </Button>
          </section>
        </div>
      </Modal>

      {isOrgAdmin ? (
        <Card title="Add organization user" description="Enter an email, then pick one of the two options below — both use this same email.">
          <div className="space-y-4">
            <Input label="Email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="new.user@company.com" />
            {!email.trim() && (
              <p className="text-xs text-amber-400/80 -mt-2">Enter an email above first — every option below is disabled until you do.</p>
            )}

            <div className="border-t border-border/60 pt-4">
              <p className="text-xs font-semibold text-gray-400 mb-2">Option 1 — Invite by link (recommended: they set their own password)</p>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-3 items-end">
                <Dropdown
                  label="Role"
                  options={[
                    { label: 'Viewer', value: 'viewer' },
                    { label: 'Contributor', value: 'contributor' },
                    { label: 'Team Lead', value: 'team_lead' },
                    { label: 'Project Admin', value: 'project_admin' },
                    { label: 'Org Admin', value: 'org_admin' },
                  ]}
                  value={inviteRole}
                  onChange={setInviteRole}
                />
                <Button onClick={handleInvite} loading={busyAction === 'invite'} disabled={!email.trim() || busy}>Invite by link</Button>
              </div>
              <p className="text-xs text-gray-500 mt-1.5">Single-use link, expires in 72 hours.</p>
            </div>

            <div className="border-t border-border/60 pt-4">
              <p className="text-xs font-semibold text-gray-400 mb-2">Option 2 — Assign roles directly (fine-grained: pick a role per team, or grant Project/Org Admin)</p>
              <Button variant="secondary" onClick={() => openAssign(email.trim())} disabled={!email.trim()}>Assign Roles</Button>
              <p className="text-xs text-gray-500 mt-1.5">Works on an existing user too — or use the button on their row below. If the email has no account yet, one is created automatically with a default password (shown after) — always attached to the access you just granted, never an orphan account.</p>
            </div>
          </div>
        </Card>
      ) : (
        <Card title="Assign roles" description="Change the role of someone already in one of your projects. Adding a new person to a project requires a project admin.">
          <Button onClick={() => openAssign('')}>Assign Roles</Button>
        </Card>
      )}

      {notice && <p className="text-emerald-400 text-sm">{notice}</p>}
      {error && <p className="text-red-400 text-sm">{error}</p>}
      <Card>
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 pb-5 border-b border-border/50">
          <div>
            <h3 className="font-semibold text-gray-100">Users in your organization</h3>
            <p className="text-sm text-gray-400 mt-1">Project roles and org-admin status.</p>
          </div>
          <div className="w-full sm:w-64 sm:ml-auto shrink-0">
            <Input
              aria-label="Search organization users by email"
              value={userSearch}
              onChange={(event) => setUserSearch(event.target.value)}
              placeholder="Search by email..."
              icon={Search}
            />
          </div>
        </div>
        {loading ? (
          <div className="flex justify-center py-8">
            <div className="w-6 h-6 border-4 border-primary/30 border-t-primary rounded-full animate-spin" />
          </div>
        ) : (
          <div className="divide-y divide-border/50">
            {users.filter((u) => {
              const query = userSearch.trim().toLowerCase();
              if (!query) return true;
              return (u.username || '').toLowerCase().includes(query);
            }).filter((u) => isOrgAdmin || u.user_id !== currentUser?.user_id).map((u) => (
              <div key={u.user_id} className="py-5 space-y-4">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <button
                      type="button"
                      onClick={() => setExpandedUserId((current) => current === u.user_id ? null : u.user_id)}
                      aria-expanded={expandedUserId === u.user_id}
                      className="text-left text-base font-semibold text-primary-light underline decoration-primary/50 underline-offset-4 hover:text-primary transition-colors"
                    >
                      {u.full_name || u.username}
                    </button>
                    <p className="text-sm text-gray-500 mt-1">{u.username}</p>
                    {u.is_org_admin && (
                      <div className="mt-2">
                        <Badge variant="active">Organization Admin</Badge>
                      </div>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    <Button size="sm" variant="secondary" onClick={() => openAssign(u.username)}>
                      Assign Roles
                    </Button>
                  </div>
                </div>
                {expandedUserId === u.user_id && (
                  <div className="rounded-lg border border-border/70 bg-background/40 overflow-x-auto">
                    <table className="w-full min-w-[520px] text-left">
                      <caption className="px-3 py-2 border-b border-border/50 text-left text-xs font-semibold uppercase tracking-wider text-gray-500">
                        Project access
                      </caption>
                      <thead className="border-b border-border/50">
                        <tr className="text-xs text-gray-500">
                          <th className="px-3 py-2 font-medium">Project</th>
                          <th className="px-3 py-2 font-medium">Role</th>
                        </tr>
                      </thead>
                      <tbody>
                        {u.roles?.length ? u.roles.map((r) => (
                          <tr key={r.project_id} className="border-b border-border/30 last:border-0">
                            <td className="px-3 py-2.5">
                              <Link
                                to={`/projects/${encodeURIComponent(r.project_id)}`}
                                className="group block rounded-md p-1 -m-1 hover:bg-primary/10 focus:outline-none focus:ring-2 focus:ring-primary/50"
                                aria-label={`Open project ${wsProjects.find((p) => p.project_id === r.project_id)?.name || r.project_id}`}
                              >
                                <p className="text-sm text-primary-light group-hover:text-primary underline underline-offset-2 truncate">
                                  {wsProjects.find((p) => p.project_id === r.project_id)?.name || r.project_id}
                                </p>
                                <p className="text-xs text-gray-600 mt-0.5">{r.project_id}</p>
                              </Link>
                            </td>
                            <td className="px-3 py-2.5"><Badge variant="neutral">{r.role}</Badge></td>
                          </tr>
                        )) : (
                          <tr>
                            <td colSpan="2" className="px-3 py-3 text-sm text-gray-600">No project access assigned.</td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                    <p className="px-3 py-2 text-xs text-gray-600">Use &ldquo;Assign Roles&rdquo; to change or remove access.</p>
                  </div>
                )}
              </div>
            ))}
            {users.length > 0 && !users.some((u) => {
              const query = userSearch.trim().toLowerCase();
              return !query || (u.username || '').toLowerCase().includes(query);
            }) && (
              <p className="py-8 text-center text-sm text-gray-500">No users match “{userSearch}”.</p>
            )}
          </div>
        )}
      </Card>
    </div>
  );
};

// Pending Approvals: every project the caller reviews, each expandable into
// team-grouped cards that show BOTH kinds of pending work —
//   • documents awaiting workflow approval (submit -> approve/reject)
//   • confidential-access requests (approve/deny)
// Every action refreshes the whole view so counts stay accurate.
const ApprovalsTab = ({ projects, canOverrideScan = false }) => {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busyKey, setBusyKey] = useState(null);
  const [grantChoices, setGrantChoices] = useState({}); // request_id -> { tier, duration }
  const [docsByProject, setDocsByProject] = useState({});
  const [accessReqs, setAccessReqs] = useState([]);
  const [activeGrants, setActiveGrants] = useState([]);
  const [openProjectId, setOpenProjectId] = useState('');
  const [reviewDoc, setReviewDoc] = useState(null); // the doc row currently open in DocumentReviewModal, or null

  const projectKey = projects.map((p) => p.project_id).join(',');

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      const [docLists, reqs, grants] = await Promise.all([
        Promise.all(projects.map((p) =>
          projectsApi.pendingApprovals(p.project_id)
            .then((d) => [p.project_id, d])
            .catch(() => [p.project_id, []]),
        )),
        accessRequestsApi.pending().catch(() => []),
        accessRequestsApi.activeGrants().catch(() => []),
      ]);
      setDocsByProject(Object.fromEntries(docLists));
      setAccessReqs(Array.isArray(reqs) ? reqs : []);
      setActiveGrants(Array.isArray(grants) ? grants : []);
    } catch (err) {
      setError(err.message || 'Could not load pending approvals.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [projectKey]);

  const act = async (key, fn, msg) => {
    setBusyKey(key);
    setError('');
    setNotice('');
    try {
      await fn();
      setNotice(msg);
      await load();
    } catch (err) {
      setError(err.message || 'Could not complete that action.');
    } finally {
      setBusyKey(null);
    }
  };

  const countFor = (pid) =>
    (docsByProject[pid]?.length || 0) + accessReqs.filter((r) => r.project_id === pid).length;
  const totalPending = projects.reduce((n, p) => n + countFor(p.project_id), 0);

  const groupByTeam = (pid) => {
    const groups = new Map();
    const bucket = (tid, tname) => {
      const key = tid || 'none';
      if (!groups.has(key)) groups.set(key, { key, team_name: tname || 'Unassigned team', docs: [], requests: [] });
      return groups.get(key);
    };
    (docsByProject[pid] || []).forEach((d) => bucket(d.uploaded_as_team_id, d.team_name).docs.push(d));
    accessReqs.filter((r) => r.project_id === pid).forEach((r) => bucket(r.team_id, r.team_name).requests.push(r));
    return [...groups.values()].sort((a, b) => a.team_name.localeCompare(b.team_name));
  };

  if (loading) {
    return (
      <div className="flex justify-center py-8">
        <div className="w-6 h-6 border-4 border-primary/30 border-t-primary rounded-full animate-spin" />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {notice && <p className="text-emerald-400 text-sm">{notice}</p>}
      {error && <p className="text-red-400 text-sm">{error}</p>}

      {projects.length === 0 ? (
        <p className="text-gray-500 text-sm">You don&rsquo;t review approvals for any project.</p>
      ) : (
        <>
          <p className="text-sm text-gray-400">
            {totalPending} item{totalPending === 1 ? '' : 's'} awaiting review across {projects.length} project{projects.length === 1 ? '' : 's'}.
          </p>

          {projects.map((p) => {
            const count = countFor(p.project_id);
            const open = openProjectId === p.project_id;
            const teams = open ? groupByTeam(p.project_id) : [];
            return (
              <Card key={p.project_id}>
                <button
                  type="button"
                  onClick={() => setOpenProjectId(open ? '' : p.project_id)}
                  className="w-full flex items-center justify-between gap-3 text-left"
                >
                  <span className="font-semibold text-gray-100">{p.project_name}</span>
                  <span className="flex items-center gap-2">
                    <Badge variant={count ? 'warning' : 'inactive'}>{count} pending</Badge>
                    <ChevronDown size={16} className={`text-gray-400 transition-transform ${open ? 'rotate-180' : ''}`} />
                  </span>
                </button>

                {open && (
                  <div className="mt-4 space-y-4">
                    {count === 0 && <p className="text-sm text-gray-500">Nothing pending in this project.</p>}
                    {teams.map((team) => (
                      <div key={team.key} className="rounded-lg border border-border/70 bg-background/40 p-3">
                        <p className="text-sm font-semibold text-gray-200 mb-2">{team.team_name}</p>

                        {team.docs.length > 0 && (
                          <div className="space-y-2 mb-3">
                            <p className="text-xs uppercase tracking-wide text-gray-500">Documents awaiting approval</p>
                            {team.docs.map((d) => (
                              <div key={d.document_id} className="flex items-center justify-between gap-3 rounded-md border border-border bg-surface px-3 py-2">
                                <div className="min-w-0">
                                  <p className="text-sm text-gray-200 truncate">{d.filename}</p>
                                  <p className="text-xs text-gray-500">
                                    {d.stage}{d.sensitivity_level === 2 ? ' · confidential' : ''}
                                  </p>
                                </div>
                                <div className="flex gap-2 shrink-0">
                                  <Button size="sm" icon={ClipboardList} onClick={() => setReviewDoc(d)}>
                                    Review &amp; decide
                                  </Button>
                                </div>
                              </div>
                            ))}
                          </div>
                        )}

                        {team.requests.length > 0 && (
                          <div className="space-y-2">
                            <p className="text-xs uppercase tracking-wide text-gray-500">Confidential-access requests</p>
                            {team.requests.map((r) => {
                              const choice = grantChoices[r.request_id] || {};
                              const canApprove = r.scope !== 'stage' || (choice.tier && choice.duration);
                              return (
                                <div key={r.request_id} className="rounded-md border border-border bg-surface px-3 py-2 space-y-2">
                                  <div className="flex items-center justify-between gap-3">
                                    <div className="min-w-0">
                                      <p className="text-sm text-gray-200 truncate">
                                        {r.requester_email || r.user_id}
                                        {r.requester_role && <span className="text-gray-500"> ({r.requester_role})</span>}{' '}
                                        {r.scope === 'document' && <>requests access to <span className="font-medium">{r.target_name}</span></>}
                                        {r.scope === 'stage' && <>requests access to the <span className="font-medium">{r.target_name}</span> stage</>}
                                        {r.scope === 'team' && <>requests team-wide access</>}
                                      </p>
                                      <p className="text-xs text-gray-500">
                                        Requested {r.requested_at ? new Date(r.requested_at).toLocaleDateString() : '—'}
                                        {' · '}
                                        {r.tier ? `${r.tier} · ` : ''}{r.grant_duration_label}
                                      </p>
                                      {r.reason && (
                                        <p className="text-xs text-gray-400 mt-1 italic">&ldquo;{r.reason}&rdquo;</p>
                                      )}
                                    </div>
                                    <div className="flex gap-2 shrink-0">
                                      <Button size="sm" icon={Check}
                                        disabled={!canApprove}
                                        loading={busyKey === `req-approve-${r.request_id}`}
                                        onClick={() => act(
                                          `req-approve-${r.request_id}`,
                                          () => accessRequestsApi.approve(r.request_id, choice),
                                          'Access request approved.',
                                        )}
                                      >Approve</Button>
                                      <Button size="sm" variant="danger" icon={X}
                                        loading={busyKey === `req-deny-${r.request_id}`}
                                        onClick={() => act(`req-deny-${r.request_id}`, () => accessRequestsApi.deny(r.request_id), 'Access request denied.')}
                                      >Deny</Button>
                                    </div>
                                  </div>
                                  {r.scope === 'stage' && (
                                    <div className="flex gap-2">
                                      <Dropdown
                                        label="Tier"
                                        options={GRANT_TIER_OPTIONS}
                                        value={choice.tier || ''}
                                        onChange={(value) => setGrantChoices((cur) => ({ ...cur, [r.request_id]: { ...cur[r.request_id], tier: value } }))}
                                        placeholder="Choose a tier"
                                      />
                                      <Dropdown
                                        label="Duration"
                                        options={GRANT_DURATION_OPTIONS}
                                        value={choice.duration || ''}
                                        onChange={(value) => setGrantChoices((cur) => ({ ...cur, [r.request_id]: { ...cur[r.request_id], duration: value } }))}
                                        placeholder="Choose a duration"
                                      />
                                    </div>
                                  )}
                                </div>
                              );
                            })}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </Card>
            );
          })}

          {activeGrants.length > 0 && (
            <Card>
              <p className="font-semibold text-gray-100 mb-3">Active stage-access grants</p>
              <div className="space-y-2">
                {activeGrants.map((r) => (
                  <div key={r.request_id} className="flex items-center justify-between gap-3 rounded-md border border-border bg-surface px-3 py-2">
                    <div className="min-w-0">
                      <p className="text-sm text-gray-200 truncate">
                        {r.requester_email || r.user_id}
                        {r.requester_role && <span className="text-gray-500"> ({r.requester_role})</span>}{' '}
                        holds <span className="font-medium">{r.tier}</span> access to the <span className="font-medium">{r.target_name}</span> stage
                        <span className="text-gray-500"> · {r.team_name}</span>
                      </p>
                      <p className="text-xs text-gray-500">
                        Approved {r.decided_at ? new Date(r.decided_at).toLocaleDateString() : '—'}
                        {' · '}{r.grant_duration_label}
                      </p>
                    </div>
                    <Button size="sm" variant="danger" icon={X}
                      loading={busyKey === `req-revoke-${r.request_id}`}
                      onClick={() => {
                        if (!window.confirm('Revoke this access grant immediately? This cannot be undone.')) return;
                        act(`req-revoke-${r.request_id}`, () => accessRequestsApi.revoke(r.request_id), 'Access grant revoked.');
                      }}
                    >Revoke</Button>
                  </div>
                ))}
              </div>
            </Card>
          )}
        </>
      )}

      {reviewDoc && (
        <DocumentReviewModal
          document={reviewDoc}
          canOverrideScan={canOverrideScan}
          onApprove={async (override) => {
            await documentsApi.approve(reviewDoc.document_id, { override });
            setReviewDoc(null);
            setNotice('Document approved.');
            await load();
          }}
          onReject={async (reason) => {
            await documentsApi.reject(reviewDoc.document_id, reason);
            setReviewDoc(null);
            setNotice('Document rejected.');
            await load();
          }}
          onClose={() => setReviewDoc(null)}
        />
      )}
    </div>
  );
};

// Audit Log — flat list (format unchanged), restricted to org_admin /
// project_admin / team_lead. What's shown is scoped by role on the backend
// (org_admin: tenant-wide; project_admin: their project(s); team_lead: their
// team(s)). Only account/permission-management actions are captured — no LOGIN.
const AuditTab = () => {
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [denied, setDenied] = useState(false);

  useEffect(() => {
    adminApi.auditLog(200)
      .then(setEntries)
      .catch((err) => {
        if (err.status === 403) setDenied(true);
        else setError(err.message || 'Could not load the audit log.');
      })
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="flex justify-center py-8">
        <div className="w-6 h-6 border-4 border-primary/30 border-t-primary rounded-full animate-spin" />
      </div>
    );
  }

  if (denied) {
    return (
      <Card title="Access denied">
        <p className="text-sm text-gray-400">
          The audit log is available to organization admins, project admins and team leads only.
          Your role doesn&rsquo;t include audit-log access.
        </p>
      </Card>
    );
  }

  if (error) return <p className="text-red-400 text-sm">{error}</p>;

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-gray-500 border-b border-border">
            <th className="py-2 pr-4">Time</th>
            <th className="py-2 pr-4">Action</th>
            <th className="py-2 pr-4">Details</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border/50">
          {entries.map((e) => (
            <tr key={e.log_id} className="text-gray-300">
              <td className="py-2 pr-4 whitespace-nowrap text-gray-500 align-top">{new Date(e.timestamp).toLocaleString()}</td>
              <td className="py-2 pr-4 align-top"><Badge variant="neutral">{e.action}</Badge></td>
              <td className="py-2 pr-4 text-gray-400 align-top">
                {e.summary ? (
                  <>
                    <span className="text-gray-100 font-medium">{e.actor_name}</span>{' '}{e.summary}
                  </>
                ) : (e.details || '—')}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {entries.length === 0 && <p className="text-gray-500 text-sm py-8 text-center">No audit events in your scope yet.</p>}
    </div>
  );
};

export default AdminPage;
