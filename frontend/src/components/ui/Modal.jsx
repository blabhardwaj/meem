import React, { useEffect } from 'react';
import { X } from 'lucide-react';

const Modal = ({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  scrollable = false,
}) => {
  useEffect(() => {
    const handleEscape = (e) => {
      if (e.key === 'Escape' && open) onClose();
    };
    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop */}
      <div 
        className="fixed inset-0 bg-background/80 backdrop-blur-sm transition-opacity"
        onClick={onClose}
      />
      
      {/* Modal Content */}
      <div className={`relative z-50 w-full max-w-lg bg-surface border border-border rounded-xl shadow-xl ${scrollable ? 'overflow-y-auto scrollbar-thin max-h-[75vh]' : 'overflow-visible'}`}>
        <div className="flex items-center justify-between p-5 border-b border-border/50">
          <div>
            <h2 className="text-lg font-semibold text-gray-100">{title}</h2>
            {description && <p className="text-sm text-gray-400 mt-1">{description}</p>}
          </div>
          <button 
            onClick={onClose}
            className="text-gray-400 hover:text-gray-200 transition-colors p-1 rounded-md hover:bg-surface-hover"
          >
            <X size={20} />
          </button>
        </div>
        
        <div className="p-5">
          {children}
        </div>
        
        {footer && (
          <div className="flex items-center justify-end gap-3 p-5 border-t border-border/50 bg-background/50">
            {footer}
          </div>
        )}
      </div>
    </div>
  );
};

export default Modal;
