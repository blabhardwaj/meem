import React, { useState } from 'react';
import { FileText, Copy, Check, Sparkles, Download, UploadCloud } from 'lucide-react';
import MarkdownMessage from '../ui/MarkdownMessage';

/**
 * Parses out trailing conversational sign-offs from drafting responses (if any),
 * so the document itself reads cleanly like a document while the agent's
 * closing prompt remains conversational.
 */
function splitDraftAndComment(text) {
  if (!text || typeof text !== 'string') {
    return { documentContent: text || '', conversationalComment: null };
  }

  const trimmed = text.trim();
  const paragraphs = trimmed.split(/\n\s*\n/);
  if (paragraphs.length <= 1) {
    return { documentContent: trimmed, conversationalComment: null };
  }

  const lastPara = paragraphs[paragraphs.length - 1].trim();

  // Check if last paragraph looks like a conversational sign-off rather than document body
  const conversationalLeadIn = /^(let me know|would you like|if you('|’)d like|tell me if|shall i|should i|say the word|if this looks good|please let me know|feel free to)/i;
  const isDocumentSyntax = /^([#*>-]|\d+\.|\`\`\`)/.test(lastPara);

  if (!isDocumentSyntax && conversationalLeadIn.test(lastPara)) {
    const documentContent = paragraphs.slice(0, -1).join('\n\n').trim();
    return { documentContent, conversationalComment: lastPara };
  }

  return { documentContent: trimmed, conversationalComment: null };
}

/**
 * Extracts a title from the first Markdown heading (e.g. # Title) if present.
 */
function extractHeading(content) {
  if (!content) return 'Generated Document';
  const match = content.match(/^#\s+([^\n]+)/m);
  return match ? match[1].trim() : 'Draft Document';
}

export default function DraftDocumentCard({
  content = '',
  filename = null,
  finalized = false,
  comment = null,
  onDownload = null,
  onUploadToProject = null,
  uploadedInfo = null,
}) {
  const [copied, setCopied] = useState(false);
  const { documentContent, conversationalComment } = splitDraftAndComment(content);
  const displayComment = comment || conversationalComment;
  const title = filename || extractHeading(documentContent);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(documentContent);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Fallback
    }
  };

  return (
    <div className="w-full my-2 flex flex-col gap-3">
      {/* Document Sheet Canvas */}
      <div className="rounded-xl border border-border/80 bg-[#13151b] shadow-xl overflow-hidden">
        {/* Document Header Bar */}
        <div className="flex items-center justify-between px-4 py-2.5 bg-surface/90 border-b border-border/60 gap-3">
          <div className="flex items-center gap-2.5 min-w-0">
            <div className="w-7 h-7 rounded-lg bg-primary/10 border border-primary/25 flex items-center justify-center text-primary-light shrink-0">
              <FileText size={15} />
            </div>
            <div className="min-w-0">
              <span className="text-xs font-semibold text-gray-100 truncate block">{title}</span>
              <div className="flex items-center gap-2">
                <span className="text-[10px] text-gray-500 block">
                  {finalized ? 'Finalized Document' : 'Draft Document'}
                </span>
                {uploadedInfo && (
                  <span className="text-[10px] text-emerald-400 font-medium bg-emerald-500/10 border border-emerald-500/20 rounded px-1.5 py-0.5">
                    ✓ Uploaded to {uploadedInfo.stageName || 'project'}
                  </span>
                )}
              </div>
            </div>
          </div>

          <div className="flex items-center gap-1.5 shrink-0 flex-wrap justify-end">
            <button
              type="button"
              onClick={handleCopy}
              className="flex items-center gap-1.5 text-xs font-medium text-gray-300 hover:text-white bg-background/70 hover:bg-surface border border-border/70 rounded-lg px-2.5 py-1 transition-colors"
              title="Copy document Markdown"
            >
              {copied ? (
                <>
                  <Check size={13} className="text-emerald-400" />
                  <span className="text-emerald-400">Copied</span>
                </>
              ) : (
                <>
                  <Copy size={13} />
                  <span>Copy</span>
                </>
              )}
            </button>

            {finalized && onDownload && (
              <button
                type="button"
                onClick={onDownload}
                className="flex items-center gap-1.5 text-xs font-medium text-gray-300 hover:text-white bg-background/70 hover:bg-surface border border-border/70 rounded-lg px-2.5 py-1 transition-colors"
                title="Download finalized Markdown"
              >
                <Download size={13} />
                <span>Download</span>
              </button>
            )}

            {finalized && onUploadToProject && (
              <button
                type="button"
                onClick={onUploadToProject}
                className="flex items-center gap-1.5 text-xs font-medium text-primary-light hover:text-white bg-primary/10 hover:bg-primary/30 border border-primary/40 rounded-lg px-2.5 py-1 transition-colors"
                title="Upload directly to a project stage"
              >
                <UploadCloud size={13} />
                <span>Upload to Project</span>
              </button>
            )}
          </div>
        </div>

        {/* Document Body */}
        <div className="p-4 sm:p-6 bg-[#0f1117]/60 overflow-x-auto">
          <MarkdownMessage content={documentContent} />
        </div>
      </div>

      {/* Trailing Conversational Comment */}
      {displayComment && (
        <div className="flex items-start gap-2 px-1 text-xs text-gray-300">
          <Sparkles size={14} className="text-primary-light mt-0.5 shrink-0" />
          <p className="leading-relaxed">{displayComment}</p>
        </div>
      )}
    </div>
  );
}
