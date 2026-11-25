import React, { useCallback, useEffect, useState } from 'react';
import {
  Activity, Check, ChevronDown, ChevronUp, Copy, Eye, EyeOff, KeyRound,
  Link2, Loader2, Plug, RefreshCw, ShieldCheck, Trash2, Wrench,
} from 'lucide-react';
import { api } from '../../lib/api';
import { useToast } from '../../lib/toast';
import { cn, copyText } from '../../lib/utils';
import Section, { Switch } from './Section';

type McpTool = { name: string; description: string; inputSchema: Record<string, any> };
type Config = {
  enabled: boolean;
  endpointPath: string;
  ssePath: string;
  token: string;
  tokenMasked: string;
  createdAt?: string;
  resetAt?: string | null;
  lastUsedAt?: string | null;
  tools: McpTool[];
  stats: { totalCalls: number; toolCalls: number; clientCount: number; lastCallAt?: string | null };
};
type ClientRow = {
  sessionId: string;
  clientName: string;
  clientVersion: string;
  protocolVersion: string;
  ip: string;
  firstSeenAt: string;
  lastSeenAt: string;
  callCount: number;
};
type LogRow = {
  id: string;
  clientName: string;
  method: string;
  tool: string;
  ok: boolean;
  error: string;
  durationMs: number;
  ip: string;
  createdAt: string;
};
type LogsPayload = { clients: ClientRow[]; logs: LogRow[]; stats: Config['stats'] };

const METHOD_LABEL: Record<string, string> = {
  initialize: '握手',
  'tools/list': '列出工具',
  'tools/call': '调用工具',
  auth: '鉴权失败',
};

function stamp(iso?: string | null) {
  if (!iso) return '—';
  const d = new Date(iso);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getMonth() + 1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 「数据与设备 → 开放 API」下面的 MCP 服务卡片：端点 / Token / 工具 / 调用记录 */
export default function McpServiceCard() {
  const toast = useToast();
  const origin = window.location.origin;
  const [config, setConfig] = useState<Config | null>(null);
  const [data, setData] = useState<LogsPayload | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [showToken, setShowToken] = useState(false);
  const [showTools, setShowTools] = useState(false);
  const [showSse, setShowSse] = useState(false);
  const [busy, setBusy] = useState('');

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const [c, l] = await Promise.all([
        api<Config>('/api/mcp-service'),
        api<LogsPayload>('/api/mcp-service/logs?limit=60'),
      ]);
      setConfig(c);
      setData(l);
      setError('');
    } catch (e: any) {
      setError(e?.message ?? '加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(true), 15_000);
    return () => clearInterval(t);
  }, [load]);

  const copy = (text: string, label = '已复制') => {
    void copyText(text);
    toast.push(label, 'success');
  };

  const endpoint = `${origin}${config?.endpointPath ?? '/mcp'}`;
  const sseEndpoint = `${origin}${config?.ssePath ?? '/sse'}`;
  const clientConfig = JSON.stringify(
    {
      mcpServers: {
        忆录: {
          url: endpoint,
          headers: { Authorization: `Bearer ${config?.token ?? '<你的 Token>'}` },
        },
      },
    },
    null,
    2,
  );
  // 页面上跟着「显示 / 隐藏 Token」走，复制出来的永远是能直接用的完整配置
  const displayConfig =
    showToken || !config?.token
      ? clientConfig
      : clientConfig.replace(config.token, '<你的 Token>');

  const resetToken = async () => {
    if (!window.confirm('重置后旧 Token 立即失效，所有客户端都要换成新的。继续？')) return;
    setBusy('token');
    try {
      const r = await api<{ token: string }>('/api/mcp-service/reset-token', { method: 'POST' });
      setConfig((prev) => (prev ? { ...prev, token: r.token } : prev));
      setShowToken(true);
      toast.push('已生成新 Token', 'success');
      await load(true);
    } catch (e: any) {
      toast.push(e?.message ?? '重置失败', 'error');
    } finally {
      setBusy('');
    }
  };

  const toggle = async (next: boolean) => {
    setBusy('enabled');
    try {
      await api('/api/mcp-service', { method: 'PATCH', body: { enabled: next } });
      setConfig((prev) => (prev ? { ...prev, enabled: next } : prev));
      toast.push(next ? 'MCP 服务已启用' : 'MCP 服务已停用', 'success');
    } catch (e: any) {
      toast.push(e?.message ?? '操作失败', 'error');
    } finally {
      setBusy('');
    }
  };

  const clearLogs = async () => {
    if (!window.confirm('清空所有客户端与调用记录？Token 不受影响。')) return;
    setBusy('clear');
    try {
      await api('/api/mcp-service/logs', { method: 'DELETE' });
      await load(true);
      toast.push('已清空', 'success');
    } catch (e: any) {
      toast.push(e?.message ?? '清空失败', 'error');
    } finally {
      setBusy('');
    }
  };

  if (loading && !config) {
    return (
      <Section title="MCP 服务" desc="把忆录开放成 MCP 服务" icon={Plug}>
        <div className="flex items-center gap-2 text-[12px] text-muted py-4">
          <Loader2 size={13} className="animate-spin" /> 正在加载…
        </div>
      </Section>
    );
  }

  if (error && !config) {
    return (
      <Section title="MCP 服务" desc="把忆录开放成 MCP 服务" icon={Plug}>
        <div className="rounded-xl bg-panel2 p-3 text-[12px] text-muted">
          {error}
          <div className="mt-2">
            新接口需要重启后端后生效，或点
            <button onClick={() => void load()} className="text-brand hover:underline mx-1">重试</button>
          </div>
        </div>
      </Section>
    );
  }

  const stats = data?.stats ?? config?.stats;
  const tools = config?.tools ?? [];
  const clients = data?.clients ?? [];
  const logs = data?.logs ?? [];

  return (
    <Section
      title="MCP 服务"
      desc="把忆录开放成 MCP 服务：AI 客户端可以上传录音、按日期读取 AI 纪要、检索回忆"
      icon={Plug}
      action={
        <div className="flex items-center gap-2 shrink-0">
          <span className={cn('text-[11px]', config?.enabled ? 'text-emerald-500' : 'text-muted')}>
            {config?.enabled ? '已启用' : '已停用'}
          </span>
          <Switch checked={!!config?.enabled} onChange={(v) => void toggle(v)} disabled={busy === 'enabled'} />
        </div>
      }
    >
      {/* 端点 */}
      <div className="rounded-xl bg-panel2 p-3">
        <div className="flex items-center gap-1.5 text-[11px] text-muted mb-1.5">
          <Link2 size={11} /> 服务地址（Streamable HTTP，推荐）
        </div>
        <div className="flex items-center gap-2 min-w-0">
          <code className="flex-1 min-w-0 truncate text-[11.5px] font-mono text-ink" title={endpoint}>{endpoint}</code>
          <button onClick={() => copy(endpoint, '地址已复制')} className="btn-ghost h-7 px-2 text-[11px] shrink-0">
            <Copy size={11} /> 复制
          </button>
        </div>
        <button
          onClick={() => setShowSse((v) => !v)}
          className="mt-2 inline-flex items-center gap-1 text-[11px] text-muted hover:text-brand"
        >
          {showSse ? <ChevronUp size={11} /> : <ChevronDown size={11} />} 旧版客户端（SSE）
        </button>
        {showSse && (
          <div className="mt-2 flex items-center gap-2 min-w-0">
            <code className="flex-1 min-w-0 truncate text-[11px] font-mono text-ink/80" title={sseEndpoint}>{sseEndpoint}</code>
            <button onClick={() => copy(sseEndpoint, '地址已复制')} className="text-[11px] text-brand hover:underline shrink-0">复制</button>
          </div>
        )}
      </div>

      {/* Token */}
      <div className="mt-2.5 rounded-xl border border-line px-3 py-2.5">
        <div className="flex items-center gap-1.5 text-[11px] text-muted mb-1.5">
          <ShieldCheck size={11} /> 访问 Token
          <span className="opacity-60">· 首次打开自动生成，重置后旧值立即失效</span>
        </div>
        <div className="flex items-center gap-2 min-w-0">
          <KeyRound size={13} className="text-muted shrink-0" />
          <code className="flex-1 min-w-0 truncate text-[11.5px] font-mono text-ink" title={showToken ? config?.token : ''}>
            {showToken ? config?.token : config?.tokenMasked}
          </code>
          <button
            onClick={() => setShowToken((v) => !v)}
            title={showToken ? '隐藏' : '显示'}
            className="p-1 rounded-md text-muted hover:text-brand hover:bg-brand/10 shrink-0"
          >
            {showToken ? <EyeOff size={12} /> : <Eye size={12} />}
          </button>
          <button onClick={() => copy(config?.token ?? '', 'Token 已复制')} className="p-1 rounded-md text-muted hover:text-brand hover:bg-brand/10 shrink-0" title="复制 Token">
            <Copy size={12} />
          </button>
          <button
            onClick={() => void resetToken()}
            disabled={busy === 'token'}
            className="btn-ghost h-7 px-2 text-[11px] shrink-0 disabled:opacity-50"
          >
            {busy === 'token' ? <Loader2 size={11} className="animate-spin" /> : <RefreshCw size={11} />} 重置
          </button>
        </div>
        <div className="mt-1.5 text-[10.5px] text-muted">
          {config?.createdAt ? `创建于 ${stamp(config.createdAt)}` : ''}
          {config?.resetAt ? ` · 上次重置 ${stamp(config.resetAt)}` : ''}
          {config?.lastUsedAt ? ` · 最后使用 ${stamp(config.lastUsedAt)}` : ' · 还没被调用过'}
        </div>
      </div>

      {/* 客户端配置 */}
      <div className="mt-2.5 rounded-xl bg-panel2 p-3">
        <div className="text-[11px] text-muted mb-1.5">
          粘贴到客户端的 MCP 配置（Claude Desktop / Cursor / WorkBuddy 通用）
          {!showToken && <span className="opacity-70"> · 点上面的小眼睛可显示真实 Token</span>}
        </div>
        <pre className="text-[10.5px] leading-relaxed overflow-x-auto text-ink/80 font-mono whitespace-pre">{displayConfig}</pre>
        <button onClick={() => copy(clientConfig, '配置已复制')} className="mt-2 inline-flex items-center gap-1 text-[11px] text-brand hover:underline">
          <Copy size={11} /> 复制配置
        </button>
      </div>

      {/* 工具清单 */}
      <div className="mt-3">
        <button
          onClick={() => setShowTools((v) => !v)}
          className="flex items-center gap-1.5 text-[12px] font-medium hover:text-brand"
        >
          <Wrench size={12} /> 提供的工具（{tools.length}）
          {showTools ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
        </button>
        {showTools && (
          <div className="mt-2 grid gap-1.5 md:grid-cols-2">
            {tools.map((t) => (
              <div key={t.name} className="rounded-lg border border-line px-2.5 py-2 min-w-0">
                <code className="text-[11px] font-mono text-brand">{t.name}</code>
                <div className="text-[10.5px] text-muted mt-0.5 leading-relaxed line-clamp-2" title={t.description}>{t.description}</div>
              </div>
            ))}
          </div>
        )}
        {!showTools && (
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {tools.map((t) => (
              <span key={t.name} className="chip text-[10.5px] font-mono" title={t.description}>{t.name}</span>
            ))}
          </div>
        )}
      </div>

      {/* 调用概览 */}
      <div className="mt-3 flex items-center gap-1.5 text-[11px] text-muted flex-wrap">
        <Activity size={12} />
        <span>累计调用 <b className="text-ink font-medium">{stats?.totalCalls ?? 0}</b></span>
        <span className="opacity-40">·</span>
        <span>工具调用 <b className="text-ink font-medium">{stats?.toolCalls ?? 0}</b></span>
        <span className="opacity-40">·</span>
        <span>客户端 <b className="text-ink font-medium">{stats?.clientCount ?? 0}</b></span>
        <span className="opacity-40">·</span>
        <span>最近 {stamp(stats?.lastCallAt)}</span>
        <button onClick={() => void load(true)} className="ml-auto inline-flex items-center gap-1 text-brand hover:underline" title="刷新">
          <RefreshCw size={11} /> 刷新
        </button>
        <button onClick={() => void clearLogs()} disabled={busy === 'clear'} className="inline-flex items-center gap-1 text-muted hover:text-red-500 disabled:opacity-50" title="清空记录">
          <Trash2 size={11} /> 清空
        </button>
      </div>

      {/* 客户端 */}
      <div className="mt-3">
        <div className="text-[11.5px] font-medium mb-1.5">连接过的客户端</div>
        {clients.length === 0 ? (
          <div className="text-[11px] text-muted text-center py-3 rounded-xl bg-panel2">还没有客户端连接</div>
        ) : (
          <div className="space-y-1.5 max-h-44 overflow-y-auto pr-0.5">
            {clients.map((c) => (
              <div key={c.sessionId} className="flex items-center gap-2 rounded-lg border border-line px-2.5 py-1.5 min-w-0">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-500 shrink-0" />
                <div className="min-w-0 flex-1">
                  <div className="text-[11.5px] truncate">
                    {c.clientName}
                    {c.clientVersion && <span className="text-muted font-normal"> · v{c.clientVersion}</span>}
                  </div>
                  <div className="text-[10px] text-muted truncate">
                    {c.ip || '未知 IP'} · 最近 {stamp(c.lastSeenAt)} · 会话 {c.sessionId.slice(-8)}
                  </div>
                </div>
                <span className="chip !px-1.5 !py-0 text-[10px] shrink-0">{c.callCount} 次</span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 调用记录 */}
      <div className="mt-3">
        <div className="text-[11.5px] font-medium mb-1.5">最近调用记录</div>
        {logs.length === 0 ? (
          <div className="text-[11px] text-muted text-center py-3 rounded-xl bg-panel2">还没有调用记录</div>
        ) : (
          <div className="space-y-1 max-h-60 overflow-y-auto pr-0.5">
            {logs.map((l) => (
              <div key={l.id} className="flex items-center gap-2 rounded-lg border border-line px-2.5 py-1.5 min-w-0">
                {l.ok ? (
                  <Check size={11} className="text-emerald-500 shrink-0" />
                ) : (
                  <span className="h-2 w-2 rounded-full bg-red-500 shrink-0" title={l.error} />
                )}
                <span className="text-[10.5px] text-muted w-[86px] shrink-0 tabular-nums">{stamp(l.createdAt)}</span>
                <span className="text-[11px] truncate max-w-[110px] shrink-0" title={l.clientName}>{l.clientName}</span>
                <span className="text-[10.5px] text-muted shrink-0">{METHOD_LABEL[l.method] ?? l.method}</span>
                {l.tool && <code className="text-[10.5px] font-mono text-brand truncate min-w-0">{l.tool}</code>}
                {!l.ok && l.error && <span className="text-[10px] text-red-500 truncate" title={l.error}>{l.error}</span>}
                <span className="ml-auto text-[10px] text-muted tabular-nums shrink-0">{l.durationMs ? `${l.durationMs}ms` : ''}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </Section>
  );
}
