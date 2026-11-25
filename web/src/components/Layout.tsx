import React from 'react';
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import {
  AudioLines, Compass, History, Home, ListChecks, Settings, Mic, LogOut, Moon, Sun, Monitor,
} from 'lucide-react';
import { cn } from '../lib/utils';
import { useAuth } from '../lib/auth';
import ErrorBoundary from './ErrorBoundary';

const navItems = [
  { to: '/', label: '首页', icon: Home, exact: true },
  { to: '/actions', label: '行动', icon: ListChecks },
  { to: '/calendar', label: '回忆', icon: History },
  { to: '/find', label: '找寻', icon: Compass },
  { to: '/settings', label: '设置', icon: Settings },
];

function Logo({ compact = false }: { compact?: boolean }) {
  return (
    <div className="flex items-center gap-2.5">
      <div className="relative h-9 w-9 rounded-[13px] bg-gradient-to-br from-[var(--brand)] to-[var(--brand-2)] grid place-items-center shadow-[0_6px_16px_-6px_var(--brand)]">
        <AudioLines size={18} className="text-white" strokeWidth={2.4} />
      </div>
      {!compact && (
        <div className="leading-none">
          <div className="text-[17px] font-semibold tracking-tight">忆录</div>
          <div className="text-[10px] text-muted mt-0.5 tracking-wide">RECALL · 录音记忆库</div>
        </div>
      )}
    </div>
  );
}

export default function Layout() {
  const { user, settings, logout, patchSettings } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  // 注意：录音详情是 /recordings/:id，不能只看 startsWith('/record')，否则详情页会误判成沉浸式录音页
  const onRecordPage = location.pathname === '/record';

  const cycleTheme = () => {
    const cur = settings?.theme ?? 'system';
    const next = cur === 'light' ? 'dark' : cur === 'dark' ? 'system' : 'light';
    void patchSettings({ theme: next });
  };
  const ThemeIcon = settings?.theme === 'dark' ? Moon : settings?.theme === 'light' ? Sun : Monitor;

  return (
    <div className="min-h-full flex bg-bg">
      {/* 桌面侧栏 */}
      <aside className="hidden md:flex w-[236px] shrink-0 flex-col border-r border-line bg-panel/60 backdrop-blur-xl sticky top-0 h-screen">
        <div className="px-5 pt-6 pb-5">
          <Logo />
        </div>

        <div className="px-3">
          <button
            onClick={() => navigate('/record')}
            className={cn(
              'w-full h-11 rounded-xl text-white text-sm font-medium flex items-center justify-center gap-2 transition-all active:scale-[.98]',
              'bg-gradient-to-br from-[var(--brand)] to-[var(--brand-2)] shadow-[0_8px_20px_-8px_var(--brand)]',
            )}
          >
            <Mic size={17} strokeWidth={2.4} />
            开始录音
          </button>
        </div>

        <nav className="mt-5 px-3 space-y-1">
          {navItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.exact}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-2.5 h-10 px-3 rounded-xl text-sm transition-colors',
                  isActive
                    ? 'bg-soft text-brand font-medium'
                    : 'text-muted hover:text-ink hover:bg-panel2',
                )
              }
            >
              <item.icon size={17} />
              {item.label}
            </NavLink>
          ))}
        </nav>

        <div className="mt-auto p-3 space-y-2">
          <button
            onClick={cycleTheme}
            className="w-full flex items-center gap-2.5 h-9 px-3 rounded-xl text-xs text-muted hover:bg-panel2 transition-colors"
          >
            <ThemeIcon size={15} />
            {settings?.theme === 'dark' ? '深色模式' : settings?.theme === 'light' ? '浅色模式' : '跟随系统'}
          </button>
          <div className="rounded-xl bg-panel2 px-3 py-2.5">
            <div className="flex items-center justify-between gap-2">
              <div className="min-w-0">
                <div className="text-[13px] font-medium truncate">{user?.displayName || user?.username}</div>
                <div className="text-[11px] text-muted truncate">@{user?.username}</div>
              </div>
              <button
                onClick={logout}
                title="退出登录"
                className="p-1.5 rounded-lg text-muted hover:text-red-500 hover:bg-red-500/10 transition-colors"
              >
                <LogOut size={15} />
              </button>
            </div>
          </div>
        </div>
      </aside>

      {/* 主内容 */}
      <div className="flex-1 min-w-0 flex flex-col">
        {/* 移动端顶栏（录音页使用沉浸式布局，不显示） */}
        {!onRecordPage && (
          <header className="md:hidden sticky top-0 z-30 bg-bg/85 backdrop-blur-xl border-b border-line pt-safe">
            <div className="h-14 px-4 flex items-center justify-between">
              <Logo />
              <button
                onClick={cycleTheme}
                className="h-9 w-9 grid place-items-center rounded-xl bg-panel2 text-muted active:scale-95 transition-transform"
              >
                <ThemeIcon size={16} />
              </button>
            </div>
          </header>
        )}

        <main className={cn('flex-1 min-w-0', onRecordPage ? '' : 'pb-24 md:pb-10')}>
          <ErrorBoundary resetKey={location.pathname}>
            <Outlet />
          </ErrorBoundary>
        </main>
      </div>

      {/* 移动端底部导航 */}
      {!onRecordPage && (
        <nav className="md:hidden fixed bottom-0 inset-x-0 z-40 bg-panel/92 backdrop-blur-xl border-t border-line pb-safe">
          <div className="grid grid-cols-5 h-[58px]">
            {navItems.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.exact}
                className={({ isActive }) =>
                  cn(
                    'flex flex-col items-center justify-center gap-1 text-[10px] transition-colors',
                    isActive ? 'text-brand' : 'text-muted',
                  )
                }
              >
                <item.icon size={20} strokeWidth={2} />
                {item.label}
              </NavLink>
            ))}
          </div>
        </nav>
      )}
    </div>
  );
}
