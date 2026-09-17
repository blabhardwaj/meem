import React, { useEffect, useState } from 'react';
import { Outlet } from 'react-router-dom';
import TopNav from './TopNav';
import TutorialPopup from './TutorialPopup';
import { useAuth } from '../../context/AuthContext';
import { SearchChatProvider } from '../../context/SearchChatContext';
import PasswordChangePrompt from '../auth/PasswordChangePrompt';

const AppLayout = () => {
  const { user, refresh } = useAuth();
  const [tutorialOpen, setTutorialOpen] = useState(false);

  useEffect(() => {
    const applyTheme = () => {
      const isLight = window.localStorage.getItem('docflow_theme') === 'light';
      document.documentElement.classList.toggle('light-theme', isLight);
      document.body.classList.toggle('light-theme', isLight);
    };
    applyTheme();
    window.addEventListener('docflow-theme-change', applyTheme);
    return () => window.removeEventListener('docflow-theme-change', applyTheme);
  }, []);

  useEffect(() => {
    const roleKey = user?.is_org_admin ? 'admin' : 'member';
    if (user && window.localStorage.getItem(`docflow_tutorial_seen_${roleKey}`) !== 'true') {
      setTutorialOpen(true);
    }
  }, [user?.user_id, user?.is_org_admin]);

  return (
    <SearchChatProvider>
      <div className="flex flex-col min-h-screen bg-background">
        <TopNav />
        <main className="flex flex-1 min-h-0 flex-col">
          <Outlet />
        </main>
        <TutorialPopup open={tutorialOpen} onClose={() => setTutorialOpen(false)} />
        {user?.must_change_password && (
          <PasswordChangePrompt onComplete={refresh} />
        )}
      </div>
    </SearchChatProvider>
  );
};

export default AppLayout;
