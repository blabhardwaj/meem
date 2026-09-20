import React, { useEffect, useRef, useState } from 'react';
import { Send } from 'lucide-react';
import Button from '../ui/Button';

// prefillValue: text placed in the input for the user to review/edit —
// never auto-submitted. Distinct from a parent's auto-send flow (e.g.
// ChatPanel's initialQuery): a prefill only fires once per identity change
// (keyed by the effect below) so retyping the box afterward doesn't get
// clobbered by the same value again.
const ChatInput = ({ onSend, disabled, placeholder = 'Ask a question about your project...', prefillValue = null, autoFocus = false }) => {
  const [message, setMessage] = useState('');
  const inputRef = useRef(null);
  const appliedPrefillRef = useRef(null);

  useEffect(() => {
    if (prefillValue && prefillValue !== appliedPrefillRef.current) {
      appliedPrefillRef.current = prefillValue;
      setMessage(prefillValue);
      inputRef.current?.focus();
    }
  }, [prefillValue]);

  useEffect(() => {
    if (autoFocus) {
      inputRef.current?.focus();
    }
  }, [autoFocus]);

  const handleSubmit = (e) => {
    e.preventDefault();
    if (message.trim() && !disabled) {
      onSend(message);
      setMessage('');
    }
  };

  return (
    <form onSubmit={handleSubmit} className="shrink-0 p-4 border-t border-border/50 bg-background/50">
      <div className="relative flex items-center">
        <input
          ref={inputRef}
          type="text"
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          disabled={disabled}
          placeholder={placeholder}
          className="w-full bg-surface border border-border rounded-lg pl-4 pr-12 py-3 text-sm text-gray-200 placeholder:text-gray-500 focus:outline-none focus:ring-1 focus:ring-primary focus:border-primary disabled:opacity-50"
        />
        <Button
          type="submit"
          variant="ghost"
          disabled={!message.trim() || disabled}
          className="absolute right-1 h-10 w-10 p-0 text-gray-400 hover:text-primary hover:bg-transparent"
        >
          <Send size={18} />
        </Button>
      </div>
    </form>
  );
};

export default ChatInput;
