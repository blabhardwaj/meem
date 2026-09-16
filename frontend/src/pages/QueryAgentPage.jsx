import React from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import ChatPanel from '../components/chat/ChatPanel';

// The former separate Query and RAG agents were merged into one Search agent
// (see UI_FIXES_2026-09-15.md #5) — both ?agent= values now route here.
const QueryAgentPage = () => {
  const [searchParams] = useSearchParams();
  const projectId = searchParams.get('project_id') || undefined;
  const mode = 'search';

  return (
    <div className="flex flex-col h-[calc(100vh-3.5rem)] overflow-hidden bg-background">
      <div className="h-14 border-b border-border bg-background px-6 flex items-center shrink-0">
        <Link to={projectId ? `/projects/${encodeURIComponent(projectId)}` : '/studio'} className="text-gray-400 hover:text-gray-200 transition-colors flex items-center gap-1 text-sm">
          <ArrowLeft size={16} />
          {projectId ? 'Back to Project' : 'Back to Studio'}
        </Link>
      </div>
      <div className="flex-1 min-h-0 max-w-4xl w-full mx-auto border-x border-border flex flex-col">
        <ChatPanel projectId={projectId} mode={mode} />
      </div>
    </div>
  );
};

export default QueryAgentPage;
