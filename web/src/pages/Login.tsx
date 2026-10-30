import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { AudioLines, ArrowRight, Loader2, Mic, BrainCircuit, CalendarDays, AlertCircle } from 'lucide-react';
import { getDbStatus } from '../lib/api';
import { useAuth } from '../lib/auth';
import { useToast } from '../lib/toast';
import { cn } from '../lib/utils';

const features = [
  { icon: Mic, title: '边录边转写', desc: '每 30 秒自动切片上传，录音没停文字就出来了' },
  { icon: BrainCircuit, title: 'AI 自动纪要', desc: '要点、结论、待办一键整理，还能生成思维导图' },
  { icon: CalendarDays, title: '按天回看', desc: '日历视图串起每一天，随时翻回去找细节' },
];

export default function Login() {
  const { login, register } = useAuth();
  const toast = useToast();
  const navigate = useNavigate();
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [dbError, setDbError] = useState<string | null>(null);

  // 数据库里一个账号都没有（刚切到外部 PostgreSQL）→ 直接进初始化向导
  useEffect(() => {
    void (async () => {
      try {
        const st = await getDbStatus();
        if (st.needsAdmin) navigate('/setup', { replace: true });
        else if (!st.connected) setDbError(st.error || '数据库未连接');
      } catch {
        /* 后端没起来时让登录表单自己报错 */
      }
    })();
  }, [navigate]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!username || !password) return;
    setLoading(true);
    try {
      if (mode === 'login') await login(username, password);
      else await register(username, password);
      navigate('/', { replace: true });
    } catch (err: any) {
      toast.push(err?.message ?? '操作失败', 'error');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-bg flex">
      {/* 左侧品牌面板 */}
      <div className="hidden lg:flex w-[46%] relative overflow-hidden bg-[var(--panel)] border-r border-line">
        <div
          className="absolute inset-0 opacity-[.55]"
          style={{
            background:
              'radial-gradient(900px 520px at 12% 8%, var(--brand-soft), transparent 62%), radial-gradient(700px 500px at 92% 92%, color-mix(in srgb, var(--brand-2) 22%, transparent), transparent 60%)',
          }}
        />
        <div className="relative z-10 flex flex-col justify-between p-12 xl:p-16 w-full">
          <div className="flex items-center gap-3">
            <div className="h-11 w-11 rounded-2xl bg-gradient-to-br from-[var(--brand)] to-[var(--brand-2)] grid place-items-center shadow-[0_10px_24px_-10px_var(--brand)]">
              <AudioLines size={22} className="text-white" strokeWidth={2.4} />
            </div>
            <div>
              <div className="text-xl font-semibold tracking-tight">忆录</div>
              <div className="text-[11px] text-muted tracking-[.18em]">RECALL WORKBENCH</div>
            </div>
          </div>

          <div className="max-w-md">
            <h1 className="text-[38px] xl:text-[42px] font-semibold leading-[1.15] tracking-tight">
              把说过的话
              <br />
              <span className="bg-gradient-to-r from-[var(--brand)] to-[var(--brand-2)] bg-clip-text text-transparent">
                变成可检索的记忆
              </span>
            </h1>
            <p className="mt-5 text-[15px] leading-relaxed text-muted">
              录音、转写、AI 纪要、思维导图、日历回看。全部跑在你自己的服务器上，音频与文字都留在本地。
            </p>

            <div className="mt-10 space-y-4">
              {features.map((f) => (
                <div key={f.title} className="flex items-start gap-3.5">
                  <div className="h-9 w-9 shrink-0 rounded-xl bg-panel2 border border-line grid place-items-center">
                    <f.icon size={16} className="text-brand" />
                  </div>
                  <div>
                    <div className="text-sm font-medium">{f.title}</div>
                    <div className="text-xs text-muted mt-0.5 leading-relaxed">{f.desc}</div>
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="text-[11px] text-muted">
            云端：百炼 Paraformer / Qwen3-ASR · 本地：faster-whisper / whisper.cpp（开源离线）
          </div>
        </div>
      </div>

      {/* 右侧表单 */}
      <div className="flex-1 flex items-center justify-center p-6">
        <div className="w-full max-w-[380px] animate-fade-up">
          <div className="lg:hidden flex items-center gap-2.5 mb-10 justify-center">
            <div className="h-10 w-10 rounded-2xl bg-gradient-to-br from-[var(--brand)] to-[var(--brand-2)] grid place-items-center">
              <AudioLines size={20} className="text-white" strokeWidth={2.4} />
            </div>
            <span className="text-lg font-semibold">忆录</span>
          </div>

          <h2 className="text-2xl font-semibold tracking-tight">
            {mode === 'login' ? '欢迎回来' : '创建账号'}
          </h2>
          <p className="text-sm text-muted mt-1.5">
            {mode === 'login' ? '登录后继续你的录音记忆库' : '数据会保存在你自己的服务器上'}
          </p>

          {dbError && (
            <div className="mt-5 rounded-xl border border-red-500/25 bg-red-500/5 px-3.5 py-3 flex items-start gap-2.5">
              <AlertCircle size={14} className="text-red-500 mt-0.5 shrink-0" />
              <div className="text-[11.5px] leading-relaxed text-red-500 break-words">
                数据库连接失败：{dbError}
                <button
                  onClick={() => navigate('/setup')}
                  className="block mt-1 font-medium underline underline-offset-2"
                >
                  去修正数据库配置
                </button>
              </div>
            </div>
          )}

          <form onSubmit={submit} className="mt-7 space-y-4">
            <div>
              <label className="label">用户名</label>
              <input
                className="field h-11"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="请输入用户名"
                autoComplete="username"
                autoCapitalize="none"
              />
            </div>
            <div>
              <label className="label">密码</label>
              <input
                className="field h-11"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={mode === 'register' ? '至少 6 位' : '请输入密码'}
                autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              />
            </div>

            <button
              type="submit"
              disabled={loading || !username || !password}
              className="btn-primary w-full h-11 mt-2"
            >
              {loading ? (
                <Loader2 size={17} className="animate-spin" />
              ) : (
                <>
                  {mode === 'login' ? '登录' : '注册并进入'}
                  <ArrowRight size={16} />
                </>
              )}
            </button>
          </form>

          <div className="mt-5 text-center text-sm text-muted">
            {mode === 'login' ? '还没有账号？' : '已有账号？'}
            <button
              onClick={() => setMode(mode === 'login' ? 'register' : 'login')}
              className="ml-1 text-brand font-medium hover:underline"
            >
              {mode === 'login' ? '注册一个' : '去登录'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
