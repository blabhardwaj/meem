import React from 'react';

const Badge = ({
  children,
  variant = 'neutral',
  className = '',
  title,
}) => {
  const variants = {
    active: "bg-primary/10 text-primary border-primary/20",
    inactive: "bg-surface-hover text-gray-400 border-border",
    success: "bg-emerald-500/10 text-emerald-700 border-emerald-500/20",
    warning: "bg-amber-500/10 text-amber-700 border-amber-500/20",
    danger: "bg-red-500/10 text-red-400 border-red-500/20",
    neutral: "bg-surface text-gray-300 border-border"
  };

  return (
    <span title={title} className={`inline-flex items-center shrink-0 whitespace-nowrap leading-none px-2.5 pt-[3px] pb-[5px] rounded-full text-xs font-medium border ${variants[variant]} ${className}`}>
      {children}
    </span>
  );
};

export default Badge;
