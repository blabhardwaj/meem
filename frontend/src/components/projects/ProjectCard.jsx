import React, { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { Folder } from 'lucide-react';
import Card from '../ui/Card';
import Badge from '../ui/Badge';
import ProjectAccessModal, { ACCESS_LABEL } from './ProjectAccessModal';

// props:
//   project     — { project_id, project_name, description }
//   accessLevel — 'org_admin' | 'project_admin' | 'member'
//   myTeams     — [{ team, role, stageNames }] the current user's per-team
//                 roles in THIS project, plus the stage(s) each team
//                 natively has access to. stageNames is null while that
//                 detail is still loading, [] once loaded with none.
const ProjectCard = ({ project, accessLevel = 'member', myTeams = [] }) => {
  const navigate = useNavigate();
  const [scopeOpen, setScopeOpen] = useState(false);

  const accessLabel = ACCESS_LABEL[accessLevel];
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
          </div>
        }
      />

      <ProjectAccessModal
        open={scopeOpen}
        onClose={() => setScopeOpen(false)}
        projectName={project.project_name}
        accessLevel={accessLevel}
        myTeams={myTeams}
      />
    </>
  );
};

export default ProjectCard;
