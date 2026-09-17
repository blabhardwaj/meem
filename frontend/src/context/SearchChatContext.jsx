import React, { createContext, useContext, useRef, useState, useCallback } from 'react';
import { chatApi } from '../lib/api';

// Search-mode chat state (session id, message list, session list, loading
// flag), lifted out of ChatPanel and up to AppLayout so it survives
// navigating between routes that each mount their own ChatPanel instance
// (ProjectWorkspace's Search tab, ProjectIntelligence's Search slide-over).
//
// Without this, every route switch remounted ChatPanel from scratch: an
// in-progress "new conversation" (no messages sent yet, so no server-side
// session exists) was silently discarded in favor of re-resuming the most
// recent real session, and the sessions+messages round trip re-ran on every
// single navigation (the visible ~2s reopen lag).
//
// Keyed by projectId so switching projects still starts clean. Draft/Review
// chat state is unaffected — draft already persists its own session id for
// the visit, and review is inherently one conversation per upload.
const SearchChatContext = createContext(null);

export const useSearchChatContext = () => useContext(SearchChatContext);

export const SearchChatProvider = ({ children }) => {
  // projectId -> { sessionId, messages, sessions, historyLoaded, loadPromise }
  const stateRef = useRef(new Map());
  // Bumped to force ChatPanel instances to re-render when another instance
  // of the same project's state changes (e.g. Workspace sends a message,
  // Intelligence's panel is opened afterward and must see it).
  const [, forceTick] = useState(0);
  const rerender = useCallback(() => forceTick((t) => t + 1), []);

  const getEntry = useCallback((projectId) => {
    if (!stateRef.current.has(projectId)) {
      stateRef.current.set(projectId, {
        sessionId: null,
        messages: [],
        sessions: [],
        historyLoaded: false,
        loadPromise: null,
      });
    }
    return stateRef.current.get(projectId);
  }, []);

  const ensureHistoryLoaded = useCallback(
    (projectId) => {
      const entry = getEntry(projectId);
      if (entry.historyLoaded) return Promise.resolve(entry);
      if (entry.loadPromise) return entry.loadPromise;

      entry.loadPromise = chatApi
        .sessions(projectId, 'search')
        .then((items) => {
          entry.sessions = items;
          entry.historyLoaded = true;
          if (items[0] && entry.sessionId === null) {
            entry.sessionId = items[0].session_id;
            return chatApi.messages(items[0].session_id).then((msgs) => {
              entry.messages = msgs.map((item) => ({
                id: item.message_id,
                text: item.content,
                sender: item.role === 'user' ? 'user' : 'bot',
                markdown: item.role !== 'user',
              }));
              rerender();
              return entry;
            });
          }
          rerender();
          return entry;
        })
        .catch(() => {
          entry.historyLoaded = true;
          rerender();
          return entry;
        })
        .finally(() => {
          entry.loadPromise = null;
        });
      return entry.loadPromise;
    },
    [getEntry, rerender],
  );

  const setMessages = useCallback(
    (projectId, updater) => {
      const entry = getEntry(projectId);
      entry.messages = typeof updater === 'function' ? updater(entry.messages) : updater;
      rerender();
    },
    [getEntry, rerender],
  );

  const setSessionId = useCallback(
    (projectId, sessionId) => {
      const entry = getEntry(projectId);
      entry.sessionId = sessionId;
      rerender();
    },
    [getEntry, rerender],
  );

  const setSessions = useCallback(
    (projectId, sessions) => {
      const entry = getEntry(projectId);
      entry.sessions = sessions;
      rerender();
    },
    [getEntry, rerender],
  );

  const startNewConversation = useCallback(
    (projectId) => {
      const entry = getEntry(projectId);
      entry.sessionId = null;
      entry.messages = [];
      rerender();
    },
    [getEntry, rerender],
  );

  const selectConversation = useCallback(
    async (projectId, sessionId) => {
      const entry = getEntry(projectId);
      entry.sessionId = sessionId;
      rerender();
      const items = await chatApi.messages(sessionId);
      entry.messages = items.map((item) => ({
        id: item.message_id,
        text: item.content,
        sender: item.role === 'user' ? 'user' : 'bot',
        markdown: item.role !== 'user',
      }));
      rerender();
    },
    [getEntry, rerender],
  );

  const removeConversation = useCallback(
    async (projectId, sessionId) => {
      await chatApi.removeSession(sessionId);
      const entry = getEntry(projectId);
      entry.sessions = entry.sessions.filter((s) => s.session_id !== sessionId);
      if (entry.sessionId === sessionId) {
        entry.sessionId = null;
        entry.messages = [];
      }
      rerender();
    },
    [getEntry, rerender],
  );

  const refreshSessionList = useCallback(
    (projectId) => {
      chatApi
        .sessions(projectId, 'search')
        .then((items) => setSessions(projectId, items))
        .catch(() => {});
    },
    [setSessions],
  );

  const value = {
    getEntry,
    ensureHistoryLoaded,
    setMessages,
    setSessionId,
    setSessions,
    startNewConversation,
    selectConversation,
    removeConversation,
    refreshSessionList,
  };

  return <SearchChatContext.Provider value={value}>{children}</SearchChatContext.Provider>;
};
