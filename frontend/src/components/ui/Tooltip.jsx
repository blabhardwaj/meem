import React from 'react';

// CSS-only floating tooltip: fades, rises and scales in on hover instead of
// the browser's plain, delayed native `title` bubble. Pure animation, no
// positioning JS -- pair the trigger with `peer` and give a `relative`
// ancestor a couple of levels up so this has something to anchor against;
// `className` positions it (e.g. `left-8 bottom-full mb-2`) per call site
// since trigger width/placement varies.
export default function Tooltip({ label, className = '', arrowClassName = 'left-3' }) {
  if (!label) return null;
  return (
    <span
      role="tooltip"
      className={`pointer-events-none absolute z-20 origin-bottom translate-y-1 scale-95 whitespace-nowrap rounded-lg border border-border bg-surface px-2.5 py-1.5 text-xs font-medium text-gray-100 shadow-xl opacity-0 transition-all duration-150 ease-out peer-hover:translate-y-0 peer-hover:scale-100 peer-hover:opacity-100 ${className}`}
    >
      {label}
      <span
        className={`absolute top-full -mt-px h-2 w-2 rotate-45 border-b border-r border-border bg-surface ${arrowClassName}`}
      />
    </span>
  );
}
