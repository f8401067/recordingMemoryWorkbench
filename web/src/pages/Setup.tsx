import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  AlertCircle, ArrowRight, AudioLines, CheckCircle2, Database, HardDrive, Loader2,
  Server, ShieldCheck, Sparkles, X,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import {
  applyDbConfig, createFirstAdmin, getDbStatus, setToken, testDbConnection,
  type DbDriver, type DbStatus,
} from '../lib/api';
import { cn } from '../lib/utils';

type Phase = 'loading' | 'choose' | 'admin' | 'ready';

type Form = {
  driver: DbDriver;
  host: string;
  port: string;
  database: string;
  user: string;
  password: string;
  sslmode: string;
  schema: string;
};

const EMPTY: Form = {
  driver: 'postgres',
  host: '127.0.0.1',
  port: '5432',
  database: 'recall',
  user: 'postgres',
  password: '',
  sslmode: 'prefer',
  schema: 'public',
};

export default function Setup() {
  const navigate = useNavigate();
  const [status, setStatus] = useState<DbStatus | null>(null);
  const [phase, setPhase] = useState<Phase>('loading');
  const [form, setForm] = useState<Form>(EMPTY);
  const [testing, setTesting] = useState(false);
  const [applying, setApplying] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');

  const load = useCallback(async () => {
    try {
      const st = await getDbStatus();
      setStatus(st);
      if (st.config) {
        setForm((f) => ({
          ...f,
          driver: st.config!.driver === 'sqlite' ? 'postgres' : st.config!.driver,
          host: st.config!.host,
          port: String(st.config!.port),
          database: st.config!.database,
          user: st.config!.user,
          sslmode: st.config!.sslmode,
          schema: st.config!.schema,
        }));
      }
      if (!st.connected) {
        setPhase('choose');
        setError(st.error);
      } else if (st.needsAdmin) {
        setPhase('admin');
      } else {
        setPhase('ready');
      }
    } catch (e: any) {
      setError(e?.message ?? '无法读取初始化状态');
      setPhase('choose');
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const payload = useMemo(
    () => ({
      driver: form.driver,
      host: form.host.trim(),
      port: Number(form.port) || 5432,
      database: form.database.trim(),
      user: form.user.trim(),
      password: form.password,
      sslmode: form.sslmode,
      schema: form.schema.trim(),
    }),
    [form],
  );

  const test = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const r = await testDbConnection(payload);
      setTestResult({ ok: r.ok, message: r.ok ? `${r.message}${r.serverVersion ? ` · ${r.serverVersion}` : ''}` : r.message });
    } catch (e: any) {
      setTestResult({ ok: false, message: e?.message ?? '测试失败' });
    } finally {
      setTesting(false);
    }
  };

  const apply = async () => {
    setApplying(true);
    setError(null);
    try {
      const r = await applyDbConfig(payload);
      setStatus(r.status);
      setPhase(r.needsAdmin ? 'admin' : 'ready');
    } catch (e: any) {
      setError(e?.message ?? '初始化失败');
      setPhase('choose');
    } finally {
      setApplying(false);
    }
  };

  const createAdmin = async () => {
    if (password.length < 6) return setError('密码至少 6 位');
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
            <div className="text-[11px] text-muted">先确定数据存在哪里，再创建第一个账号</div>
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

        {phase === 'choose' && (
          <div className="card p-5 md:p-6 space-y-5">
            <div>
              <h1 className="text-[17px] font-semibold">选择数据存储</h1>
              <p className="text-[12px] text-muted mt-1 leading-relaxed">
                内置 SQLite 开箱即用，只适合调试和单人使用；长期运行、多人或多端同时访问请用外置 PostgreSQL。
              </p>
            </div>

            <div className="grid sm:grid-cols-2 gap-2.5">
              <DriverCard
                active={form.driver === 'sqlite'}
                icon={HardDrive}
                title="内置 SQLite"
                desc="零配置，数据存在服务器本地文件里"
                tag="仅适合调试"
                onClick={() => setForm({ ...form, driver: 'sqlite' })}
              />
              <DriverCard
                active={form.driver === 'postgres'}
                icon={Database}
                title="外置 PostgreSQL"
                desc="独立数据库服务，适合长期与多人使用"
                tag="推荐正式使用"
                onClick={() => setForm({ ...form, driver: 'postgres' })}
              />
            </div>

            {form.driver === 'postgres' ? (
              <div className="space-y-3">
                <div className="grid grid-cols-3 gap-2.5">
                  <div className="col-span-2">
                    <label className="label">主机</label>
                    <input className="field font-mono text-[12.5px]" value={form.host}
                      onChange={(e) => setForm({ ...form, host: e.target.value })} placeholder="127.0.0.1" />
                  </div>
                  <div>
                    <label className="label">端口</label>
                    <input className="field font-mono text-[12.5px]" value={form.port} inputMode="numeric"
                      onChange={(e) => setForm({ ...form, port: e.target.value.replace(/\D/g, '') })} placeholder="5432" />
                  </div>
                </div>
                <div className="grid sm:grid-cols-2 gap-2.5">
                  <div>
                    <label className="label">数据库名</label>
                    <input className="field font-mono text-[12.5px]" value={form.database}
                      onChange={(e) => setForm({ ...form, database: e.target.value })} placeholder="recall" />
                  </div>
                  <div>
                    <label className="label">用户名</label>
                    <input className="field font-mono text-[12.5px]" value={form.user}
                      onChange={(e) => setForm({ ...form, user: e.target.value })} placeholder="postgres" />
                  </div>
                </div>
                <div className="grid sm:grid-cols-2 gap-2.5">
                  <div>
                    <label className="label">密码</label>
                    <input className="field font-mono text-[12.5px]" type="password" value={form.password}
                      onChange={(e) => setForm({ ...form, password: e.target.value })} placeholder="postgres 密码" />
                  </div>
                  <div>
                    <label className="label">SSL 模式</label>
                    <select className="field text-[12.5px]" value={form.sslmode}
                      onChange={(e) => setForm({ ...form, sslmode: e.target.value })}>
                      <option value="prefer">prefer（默认）</option>
                      <option value="disable">disable（内网/本机）</option>
                      <option value="require">require（强制加密）</option>
                      <option value="verify-full">verify-full（校验证书）</option>
                    </select>
                  </div>
                </div>
                <div>
                  <label className="label">Schema</label>
                  <input className="field font-mono text-[12.5px]" value={form.schema}
                    onChange={(e) => setForm({ ...form, schema: e.target.value })} placeholder="public" />
                </div>
                <p className="text-[11px] text-muted leading-relaxed">
                  数据库不存在时会尝试自动创建（需要建库权限）；已有表会被直接复用，不会覆盖数据。
                </p>
              </div>
            ) : (
              <div className="rounded-xl bg-panel2 px-3.5 py-3 text-[11.5px] text-muted leading-relaxed">
                数据文件：<span className="font-mono text-ink/80">{status?.config?.sqlitePath ?? 'data/recall.db'}</span>
                <br />
                SQLite 没有并发写能力，多人同时使用会出现锁等待，正式使用请切到 PostgreSQL。
              </div>
            )}

            {testResult && (
              <div className={cn(
                'rounded-xl px-3.5 py-2.5 flex items-start gap-2 text-[12px] leading-relaxed',
                testResult.ok ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400' : 'bg-red-500/10 text-red-500',
              )}>
                {testResult.ok ? <CheckCircle2 size={14} className="mt-0.5 shrink-0" /> : <AlertCircle size={14} className="mt-0.5 shrink-0" />}
                <span className="break-words">{testResult.message}</span>
              </div>
            )}

            <div className="flex flex-col sm:flex-row gap-2 pt-1">
              {form.driver === 'postgres' && (
                <button onClick={test} disabled={testing} className="btn-outline h-10 sm:w-auto">
                  {testing ? <Loader2 size={15} className="animate-spin" /> : <Server size={15} />} 测试连接
                </button>
              )}
              <button onClick={apply} disabled={applying} className="btn-primary h-10 flex-1 sm:flex-none sm:px-6">
                {applying ? <Loader2 size={15} className="animate-spin" /> : <ArrowRight size={15} />}
                {form.driver === 'sqlite' ? '使用内置 SQLite 开始' : '连接并初始化'}
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
              <p className="text-[12px] text-muted mt-1">这是新数据库里的第一个账号，用它登录后可以继续配置 AI 服务。</p>
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
                    onChange={(e) => setPassword(e.target.value)} placeholder="至少 6 位" />
                </div>
                <div>
                  <label className="label">确认密码</label>
                  <input className="field" type="password" value={confirm}
                    onChange={(e) => setConfirm(e.target.value)} placeholder="再输一次" />
                </div>
              </div>
            </div>

            <div className="flex flex-col sm:flex-row gap-2">
              <button onClick={createAdmin} disabled={creating || !username || !password}
                className="btn-primary h-10 flex-1">
                {creating ? <Loader2 size={15} className="animate-spin" /> : <ShieldCheck size={15} />} 创建并进入
              </button>
              <button onClick={() => void load()} className="btn-outline h-10 sm:w-auto">
                <X size={14} /> 重新检测
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
          初始化只影响数据库结构，不会迁移任何已有数据
        </div>
      </div>
    </div>
  );
}

function DriverCard({
  active, icon: Icon, title, desc, tag, onClick,
}: {
  active: boolean;
  icon: LucideIcon;
  title: string;
  desc: string;
  tag: string;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        'text-left rounded-xl border p-3.5 transition-all',
        active ? 'border-brand bg-soft/60' : 'border-line hover:border-brand/30 hover:bg-panel2',
      )}
    >
      <div className="flex items-center gap-2">
        <Icon size={15} className={active ? 'text-brand' : 'text-muted'} />
        <span className="text-[13px] font-medium flex-1">{title}</span>
        {active && <CheckCircle2 size={14} className="text-brand shrink-0" />}
      </div>
      <div className="text-[11px] text-muted mt-1.5 leading-relaxed">{desc}</div>
      <span className={cn('chip mt-2 !px-1.5 !py-0 !text-[10px]', active ? 'bg-brand/10 text-brand' : 'bg-panel2 text-muted')}>
        {tag}
      </span>
    </button>
  );
}
