import React, { useEffect } from 'react';
import { X, Download, FileText } from 'lucide-react';
import MarkdownMessage from './MarkdownMessage';

// In-app document viewer — renders Markdown content as formatted HTML in a
// centered modal. Wider than the shared <Modal>; carries an optional Download
// action in its header (used for finalized drafts).
const MarkdownViewer = ({ open, onClose, title, subtitle, content = '', onDownload }) => {
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[90] flex items-center justify-center p-4">
      <div className="fixed inset-0 bg-background/80 backdrop-blur-sm" onClick={onClose} />
      <div className="relative z-[91] flex w-full max-w-3xl max-h-[85vh] flex-col rounded-xl border border-border bg-surface shadow-2xl">
        <div className="flex items-start justify-between gap-3 border-b border-border/50 p-4">
          <div className="flex items-start gap-3 min-w-0">
            <div className="mt-0.5 rounded-lg bg-background p-2 text-primary shrink-0">
              <FileText size={18} />
            </div>
            <div className="min-w-0">
              <h2 className="text-base font-semibold text-gray-100 truncate">{title || 'Document'}</h2>
              {subtitle && <p className="text-xs text-gray-500 mt-0.5 truncate">{subtitle}</p>}
            </div>
          </div>
          <div className="flex items-center gap-1 shrink-0">
            {onDownload && (
              <button
                type="button"
                onClick={onDownload}
                title="Download"
                aria-label="Download"
                className="rounded-md p-2 text-gray-400 hover:text-primary-light hover:bg-surface-hover transition-colors"
              >
                <Download size={16} />
              </button>
            )}
            <button
              type="button"
              onClick={onClose}
              aria-label="Close"
              className="rounded-md p-2 text-gray-400 hover:text-gray-100 hover:bg-surface-hover transition-colors"
            >
              <X size={18} />
            </button>
          </div>
        </div>

        <div className="overflow-y-auto scrollbar-thin p-6">
          <MarkdownMessage content={content} />
        </div>
      </div>
    </div>
  );
};

export default MarkdownViewer;
