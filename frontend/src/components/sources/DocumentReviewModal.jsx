import React, { useEffect, useState } from 'react';
import { AlertTriangle, FileText, ShieldAlert, ShieldCheck, X } from 'lucide-react';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import Textarea from '../ui/Textarea';
import MarkdownMessage from '../ui/MarkdownMessage';
import { CoherenceIssuesPanel, CriterionChip } from './DocumentViewerModal';
import { documentsApi } from '../../lib/api';
import { sensitivityLabel } from '../../constants/docTypes';

const stateVariant = {
  draft: 'neutral',
  pending_review: 'warning',
  approved: 'success',
  rejected: 'danger',
};

// Master Plan v2, item 3: approve/reject used to fire blind (no content, no
// scan score shown). This is the single place both decisions are made from
// now — content + scan result load first, and Approve is disabled client-side
// on a failed scan. The REAL gate is server-side (item 1's approve_document
// check) — canOverrideScan only controls whether this UI even offers the
// override path; the backend re-checks the caller's role regardless.
const DocumentReviewModal = ({ document: doc, canOverrideScan, onApprove, onReject, onClose }) => {
  const [data, setData] = useState(null);
  const [loadError, setLoadError] = useState('');
  const [loading, setLoading] = useState(true);
  // Which action is in flight, if any — tracked separately so only the
  // button actually clicked shows a spinner (previously a single shared
  // `busy` flag lit up both Approve and Reject together).
  const [busyAction, setBusyAction] = useState(null); // null | 'approve' | 'reject'
  const busy = busyAction !== null;
  const [actionError, setActionError] = useState('');
  const [reason, setReason] = useState('');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setLoadError('');
    documentsApi.view(doc.document_id)
      .then((result) => { if (!cancelled) setData(result); })
      .catch((err) => { if (!cancelled) setLoadError(err.message || 'Could not load this document.'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [doc.document_id]);

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  const scanFailed = data?.scan_passed === false;

  const runApprove = async (override) => {
    setBusyAction('approve');
    setActionError('');
    try {
      await onApprove(override);
    } catch (err) {
      setActionError(err.message || 'Approval failed.');
    } finally {
      setBusyAction(null);
    }
  };

  const handleApproveClick = () => {
    if (!scanFailed) {
      runApprove(false);
      return;
    }
    if (!canOverrideScan) return; // button is disabled in this state, defensive no-op
    if (window.confirm('This document has failing scan criteria. Approve anyway? This will be recorded in the audit log.')) {
      runApprove(true);
    }
  };

  const handleReject = async () => {
    if (!reason.trim()) return;
    setBusyAction('reject');
    setActionError('');
    try {
      await onReject(reason.trim());
    } catch (err) {
      setActionError(err.message || 'Rejection failed.');
    } finally {
      setBusyAction(null);
    }
  };

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
              <h2 className="text-base font-semibold text-gray-100 truncate">{doc.filename}</h2>
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

        <div className="overflow-y-auto scrollbar-thin p-6 space-y-5 flex-1">
          {loading && <p className="text-sm text-gray-500">Loading...</p>}
          {loadError && <p className="text-sm text-red-400">{loadError}</p>}

          {data && (
            <>
              {data.scan_criteria && data.scan_criteria.length > 0 && (
                <div className={`rounded-lg border p-3 space-y-2 ${scanFailed ? 'border-red-500/30 bg-red-500/5' : 'border-border/60 bg-background/50'}`}>
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
                  {scanFailed && (
                    <p className="flex items-center gap-1.5 text-xs text-red-400 mt-1">
                      <ShieldAlert size={12} /> This version did not pass the scan. Approval is blocked
                      {canOverrideScan ? ' unless overridden.' : '.'}
                    </p>
                  )}
                </div>
              )}

              <CoherenceIssuesPanel issues={data.coherence_issues} />

              <MarkdownMessage content={data.content_markdown} />

              <div className="pt-2 border-t border-border/50">
                <Textarea
                  label="Rejection reason"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="Explain what needs to change..."
                />
              </div>

              {actionError && <p className="text-sm text-red-400">{actionError}</p>}
            </>
          )}
        </div>

        <div className="flex items-center justify-end gap-3 p-4 border-t border-border/50 bg-background/50 shrink-0">
          <Button variant="ghost" onClick={onClose} disabled={busy}>Close</Button>
          <Button
            variant="danger"
            icon={X}
            onClick={handleReject}
            disabled={!reason.trim() || busy || loading}
            loading={busyAction === 'reject'}
          >
            Request changes
          </Button>
          <Button
            variant={scanFailed && canOverrideScan ? 'danger' : 'primary'}
            icon={scanFailed && canOverrideScan ? ShieldAlert : ShieldCheck}
            onClick={handleApproveClick}
            disabled={busy || loading || (scanFailed && !canOverrideScan)}
            loading={busyAction === 'approve'}
          >
            {scanFailed && canOverrideScan ? 'Override & approve' : 'Approve'}
          </Button>
        </div>
      </div>
    </div>
  );
};

export default DocumentReviewModal;
