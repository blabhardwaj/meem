import React, { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { Folder, ShieldCheck } from 'lucide-react';
import Card from '../ui/Card';
import Badge from '../ui/Badge';
import Modal from '../ui/Modal';

const ACCESS_LABEL = {
  org_admin: 'Organization Admin',
  project_admin: 'Project Admin',
  member: 'Member',
};

const ROLE_LABEL = {
  team_lead: 'Team Lead',
  contributor: 'Contributor',
  viewer: 'Viewer',
};

// props:
//   project     — { project_id, project_name, description }
//   accessLevel — 'org_admin' | 'project_admin' | 'member'
//   myTeams     — [{ team, role }] the current user's per-team roles in THIS project
const ProjectCard = ({ project, accessLevel = 'member', myTeams = [] }) => {
  const navigate = useNavigate();
  const [scopeOpen, setScopeOpen] = useState(false);

  const accessLabel = ACCESS_LABEL[accessLevel];
  const seesAllTeams = accessLevel === 'org_admin' || accessLevel === 'project_admin';
  const teamChips = seesAllTeams
    ? ['All teams']
    : myTeams.length
      ? myTeams.map((t) => t.team)
      : ['No team assigned'];
  const isTeamLeadHere = myTeams.some((t) => t.role === 'team_lead');
  const canManage = seesAllTeams || isTeamLeadHere;

  const stop = (event) => event.stopPropagation();

  return (
    <>
      <Card
        title={(
          <Link
            to={`/projects/${encodeURIComponent(project.project_id)}`}
            onClick={stop}
            className="hover:text-primary transition-colors"
          >
            {project.project_name}
          </Link>
        )}
        description={project.description || 'No description yet.'}
        icon={Folder}
        className="h-full min-h-[220px] flex flex-col"
        hoverable
        onClick={() => navigate(`/projects/${project.project_id}`)}
        footer={
          <div className="flex items-center justify-between gap-3">
            <button
              type="button"
              onClick={(event) => { stop(event); setScopeOpen(true); }}
              className="rounded-full focus:outline-none focus:ring-2 focus:ring-primary/50"
              title="View your access in this project"
            >
              <Badge variant="neutral" className="cursor-pointer hover:border-primary/50">
                {accessLabel}
              </Badge>
            </button>
            {canManage && (
              <Link
                to={`/admin?project_id=${encodeURIComponent(project.project_id)}`}
                onClick={stop}
                className="text-sm text-primary-light hover:text-primary underline underline-offset-2"
              >
                {seesAllTeams ? 'Manage project access' : 'Manage team access'}
              </Link>
            )}
          </div>
        }
      >
        <div>
          <p className="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-2">Assigned teams</p>
          <div className="flex flex-wrap gap-1.5">
            {teamChips.map((team) => (
              <Badge key={team} variant="neutral">{team}</Badge>
            ))}
          </div>
        </div>
      </Card>

      <Modal
        open={scopeOpen}
        onClose={() => setScopeOpen(false)}
        title="Your access in this project"
        description={project.project_name}
      >
        <div className="space-y-4 text-sm">
          <div className="flex items-center gap-2">
            <ShieldCheck size={16} className="text-primary" />
            <span className="text-gray-200">{accessLabel}</span>
          </div>

          {accessLevel === 'org_admin' && (
            <p className="text-gray-400">
              As an organization admin you have full access to every team and workflow in this project.
            </p>
          )}
          {accessLevel === 'project_admin' && (
            <p className="text-gray-400">
              As a project admin you can act across all teams in this project and review its documents.
            </p>
          )}

          <div>
            <p className="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-2">Per-team roles</p>
            {myTeams.length ? (
              <table className="w-full text-left">
                <thead>
                  <tr className="text-xs text-gray-500 border-b border-border/50">
                    <th className="py-1.5 pr-4 font-medium">Team</th>
                    <th className="py-1.5 font-medium">Role</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border/30">
                  {myTeams.map((t) => (
                    <tr key={t.team} className="text-gray-300">
                      <td className="py-2 pr-4">{t.team}</td>
                      <td className="py-2"><Badge variant="neutral">{ROLE_LABEL[t.role] || t.role}</Badge></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : accessLevel === 'org_admin' ? (
              <p className="text-gray-400">
                <span className="text-gray-300">Not applicable</span> — your organization admin
                status grants full access to every team automatically, without needing
                individual team assignments.
              </p>
            ) : accessLevel === 'project_admin' ? (
              <p className="text-gray-400">
                <span className="text-gray-300">Not applicable</span> — your project admin status
                grants full access to every team in this project automatically, without needing
                individual team assignments.
              </p>
            ) : (
              <p className="text-gray-500">You are not a member of any team in this project.</p>
            )}
          </div>
        </div>
      </Modal>
    </>
  );
};

export default ProjectCard;
