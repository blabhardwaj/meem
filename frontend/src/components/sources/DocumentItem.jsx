import React, { useRef, useState } from 'react';
import { FileText, File, FileImage, FileSpreadsheet, Send, ClipboardCheck, History, Upload, Trash2, FileDiff, Eye, Pencil, X as XIcon } from 'lucide-react';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import Modal from '../ui/Modal';
import DocumentViewerModal from './DocumentViewerModal';
import DocumentReviewModal from './DocumentReviewModal';
import VersionDiffModal, { DiffStats } from './VersionDiffModal';
import Dropdown from '../ui/Dropdown';
import { documentsApi } from '../../lib/api';
import { sensitivityLabel } from '../../constants/docTypes';

const formatFileSize = (bytes) => {
  if (!bytes || bytes <= 0) return '0 KB';
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
};

const getFileIcon = (filename) => {
  const ext = (filename || '').split('.').pop()?.toLowerCase();
  switch (ext) {
    case 'pdf': return <FileText className="text-red-400" size={16} />;
    case 'doc':
    case 'docx': return <File className="text-blue-400" size={16} />;
    case 'ppt':
    case 'pptx': return <FileSpreadsheet className="text-amber-400" size={16} />;
    case 'png':
    case 'jpg':
    case 'jpeg': return <FileImage className="text-emerald-400" size={16} />;
    default: return <FileText className="text-gray-400" size={16} />;
  }
};

const stateVariant = {
  draft: 'neutral',
  pending_review: 'warning',
  approved: 'success',
  rejected: 'danger',
};

// DocumentVersion.status — Scanner-driven (structure score + injection
// check), fully independent of the document's human-approval WorkflowState
// (shown separately, above, as the "draft/pending_review/approved/rejected"
// badge). A version can be human-approved while its own Scanner status is
// needs_attention (e.g. a later re-scan of the same content scored
// differently) — the label/tooltip make that distinction explicit instead of
// the two badges silently contradicting each other with no explanation.
const versionStatusVariant = {
  indexed: 'success',
  pending_review: 'warning',
  needs_attention: 'danger',
};
const versionStatusLabel = {
  indexed: 'Scan passed',
  pending_review: 'Awaiting first scan',
  needs_attention: 'Scan flagged this version',
};
const versionStatusTooltip = {
  indexed: 'This version passed the Structure Scanner and injection check.',
  pending_review: "This version hasn't been through a finalize/scan pass yet.",
  needs_attention: 'The Structure Scanner or injection check flagged this version — separate from human approval.',
};

// A version with no version_number yet is unresolved (draft/pending
// review/needs_attention) or was rejected/superseded — it never became
// part of the document's real "v1, v2, v3..." approved lineage. See
// DocumentVersion's docstring (backend) for why the number itself is only
// ever assigned on approval now.
const versionDisplayLabel = (version) => {
  if (version.version_number != null) return `v${version.version_number}`;
  if (version.approval_outcome === 'rejected') return 'Rejected';
  return 'Draft';
};

const formatDate = (isoString) => {
  if (!isoString) return '';
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
};

const DocumentItem = ({ document, canReview, canOverrideScan = false, canDelete, onChanged, highlighted = false, canEdit = false }) => {
  // Which action is in flight, if any — a string key rather than a plain
  // boolean, so "Submit for review" and "Delete" (which can appear on the
  // same card at once) don't both spin when only one was clicked.
  const [busyAction, setBusyAction] = useState(null); // null | 'submit' | 'delete'
  const busy = busyAction !== null;
  const [actionError, setActionError] = useState('');
  const [versionsOpen, setVersionsOpen] = useState(false);
  const [versions, setVersions] = useState([]);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [versionFile, setVersionFile] = useState(null);
  const [versionBusy, setVersionBusy] = useState(false);
  const [versionError, setVersionError] = useState('');
  const versionInputRef = useRef(null);
  const [viewerOpen, setViewerOpen] = useState(false);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [diffReview, setDiffReview] = useState(null); // upload-version outcome, while VersionDiffModal is open
  // Two-version compare (pick "from"/"to" from the version list, diffed
  // against each other — separate from the "View" single-version reader,
  // and from VersionDiffModal's own current-vs-new-upload diff).
  const [compareOpen, setCompareOpen] = useState(false);
  const [compareFromId, setCompareFromId] = useState(null);
  const [compareToId, setCompareToId] = useState(null);
  const [compareDiff, setCompareDiff] = useState(null);
  const [compareLoading, setCompareLoading] = useState(false);
  const [compareError, setCompareError] = useState('');

  // Whole-document delete: type "delete" -> are-you-sure, two separate
  // confirm steps (SESSION_HANDOFF_2026-09-16.md's confirmed design).
  const [deleteStep, setDeleteStep] = useState(null); // null | 'type' | 'confirm'
  const [deleteTypedText, setDeleteTypedText] = useState('');
  const [deleteDocError, setDeleteDocError] = useState('');

  // Per-version delete: a single confirm popup on the version's own "X".
  const [versionToDelete, setVersionToDelete] = useState(null);
  const [versionDeleteBusy, setVersionDeleteBusy] = useState(false);
  const [versionDeleteError, setVersionDeleteError] = useState('');

  const state = document.workflow_state;

  const run = async (fn, actionKey) => {
    setBusyAction(actionKey);
    setActionError('');
    try {
      await fn();
      onChanged?.();
    } catch (err) {
      setActionError(err.message || 'Action failed.');
    } finally {
      setBusyAction(null);
    }
  };

  const openVersions = async () => {
    setVersionsOpen(true);
    setVersionsLoading(true);
    setVersionError('');
    setCompareOpen(false);
    setCompareFromId(null);
    setCompareToId(null);
    setCompareDiff(null);
    setCompareError('');
    try {
      setVersions(await documentsApi.versions(document.document_id));
    } catch (err) {
      setVersionError(err.message || 'Could not load version history.');
    } finally {
      setVersionsLoading(false);
    }
  };

  const uploadVersion = async () => {
    if (!versionFile) return;
    if (versionFile.size > 10 * 1024 * 1024) {
      setVersionError('Files must be 10 MB or smaller.');
      return;
    }
    setVersionBusy(true);
    setVersionError('');
    try {
      // Master Plan v2, item 12: this never silently creates the new
      // version — it returns a diff-review session; VersionDiffModal
      // handles the actual finalize (see onFinalized below).
      const outcome = await documentsApi.uploadVersion(document.document_id, versionFile);
      setVersionFile(null);
      if (versionInputRef.current) versionInputRef.current.value = '';
      setVersionsOpen(false);
      setDiffReview(outcome);
    } catch (err) {
      setVersionError(err.message || 'Could not upload this version.');
    } finally {
      setVersionBusy(false);
    }
  };

  const handleEdit = async () => {
    setBusyAction('edit');
    setActionError('');
    try {
      // UI_FIXES_2026-09-15.md: edit the current document via chat, no file
      // re-upload needed — reuses the same VersionDiffModal chat/finalize
      // flow as uploading a new version.
      const outcome = await documentsApi.startEdit(document.document_id);
      setDiffReview({ ...outcome, diff: null, scan: null, scan_error: null, injection_flagged: false, failed_criteria: [] });
    } catch (err) {
      setActionError(err.message || 'Could not start editing this document.');
    } finally {
      setBusyAction(null);
    }
  };

  const runCompare = async (fromId, toId) => {
    if (!fromId || !toId) return;
    setCompareDiff(null);
    setCompareError('');
    setCompareLoading(true);
    try {
      const result = await documentsApi.versionsDiff(document.document_id, fromId, toId);
      setCompareDiff(result);
    } catch (err) {
      setCompareError(err.message || 'Could not compare these versions.');
    } finally {
      setCompareLoading(false);
    }
  };

  const selectCompareFrom = (id) => {
    setCompareFromId(id);
    if (id && compareToId) runCompare(id, compareToId);
  };

  const selectCompareTo = (id) => {
    setCompareToId(id);
    if (compareFromId && id) runCompare(compareFromId, id);
  };

  const openDeleteDocument = () => {
    setDeleteTypedText('');
    setDeleteDocError('');
    setDeleteStep('type');
  };

  const confirmDeleteDocumentTyped = () => {
    if (deleteTypedText.trim().toLowerCase() !== 'delete') {
      setDeleteDocError('Type "delete" to continue.');
      return;
    }
    setDeleteDocError('');
    setDeleteStep('confirm');
  };

  const finalizeDeleteDocument = async () => {
    setBusyAction('delete');
    setDeleteDocError('');
    try {
      await documentsApi.remove(document.document_id, deleteTypedText.trim());
      setDeleteStep(null);
      onChanged?.();
    } catch (err) {
      setDeleteDocError(err.message || 'Could not delete this document.');
    } finally {
      setBusyAction(null);
    }
  };

  const confirmDeleteVersion = async () => {
    if (!versionToDelete) return;
    setVersionDeleteBusy(true);
    setVersionDeleteError('');
    try {
      await documentsApi.deleteVersion(document.document_id, versionToDelete.version_id);
      setVersionToDelete(null);
      setVersions(await documentsApi.versions(document.document_id));
      onChanged?.();
    } catch (err) {
      setVersionDeleteError(err.message || 'Could not delete this version.');
    } finally {
      setVersionDeleteBusy(false);
    }
  };

  return (
    <div
      id={`document-${document.document_id}`}
      className={`flex items-start gap-3 p-3 rounded-lg hover:bg-surface-hover transition-colors group ${highlighted ? 'ring-2 ring-primary bg-primary/5' : ''}`}
    >
      <div className="mt-0.5">{getFileIcon(document.filename)}</div>
      <div className="flex-1 min-w-0">
        <button
          type="button"
          onClick={() => setViewerOpen(true)}
          className="w-full min-w-0 text-sm font-medium text-gray-200 group-hover:text-primary-light transition-colors flex items-center gap-1 text-left"
        >
          <span className="truncate">{document.filename}</span>
        </button>
        <p className="text-xs text-gray-500 mt-1">
          {document.doc_type} &middot; {formatDate(document.created_at)} &middot; {sensitivityLabel(document.sensitivity_level)}
        </p>
        <div className="flex flex-wrap items-center gap-2 mt-2">
          {state ? (
            <Badge variant={stateVariant[state] || 'neutral'}>{state.replace('_', ' ')}</Badge>
          ) : (
            <Badge variant="success">approved</Badge>
          )}

          {state && (state === 'draft' || state === 'rejected') && (
            <Button size="sm" variant="ghost" icon={Send} className="h-6 px-2 text-xs" loading={busyAction === 'submit'} disabled={busy}
              onClick={() => run(() => documentsApi.submit(document.document_id), 'submit')}>
              {state === 'rejected' ? 'Resubmit for review' : 'Submit for review'}
            </Button>
          )}
          {canReview && state === 'pending_review' && (
            <Button size="sm" variant="ghost" icon={ClipboardCheck} className="h-6 px-2 text-xs" disabled={busy}
              onClick={() => setReviewOpen(true)}>
              Review &amp; decide
            </Button>
          )}
          {canEdit && (
            <Button size="sm" variant="ghost" icon={Pencil} className="h-6 px-2 text-xs" loading={busyAction === 'edit'} disabled={busy} onClick={handleEdit}>
              Edit
            </Button>
          )}
          <Button size="sm" variant="ghost" icon={History} className="h-6 px-2 text-xs" onClick={openVersions} disabled={busy}>
            Versions
          </Button>
          {canDelete && (
            <Button size="sm" variant="ghost" icon={Trash2} className="h-6 px-2 text-xs text-red-400" loading={busyAction === 'delete'} disabled={busy} onClick={openDeleteDocument}>
              Delete
            </Button>
          )}
        </div>
        {actionError && <p className="text-xs text-red-400 mt-1">{actionError}</p>}
      </div>

      <Modal
        open={versionsOpen}
        onClose={() => setVersionsOpen(false)}
        title="Document versions"
        description={document.filename}
      >
        <div className="space-y-4">
          <div className="flex items-center justify-between gap-3 border-b border-border pb-4">
            <div>
              <p className="text-sm font-medium text-gray-200">Upload a new version</p>
              <p className="text-xs text-gray-500 mt-1">The document identity and access rules stay the same.</p>
            </div>
            <label className="cursor-pointer shrink-0">
              <span className="inline-flex items-center gap-1.5 h-8 px-3 text-xs font-medium rounded-md bg-surface-hover text-gray-200 hover:bg-border transition-colors"><Upload size={13} /> Choose</span>
              <input ref={versionInputRef} type="file" className="hidden" onChange={(event) => setVersionFile(event.target.files?.[0] || null)} />
            </label>
          </div>
          {versionFile && (
            <div className="flex items-center justify-between gap-3 rounded-lg bg-background p-3 text-sm">
              <span className="truncate text-gray-300">{versionFile.name}</span>
              <Button size="sm" onClick={uploadVersion} loading={versionBusy}>Upload</Button>
            </div>
          )}
          {versionError && <p className="text-sm text-red-400">{versionError}</p>}
          {versionsLoading ? (
            <p className="text-sm text-gray-500">Loading history...</p>
          ) : versions.length > 0 ? (
            <div className="space-y-2 max-h-64 overflow-y-auto scrollbar-thin">
              {versions.map((version) => {
                const isNumbered = version.version_number != null;
                return (
                <div
                  key={version.version_id}
                  className={
                    isNumbered
                      ? 'flex items-center justify-between gap-3 rounded-lg border border-border p-3'
                      : 'flex items-center justify-between gap-3 rounded-r-lg border border-border border-l-2 border-l-primary/40 ml-3 pl-3 py-2 pr-3 bg-background/40'
                  }
                >
                  <div className="min-w-0">
                    <p className={`flex items-center gap-1.5 ${isNumbered ? 'text-sm text-gray-200' : 'text-xs text-gray-400'}`}>
                      <span className="truncate">{versionDisplayLabel(version)}</span>
                      {version.is_latest && <span className="text-gray-500 shrink-0">(Latest)</span>}
                      {version.is_live && (
                        <Badge
                          variant="success"
                          className="shrink-0"
                          title="This is the version Search Agent answers and audits are grounded in."
                        >
                          Live
                        </Badge>
                      )}
                    </p>
                    <p className="text-xs text-gray-500 mt-1">
                      {new Date(version.created_at).toLocaleString()} · {formatFileSize(version.file_size_bytes)}
                    </p>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <Badge
                      variant={versionStatusVariant[version.status] || 'neutral'}
                      title={versionStatusTooltip[version.status]}
                    >
                      {versionStatusLabel[version.status] || version.status}
                    </Badge>
                    <Button
                      size="sm"
                      variant="ghost"
                      icon={Eye}
                      className="h-7 px-2 text-xs"
                      onClick={() => {
                        setCompareOpen(true);
                        setCompareToId(version.version_id);
                        if (compareFromId && compareFromId !== version.version_id) {
                          runCompare(compareFromId, version.version_id);
                        }
                      }}
                    >
                      View
                    </Button>
                    {version.can_delete && (
                      <Button
                        size="sm"
                        variant="ghost"
                        icon={XIcon}
                        className="h-7 px-2 text-xs text-red-400"
                        title="Delete this version"
                        onClick={() => { setVersionDeleteError(''); setVersionToDelete(version); }}
                      >
                        Delete
                      </Button>
                    )}
                  </div>
                </div>
                );
              })}
            </div>
          ) : <p className="text-sm text-gray-500">No version history yet.</p>}

          {versions.length > 1 && !compareOpen && (
            <Button size="sm" variant="secondary" icon={FileDiff} className="w-full" onClick={() => setCompareOpen(true)}>
              Compare versions
            </Button>
          )}

          {compareOpen && (
            <div className="rounded-lg border border-border bg-background/40 p-3 space-y-3">
              <div className="flex items-center justify-between">
                <span className="flex items-center gap-1.5 text-xs font-medium text-gray-300">
                  <FileDiff size={13} /> Compare versions
                </span>
                <button
                  type="button"
                  onClick={() => { setCompareOpen(false); setCompareFromId(null); setCompareToId(null); setCompareDiff(null); setCompareError(''); }}
                  className="text-xs text-gray-500 hover:text-gray-200"
                >
                  Close
                </button>
              </div>
              <div className="flex flex-col sm:flex-row items-stretch sm:items-end gap-2">
                <div className="flex-1 min-w-0">
                  <Dropdown
                    label="From"
                    placeholder="Older version"
                    value={compareFromId}
                    onChange={selectCompareFrom}
                    options={versions.map((v) => ({
                      value: v.version_id,
                      label: `${versionDisplayLabel(v)}${v.is_latest ? ' (Latest)' : ''}${v.is_live ? ' • Live' : ''}`,
                      disabled: v.version_id === compareToId,
                    }))}
                  />
                </div>
                <div className="flex-1 min-w-0">
                  <Dropdown
                    label="To"
                    placeholder="Newer version"
                    value={compareToId}
                    onChange={selectCompareTo}
                    options={versions.map((v) => ({
                      value: v.version_id,
                      label: `${versionDisplayLabel(v)}${v.is_latest ? ' (Latest)' : ''}${v.is_live ? ' • Live' : ''}`,
                      disabled: v.version_id === compareFromId,
                    }))}
                  />
                </div>
              </div>
              {compareLoading && <p className="text-sm text-gray-500">Comparing...</p>}
              {compareError && <p className="text-sm text-red-400">{compareError}</p>}
              {compareDiff && <DiffStats diff={compareDiff} />}
              {!compareLoading && !compareDiff && !compareError && compareFromId && compareToId && (
                <p className="text-sm text-gray-500">Select two different versions to compare.</p>
              )}
            </div>
          )}
        </div>
      </Modal>

      {diffReview && (
        <VersionDiffModal
          document={document}
          initialOutcome={diffReview}
          onClose={() => setDiffReview(null)}
          onFinalized={() => onChanged?.()}
        />
      )}

      {viewerOpen && (
        <DocumentViewerModal
          documentId={document.document_id}
          onClose={() => setViewerOpen(false)}
        />
      )}

      {reviewOpen && (
        <DocumentReviewModal
          document={document}
          canOverrideScan={canOverrideScan}
          onApprove={async (override) => {
            await documentsApi.approve(document.document_id, { override });
            setReviewOpen(false);
            onChanged?.();
          }}
          onReject={async (reason) => {
            await documentsApi.reject(document.document_id, reason);
            setReviewOpen(false);
            onChanged?.();
          }}
          onClose={() => setReviewOpen(false)}
        />
      )}

      <Modal
        open={deleteStep === 'type'}
        onClose={() => setDeleteStep(null)}
        title="Delete document"
        description={document.filename}
        footer={
          <>
            <Button variant="ghost" onClick={() => setDeleteStep(null)}>Cancel</Button>
            <Button variant="danger" onClick={confirmDeleteDocumentTyped}>Continue</Button>
          </>
        }
      >
        <div className="space-y-3">
          <p className="text-sm text-gray-300">
            This permanently deletes <span className="font-medium text-gray-100">{document.filename}</span> and every
            version of it. This cannot be undone.
          </p>
          <p className="text-sm text-gray-400">Type <span className="font-mono text-gray-200">delete</span> to continue.</p>
          <input
            autoFocus
            type="text"
            value={deleteTypedText}
            onChange={(e) => setDeleteTypedText(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') confirmDeleteDocumentTyped(); }}
            className="w-full h-9 px-3 rounded-md bg-background border border-border text-sm text-gray-200 focus:outline-none focus:ring-1 focus:ring-primary"
            placeholder="delete"
          />
          {deleteDocError && <p className="text-sm text-red-400">{deleteDocError}</p>}
        </div>
      </Modal>

      <Modal
        open={deleteStep === 'confirm'}
        onClose={() => setDeleteStep(null)}
        title="Are you sure?"
        description={document.filename}
        footer={
          <>
            <Button variant="ghost" onClick={() => setDeleteStep(null)} disabled={busyAction === 'delete'}>Cancel</Button>
            <Button variant="danger" onClick={finalizeDeleteDocument} loading={busyAction === 'delete'}>
              Delete permanently
            </Button>
          </>
        }
      >
        <div className="space-y-3">
          <p className="text-sm text-gray-300">
            Last chance — this deletes the document and all of its versions permanently. There is no undo.
          </p>
          {deleteDocError && <p className="text-sm text-red-400">{deleteDocError}</p>}
        </div>
      </Modal>

      <Modal
        open={!!versionToDelete}
        onClose={() => setVersionToDelete(null)}
        title="Delete this version?"
        description={versionToDelete ? versionDisplayLabel(versionToDelete) : ''}
        footer={
          <>
            <Button variant="ghost" onClick={() => setVersionToDelete(null)} disabled={versionDeleteBusy}>Cancel</Button>
            <Button variant="danger" onClick={confirmDeleteVersion} loading={versionDeleteBusy}>Delete</Button>
          </>
        }
      >
        <div className="space-y-3">
          <p className="text-sm text-gray-300">
            This permanently deletes this version. The document's other versions are not affected.
          </p>
          {versionDeleteError && <p className="text-sm text-red-400">{versionDeleteError}</p>}
        </div>
      </Modal>
    </div>
  );
};

export default DocumentItem;
