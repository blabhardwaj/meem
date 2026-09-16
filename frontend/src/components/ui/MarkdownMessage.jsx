import React, { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { BookOpen, Check, Copy } from 'lucide-react';

const isSafeUrl = (url) => {
  if (!url) return false;
  try {
    const parsed = new URL(url, window.location.href);
    return ['http:', 'https:', 'mailto:'].includes(parsed.protocol);
  } catch {
    return false;
  }
};

/**
 * Parses text containing [Source N — <Stage>] citation tags and renders them
 * as polished inline badge pills.
 */
function renderTextWithCitations(text) {
  if (typeof text !== 'string') return text;
  const citationRegex = /\[(Source\s+\d+[^\]]*)\]/g;
  if (!citationRegex.test(text)) return text;

  const parts = [];
  let lastIndex = 0;
  let match;
  citationRegex.lastIndex = 0;

  while ((match = citationRegex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index));
    }
    const citationText = match[1];
    parts.push(
      <span
        key={`cit-${match.index}`}
        className="inline-flex items-center gap-1 mx-1 px-1.5 py-0.5 rounded text-[11px] font-medium bg-primary/15 text-primary-light border border-primary/30 select-none align-baseline shadow-xs"
        title="Verified Project Knowledge Citation"
      >
        <BookOpen size={11} className="shrink-0 text-primary" />
        {citationText}
      </span>
    );
    lastIndex = match.index + match[0].length;
  }

  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }

  return parts;
}

function CodeBlock({ children, className, ...props }) {
  const [copied, setCopied] = useState(false);
  const codeString = String(children).replace(/\n$/, '');
  const match = /language-(\w+)/.exec(className || '');
  const language = match ? match[1] : '';

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(codeString);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Fallback if clipboard API unavailable
    }
  };

  return (
    <div className="relative group my-2.5 rounded-lg border border-border/80 bg-[#0d0f14] overflow-hidden">
      <div className="flex items-center justify-between px-3 py-1.5 bg-surface/80 border-b border-border/50 text-[11px] text-gray-400 font-mono select-none">
        <span>{language || 'code'}</span>
        <button
          type="button"
          onClick={handleCopy}
          aria-label="Copy code block"
          className="flex items-center gap-1 text-gray-400 hover:text-gray-100 transition-colors py-0.5 px-1.5 rounded hover:bg-surface"
        >
          {copied ? <Check size={12} className="text-emerald-400" /> : <Copy size={12} />}
          <span>{copied ? 'Copied' : 'Copy'}</span>
        </button>
      </div>
      <pre className="p-3 overflow-x-auto text-xs font-mono text-gray-200 leading-relaxed scrollbar-thin">
        <code className={className} {...props}>
          {codeString}
        </code>
      </pre>
    </div>
  );
}

/**
 * GFM table cells can't contain a real newline (it would end the row), so
 * the LLM writes a literal `<br>`/`<br/>` tag to force a line break inside a
 * cell — the standard, widely-supported convention for this. react-markdown
 * doesn't render raw inline HTML by default (and we deliberately don't add
 * rehype-raw + a broad allowlist just for this, since that widens the HTML
 * surface for LLM-generated content well beyond what's needed here). This
 * narrowly handles only literal <br> text within table cells, rendering a
 * real <br/> element in its place — no HTML parsing, no injection surface.
 */
function renderCellWithBreaks(children) {
  return React.Children.map(children, (child) => {
    if (typeof child !== 'string' || !/<br\s*\/?>/i.test(child)) return child;
    const parts = child.split(/<br\s*\/?>/i);
    return parts.map((part, i) => (
      <React.Fragment key={i}>
        {i > 0 && <br />}
        {part}
      </React.Fragment>
    ));
  });
}

export default function MarkdownMessage({ content = '', className = '' }) {
  if (!content) return null;

  const components = {
    // Links: force safe protocols and external attributes
    a({ href, children, ...props }) {
      if (!isSafeUrl(href)) {
        return <span>{children}</span>;
      }
      return (
        <a
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          className="text-primary-light hover:underline font-medium break-all"
          {...props}
        >
          {children}
        </a>
      );
    },

    // Paragraphs: support in-text citation pill extraction
    p({ children }) {
      const processed = React.Children.map(children, (child) => {
        if (typeof child === 'string') {
          return renderTextWithCitations(child);
        }
        return child;
      });
      return <p className="my-1.5 leading-relaxed text-gray-200 break-words first:mt-0 last:mb-0">{processed}</p>;
    },

    // Headings: clear visual hierarchy
    h1({ children }) {
      return (
        <h1 className="mt-4 mb-2 text-base sm:text-lg font-bold text-gray-100 border-b border-border/40 pb-1 first:mt-0">
          {children}
        </h1>
      );
    },
    h2({ children }) {
      return (
        <h2 className="mt-3 mb-1.5 text-sm sm:text-base font-semibold text-gray-100 first:mt-0">
          {children}
        </h2>
      );
    },
    h3({ children }) {
      return (
        <h3 className="mt-2.5 mb-1 text-xs sm:text-sm font-semibold text-gray-200 first:mt-0">
          {children}
        </h3>
      );
    },
    h4({ children }) {
      return (
        <h4 className="mt-2 mb-1 text-xs font-semibold text-gray-300 first:mt-0">
          {children}
        </h4>
      );
    },

    // Lists
    ul({ children }) {
      return <ul className="my-2 pl-5 list-disc space-y-1 text-sm text-gray-200">{children}</ul>;
    },
    ol({ children }) {
      return <ol className="my-2 pl-5 list-decimal space-y-1 text-sm text-gray-200">{children}</ol>;
    },
    li({ children }) {
      const processed = React.Children.map(children, (child) => {
        if (typeof child === 'string') {
          return renderTextWithCitations(child);
        }
        return child;
      });
      return <li className="leading-relaxed">{processed}</li>;
    },

    // Blockquotes
    blockquote({ children }) {
      return (
        <blockquote className="my-2.5 border-l-2 border-primary/60 bg-primary/5 pl-3 py-1.5 text-xs sm:text-sm text-gray-300 italic rounded-r">
          {children}
        </blockquote>
      );
    },

    // Horizontal Rules
    hr() {
      return <hr className="my-3 border-0 border-t border-border/60" />;
    },

    // Code: differentiate inline vs block
    code({ inline, className, children, ...props }) {
      // Inline code
      if (inline || (!className && typeof children === 'string' && !children.includes('\n'))) {
        return (
          <code
            className="rounded bg-background/80 px-1.5 py-0.5 text-[0.85em] font-mono text-primary-light border border-border/60"
            {...props}
          >
            {children}
          </code>
        );
      }
      return (
        <CodeBlock className={className} {...props}>
          {children}
        </CodeBlock>
      );
    },

    // Tables: responsive container with clean borders
    table({ children }) {
      return (
        <div className="my-3 overflow-x-auto rounded-lg border border-border/70 max-w-full">
          <table className="min-w-full divide-y divide-border/60 text-left text-xs sm:text-sm">
            {children}
          </table>
        </div>
      );
    },
    thead({ children }) {
      return <thead className="bg-surface">{children}</thead>;
    },
    tbody({ children }) {
      return <tbody className="divide-y divide-border/40 bg-background/40">{children}</tbody>;
    },
    th({ children }) {
      return (
        <th className="px-3 py-2 text-xs font-semibold text-gray-200 uppercase tracking-wider">
          {renderCellWithBreaks(children)}
        </th>
      );
    },
    td({ children }) {
      return <td className="px-3 py-2 text-xs sm:text-sm text-gray-300">{renderCellWithBreaks(children)}</td>;
    },

    // Text formatting
    strong({ children }) {
      return <strong className="font-semibold text-gray-100">{children}</strong>;
    },
    em({ children }) {
      return <em className="italic text-gray-200">{children}</em>;
    },
    del({ children }) {
      return <del className="line-through text-gray-400">{children}</del>;
    },
  };

  return (
    <div className={`markdown-message text-sm text-gray-200 leading-relaxed max-w-full ${className}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  );
}
