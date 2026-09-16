import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Search } from 'lucide-react';
import ProjectCard from '../components/projects/ProjectCard';
import CreateProjectCard from '../components/projects/CreateProjectCard';
import Input from '../components/ui/Input';
import Modal from '../components/ui/Modal';
import Button from '../components/ui/Button';
import Textarea from '../components/ui/Textarea';
import { projectsApi } from '../lib/api';
import { useAuth } from '../context/AuthContext';

const slugify = (name) =>
  `${name.toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '') || 'project'}-${Date.now().toString(36)}`;

const getKolkataGreeting = () => {
  const hour = Number(
    new Intl.DateTimeFormat('en-IN', {
      timeZone: 'Asia/Kolkata',
      hour: 'numeric',
      hour12: false,
    }).format()
  );

  if (hour < 5) return 'Working late';
  if (hour < 12) return 'Good morning';
  if (hour < 17) return 'Good afternoon';
  return 'Good evening';
};

const ProjectsPage = () => {
  const navigate = useNavigate();
  const { user } = useAuth();
  const [greeting, setGreeting] = useState(getKolkataGreeting);
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [searchQuery, setSearchQuery] = useState('');
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [creating, setCreating] = useState(false);

  const [newProjectName, setNewProjectName] = useState('');
  const [newProjectDesc, setNewProjectDesc] = useState('');

  const loadProjects = async () => {
    setLoading(true);
    setError('');
    try {
      const data = await projectsApi.list();
      setProjects(data);
    } catch (err) {
      setError(err.message || 'Could not load projects.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadProjects();
    const interval = window.setInterval(loadProjects, 15 * 1000);
    const refreshOnFocus = () => loadProjects();
    window.addEventListener('focus', refreshOnFocus);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener('focus', refreshOnFocus);
    };
  }, []);

  useEffect(() => {
    const updateGreeting = () => setGreeting(getKolkataGreeting());
    const interval = window.setInterval(updateGreeting, 60 * 1000);
    return () => window.clearInterval(interval);
  }, []);

  const filteredProjects = projects.filter(
    (p) =>
      p.project_name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      (p.description || '').toLowerCase().includes(searchQuery.toLowerCase())
  );
  const canCreateProject = Boolean(user?.is_org_admin);

  const handleCreateProject = async () => {
    if (!newProjectName.trim()) return;
    setCreating(true);
    try {
      const project = await projectsApi.create({
        project_id: slugify(newProjectName),
        project_name: newProjectName.trim(),
        description: newProjectDesc.trim() || null,
      });
      setIsModalOpen(false);
      setNewProjectName('');
      setNewProjectDesc('');
      navigate(`/projects/${project.project_id}`);
    } catch (err) {
      setError(err.message || 'Could not create the project.');
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="flex-1 px-5 py-8 lg:px-10 max-w-[1500px] mx-auto w-full">
      <div className="mb-9">
        <p className="text-xs font-bold uppercase tracking-[0.2em] text-primary mb-3">
          {user?.tenant_name || 'Workspace overview'}
        </p>
        <h1 className="text-4xl font-bold tracking-tight text-gray-100">{greeting}, let&apos;s make progress.</h1>
        <p className="text-gray-500 mt-2 max-w-xl">One calm place for the documents, decisions, and drafts moving your team forward.</p>
      </div>

      {error && <p className="text-red-400 text-sm mb-4">{error}</p>}

      {loading ? (
        <div className="flex justify-center py-16">
          <div className="w-8 h-8 border-4 border-primary/30 border-t-primary rounded-full animate-spin" />
        </div>
      ) : (
        <>
          <div className="flex flex-col sm:flex-row sm:items-center justify-between mb-4 gap-3">
            <div>
              <h2 className="text-lg font-bold text-gray-100">Your projects</h2>
              <p className="text-sm text-gray-500">Pick up where your team left off.</p>
            </div>
            <div className="w-full sm:w-64 sm:ml-auto shrink-0">
              <Input
                aria-label="Search projects"
                icon={Search}
                placeholder="Search projects..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
              />
            </div>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-5">
            {canCreateProject && <CreateProjectCard onClick={() => setIsModalOpen(true)} />}
            {filteredProjects.map((project) => {
              const myMemberships = (project.members || []).filter((m) => m.user_id === user?.user_id);
              const isProjectAdmin =
                user?.project_roles?.[project.project_id] === 'project_admin' ||
                myMemberships.some((m) => m.role === 'project_admin');
              const accessLevel = user?.is_org_admin
                ? 'org_admin'
                : isProjectAdmin
                  ? 'project_admin'
                  : 'member';
              const myTeams = myMemberships
                .filter((m) => m.role !== 'project_admin' && m.team_name)
                .map((m) => ({ team: m.team_name, role: m.role }));
              return (
                <ProjectCard
                  key={project.project_id}
                  project={project}
                  accessLevel={accessLevel}
                  myTeams={myTeams}
                />
              );
            })}
          </div>
        </>
      )}

      <Modal
        open={isModalOpen}
        onClose={() => setIsModalOpen(false)}
        title="Create New Project"
        footer={
          <>
            <Button variant="ghost" onClick={() => setIsModalOpen(false)}>Cancel</Button>
            <Button onClick={handleCreateProject} disabled={!newProjectName.trim()} loading={creating}>
              Create Project
            </Button>
          </>
        }
      >
        <div className="space-y-4">
          <Input
            label="Project Name"
            placeholder="e.g. Q3 Financial Report"
            required
            value={newProjectName}
            onChange={(e) => setNewProjectName(e.target.value)}
          />
          <Textarea
            label="Description"
            placeholder="What is this project about?"
            value={newProjectDesc}
            onChange={(e) => setNewProjectDesc(e.target.value)}
          />
        </div>
      </Modal>
    </div>
  );
};

export default ProjectsPage;
