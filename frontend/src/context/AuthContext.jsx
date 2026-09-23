import React, { createContext, useContext, useEffect, useState, useCallback } from 'react';
import { authApi, getToken, setToken, clearWorkspaceCache } from '../lib/api';

const AuthContext = createContext(null);

// eslint-disable-next-line react-refresh/only-export-components
export const useAuth = () => useContext(AuthContext);

// Our /auth/me returns team_memberships[] + project_admin_project_ids[].
// His pages read user.project_roles[projectId] and user.is_org_admin, so derive
// a per-project "best role" map in our own vocabulary
// (viewer < contributor < team_lead, plus project_admin).
const RANK = { viewer: 1, contributor: 2, team_lead: 3, project_admin: 4 };
function withProjectRoles(me) {
  const roles = {};
  for (const m of me.team_memberships || []) {
    if (!roles[m.project_id] || RANK[m.role] > RANK[roles[m.project_id]]) {
      roles[m.project_id] = m.role;
    }
  }
  for (const pid of me.project_admin_project_ids || []) roles[pid] = 'project_admin';
  // his TopNav / pages read user.username / user.full_name / user.role — /auth/me
  // now returns a real full_name (added alongside this fix), but fall back to
  // email for the (rare) account that was never given one, e.g. via accept-invite
  // with no name entered.
  return {
    ...me,
    project_roles: roles,
    username: me.email,
    full_name: me.full_name || me.email,
    role: me.is_org_admin ? 'org_admin' : (Object.values(roles)[0] || 'member'),
  };
}

export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  const loadUser = useCallback(async () => {
    if (!getToken()) {
      setUser(null);
      setLoading(false);
      return;
    }
    try {
      clearWorkspaceCache();
      setUser(withProjectRoles(await authApi.me()));
    } catch {
      setToken(null);
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  // Capture ?oauth_code=... from the Google OAuth callback redirect.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const code = params.get('oauth_code');
    const legacyToken = params.get('auth_token');

    if (code) {
      params.delete('oauth_code');
      params.delete('is_new_user');
      const clean = window.location.pathname + (params.toString() ? `?${params}` : '');
      window.history.replaceState({}, '', clean);

      authApi.exchangeOAuth(code)
        .then(({ access_token }) => {
          setToken(access_token);
          loadUser();
        })
        .catch((err) => {
          console.error('OAuth exchange failed:', err);
          loadUser();
        });
    } else if (legacyToken) {
      setToken(legacyToken);
      params.delete('auth_token');
      params.delete('is_new_user');
      const clean = window.location.pathname + (params.toString() ? `?${params}` : '');
      window.history.replaceState({}, '', clean);
      loadUser();
    } else {
      loadUser();
    }
  }, [loadUser]);


  const login = async (email, password) => {
    const { access_token } = await authApi.login(email, password);
    setToken(access_token);
    await loadUser();
  };

  const registerOrg = async (data) => {
    const { access_token } = await authApi.registerOrg(data);
    setToken(access_token);
    await loadUser();
  };

  const acceptInvite = async (data) => {
    const { access_token } = await authApi.acceptInvite(data);
    setToken(access_token);
    await loadUser();
  };

  const loginWithGoogle = async () => {
    const { authorization_url } = await authApi.googleAuthorize();
    window.location.href = authorization_url;
  };

  const logout = () => {
    clearWorkspaceCache();
    setToken(null);
    setUser(null);
  };

  return (
    <AuthContext.Provider
      value={{
        user,
        loading,
        isAuthenticated: !!user,
        login,
        registerOrg,
        acceptInvite,
        loginWithGoogle,
        logout,
        refresh: loadUser,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
};
