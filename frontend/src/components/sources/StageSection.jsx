import React, { useState, useEffect } from 'react';
import { ChevronDown, ShieldCheck, Settings2, ListChecks } from 'lucide-react';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import Modal from '../ui/Modal';
import Tooltip from '../ui/Tooltip';
import DocumentItem from './DocumentItem';
import { accessRequestsApi } from '../../lib/api';

// A stage's own "Request access" affordance, shown in its header next to
// the document count whenever the caller doesn't already hold a granted
// stage-scope confidential-access grant. Serves TWO distinct cases through
// the same backend call (request_confidential_access's stage branch
// already handles both):
//   - hasAccess is false: the caller has zero team_stage_access to this
//     stage at all -- routes to a project admin (there's no team lead to
//     ask yet), and approval creates a brand-new per-user stage grant.
//   - hasAccess is true: the caller already sees this stage's
//     sub-confidential documents via their own team, but wants the
//     stage's CONFIDENTIAL-tier documents too -- routes to that team's
//     lead as an upgrade request, per spec's tier system.
// initialStatus/initialTier come from GET /workspace's per-stage
// access_request_status/access_request_tier -- bundled there so this
// component needs no network call of its own. Previously it fetched its
// own status via GET /access-requests/status on mount; with N stages that
// meant N sequential round trips firing AFTER the page had already
// rendered everything else, each with several DB queries behind it,
// which is exactly what made these buttons visibly slower to appear than
// everything around them.
// 'granted' covers ANY active stage grant (viewer/contributor/
// contributor_confidential) -- a plain viewer or contributor grant makes
// the stage itself visible but does NOT unlock its confidential
// documents. Once the caller already has a contributor_confidential
// grant (tier === 'contributor_confidential'), there's genuinely nothing
// further to request and the button hides; a lesser tier still needs the
// confidential-upgrade request to show.
const StageAccessRequest = ({ stageId, stageName, hasAccess, initialStatus, initialTier }) => {
  const [state, setState] = useState(initialStatus || 'none');
  const [tier, setTier] = useState(initialTier || null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const openDialog = (e) => {
    e.stopPropagation();
    setError('');
    setReason('');
    setDialogOpen(true);
  };

  const submit = async () => {
    setBusy(true);
    setError('');
    try {
      await accessRequestsApi.createForStage(stageId, reason.trim() || null);
      setState('pending');
      setDialogOpen(false);
    } catch (err) {
      setError(err.message || 'Could not send the request.');
    } finally {
      setBusy(false);
    }
  };

  if (state === 'granted' && tier === 'contributor_confidential') return null;

  if (state === 'pending') {
    return <span className="text-xs text-gray-500">Access requested — pending review.</span>;
  }

  return (
    <>
      <button
        type="button"
        onClick={openDialog}
        title={hasAccess ? 'Request access to this stage’s confidential documents' : 'Request access to this stage'}
        className="text-xs text-primary-light hover:text-primary transition-colors py-1 px-2 rounded-md hover:bg-surface-hover"
      >
        {hasAccess ? 'Request confidential access' : 'Request access'}
      </button>
      <Modal
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        title={hasAccess ? 'Request confidential access' : 'Request stage access'}
        description={
          hasAccess
            ? `Ask for access to ${stageName || 'this stage'}'s confidential documents.`
            : `Ask for access to ${stageName || 'this stage'}.`
        }
      >
        <div className="space-y-3">
          <div>
            <label htmlFor="access-request-reason" className="block text-xs text-gray-400 mb-1">
              Why do you need access? (optional)
            </label>
            <textarea
              id="access-request-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              maxLength={500}
              rows={3}
              placeholder="e.g. Need to review QA's test coverage before sign-off"
              className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-gray-200 placeholder:text-gray-600 focus:outline-none focus:ring-1 focus:ring-primary"
            />
          </div>
          {error && <p className="text-xs text-red-400">{error}</p>}
          <div className="flex justify-end gap-2">
            <Button variant="secondary" onClick={() => setDialogOpen(false)} disabled={busy}>
              Cancel
            </Button>
            <Button onClick={submit} loading={busy}>
              Send request
            </Button>
          </div>
        </div>
      </Modal>
    </>
  );
};

// One stage grouping in the Sources panel. Renders even when it has no
// documents (so newly created / empty stages are visible and manageable),
// and even when the caller has no document access to it at all (hasAccess
// false) — the stage itself, its name, and its position are never hidden.
const StageSection = ({
  stage,
  stageId = null,
  documents = [],
  hasAccess = true,
  accessRequestStatus = 'none',
  accessRequestTier = null,
  canReview,
  canOverrideScan = false,
  canDelete,
  onChanged,
  requiresApproval = false,
  onEdit = null, // direct edit stage & settings callback
  onViewChecklist = null, // open the read-only required-documents checklist — no admin gate
  menu = null, // optional <KebabMenu /> element rendered in the header
  defaultExpanded = true,
  highlightDocumentId = null,
  canEditAny = false,
  currentUserId = null,
}) => {
  const containsHighlight = highlightDocumentId
    && documents.some((d) => d.document_id === highlightDocumentId);
  const [expanded, setExpanded] = useState(defaultExpanded || containsHighlight);

  // BUGFIXES_2026-09-15.md: useState's initializer only runs once, at
  // mount. `documents` (and therefore `containsHighlight`) commonly arrives
  // on a LATER render than this component's own mount (stage structure and
  // document lists load separately) — a highlighted document outside the
  // default-expanded stage would then never actually auto-expand, even
  // though the parent's scrollIntoView still "succeeds" against the
  // collapsed (0-height, opacity-0, but still DOM-present) section, landing
  // the user on what looks like an empty area with nothing visible.
  useEffect(() => {
    if (containsHighlight) setExpanded(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [containsHighlight]);

  return (
    <div className="border border-border rounded-xl bg-surface mb-4">
      <div className="flex items-center justify-between pr-2 bg-surface hover:bg-surface-hover transition-colors rounded-t-xl">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="relative flex flex-1 items-center gap-3 p-4 min-w-0"
        >
          <ChevronDown
            size={18}
            className={`text-gray-400 transition-transform duration-200 shrink-0 ${expanded ? 'rotate-180' : ''}`}
          />
          <h3 className="peer font-medium text-gray-100 truncate">{stage}</h3>
          <Tooltip label={stage} className="left-9 bottom-full mb-2" />
          <Badge variant="neutral">{documents.length}</Badge>
          {requiresApproval && (
            <span className="inline-flex items-center gap-1 text-[11px] text-amber-400" title="Documents in this stage need approval">
              <ShieldCheck size={12} /> approval
            </span>
          )}
        </button>
        <div className="flex items-center gap-1 shrink-0 pr-1">
          {stageId && (
            <StageAccessRequest
              stageId={stageId}
              stageName={stage}
              hasAccess={hasAccess}
              initialStatus={accessRequestStatus}
              initialTier={accessRequestTier}
            />
          )}
          {stageId && onViewChecklist && (
            <button
              type="button"
              aria-label={`View required documents for ${stage}`}
              title={`View required documents for ${stage}`}
              onClick={(e) => {
                e.stopPropagation();
                onViewChecklist();
              }}
              className="p-1.5 rounded-md text-gray-400 hover:text-gray-100 hover:bg-surface-hover transition-colors"
            >
              <ListChecks size={16} />
            </button>
          )}
          {onEdit && (
            <button
              type="button"
              aria-label={`Edit stage & settings for ${stage}`}
              title={`Edit stage & settings for ${stage}`}
              onClick={(e) => {
                e.stopPropagation();
                onEdit();
              }}
              className="p-1.5 rounded-md text-gray-400 hover:text-gray-100 hover:bg-surface-hover transition-colors"
            >
              <Settings2 size={16} />
            </button>
          )}
          {menu}
        </div>
      </div>

      <div className={`grid transition-all duration-200 ease-in-out ${expanded ? 'grid-rows-[1fr] opacity-100' : 'grid-rows-[0fr] opacity-0'}`}>
        <div className="overflow-hidden">
          <div className="p-4 border-t border-border/50 bg-background/30">
            {!hasAccess ? (
              <p className="text-xs text-gray-600 py-1">
                You don&apos;t have access to this stage&apos;s documents. Request access above to view them.
              </p>
            ) : documents.length > 0 ? (
              <div className="flex flex-col gap-1">
                {documents.map((doc) => (
                  <DocumentItem
                    key={doc.document_id}
                    document={doc}
                    canReview={canReview}
                    canOverrideScan={canOverrideScan}
                    canDelete={canDelete}
                    onChanged={onChanged}
                    highlighted={doc.document_id === highlightDocumentId}
                    canEdit={canEditAny || doc.uploaded_by === currentUserId}
                  />
                ))}
              </div>
            ) : (
              <p className="text-xs text-gray-600 py-1">No documents in this stage yet.</p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

export default StageSection;
