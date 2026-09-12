import React, { useState, useEffect, useMemo } from 'react';
import { UploadCloud } from 'lucide-react';
import Modal from '../ui/Modal';
import Dropdown from '../ui/Dropdown';
import Button from '../ui/Button';
import { workspaceApi, agentsApi, clearWorkspaceCache } from '../../lib/api';

const SENSITIVITY_OPTIONS = [
  { label: 'Public', value: 'public' },
  { label: 'Internal', value: 'internal' },
  { label: 'Confidential', value: 'confidential' },
];

export default function UploadToProjectModal({
  open,
  onClose,
  draftId,
  defaultTitle = 'Document',
  initialProjectId = '',
  onSuccess,
}) {
  const [workspace, setWorkspace] = useState(null);
  const [loadingWs, setLoadingWs] = useState(false);
  const [projectId, setProjectId] = useState(initialProjectId || '');
  const [stageId, setStageId] = useState('');
  const [teamId, setTeamId] = useState('');
  const [sensitivity, setSensitivity] = useState('internal');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open) return;
    setError('');
    setLoadingWs(true);
    workspaceApi
      .get()
      .then((ws) => {
        setWorkspace(ws);
        const projects = ws?.projects || [];
        if (projects.length > 0) {
          const matchedProj = initialProjectId && projects.some((p) => p.project_id === initialProjectId)
            ? initialProjectId
            : projects[0].project_id;
          setProjectId(matchedProj);
        }
      })
      .catch((err) => {
        setError(err.message || 'Could not load workspace projects.');
      })
      .finally(() => {
        setLoadingWs(false);
      });
  }, [open, initialProjectId]);

  const projects = workspace?.projects || [];
  const selectedProject = useMemo(
    () => projects.find((p) => p.project_id === projectId) || null,
    [projects, projectId],
  );

  const stages = selectedProject?.stages || [];
  const teams = selectedProject?.teams || [];

  // Update default stage and team when project changes
  useEffect(() => {
    if (stages.length > 0 && (!stageId || !stages.some((s) => s.stage_id === stageId))) {
      setStageId(stages[0].stage_id);
    }
    if (teams.length > 0 && (!teamId || !teams.some((t) => t.team_id === teamId))) {
      setTeamId(teams[0].team_id);
    }
  }, [selectedProject, stages, teams, stageId, teamId]);

  const projectOptions = useMemo(
    () => projects.map((p) => ({ label: p.name, value: p.project_id })),
    [projects],
  );

  const stageOptions = useMemo(
    () => stages.map((s) => ({ label: s.name, value: s.stage_id })),
    [stages],
  );

  const teamOptions = useMemo(
    () => teams.map((t) => ({ label: t.name, value: t.team_id })),
    [teams],
  );

  const handleUpload = async () => {
    if (!draftId) {
      setError('No finalized draft reference found.');
      return;
    }
    if (!projectId) {
      setError('Please select a project.');
      return;
    }
    if (!stageId) {
      setError('Please select a stage.');
      return;
    }
    if (!teamId) {
      setError('Please select a team.');
      return;
    }

    setSubmitting(true);
    setError('');

    try {
      const res = await agentsApi.uploadDraftToProject({
        draftId,
        projectId,
        stageId,
        teamId,
        sensitivityLevel: sensitivity,
      });

      clearWorkspaceCache();
      onSuccess?.({
        ...res,
        projectName: selectedProject?.name || 'Project',
        stageName: res.stage_name || stages.find((s) => s.stage_id === stageId)?.name || 'Stage',
      });
      onClose();
    } catch (err) {
      setError(err.message || 'Failed to upload document to project.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={() => !submitting && onClose()}
      title="Upload to Project"
      description={`Upload "${defaultTitle}" directly into a project without re-downloading.`}
      footer={
        <>
          <Button type="button" variant="ghost" onClick={onClose} disabled={submitting}>
            Cancel
          </Button>
          <Button
            type="button"
            icon={UploadCloud}
            onClick={handleUpload}
            loading={submitting}
            disabled={loadingWs || !projectId || !stageId || !teamId}
          >
            Upload
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        {error && (
          <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-xs text-red-300">
            {error}
          </div>
        )}

        {loadingWs ? (
          <div className="flex justify-center py-6">
            <div className="w-6 h-6 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
          </div>
        ) : (
          <>
            <Dropdown
              label="Project"
              options={projectOptions}
              value={projectId}
              onChange={(val) => {
                setProjectId(val);
                setStageId('');
                setTeamId('');
              }}
              placeholder={projectOptions.length ? 'Select project' : 'No accessible projects'}
            />

            <Dropdown
              label="Stage"
              options={stageOptions}
              value={stageId}
              onChange={setStageId}
              placeholder={stageOptions.length ? 'Select stage' : 'No stages available'}
            />

            <Dropdown
              label="Team"
              options={teamOptions}
              value={teamId}
              onChange={setTeamId}
              placeholder={teamOptions.length ? 'Select team' : 'No teams available'}
            />

            <Dropdown
              label="Sensitivity"
              options={SENSITIVITY_OPTIONS}
              value={sensitivity}
              onChange={setSensitivity}
            />

            <p className="text-[11px] text-gray-500">
              The finalized document has already been verified and will be saved directly into the destination stage.
            </p>
          </>
        )}
      </div>
    </Modal>
  );
}
