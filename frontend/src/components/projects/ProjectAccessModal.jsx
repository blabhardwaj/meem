import React, { useState } from 'react';
import { ShieldCheck, Users, Layers } from 'lucide-react';
import Modal from '../ui/Modal';
import Badge from '../ui/Badge';
import Dropdown from '../ui/Dropdown';

export const ACCESS_LABEL = {
  org_admin: 'Organization Admin',
  project_admin: 'Project Admin',
  member: 'Member',
};

export const ROLE_LABEL = {
  team_lead: 'Team Lead',
  contributor: 'Contributor',
  viewer: 'Viewer',
  project_admin: 'Project Admin',
  org_admin: 'Organization Admin',
};

// Above this many teams on one project, listing every one of them inline
// gets long enough that picking one from a dropdown reads better.
const TEAM_DROPDOWN_THRESHOLD = 5;

// The caller's own per-team roles on `project`, each paired with the
// stage(s) that team natively has access to -- for ProjectAccessDetail's
// "Per-team roles" section. `project` is one entry from GET /projects (has
// `.members`, each a distinct role: a UserTeamMembership row OR a
// ProjectAdmin row); `workspace` is GET /workspace's response (has the
// team_id + stage-level team_access this doesn't carry); `accessLevel` is
// the caller's already-computed 'org_admin' | 'project_admin' | 'member'.
//
// An org/project admin's blanket access already reads from the badge alone
// (ProjectAccessDetail skips this section entirely when it comes back
// empty) -- so for them this deliberately returns ONLY a genuine, explicit
// per-team role held ALONGSIDE that admin status, never a synthetic "every
// team in the project" listing (workspace hands admins exactly that
// blanket list -- every team, role forced to their admin level -- which
// would just repeat the same admin badge N times with nothing left to
// actually explain).
export function teamBreakdownForProject(project, workspace, userId, accessLevel) {
  const wsProject = workspace?.projects?.find((w) => w.project_id === project.project_id);
  if (!wsProject) return [];

  const stagesForTeamId = (teamId) =>
    wsProject.stages.filter((s) => s.team_access.includes(teamId)).map((s) => s.name);

  if (accessLevel === 'member') {
    // /workspace's own team list for this project is already exactly the
    // caller's memberships (no admin-wide scoping applies).
    return wsProject.teams.map((t) => ({ team: t.name, role: t.role, stageNames: stagesForTeamId(t.team_id) }));
  }

  const myMemberships = (project.members || []).filter((m) => m.user_id === userId);
  return myMemberships
    .filter((m) => m.role !== 'project_admin' && m.team_name)
    .map((m) => {
      const wsTeam = wsProject.teams.find((t) => t.name === m.team_name);
      return { team: m.team_name, role: m.role, stageNames: wsTeam ? stagesForTeamId(wsTeam.team_id) : null };
    });
}

function TeamRow({ t }) {
  return (
    <div className="px-3 py-2.5 space-y-1.5 bg-background/60">
      <span className="flex items-center gap-2 text-sm">
        <Users size={13} className="text-gray-500 shrink-0" />
        <span className="text-gray-200 font-medium truncate">{t.team}</span>
        <Badge variant="neutral" className="ml-auto">{ROLE_LABEL[t.role] || t.role}</Badge>
      </span>
      <span className="flex items-start gap-2 text-xs text-gray-500 pl-[21px]">
        <Layers size={12} className="mt-0.5 shrink-0" />
        <span>
          {t.stageNames === null
            ? 'Loading stage details…'
            : t.stageNames.length > 0
              ? t.stageNames.join(', ')
              : 'No stage access yet'}
        </span>
      </span>
    </div>
  );
}

// The actual "your access" breakdown — used both inline (Profile page, no
// click needed) and inside ProjectAccessModal's popup (the project card
// grid), so the two surfaces never drift apart.
//
// props:
//   accessLevel — 'org_admin' | 'project_admin' | 'member'
//   myTeams     — [{ team, role, stageNames }] the current user's per-team
//                 roles in THIS project, plus the stage(s) each team
//                 natively has access to. stageNames is null while that
//                 detail is still loading, [] once loaded with none.
//   showSummary — the shield-icon "Member"/"Project Admin" line. Default on
//                 (the modal has nothing else stating it); pass false when
//                 embedding this inline right under a row that already
//                 carries that same label as its own badge, so it isn't
//                 stated twice in the same glance.
export function ProjectAccessDetail({ accessLevel = 'member', myTeams = [], showSummary = true }) {
  const accessLabel = ACCESS_LABEL[accessLevel];
  const [selectedTeam, setSelectedTeam] = useState('');
  const showTeamDropdown = myTeams.length > TEAM_DROPDOWN_THRESHOLD;
  const activeTeam = showTeamDropdown
    ? myTeams.find((t) => t.team === selectedTeam) || myTeams[0]
    : null;

  const summary = showSummary && (
    <div className="flex items-center gap-2">
      <ShieldCheck size={16} className="text-primary" />
      <span className="text-gray-200">{accessLabel}</span>
    </div>
  );

  // Blanket access (org/project admin) with no distinct team role of their
  // own on this project: there's nothing specific to explain -- which team,
  // which stage -- beyond the badge itself, so no filler boilerplate here.
  if (myTeams.length === 0 && accessLevel !== 'member') {
    return summary || null;
  }

  return (
    <div className="space-y-4 text-sm">
      {summary}

      <div>
        {myTeams.length > 0 && (
          <p className="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-2">Per-team roles</p>
        )}
        {myTeams.length ? (
          showTeamDropdown ? (
            <div className="space-y-2">
              <Dropdown
                label={`Team (${myTeams.length} total)`}
                options={myTeams.map((t) => ({ label: t.team, value: t.team }))}
                value={activeTeam.team}
                onChange={setSelectedTeam}
              />
              <div className="rounded-lg border border-border overflow-hidden">
                <TeamRow t={activeTeam} />
              </div>
            </div>
          ) : (
            <div className="rounded-lg border border-border divide-y divide-border/50 overflow-hidden">
              {myTeams.map((t) => <TeamRow key={t.team} t={t} />)}
            </div>
          )
        ) : (
          <p className="text-gray-500">You are not a member of any team in this project.</p>
        )}
      </div>
    </div>
  );
}

// Shared "Your access in this project" detail popup -- opened from a click
// on the access badge on the project card grid (ProjectsPage).
//
// props:
//   projectName — for the modal's description line
//   accessLevel, myTeams — see ProjectAccessDetail above
export default function ProjectAccessModal({ open, onClose, projectName, accessLevel = 'member', myTeams = [] }) {
  return (
    <Modal open={open} onClose={onClose} title="Your access in this project" description={projectName}>
      <ProjectAccessDetail accessLevel={accessLevel} myTeams={myTeams} />
    </Modal>
  );
}
