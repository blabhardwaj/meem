import React from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';

// Real "go back one step" navigation — history-based, not a hardcoded route.
// The logo and the Projects dropdown in TopNav already cover "go to all
// projects"; this button now does what a back button should (UI_FIXES_2026-09-15.md #6).
// Falls back to `fallbackTo` (default "/") when there's no in-app history to
// go back to (e.g. the page was opened directly via a bookmarked URL).
const BackButton = ({ fallbackTo = '/', label = 'Back', className = '' }) => {
  const navigate = useNavigate();

  const handleClick = () => {
    if (window.history.state?.idx > 0) {
      navigate(-1);
    } else {
      navigate(fallbackTo);
    }
  };

  return (
    <button
      type="button"
      onClick={handleClick}
      className={`text-gray-400 hover:text-gray-200 transition-colors flex items-center gap-1 text-sm ${className}`}
    >
      <ArrowLeft size={16} />
      {label}
    </button>
  );
};

export default BackButton;
