import React from 'react';

const Card = ({
  title,
  description,
  icon: Icon,
  children,
  footer,
  onClick,
  className = '',
  hoverable = false
}) => {
  const isClickable = !!onClick || hoverable;
  
  return (
    <div 
      className={`bg-surface border border-border rounded-xl overflow-visible ${isClickable ? 'hover:border-primary/50 hover:shadow-lg hover:shadow-primary/5 cursor-pointer transition-all' : ''} ${className}`}
      onClick={onClick}
    >
      {(title || description || Icon) && (
        <div className={`p-5 ${children ? 'border-b border-border/50' : ''}`}>
          <div className="flex items-center gap-3">
            {Icon && (
              <div className="p-2 bg-background rounded-lg text-primary">
                <Icon size={20} />
              </div>
            )}
            <div>
              {title && <h3 className="font-semibold text-gray-100">{title}</h3>}
              {description && <p className="text-sm text-gray-400 mt-1">{description}</p>}
            </div>
          </div>
        </div>
      )}

      {children && (
        <div className="p-5">
          {children}
        </div>
      )}

      {footer && (
        <div className="card-footer px-5 py-4 bg-background/50 border-t border-border/50">
          {footer}
        </div>
      )}
    </div>
  );
};

export default Card;
