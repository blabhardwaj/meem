import React, { useEffect, useState } from 'react';
import { Check, Circle, FileText } from 'lucide-react';
import Modal from '../ui/Modal';
import Badge from '../ui/Badge';
import { stagesApi } from '../../lib/api';

// Read-only requirements checklist, open to any project member (not gated
// to canManageStages) — the non-admin counterpart to the mutable list in
// SourcePanel's stage-settings modal. satisfied/satisfied_by come from the
// real audit engine's RequirementSatisfaction table via the requirements
// API, already ABAC-filtered server-side: satisfied can be true while
// satisfied_by is null when the caller can't see the satisfying document.
const RequirementsChecklistModal = ({ open, onClose, projectId, stageId, stageName }) => {
  const [requirements, setRequirements] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open || !stageId) return undefined;
    let cancelled = false;
    setLoading(true);
    setError('');
    stagesApi.listRequirements(projectId, stageId)
      .then((data) => { if (!cancelled) setRequirements(data); })
      .catch((err) => { if (!cancelled) setError(err.message || 'Could not load requirements.'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [open, projectId, stageId]);

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`Required Documents${stageName ? `: ${stageName}` : ''}`}
      description="What this stage needs to be considered complete."
      scrollable
    >
      {loading ? (
        <p className="text-sm text-gray-500">Loading…</p>
      ) : error ? (
        <p className="text-sm text-red-400">{error}</p>
      ) : requirements.length === 0 ? (
        <p className="text-sm text-gray-500">No requirements defined for this stage.</p>
      ) : (
        <div className="space-y-2">
          {requirements.map((r) => (
            <div
              key={r.requirement_id}
              className="flex items-start gap-3 rounded-lg border border-border bg-background px-3 py-2.5"
            >
              <div className="mt-0.5 shrink-0">
                {r.satisfied ? (
                  <Check size={16} className="text-emerald-400" />
                ) : (
                  <Circle size={16} className="text-gray-500" />
                )}
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-sm font-medium text-gray-200">{r.name}</span>
                  {r.is_mandatory && <Badge variant="warning">Mandatory</Badge>}
                </div>
                {r.description && (
                  <p className="text-xs text-gray-500 mt-0.5">{r.description}</p>
                )}
                {r.satisfied && r.satisfied_by && (
                  <p className="text-xs text-emerald-400/80 mt-1 flex items-center gap-1">
                    <FileText size={11} /> Satisfied by: {r.satisfied_by.filename}
                  </p>
                )}
                {r.satisfied && !r.satisfied_by && (
                  <p className="text-xs text-gray-500 mt-1">
                    Satisfied by a document you don&apos;t have access to.
                  </p>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
};

export default RequirementsChecklistModal;
