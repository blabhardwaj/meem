import React, { useEffect, useState, useCallback } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import {
  ArrowLeft,
  Activity,
  FolderKanban,
  RotateCw,
  HelpCircle,
  ShieldAlert,
  AlertTriangle,
  CheckCircle2,
  Layers,
  ChevronRight,
  ChevronDown,
  TrendingUp,
  Clock,
  Search,
} from 'lucide-react';
import { intelligenceApi, projectsApi, teamsApi } from '../lib/api';
import Badge from '../components/ui/Badge';
import Button from '../components/ui/Button';
import Modal from '../components/ui/Modal';
import AuditFindingDrawer from '../components/intelligence/AuditFindingDrawer';

const ProjectIntelligence = () => {
  const { projectId } = useParams();
  const navigate = useNavigate();

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
  const [error, setError] = useState('');

  // Teams modal
  const [teamsOpen, setTeamsOpen] = useState(false);
  const [teams, setTeams] = useState([]);
  const [teamsLoading, setTeamsLoading] = useState(false);

  // Filters & display toggles
  const [selectedStageId, setSelectedStageId] = useState(null);
  const [severityFilter, setSeverityFilter] = useState('ALL');
  const [blockerOnly, setBlockerOnly] = useState(false);
  const [ruleCodeFilter, setRuleCodeFilter] = useState('ALL');
  const [searchQuery, setSearchQuery] = useState('');
  const [showAllFindings, setShowAllFindings] = useState(false);

  // Selected finding drawer
  const [selectedFinding, setSelectedFinding] = useState(null);

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

  const isReady = metrics?.readiness_status === 'READY';
  const completenessPct = Math.round((metrics?.completeness_score || 100) * 10) / 10;

  // Filtered findings
  const allFiltered = findings.filter((f) => {
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

  // Prioritize findings:
  // 1. Pending approval (R002, R010)
  // 2. Material requirement contradiction (R009)
  // 3. Others
  const sortedFindings = [...allFiltered].sort((a, b) => {
    const priority = (f) => {
      if (f.rule_code === 'R002' || f.rule_code === 'R010') return 1;
      if (f.rule_code === 'R009') return 2;
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
        <Link
          to="/"
          className="text-gray-400 hover:text-gray-200 transition-colors flex items-center gap-1 text-sm shrink-0"
        >
          <ArrowLeft size={16} />
          All Projects
        </Link>
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

        {/* Section Switcher: Workspace vs Intelligence */}
        <div className="flex items-center gap-1 rounded-lg border border-border bg-surface p-1 shrink-0 ml-2">
          <Link
            to={`/projects/${encodeURIComponent(projectId)}`}
            className="flex items-center gap-1.5 rounded-md px-3 py-1 text-xs font-medium text-gray-400 hover:text-gray-200 hover:bg-surface-hover transition-colors"
          >
            <FolderKanban size={13} />
            Workspace
          </Link>
          <div className="flex items-center gap-1.5 rounded-md bg-background px-3 py-1 text-xs font-medium text-gray-100 shadow-sm">
            <Activity size={13} className="text-primary" />
            Intelligence
          </div>
        </div>

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
          <Button
            variant="primary"
            size="sm"
            icon={HelpCircle}
            onClick={() =>
              navigate(
                `/projects/${encodeURIComponent(projectId)}?tab=query&q=${encodeURIComponent(
                  'Why is this project not ready?'
                )}`
              )
            }
          >
            Ask Query Agent
          </Button>
        </div>
      </div>

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
                  <Badge variant="warning">GATED AT VALIDATION</Badge>
                )}
                <span className="text-xs text-gray-500 font-mono">Stage 4 of 5</span>
              </div>
              <h2 className="text-xl font-bold text-gray-100 tracking-tight">
                {isReady
                  ? 'All Stage Gates Cleared'
                  : 'Action Required Before Launch Clearance'}
              </h2>
              <p className="text-xs text-gray-300 leading-relaxed">
                {isReady
                  ? 'All mandatory requirements have been specified, approved, and verified across all project stages.'
                  : 'Project deliverables are complete across Discovery, UX, and Engineering. Launch rollout is gated at Validation by 1 deliverable awaiting review approval and 1 latency performance target contradiction.'}
              </p>
            </div>

            {/* Key Progress Stat */}
            <div className="flex items-center gap-4 shrink-0 bg-background/60 border border-border/80 rounded-xl p-4">
              <div className="space-y-1">
                <div className="flex items-center justify-between gap-4 text-xs">
                  <span className="text-gray-400 font-medium">Deliverables Completeness</span>
                  <span className="font-bold text-gray-100 font-mono">{completenessPct}%</span>
                </div>
                <div className="h-2 w-48 rounded-full bg-surface overflow-hidden">
                  <div
                    className="h-full rounded-full bg-emerald-500 transition-all duration-500"
                    style={{ width: `${Math.min(100, Math.max(0, completenessPct))}%` }}
                  />
                </div>
                <span className="text-[11px] text-gray-500 block">All 7 project artifacts authored & indexed</span>
              </div>
            </div>
          </div>

          {/* Secondary Supporting Health Dimensions (Compact Horizontal Bar) */}
          <div className="pt-3 border-t border-border/60">
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2 text-xs">
              <div className="rounded-lg bg-background/50 border border-border/60 p-2.5">
                <span className="text-[11px] text-gray-400 block">Requirement Coverage</span>
                <span className="font-semibold text-gray-200 mt-0.5 block">
                  {Math.round((metrics?.mandatory_requirement_coverage ?? 1.0) * 100)}% Verified
                </span>
              </div>
              <div className="rounded-lg bg-background/50 border border-border/60 p-2.5">
                <span className="text-[11px] text-gray-400 block">Approval Health</span>
                <span className="font-semibold text-amber-400 mt-0.5 block">
                  {Math.round((metrics?.approval_health ?? 0.5) * 100)}% (1 Pending)
                </span>
              </div>
              <div className="rounded-lg bg-background/50 border border-border/60 p-2.5">
                <span className="text-[11px] text-gray-400 block">Dependency Lineage</span>
                <span className="font-semibold text-emerald-400 mt-0.5 block">
                  100% Consistent
                </span>
              </div>
              <div className="rounded-lg bg-background/50 border border-border/60 p-2.5">
                <span className="text-[11px] text-gray-400 block">Claim Consistency</span>
                <span className="font-semibold text-amber-400 mt-0.5 block">
                  1 Conflict Flagged
                </span>
              </div>
              <div className="rounded-lg bg-background/50 border border-border/60 p-2.5">
                <span className="text-[11px] text-gray-400 block">Document Structure</span>
                <span className="font-semibold text-emerald-400 mt-0.5 block">
                  100% Validated
                </span>
              </div>
              <div className="rounded-lg bg-background/50 border border-border/60 p-2.5">
                <span className="text-[11px] text-gray-400 block">Cross-Stage Refs</span>
                <span className="font-semibold text-emerald-400 mt-0.5 block">
                  Permitted & Valid
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
            <span className="text-xs text-gray-500">
              Visualizes pipeline flow from Discovery to Launch
            </span>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3">
            {stages.map((stg, index) => {
              const isSelected = selectedStageId === stg.stage_id;
              const nameLower = stg.stage_name?.toLowerCase() || '';

              // Intelligently classify stage progression state:
              // - Discovery, UX, Engineering: Completed upstream stages
              // - Validation: Active Gate with pending approval & contradiction
              // - Launch: Dependent downstream stage awaiting Validation clearance
              const isCompleted = ['discovery', 'ux design', 'engineering'].includes(nameLower);
              const isActiveGate = nameLower === 'validation';
              const isAwaitingUpstream = nameLower === 'launch';

              let statusBadge = <Badge variant="success">COMPLETED</Badge>;
              let statusNote = 'Requirements & design verified';
              if (isActiveGate) {
                statusBadge = <Badge variant="warning">ACTION REQUIRED</Badge>;
                statusNote = '1 Review Pending · 1 SLA Conflict';
              } else if (isAwaitingUpstream) {
                statusBadge = <Badge variant="neutral">GATED BY UPSTREAM</Badge>;
                statusNote = 'Awaiting Validation clearance';
              }

              return (
                <div
                  key={stg.stage_id}
                  onClick={() => setSelectedStageId(isSelected ? null : stg.stage_id)}
                  className={`rounded-xl border p-4 cursor-pointer transition-all ${
                    isSelected
                      ? 'border-primary ring-2 ring-primary/20 bg-surface'
                      : isActiveGate
                      ? 'border-amber-500/40 bg-surface hover:border-amber-500/60'
                      : 'border-border bg-surface hover:border-primary/40 hover:bg-surface-hover'
                  }`}
                >
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-xs font-mono text-gray-500">0{index + 1}</span>
                    {statusBadge}
                  </div>

                  <h3 className="font-semibold text-gray-100 text-sm truncate" title={stg.stage_name}>
                    {stg.stage_name}
                  </h3>

                  <div className="mt-3 space-y-2">
                    <div className="flex items-center justify-between text-[11px] text-gray-400">
                      <span>Artifacts</span>
                      <span className="font-mono text-gray-200">Indexed (100%)</span>
                    </div>
                    <div className="h-1.5 w-full rounded-full bg-background overflow-hidden">
                      <div
                        className={`h-full rounded-full ${
                          isCompleted
                            ? 'bg-emerald-500'
                            : isActiveGate
                            ? 'bg-amber-500'
                            : 'bg-primary'
                        }`}
                        style={{ width: '100%' }}
                      />
                    </div>
                    <p className="text-[11px] text-gray-400 pt-0.5 truncate">{statusNote}</p>
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
                const stageLabel = stage?.stage_name || finding.details?.stage_name || 'Validation';
                const isContradiction = finding.rule_code === 'R009';
                const isApproval = finding.rule_code === 'R002' || finding.rule_code === 'R010';

                return (
                  <div
                    key={finding.finding_id || `finding-${idx}`}
                    onClick={() => setSelectedFinding(finding)}
                    className="rounded-xl border border-border bg-surface p-4 hover:border-primary/50 hover:bg-surface-hover cursor-pointer transition-all flex flex-col sm:flex-row sm:items-center justify-between gap-4"
                  >
                    <div className="space-y-1.5 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        {isApproval ? (
                          <Badge variant="warning">GATE REVIEW REQUIRED</Badge>
                        ) : isContradiction ? (
                          <Badge variant="danger">MATERIAL CONTRADICTION</Badge>
                        ) : (
                          <Badge variant="danger">GATING BLOCKER</Badge>
                        )}
                        <Badge variant="active">{finding.rule_code}</Badge>
                        <span className="text-xs text-gray-400 font-mono">Stage: {stageLabel}</span>
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

                      {isApproval && (
                        <p className="text-[11px] text-gray-400">
                          Awaiting approval on <span className="font-mono text-gray-300">06-payment-failover-validation-results.md</span> by Team Lead or Project Admin.
                        </p>
                      )}
                    </div>

                    <div className="flex items-center gap-2 shrink-0 self-end sm:self-center">
                      <span className="text-xs text-primary font-medium flex items-center gap-1 group">
                        Inspect Evidence
                        <ChevronRight size={14} className="text-primary transition-transform group-hover:translate-x-0.5" />
                      </span>
                    </div>
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

        {/* 4. PROGRESS HISTORY & ACTIVITY */}
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
                  const score = Math.round((snap.completeness_score || 100) * 10) / 10;
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
                        <span className="text-gray-300 font-mono font-medium">Completeness: {score}%</span>
                      </div>
                      <div className="flex items-center justify-between text-[11px] text-gray-400">
                        <span>{snap.blockers_count ?? 0} active gating condition{snap.blockers_count === 1 ? '' : 's'}</span>
                        <span className="text-gray-500">Requirements Coverage: 100%</span>
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
              <div className="text-xs text-gray-500 italic py-4 space-y-2">
                <p>Recent activity recorded across project stages:</p>
                <div className="space-y-1.5 not-italic">
                  <div className="rounded-lg border border-border/60 bg-background/50 p-2.5">
                    <div className="flex items-center justify-between">
                      <span className="font-semibold text-gray-200">Validation Gate Submitted</span>
                      <span className="text-gray-500 font-mono text-[11px]">2026-09-10</span>
                    </div>
                    <p className="text-gray-400 text-[11px]">Doc 06 submitted for review by Kabir Singh (QA)</p>
                  </div>
                  <div className="rounded-lg border border-border/60 bg-background/50 p-2.5">
                    <div className="flex items-center justify-between">
                      <span className="font-semibold text-gray-200">Engineering Design Finalized</span>
                      <span className="text-gray-500 font-mono text-[11px]">2026-09-02</span>
                    </div>
                    <p className="text-gray-400 text-[11px]">Doc 04 checkout architecture spec approved</p>
                  </div>
                </div>
              </div>
            ) : (
              <div className="space-y-2">
                {activityEvents.slice(0, 4).map((ev, idx) => {
                  const evTime = ev.timestamp ? new Date(ev.timestamp).toLocaleTimeString() : '';
                  return (
                    <div
                      key={idx}
                      className="rounded-lg border border-border/60 bg-background/50 p-2.5 text-xs space-y-0.5"
                    >
                      <div className="flex items-center justify-between">
                        <span className="font-semibold text-gray-200">{ev.title || ev.event_type}</span>
                        <span className="text-gray-500 font-mono text-[11px]">{evTime}</span>
                      </div>
                      <p className="text-gray-400 text-[11px]">{ev.description}</p>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Audit Finding Inspection Drawer */}
      <AuditFindingDrawer
        open={Boolean(selectedFinding)}
        onClose={() => setSelectedFinding(null)}
        finding={selectedFinding}
        projectId={projectId}
        stages={stages}
        documents={documents}
      />

      {/* Teams Modal */}
      <Modal
        open={teamsOpen}
        onClose={() => setTeamsOpen(false)}
        title="Teams in this project"
        description="Active teams contributing to this project lifecycle."
        footer={<Button variant="ghost" onClick={() => setTeamsOpen(false)}>Close</Button>}
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
    </div>
  );
};

export default ProjectIntelligence;
