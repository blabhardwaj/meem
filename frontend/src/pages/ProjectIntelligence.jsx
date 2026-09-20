import React, { useEffect, useState, useCallback } from 'react';
import { useParams, Link } from 'react-router-dom';
import {
  RotateCw,
  ShieldAlert,
  AlertTriangle,
  CheckCircle2,
  Layers,
  ChevronRight,
  ChevronDown,
  TrendingUp,
  Clock,
  Search,
  Settings2,
  ShieldCheck,
  Link2,
  X,
} from 'lucide-react';
import { intelligenceApi, projectsApi, teamsApi, stagesApi } from '../lib/api';
import Badge from '../components/ui/Badge';
import Button from '../components/ui/Button';
import Input from '../components/ui/Input';
import Modal from '../components/ui/Modal';
import AuditFindingDrawer from '../components/intelligence/AuditFindingDrawer';
import BackButton from '../components/ui/BackButton';
import ChatPanel from '../components/chat/ChatPanel';
import { useAuth } from '../context/AuthContext';

const ProjectIntelligence = () => {
  const { projectId } = useParams();
  const { user } = useAuth();
  // Matches ProjectWorkspace's canManageStages exactly — team_lead can review
  // documents but not restructure a project's stages.
  const role = user?.is_org_admin ? 'org_admin' : user?.project_roles?.[projectId];
  const canManageStages = ['org_admin', 'project_admin'].includes(role) || Boolean(user?.is_org_admin);
  // UI_FIXES_2026-09-15.md #19: Progress History / Recent Activity are
  // project-admin+ only — same population as canManageStages, kept as its
  // own name since the two happen to coincide today but answer different
  // questions.
  const canViewProgressActivity = canManageStages;

  // State
  const [project, setProject] = useState(null);
  const [documents, setDocuments] = useState([]);
  const [metrics, setMetrics] = useState(null);
  const [stages, setStages] = useState([]);
  const [findings, setFindings] = useState([]);
  const [history, setHistory] = useState([]);
  const [timeline, setTimeline] = useState([]);
  const [loading, setLoading] = useState(true);
  const [auditRunning, setAuditRunning] = useState(false);
  const [auditPending, setAuditPending] = useState(false);
  const [error, setError] = useState('');

  // Completion (Layer 4, AUDIT_RULE_TAXONOMY_MATRIX.md): a persisted human
  // decision, project-level only -- no stage equivalent.
  const [completion, setCompletion] = useState({ is_complete: false, completed_at: null, completed_by: null });
  const [completionBusy, setCompletionBusy] = useState(false);
  const [completionErr, setCompletionErr] = useState('');

  // Teams modal
  const [teamsOpen, setTeamsOpen] = useState(false);
  const [teams, setTeams] = useState([]);
  const [teamsLoading, setTeamsLoading] = useState(false);

  // Stage editing modal state in Intelligence
  const [editingStage, setEditingStage] = useState(null);
  const [stageEditName, setStageEditName] = useState('');
  const [stageEditApproval, setStageEditApproval] = useState(false);
  const [stageEditRefs, setStageEditRefs] = useState([]);
  const [stageBusy, setStageBusy] = useState(false);
  const [stageNotice, setStageNotice] = useState('');
  const [stageErr, setStageErr] = useState('');

  // Filters & display toggles
  const [selectedStageId, setSelectedStageId] = useState(null);
  const [severityFilter, setSeverityFilter] = useState('ALL');
  const [statusFilter, setStatusFilter] = useState('active');
  const [blockerOnly, setBlockerOnly] = useState(false);
  const [ruleCodeFilter, setRuleCodeFilter] = useState('ALL');
  const [searchQuery, setSearchQuery] = useState('');
  const [showAllFindings, setShowAllFindings] = useState(false);
  // Which grouped finding card (see groupKey/groupedFindings below) has its
  // per-occurrence list expanded.
  const [expandedGroupIdx, setExpandedGroupIdx] = useState(null);

  // Selected finding drawer
  const [selectedFinding, setSelectedFinding] = useState(null);

  // Search agent slide-over — UI_FIXES_2026-09-15.md #18: opens in place
  // instead of navigating away to the Workspace tab. `searchPanelQuery` is
  // bumped to a fresh string each open so ChatPanel's initialQuery
  // auto-send fires even if the panel is reopened with the same question.
  //
  // `searchPanelPrefill` is a SEPARATE, non-sending channel: a suggested
  // query the user picks should fire immediately (that's the point of
  // suggesting it), but "Ask a custom question" must only open the panel
  // with an empty, focused input for the user's OWN words — auto-sending a
  // canned string there would silently ignore what the button says it does.
  const [searchPanelOpen, setSearchPanelOpen] = useState(false);
  const [searchPanelQuery, setSearchPanelQuery] = useState(null);
  const [searchPanelPrefill, setSearchPanelPrefill] = useState(null);
  const openSearchPanel = (query) => {
    setSearchPanelQuery(query || null);
    setSearchPanelPrefill(null);
    setSearchPanelOpen(true);
  };
  const openSearchPanelForCustomQuestion = (prefill = null) => {
    setSearchPanelQuery(null);
    setSearchPanelPrefill(prefill || null);
    setSearchPanelOpen(true);
  };

  const loadData = useCallback(
    async ({ silent = false } = {}) => {
      if (!silent) setLoading(true);
      setError('');
      try {
        const [projects, docs, metricsData, findingsData, historyData, timelineData] =
          await Promise.all([
            projectsApi.list().catch(() => []),
            projectsApi.documents(projectId).catch(() => []),
            intelligenceApi.metrics(projectId),
            intelligenceApi.findings(projectId),
            intelligenceApi.progressHistory(projectId, 50).catch(() => ({ history: [] })),
            intelligenceApi.timeline(projectId, 50).catch(() => ({ timeline: [] })),
          ]);

        const currentProj =
          projects.find((p) => p.project_id === projectId) || {
            project_id: projectId,
            project_name: 'Holiday Checkout Modernization',
          };

        setProject(currentProj);
        setDocuments(docs || []);
        setMetrics(metricsData?.project_metric || null);
        setStages(metricsData?.stages || []);
        setFindings(findingsData?.findings || []);
        setHistory(historyData?.history || []);
        setTimeline(timelineData?.timeline || []);
        setAuditPending(Boolean(metricsData?.audit_pending));
        setCompletion(
          metricsData?.completion || { is_complete: false, completed_at: null, completed_by: null }
        );
      } catch (err) {
        setError(err.message || 'Could not load project intelligence data.');
      } finally {
        setLoading(false);
      }
    },
    [projectId]
  );

  useEffect(() => {
    loadData();
  }, [loadData]);

  // Master Plan v2, item 5: /metrics now returns audit_pending=true instead
  // of blocking while the first audit runs in the background. Poll until it
  // lands rather than making the user manually refresh.
  useEffect(() => {
    if (!auditPending) return undefined;
    const timer = setTimeout(() => loadData({ silent: true }), 4000);
    return () => clearTimeout(timer);
  }, [auditPending, loadData]);

  const handleTriggerAudit = async () => {
    setAuditRunning(true);
    try {
      await intelligenceApi.triggerAudit(projectId);
      await loadData({ silent: true });
    } catch (err) {
      alert(`Audit run failed: ${err.message}`);
    } finally {
      setAuditRunning(false);
    }
  };

  const handleMarkComplete = async () => {
    setCompletionErr('');
    setCompletionBusy(true);
    try {
      const result = await intelligenceApi.markComplete(projectId);
      setCompletion(result);
    } catch (err) {
      setCompletionErr(err.message || 'Could not mark project complete.');
    } finally {
      setCompletionBusy(false);
    }
  };

  const handleReopenProject = async () => {
    setCompletionErr('');
    setCompletionBusy(true);
    try {
      const result = await intelligenceApi.reopen(projectId);
      setCompletion(result);
    } catch (err) {
      setCompletionErr(err.message || 'Could not reopen project.');
    } finally {
      setCompletionBusy(false);
    }
  };

  const openTeams = async () => {
    setTeamsOpen(true);
    setTeamsLoading(true);
    try {
      setTeams(await teamsApi.list(projectId));
    } catch {
      setTeams([]);
    } finally {
      setTeamsLoading(false);
    }
  };

  const openStageModal = (stg) => {
    setEditingStage(stg);
    setStageEditName(stg.stage_name || stg.name || '');
    setStageEditApproval(Boolean(stg.requires_approval));
    setStageEditRefs(stg.references || []);
    setStageNotice('');
    setStageErr('');
  };

  const handleSaveStage = async () => {
    if (!editingStage) return;
    setStageBusy(true);
    setStageErr('');
    setStageNotice('');
    try {
      await stagesApi.update(projectId, editingStage.stage_id, {
        name: stageEditName.trim(),
        requires_approval: stageEditApproval,
      });
      if (stageEditRefs) {
        await stagesApi.setReferences(projectId, editingStage.stage_id, stageEditRefs);
      }
      setStageNotice('Stage settings saved.');
      await loadData({ silent: true });
    } catch (err) {
      setStageErr(err.message || 'Could not update stage settings.');
    } finally {
      setStageBusy(false);
    }
  };

  const toggleStageRef = (targetId) => {
    setStageEditRefs((prev) =>
      prev.includes(targetId) ? prev.filter((id) => id !== targetId) : [...prev, targetId]
    );
  };

  if (loading) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center min-h-[70vh]">
        <div className="w-9 h-9 border-4 border-primary/30 border-t-primary rounded-full animate-spin mb-3" />
        <p className="text-sm text-gray-400">Loading project intelligence metrics…</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center p-8 text-center min-h-[70vh]">
        <AlertTriangle className="text-amber-500 mb-3" size={36} />
        <h2 className="text-2xl font-bold text-gray-200 mb-2">Intelligence Unavailable</h2>
        <p className="text-gray-400 max-w-md mb-6">{error}</p>
        <div className="flex items-center gap-3">
          <Button onClick={() => loadData()}>Retry</Button>
          <Link to={`/projects/${encodeURIComponent(projectId)}`} className="text-primary hover:underline text-sm">
            Return to Workspace
          </Link>
        </div>
      </div>
    );
  }

  if (auditPending && !metrics) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center min-h-[70vh] gap-3 p-8 text-center">
        <div className="w-9 h-9 border-4 border-primary/30 border-t-primary rounded-full animate-spin" />
        <p className="text-sm text-gray-300">First audit is running in the background…</p>
        <p className="text-xs text-gray-500 max-w-sm">
          This syncs the project's knowledge graph and evaluates every audit rule against it —
          usually a few seconds for a new project.
        </p>
        <Button variant="ghost" onClick={() => loadData({ silent: true })} className="text-xs">
          Check now
        </Button>
      </div>
    );
  }

  const isReady = metrics?.readiness_status === 'READY';
  // Coverage sentinel: -1 means "no mandatory requirements configured to
  // measure against" (Decision B, AUDIT_RULE_TAXONOMY_MATRIX.md) -- NOT 0%
  // and NOT 100%. Never render a percentage for it.
  const rawCompleteness = metrics?.completeness_score;
  const coverageConfigured = rawCompleteness !== undefined && rawCompleteness !== null && rawCompleteness >= 0;
  const completenessPct = coverageConfigured ? Math.round(rawCompleteness * 10) / 10 : null;
  const rawReqCoverage = metrics?.mandatory_requirement_coverage;
  const reqCoverageConfigured = rawReqCoverage !== undefined && rawReqCoverage !== null && rawReqCoverage >= 0;

  // Master Plan v2, item 8: real per-project data in real stage order —
  // replaces the previous hardcoded Northstar-demo strings ("Stage 4 of 5",
  // "Discovery, UX, and Engineering...", fixed 100% health cells, and
  // stage-name string matching for pipeline status).
  const orderedStages = [...stages].sort((a, b) => {
    const ao = a.order_index ?? Infinity;
    const bo = b.order_index ?? Infinity;
    return ao - bo;
  });
  const blockerFindings = findings.filter((f) => f.is_blocker);
  const gatedStageIndex = orderedStages.findIndex((s) =>
    blockerFindings.some((f) => f.target_stage_id === s.stage_id)
  );
  const gatedStage = gatedStageIndex >= 0 ? orderedStages[gatedStageIndex] : null;
  const stageProgressLabel = orderedStages.length
    ? gatedStage
      ? `Stage ${gatedStageIndex + 1} of ${orderedStages.length}`
      : `${orderedStages.length} stage${orderedStages.length !== 1 ? 's' : ''}`
    : null;

  const pendingApprovalCount = findings.filter((f) => f.rule_code === 'R008').length;
  const contradictionCount = findings.filter((f) => f.rule_code === 'R007').length;
  const dependencyIssueCount = findings.filter((f) => ['R003', 'R005'].includes(f.rule_code)).length;

  const readinessDescription = isReady
    ? (coverageConfigured
        ? 'All mandatory requirements have been specified, approved, and verified across all project stages.'
        : 'No blocking audit findings. Note: no mandatory requirements are configured yet, so coverage is not being measured.')
    : [
        blockerFindings.length > 0 &&
          `${blockerFindings.length} blocker${blockerFindings.length !== 1 ? 's' : ''} require attention${gatedStage ? ` at ${gatedStage.stage_name}` : ''}.`,
        pendingApprovalCount > 0 &&
          `${pendingApprovalCount} document${pendingApprovalCount !== 1 ? 's' : ''} awaiting approval.`,
        contradictionCount > 0 &&
          `${contradictionCount} claim conflict${contradictionCount !== 1 ? 's' : ''} detected.`,
      ].filter(Boolean).join(' ') || 'Review the findings below for details.';

  // Filtered findings
  const allFiltered = findings.filter((f) => {
    if (statusFilter === 'active' && f.is_dismissed) return false;
    if (statusFilter === 'dismissed' && !f.is_dismissed) return false;
    if (selectedStageId && f.target_stage_id !== selectedStageId) return false;
    if (severityFilter !== 'ALL' && f.severity?.toUpperCase() !== severityFilter) return false;
    if (blockerOnly && !f.is_blocker) return false;
    if (ruleCodeFilter !== 'ALL' && f.rule_code?.toUpperCase() !== ruleCodeFilter) return false;
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      const matchTitle = f.title?.toLowerCase().includes(q);
      const matchDesc = f.description?.toLowerCase().includes(q);
      const matchRule = f.rule_code?.toLowerCase().includes(q);
      if (!matchTitle && !matchDesc && !matchRule) return false;
    }
    return true;
  });

  // Group findings that are the same underlying issue reported once per
  // stage/document-pair into one card (UI_FIXES_2026-09-15.md #17) — e.g.
  // three R007 contradictions against the same downstream claim, one per
  // upstream stage, are one real-world problem, not three. The grouping key
  // deliberately does NOT include target_stage_id or the specific document
  // pair; it groups by what makes two findings read as "the same thing" to a
  // person: same rule + same claim/target being contradicted (R007), or same
  // rule + same title/description text (everything else — e.g. the same
  // stage-reference violation flagged from two directions).
  const groupKey = (f) => {
    if (f.rule_code === 'R007' && f.details?.subject && f.details?.predicate) {
      const a = f.details.value_a ?? '';
      const b = f.details.value_b ?? '';
      return `R007::${f.details.subject.toLowerCase()}::${f.details.predicate.toLowerCase()}::${[a, b].sort().join('|')}`;
    }
    return `${f.rule_code}::${f.title}::${f.description}`;
  };

  const groupedFindingsMap = new Map();
  for (const f of allFiltered) {
    const key = groupKey(f);
    if (!groupedFindingsMap.has(key)) groupedFindingsMap.set(key, []);
    groupedFindingsMap.get(key).push(f);
  }
  // Each group's representative is its most-severe / first-seen member;
  // occurrences carries every original finding so nothing is discarded —
  // AuditFindingDrawer can still show each one on expand.
  const groupedFindings = Array.from(groupedFindingsMap.values()).map((occurrences) => ({
    ...occurrences[0],
    occurrences,
  }));

  // Prioritize findings:
  // 1. Pending approval (R002, R008)
  // 2. Material requirement contradiction (R007)
  // 3. Others
  const sortedFindings = [...groupedFindings].sort((a, b) => {
    const priority = (f) => {
      if (f.rule_code === 'R002' || f.rule_code === 'R008') return 1;
      if (f.rule_code === 'R007') return 2;
      return 3;
    };
    return priority(a) - priority(b);
  });

  const topFindings = showAllFindings ? sortedFindings : sortedFindings.slice(0, 3);
  const remainingCount = Math.max(0, sortedFindings.length - 3);

  const availableRuleCodes = Array.from(
    new Set(findings.map((f) => f.rule_code?.toUpperCase()).filter(Boolean))
  );

  // Filter out internal AUDIT_RUN events for the Activity section
  const activityEvents = timeline.filter((t) => t.event_type !== 'AUDIT_RUN');

  return (
    <div className="flex flex-col min-h-[calc(100vh-3.5rem)] bg-background">
      {/* Workspace & Intelligence Header */}
      <div className="h-14 border-b border-border bg-background px-6 flex items-center gap-4 shrink-0 overflow-x-auto">
        <BackButton fallbackTo={`/projects/${encodeURIComponent(projectId)}`} className="shrink-0" />
        <div className="w-px h-4 bg-border shrink-0" />
        <h1 className="font-semibold text-gray-100 truncate max-w-[240px]">
          {project?.project_name || 'Project'}
        </h1>

        <button
          type="button"
          onClick={openTeams}
          title="Teams in this project"
          className="inline-flex items-center gap-1.5 rounded-full border border-border bg-surface px-2.5 py-1 text-xs font-medium text-gray-300 hover:border-primary/50 hover:text-gray-100 transition-colors shrink-0"
        >
          Teams
        </button>


        <div className="flex-1" />

        {/* Action Buttons */}
        <div className="flex items-center gap-2.5 shrink-0">
          <Button
            variant="secondary"
            size="sm"
            icon={RotateCw}
            loading={auditRunning}
            onClick={handleTriggerAudit}
            title="Re-run project audit"
          >
            Run Audit
          </Button>
          {canManageStages && (
            completion.is_complete ? (
              <Button
                variant="secondary"
                size="sm"
                icon={X}
                loading={completionBusy}
                onClick={handleReopenProject}
                title="Clear the completion mark"
              >
                Reopen
              </Button>
            ) : (
              <Button
                variant="secondary"
                size="sm"
                icon={CheckCircle2}
                loading={completionBusy}
                onClick={handleMarkComplete}
                title="Record that this project is done. A human decision, independent of the readiness gate."
              >
                Mark Complete
              </Button>
            )
          )}
          <Button
            variant="primary"
            size="sm"
            icon={Search}
            onClick={() => openSearchPanel(null)}
          >
            Ask Search Agent
          </Button>
        </div>
      </div>
      {completionErr && (
        <div className="max-w-7xl w-full mx-auto px-6 -mt-2">
          <p className="text-xs text-red-400">{completionErr}</p>
        </div>
      )}

      {/* Main Content Area */}
      <div className="flex-1 max-w-7xl w-full mx-auto p-6 space-y-6">
        {/* 1. PROJECT STATUS (Calm, Informative Hierarchy) */}
        <div className="rounded-xl border border-border bg-surface p-5 space-y-4">
          <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-4">
            <div className="space-y-1.5 max-w-2xl">
              <div className="flex items-center gap-2.5">
                <span className="text-xs font-semibold uppercase tracking-wider text-gray-400">
                  Project Readiness & Gate Status
                </span>
                {isReady ? (
                  <Badge variant="success">READY FOR LAUNCH</Badge>
                ) : (
                  <Badge variant="warning">
                    {gatedStage ? `GATED AT ${gatedStage.stage_name.toUpperCase()}` : 'ACTION REQUIRED'}
                  </Badge>
                )}
                {completion.is_complete && (
                  <Badge variant="success">COMPLETE</Badge>
                )}
                {stageProgressLabel && (
                  <span className="text-xs text-gray-500 font-mono">{stageProgressLabel}</span>
                )}
              </div>
              {completion.is_complete && (
                <p className="text-[11px] text-emerald-400">
                  Marked complete
                  {completion.completed_at ? ` ${new Date(completion.completed_at).toLocaleString()}` : ''}.
                  Reopens automatically if coverage or audit findings change.
                </p>
              )}
              <h2 className="text-xl font-bold text-gray-100 tracking-tight">
                {isReady
                  ? 'All Stage Gates Cleared'
                  : 'Action Required Before Launch Clearance'}
              </h2>
              <p className="text-xs text-gray-300 leading-relaxed">
                {readinessDescription}
              </p>
            </div>

            {/* Key Progress Stat */}
            <div className="flex items-center gap-4 shrink-0 bg-background/60 border border-border/80 rounded-xl p-4">
              <div className="space-y-1">
                <div className="flex items-center justify-between gap-4 text-xs">
                  <span className="text-gray-400 font-medium">Deliverables Completeness</span>
                  <span className="font-bold text-gray-100 font-mono">
                    {coverageConfigured ? `${completenessPct}%` : 'Not configured'}
                  </span>
                </div>
                <div className="h-2 w-48 rounded-full bg-surface overflow-hidden">
                  {coverageConfigured && (
                    <div
                      className="h-full rounded-full bg-emerald-500 transition-all duration-500"
                      style={{ width: `${Math.min(100, Math.max(0, completenessPct))}%` }}
                    />
                  )}
                </div>
                <span className="text-[11px] text-gray-500 block">
                  {documents.length} project document{documents.length !== 1 ? 's' : ''} authored &amp; indexed
                </span>
              </div>
            </div>
          </div>

          {/* Secondary Supporting Health Dimensions (Compact Horizontal Bar) */}
          <div className="pt-3 border-t border-border/60">
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2 text-xs">
              <div className="rounded-lg bg-background/50 border border-border/60 p-2.5">
                <span className="text-[11px] text-gray-400 block">Requirement Coverage</span>
                <span className={`font-semibold mt-0.5 block ${!reqCoverageConfigured ? 'text-gray-400' : rawReqCoverage >= 1.0 ? 'text-emerald-400' : 'text-amber-400'}`}>
                  {reqCoverageConfigured ? `${Math.round(rawReqCoverage * 100)}% Verified` : 'Not configured'}
                </span>
              </div>
              <div className="rounded-lg bg-background/50 border border-border/60 p-2.5">
                <span className="text-[11px] text-gray-400 block">Approval Health</span>
                <span className={`font-semibold mt-0.5 block ${(metrics?.approval_health ?? 1.0) >= 1.0 ? 'text-emerald-400' : 'text-amber-400'}`}>
                  {Math.round((metrics?.approval_health ?? 1.0) * 100)}%
                  {pendingApprovalCount > 0 ? ` (${pendingApprovalCount} Pending)` : ''}
                </span>
              </div>
              <div className="rounded-lg bg-background/50 border border-border/60 p-2.5">
                <span className="text-[11px] text-gray-400 block">Dependency Lineage</span>
                <span className={`font-semibold mt-0.5 block ${dependencyIssueCount === 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                  {dependencyIssueCount === 0 ? 'Consistent' : `${dependencyIssueCount} Issue${dependencyIssueCount !== 1 ? 's' : ''}`}
                </span>
              </div>
              <div className="rounded-lg bg-background/50 border border-border/60 p-2.5">
                <span className="text-[11px] text-gray-400 block">Claim Consistency</span>
                <span className={`font-semibold mt-0.5 block ${contradictionCount === 0 ? 'text-emerald-400' : 'text-amber-400'}`}>
                  {contradictionCount === 0 ? 'No Conflicts' : `${contradictionCount} Conflict${contradictionCount !== 1 ? 's' : ''} Flagged`}
                </span>
              </div>
            </div>
          </div>
        </div>

        {/* 2. STAGE PROGRESSION PIPELINE */}
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-xs font-semibold uppercase tracking-wider text-gray-400 flex items-center gap-2">
              <Layers size={14} className="text-primary" />
              Stage Lifecycle Progression
            </h2>
            <div className="flex items-center gap-2">
              <span className="text-xs text-gray-500 hidden sm:inline">
                Visualizes progress through each project stage in order
              </span>
              {canManageStages && (
                <Button
                  size="sm"
                  variant="secondary"
                  icon={Settings2}
                  onClick={() => {
                    if (orderedStages.length > 0) openStageModal(orderedStages[0]);
                  }}
                >
                  Edit Stages
                </Button>
              )}
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3">
            {orderedStages.map((stg, index) => {
              const isSelected = selectedStageId === stg.stage_id;
              const stageFindings = findings.filter((f) => f.target_stage_id === stg.stage_id);
              const stageBlockers = stageFindings.filter((f) => f.is_blocker);
              const hasRequirements = stg.mandatory_requirements_total > 0;
              // stg.completeness_score is -1 when hasRequirements is false
              // (not configured, Decision B) -- never render it as 0%/100%.
              const pct = hasRequirements ? Math.round(stg.completeness_score ?? 0) : null;

              // Stages only ever show Coverage + Audit (AUDIT_RULE_TAXONOMY_MATRIX.md)
              // -- no COMPLETED/IN PROGRESS completion vocabulary at stage level.
              // The badge is just a glance-level flag; the real numbers are the
              // Coverage/Audit lines in the card body below.
              let statusBadge = stageBlockers.length > 0
                ? <Badge variant="warning">BLOCKED</Badge>
                : !hasRequirements
                ? <Badge variant="neutral">NOT CONFIGURED</Badge>
                : null;
              let statusNote = hasRequirements
                ? `${stg.mandatory_requirements_satisfied}/${stg.mandatory_requirements_total} requirements have evidence`
                : 'No mandatory requirements defined for this stage';
              if (stageBlockers.length > 0) {
                statusNote = `${stageBlockers.length} blocking finding${stageBlockers.length !== 1 ? 's' : ''} · ${statusNote}`;
              }
              const showAddRequirementsCta = !hasRequirements && stageBlockers.length === 0 && canManageStages;

              return (
                <div
                  key={stg.stage_id}
                  onClick={() => setSelectedStageId(isSelected ? null : stg.stage_id)}
                  className={`rounded-xl border p-4 cursor-pointer transition-all ${
                    isSelected
                      ? 'border-primary ring-2 ring-primary/20 bg-surface'
                      : stageBlockers.length > 0
                      ? 'border-amber-500/40 bg-surface hover:border-amber-500/60'
                      : 'border-border bg-surface hover:border-primary/40 hover:bg-surface-hover'
                  }`}
                >
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-xs font-mono text-gray-500">0{index + 1}</span>
                    <div className="flex items-center gap-1.5">
                      {statusBadge}
                      {canManageStages && (
                        <button
                          type="button"
                          aria-label={`Edit ${stg.stage_name || stg.name}`}
                          title={`Edit ${stg.stage_name || stg.name} settings`}
                          onClick={(e) => {
                            e.stopPropagation();
                            openStageModal(stg);
                          }}
                          className="p-1 rounded text-gray-400 hover:text-gray-100 hover:bg-surface-hover transition-colors"
                        >
                          <Settings2 size={13} />
                        </button>
                      )}
                    </div>
                  </div>

                  <h3 className="font-semibold text-gray-100 text-sm truncate" title={stg.stage_name}>
                    {stg.stage_name}
                  </h3>

                  <div className="mt-3 space-y-2">
                    <div className="flex items-center justify-between text-[11px] text-gray-400">
                      <span>Coverage</span>
                      <span className="font-mono text-gray-200">{hasRequirements ? `${pct}%` : 'Not configured'}</span>
                    </div>
                    <div className="h-1.5 w-full rounded-full bg-background overflow-hidden">
                      {hasRequirements && (
                        <div
                          className={`h-full rounded-full ${
                            pct >= 100 ? 'bg-emerald-500' : 'bg-primary'
                          }`}
                          style={{ width: `${Math.min(100, Math.max(0, pct))}%` }}
                        />
                      )}
                    </div>
                    <div className="flex items-center justify-between text-[11px] text-gray-400">
                      <span>Audit</span>
                      <span className={`font-mono ${stageBlockers.length > 0 ? 'text-amber-400' : 'text-gray-200'}`}>
                        {stageFindings.length} finding{stageFindings.length !== 1 ? 's' : ''}
                        {stageBlockers.length > 0 ? ` · ${stageBlockers.length} blocking` : ''}
                      </span>
                    </div>
                    <p className="text-[11px] text-gray-400 pt-0.5 truncate">{statusNote}</p>
                    {showAddRequirementsCta && (
                      <Link
                        to={`/projects/${projectId}`}
                        onClick={(e) => e.stopPropagation()}
                        className="text-[11px] text-primary-light hover:text-primary transition-colors inline-block"
                      >
                        Define requirements →
                      </Link>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        {/* 3. WHAT NEEDS ATTENTION (Prioritized Actionable Findings) */}
        <div className="space-y-3">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
            <div>
              <h2 className="text-xs font-semibold uppercase tracking-wider text-gray-400 flex items-center gap-2">
                <ShieldAlert size={15} className="text-amber-400" />
                What Needs Attention
              </h2>
              <p className="text-xs text-gray-500 mt-0.5">
                Prioritized items required to clear project launch gates
              </p>
            </div>

            {/* Filter Bar */}
            <div className="flex items-center gap-2 flex-wrap text-xs">
              <div className="relative">
                <Search size={14} className="absolute left-2.5 top-2 text-gray-500" />
                <input
                  type="text"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  placeholder="Filter attention items..."
                  className="rounded-lg border border-border bg-surface pl-8 pr-3 py-1.5 text-xs text-gray-200 placeholder-gray-500 focus:border-primary focus:outline-none"
                />
              </div>

              {/* Status Filter */}
              <select
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value)}
                className="rounded-lg border border-border bg-surface px-2.5 py-1.5 text-xs text-gray-300 focus:border-primary focus:outline-none"
              >
                <option value="active">Active Only</option>
                <option value="dismissed">Dismissed Only</option>
                <option value="all">All Findings</option>
              </select>

              {/* Severity Filter */}
              <select
                value={severityFilter}
                onChange={(e) => setSeverityFilter(e.target.value)}
                className="rounded-lg border border-border bg-surface px-2.5 py-1.5 text-xs text-gray-300 focus:border-primary focus:outline-none"
              >
                <option value="ALL">All Severities</option>
                <option value="HIGH">High</option>
                <option value="MEDIUM">Medium</option>
                <option value="LOW">Low</option>
              </select>

              {/* Rule Filter */}
              {availableRuleCodes.length > 0 && (
                <select
                  value={ruleCodeFilter}
                  onChange={(e) => setRuleCodeFilter(e.target.value)}
                  className="rounded-lg border border-border bg-surface px-2.5 py-1.5 text-xs text-gray-300 focus:border-primary focus:outline-none"
                >
                  <option value="ALL">All Rules</option>
                  {availableRuleCodes.map((code) => (
                    <option key={code} value={code}>
                      Rule {code}
                    </option>
                  ))}
                </select>
              )}

              {/* Blocker Filter Toggle */}
              <button
                type="button"
                onClick={() => setBlockerOnly(!blockerOnly)}
                className={`rounded-lg border px-2.5 py-1.5 text-xs font-medium transition-colors ${
                  blockerOnly
                    ? 'border-red-500/50 bg-red-500/10 text-red-300'
                    : 'border-border bg-surface text-gray-400 hover:text-gray-200'
                }`}
              >
                {blockerOnly ? 'Blockers Only' : 'All Findings'}
              </button>

              {selectedStageId && (
                <button
                  type="button"
                  onClick={() => setSelectedStageId(null)}
                  className="rounded-lg border border-border bg-surface-hover px-2.5 py-1.5 text-gray-300 hover:text-gray-100 flex items-center gap-1"
                >
                  Clear Stage Filter
                </button>
              )}
            </div>
          </div>

          {/* Actionable Findings Cards */}
          {topFindings.length === 0 ? (
            <div className="rounded-xl border border-border bg-surface p-8 text-center">
              <CheckCircle2 size={32} className="mx-auto text-emerald-400 mb-2" />
              <p className="text-sm font-semibold text-gray-200">No active gate blockers</p>
              <p className="text-xs text-gray-500 mt-1">All gates are currently satisfied.</p>
            </div>
          ) : (
            <div className="space-y-2.5">
              {topFindings.map((finding, idx) => {
                const stage = stages.find((s) => s.stage_id === finding.target_stage_id);
                const stageLabel = stage?.stage_name || finding.details?.stage_name || 'Unknown Stage';
                const isContradiction = finding.rule_code === 'R007';
                const isApproval = finding.rule_code === 'R002' || finding.rule_code === 'R008';
                const occurrences = finding.occurrences || [finding];
                const isGrouped = occurrences.length > 1;
                const isExpanded = expandedGroupIdx === idx;

                return (
                  <div
                    key={finding.finding_id || `finding-${idx}`}
                    className="rounded-xl border border-border bg-surface p-4 hover:border-primary/50 transition-all"
                  >
                  <div
                    onClick={() => (isGrouped ? setExpandedGroupIdx(isExpanded ? null : idx) : setSelectedFinding(finding))}
                    className="cursor-pointer hover:bg-surface-hover -m-4 p-4 rounded-xl flex flex-col sm:flex-row sm:items-center justify-between gap-4"
                  >
                    <div className="space-y-1.5 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        {finding.is_dismissed ? (
                          <Badge variant="warning">DISMISSED ADVISORY</Badge>
                        ) : isApproval ? (
                          <Badge variant="warning">GATE REVIEW REQUIRED</Badge>
                        ) : isContradiction ? (
                          <Badge variant="danger">MATERIAL CONTRADICTION</Badge>
                        ) : finding.is_blocker ? (
                          <Badge variant="danger">GATING BLOCKER</Badge>
                        ) : (
                          <Badge variant="neutral">ADVISORY</Badge>
                        )}
                        <Badge variant="active">{finding.rule_code}</Badge>
                        {isGrouped ? (
                          <span className="text-xs text-gray-400 font-mono">Seen in {occurrences.length} stages</span>
                        ) : (
                          <span className="text-xs text-gray-400 font-mono">Stage: {stageLabel}</span>
                        )}
                        {finding.details?.requirement_context && (
                          <span className="text-xs text-primary font-mono bg-primary/10 px-2 py-0.5 rounded">
                            {finding.details.requirement_context}
                          </span>
                        )}
                      </div>

                      <h4 className="font-semibold text-gray-100 text-sm">{finding.title}</h4>
                      <p className="text-xs text-gray-300 line-clamp-2">{finding.description}</p>

                      {/* Quick Evidence Highlight */}
                      {isContradiction && finding.details?.value_a && (
                        <div className="flex items-center gap-3 text-xs text-gray-400 pt-0.5">
                          <span>
                            Target: <strong className="font-mono text-emerald-400">{finding.details.value_a}</strong>
                          </span>
                          <span>↔</span>
                          <span>
                            Observed: <strong className="font-mono text-red-400">{finding.details.value_b}</strong>
                          </span>
                          <span className="text-gray-500 font-mono">(SLA breach)</span>
                        </div>
                      )}

                      {isApproval && finding.details?.entity_label && (
                        <p className="text-[11px] text-gray-400">
                          Awaiting approval on <span className="font-mono text-gray-300">{finding.details.entity_label}</span> by Team Lead or Project Admin.
                        </p>
                      )}
                    </div>

                    <div className="flex items-center gap-2 shrink-0 self-end sm:self-center">
                      {isGrouped ? (
                        <ChevronDown size={16} className={`text-gray-500 transition-transform ${isExpanded ? 'rotate-180' : ''}`} />
                      ) : (
                        <span className="text-xs text-primary font-medium flex items-center gap-1 group">
                          Inspect Evidence
                          <ChevronRight size={14} className="text-primary transition-transform group-hover:translate-x-0.5" />
                        </span>
                      )}
                    </div>
                  </div>

                  {isGrouped && isExpanded && (
                    <div className="mt-3 pt-3 border-t border-border/50 space-y-1.5">
                      {occurrences.map((occ) => {
                        const occStage = stages.find((s) => s.stage_id === occ.target_stage_id);
                        const occStageLabel = occStage?.stage_name || occ.details?.stage_name || 'Unknown Stage';
                        return (
                          <button
                            key={occ.finding_id}
                            type="button"
                            onClick={() => setSelectedFinding(occ)}
                            className="w-full flex items-center justify-between gap-3 rounded-lg px-2.5 py-1.5 text-left text-xs hover:bg-surface-hover transition-colors"
                          >
                            <span className="text-gray-300 font-mono">Stage: {occStageLabel}</span>
                            <span className="text-primary flex items-center gap-1 shrink-0">
                              Inspect Evidence
                              <ChevronRight size={12} />
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  )}
                  </div>
                );
              })}
            </div>
          )}

          {/* Show All / Show Less Toggle */}
          {sortedFindings.length > 3 && (
            <div className="pt-1 text-center">
              <button
                type="button"
                onClick={() => setShowAllFindings((v) => !v)}
                className="text-xs font-medium text-gray-400 hover:text-primary transition-colors inline-flex items-center gap-1"
              >
                {showAllFindings
                  ? 'Show fewer attention items'
                  : `View all remaining findings (${remainingCount})`}
                <ChevronDown
                  size={14}
                  className={`transition-transform duration-200 ${showAllFindings ? 'rotate-180' : ''}`}
                />
              </button>
            </div>
          )}
        </div>

        {/* 4. PROGRESS HISTORY & ACTIVITY — project_admin+ only */}
        {canViewProgressActivity && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-5 pt-2">
          {/* Project Progress / History (from /progress-history) */}
          <div className="rounded-xl border border-border bg-surface p-5 space-y-4">
            <div className="flex items-center justify-between">
              <div>
                <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-400 flex items-center gap-1.5">
                  <TrendingUp size={15} className="text-primary" />
                  Project Progress History
                </h3>
                <p className="text-[11px] text-gray-500 mt-0.5">
                  Chronological progression of project readiness & completeness
                </p>
              </div>
              <span className="text-xs text-gray-500 font-mono">
                {history.length} snapshot{history.length === 1 ? '' : 's'}
              </span>
            </div>

            {history.length === 0 ? (
              <p className="text-xs text-gray-500 italic py-4">No historical snapshots recorded yet.</p>
            ) : (
              <div className="space-y-2.5">
                {history.slice(0, 4).map((snap, idx) => {
                  const snapTime = snap.snapshot_at
                    ? new Date(snap.snapshot_at).toLocaleString()
                    : `Snapshot #${idx + 1}`;
                  const snapConfigured = (snap.completeness_score ?? -1) >= 0;
                  const score = snapConfigured ? Math.round(snap.completeness_score * 10) / 10 : null;
                  const snapCoverageConfigured = (snap.mandatory_requirement_coverage ?? -1) >= 0;
                  const coveragePct = snapCoverageConfigured ? Math.round(snap.mandatory_requirement_coverage * 100) : null;
                  const isSnapReady = snap.readiness_status === 'READY';

                  return (
                    <div
                      key={idx}
                      className="rounded-lg border border-border/60 bg-background/50 p-3 text-xs space-y-1.5"
                    >
                      <div className="flex items-center justify-between">
                        <div className="flex items-center gap-2">
                          <span className="text-gray-400 font-mono text-[11px]">{snapTime}</span>
                          <Badge variant={isSnapReady ? 'success' : 'warning'}>
                            {isSnapReady ? 'READY' : 'GATED'}
                          </Badge>
                        </div>
                        <span className="text-gray-300 font-mono font-medium">
                          Completeness: {snapConfigured ? `${score}%` : 'Not configured'}
                        </span>
                      </div>
                      <div className="flex items-center justify-between text-[11px] text-gray-400">
                        <span>{snap.blockers_count ?? 0} active gating condition{snap.blockers_count === 1 ? '' : 's'}</span>
                        <span className="text-gray-500">
                          Requirements Coverage: {snapCoverageConfigured ? `${coveragePct}%` : 'Not configured'}
                        </span>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* Recent Project Activity (from timeline, filtered of audit runs) */}
          <div className="rounded-xl border border-border bg-surface p-5 space-y-4">
            <div className="flex items-center justify-between">
              <div>
                <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-400 flex items-center gap-1.5">
                  <Clock size={15} className="text-primary" />
                  Recent Project Activity
                </h3>
                <p className="text-[11px] text-gray-500 mt-0.5">
                  Document lifecycle events, submissions, and review actions
                </p>
              </div>
              <span className="text-xs text-gray-500 font-mono">Latest Milestones</span>
            </div>

            {activityEvents.length === 0 ? (
              <p className="text-xs text-gray-500 italic py-4">No recent activity recorded for this project yet.</p>
            ) : (
              <div className="space-y-2">
                {activityEvents.slice(0, 4).map((ev, idx) => {
                  const evTime = ev.timestamp ? new Date(ev.timestamp).toLocaleTimeString() : '';
                  // Document-typed events link into the Workspace and
                  // highlight the referenced document (UI_FIXES_2026-09-15.md #19).
                  const docId = ev.metadata?.resource_type === 'document' ? ev.metadata?.resource_id : null;
                  const content = (
                    <>
                      <div className="flex items-center justify-between">
                        <span className="font-semibold text-gray-200">{ev.title || ev.event_type}</span>
                        <span className="text-gray-500 font-mono text-[11px]">{evTime}</span>
                      </div>
                      <p className="text-gray-400 text-[11px]">{ev.description}</p>
                    </>
                  );
                  return docId ? (
                    <Link
                      key={idx}
                      to={`/projects/${encodeURIComponent(projectId)}?highlight=${encodeURIComponent(docId)}`}
                      className="block rounded-lg border border-border/60 bg-background/50 p-2.5 text-xs space-y-0.5 hover:border-primary/50 hover:bg-surface-hover transition-colors"
                    >
                      {content}
                    </Link>
                  ) : (
                    <div
                      key={idx}
                      className="rounded-lg border border-border/60 bg-background/50 p-2.5 text-xs space-y-0.5"
                    >
                      {content}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
        )}
      </div>

      {/* Audit Finding Inspection Drawer */}
      <AuditFindingDrawer
        open={Boolean(selectedFinding)}
        onClose={() => setSelectedFinding(null)}
        finding={selectedFinding}
        projectId={projectId}
        stages={stages}
        documents={documents}
        onAskSearchAgent={openSearchPanel}
        onAskCustomQuestion={openSearchPanelForCustomQuestion}
        onFindingUpdated={() => loadData({ silent: true })}
      />

      {/* Search Agent slide-over — UI_FIXES_2026-09-15.md #18: stays on
          Intelligence instead of navigating away to the Workspace tab. */}
      {searchPanelOpen && (
        <>
          <button
            type="button"
            aria-label="Dismiss"
            onClick={() => setSearchPanelOpen(false)}
            className="fixed inset-0 z-40 bg-background/50"
          />
          <div className="fixed top-0 bottom-0 right-0 z-50 w-full max-w-md border-l border-border bg-background shadow-2xl flex flex-col">
            <div className="flex items-center justify-between px-4 py-3 border-b border-border/50 shrink-0">
              <span className="text-sm font-semibold text-gray-100">Search Agent</span>
              <button
                type="button"
                onClick={() => setSearchPanelOpen(false)}
                className="text-gray-400 hover:text-gray-200 p-1 rounded"
                aria-label="Close Search"
              >
                <X size={18} />
              </button>
            </div>
            <div className="flex-1 min-h-0 flex flex-col">
              <ChatPanel
                key={searchPanelQuery || searchPanelPrefill || 'search-panel'}
                projectId={projectId}
                mode="search"
                initialQuery={searchPanelQuery}
                prefillQuery={searchPanelPrefill}
              />
            </div>
          </div>
        </>
      )}

      {/* Teams Modal */}
      <Modal
        open={teamsOpen}
        onClose={() => setTeamsOpen(false)}
        title="Teams in this project"
        description="Active teams contributing to this project lifecycle."
      >
        <div className="space-y-3">
          {teamsLoading ? (
            <p className="text-sm text-gray-400">Loading teams…</p>
          ) : teams.length === 0 ? (
            <p className="text-sm text-gray-500">No teams found for this project.</p>
          ) : (
            teams.map((t) => (
              <div
                key={t.team_id}
                className="flex items-center justify-between rounded-lg border border-border bg-background px-3 py-2.5"
              >
                <span className="text-sm font-medium text-gray-200">{t.name}</span>
                <span className="text-xs text-gray-500">
                  {t.member_count} member{t.member_count === 1 ? '' : 's'}
                </span>
              </div>
            ))
          )}
        </div>
      </Modal>

      {/* Edit Stage Modal */}
      <Modal
        open={Boolean(editingStage)}
        onClose={() => setEditingStage(null)}
        title={editingStage ? `Stage Settings: ${editingStage.stage_name || editingStage.name}` : 'Stage Settings'}
        description="Rename, configure gate approval, or update permitted stage references."
        footer={
          <div className="flex items-center justify-between w-full">
            <Link
              to={`/projects/${encodeURIComponent(projectId)}`}
              className="text-xs text-primary hover:underline flex items-center gap-1"
            >
              Open Workspace Sources →
            </Link>
            <div className="flex items-center gap-2">
              <Button variant="ghost" onClick={() => setEditingStage(null)}>
                Close
              </Button>
              <Button
                onClick={handleSaveStage}
                loading={stageBusy}
                disabled={!stageEditName.trim()}
              >
                Save Changes
              </Button>
            </div>
          </div>
        }
      >
        {editingStage && (
          <div className="space-y-4">
            {stageNotice && <p className="text-sm text-emerald-400">{stageNotice}</p>}
            {stageErr && <p className="text-sm text-red-400">{stageErr}</p>}

            {/* Stage Selector */}
            <div>
              <label className="block text-xs font-medium text-gray-300 mb-1.5">Select Stage to Edit</label>
              <div className="flex flex-wrap gap-1.5">
                {orderedStages.map((s) => {
                  const isCurrent = s.stage_id === editingStage.stage_id;
                  return (
                    <button
                      key={s.stage_id}
                      type="button"
                      onClick={() => openStageModal(s)}
                      className={`px-2.5 py-1 text-xs rounded-lg border transition-colors ${
                        isCurrent
                          ? 'border-primary bg-primary/15 text-primary font-semibold'
                          : 'border-border bg-surface text-gray-400 hover:text-gray-200'
                      }`}
                    >
                      {s.stage_name || s.name}
                    </button>
                  );
                })}
              </div>
            </div>

            {/* Rename */}
            <div>
              <Input
                label="Stage Name"
                value={stageEditName}
                onChange={(e) => setStageEditName(e.target.value)}
              />
            </div>

            {/* Require Approval Switch */}
            <div className="flex items-start justify-between gap-3 rounded-lg border border-border bg-background p-3">
              <div className="min-w-0">
                <p className="text-sm font-medium text-gray-200 flex items-center gap-1.5">
                  <ShieldCheck size={14} className="text-amber-400" /> Require Approval Gate
                </p>
                <p className="text-xs text-gray-400 mt-0.5">
                  Documents in this stage must be formally reviewed and approved before downstream stage progression.
                </p>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={stageEditApproval}
                disabled={stageBusy}
                onClick={() => setStageEditApproval(!stageEditApproval)}
                className={`mt-0.5 shrink-0 relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${
                  stageEditApproval ? 'bg-primary' : 'bg-border'
                }`}
              >
                <span
                  className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                    stageEditApproval ? 'translate-x-4' : 'translate-x-0.5'
                  }`}
                />
              </button>
            </div>

            {/* Stage References */}
            <div className="border-t border-border pt-3">
              <p className="text-xs font-semibold uppercase tracking-wider text-gray-300 flex items-center gap-1.5 mb-1">
                <Link2 size={13} className="text-primary" /> Permitted Stage References
              </p>
              <p className="text-xs text-gray-500 mb-2">
                Let the Search Agent also pull content from these stages when a question is scoped to &ldquo;{editingStage.stage_name || editingStage.name}&rdquo;.
              </p>
              <div className="space-y-1">
                {stages
                  .filter((s) => s.stage_id !== editingStage.stage_id)
                  .map((s) => (
                    <label
                      key={s.stage_id}
                      className="flex items-center gap-2 rounded-md px-2 py-1 text-xs text-gray-300 hover:bg-surface-hover cursor-pointer"
                    >
                      <input
                        type="checkbox"
                        className="accent-primary"
                        checked={stageEditRefs.includes(s.stage_id)}
                        onChange={() => toggleStageRef(s.stage_id)}
                      />
                      <span>{s.stage_name || s.name}</span>
                    </label>
                  ))}
              </div>
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
};

export default ProjectIntelligence;
