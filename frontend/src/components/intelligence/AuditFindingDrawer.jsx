import React, { useState, useEffect } from 'react';
import {
  ShieldAlert,
  FileText,
  ExternalLink,
  ChevronRight,
  Sparkles,
  Layers,
  HelpCircle,
  CheckCircle2,
  EyeOff,
  RotateCcw,
} from 'lucide-react';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import Modal from '../ui/Modal';
import MarkdownViewer from '../ui/MarkdownViewer';
import { intelligenceApi, documentsApi } from '../../lib/api';
import { useAuth } from '../../context/AuthContext';

const getSeverityBadge = (severity) => {
  const s = (severity || '').toUpperCase();
  if (s === 'HIGH' || s === 'CRITICAL') return <Badge variant="danger">High Severity</Badge>;
  if (s === 'MEDIUM') return <Badge variant="warning">Medium</Badge>;
  return <Badge variant="neutral">Low</Badge>;
};

const RULE_CATEGORY_LABELS = {
  R001: 'Missing Requirement',
  R002: 'Gate Approval Missing',
  R003: 'Broken Dependency',
  R004: 'Stale Document Reference',
  R005: 'Broken Dependency',
  R006: 'Unassigned Stage Requirement',
  R007: 'Performance Contradiction',
  R008: 'Pending Workflow Review',
  R009: 'Document-Level Coherence',
  R010: 'Scanner-Flagged Current Version',
};

const getRuleCategory = (ruleCode) => RULE_CATEGORY_LABELS[(ruleCode || '').toUpperCase()] || 'Audit Rule Violation';

// AuditFindingDrawer — reads ONLY real fields off the AuditFinding row
// (title, description, severity, is_blocker, rule_code, target_stage_id,
// evidence_sources, details) plus whatever documents/stages the caller
// already has loaded. No rule-specific hardcoded copy, snippets, or
// people's names — a finding with sparse evidence just shows fewer
// sections instead of inventing content to fill the gap.
const AuditFindingDrawer = ({
  open,
  onClose,
  finding,
  projectId,
  stages = [],
  documents = [],
  onAskSearchAgent,
  onAskCustomQuestion,
  onFindingUpdated,
}) => {
  const { user } = useAuth();
  const role = user?.is_org_admin ? 'org_admin' : user?.project_roles?.[projectId];
  const canDismiss = Boolean(user?.is_org_admin || ['team_lead', 'project_admin'].includes(role));

  const [viewerDoc, setViewerDoc] = useState(null);
  const [viewerLoading, setViewerLoading] = useState(false);
  const [neighborhood, setNeighborhood] = useState(null);

  const [showDismissModal, setShowDismissModal] = useState(false);
  const [dismissReason, setDismissReason] = useState('');
  const [dismissBusy, setDismissBusy] = useState(false);
  const [dismissError, setDismissError] = useState('');

  useEffect(() => {
    if (!open || !finding) {
      setNeighborhood(null);
      return;
    }
    if (finding.affected_entity_type === 'document' && finding.affected_entity_id) {
      intelligenceApi
        .neighborhood(projectId, { sourceTable: 'documents', sourceId: finding.affected_entity_id, depth: 2 })
        .then(setNeighborhood)
        .catch(() => setNeighborhood(null));
    } else {
      setNeighborhood(null);
    }
  }, [open, finding, projectId]);

  if (!open || !finding) return null;

  const details = finding.details || {};
  const evidenceSources = Array.isArray(finding.evidence_sources) ? finding.evidence_sources : [];

  const targetStage = stages.find((s) => s.stage_id === finding.target_stage_id) || null;
  const stageName = targetStage?.name || targetStage?.stage_name || details.stage_name || 'Project-wide';

  // The document this finding is actually about, when affected_entity_type
  // is "document" — real lookup by document_id. Field is `filename`, per
  // projectsApi.documents()'s own mapDocs() remapping (the raw backend
  // field is original_filename, but every caller of that API function
  // receives the remapped `filename` — this is what's actually passed in
  // via the `documents` prop, from ProjectIntelligence.jsx).
  const affectedDoc =
    finding.affected_entity_type === 'document'
      ? documents.find((d) => d.document_id === finding.affected_entity_id)
      : null;

  // Evidence documents referenced by evidence_sources (shape varies by
  // rule; only render entries that actually resolve to a real document).
  // If the affected document is already displayed in the Affected Document card above,
  // we filter it out so the Evidence Documents card shows the conflicting/referenced files.
  const evidenceDocs = evidenceSources
    .map((src) => {
      const docId = typeof src === 'string' ? src : src?.document_id;
      return documents.find((d) => d.document_id === docId) || null;
    })
    .filter((doc, idx, self) =>
      doc &&
      (!affectedDoc || doc.document_id !== affectedDoc.document_id || evidenceSources.length === 1) &&
      self.findIndex((d) => d?.document_id === doc.document_id) === idx
    );

  const handleInspectDocument = async (doc) => {
    if (!doc) return;
    setViewerLoading(true);
    try {
      const data = await documentsApi.view(doc.document_id);
      const docStage = stages.find((s) => s.stage_id === doc.stage_id);
      const docStageName = docStage?.name || docStage?.stage_name || stageName;
      setViewerDoc({
        title: doc.filename,
        subtitle: `${docStageName} · Current version`,
        content: data.content_markdown || '_No content available for this version._',
      });
    } catch (err) {
      setViewerDoc({
        title: doc.filename,
        subtitle: stageName,
        content: `_Could not load this document: ${err.message || 'unknown error'}_`,
      });
    } finally {
      setViewerLoading(false);
    }
  };

  // Every suggested query names the actual document and stage this finding
  // is about, not just the rule code — get_project_gaps (the tool the
  // Search Agent uses to answer these) accepts a document_reference filter
  // precisely so a query like this can be scoped to ONE document's findings
  // instead of returning everything flagged anywhere in the stage. A query
  // that only said "R009" would have no way to tell the agent which of
  // several R009 findings in the same stage the user actually means.
  const docClause = affectedDoc ? ` in "${affectedDoc.filename}"` : '';
  const stageClause = stageName !== 'Project-wide' ? ` (${stageName} stage)` : '';
  const suggestedQueries = [
    `Explain the ${getRuleCategory(finding.rule_code)} finding${docClause}${stageClause}.`,
    affectedDoc
      ? `What would fix this issue in "${affectedDoc.filename}"?`
      : `What would resolve this ${finding.rule_code} finding?`,
    affectedDoc ? `Is "${affectedDoc.filename}" ready for approval?` : `What else is blocking this stage?`,
  ];

  const handleAskQueryAgent = (queryText) => {
    onClose();
    onAskSearchAgent?.(queryText);
  };

  // Distinct from the suggested queries above: this opens the Search Agent
  // with an EMPTY, focused input for the user's own question — it must
  // never auto-send anything, matching what "Ask a custom question" says
  // it does. A short, document-naming placeholder is prefilled as a
  // starting point the user can overwrite, not a query that fires on click.
  const handleAskCustomQuestion = () => {
    onClose();
    onAskCustomQuestion?.(affectedDoc ? `About "${affectedDoc.filename}": ` : '');
  };

  const handleDismiss = async () => {
    if (!dismissReason.trim() || dismissReason.trim().length < 5) {
      setDismissError('Please provide a substantive rationale (at least 5 characters).');
      return;
    }
    try {
      setDismissBusy(true);
      setDismissError('');
      await intelligenceApi.dismissFinding(projectId, finding.finding_id, dismissReason.trim());
      setShowDismissModal(false);
      setDismissReason('');
      onFindingUpdated?.();
      onClose();
    } catch (err) {
      setDismissError(err.message || 'Failed to dismiss finding.');
    } finally {
      setDismissBusy(false);
    }
  };

  const handleRestore = async () => {
    try {
      setDismissBusy(true);
      await intelligenceApi.restoreFinding(projectId, finding.finding_id);
      onFindingUpdated?.();
      onClose();
    } catch (err) {
      alert(err.message || 'Failed to restore finding.');
    } finally {
      setDismissBusy(false);
    }
  };

  return (
    <>
      <Modal open={open} onClose={onClose} title={finding.title} scrollable>
        <div className="space-y-6 -mt-1">
          {/* Badges row */}
          <div className="flex items-center gap-2 flex-wrap -mt-2">
            {getSeverityBadge(finding.severity)}
            <Badge variant="active">{finding.rule_code}</Badge>
            {finding.is_blocker ? (
              <Badge variant="danger">Gating Blocker</Badge>
            ) : (
              <Badge variant="neutral">Advisory</Badge>
            )}
            {finding.is_dismissed && (
              <Badge variant="warning">Dismissed</Badge>
            )}
            <span className="text-xs text-gray-400 font-mono">Stage: {stageName}</span>
          </div>

          {/* Dismissal Status Banner */}
          {finding.is_dismissed && (
            <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/10 p-4 space-y-2">
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <CheckCircle2 size={16} className="text-emerald-400 shrink-0" />
                  <span className="text-xs font-semibold uppercase tracking-wider text-emerald-300">
                    Advisory Finding Dismissed
                  </span>
                </div>
                {canDismiss && (
                  <Button
                    variant="secondary"
                    size="sm"
                    icon={RotateCcw}
                    loading={dismissBusy}
                    onClick={handleRestore}
                  >
                    Restore finding
                  </Button>
                )}
              </div>
              <p className="text-xs text-emerald-200/90 font-medium">
                Rationale: "{finding.dismissal?.reason || 'Dismissed as reviewed advisory / false positive.'}"
              </p>
              <div className="flex items-center gap-2 text-[11px] text-emerald-400/70 pt-1">
                {finding.dismissal?.dismissed_at && (
                  <span>Dismissed {new Date(finding.dismissal.dismissed_at).toLocaleString()}</span>
                )}
                <span>·</span>
                <span>Audit trail logged</span>
                <span>·</span>
                <span>Readiness calculation unaffected</span>
              </div>
            </div>
          )}

          {/* Dismiss Finding Action Card for Active Advisories */}
          {!finding.is_dismissed && !finding.is_blocker && canDismiss && (
            <div className="rounded-xl border border-border bg-surface/80 p-3.5 flex items-center justify-between gap-3">
              <div>
                <span className="text-xs font-semibold text-gray-200">Advisory Finding Adjudication</span>
                <p className="text-xs text-gray-400 mt-0.5">
                  Dismiss this advisory if reviewed and accepted as a false positive or intentional variance.
                </p>
              </div>
              <Button
                variant="secondary"
                size="sm"
                icon={EyeOff}
                onClick={() => setShowDismissModal(true)}
              >
                Dismiss finding
              </Button>
            </div>
          )}

          {/* Finding Overview */}
          <div className="rounded-xl border border-border bg-background/80 p-4 space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider text-gray-400">
                Finding Overview
              </span>
              <span className="text-xs font-medium text-primary">{getRuleCategory(finding.rule_code)}</span>
            </div>
            <p className="text-sm text-gray-200 leading-relaxed">{finding.description}</p>
            <div className="pt-2 border-t border-border/50 flex items-center justify-between text-xs text-gray-400">
              <span>
                Project Impact:{' '}
                <strong className={finding.is_blocker ? 'text-amber-400 font-medium' : 'text-gray-300'}>
                  {finding.is_blocker ? 'Blocks stage/project readiness until resolved' : 'Advisory recommendation'}
                </strong>
              </span>
            </div>
          </div>

          {/* Affected document, if this finding is about one */}
          {affectedDoc && (
            <div className="rounded-xl border border-border bg-background/80 p-4 space-y-2">
              <span className="text-xs font-semibold uppercase tracking-wider text-gray-400 flex items-center gap-1.5">
                <FileText size={14} className="text-primary" />
                Affected Document
              </span>
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-medium text-gray-200 truncate">{affectedDoc.filename}</span>
                <button
                  type="button"
                  onClick={() => handleInspectDocument(affectedDoc)}
                  disabled={viewerLoading}
                  className="text-xs text-primary hover:underline shrink-0 flex items-center gap-1 disabled:opacity-50"
                >
                  Inspect <ExternalLink size={12} />
                </button>
              </div>
              {details.current_state && (
                <p className="text-xs text-gray-400">Current state: <span className="font-mono text-gray-300">{details.current_state}</span></p>
              )}
            </div>
          )}

          {/* Evidence documents, only if there are real ones to show */}
          {evidenceDocs.length > 0 && (
            <div className="space-y-2">
              <span className="text-xs font-semibold uppercase tracking-wider text-gray-400 flex items-center gap-1.5">
                <Layers size={14} className="text-primary" />
                Evidence Documents
              </span>
              <div className="space-y-2">
                {evidenceDocs.map((doc) => (
                  <div key={doc.document_id} className="rounded-lg border border-border/80 bg-background/80 p-3 flex items-center justify-between gap-2">
                    <span className="text-xs font-medium text-gray-200 truncate flex items-center gap-1.5">
                      <FileText size={13} className="text-gray-400 shrink-0" />
                      {doc.filename}
                    </span>
                    <button
                      type="button"
                      onClick={() => handleInspectDocument(doc)}
                      disabled={viewerLoading}
                      className="text-[11px] text-primary hover:underline shrink-0 flex items-center gap-1 disabled:opacity-50"
                    >
                      Inspect <ExternalLink size={11} />
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Any other unresolved detail fields, shown plainly rather than
              silently dropped — still no invented content. */}
          {details.existing_unapproved_evidence?.length > 0 && (
            <div className="rounded-xl border border-border bg-background/80 p-4 space-y-1.5">
              <span className="text-xs font-semibold uppercase tracking-wider text-gray-400">
                Unapproved Evidence On File
              </span>
              <ul className="text-xs text-gray-300 space-y-1 list-disc list-inside">
                {details.existing_unapproved_evidence.map((name) => (
                  <li key={name}>{name}</li>
                ))}
              </ul>
            </div>
          )}

          {/* Knowledge Graph Neighborhood — real data, unchanged */}
          {neighborhood && (neighborhood.nodes?.length > 0 || neighborhood.edges?.length > 0) && (
            <div className="rounded-xl border border-border bg-surface/60 p-4 space-y-2.5">
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold uppercase tracking-wider text-gray-300 flex items-center gap-1.5">
                  <Layers size={14} className="text-indigo-400" />
                  Knowledge Graph Neighborhood
                </span>
                <span className="text-[11px] text-gray-400 font-mono">
                  {neighborhood.nodes?.length || 0} entities · {neighborhood.edges?.length || 0} relations
                </span>
              </div>
              <div className="flex flex-wrap gap-1.5">
                {neighborhood.nodes?.slice(0, 8).map((node) => (
                  <span
                    key={node.node_id}
                    className="inline-flex items-center gap-1 text-[11px] font-mono px-2 py-0.5 rounded bg-surface border border-border text-gray-300"
                  >
                    <span className="text-primary font-bold">{node.entity_type}</span>: {node.label || node.entity_name}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Ask Search Agent */}
          <div className="rounded-xl border border-border bg-background/70 p-4 space-y-3">
            <div className="flex items-center justify-between gap-2">
              <span className="min-w-0 text-xs font-semibold uppercase tracking-wider text-gray-300 flex items-center gap-1.5">
                <Sparkles size={14} className="text-primary shrink-0" />
                <span className="truncate">Ask the Search Agent</span>
              </span>
              <Badge variant="neutral">Read-only</Badge>
            </div>
            <p className="text-xs text-gray-400">
              Sends the question below to the Search Agent right away and shows the answer there
              {affectedDoc ? <> — scoped to <span className="text-gray-300 font-medium">{affectedDoc.filename}</span></> : null}.
            </p>
            <div className="space-y-1.5 pt-1">
              {suggestedQueries.map((queryText, idx) => (
                <button
                  key={idx}
                  type="button"
                  onClick={() => handleAskQueryAgent(queryText)}
                  className="w-full flex items-center justify-between rounded-lg border border-border bg-surface px-3 py-2 text-xs font-medium text-gray-300 hover:border-primary/50 hover:bg-surface-hover hover:text-gray-100 transition-colors text-left"
                >
                  <span className="truncate pr-2">"{queryText}"</span>
                  <ChevronRight size={14} className="text-gray-500 shrink-0" />
                </button>
              ))}
            </div>
            <div className="pt-1">
              <Button
                variant="secondary"
                size="sm"
                className="w-full"
                icon={HelpCircle}
                onClick={handleAskCustomQuestion}
              >
                Write my own question instead
              </Button>
            </div>
          </div>

          {/* Finding ID footer line — informational only, no duplicate close button */}
          <div className="flex items-center justify-between text-xs text-gray-500 font-mono pt-1">
            <span className="flex items-center gap-1.5">
              <ShieldAlert size={12} />
              Finding ID: {finding.finding_id || '—'}
            </span>
            {finding.finding_fingerprint && (
              <span className="text-[10px] text-gray-600 font-mono">
                FP: {finding.finding_fingerprint.slice(0, 10)}...
              </span>
            )}
          </div>
        </div>
      </Modal>

      {/* Dismissal Rationale Modal */}
      <Modal
        open={showDismissModal}
        onClose={() => {
          if (!dismissBusy) {
            setShowDismissModal(false);
            setDismissError('');
          }
        }}
        title="Dismiss Advisory Finding"
      >
        <div className="space-y-4">
          <p className="text-xs text-gray-300 leading-relaxed">
            Dismissing an advisory finding records human review in the append-only audit log and marks this finding as dismissed in project intelligence.
            Underlying document content and readiness calculations remain unmutated.
          </p>

          <div className="rounded-lg border border-border bg-surface/60 p-3 text-xs space-y-1">
            <div className="text-gray-400 font-medium uppercase tracking-wider text-[10px]">Target Finding</div>
            <div className="text-gray-200 font-semibold">{finding.title}</div>
            <div className="text-gray-400 font-mono text-[11px]">{finding.rule_code} · {getRuleCategory(finding.rule_code)}</div>
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-medium text-gray-300">
              Reason / Substantive Rationale <span className="text-red-400">*</span>
            </label>
            <textarea
              rows={3}
              value={dismissReason}
              onChange={(e) => {
                setDismissReason(e.target.value);
                if (dismissError) setDismissError('');
              }}
              placeholder="E.g. Documented false positive: section repetitions are intentional for executive summary and appendix..."
              className="w-full rounded-lg border border-border bg-surface p-2.5 text-xs text-gray-200 placeholder-gray-500 focus:border-primary focus:outline-none resize-none"
            />
            {dismissError && (
              <p className="text-xs text-red-400">{dismissError}</p>
            )}
            <p className="text-[11px] text-gray-500">
              Minimum 5 characters. This rationale will be permanently recorded in the audit trail.
            </p>
          </div>

          <div className="flex items-center justify-end gap-2 pt-2 border-t border-border">
            <Button
              variant="secondary"
              size="sm"
              disabled={dismissBusy}
              onClick={() => {
                setShowDismissModal(false);
                setDismissError('');
              }}
            >
              Cancel
            </Button>
            <Button
              variant="primary"
              size="sm"
              loading={dismissBusy}
              onClick={handleDismiss}
            >
              Confirm Dismissal
            </Button>
          </div>
        </div>
      </Modal>

      <MarkdownViewer
        open={Boolean(viewerDoc)}
        onClose={() => setViewerDoc(null)}
        title={viewerDoc?.title}
        subtitle={viewerDoc?.subtitle}
        content={viewerDoc?.content || ''}
      />
    </>
  );
};

export default AuditFindingDrawer;
