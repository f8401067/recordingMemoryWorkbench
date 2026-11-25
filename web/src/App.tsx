import React from 'react';
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { AuthProvider, ThemeSync, useAuth } from './lib/auth';
import { ToastProvider } from './lib/toast';
import Layout from './components/Layout';
import Login from './pages/Login';
import Home from './pages/Home';
import ActionsPage from './pages/Actions';
import RecordPage from './pages/Record';
import RecordingDetail from './pages/RecordingDetail';
import CalendarPage from './pages/Calendar';
import FindPage from './pages/Find';
import SettingsPage from './pages/Settings';
import SetupPage from './pages/Setup';
import SummaryDetailPage from './pages/SummaryDetail';

function Protected({ children }: { children: React.ReactNode }) {
  const { user, ready } = useAuth();
  const location = useLocation();
  if (!ready) {
    return (
      <div className="h-screen grid place-items-center bg-bg">
        <div className="flex flex-col items-center gap-3">
          <div className="h-8 w-8 rounded-full border-2 border-brand border-t-transparent animate-spin" />
          <div className="text-xs text-muted">加载中…</div>
        </div>
      </div>
    );
  }
  if (!user) return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  return <>{children}</>;
}

function GuestOnly({ children }: { children: React.ReactNode }) {
  const { user, ready } = useAuth();
  if (!ready) return <div className="h-screen bg-bg" />;
  if (user) return <Navigate to="/" replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <ToastProvider>
          <ThemeSync />
          <Routes>
            <Route
              path="/login"
              element={
                <GuestOnly>
                  <Login />
                </GuestOnly>
              }
            />
            {/* 初始化向导：数据库连不上或还没有账号时也能访问 */}
            <Route path="/setup" element={<SetupPage />} />
            <Route
              element={
                <Protected>
                  <Layout />
                </Protected>
              }
            >
              <Route index element={<Home />} />
              <Route path="actions" element={<ActionsPage />} />
              <Route path="record" element={<RecordPage />} />
              <Route path="recordings/:id" element={<RecordingDetail />} />
              <Route path="summaries/:id" element={<SummaryDetailPage />} />
              <Route path="calendar" element={<CalendarPage />} />
              <Route path="find" element={<FindPage />} />
              <Route path="settings" element={<SettingsPage />} />
            </Route>
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </ToastProvider>
      </AuthProvider>
    </BrowserRouter>
  );
}
