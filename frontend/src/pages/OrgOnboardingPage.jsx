import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Sparkles, Check, Plus, Trash2, Copy, ArrowRight, ArrowLeft } from 'lucide-react';
import { projectsApi, stagesApi, teamsApi, adminApi } from '../lib/api';
import Input from '../components/ui/Input';
import Textarea from '../components/ui/Textarea';
import Button from '../components/ui/Button';
import Dropdown from '../components/ui/Dropdown';

// Master Plan v2, item 16 (deferred out of item 11, built later as its own
// product decision): a guided first-run wizard for a brand-new org —
// project -> invite teammates -> stages. Nothing here is a NEW capability;
// every step calls the exact same endpoints AdminPage/ProjectsPage/
// ProjectWorkspace already use elsewhere. This just sequences them into one
// screen for a fresh org's first few minutes instead of leaving the admin
// to discover each flow separately.

const STEPS = [
  { id: 'project', label: 'Create a project' },
  { id: 'invite', label: 'Invite your team' },
  { id: 'stages', label: 'Set up stages' },
];

const STAGE_PRESETS = [
  {
    label: 'Discovery → Build → Review → Launch',
    stages: [
      { name: 'Discovery', requires_approval: false },
      { name: 'Build', requires_approval: false },
      { name: 'Review', requires_approval: true },
      { name: 'Launch', requires_approval: true },
    ],
  },
  {
    label: 'Requirements → Design → Development → Testing',
    stages: [
      { name: 'Requirements', requires_approval: false },
      { name: 'Design', requires_approval: false },
      { name: 'Development', requires_approval: false },
      { name: 'Testing', requires_approval: true },
    ],
  },
  {
    label: 'Draft → Internal Review → Approved',
    stages: [
      { name: 'Draft', requires_approval: false },
      { name: 'Internal Review', requires_approval: true },
      { name: 'Approved', requires_approval: true },
    ],
  },
];

const ROLE_OPTIONS = [
  { label: 'Viewer', value: 'viewer' },
  { label: 'Contributor', value: 'contributor' },
  { label: 'Team Lead', value: 'team_lead' },
];

const StepIndicator = ({ stepIndex }) => (
  <div className="flex gap-1 border-b border-border mb-8">
    {STEPS.map((s, i) => (
      <div
        key={s.id}
        className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors
          ${i === stepIndex ? 'border-primary text-primary-light' : i < stepIndex ? 'border-transparent text-gray-300' : 'border-transparent text-gray-500'}`}
      >
        <span className={`flex items-center justify-center w-5 h-5 rounded-full text-xs shrink-0
          ${i < stepIndex ? 'bg-primary text-white' : i === stepIndex ? 'border border-primary text-primary-light' : 'border border-border text-gray-500'}`}
        >
          {i < stepIndex ? <Check size={12} /> : i + 1}
        </span>
        {s.label}
      </div>
    ))}
  </div>
);

const OrgOnboardingPage = () => {
  const navigate = useNavigate();
  const [stepIndex, setStepIndex] = useState(0);
  const [error, setError] = useState('');

  // Step 1: project
  const [projectName, setProjectName] = useState('');
  const [projectDesc, setProjectDesc] = useState('');
  const [creatingProject, setCreatingProject] = useState(false);
  const [project, setProject] = useState(null);
  const [team, setTeam] = useState(null);

  // Step 2: invite
  const [inviteEmail, setInviteEmail] = useState('');
  const [inviteRole, setInviteRole] = useState('contributor');
  const [invitesBusy, setInvitesBusy] = useState(false);
  const [invites, setInvites] = useState([]); // [{email, role, invite_link}]

  // Step 3: stages
  const [stages, setStages] = useState([]);
  const [stagesLoading, setStagesLoading] = useState(false);
  const [stagesSaving, setStagesSaving] = useState(false);
  const [newStageName, setNewStageName] = useState('');

  const loadStagesFromProject = async (projectId) => {
    setStagesLoading(true);
    try {
      const rows = await stagesApi.list(projectId);
      setStages(rows.map((s) => ({
        stage_id: s.stage_id, name: s.name, requires_approval: s.requires_approval, isNew: false,
      })));
    } catch (err) {
      setError(err.message || 'Could not load stages.');
    } finally {
      setStagesLoading(false);
    }
  };

  const handleCreateProject = async () => {
    if (!projectName.trim()) return;
    setCreatingProject(true);
    setError('');
    try {
      const created = await projectsApi.create({ project_name: projectName.trim(), description: projectDesc.trim() || null });
      setProject(created);
      const teams = await teamsApi.list(created.project_id);
      setTeam(teams[0] || null);
      setStepIndex(1);
    } catch (err) {
      setError(err.message || 'Could not create the project.');
    } finally {
      setCreatingProject(false);
    }
  };

  const handleSendInvite = async () => {
    if (!inviteEmail.trim() || !project || !team) return;
    setInvitesBusy(true);
    setError('');
    try {
      const res = await adminApi.invite(inviteEmail.trim(), inviteRole, {
        projectId: project.project_id, teamId: team.team_id,
      });
      setInvites((cur) => [...cur, { email: res.email, role: res.role, invite_link: res.invite_link }]);
      setInviteEmail('');
    } catch (err) {
      setError(err.message || 'Could not send this invite.');
    } finally {
      setInvitesBusy(false);
    }
  };

  const goToStages = () => {
    if (project) loadStagesFromProject(project.project_id);
    setStepIndex(2);
  };

  const applyPreset = (preset) => {
    setStages(preset.stages.map((s) => ({ ...s, stage_id: null, isNew: true })));
  };

  const addStage = () => {
    if (!newStageName.trim()) return;
    setStages((cur) => [...cur, { stage_id: null, name: newStageName.trim(), requires_approval: false, isNew: true }]);
    setNewStageName('');
  };

  const removeStage = (index) => {
    setStages((cur) => cur.filter((_, i) => i !== index));
  };

  const toggleApproval = (index) => {
    setStages((cur) => cur.map((s, i) => i === index ? { ...s, requires_approval: !s.requires_approval } : s));
  };

  const renameStage = (index, name) => {
    setStages((cur) => cur.map((s, i) => i === index ? { ...s, name } : s));
  };

  const finishStages = async () => {
    if (!project) return;
    setStagesSaving(true);
    setError('');
    try {
      // Reconcile against the project's current (seeded) stages: delete ones
      // no longer present, update existing, create new ones — same three
      // operations ProjectWorkspace's own stage-settings UI already uses.
      // Order matters: create new stages BEFORE deleting old ones — the
      // backend refuses to delete a project's last remaining stage ("A
      // project must keep at least one stage"), which a preset that
      // replaces every seeded stage would otherwise hit.
      const existing = await stagesApi.list(project.project_id);
      const keptIds = new Set(stages.filter((s) => s.stage_id).map((s) => s.stage_id));

      for (let i = 0; i < stages.length; i += 1) {
        const s = stages[i];
        if (s.stage_id) {
          const original = existing.find((e) => e.stage_id === s.stage_id);
          if (original && (original.name !== s.name || original.requires_approval !== s.requires_approval)) {
            await stagesApi.update(project.project_id, s.stage_id, { name: s.name, requires_approval: s.requires_approval });
          }
        } else {
          const createdStage = await stagesApi.create(project.project_id, { name: s.name, order_index: i });
          if (s.requires_approval) {
            await stagesApi.update(project.project_id, createdStage.stage_id, { requires_approval: true });
          }
        }
      }

      for (const ex of existing) {
        if (!keptIds.has(ex.stage_id)) {
          await stagesApi.remove(project.project_id, ex.stage_id);
        }
      }

      navigate(`/projects/${project.project_id}`);
    } catch (err) {
      setError(err.message || 'Could not save stages.');
    } finally {
      setStagesSaving(false);
    }
  };

  const copyLink = async (link) => {
    try {
      await navigator.clipboard.writeText(link);
    } catch {
      // clipboard unavailable — the link is still visible/selectable as text
    }
  };

  return (
    <div className="min-h-screen bg-background flex items-start justify-center p-5 lg:p-10">
      <div className="w-full max-w-2xl">
        <div className="text-center mb-8">
          <div className="inline-flex items-center gap-2 text-2xl font-bold text-gray-100">
            <Sparkles className="text-primary" size={26} />
            DocFlow <span className="text-primary">AI</span>
          </div>
          <p className="text-gray-400 mt-2">Let&rsquo;s get your organization set up.</p>
        </div>

        <StepIndicator stepIndex={stepIndex} />

        {error && <p className="text-sm text-red-400 mb-4">{error}</p>}

        <div className="bg-surface border border-border rounded-xl p-6 shadow-lg shadow-black/20">
          {stepIndex === 0 && (
            <div className="space-y-4">
              <h2 className="text-lg font-semibold text-gray-100">Create your first project</h2>
              <p className="text-sm text-gray-400">
                A project holds your team&rsquo;s documents, stages, and approvals. You can create more later.
              </p>
              <Input
                label="Project name"
                required
                value={projectName}
                onChange={(e) => setProjectName(e.target.value)}
                placeholder="e.g. Q3 Product Launch"
              />
              <Textarea
                label="Description (optional)"
                value={projectDesc}
                onChange={(e) => setProjectDesc(e.target.value)}
                placeholder="What is this project about?"
              />
              <div className="flex justify-end pt-2">
                <Button icon={ArrowRight} onClick={handleCreateProject} loading={creatingProject} disabled={!projectName.trim()}>
                  Create project &amp; continue
                </Button>
              </div>
            </div>
          )}

          {stepIndex === 1 && (
            <div className="space-y-5">
              <div>
                <h2 className="text-lg font-semibold text-gray-100">Invite your team</h2>
                <p className="text-sm text-gray-400 mt-1">
                  Each invite is a single-use link (expires in 72 hours) that adds the teammate directly to{' '}
                  <span className="text-gray-200">{project?.project_name}</span>&rsquo;s {team?.name} team. Skip this and invite people later from Admin if you&rsquo;d rather.
                </p>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-[1fr_160px_auto] gap-3 items-end">
                <Input
                  label="Email"
                  value={inviteEmail}
                  onChange={(e) => setInviteEmail(e.target.value)}
                  placeholder="teammate@company.com"
                />
                <Dropdown label="Role" options={ROLE_OPTIONS} value={inviteRole} onChange={setInviteRole} />
                <Button icon={Plus} onClick={handleSendInvite} loading={invitesBusy} disabled={!inviteEmail.trim()}>
                  Invite
                </Button>
              </div>

              {invites.length > 0 && (
                <div className="space-y-2 pt-2 border-t border-border/60">
                  {invites.map((inv, i) => (
                    <div key={i} className="flex items-center justify-between gap-3 rounded-md border border-border bg-background px-3 py-2">
                      <div className="min-w-0">
                        <p className="text-sm text-gray-200 truncate">{inv.email}</p>
                        <p className="text-xs text-gray-500">{inv.role}</p>
                      </div>
                      <Button size="sm" variant="ghost" icon={Copy} onClick={() => copyLink(inv.invite_link)}>
                        Copy link
                      </Button>
                    </div>
                  ))}
                </div>
              )}

              <div className="flex justify-between pt-2">
                <Button variant="ghost" icon={ArrowLeft} onClick={() => setStepIndex(0)}>Back</Button>
                <Button icon={ArrowRight} onClick={goToStages}>
                  {invites.length > 0 ? 'Continue' : 'Skip for now'}
                </Button>
              </div>
            </div>
          )}

          {stepIndex === 2 && (
            <div className="space-y-5">
              <div>
                <h2 className="text-lg font-semibold text-gray-100">Set up stages</h2>
                <p className="text-sm text-gray-400 mt-1">
                  Stages are the pipeline documents move through. Pick a starting point and customize it, or keep the default.
                </p>
              </div>

              <div className="flex flex-wrap gap-2">
                {STAGE_PRESETS.map((preset) => (
                  <Button key={preset.label} size="sm" variant="secondary" onClick={() => applyPreset(preset)}>
                    {preset.label}
                  </Button>
                ))}
              </div>

              {stagesLoading ? (
                <p className="text-sm text-gray-500">Loading current stages...</p>
              ) : (
                <div className="space-y-2">
                  {stages.map((s, i) => (
                    <div key={i} className="flex items-center gap-2 rounded-md border border-border bg-background px-3 py-2">
                      <Input
                        value={s.name}
                        onChange={(e) => renameStage(i, e.target.value)}
                        className="h-8"
                      />
                      <label className="flex items-center gap-1.5 text-xs text-gray-400 shrink-0 whitespace-nowrap">
                        <input type="checkbox" checked={s.requires_approval} onChange={() => toggleApproval(i)} className="accent-primary" />
                        Requires approval
                      </label>
                      <button
                        type="button"
                        onClick={() => removeStage(i)}
                        className="shrink-0 rounded-md p-1.5 text-gray-500 hover:text-red-400 hover:bg-red-500/10 transition-colors"
                        aria-label={`Remove stage ${s.name}`}
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  ))}
                  <div className="flex items-center gap-2 pt-1">
                    <Input
                      value={newStageName}
                      onChange={(e) => setNewStageName(e.target.value)}
                      placeholder="Add a custom stage..."
                      className="h-8"
                      onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addStage(); } }}
                    />
                    <Button size="sm" variant="ghost" icon={Plus} onClick={addStage} disabled={!newStageName.trim()}>
                      Add
                    </Button>
                  </div>
                </div>
              )}

              <div className="flex justify-between pt-2">
                <Button variant="ghost" icon={ArrowLeft} onClick={() => setStepIndex(1)}>Back</Button>
                <Button icon={Check} onClick={finishStages} loading={stagesSaving} disabled={stages.length === 0}>
                  Finish setup
                </Button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default OrgOnboardingPage;
