import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { api, getToken, setToken, type UserSettings } from './api';

type User = { id: string; username: string; displayName: string; createdAt: string };

type AuthState = {
  user: User | null;
  settings: UserSettings | null;
  ready: boolean;
  login: (username: string, password: string) => Promise<void>;
  register: (username: string, password: string) => Promise<void>;
  logout: () => void;
  patchSettings: (patch: Partial<UserSettings>) => Promise<void>;
  refresh: () => Promise<void>;
};

const Ctx = createContext<AuthState>(null as any);

export const DEFAULT_SETTINGS: UserSettings = {
  chunkSeconds: 120,
  autoSummarize: true,
  speakerDiarization: false,
  diarizationThreshold: 0.7,
  summaryProviderId: null,
  summaryTemplate: 'meeting',
  summaryPrompt: '',
  promptDay: '',
  promptWeek: '',
  promptMonth: '',
  promptYear: '',
  promptRange: '',
  language: 'zh',
  theme: 'system',
};

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [settings, setSettings] = useState<UserSettings | null>(null);
  const [ready, setReady] = useState(false);

  const refresh = useCallback(async () => {
    if (!getToken()) {
      setUser(null);
      setReady(true);
      return;
    }
    try {
      const res = await api<{ user: User; settings: UserSettings }>('/api/auth/me');
      setUser(res.user);
      setSettings(res.settings ?? DEFAULT_SETTINGS);
    } catch {
      setUser(null);
    } finally {
      setReady(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const onUnauthorized = () => setUser(null);
    window.addEventListener('recall:unauthorized', onUnauthorized);
    return () => window.removeEventListener('recall:unauthorized', onUnauthorized);
  }, [refresh]);

  const login = useCallback(async (username: string, password: string) => {
    const res = await api<{ token: string; user: User; settings: UserSettings }>('/api/auth/login', {
      method: 'POST',
      body: { username, password },
    });
    setToken(res.token);
    setUser(res.user);
    setSettings(res.settings ?? DEFAULT_SETTINGS);
  }, []);

  const register = useCallback(async (username: string, password: string) => {
    const res = await api<{ token: string; user: User; settings: UserSettings }>('/api/auth/register', {
      method: 'POST',
      body: { username, password },
    });
    setToken(res.token);
    setUser(res.user);
    setSettings(res.settings ?? DEFAULT_SETTINGS);
  }, []);

  const logout = useCallback(() => {
    setToken(null);
    setUser(null);
  }, []);

  const patchSettings = useCallback(async (patch: Partial<UserSettings>) => {
    const next = await api<UserSettings>('/api/settings', { method: 'PATCH', body: patch });
    setSettings(next);
  }, []);

  const value = useMemo(
    () => ({ user, settings, ready, login, register, logout, patchSettings, refresh }),
    [user, settings, ready, login, register, logout, patchSettings, refresh],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth() {
  return useContext(Ctx);
}

/* ---------------- 主题 ---------------- */
export function applyTheme(theme: UserSettings['theme']) {
  const dark =
    theme === 'dark' ||
    (theme === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.classList.toggle('dark', dark);
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute('content', dark ? '#0b0c11' : '#f5f6fa');
  try {
    localStorage.setItem('recall.theme', theme);
  } catch { /* noop */ }
}

export function ThemeSync() {
  const { settings } = useAuth();
  useEffect(() => {
    const theme = settings?.theme ?? 'system';
    applyTheme(theme);
    if (theme !== 'system') return;
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const handler = () => applyTheme('system');
    mq.addEventListener('change', handler);
    return () => mq.removeEventListener('change', handler);
  }, [settings?.theme]);
  return null;
}
