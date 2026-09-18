import React, { useState, useEffect } from 'react';
import {
  ShieldAlert,
  FileText,
  ExternalLink,
  ChevronRight,
  Sparkles,
  Layers,
  HelpCircle,
} from 'lucide-react';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import Modal from '../ui/Modal';
import MarkdownViewer from '../ui/MarkdownViewer';
import { intelligenceApi, documentsApi } from '../../lib/api';

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
}) => {
  const [viewerDoc, setViewerDoc] = useState(null);
  const [viewerLoading, setViewerLoading] = useState(false);
  const [neighborhood, setNeighborhood] = useState(null);

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
  const evidenceDocs = evidenceSources
    .map((src) => {
      const docId = typeof src === 'string' ? src : src?.document_id;
      return documents.find((d) => d.document_id === docId) || null;
    })
    .filter(Boolean);

  const handleInspectDocument = async (doc) => {
    if (!doc) return;
    setViewerLoading(true);
    try {
      const data = await documentsApi.view(doc.document_id);
      setViewerDoc({
        title: doc.filename,
        subtitle: `${stageName} · Current version`,
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

  const suggestedQueries = [
    `How can I resolve this finding (${finding.rule_code}: ${finding.title})?`,
    `What evidence caused finding ${finding.rule_code}?`,
    affectedDoc ? `Can this document be approved: ${affectedDoc.filename}?` : `What should I do about ${finding.rule_code}?`,
  ];

  const handleAskQueryAgent = (queryText) => {
    onClose();
    onAskSearchAgent?.(queryText);
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
            <span className="text-xs text-gray-400 font-mono">Stage: {stageName}</span>
          </div>

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
                  handleAskQueryAgent(`Explain why ${finding.rule_code} (${finding.title}) is flagged in this project.`)
                }
              >
                Ask Custom Question
              </Button>
            </div>
          </div>

          {/* Finding ID footer line — informational only, no duplicate close button */}
          <div className="flex items-center justify-between text-xs text-gray-500 font-mono pt-1">
            <span className="flex items-center gap-1.5">
              <ShieldAlert size={12} />
              Finding ID: {finding.finding_id || '—'}
            </span>
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
