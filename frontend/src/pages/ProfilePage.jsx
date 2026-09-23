import React, { useEffect, useState } from 'react';
import { User as UserIcon, KeyRound, FolderKanban, CircleHelp } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import { authApi, projectsApi, workspaceApi, setToken } from '../lib/api';
import Card from '../components/ui/Card';
import Input from '../components/ui/Input';
import Button from '../components/ui/Button';
import Modal from '../components/ui/Modal';
import Badge from '../components/ui/Badge';
import BackButton from '../components/ui/BackButton';
import TutorialPopup from '../components/layout/TutorialPopup';
import { ProjectAccessDetail, ACCESS_LABEL, teamBreakdownForProject } from '../components/projects/ProjectAccessModal';

const ProfilePage = () => {
  const { user } = useAuth();
  const [projects, setProjects] = useState([]);
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [workspace, setWorkspace] = useState(null);

  const [passwordModalOpen, setPasswordModalOpen] = useState(false);
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [tutorialOpen, setTutorialOpen] = useState(false);

  useEffect(() => {
    projectsApi.list()
      .then(setProjects)
      .catch(() => setProjects([]))
      .finally(() => setProjectsLoading(false));
    // Per-team stage access isn't part of GET /projects — /workspace already
    // computes it (native TeamStageAccess grants for the caller's own teams),
    // so we pull it in just to resolve "which stage(s) does this team
    // actually see" instead of leaving that unstated next to each role.
    workspaceApi.get().then(setWorkspace).catch(() => setWorkspace(null));
  }, []);

  const closePasswordModal = () => {
    setPasswordModalOpen(false);
    setCurrentPassword('');
    setNewPassword('');
    setConfirmPassword('');
    setError('');
  };

  const handleChangePassword = async (e) => {
    e.preventDefault();
    setError('');
    setNotice('');
    if (newPassword !== confirmPassword) {
      setError('New password and confirmation do not match.');
      return;
    }
    setBusy(true);
    try {
      const { access_token } = await authApi.changePassword(currentPassword, newPassword);
      // The old token is now invalid everywhere (token_version bumped) — the
      // response carries a fresh one for THIS session so the user isn't logged out.
      setToken(access_token);
      setPasswordModalOpen(false);
      setCurrentPassword('');
      setNewPassword('');
      setConfirmPassword('');
      setNotice('Password changed. You have been signed out of every other session.');
    } catch (err) {
      setError(err.detail?.detail || err.message || 'Could not change your password.');
    } finally {
      setBusy(false);
    }
  };

  // Every project the user actually has a role in, paired with a generic
  // accessLevel ('org_admin' | 'project_admin' | 'member') for the
  // project-level badge -- the specific per-team role is stated once, in
  // the per-team breakdown below, so it isn't shown twice for the same
  // project (see ACCESS_LABEL above). Mirrors ProjectsPage.jsx's own
  // accessLevel computation for ProjectCard.
  const myProjects = projects
    .map((p) => {
      const myMemberships = (p.members || []).filter((m) => m.user_id === user?.user_id);
      const isProjectAdmin =
        user?.project_roles?.[p.project_id] === 'project_admin' ||
        myMemberships.some((m) => m.role === 'project_admin');
      const accessLevel = user?.is_org_admin
        ? 'org_admin'
        : isProjectAdmin
          ? 'project_admin'
          : myMemberships.length
            ? 'member'
            : null;
      return { ...p, accessLevel };
    })
    .filter((p) => p.accessLevel);

  return (
    <div className="flex-1 p-8 max-w-2xl mx-auto w-full">
      <div className="mb-6">
        <BackButton fallbackTo="/" className="mb-4" />
        <h1 className="text-2xl font-bold text-gray-100 flex items-center gap-2">
          <UserIcon className="text-primary" size={22} />
          Profile
        </h1>
        <p className="text-gray-400 mt-1">Your account details and security settings.</p>
      </div>

      <div className="space-y-6">
        <Card title="Account">
          <div className="space-y-3">
            <div className="space-y-1">
              {user?.full_name && user.full_name !== user.email && (
                <p className="text-sm text-gray-200">{user.full_name}</p>
              )}
              <p className={user?.full_name && user.full_name !== user.email ? 'text-sm text-gray-500' : 'text-sm text-gray-200'}>
                {user?.email}
              </p>
            </div>
            {user?.is_org_admin && (
              <Badge variant="active">Organization Admin</Badge>
            )}
            <Button type="button" variant="secondary" icon={KeyRound} onClick={() => setPasswordModalOpen(true)}>
              Change password
            </Button>
            {notice && <p className="text-sm text-emerald-400">{notice}</p>}
          </div>
        </Card>

        <Card
          title="Your access"
          description={
            user?.is_org_admin
              ? 'As an Organization Admin, you have access to every project in the organization.'
              : "The projects and roles you've been granted."
          }
        >
          {projectsLoading ? (
            <div className="flex justify-center py-6"><div className="w-6 h-6 border-4 border-primary/30 border-t-primary rounded-full animate-spin" /></div>
          ) : user?.is_org_admin ? (
            <p className="text-sm text-gray-400">{projects.length} project{projects.length === 1 ? '' : 's'} in the organization.</p>
          ) : myProjects.length === 0 ? (
            <p className="text-sm text-gray-500">You don't have a role on any project yet.</p>
          ) : (
            <div className="space-y-3">
              {myProjects.map((p) => (
                <div key={p.project_id} className="rounded-lg border border-border bg-background/60 overflow-hidden">
                  <div className="flex items-center justify-between gap-3 px-3 py-2.5">
                    <span className="flex items-center gap-2 text-sm text-gray-200 min-w-0">
                      <FolderKanban size={15} className="text-primary shrink-0" />
                      <span className="truncate">{p.project_name}</span>
                    </span>
                    <Badge variant="neutral">{ACCESS_LABEL[p.accessLevel] || p.accessLevel}</Badge>
                  </div>
                  <div className="border-t border-border/50 px-3 py-3">
                    <ProjectAccessDetail
                      accessLevel={p.accessLevel}
                      myTeams={teamBreakdownForProject(p, workspace, user?.user_id, p.accessLevel)}
                      showSummary={false}
                    />
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card title="Help" description="Revisit the getting-started walkthrough for your role.">
          <Button type="button" variant="secondary" icon={CircleHelp} onClick={() => setTutorialOpen(true)}>
            Revisit tutorial
          </Button>
        </Card>
      </div>

      <Modal
        open={passwordModalOpen}
        onClose={closePasswordModal}
        title="Change password"
        description="Changing your password signs you out of every other session immediately."
      >
        <form onSubmit={handleChangePassword} className="space-y-4">
          <Input
            label="Current password"
            type="password"
            required
            value={currentPassword}
            onChange={(e) => setCurrentPassword(e.target.value)}
            placeholder="••••••••"
          />
          <Input
            label="New password"
            type="password"
            required
            minLength={8}
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            placeholder="At least 8 characters"
          />
          <Input
            label="Confirm new password"
            type="password"
            required
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            placeholder="••••••••"
          />

          {error && <p className="text-sm text-red-400">{error}</p>}

          <div className="flex justify-end gap-3">
            <Button type="button" variant="secondary" onClick={closePasswordModal}>Cancel</Button>
            <Button type="submit" icon={KeyRound} loading={busy}>
              Change password
            </Button>
          </div>
        </form>
      </Modal>

      <TutorialPopup open={tutorialOpen} onClose={() => setTutorialOpen(false)} />
    </div>
  );
};

export default ProfilePage;
