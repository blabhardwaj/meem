import React from 'react';
import { PenLine, Search } from 'lucide-react';

// Deterministic mode selection for the chat panel — clickable tabs, NOT
// LLM-judged routing (that approach was tried and dropped earlier in the
// project). The active tab decides which backend a typed message hits.
// Switching tabs remounts ChatPanel (see ProjectWorkspace `key=`), so each
// mode starts a fresh session rather than carrying context across.
//
// The former separate Scan and Query tabs were removed/merged (see
// UI_FIXES_2026-09-15.md #4b, #5): every real document already gets scanned
// automatically on upload/finalize, and Search now answers both content and
// metadata questions in one agent.
const TABS = [
  { id: 'draft', label: 'Draft', icon: PenLine, hint: 'Draft a document with the AI (downloadable, not persisted)' },
  { id: 'search', label: 'Search', icon: Search, hint: 'Search this project’s documents, or ask a status/version/approval question' },
];

const ChatModeTabs = ({ active, onChange }) => (
  <div role="tablist" aria-label="Chat mode" className="flex shrink-0 items-stretch gap-1 border-b border-border bg-surface px-2 pt-2">
    {TABS.map(({ id, label, icon: Icon, hint }) => {
      const selected = id === active;
      return (
        <button
          key={id}
          type="button"
          role="tab"
          aria-selected={selected}
          title={hint}
          onClick={() => onChange(id)}
          className={`flex items-center gap-1.5 rounded-t-lg border border-b-0 px-3 py-2 text-sm font-medium transition-colors ${
            selected
              ? 'border-border bg-background text-gray-100'
              : 'border-transparent text-gray-400 hover:text-gray-200 hover:bg-background/50'
          }`}
        >
          <Icon size={15} className={selected ? 'text-primary' : ''} />
          {label}
        </button>
      );
    })}
  </div>
);

export default ChatModeTabs;
