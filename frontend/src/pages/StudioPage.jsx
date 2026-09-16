import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, Sparkles, Bot, SearchCheck, Users, Database } from 'lucide-react';
import { agentsApi, projectsApi } from '../lib/api';
import Card from '../components/ui/Card';
import Badge from '../components/ui/Badge';
import Button from '../components/ui/Button';
import Modal from '../components/ui/Modal';

const templates = [
  { id: 'prd', label: 'PRD' },
  { id: 'ard', label: 'ARD' },
  { id: 'test-plan', label: 'Test Plan' },
  { id: 'brd', label: 'BRD' },
];

const StudioPage = () => {
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [gapReport, setGapReport] = useState(null);
  const [gapProject, setGapProject] = useState('');
  const [gapLoading, setGapLoading] = useState(false);
  const [teamProject, setTeamProject] = useState(null);

  useEffect(() => {
    projectsApi.list()
      .then(setProjects)
      .catch((err) => setError(err.message || 'Could not load projects.'))
      .finally(() => setLoading(false));
  }, []);

  const analyzeGaps = async (projectId) => {
    setGapProject(projectId);
    setGapReport(null);
    setGapLoading(true);
    try {
      setGapReport(await agentsApi.analyzeGaps(projectId));
    } catch (err) {
      setGapReport({ gap_report: err.message || 'Could not analyze gaps.' });
    } finally {
      setGapLoading(false);
    }
  };

  return (
    <div className="flex-1 px-6 py-8 max-w-6xl mx-auto w-full">
      <div className="mb-8">
        <div className="flex items-center gap-2 text-primary text-xs font-bold uppercase tracking-widest mb-3"><Sparkles size={15} /> Studio</div>
        <h1 className="text-3xl font-bold text-gray-100">AI-powered document workspace</h1>
        <p className="text-gray-400 mt-2">Work independently in AI Studio or use your project context in Project AI Studio.</p>
      </div>

      <h2 className="text-lg font-semibold text-gray-100 mb-3">AI Studio</h2>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-8 items-stretch">
        <Link to="/studio/query" className="h-full"><Card className="h-full min-h-[180px]" title="Search Agent" description="Ask grounded content questions or metadata questions across your project sources." icon={SearchCheck} /></Link>
      </div>
      <p className="text-sm text-gray-500 -mt-5 mb-8">
        Drafting and Search now live inside a project&rsquo;s workspace (Draft / Search tabs) — open a project below.
      </p>

      <h2 className="text-lg font-semibold text-gray-100 mb-3">Project AI Studio</h2>
      {error && <p className="text-red-400 text-sm mb-4">{error}</p>}
      {loading ? (
        <div className="flex justify-center py-16"><div className="w-8 h-8 border-4 border-primary/30 border-t-primary rounded-full animate-spin" /></div>
      ) : projects.length === 0 ? (
        <Card title="No projects yet" description="Create a project before opening Studio." footer={<Link to="/" className="text-sm text-primary-light hover:underline">Go to Projects</Link>} />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
          {projects.map((project) => (
            <Card key={project.project_id} className="h-full min-h-[190px]" title={project.project_name} description={project.description || 'Create and scan project documents with AI agents.'} icon={Sparkles}>
              <div className="flex flex-wrap items-center gap-2">
              <div className="w-full rounded-lg border border-border bg-background/60 px-3 py-2">
                <button
                  type="button"
                  onClick={() => setTeamProject(project)}
                  className="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-primary-light transition-colors hover:text-primary"
                >
                  <Users size={14} /> Associated team
                </button>
                <button
                  type="button"
                  onClick={() => setTeamProject(project)}
                  className="mt-2 text-left text-xs text-gray-500 transition-colors hover:text-gray-300"
                >
                  {(project.members || []).length > 0
                    ? `${project.members.length} member${project.members.length === 1 ? '' : 's'} assigned`
                    : 'No members assigned yet.'}
                </button>
              </div>
              <p className="w-full text-xs font-bold text-gray-500 uppercase tracking-wider mb-1">Project agents</p>
                <Link to={`/projects/${project.project_id}?tab=draft`}><Button size="sm" variant="secondary" icon={Bot}>Draft</Button></Link>
                <Link to={`/projects/${project.project_id}?tab=search`}><Button size="sm" variant="secondary" icon={Database}>Search</Button></Link>
                <Button size="sm" variant="secondary" icon={SearchCheck} onClick={() => analyzeGaps(project.project_id)}>Gaps</Button>
                <div className="w-full h-px bg-border my-1" />
                {templates.map((template) => (
                  <Link key={template.id} to={`/projects/${project.project_id}?tab=draft`}>
                    <Badge className="hover:border-primary/50 hover:text-primary transition-colors cursor-pointer">{template.label}</Badge>
                  </Link>
                ))}
                <Link to={`/projects/${project.project_id}`} className="ml-auto inline-flex items-center gap-1 text-xs text-primary-light hover:text-primary">
                  Open project <ArrowRight size={14} />
                </Link>
              </div>
            </Card>
          ))}
        </div>
      )}

      <Modal open={!!gapProject} onClose={() => setGapProject('')} title="Documentation Gap Analysis" description={gapProject}>
        {gapLoading ? <div className="flex justify-center py-8"><div className="w-6 h-6 border-4 border-primary/30 border-t-primary rounded-full animate-spin" /></div> : <div className="text-sm text-gray-300 whitespace-pre-wrap max-h-[50vh] overflow-y-auto scrollbar-thin">{gapReport?.gap_report}</div>}
      </Modal>

      <Modal
        open={!!teamProject}
        onClose={() => setTeamProject(null)}
        title="Associated team"
        description={teamProject ? `${teamProject.project_name} team members` : undefined}
      >
        {teamProject?.members?.length ? (
          <div className="space-y-3">
            {teamProject.members.map((member) => (
              <div key={member.user_id} className="rounded-lg border border-border bg-background/60 p-4">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <h3 className="font-semibold text-gray-100">{member.name || 'Unnamed member'}</h3>
                    <p className="mt-1 text-sm text-primary-light">{member.email || member.username || 'No email available'}</p>
                  </div>
                  {member.role && <Badge variant="active">{member.role}</Badge>}
                </div>
                <dl className="mt-4 grid grid-cols-1 gap-2 text-sm sm:grid-cols-2">
                  <div><dt className="text-xs text-gray-500">Team</dt><dd className="text-gray-300">{member.team_name || teamProject.team_name || 'Not specified'}</dd></div>
                  <div><dt className="text-xs text-gray-500">Organization</dt><dd className="text-gray-300">{member.organization || 'Not specified'}</dd></div>
                  <div><dt className="text-xs text-gray-500">Job title</dt><dd className="text-gray-300">{member.job_title || 'Not specified'}</dd></div>
                  <div><dt className="text-xs text-gray-500">Username</dt><dd className="text-gray-300">{member.username || 'Not specified'}</dd></div>
                </dl>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-sm text-gray-400">No team members are assigned to this project yet.</p>
        )}
      </Modal>
    </div>
  );
};

export default StudioPage;
