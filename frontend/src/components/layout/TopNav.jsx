import React, { useState, useRef, useEffect } from 'react';
import { ShieldCheck, LogOut, ChevronDown, User, Command, CircleHelp, Bell, X, FolderKanban, Activity, Sun, Moon } from 'lucide-react';
import { Link, useNavigate, useMatch, useSearchParams } from 'react-router-dom';
import { useAuth } from '../../context/AuthContext';
import { notificationsApi, projectsApi } from '../../lib/api';

const TopNav = () => {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  // Workspace/Intelligence switcher only makes sense while inside a project
  // — moved here from the per-project header (UI_FIXES_2026-09-15.md #8) so
  // its position stays fixed instead of shifting with badge content.
  const workspaceMatch = useMatch('/projects/:projectId');
  const intelligenceMatch = useMatch('/projects/:projectId/intelligence');
  const uploadMatch = useMatch('/projects/:projectId/upload');
  const routeProjectId = workspaceMatch?.params.projectId || intelligenceMatch?.params.projectId || uploadMatch?.params.projectId;
  const inIntelligence = Boolean(intelligenceMatch);
  const adminMatch = useMatch('/admin');
  const homeMatch = useMatch('/');

  const [storedProjectId, setStoredProjectId] = useState(() => {
    return window.sessionStorage?.getItem('docflow_active_project_id') || null;
  });

  useEffect(() => {
    if (routeProjectId) {
      setStoredProjectId(routeProjectId);
      window.sessionStorage?.setItem('docflow_active_project_id', routeProjectId);
    } else if (homeMatch) {
      setStoredProjectId(null);
      window.sessionStorage?.removeItem('docflow_active_project_id');
    }
  }, [routeProjectId, homeMatch]);

  const activeProjectId = routeProjectId || (adminMatch ? (searchParams.get('project_id') || storedProjectId) : null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [projectMenuOpen, setProjectMenuOpen] = useState(false);
  const [projects, setProjects] = useState([]);
  const [notificationOpen, setNotificationOpen] = useState(false);
  const [notifications, setNotifications] = useState([]);
  const [unreadCount, setUnreadCount] = useState(0);
  const [lightMode, setLightMode] = useState(() => window.localStorage.getItem('docflow_theme') === 'light');
  const menuRef = useRef(null);
  const projectMenuRef = useRef(null);
  const notificationRef = useRef(null);
  // The Admin area now hosts role-scoped tabs (Audit Log, Project Activity) that
  // are open to project admins, team leads and contributors — not just org
  // admins. Show the entry point to anyone with a non-viewer role.
  const canOpenAdmin = Boolean(user?.is_org_admin)
    || Object.values(user?.project_roles || {}).some((r) => ['contributor', 'team_lead', 'project_admin'].includes(r));

  useEffect(() => {
    const onClick = (e) => {
      if (menuRef.current && !menuRef.current.contains(e.target)) setMenuOpen(false);
      if (projectMenuRef.current && !projectMenuRef.current.contains(e.target)) setProjectMenuOpen(false);
      if (notificationRef.current && !notificationRef.current.contains(e.target)) setNotificationOpen(false);
    };
    document.addEventListener('mousedown', onClick);
    return () => document.removeEventListener('mousedown', onClick);
  }, []);

  const openProjectMenu = () => {
    const nextOpen = !projectMenuOpen;
    setProjectMenuOpen(nextOpen);
    if (nextOpen && projects.length === 0) {
      projectsApi.list().then(setProjects).catch(() => {});
    }
  };

  const loadNotifications = async () => {
    try {
      const data = await notificationsApi.list();
      setNotifications(data.notifications || []);
      setUnreadCount(data.unread_count || 0);
    } catch {
      // Notifications should not interrupt the main workspace.
    }
  };

  useEffect(() => {
    if (!user) return undefined;
    loadNotifications();
    const interval = window.setInterval(loadNotifications, 10 * 1000);
    window.addEventListener('focus', loadNotifications);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener('focus', loadNotifications);
    };
  }, [user?.user_id, user?.username]);

  const openNotifications = () => {
    const nextOpen = !notificationOpen;
    setNotificationOpen(nextOpen);
    if (nextOpen) {
      loadNotifications();
    }
  };

  const dismissNotification = async (notificationId) => {
    setNotifications((current) => current.filter((n) => n.notification_id !== notificationId));
    setUnreadCount((count) => {
      const wasUnread = notifications.find((n) => n.notification_id === notificationId && !n.read);
      return wasUnread ? Math.max(0, count - 1) : count;
    });
    try {
      await notificationsApi.markRead(notificationId);
    } catch {
      // Best-effort — the row stays unread server-side but is already hidden here.
    }
  };

  const clearAllNotifications = async () => {
    setNotifications([]);
    setUnreadCount(0);
    try {
      await notificationsApi.markAllRead();
    } catch {
      // Best-effort — a later reload will just show them again.
    }
  };

  const handleLogout = () => {
    logout();
    navigate('/login');
  };

  const toggleAdmin = () => {
    if (adminMatch) {
      if (window.history.state?.idx > 0) navigate(-1);
      else if (activeProjectId) navigate(`/projects/${encodeURIComponent(activeProjectId)}`);
      else navigate('/');
    } else {
      navigate(activeProjectId ? `/admin?project_id=${encodeURIComponent(activeProjectId)}` : '/admin');
    }
  };

  const toggleTheme = () => {
    const nextLightMode = !lightMode;
    setLightMode(nextLightMode);
    window.localStorage.setItem('docflow_theme', nextLightMode ? 'light' : 'dark');
    window.dispatchEvent(new Event('docflow-theme-change'));
  };

  return (
    <nav className="relative h-14 border-b border-border bg-surface/95 backdrop-blur flex items-center justify-between px-6 sticky top-0 z-40">
      {activeProjectId && (
        <div className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 hidden md:flex items-center gap-1 rounded-lg border border-border bg-surface p-1">
          <Link
            to={`/projects/${encodeURIComponent(activeProjectId)}`}
            className={`flex items-center gap-1.5 rounded-md px-3 py-1 text-xs font-medium transition-colors ${
              !inIntelligence && !adminMatch
                ? 'bg-background text-gray-100 shadow-sm'
                : 'text-gray-400 hover:text-gray-200 hover:bg-surface-hover'
            }`}
          >
            <FolderKanban size={13} className={!inIntelligence && !adminMatch ? 'text-primary' : ''} />
            Workspace
          </Link>
          <Link
            to={`/projects/${encodeURIComponent(activeProjectId)}/intelligence`}
            className={`flex items-center gap-1.5 rounded-md px-3 py-1 text-xs font-medium transition-colors ${
              inIntelligence && !adminMatch
                ? 'bg-background text-gray-100 shadow-sm'
                : 'text-gray-400 hover:text-gray-200 hover:bg-surface-hover'
            }`}
          >
            <Activity size={13} className={inIntelligence && !adminMatch ? 'text-primary' : ''} />
            Intelligence
          </Link>
        </div>
      )}
      <div className="flex items-center gap-8">
        <Link to="/" className="flex items-center gap-2.5 text-xl font-bold tracking-tight bg-gemini-gradient bg-clip-text text-transparent" title="All Projects">
          <Command className="text-primary" size={19} /> DocFlow AI
        </Link>
        <div className="hidden md:flex items-center gap-5 text-sm text-gray-500">
          <div className="relative" ref={projectMenuRef}>
            <button
              type="button"
              onClick={openProjectMenu}
              aria-expanded={projectMenuOpen}
              className="flex items-center gap-1 hover:text-primary transition-colors"
            >
              Projects <ChevronDown size={14} />
            </button>
            {projectMenuOpen && (
              <div className="absolute left-0 mt-2 w-64 max-w-[calc(100vw-2rem)] bg-surface border border-border rounded-lg shadow-lg overflow-hidden z-[70]">
                <Link
                  to="/"
                  onClick={() => setProjectMenuOpen(false)}
                  className="flex items-center gap-2 px-4 py-2.5 text-sm font-medium text-gray-100 hover:bg-surface-hover transition-colors border-b border-border/50"
                >
                  <FolderKanban size={15} /> All Projects
                </Link>
                <div className="max-h-72 overflow-y-auto scrollbar-thin">
                  {projects.length === 0 ? (
                    <p className="px-4 py-3 text-xs text-gray-500">No projects yet</p>
                  ) : projects.map((p) => (
                    <Link
                      key={p.project_id}
                      to={`/projects/${p.project_id}`}
                      onClick={() => setProjectMenuOpen(false)}
                      className="block px-4 py-2.5 text-sm text-gray-300 hover:bg-surface-hover transition-colors truncate"
                    >
                      {p.project_name}
                    </Link>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={toggleTheme}
          className="p-2 text-gray-500 hover:text-primary transition-colors rounded-full hover:bg-surface-hover"
          aria-label={`Switch to ${lightMode ? 'dark' : 'light'} mode`}
          title={`Switch to ${lightMode ? 'dark' : 'light'} mode`}
        >
          {lightMode ? <Moon size={20} /> : <Sun size={20} />}
        </button>
        <div className="relative" ref={notificationRef}>
          <button
            onClick={openNotifications}
            className="relative p-2 text-gray-500 hover:text-primary transition-colors rounded-full hover:bg-surface-hover"
            title="Notifications"
            aria-label="Notifications"
          >
            <Bell size={20} />
            {unreadCount > 0 && (
              <span className="absolute -right-0.5 -top-0.5 min-w-4 h-4 px-1 rounded-full bg-red-500 text-[10px] leading-4 text-white text-center">
                {unreadCount > 99 ? '99+' : unreadCount}
              </span>
            )}
          </button>
          {notificationOpen && (
            <div className="absolute right-0 mt-2 w-80 max-w-[calc(100vw-2rem)] bg-surface border border-border rounded-lg shadow-lg overflow-hidden z-[70]">
              <div className="px-4 py-3 border-b border-border/50 flex items-start justify-between gap-3">
                <div>
                  <p className="text-sm font-semibold text-gray-100">Notifications</p>
                  <p className="text-xs text-gray-500 mt-1">
                    {user?.is_org_admin ? 'All recent organization activity.' : 'Activity from your assigned projects.'}
                  </p>
                </div>
                {unreadCount > 0 && (
                  <button
                    type="button"
                    onClick={clearAllNotifications}
                    className="shrink-0 text-xs font-medium text-primary-light hover:text-primary transition-colors whitespace-nowrap"
                  >
                    Mark all as read
                  </button>
                )}
              </div>
              <div className="max-h-80 overflow-y-auto scrollbar-thin">
                {notifications.length === 0 ? (
                  <p className="px-4 py-6 text-sm text-gray-500 text-center">No recent notifications.</p>
                ) : notifications.map((notification) => (
                  <div
                    key={notification.notification_id}
                    className={`px-4 py-3 border-b border-border/30 last:border-0 ${notification.read ? '' : 'bg-primary/5'}`}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <span className={`text-xs font-semibold ${notification.read ? 'text-gray-300' : 'text-primary-light'}`}>
                        {notification.title}
                      </span>
                      <div className="flex items-center gap-2 shrink-0">
                        <span className="text-[10px] text-gray-600">{new Date(notification.created_at).toLocaleString()}</span>
                        <button
                          type="button"
                          onClick={() => dismissNotification(notification.notification_id)}
                          className="text-gray-500 hover:text-gray-200 transition-colors"
                          title="Clear notification"
                          aria-label="Clear notification"
                        >
                          <X size={13} />
                        </button>
                      </div>
                    </div>
                    {notification.body && (
                      <p className="text-xs text-gray-400 mt-1">{notification.body}</p>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
        {canOpenAdmin && (
          <button
            type="button"
            onClick={toggleAdmin}
            aria-pressed={Boolean(adminMatch)}
            className={`p-2 rounded-full transition-colors ${
              adminMatch ? 'text-primary bg-surface-hover' : 'text-gray-500 hover:text-primary hover:bg-surface-hover'
            }`}
            title="Access & Governance"
          >
            <ShieldCheck size={20} />
          </button>
        )}

        <div className="relative ml-2" ref={menuRef}>
          <button
            onClick={() => setMenuOpen((v) => !v)}
            className="flex items-center gap-2 pl-1 pr-2 py-1 rounded-full hover:bg-surface-hover transition-colors"
          >
              <div className="w-7 h-7 rounded-full bg-primary/20 text-primary-light flex items-center justify-center">
              <User size={16} />
            </div>
            <span className="text-sm text-gray-300 max-w-[140px] truncate hidden sm:inline">
              {user?.full_name || user?.username || 'Account'}
            </span>
            <ChevronDown size={14} className="text-gray-500" />
          </button>

          {menuOpen && (
            <div className="absolute right-0 mt-2 w-56 max-w-[calc(100vw-2rem)] bg-surface border border-border rounded-lg shadow-lg overflow-visible z-[70]">
              <div className="px-4 py-3 border-b border-border/50">
                <p className="text-sm font-medium text-gray-100 truncate">{user?.full_name || user?.username}</p>
                <p className="text-xs text-gray-500 truncate">{user?.username}</p>
              </div>
              <Link
                to="/profile"
                onClick={() => setMenuOpen(false)}
                className="w-full flex items-center gap-2 px-4 py-2.5 text-sm text-gray-300 hover:bg-surface-hover transition-colors"
              >
                <User size={16} />
                Profile
              </Link>
              <Link
                to="/faq"
                onClick={() => setMenuOpen(false)}
                className="w-full flex items-center gap-2 px-4 py-2.5 text-sm text-primary-light hover:bg-surface-hover transition-colors"
              >
                <CircleHelp size={16} />
                FAQ
              </Link>
              <button
                onClick={handleLogout}
                className="w-full flex items-center gap-2 px-4 py-2.5 text-sm text-gray-300 hover:bg-surface-hover hover:text-red-400 transition-colors"
              >
                <LogOut size={16} />
                Sign out
              </button>
            </div>
          )}
        </div>
      </div>
    </nav>
  );
};

export default TopNav;
