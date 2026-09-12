import React, { useRef, useState } from 'react';
import { FileText, File, FileImage, FileSpreadsheet, ExternalLink, Send, Check, X, History, Upload, Trash2 } from 'lucide-react';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import Modal from '../ui/Modal';
import Textarea from '../ui/Textarea';
import { documentsApi } from '../../lib/api';
import { sensitivityLabel } from '../../constants/docTypes';

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

const formatDate = (isoString) => {
  if (!isoString) return '';
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
};

const DocumentItem = ({ document, canReview, canDelete, onChanged }) => {
  const [busy, setBusy] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [rejectionReason, setRejectionReason] = useState('');
  const [actionError, setActionError] = useState('');
  const [versionsOpen, setVersionsOpen] = useState(false);
  const [versions, setVersions] = useState([]);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [versionFile, setVersionFile] = useState(null);
  const [versionBusy, setVersionBusy] = useState(false);
  const [versionError, setVersionError] = useState('');
  const versionInputRef = useRef(null);

  const state = document.workflow_state;

  const run = async (fn) => {
    setBusy(true);
    setActionError('');
    try {
      await fn();
      onChanged?.();
    } catch (err) {
      setActionError(err.message || 'Action failed.');
    } finally {
      setBusy(false);
    }
  };

  const handleReject = async () => {
    if (!rejectionReason.trim()) return;
    await run(() => documentsApi.reject(document.document_id, rejectionReason.trim()));
    setRejecting(false);
    setRejectionReason('');
  };

  const openVersions = async () => {
    setVersionsOpen(true);
    setVersionsLoading(true);
    setVersionError('');
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
      const formData = new FormData();
      formData.append('file', versionFile, versionFile.name);
      const created = await documentsApi.uploadVersion(document.document_id, formData);
      setVersions((current) => [created, ...current]);
      setVersionFile(null);
      if (versionInputRef.current) versionInputRef.current.value = '';
      onChanged?.();
    } catch (err) {
      setVersionError(err.message || 'Could not upload this version.');
    } finally {
      setVersionBusy(false);
    }
  };

  const handleDelete = async () => {
    if (!window.confirm(`Delete ${document.filename}? This cannot be undone.`)) return;
    await run(() => documentsApi.remove(document.document_id));
  };

  return (
    <div className="flex items-start gap-3 p-3 rounded-lg hover:bg-surface-hover transition-colors group">
      <div className="mt-0.5">{getFileIcon(document.filename)}</div>
      <div className="flex-1 min-w-0">
        <a
          href={documentsApi.openUrl(document.document_id)}
          target="_blank"
          rel="noreferrer"
          className="text-sm font-medium text-gray-200 truncate group-hover:text-primary-light transition-colors flex items-center gap-1"
        >
          <span className="truncate">{document.filename}</span>
          <ExternalLink size={12} className="shrink-0 opacity-0 group-hover:opacity-100 transition-opacity" />
        </a>
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
            <Button size="sm" variant="ghost" icon={Send} className="h-6 px-2 text-xs" loading={busy}
              onClick={() => run(() => documentsApi.submit(document.document_id))}>
              {state === 'rejected' ? 'Resubmit for review' : 'Submit for review'}
            </Button>
          )}
          {canReview && state === 'pending_review' && (
            <>
              <Button size="sm" variant="ghost" icon={Check} className="h-6 px-2 text-xs text-emerald-400" loading={busy}
                onClick={() => run(() => documentsApi.approve(document.document_id))}>
                Approve
              </Button>
              <Button size="sm" variant="ghost" icon={X} className="h-6 px-2 text-xs text-red-400" loading={busy}
                onClick={() => setRejecting(true)}>
                Reject
              </Button>
            </>
          )}
          {canDelete && (
            <Button size="sm" variant="ghost" icon={Trash2} className="h-6 px-2 text-xs text-red-400" loading={busy} onClick={handleDelete}>
              Delete
            </Button>
          )}
        </div>
        {actionError && <p className="text-xs text-red-400 mt-1">{actionError}</p>}
      </div>

      <Modal
        open={rejecting}
        onClose={() => setRejecting(false)}
        title="Reject document"
        description={document.filename}
        footer={
          <>
            <Button variant="ghost" onClick={() => setRejecting(false)}>Cancel</Button>
            <Button variant="danger" onClick={handleReject} disabled={!rejectionReason.trim()} loading={busy}>
              Reject
            </Button>
          </>
        }
      >
        <Textarea
          label="Rejection reason"
          required
          value={rejectionReason}
          onChange={(e) => setRejectionReason(e.target.value)}
          placeholder="Explain what needs to change..."
        />
      </Modal>

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
            <div className="space-y-2 max-h-64 overflow-y-auto">
              {versions.map((version) => (
                <div key={version.version_id} className="flex items-center justify-between gap-3 rounded-lg border border-border p-3">
                  <div className="min-w-0"><p className="text-sm text-gray-200 truncate">v{version.version_number} · {version.filename}</p><p className="text-xs text-gray-500 mt-1">{new Date(version.created_at).toLocaleString()} · {(version.file_size_bytes / 1024 / 1024).toFixed(2)} MB</p></div>
                  <Badge variant="neutral">{version.status}</Badge>
                </div>
              ))}
            </div>
          ) : <p className="text-sm text-gray-500">No version history yet.</p>}
        </div>
      </Modal>
    </div>
  );
};

export default DocumentItem;
