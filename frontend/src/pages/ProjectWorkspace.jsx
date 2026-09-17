import React, { useEffect, useState, useCallback } from 'react';
import { useParams, Link, useSearchParams } from 'react-router-dom';
import { Layers } from 'lucide-react';
import BackButton from '../components/ui/BackButton';
import SourcePanel from '../components/sources/SourcePanel';
import ChatPanel from '../components/chat/ChatPanel';
import ChatModeTabs from '../components/chat/ChatModeTabs';
import Badge from '../components/ui/Badge';
import Modal from '../components/ui/Modal';
import Button from '../components/ui/Button';
import Input from '../components/ui/Input';
import { projectsApi, workspaceApi, accessRequestsApi, teamsApi } from '../lib/api';
import { useAuth } from '../context/AuthContext';

const ProjectWorkspace = () => {
  const { projectId } = useParams();
  const [searchParams] = useSearchParams();
  const urlTab = searchParams.get('tab');
  const urlQuery = searchParams.get('q');
  const highlightDocumentId = searchParams.get('highlight');
  const { user } = useAuth();
  const [project, setProject] = useState(null);
  const [documents, setDocuments] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  // Document-review session — set the moment "Upload Doc" succeeds, switches
  // the center ChatPanel into review mode (seeded with the auto-scan result,
  // not a blank drafting conversation). Cleared on "Back to drafting" or
  // uploading a different document.
  const [reviewSession, setReviewSession] = useState(null);

  // Chat panel mode — deterministic tab selection (Draft | Search). An active
  // reviewSession overrides this (upload -> review flow). 'rag' and 'query'
  // are accepted as legacy URL aliases for 'search' (old bookmarks/links).
  const CHAT_TABS = ['draft', 'search', 'rag', 'query'];
  const normalizeTab = (tab) => (tab === 'rag' || tab === 'query' ? 'search' : tab);
  const [chatTab, setChatTab] = useState(
    urlTab && CHAT_TABS.includes(urlTab) ? normalizeTab(urlTab) : 'draft'
  );

  useEffect(() => {
    if (urlTab && CHAT_TABS.includes(urlTab)) {
      setChatTab(normalizeTab(urlTab));
    }
  }, [urlTab]);


  // Confidential-access requests (viewer/contributor entry point)
  const [wsProject, setWsProject] = useState(null); // this project's teams + my per-team role
  const [myRequests, setMyRequests] = useState([]);
  const [accessOpen, setAccessOpen] = useState(false);
  const [reqBusy, setReqBusy] = useState(null); // team_id in flight
  const [reqNotice, setReqNotice] = useState('');
  const [reqError, setReqError] = useState('');

  // Teams modal — manage the teams that exist in this project (not user assignment)
  const [teamsOpen, setTeamsOpen] = useState(false);
  const [teams, setTeams] = useState([]);
  const [teamsLoading, setTeamsLoading] = useState(false);
  const [newTeamName, setNewTeamName] = useState('');
  const [creatingTeam, setCreatingTeam] = useState(false);
  const [teamError, setTeamError] = useState('');

  const load = useCallback(async ({ silent = false } = {}) => {
    if (!silent) setLoading(true);
    setError('');
    try {
      const [projects, docs, ws, mine] = await Promise.all([
        projectsApi.list(),
        // includeDrafts is server-gated to org_admin/project_admin — a
        // contributor passing it still only gets their own drafts back.
        projectsApi.documents(projectId, { includeDrafts: true }),
        workspaceApi.get().catch(() => null),
        accessRequestsApi.mine().catch(() => []),
      ]);
      setProject(projects.find((p) => p.project_id === projectId) || { project_id: projectId, project_name: projectId });
      setDocuments(docs);
      setWsProject(ws?.projects?.find((p) => p.project_id === projectId) || null);
      setMyRequests(Array.isArray(mine) ? mine : []);
    } catch (err) {
      setError(err.message || 'Could not load this project.');
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    load();
  }, [load]);

  // Scroll a document into view when opened via ?highlight=<document_id>
  // (e.g. from Intelligence's Recent Project Activity — see
  // UI_FIXES_2026-09-15.md #19). StageSection auto-expands the stage that
  // contains it; this just brings it into the viewport once rendered.
  //
  // BUGFIXES_2026-09-15.md: StageSection's own expand happens in a CHILD
  // effect + state update, which lands in a LATER commit than this effect
  // (same trigger, but a child's setState-in-effect always schedules an
  // extra render). Reading the DOM here on the very first pass can catch
  // the section still collapsed (present in the DOM via CSS grid-rows-[0fr]
  // opacity-0, so scrollIntoView "succeeds" against a zero-height target —
  // the page silently jumps to an empty-looking area). Poll across a
  // handful of frames instead of a single attempt, so this effect's own
  // scroll waits for the child's expand to actually commit.
  useEffect(() => {
    if (!highlightDocumentId || loading) return undefined;
    let cancelled = false;
    let attempts = 0;
    const tryScroll = () => {
      if (cancelled) return;
      const el = document.getElementById(`document-${highlightDocumentId}`);
      // StageSection's collapsed state uses grid-rows-[0fr] + opacity-0, not
      // display:none — the element stays in the DOM and offsetParent stays
      // non-null even collapsed, so check its actual rendered height instead.
      if (el && el.getBoundingClientRect().height > 0) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        return;
      }
      attempts += 1;
      if (attempts < 10) requestAnimationFrame(tryScroll);
    };
    requestAnimationFrame(tryScroll);
    return () => { cancelled = true; };
  }, [highlightDocumentId, loading, documents]);

  // Teams in this project where I'm viewer/contributor — i.e. not auto-cleared for confidential docs.
  const requestableTeams = (wsProject?.teams || []).filter(
    (t) => t.role === 'viewer' || t.role === 'contributor',
  );
  const requestStatus = (teamId) => {
    const r = myRequests.find((x) => x.team_id === teamId);
    if (!r) return { state: 'none' };
    if (r.active) return { state: 'granted', request: r };
    if (r.status === 'pending') return { state: 'pending', request: r };
    return { state: r.status === 'denied' ? 'denied' : 'expired', request: r };
  };

  const handleRequestAccess = async (teamId) => {
    setReqBusy(teamId);
    setReqNotice('');
    setReqError('');
    try {
      await accessRequestsApi.create(teamId);
      setReqNotice('Request sent — a team lead will review it.');
      setMyRequests(await accessRequestsApi.mine());
    } catch (err) {
      setReqError(err.message || 'Could not send the request.');
    } finally {
      setReqBusy(null);
    }
  };

  const role = user?.is_org_admin ? 'org_admin' : user?.project_roles?.[projectId];
  // Our model: team_lead+ or project_admin can review (the backend still checks
  // the document's *specific* team and returns a clear 403 otherwise).
  const canReview = ['org_admin', 'project_admin', 'team_lead'].includes(role);
  // Only org_admin/project_admin may force through a document that failed
  // the scan (approve_document() re-checks this server-side regardless).
  const canOverrideScan = ['org_admin', 'project_admin'].includes(role);
  const canManageStages = ['org_admin', 'project_admin'].includes(role) || Boolean(user?.is_org_admin);
  const canManageTeams = ['org_admin', 'project_admin'].includes(role);

  const teamNames = (wsProject?.teams || []).map((t) => t.name);

  const openTeams = async () => {
    setTeamsOpen(true);
    setTeamError('');
    setNewTeamName('');
    setTeamsLoading(true);
    try {
      setTeams(await teamsApi.list(projectId));
    } catch (err) {
      setTeamError(err.message || 'Could not load teams.');
    } finally {
      setTeamsLoading(false);
    }
  };

  const handleCreateTeam = async () => {
    const name = newTeamName.trim();
    if (!name) return;
    setCreatingTeam(true);
    setTeamError('');
    try {
      await teamsApi.create(projectId, name);
      setNewTeamName('');
      setTeams(await teamsApi.list(projectId));
      await load({ silent: true }); // refresh the toolbar label + workspace teams
    } catch (err) {
      setTeamError(err.message || 'Could not create the team.');
    } finally {
      setCreatingTeam(false);
    }
  };

  // "Upload Doc" succeeded (SourcePanel) — switch the chat panel into review
  // mode, seeded with the upload response's auto-scan result.
  const handleDocumentUploaded = (result) => {
    setReviewSession({
      documentId: result.document_id,
      sessionId: result.session_id,
      stageName: result.stage_name,
      originalFilename: result.originalFilename,
      initialReply: result.reply,
      scan: result.scan,
      scanError: result.scan_error,
      scanSkipped: result.scan_skipped,
      reformedContent: result.reformed_content,
      injectionFlagged: result.injection_flagged,
      injectionFindings: result.injection_findings,
      failedCriteria: result.failed_criteria,
    });
  };

  // A review turn finalized (new DocumentVersion written) — refresh the
  // Sources panel so document counts / any status the UI shows stay current.
  const handleReviewFinalized = () => {
    load({ silent: true });
  };

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="w-8 h-8 border-4 border-primary/30 border-t-primary rounded-full animate-spin" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center p-8 text-center">
        <h2 className="text-2xl font-bold text-gray-200 mb-2">Couldn't open this project</h2>
        <p className="text-gray-500 mb-4">{error}</p>
        <BackButton fallbackTo="/" label="Return to Projects" />
      </div>
    );
  }

  return (
    <div className="flex flex-col h-[calc(100vh-3.5rem)] overflow-hidden">
      {/* Workspace Header — stays fixed; the columns below scroll independently */}
      <div className="h-14 border-b border-border bg-background px-6 flex items-center gap-4 shrink-0 overflow-x-auto">
        <BackButton fallbackTo="/" />
        <div className="w-px h-4 bg-border"></div>
        <h1 className="font-semibold text-gray-100">{project.project_name}</h1>
        <button
          type="button"
          onClick={openTeams}
          title="Teams in this project"
          className="inline-flex items-center gap-1.5 rounded-full border border-border bg-surface px-2.5 py-1 text-xs font-medium text-gray-300 hover:border-primary/50 hover:text-gray-100 transition-colors shrink-0"
        >
          Teams
          <span className="text-gray-500 max-w-[220px] truncate">
            {teamNames.length ? teamNames.join(', ') : '—'}
          </span>
        </button>

        <div
          title="Pipeline stages in this project"
          className="inline-flex items-center gap-1.5 rounded-full border border-border bg-surface px-2.5 py-1 text-xs font-medium text-gray-300 shrink-0"
        >
          <Layers size={13} className="text-primary" />
          Stages
          <span className="text-gray-500 max-w-[260px] truncate">
            {(wsProject?.stages || []).map((s) => s.name).join(' → ') || '—'}
          </span>
        </div>

        {requestableTeams.length > 0 && (
          <button
            type="button"
            onClick={() => { setReqNotice(''); setReqError(''); setAccessOpen(true); }}
            title="Request access to confidential documents shared with a team"
            className="inline-flex items-center gap-1.5 rounded-full border border-border bg-surface px-2.5 py-1 text-xs font-medium text-gray-300 hover:border-primary/50 hover:text-gray-100 transition-colors shrink-0"
          >
            Team access
          </button>
        )}

        <div className="flex-1" />
      </div>

      {/* Two-column layout (Sources + Chat). Each column scrolls inside its
          own fixed-height pane; below lg the row scrolls as one stack. */}
      <div className="relative flex-1 min-h-0 flex flex-col lg:flex-row overflow-y-auto lg:overflow-hidden">
        <div className="w-full lg:w-[32%] shrink-0 lg:h-full lg:min-h-0 lg:overflow-y-auto scrollbar-thin">
          <SourcePanel
            documents={documents}
            canReview={canReview}
            canOverrideScan={canOverrideScan}
            canDelete={user?.is_org_admin}
            canEditAny={canReview}
            currentUserId={user?.user_id}
            onChanged={load}
            projectId={projectId}
            stages={wsProject?.stages || []}
            teams={wsProject?.teams || []}
            canManageStages={canManageStages}
            onStagesChanged={() => load({ silent: true })}
            onDocumentUploaded={handleDocumentUploaded}
            highlightDocumentId={highlightDocumentId}
          />
        </div>
        {/* Center pane: header + scrolling message area + input pinned to the
            bottom. The column is height-boxed so ChatPanel manages its own
            internal scroll and the input never scrolls away. Switches into
            document-review mode the moment an upload succeeds. */}
        <div className="w-full lg:flex-1 shrink-0 flex flex-col h-[70vh] lg:h-full lg:min-h-0 overflow-hidden border-t border-border lg:border-t-0 lg:border-l">
          {!reviewSession && <ChatModeTabs active={chatTab} onChange={setChatTab} />}
          <ChatPanel
            key={reviewSession ? 'review' : `${chatTab}-${urlQuery || ''}`}
            projectId={projectId}
            mode={reviewSession ? 'review' : chatTab}
            reviewSession={reviewSession}
            initialQuery={urlQuery}
            onReviewFinalized={handleReviewFinalized}
            onReviewExit={() => setReviewSession(null)}
          />
        </div>

      </div>

      <Modal
        open={teamsOpen}
        onClose={() => setTeamsOpen(false)}
        title="Teams"
        description="The teams that exist in this project. (To add people to a team, use Assign Roles.)"
      >
        <div className="space-y-4">
          {teamError && <p className="text-sm text-red-400">{teamError}</p>}

          {teamsLoading ? (
            <p className="text-sm text-gray-500">Loading teams…</p>
          ) : (
            <div className="space-y-2">
              {teams.length === 0 && <p className="text-sm text-gray-500">This project has no teams yet.</p>}
              {teams.map((t) => (
                <div key={t.team_id} className="flex items-center justify-between rounded-lg border border-border bg-background px-3 py-2.5">
                  <span className="text-sm text-gray-200">{t.name}</span>
                  <span className="text-xs text-gray-500">
                    {t.member_count} member{t.member_count === 1 ? '' : 's'}
                  </span>
                </div>
              ))}
            </div>
          )}

          {canManageTeams && (
            <form
              onSubmit={(e) => { e.preventDefault(); handleCreateTeam(); }}
              className="border-t border-border/60 pt-4 space-y-2"
            >
              <p className="text-xs font-semibold uppercase tracking-wider text-gray-500">Create new team</p>
              <div className="flex items-end gap-2">
                <Input
                  label="Team name"
                  value={newTeamName}
                  onChange={(e) => setNewTeamName(e.target.value)}
                  placeholder="e.g. QA"
                />
                <Button type="submit" loading={creatingTeam} disabled={!newTeamName.trim()}>
                  Create team
                </Button>
              </div>
              <p className="text-xs text-gray-500">
                New teams are immediately available in the Assign Roles team dropdown.
              </p>
            </form>
          )}
        </div>
      </Modal>

      <Modal
        open={accessOpen}
        onClose={() => setAccessOpen(false)}
        title="Team access"
        description="Request access to every confidential document shared with a team — the broadest option. For a single document or one stage, use the Request access action on that document or stage instead."
      >
        <div className="space-y-3">
          {reqNotice && <p className="text-sm text-emerald-400">{reqNotice}</p>}
          {reqError && <p className="text-sm text-red-400">{reqError}</p>}
          {requestableTeams.map((t) => {
            const { state } = requestStatus(t.team_id);
            return (
              <div key={t.team_id} className="flex items-center justify-between gap-3 rounded-lg border border-border bg-background px-3 py-2.5">
                <div className="min-w-0">
                  <p className="text-sm text-gray-200 truncate">{t.name}</p>
                  <p className="text-xs text-gray-500">Your role: {t.role}</p>
                </div>
                {state === 'granted' && <Badge variant="success">Access granted</Badge>}
                {state === 'pending' && <Badge variant="warning">Pending review</Badge>}
                {(state === 'none' || state === 'denied' || state === 'expired') && (
                  <div className="flex items-center gap-2">
                    {state === 'denied' && <span className="text-xs text-gray-500">Previously denied</span>}
                    {state === 'expired' && <span className="text-xs text-gray-500">Grant expired</span>}
                    <Button
                      size="sm"
                      onClick={() => handleRequestAccess(t.team_id)}
                      loading={reqBusy === t.team_id}
                    >
                      {state === 'none' ? 'Request access' : 'Request again'}
                    </Button>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </Modal>
    </div>
  );
};

export default ProjectWorkspace;
