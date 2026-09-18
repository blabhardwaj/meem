import React, { useEffect, useState } from 'react';
import { AlertTriangle, Copy, FileText, ShieldAlert, X } from 'lucide-react';
import Badge from '../ui/Badge';
import MarkdownMessage from '../ui/MarkdownMessage';
import { documentsApi } from '../../lib/api';
import { sensitivityLabel } from '../../constants/docTypes';

const stateVariant = {
  draft: 'neutral',
  pending_review: 'warning',
  approved: 'success',
  rejected: 'danger',
};

// 20-pt-per-criterion thresholds, matching the PER_CRITERION_MINIMUM=8 floor
// (Master Plan v2, item 4 — not yet shipped, but the display convention is
// agreed: red below 8, amber 8-13, green 14+).
const criterionColor = (score) => (score < 8 ? 'text-red-400' : score < 14 ? 'text-amber-400' : 'text-emerald-400');
const criterionBar = (score) => (score < 8 ? 'bg-red-500' : score < 14 ? 'bg-amber-400' : 'bg-emerald-500');

export const CriterionChip = ({ name, score, maxScore = 20 }) => (
  <div className="flex items-center gap-2">
    <span className="text-xs text-gray-400 w-32 truncate shrink-0">{name.replace(/_/g, ' ')}</span>
    <div className="flex-1 h-1.5 rounded-full bg-surface overflow-hidden">
      <div className={`h-full rounded-full ${criterionBar(score)}`} style={{ width: `${Math.min(100, (score / maxScore) * 100)}%` }} />
    </div>
    <span className={`text-xs font-mono w-10 text-right shrink-0 ${criterionColor(score)}`}>{score}/{maxScore}</span>
  </div>
);

// Master Plan v2, item 10: the document-level coherence check — catches a
// contradiction, duplicate, or unmet requirement in THIS document's content
// against the rest of the project, which the Scanner (structure only) and
// the project-wide audit (R001-R010, no single-document content check) both
// miss. Shown wherever scan results are shown, per the plan's own
// instruction not to bury this in the separate Intelligence page.
const ISSUE_ICON = { contradiction: ShieldAlert, unmet_requirement: AlertTriangle, duplicate: Copy };
const ISSUE_LABEL = { contradiction: 'Contradiction', unmet_requirement: 'Unmet requirement', duplicate: 'Possible duplicate' };
const ISSUE_COLOR = { contradiction: 'text-red-400', unmet_requirement: 'text-red-400', duplicate: 'text-amber-400' };

export const CoherenceIssuesPanel = ({ issues }) => {
  if (!issues || issues.length === 0) return null;
  return (
    <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 space-y-2.5">
      <span className="text-xs font-medium text-gray-300">Content Coherence Check</span>
      {issues.map((issue, idx) => {
        const Icon = ISSUE_ICON[issue.type] || AlertTriangle;
        const color = ISSUE_COLOR[issue.type] || 'text-amber-400';
        return (
          <div key={idx} className="flex items-start gap-2">
            <Icon size={13} className={`mt-0.5 shrink-0 ${color}`} />
            <div className="min-w-0">
              <p className={`text-xs font-medium ${color}`}>{ISSUE_LABEL[issue.type] || issue.type}</p>
              <p className="text-xs text-gray-300 mt-0.5">{issue.description}</p>
              {issue.related_context && (
                <p className="text-[11px] text-gray-500 mt-1 italic truncate" title={issue.related_context}>
                  Related: {issue.related_context}
                </p>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
};

// Master Plan v2, item 2: the document viewer. Previously nothing in the app
// rendered a document's actual content — this is the single place that does,
// reused by both the Sources panel filename click and the approval review
// modal (item 3).
const DocumentViewerModal = ({ documentId, onClose }) => {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    setData(null);
    documentsApi.view(documentId)
      .then((result) => { if (!cancelled) setData(result); })
      .catch((err) => { if (!cancelled) setError(err.message || 'Could not load this document.'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [documentId]);

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-[90] flex items-center justify-center p-4">
      <div className="fixed inset-0 bg-background/80 backdrop-blur-sm" onClick={onClose} />
      <div className="relative z-[91] flex w-full max-w-3xl max-h-[85vh] flex-col rounded-xl border border-border bg-surface shadow-2xl">
        <div className="flex items-start justify-between gap-3 border-b border-border/50 p-4">
          <div className="flex items-start gap-3 min-w-0">
            <div className="mt-0.5 rounded-lg bg-background p-2 text-primary shrink-0">
              <FileText size={18} />
            </div>
            <div className="min-w-0">
              <h2 className="text-base font-semibold text-gray-100 truncate">{data?.filename || 'Document'}</h2>
              <div className="flex items-center gap-2 mt-1.5 flex-wrap">
                {data && <Badge variant="neutral">v{data.version_number}</Badge>}
                {data && <Badge variant="neutral">{sensitivityLabel(data.sensitivity_level)}</Badge>}
                {data?.workflow_state && (
                  <Badge variant={stateVariant[data.workflow_state] || 'neutral'}>
                    {data.workflow_state.replace('_', ' ')}
                  </Badge>
                )}
              </div>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded-md p-2 text-gray-400 hover:text-gray-100 hover:bg-surface-hover transition-colors shrink-0"
          >
            <X size={18} />
          </button>
        </div>

        <div className="overflow-y-auto scrollbar-thin p-6 space-y-5">
          {loading && <p className="text-sm text-gray-500">Loading...</p>}
          {error && <p className="text-sm text-red-400">{error}</p>}

          {data && (
            <>
              {data.scan_criteria && data.scan_criteria.length > 0 && (
                <div className="rounded-lg border border-border/60 bg-background/50 p-3 space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-medium text-gray-300">Structure Scanner</span>
                    <span className="text-xs font-mono text-gray-400">
                      {data.scan_overall_score}/{data.scan_criteria.length * 20}
                    </span>
                  </div>
                  {data.scan_criteria.map((c) => (
                    <CriterionChip key={c.name} name={c.name} score={c.score} />
                  ))}
                  {data.injection_flagged && (
                    <p className="flex items-center gap-1.5 text-xs text-red-400 mt-1">
                      <AlertTriangle size={12} /> Flagged by the Injection Scanner — needs human review.
                    </p>
                  )}
                  {data.scan_passed === false && !data.injection_flagged && (
                    <p className="text-xs text-amber-400 mt-1">Below the quality threshold.</p>
                  )}
                </div>
              )}

              <CoherenceIssuesPanel issues={data.coherence_issues} />

              <MarkdownMessage content={data.content_markdown} />
            </>
          )}
        </div>
      </div>
    </div>
  );
};

export default DocumentViewerModal;
