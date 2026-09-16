import React, { useState } from 'react';
import { Send } from 'lucide-react';
import Button from '../ui/Button';

const ChatInput = ({ onSend, disabled, placeholder = 'Ask a question about your project...' }) => {
  const [message, setMessage] = useState('');

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
