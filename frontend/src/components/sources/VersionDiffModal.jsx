import React, { useState } from 'react';
import { AlertTriangle, ChevronDown, FileDiff, FileText, Send, ShieldAlert, X } from 'lucide-react';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import Textarea from '../ui/Textarea';
import MarkdownMessage from '../ui/MarkdownMessage';
import { CriterionChip } from './DocumentViewerModal';
import { documentsApi } from '../../lib/api';

// Master Plan v2, item 12: the version diff/review gate. Re-uploading an
// existing document never silently replaces it — this shows the real diff
// against the current version, the initial scan, and a chat-style loop
// (revise further, or finalize) before a new DocumentVersion is written.
export const DiffStats = ({ diff }) => {
  if (!diff) return null;
  if (!diff.has_changes) {
    return <p className="text-sm text-gray-400">This file is identical to the current version — no changes detected.</p>;
  }
  return (
    <div className="rounded-lg border border-border/60 bg-background/50 overflow-hidden">
      <div className="flex items-center justify-between px-3 py-2 border-b border-border/50">
        <span className="flex items-center gap-1.5 text-xs font-medium text-gray-300">
          <FileDiff size={13} /> Changes vs. current version
        </span>
        <span className="text-xs font-mono">
          <span className="text-emerald-400">+{diff.added_lines}</span>{' '}
          <span className="text-red-400">-{diff.removed_lines}</span>
        </span>
      </div>
      <pre className="max-h-64 overflow-auto p-3 text-xs font-mono leading-relaxed whitespace-pre-wrap">
        {diff.unified_diff.split('\n').map((line, i) => {
          let color = 'text-gray-400';
          if (line.startsWith('+') && !line.startsWith('+++')) color = 'text-emerald-400';
          else if (line.startsWith('-') && !line.startsWith('---')) color = 'text-red-400';
          else if (line.startsWith('@@')) color = 'text-primary-light';
          return <div key={i} className={color}>{line}</div>;
        })}
      </pre>
    </div>
  );
};

const ScanPanel = ({ scan, scanError, injectionFlagged, failedCriteria }) => {
  if (!scan && !scanError) return null;
  return (
    <div className="rounded-lg border border-border/60 bg-background/50 p-3 space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-gray-300">Structure Scanner</span>
        {scan && <span className="text-xs font-mono text-gray-400">{scan.overall_score}/60</span>}
      </div>
      {scan?.criteria?.map((c) => <CriterionChip key={c.name} name={c.name} score={c.score} />)}
      {scanError && <p className="text-xs text-red-400">{scanError}</p>}
      {failedCriteria?.length > 0 && (
        <p className="text-xs text-amber-400">Below the minimum on: {failedCriteria.join(', ')}.</p>
      )}
      {injectionFlagged && (
        <p className="flex items-center gap-1.5 text-xs text-red-400">
          <AlertTriangle size={12} /> Flagged by the Injection Scanner — needs human review.
        </p>
      )}
    </div>
  );
};

const VersionDiffModal = ({ document, initialOutcome, onClose, onFinalized }) => {
  // UI_FIXES_2026-09-15.md: this modal now also opens for "edit the current
  // document via chat" (documentsApi.startEdit), not only for a re-uploaded
  // file — distinguished by whether the initial outcome has diff/scan data.
  const isEditingCurrent = initialOutcome.diff === null && initialOutcome.scan === null;
  const [sessionId] = useState(initialOutcome.session_id);
  // The document's current working text — shown live so the user can see
  // exactly what they're editing, not just the diff of what changed.
  const [content, setContent] = useState(initialOutcome.content || '');
  const [contentOpen, setContentOpen] = useState(true);
  const [diff, setDiff] = useState(initialOutcome.diff);
  const [scan, setScan] = useState(initialOutcome.scan);
  const [scanError, setScanError] = useState(initialOutcome.scan_error);
  const [injectionFlagged, setInjectionFlagged] = useState(initialOutcome.injection_flagged);
  const [failedCriteria, setFailedCriteria] = useState(initialOutcome.failed_criteria || []);
  const [messages, setMessages] = useState([{ role: 'assistant', content: initialOutcome.reply }]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [finalized, setFinalized] = useState(null);

  const send = async (text) => {
    if (busy) return; // BUGFIXES_2026-09-15.md: the Enter-key path had no busy guard
    if (!text.trim() && !finalized) return;
    setBusy(true);
    setError('');
    setMessages((cur) => [...cur, { role: 'user', content: text }]);
    setInput('');
    try {
      const result = await documentsApi.versionReviewMessage(document.document_id, sessionId, text);
      setMessages((cur) => [...cur, { role: 'assistant', content: result.reply }]);
      if (result.content !== null && result.content !== undefined) setContent(result.content);
      if (result.diff) setDiff(result.diff);
      if (result.scan !== undefined) setScan(result.scan);
      setScanError(result.scan_error);
      if (result.injection_flagged !== null && result.injection_flagged !== undefined) {
        setInjectionFlagged(result.injection_flagged);
      }
      if (result.failed_criteria) setFailedCriteria(result.failed_criteria);
      if (result.finalized) {
        setFinalized(result);
        onFinalized?.();
      }
    } catch (err) {
      setError(err.message || 'Something went wrong.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[90] flex items-center justify-center p-4">
      <div className="fixed inset-0 bg-background/80 backdrop-blur-sm" onClick={onClose} />
      <div className="relative z-[91] flex w-full max-w-2xl max-h-[85vh] flex-col rounded-xl border border-border bg-surface shadow-2xl">
        <div className="flex items-start justify-between gap-3 border-b border-border/50 p-4">
          <div>
            <h2 className="text-base font-semibold text-gray-100">{isEditingCurrent ? 'Edit document' : 'Review new version'}</h2>
            <p className="text-xs text-gray-500 mt-1 truncate">{document.filename}</p>
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

        <div className="overflow-y-auto scrollbar-thin p-5 space-y-4 flex-1">
          {content && (
            <div className="rounded-lg border border-border/60 bg-background/50 overflow-hidden">
              <button
                type="button"
                onClick={() => setContentOpen((v) => !v)}
                className="w-full flex items-center justify-between px-3 py-2 border-b border-border/50 hover:bg-surface-hover transition-colors"
              >
                <span className="flex items-center gap-1.5 text-xs font-medium text-gray-300">
                  <FileText size={13} /> {finalized ? 'Finalized content' : 'Current document'}
                </span>
                <ChevronDown size={14} className={`text-gray-500 transition-transform ${contentOpen ? 'rotate-180' : ''}`} />
              </button>
              {contentOpen && (
                <div className="max-h-64 overflow-auto p-3">
                  <MarkdownMessage content={content} className="text-xs" />
                </div>
              )}
            </div>
          )}
          <DiffStats diff={diff} />
          <ScanPanel scan={scan} scanError={scanError} injectionFlagged={injectionFlagged} failedCriteria={failedCriteria} />

          <div className="space-y-3">
            {messages.map((m, i) => (
              <div key={i} className={`text-sm rounded-lg p-3 ${m.role === 'user' ? 'bg-primary/10 text-gray-100 ml-8' : 'bg-background/60 text-gray-300 mr-8'}`}>
                {m.role === 'user' ? (
                  <p className="whitespace-pre-wrap">{m.content}</p>
                ) : (
                  <MarkdownMessage content={m.content} />
                )}
              </div>
            ))}
          </div>

          {finalized && (
            <div className={`rounded-lg border p-3 ${finalized.status === 'indexed' ? 'border-emerald-500/30 bg-emerald-500/5' : 'border-amber-500/30 bg-amber-500/5'}`}>
              <p className="text-sm text-gray-200">
                Finalized as v{finalized.version_number} — status <Badge variant={finalized.status === 'indexed' ? 'success' : 'warning'}>{finalized.status}</Badge>
              </p>
              {finalized.workflow_reset && (
                <p className="flex items-center gap-1.5 text-xs text-amber-400 mt-2">
                  <ShieldAlert size={12} /> This document's prior approval was reset to draft — it needs to be re-approved.
                </p>
              )}
            </div>
          )}

          {error && <p className="text-sm text-red-400">{error}</p>}
        </div>

        {!finalized && (
          <div className="flex items-end gap-2 p-4 border-t border-border/50 bg-background/50 shrink-0">
            <div className="flex-1">
              <Textarea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                placeholder="Describe a change, or say it looks good to finalize..."
                onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(input); } }}
              />
            </div>
            <Button icon={Send} onClick={() => send(input)} loading={busy} disabled={!input.trim()}>
              Send
            </Button>
          </div>
        )}
        {finalized && (
          <div className="flex justify-end p-4 border-t border-border/50 bg-background/50 shrink-0">
            <Button variant="secondary" onClick={onClose}>Close</Button>
          </div>
        )}
      </div>
    </div>
  );
};

export default VersionDiffModal;
