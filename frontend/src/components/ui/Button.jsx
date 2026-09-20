import React from 'react';
import { Loader2 } from 'lucide-react';

const Button = ({
  children,
  variant = 'primary',
  size = 'md',
  disabled = false,
  loading = false,
  icon: Icon,
  className = '',
  ...props
}) => {
  const baseStyles = "inline-flex items-center justify-center shrink-0 whitespace-nowrap font-medium rounded-md transition-colors focus:outline-none focus:ring-2 focus:ring-primary focus:ring-offset-2 focus:ring-offset-background disabled:opacity-50 disabled:pointer-events-none";
  
  const variants = {
    primary: "bg-primary text-white hover:bg-primary-dark shadow-sm",
    secondary: "bg-surface hover:bg-surface-hover text-gray-200 border border-border shadow-sm",
    ghost: "bg-transparent border border-border/60 text-gray-300 hover:bg-surface-hover hover:border-border hover:text-gray-100 hover:shadow-sm",
    danger: "bg-red-500 text-white hover:bg-red-600 shadow-sm"
  };

  const sizes = {
    xs: "h-7 px-2.5 text-xs",
    sm: "h-8 px-3 text-sm",
    md: "h-10 px-4 py-2 text-sm",
    lg: "h-12 px-6 text-base"
  };

  const iconSizes = {
    xs: "h-3.5 w-3.5 mr-1.5",
    sm: "h-4 w-4 mr-2",
    md: "h-4 w-4 mr-2",
    lg: "h-5 w-5 mr-2"
  };

  const resolvedSize = sizes[size] ? size : 'md';
  const sizeStyles = sizes[resolvedSize];
  const iconClass = iconSizes[resolvedSize];

  return (
    <button
      className={`${baseStyles} ${variants[variant] || variants.primary} ${sizeStyles} ${className}`}
      disabled={disabled || loading}
      {...props}
    >
      {loading && <Loader2 className={`${iconClass} animate-spin`} />}
      {!loading && Icon && <Icon className={iconClass} />}
      {children}
    </button>
  );
};

export default Button;
