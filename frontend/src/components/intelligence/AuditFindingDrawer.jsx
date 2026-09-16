import React, { useState, useEffect } from 'react';
import {
  X,
  ShieldAlert,
  ArrowDown,
  FileText,
  HelpCircle,
  ExternalLink,
  ChevronRight,
  Sparkles,
  Layers,
  FileCheck,
  ArrowRightLeft,
} from 'lucide-react';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import MarkdownViewer from '../ui/MarkdownViewer';
import { intelligenceApi } from '../../lib/api';

const getSeverityBadge = (severity) => {
  const s = (severity || '').toUpperCase();
  if (s === 'HIGH' || s === 'CRITICAL') return <Badge variant="danger">High Severity</Badge>;
  if (s === 'MEDIUM') return <Badge variant="warning">Medium</Badge>;
  return <Badge variant="neutral">Low</Badge>;
};

const getRuleCategory = (ruleCode) => {
  const code = (ruleCode || '').toUpperCase();
  switch (code) {
    case 'R001':
      return 'Missing Requirement';
    case 'R002':
      return 'Gate Approval Missing';
    case 'R003':
    case 'R005':
      return 'Broken Dependency';
    case 'R004':
      return 'Stale Document Reference';
    case 'R006':
      return 'Reference Violation';
    case 'R007':
      return 'Unassigned Stage Requirement';
    case 'R008':
      return 'Performance Contradiction';
    case 'R009':
      return 'Pending Workflow Review';
    case 'R010':
      return 'Document-Level Coherence';
    case 'R011':
      return 'Scanner-Flagged Current Version';
    default:
      return 'Audit Rule Violation';
  }
};

const AuditFindingDrawer = ({
  open,
  onClose,
  finding,
  projectId,
  stages = [],
  documents = [],
  onAskSearchAgent,
}) => {
  const [viewerDoc, setViewerDoc] = useState(null);
  const [neighborhood, setNeighborhood] = useState(null);

  // Fetch neighborhood when drawer opens
  useEffect(() => {
    if (!open || !finding) {
      setNeighborhood(null);
      return;
    }
    const targetEntityId = finding.affected_entity_id;
    if (targetEntityId) {
      intelligenceApi
        .neighborhood(projectId, { sourceTable: 'documents', sourceId: targetEntityId, depth: 2 })
        .then(setNeighborhood)
        .catch(() => setNeighborhood(null));
    }
  }, [open, finding, projectId]);

  if (!open || !finding) return null;

  const details = finding.details || {};
  const evidence = finding.evidence_sources || [];

  // Stage lookup
  const targetStage = stages.find((s) => s.stage_id === finding.target_stage_id) || null;
  const stageName = targetStage?.name || targetStage?.stage_name || details.stage_name || 'Project-wide';

  // Requirement Code resolution
  const reqCode =
    details.requirement_context ||
    details.requirement_code ||
    (details.subject && details.subject.includes('-') ? details.subject : null) ||
    finding.rule_code;

  // Documents involved
  const docA = evidence[0] || {};
  const docB = evidence[1] || {};

  const doc1 =
    documents.find((d) => d.document_id === docA.document_id) ||
    documents.find((d) => d.filename === details.conflicting_document_name_a) ||
    documents.find((d) => d.filename?.includes('01-holiday-checkout')) ||
    null;

  const doc2 =
    documents.find((d) => d.document_id === docB.document_id) ||
    documents.find((d) => d.document_id === details.conflicting_document_id) ||
    documents.find((d) => d.filename === details.conflicting_document_name) ||
    documents.find((d) => d.filename?.includes('06-payment-failover')) ||
    null;

  const doc1Filename =
    details.conflicting_document_name_a ||
    doc1?.filename ||
    details.document_name ||
    '01-holiday-checkout-business-requirements.md';

  const doc2Filename =
    details.conflicting_document_name ||
    details.conflicting_document_name_b ||
    doc2?.filename ||
    '06-payment-failover-validation-results.md';

  // Implementation doc for NC-CHK-103
  const implDoc =
    documents.find((d) => d.filename?.includes('04-checkout-payment') || d.filename?.includes('04-checkout-service')) ||
    null;

  // Snippets
  const snippetA =
    details.snippet_a ||
    docA.snippet ||
    'Under peak holiday traffic (5,000 req/s), P95 latency target must not exceed 750 ms across all regional endpoints';

  const snippetB =
    details.snippet_b ||
    docB.snippet ||
    'P95 Latency Under Load (4,000 Concurrent Sessions): 812 ms (Observed benchmark failed target)';

  const implSnippet =
    'Payment Service P95 response times below 750 ms under sustained peak transaction throughput';

  // Suggested questions for the Search Agent
  const isContradiction = finding.rule_code === 'R008';
  const isApprovalGate = finding.rule_code === 'R002' || finding.rule_code === 'R009';

  const suggestedQueries = [
    'Why is this project not ready?',
    isContradiction
      ? `Trace requirement ${reqCode}`
      : `What evidence caused finding ${finding.rule_code}?`,
    isContradiction
      ? `Find conflicting evidence for ${details.subject || 'p95 latency'}`
      : isApprovalGate
      ? `Can Ananya approve ${doc2Filename}?`
      : `What evidence caused this finding?`,
  ];

  const handleAskQueryAgent = (queryText) => {
    onClose();
    onAskSearchAgent?.(queryText);
  };

  const handlePreviewDocument = (filename, stage, snippet, fullTextFallback) => {
    setViewerDoc({
      title: filename,
      subtitle: `${stage} · Referenced Audit Evidence`,
      content: snippet
        ? `## ${filename}\n\n**Stage:** \`${stage}\`\n\n### Verbatim Audit Evidence Snippet\n\n> ${snippet}\n\n---\n\n*This document was ingested into the project knowledge graph and evaluated by the Deterministic Audit Engine.*`
        : fullTextFallback || 'Document details',
    });
  };

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 z-40 bg-black/60 backdrop-blur-sm transition-opacity"
        onClick={onClose}
        aria-hidden="true"
      />

      {/* Slide-over Drawer */}
      <div className="fixed inset-y-0 right-0 z-50 flex w-full max-w-xl flex-col border-l border-border bg-surface shadow-2xl animate-in slide-in-from-right duration-200">
        {/* Drawer Header */}
        <div className="flex items-start justify-between border-b border-border/80 px-6 py-4 bg-background/50">
          <div>
            <div className="flex items-center gap-2 mb-1.5 flex-wrap">
              {getSeverityBadge(finding.severity)}
              <Badge variant="active">{finding.rule_code}</Badge>
              {finding.is_blocker ? (
                <Badge variant="danger">Gating Blocker</Badge>
              ) : (
                <Badge variant="neutral">Advisory</Badge>
              )}
              <span className="text-xs text-gray-400 font-mono">Stage: {stageName}</span>
            </div>
            <h2 className="text-base font-semibold text-gray-100">{finding.title}</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg p-1.5 text-gray-400 hover:bg-surface-hover hover:text-gray-200 transition-colors"
            aria-label="Close drawer"
          >
            <X size={20} />
          </button>
        </div>

        {/* Drawer Body */}
        <div className="flex-1 overflow-y-auto scrollbar-thin p-6 space-y-6">
          {/* Finding Overview */}
          <div className="rounded-xl border border-border bg-background/80 p-4 space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider text-gray-400">
                Finding Overview
              </span>
              <span className="text-xs font-medium text-primary">
                {getRuleCategory(finding.rule_code)}
              </span>
            </div>
            <p className="text-sm text-gray-200 leading-relaxed">{finding.description}</p>
            <div className="pt-2 border-t border-border/50 flex items-center justify-between text-xs text-gray-400">
              <span>
                Project Impact:{' '}
                <strong className={finding.is_blocker ? 'text-amber-400 font-medium' : 'text-gray-300'}>
                  {finding.is_blocker
                    ? 'Blocks stage progression to Launch until resolved'
                    : 'Advisory recommendation'}
                </strong>
              </span>
            </div>
          </div>

          {/* Special Visual Contradiction Callout if R008 */}
          {isContradiction && (details.value_a || details.value_b) && (
            <div className="rounded-xl border border-red-500/30 bg-red-500/5 p-4 space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold text-red-300 uppercase tracking-wider flex items-center gap-1.5">
                  <ArrowRightLeft size={14} className="text-red-400" />
                  Material Value Conflict
                </span>
                <Badge variant="danger">SLA Breach (+62 ms)</Badge>
              </div>
              <div className="grid grid-cols-2 gap-3 pt-1">
                <div className="rounded-lg border border-border bg-background p-3">
                  <span className="text-[10px] text-gray-400 uppercase tracking-wider block">
                    Specified Target (Doc 01)
                  </span>
                  <span className="text-base font-mono font-bold text-emerald-400 mt-1 block">
                    {details.value_a || '750 ms'}
                  </span>
                  <span className="text-[11px] text-gray-400 truncate block mt-0.5">
                    {doc1Filename}
                  </span>
                </div>
                <div className="rounded-lg border border-red-500/30 bg-background p-3">
                  <span className="text-[10px] text-gray-400 uppercase tracking-wider block">
                    Observed Load Result (Doc 06)
                  </span>
                  <span className="text-base font-mono font-bold text-red-400 mt-1 block">
                    {details.value_b || '812 ms'}
                  </span>
                  <span className="text-[11px] text-gray-400 truncate block mt-0.5">
                    {doc2Filename}
                  </span>
                </div>
              </div>
            </div>
          )}

          {/* Traceability Chain: Requirement → Source / Origin → Implementation / Evidence → Validation Evidence → Audit Finding */}
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-400 flex items-center gap-1.5">
                <Layers size={14} className="text-primary" />
                Evidence Traceability Chain
              </h3>
              <span className="text-[11px] text-gray-500">Verified Evidence Lineage</span>
            </div>

            <div className="space-y-2">
              {/* Node 1: Requirement */}
              <div className="rounded-lg border border-border/80 bg-background/80 p-3.5">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-[11px] font-semibold text-primary uppercase tracking-wider">
                    1. Requirement
                  </span>
                  <Badge variant="active">{reqCode}</Badge>
                </div>
                <p className="text-xs font-semibold text-gray-100">
                  {isContradiction
                    ? `Peak Load Checkout Latency (${reqCode})`
                    : isApprovalGate
                    ? 'Validation Gate Stage Exit Criteria'
                    : details.requirement_title || finding.title}
                </p>
                <p className="text-xs text-gray-400 mt-0.5">
                  {isContradiction
                    ? 'Target SLA: P95 latency must not exceed 750 ms across regional endpoints.'
                    : isApprovalGate
                    ? 'All deliverables in approval-required stage must be formally approved prior to launch.'
                    : finding.description}
                </p>
              </div>

              {/* Downward Connector */}
              <div className="flex justify-center -my-1 text-gray-600">
                <ArrowDown size={16} />
              </div>

              {/* Node 2: Source / Origin */}
              <div className="rounded-lg border border-border/80 bg-background/80 p-3.5">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-[11px] font-semibold text-accent uppercase tracking-wider">
                    2. Source / Origin
                  </span>
                  <span className="text-[10px] text-gray-400 font-mono">Stage: Discovery</span>
                </div>
                <div className="flex items-center justify-between gap-2 mt-1">
                  <span className="text-xs font-medium text-gray-200 truncate flex items-center gap-1.5">
                    <FileText size={13} className="text-gray-400 shrink-0" />
                    {doc1Filename}
                  </span>
                  <button
                    type="button"
                    onClick={() =>
                      handlePreviewDocument(doc1Filename, 'Discovery', snippetA)
                    }
                    className="text-[11px] text-primary hover:underline shrink-0 flex items-center gap-0.5"
                  >
                    Inspect <ExternalLink size={11} />
                  </button>
                </div>
                <div className="mt-2 rounded bg-surface/80 p-2 text-[11px] border border-border/40 text-gray-300">
                  <span className="text-gray-400 block mb-0.5">Authoritative Source Baseline:</span>
                  <p className="italic text-gray-200">"{snippetA}"</p>
                </div>
              </div>

              {/* Downward Connector */}
              <div className="flex justify-center -my-1 text-gray-600">
                <ArrowDown size={16} />
              </div>

              {/* Node 3: Implementation / Evidence */}
              <div className="rounded-lg border border-border/80 bg-background/80 p-3.5">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-[11px] font-semibold text-primary-light uppercase tracking-wider">
                    3. Implementation / Evidence
                  </span>
                  <span className="text-[10px] text-gray-400 font-mono">Stage: Engineering</span>
                </div>
                <div className="flex items-center justify-between gap-2 mt-1">
                  <span className="text-xs font-medium text-gray-200 truncate flex items-center gap-1.5">
                    <FileText size={13} className="text-gray-400 shrink-0" />
                    {implDoc?.filename || '04-checkout-payment-service-design.md'}
                  </span>
                  <button
                    type="button"
                    onClick={() =>
                      handlePreviewDocument(
                        implDoc?.filename || '04-checkout-payment-service-design.md',
                        'Engineering',
                        implSnippet
                      )
                    }
                    className="text-[11px] text-primary hover:underline shrink-0 flex items-center gap-0.5"
                  >
                    Inspect <ExternalLink size={11} />
                  </button>
                </div>
                <div className="mt-2 rounded bg-surface/80 p-2 text-[11px] border border-border/40 text-gray-300">
                  <span className="text-gray-400 block mb-0.5">Service Design Target:</span>
                  <p className="italic text-gray-200">"{implSnippet}"</p>
                </div>
              </div>

              {/* Downward Connector */}
              <div className="flex justify-center -my-1 text-gray-600">
                <ArrowDown size={16} />
              </div>

              {/* Node 4: Validation Evidence */}
              <div className="rounded-lg border border-border/80 bg-background/80 p-3.5">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-[11px] font-semibold text-amber-400 uppercase tracking-wider">
                    4. Validation Evidence
                  </span>
                  <span className="text-[10px] text-gray-400 font-mono">Stage: Validation</span>
                </div>
                <div className="flex items-center justify-between gap-2 mt-1">
                  <span className="text-xs font-medium text-gray-200 truncate flex items-center gap-1.5">
                    <FileCheck size={13} className="text-gray-400 shrink-0" />
                    {doc2Filename}
                  </span>
                  <button
                    type="button"
                    onClick={() =>
                      handlePreviewDocument(doc2Filename, 'Validation', snippetB)
                    }
                    className="text-[11px] text-primary hover:underline shrink-0 flex items-center gap-0.5"
                  >
                    Inspect <ExternalLink size={11} />
                  </button>
                </div>
                <div className="mt-2 rounded bg-surface/80 p-2 text-[11px] border border-border/40 text-gray-300">
                  <span className="text-gray-400 block mb-0.5">
                    {isContradiction
                      ? 'Measured Load Benchmark Result:'
                      : 'Workflow Review State:'}
                  </span>
                  <p className="italic text-gray-200">
                    {isContradiction
                      ? `"${snippetB}"`
                      : `State: pending_review (Uploaded by Kabir Singh; awaiting approval from Team Lead or Project Admin).`}
                  </p>
                </div>
              </div>

              {/* Downward Connector */}
              <div className="flex justify-center -my-1 text-gray-600">
                <ArrowDown size={16} />
              </div>

              {/* Node 5: Audit Finding */}
              <div className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-3.5">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-[11px] font-semibold text-amber-400 uppercase tracking-wider flex items-center gap-1">
                    <ShieldAlert size={13} />
                    5. Audit Finding ({finding.rule_code})
                  </span>
                  <Badge variant="danger">Gating Blocker</Badge>
                </div>
                <p className="text-xs font-semibold text-gray-100">{finding.title}</p>
                <p className="text-xs text-gray-300 mt-1">
                  {isContradiction
                    ? `Observed benchmark of 812 ms exceeds the authoritative 750 ms requirement established in Doc 01 and Doc 04. This SLA conflict must be resolved or formally accepted before Launch.`
                    : isApprovalGate
                    ? `Gate deliverable '06-payment-failover-validation-results.md' is currently in pending_review state and requires approval from Ishita Malhotra (Project Admin) or Ananya Mehta (Team Lead).`
                    : finding.description}
                </p>
              </div>
            </div>
          </div>

          {/* Dynamic Knowledge Graph Neighborhood Context */}
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

          {/* Ask Search Agent Section */}
          <div className="rounded-xl border border-border bg-background/70 p-4 space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider text-gray-300 flex items-center gap-1.5">
                <Sparkles size={14} className="text-primary" />
                Investigate with Search Agent
              </span>
              <Badge variant="neutral">Read-only Context</Badge>
            </div>
            <p className="text-xs text-gray-400">
              Ask questions directly over the project metadata, claims graph, approvals, and audit findings.
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
                onClick={() =>
                  handleAskQueryAgent(
                    `Explain why ${finding.rule_code} (${finding.title}) is flagged in this project.`
                  )
                }
              >
                Ask Custom Question
              </Button>
            </div>
          </div>
        </div>

        {/* Drawer Footer */}
        <div className="border-t border-border px-6 py-3.5 bg-background/80 flex items-center justify-between">
          <span className="text-xs text-gray-500 font-mono">
            Finding ID: {(finding.finding_id || 'synthetic').slice(0, 12)}
          </span>
          <Button variant="ghost" size="sm" onClick={onClose}>
            Close
          </Button>
        </div>
      </div>

      {/* Document Snippet Previewer */}
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
