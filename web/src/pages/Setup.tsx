import React, { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  AlertCircle, AudioLines, CheckCircle2, Loader2, RefreshCw, ServerCog, ShieldCheck, Sparkles, X,
} from 'lucide-react';
import { createFirstAdmin, getDbStatus, setToken, type DbStatus } from '../lib/api';

type Phase = 'loading' | 'broken' | 'admin' | 'ready';

/**
 * 初始化页：数据库连接信息只来自配置文件（server-py/.env 或容器环境变量），
 * 所以这里不再有「填主机/端口/密码」的表单，只做两件事：
 * 连不上时告诉用户去哪改，连上了但没有账号时创建第一个管理员。
 */
export default function Setup() {
  const navigate = useNavigate();
  const [status, setStatus] = useState<DbStatus | null>(null);
  const [phase, setPhase] = useState<Phase>('loading');
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');

  const load = useCallback(async () => {
    setPhase('loading');
    try {
      const st = await getDbStatus();
      setStatus(st);
      if (!st.connected) {
        setPhase('broken');
        setError(st.error);
      } else if (st.needsAdmin) {
        setPhase('admin');
      } else {
        setPhase('ready');
      }
    } catch (e: any) {
      setError(e?.message ?? '无法读取初始化状态');
      setPhase('broken');
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const createAdmin = async () => {
    if (password.length < 6) return setError('密码至少 6 位');
    if (!confirm) return setError('请再输入一遍密码');
    if (password !== confirm) return setError('两次输入的密码不一致');
    setCreating(true);
    setError(null);
    try {
      const r = await createFirstAdmin(username.trim(), password);
      setToken(r.token);
      window.location.assign('/');
    } catch (e: any) {
      setError(e?.message ?? '创建失败');
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="min-h-screen bg-bg">
      <div className="mx-auto w-full max-w-2xl px-4 py-8 md:py-14">
        <div className="flex items-center gap-3 mb-7">
          <div className="h-11 w-11 rounded-2xl bg-gradient-to-br from-[var(--brand)] to-[var(--brand-2)] grid place-items-center shadow-[0_10px_24px_-10px_var(--brand)]">
            <AudioLines size={22} className="text-white" strokeWidth={2.4} />
          </div>
          <div>
            <div className="text-lg font-semibold tracking-tight">忆录 · 初始化</div>
            <div className="text-[11px] text-muted">数据库连接写在配置文件里，这里只做检查和建号</div>
          </div>
        </div>

        {error && (
          <div className="mb-4 rounded-xl border border-red-500/25 bg-red-500/5 px-3.5 py-2.5 flex items-start gap-2">
            <AlertCircle size={14} className="text-red-500 mt-0.5 shrink-0" />
            <div className="text-[12px] leading-relaxed text-red-500 break-words">{error}</div>
          </div>
        )}

        {phase === 'loading' && (
          <div className="card p-10 grid place-items-center">
            <Loader2 size={22} className="animate-spin text-brand" />
          </div>
        )}

        {phase === 'broken' && (
          <div className="card p-5 md:p-6 space-y-4">
            <div className="flex items-start gap-2.5">
              <ServerCog size={16} className="text-amber-500 mt-0.5 shrink-0" />
              <div>
                <h1 className="text-[17px] font-semibold">数据库连不上</h1>
                <p className="text-[12px] text-muted mt-1 leading-relaxed">
                  数据库只支持 PostgreSQL，连接信息写在配置文件里，改完重启后端即可，页面上不再提供修改入口。
                </p>
              </div>
            </div>

            <div className="rounded-xl bg-panel2 px-3.5 py-3 text-[11.5px] leading-relaxed space-y-1.5">
              <div>
                配置文件：<span className="font-mono text-ink/80 break-all">{status?.configFile ?? 'server-py/.env'}</span>
              </div>
              <div>
                需要一行：
                <span className="font-mono text-ink/80 break-all">
                  {status?.configKey ?? 'DATABASE_URL'}=postgresql://用户:密码@127.0.0.1:5432/recall?sslmode=disable
                </span>
              </div>
              <div className="text-muted">
                Docker 部署改 <span className="font-mono">docker-compose.yml</span> 里 recall 服务的 DATABASE_URL，
                改完 <span className="font-mono">docker compose up -d</span> 重建容器。
              </div>
            </div>

            <div className="rounded-xl border border-line px-3.5 py-2.5 text-[11.5px] text-muted break-all">
              当前连接：<span className="font-mono text-ink/80">{status?.target ?? '未配置'}</span>
            </div>

            <div className="flex flex-col sm:flex-row gap-2">
              <button onClick={() => void load()} className="btn-outline h-10 sm:w-auto">
                <RefreshCw size={14} /> 重新检测
              </button>
              <button onClick={() => navigate('/login', { replace: true })} className="btn-ghost h-10 sm:w-auto">
                <X size={14} /> 返回登录
              </button>
            </div>
          </div>
        )}

        {phase === 'admin' && (
          <div className="card p-5 md:p-6 space-y-5">
            <div className="flex items-start gap-2.5 rounded-xl bg-emerald-500/10 px-3.5 py-3">
              <CheckCircle2 size={15} className="text-emerald-600 dark:text-emerald-400 mt-0.5 shrink-0" />
              <div className="text-[11.5px] leading-relaxed text-emerald-700 dark:text-emerald-300">
                <div className="font-medium">数据库已就绪</div>
                <div className="mt-0.5 break-all">{status?.target}</div>
                {status?.serverVersion && <div className="mt-0.5 opacity-80">{status.serverVersion}</div>}
                <div className="mt-0.5 opacity-80">数据表已自动检查/创建完成</div>
              </div>
            </div>

            <div>
              <h1 className="text-[17px] font-semibold">创建管理员账号</h1>
              <p className="text-[12px] text-muted mt-1">这是这个库里的第一个账号，用它登录后可以继续配置 AI 服务。</p>
            </div>

            <div className="space-y-3">
              <div>
                <label className="label">用户名</label>
                <input className="field" value={username} autoCapitalize="none"
                  onChange={(e) => setUsername(e.target.value)} placeholder="admin" />
              </div>
              <div className="grid sm:grid-cols-2 gap-2.5">
                <div>
                  <label className="label">密码</label>
                  <input className="field" type="password" value={password}
                    onChange={(e) => setPassword(e.target.value)} placeholder="至少 6 位"
                    autoComplete="new-password" />
                </div>
                <div>
                  <label className="label">确认密码</label>
                  <input className="field" type="password" value={confirm}
                    onChange={(e) => setConfirm(e.target.value)} placeholder="再输一次"
                    autoComplete="new-password" />
                </div>
              </div>
              {!!confirm && password !== confirm && (
                <div className="flex items-center gap-1 text-[11px] text-red-500">
                  <AlertCircle size={11} className="shrink-0" /> 两次输入的密码不一致
                </div>
              )}
            </div>

            <div className="flex flex-col sm:flex-row gap-2">
              <button onClick={createAdmin}
                disabled={creating || !username || !password || !confirm || password !== confirm}
                className="btn-primary h-10 flex-1">
                {creating ? <Loader2 size={15} className="animate-spin" /> : <ShieldCheck size={15} />} 创建并进入
              </button>
              <button onClick={() => void load()} className="btn-outline h-10 sm:w-auto">
                <RefreshCw size={14} /> 重新检测
              </button>
            </div>
          </div>
        )}

        {phase === 'ready' && (
          <div className="card p-6 space-y-4 text-center">
            <div className="mx-auto h-12 w-12 rounded-2xl bg-emerald-500/10 grid place-items-center">
              <CheckCircle2 size={22} className="text-emerald-600 dark:text-emerald-400" />
            </div>
            <div>
              <h1 className="text-[17px] font-semibold">数据库已就绪</h1>
              <p className="text-[12px] text-muted mt-1 break-all">{status?.target}</p>
              {status?.serverVersion && <p className="text-[11px] text-muted mt-0.5">{status.serverVersion}</p>}
            </div>
            <div className="flex gap-2 justify-center">
              <button onClick={() => navigate('/login', { replace: true })} className="btn-primary h-10 px-6">
                <Sparkles size={15} /> 去登录
              </button>
            </div>
          </div>
        )}

        <div className="mt-6 text-center text-[11px] text-muted/70">
          初始化只检查数据库结构与账号，不会迁移任何数据
        </div>
      </div>
    </div>
  );
}
