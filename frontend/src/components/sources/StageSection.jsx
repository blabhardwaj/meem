import React, { useEffect, useState } from 'react';
import { ChevronDown, ShieldCheck, Settings2 } from 'lucide-react';
import Badge from '../ui/Badge';
import DocumentItem from './DocumentItem';

// One stage grouping in the Sources panel. Renders even when it has no
// documents (so newly created / empty stages are visible and manageable).
const StageSection = ({
  stage,
  documents = [],
  canReview,
  canOverrideScan = false,
  canDelete,
  onChanged,
  requiresApproval = false,
  onEdit = null, // direct edit stage & settings callback
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
          className="flex flex-1 items-center gap-3 p-4 min-w-0"
        >
          <ChevronDown
            size={18}
            className={`text-gray-400 transition-transform duration-200 shrink-0 ${expanded ? 'rotate-180' : ''}`}
          />
          <h3 className="font-medium text-gray-100 truncate">{stage}</h3>
          <Badge variant="neutral">{documents.length}</Badge>
          {requiresApproval && (
            <span className="inline-flex items-center gap-1 text-[11px] text-amber-400" title="Documents in this stage need approval">
              <ShieldCheck size={12} /> approval
            </span>
          )}
        </button>
        <div className="flex items-center gap-1 shrink-0 pr-1">
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
            {documents.length > 0 ? (
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
