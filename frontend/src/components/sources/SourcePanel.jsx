import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Plus, Pencil, ArrowUp, ArrowDown, ShieldCheck, Trash2, Settings2, Link2, Users, UploadCloud, ListChecks, Check, Circle, X as XIcon } from 'lucide-react';
import { STAGES } from '../../constants/stages';
import StageSection from './StageSection';
import RequirementsChecklistModal from './RequirementsChecklistModal';
import Modal from '../ui/Modal';
import Button from '../ui/Button';
import Input from '../ui/Input';
import Dropdown from '../ui/Dropdown';
import { stagesApi, documentsApi } from '../../lib/api';

const UPLOAD_ACCEPT = '.pdf,.docx,.txt,.md,.markdown,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain,text/markdown';

const SourcePanel = ({
  documents = [],
  canReview = false,
  canOverrideScan = false,
  canDelete = false,
  onChanged,
  projectId,
  stages = [],
  teams = [],
  canManageStages = false,
  onStagesChanged,
  onDocumentUploaded,
  highlightDocumentId = null,
  canEditAny = false,
  currentUserId = null,
}) => {
  const groupedDocs = useMemo(() => documents.reduce((acc, doc) => {
    const stage = doc.stage || 'Unspecified';
    (acc[stage] = acc[stage] || []).push(doc);
    return acc;
  }, {}), [documents]);

  // Real project stages first (in order, including empty ones), then any
  // stage names that appear only on documents (legacy / unspecified).
  const orderedStages = useMemo(
    () => [...stages].sort((a, b) => a.order_index - b.order_index),
    [stages],
  );
  const managedNames = new Set(orderedStages.map((s) => s.name));
  const extraNames = Object.keys(groupedDocs)
    .filter((name) => !managedNames.has(name))
    .sort((a, b) => {
      const ia = STAGES.indexOf(a); const ib = STAGES.indexOf(b);
      if (ia !== -1 && ib !== -1) return ia - ib;
      return a.localeCompare(b);
    });

  // --- create-stage modal ---
  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState('');
  const [newPos, setNewPos] = useState(''); // '' = append
  const [createBusy, setCreateBusy] = useState(false);
  const [createErr, setCreateErr] = useState('');

  // --- per-stage settings modal ---
  const [settingsStage, setSettingsStage] = useState(null);
  const [editName, setEditName] = useState('');
  const [reqApproval, setReqApproval] = useState(false);
  const [refIds, setRefIds] = useState([]); // stage_ids this stage references
  const [teamAccessIds, setTeamAccessIds] = useState([]); // team_ids granted access to this stage
  const [reassignTo, setReassignTo] = useState('');
  // Which stage-settings action is in flight, if any — a string key rather
  // than a plain boolean, so e.g. "Rename" and "Delete stage" (both visible
  // in the same modal) don't spin together when only one was clicked.
  const [stageBusyAction, setStageBusyAction] = useState(null);
  const stageBusy = stageBusyAction !== null;
  const [stageErr, setStageErr] = useState('');
  const [stageNotice, setStageNotice] = useState('');

  // --- read-only requirements checklist, open to any project member ---
  const [checklistStage, setChecklistStage] = useState(null);

  // --- per-stage required-documents checklist (admin settings modal, mutable) ---
  const [requirements, setRequirements] = useState([]);
  const [requirementsLoading, setRequirementsLoading] = useState(false);
  const [newReqName, setNewReqName] = useState('');
  const [newReqDescription, setNewReqDescription] = useState('');
  const [newReqMandatory, setNewReqMandatory] = useState(true);

  // --- upload-doc modal ---
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadFile, setUploadFile] = useState(null);
  const [uploadStageId, setUploadStageId] = useState('');
  const [uploadTeamId, setUploadTeamId] = useState('');
  const [uploadSensitivity, setUploadSensitivity] = useState('internal');
  const [uploadBusy, setUploadBusy] = useState(false);
  const [uploadErr, setUploadErr] = useState('');
  const uploadFileRef = useRef(null);

  const countFor = (name) => (groupedDocs[name] || []).length;
  // A single silent reload (docs + stages) — keeps the settings modal open.
  const refresh = async () => { await onStagesChanged?.(); };

  const openSettings = (stage) => {
    setSettingsStage(stage);
    setEditName(stage.name);
    setReqApproval(Boolean(stage.requires_approval));
    setRefIds(stage.references || []);
    setTeamAccessIds(stage.team_access || []);
    setReassignTo('');
    setStageErr(''); setStageNotice('');
    setNewReqName(''); setNewReqDescription(''); setNewReqMandatory(true);
    loadRequirements(stage.stage_id);
  };

  const loadRequirements = async (stageId) => {
    setRequirementsLoading(true);
    try {
      setRequirements(await stagesApi.listRequirements(projectId, stageId));
    } catch (err) {
      setStageErr(err.message || 'Could not load required documents.');
    } finally {
      setRequirementsLoading(false);
    }
  };

  const addRequirement = async () => {
    const name = newReqName.trim();
    if (!name) return;
    setStageBusyAction('req:create'); setStageErr(''); setStageNotice('');
    try {
      await stagesApi.createRequirement(projectId, settingsStage.stage_id, {
        name, description: newReqDescription.trim() || null, is_mandatory: newReqMandatory,
      });
      setNewReqName(''); setNewReqDescription(''); setNewReqMandatory(true);
      await loadRequirements(settingsStage.stage_id);
      setStageNotice('Requirement added.');
      await refresh();
    } catch (err) {
      setStageErr(err.message || 'Could not add this requirement.');
    } finally {
      setStageBusyAction(null);
    }
  };

  const toggleRequirementMandatory = async (requirement) => {
    setStageBusyAction(`req:${requirement.requirement_id}`); setStageErr(''); setStageNotice('');
    try {
      await stagesApi.updateRequirement(projectId, settingsStage.stage_id, requirement.requirement_id, {
        is_mandatory: !requirement.is_mandatory,
      });
      await loadRequirements(settingsStage.stage_id);
      await refresh();
    } catch (err) {
      setStageErr(err.message || 'Could not update this requirement.');
    } finally {
      setStageBusyAction(null);
    }
  };

  const removeRequirement = async (requirement) => {
    if (!window.confirm(`Delete the requirement "${requirement.name}"?`)) return;
    setStageBusyAction(`req:${requirement.requirement_id}`); setStageErr(''); setStageNotice('');
    try {
      await stagesApi.removeRequirement(projectId, settingsStage.stage_id, requirement.requirement_id);
      await loadRequirements(settingsStage.stage_id);
      setStageNotice('Requirement deleted.');
      await refresh();
    } catch (err) {
      setStageErr(err.message || 'Could not delete this requirement.');
    } finally {
      setStageBusyAction(null);
    }
  };

  const handleCreate = async () => {
    const name = newName.trim();
    if (!name) return;
    setCreateBusy(true); setCreateErr('');
    try {
      const body = { name };
      if (newPos !== '') body.order_index = Number(newPos);
      await stagesApi.create(projectId, body);
      setCreateOpen(false); setNewName(''); setNewPos('');
      await refresh();
    } catch (err) {
      setCreateErr(err.message || 'Could not create the stage.');
    } finally {
      setCreateBusy(false);
    }
  };

  // --- upload-doc modal -----------------------------------------------

  const openUpload = () => {
    setUploadOpen(true);
    setUploadFile(null);
    setUploadStageId('');
    setUploadTeamId('');
    setUploadSensitivity('internal');
    setUploadErr('');
    if (uploadFileRef.current) uploadFileRef.current.value = '';
  };

  // Which of the caller's teams can actually upload to the selected stage.
  // Admins (canManageStages) bypass team_stage_access entirely, same as the
  // backend — any team in the project is a valid choice for them.
  const eligibleUploadTeams = useMemo(() => {
    const stage = orderedStages.find((s) => s.stage_id === uploadStageId);
    if (!stage) return [];
    if (canManageStages) return teams;
    const granted = new Set(stage.team_access || []);
    return teams.filter((t) => granted.has(t.team_id));
  }, [uploadStageId, orderedStages, teams, canManageStages]);

  // Auto-infer the team when there's exactly one eligible choice; otherwise
  // clear it so the dropdown forces an explicit pick.
  useEffect(() => {
    if (eligibleUploadTeams.length === 1) {
      setUploadTeamId(eligibleUploadTeams[0].team_id);
    } else if (!eligibleUploadTeams.some((t) => t.team_id === uploadTeamId)) {
      setUploadTeamId('');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [eligibleUploadTeams]);

  const handleUpload = async () => {
    if (!uploadFile || !uploadStageId || !uploadTeamId) return;
    setUploadBusy(true); setUploadErr('');
    try {
      const result = await documentsApi.uploadFile({
        file: uploadFile, stageId: uploadStageId, teamId: uploadTeamId,
        sensitivityLevel: uploadSensitivity,
      });
      setUploadOpen(false);
      await refresh();
      onChanged?.();
      onDocumentUploaded?.({ ...result, originalFilename: uploadFile.name });
    } catch (err) {
      setUploadErr(err.message || 'Could not upload the document.');
    } finally {
      setUploadBusy(false);
    }
  };

  const patchStage = async (patch, successMsg, actionKey) => {
    setStageBusyAction(actionKey); setStageErr(''); setStageNotice('');
    try {
      const updated = await stagesApi.update(projectId, settingsStage.stage_id, patch);
      setSettingsStage(updated);
      setEditName(updated.name);
      setReqApproval(Boolean(updated.requires_approval));
      setRefIds(updated.references || []);
      setTeamAccessIds(updated.team_access || []);
      if (successMsg) setStageNotice(successMsg);
      await refresh();
    } catch (err) {
      setStageErr(err.message || 'Could not update the stage.');
    } finally {
      setStageBusyAction(null);
    }
  };

  const toggleReference = async (targetId) => {
    const next = refIds.includes(targetId)
      ? refIds.filter((id) => id !== targetId)
      : [...refIds, targetId];
    setRefIds(next); // optimistic
    setStageBusyAction('references'); setStageErr(''); setStageNotice('');
    try {
      const updated = await stagesApi.setReferences(projectId, settingsStage.stage_id, next);
      setSettingsStage(updated);
      setRefIds(updated.references || []);
      setStageNotice('References updated.');
      await refresh();
    } catch (err) {
      setRefIds(refIds); // roll back
      setStageErr(err.message || 'Could not update references.');
    } finally {
      setStageBusyAction(null);
    }
  };

  const toggleTeamAccess = async (targetTeamId) => {
    const next = teamAccessIds.includes(targetTeamId)
      ? teamAccessIds.filter((id) => id !== targetTeamId)
      : [...teamAccessIds, targetTeamId];
    setTeamAccessIds(next); // optimistic
    setStageBusyAction('team-access'); setStageErr(''); setStageNotice('');
    try {
      const updated = await stagesApi.setTeamAccess(projectId, settingsStage.stage_id, next);
      setSettingsStage(updated);
      setTeamAccessIds(updated.team_access || []);
      setStageNotice('Team access updated.');
      await refresh();
    } catch (err) {
      setTeamAccessIds(teamAccessIds); // roll back
      setStageErr(err.message || 'Could not update team access.');
    } finally {
      setStageBusyAction(null);
    }
  };

  const handleDelete = async () => {
    const docs = countFor(settingsStage.name);
    if (docs > 0 && !reassignTo) {
      setStageErr(`This stage has ${docs} document(s). Choose a stage to move them to first.`);
      return;
    }
    if (!window.confirm(
      docs > 0
        ? `Move ${docs} document(s) to the selected stage and delete "${settingsStage.name}"?`
        : `Delete the stage "${settingsStage.name}"?`,
    )) return;
    setStageBusyAction('delete'); setStageErr(''); setStageNotice('');
    try {
      await stagesApi.remove(projectId, settingsStage.stage_id, reassignTo || undefined);
      setSettingsStage(null);
      await refresh();
    } catch (err) {
      setStageErr(err.message || 'Could not delete the stage.');
    } finally {
      setStageBusyAction(null);
    }
  };

  const moveStage = (dir) => {
    const idx = orderedStages.findIndex((s) => s.stage_id === settingsStage.stage_id);
    const target = idx + dir;
    if (target < 0 || target >= orderedStages.length) return;
    patchStage({ order_index: target }, 'Order updated.', 'move');
  };

  const reassignOptions = orderedStages
    .filter((s) => s.stage_id !== settingsStage?.stage_id)
    .map((s) => ({ label: s.name, value: s.stage_id }));
  const settingsIdx = settingsStage
    ? orderedStages.findIndex((s) => s.stage_id === settingsStage.stage_id)
    : -1;

  const hasAnything = orderedStages.length > 0 || extraNames.length > 0;

  return (
    <div className="flex flex-col bg-surface border-r border-border lg:min-h-full">
      <div className="p-4 border-b border-border/50">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 min-w-0">
            <h2 className="text-lg font-semibold text-gray-100">Sources</h2>
            {orderedStages.length > 0 && (
              <Button size="sm" variant="secondary" icon={UploadCloud} onClick={openUpload}>
                Upload Doc
              </Button>
            )}
          </div>
          <div className="flex items-center gap-2">
            {canManageStages && (
              <Button
                size="sm"
                variant="secondary"
                icon={Plus}
                onClick={() => { setCreateErr(''); setCreateOpen(true); }}
                className="text-xs h-7 px-2"
              >
                New Stage
              </Button>
            )}
          </div>
        </div>
        <p className="text-sm text-gray-400 mt-1">Project documents and evidence</p>
      </div>

      <div className="p-4">
        {!hasAnything ? (
          <div className="text-center py-8 text-gray-500 text-sm">
            {canManageStages ? 'No stages yet — create one from the New Stage button above.' : 'No documents found for this project.'}
          </div>
        ) : (
          <>
            {orderedStages.map((stage) => (
              <StageSection
                key={stage.stage_id}
                stage={stage.name}
                stageId={stage.stage_id}
                documents={groupedDocs[stage.name] || []}
                canReview={canReview}
                canOverrideScan={canOverrideScan}
                canDelete={canDelete}
                onChanged={onChanged}
                requiresApproval={stage.requires_approval}
                onEdit={canManageStages ? () => openSettings(stage) : null}
                onViewChecklist={() => setChecklistStage(stage)}
                highlightDocumentId={highlightDocumentId}
                canEditAny={canEditAny}
                currentUserId={currentUserId}
              />
            ))}
            {extraNames.map((name) => (
              <StageSection
                key={name}
                stage={name}
                documents={groupedDocs[name] || []}
                canReview={canReview}
                canOverrideScan={canOverrideScan}
                canDelete={canDelete}
                onChanged={onChanged}
                highlightDocumentId={highlightDocumentId}
                canEditAny={canEditAny}
                currentUserId={currentUserId}
              />
            ))}
          </>
        )}
      </div>

      {/* Create stage */}
      <Modal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        title="Create new stage"
        description="Stages structure the Sources library and drive stage-gated approval."
        footer={(
          <>
            <Button variant="ghost" onClick={() => setCreateOpen(false)}>Cancel</Button>
            <Button onClick={handleCreate} loading={createBusy} disabled={!newName.trim()}>Create stage</Button>
          </>
        )}
      >
        <div className="space-y-4">
          {createErr && <p className="text-sm text-red-400">{createErr}</p>}
          <Input
            label="Stage name" required autoFocus
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="e.g. Deployment"
          />
          <Dropdown
            label="Position"
            value={newPos}
            onChange={setNewPos}
            options={[
              { label: 'Add to the end', value: '' },
              ...orderedStages.map((s, i) => ({ label: `Before "${s.name}" (position ${i + 1})`, value: String(i) })),
            ]}
          />
        </div>
      </Modal>

      {/* Per-stage settings */}
      <Modal
        open={Boolean(settingsStage)}
        onClose={() => setSettingsStage(null)}
        title={settingsStage ? `Stage: ${settingsStage.name}` : 'Stage'}
        description="Rename, reorder, toggle approval, or delete this stage."
        scrollable={true}
      >
        {settingsStage && (
          <div className="space-y-5">
            {stageNotice && <p className="text-sm text-emerald-400">{stageNotice}</p>}
            {stageErr && <p className="text-sm text-red-400">{stageErr}</p>}

            {/* Rename */}
            <div className="flex items-end gap-2">
              <Input
                label="Name"
                value={editName}
                onChange={(e) => setEditName(e.target.value)}
              />
              <Button
                variant="secondary" icon={Pencil} loading={stageBusyAction === 'rename'}
                disabled={!editName.trim() || editName.trim() === settingsStage.name || stageBusy}
                onClick={() => patchStage({ name: editName.trim() }, 'Stage renamed.', 'rename')}
              >
                Rename
              </Button>
            </div>

            {/* Reorder */}
            <div>
              <p className="text-sm font-medium text-gray-300 mb-1.5">
                Order — position {settingsIdx + 1} of {orderedStages.length}
              </p>
              <div className="flex gap-2">
                <Button variant="secondary" icon={ArrowUp} disabled={stageBusy || settingsIdx <= 0} onClick={() => moveStage(-1)}>
                  Move up
                </Button>
                <Button variant="secondary" icon={ArrowDown} disabled={stageBusy || settingsIdx === orderedStages.length - 1} onClick={() => moveStage(1)}>
                  Move down
                </Button>
              </div>
            </div>

            {/* requires_approval */}
            <div className="flex items-start justify-between gap-3 rounded-lg border border-border bg-background px-3 py-2.5">
              <div className="min-w-0">
                <p className="text-sm text-gray-200 flex items-center gap-1.5">
                  <ShieldCheck size={14} className="text-amber-400" /> Require approval
                </p>
                <p className="text-xs text-gray-500 mt-0.5">
                  Documents uploaded to this stage must be submitted and approved (draft → pending → approved).
                </p>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={reqApproval}
                disabled={stageBusy}
                onClick={() => patchStage({ requires_approval: !reqApproval }, `Approval ${!reqApproval ? 'enabled' : 'disabled'} for this stage.`, 'approval-toggle')}
                className={`mt-0.5 shrink-0 relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${reqApproval ? 'bg-primary' : 'bg-border'} disabled:opacity-50`}
              >
                <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${reqApproval ? 'translate-x-4' : 'translate-x-0.5'}`} />
              </button>
            </div>

            {/* Stage references */}
            <div className="border-t border-border/60 pt-4">
              <p className="text-sm font-medium text-gray-300 flex items-center gap-1.5">
                <Link2 size={14} className="text-primary" /> References
              </p>
              <p className="text-xs text-gray-500 mt-0.5 mb-2">
                Other stages this one depends on. Retrieval scoped to
                &ldquo;{settingsStage.name}&rdquo; also pulls from these (one-way).
              </p>
              {orderedStages.filter((s) => s.stage_id !== settingsStage.stage_id).length === 0 ? (
                <p className="text-xs text-gray-600">No other stages in this project.</p>
              ) : (
                <div className="space-y-1">
                  {orderedStages
                    .filter((s) => s.stage_id !== settingsStage.stage_id)
                    .map((s) => (
                      <label
                        key={s.stage_id}
                        className="flex items-center gap-2 rounded-md px-2 py-1.5 text-sm text-gray-300 hover:bg-surface-hover cursor-pointer"
                      >
                        <input
                          type="checkbox"
                          className="accent-primary"
                          disabled={stageBusy}
                          checked={refIds.includes(s.stage_id)}
                          onChange={() => toggleReference(s.stage_id)}
                        />
                        {s.name}
                      </label>
                    ))}
                </div>
              )}
            </div>

            {/* Team access */}
            <div className="border-t border-border/60 pt-4">
              <p className="text-sm font-medium text-gray-300 flex items-center gap-1.5">
                <Users size={14} className="text-primary" /> Team access
              </p>
              <p className="text-xs text-gray-500 mt-0.5 mb-2">
                Which teams can upload to and see &ldquo;{settingsStage.name}&rdquo;. Org/project
                admins always have full access regardless of this list.
              </p>
              {teams.length === 0 ? (
                <p className="text-xs text-gray-600">No teams in this project yet.</p>
              ) : (
                <div className="space-y-1">
                  {teams.map((t) => (
                    <label
                      key={t.team_id}
                      className="flex items-center gap-2 rounded-md px-2 py-1.5 text-sm text-gray-300 hover:bg-surface-hover cursor-pointer"
                    >
                      <input
                        type="checkbox"
                        className="accent-primary"
                        disabled={stageBusy}
                        checked={teamAccessIds.includes(t.team_id)}
                        onChange={() => toggleTeamAccess(t.team_id)}
                      />
                      {t.name}
                    </label>
                  ))}
                </div>
              )}
            </div>

            {/* Required documents (stage completion checklist) */}
            <div className="border-t border-border/60 pt-4">
              <p className="text-sm font-medium text-gray-300 flex items-center gap-1.5">
                <ListChecks size={14} className="text-primary" /> Required documents
              </p>
              <p className="text-xs text-gray-500 mt-0.5 mb-2">
                What &ldquo;{settingsStage.name}&rdquo; needs to be considered complete. Mandatory items
                drive the requirement-coverage score on the Intelligence page.
              </p>
              {requirementsLoading ? (
                <p className="text-xs text-gray-600">Loading…</p>
              ) : requirements.length === 0 ? (
                <p className="text-xs text-gray-600">No requirements defined yet.</p>
              ) : (
                <div className="space-y-1.5 mb-3">
                  {requirements.map((r) => (
                    <div
                      key={r.requirement_id}
                      className="flex items-start justify-between gap-2 rounded-md border border-border bg-background px-2.5 py-2"
                    >
                      <div className="min-w-0 flex items-start gap-2">
                        <div className="mt-0.5 shrink-0">
                          {r.satisfied ? (
                            <Check size={14} className="text-emerald-400" />
                          ) : (
                            <Circle size={14} className="text-gray-500" />
                          )}
                        </div>
                        <div className="min-w-0">
                          <p className="text-sm text-gray-200 truncate">{r.name}</p>
                          {r.description && (
                            <p className="text-xs text-gray-500 mt-0.5">{r.description}</p>
                          )}
                          {r.satisfied && r.satisfied_by && (
                            <p className="text-xs text-emerald-400/80 mt-1">
                              Satisfied by: {r.satisfied_by.filename}
                            </p>
                          )}
                          {r.satisfied && !r.satisfied_by && (
                            <p className="text-xs text-gray-500 mt-1">
                              Satisfied by a document you don&apos;t have access to.
                            </p>
                          )}
                          <label className="flex items-center gap-1.5 mt-1 text-xs text-gray-400 cursor-pointer w-fit">
                            <input
                              type="checkbox"
                              className="accent-primary"
                              disabled={stageBusy}
                              checked={r.is_mandatory}
                              onChange={() => toggleRequirementMandatory(r)}
                            />
                            Mandatory
                          </label>
                        </div>
                      </div>
                      <button
                        type="button"
                        disabled={stageBusy}
                        onClick={() => removeRequirement(r)}
                        className="shrink-0 text-gray-500 hover:text-red-400 transition-colors p-1 disabled:opacity-50"
                        title="Delete this requirement"
                      >
                        <XIcon size={14} />
                      </button>
                    </div>
                  ))}
                </div>
              )}
              <div className="space-y-2 rounded-md border border-border/60 p-2.5">
                <Input
                  placeholder="Requirement name, e.g. Test Plan"
                  value={newReqName}
                  onChange={(e) => setNewReqName(e.target.value)}
                />
                <Input
                  placeholder="Description (optional)"
                  value={newReqDescription}
                  onChange={(e) => setNewReqDescription(e.target.value)}
                />
                <div className="flex items-center justify-between gap-2">
                  <label className="flex items-center gap-1.5 text-xs text-gray-400 cursor-pointer">
                    <input
                      type="checkbox"
                      className="accent-primary"
                      checked={newReqMandatory}
                      onChange={(e) => setNewReqMandatory(e.target.checked)}
                    />
                    Mandatory
                  </label>
                  <Button
                    size="sm" icon={Plus} loading={stageBusyAction === 'req:create'}
                    disabled={!newReqName.trim() || stageBusy}
                    onClick={addRequirement}
                  >
                    Add requirement
                  </Button>
                </div>
              </div>
            </div>

            {/* Delete */}
            <div className="border-t border-border/60 pt-4 space-y-2">
              <p className="text-sm font-medium text-gray-300">Delete stage</p>
              {countFor(settingsStage.name) > 0 ? (
                <>
                  <p className="text-xs text-gray-500">
                    This stage has {countFor(settingsStage.name)} document(s). Pick a stage to move them to,
                    then delete.
                  </p>
                  <Dropdown
                    options={reassignOptions}
                    value={reassignTo}
                    onChange={setReassignTo}
                    placeholder={reassignOptions.length ? 'Reassign documents to…' : 'No other stage available'}
                  />
                </>
              ) : (
                <p className="text-xs text-gray-500">This stage has no documents and can be deleted.</p>
              )}
              <Button
                variant="danger" icon={Trash2} loading={stageBusyAction === 'delete'}
                disabled={orderedStages.length <= 1 || stageBusy}
                onClick={handleDelete}
              >
                Delete stage
              </Button>
              {orderedStages.length <= 1 && (
                <p className="text-xs text-amber-400">A project must keep at least one stage.</p>
              )}
            </div>
          </div>
        )}
      </Modal>

      {/* Read-only requirements checklist — open to any project member */}
      <RequirementsChecklistModal
        open={Boolean(checklistStage)}
        onClose={() => setChecklistStage(null)}
        projectId={projectId}
        stageId={checklistStage?.stage_id}
        stageName={checklistStage?.name}
      />

      {/* Upload a real document */}
      <Modal
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        title="Upload document"
        description="A real file (PDF, DOCX, TXT, or MD). It's scanned automatically and opens in the Chat Interface for review — nothing is indexed until you finalize there."
        footer={(
          <>
            <Button variant="ghost" onClick={() => setUploadOpen(false)}>Cancel</Button>
            <Button
              icon={UploadCloud}
              loading={uploadBusy}
              disabled={!uploadFile || !uploadStageId || !uploadTeamId}
              onClick={handleUpload}
            >
              Upload
            </Button>
          </>
        )}
      >
        <div className="space-y-4">
          {uploadErr && <p className="text-sm text-red-400">{uploadErr}</p>}

          <div>
            <p className="text-sm font-medium text-gray-300 mb-1.5">File</p>
            <input
              ref={uploadFileRef}
              type="file"
              accept={UPLOAD_ACCEPT}
              onChange={(e) => setUploadFile(e.target.files?.[0] || null)}
              className="block w-full text-sm text-gray-300 file:mr-3 file:rounded-md file:border-0 file:bg-primary/10 file:px-3 file:py-1.5 file:text-primary file:text-sm file:cursor-pointer hover:file:bg-primary/20"
            />
            {uploadFile && (
              <p className="mt-1 text-xs text-gray-500">
                {uploadFile.name} · {(uploadFile.size / 1024).toFixed(1)} KB
              </p>
            )}
          </div>

          <Dropdown
            label="Stage"
            value={uploadStageId}
            onChange={setUploadStageId}
            options={orderedStages.map((s) => ({ label: s.name, value: s.stage_id }))}
            placeholder={orderedStages.length ? 'Select a stage…' : 'No accessible stages'}
          />

          {uploadStageId && (
            eligibleUploadTeams.length === 0 ? (
              <p className="text-xs text-amber-400">
                None of your teams have access to this stage.
              </p>
            ) : eligibleUploadTeams.length === 1 ? (
              <p className="text-xs text-gray-500">
                Uploading as <span className="text-gray-300">{eligibleUploadTeams[0].name}</span>.
              </p>
            ) : (
              <Dropdown
                label="Upload as team"
                value={uploadTeamId}
                onChange={setUploadTeamId}
                options={eligibleUploadTeams.map((t) => ({ label: t.name, value: t.team_id }))}
                placeholder="Select a team…"
              />
            )
          )}

          <Dropdown
            label="Sensitivity"
            value={uploadSensitivity}
            onChange={setUploadSensitivity}
            options={[
              { label: 'Public', value: 'public' },
              { label: 'Internal', value: 'internal' },
              { label: 'Confidential', value: 'confidential' },
            ]}
          />
        </div>
      </Modal>
    </div>
  );
};

export default SourcePanel;
