import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertCircle, Check, ChevronDown, ChevronRight, Copy, Loader2, Play,
  Plug, Plus, RefreshCw, RotateCcw, Save, Send, Sparkles, Trash2, Upload, Wand2, X,
} from 'lucide-react';
import {
  callMcpTool, createMcpServer, deleteMcpServer, getMcpToolArgs, importMcpServers,
  listMcpLogs, listMcpServers, refreshMcpServer, setMcpToolArgs,
  setMcpToolCapability, suggestMcpToolArgs, updateMcpServer,
  type McpCallLog, type McpCallResult, type McpPushMode, type McpServer, type McpTool,
} from '../../lib/api';
import { useToast } from '../../lib/toast';
import { cn, copyText } from '../../lib/utils';
import Section, { Switch } from './Section';

type FormState = {
  id?: string;
  name: string;
  url: string;
  transport: 'auto' | 'http' | 'sse';
  headersText: string;
  enabled: boolean;
  useInChat: boolean;
  pushMode: McpPushMode;
  pushHint: string;
};

const EMPTY_FORM: FormState = {
  name: '',
  url: '',
  transport: 'auto',
  headersText: '',
  enabled: true,
  useInChat: true,
  pushMode: 'manual',
  pushHint: '',
};

const PUSH_LABEL: Record<McpPushMode, string> = {
  off: '不推送',
  manual: '手动发送',
  auto: '自动发送',
};

const EXAMPLE = `{
  "mcpServers": {
    "我的笔记": {
      "url": "https://example.com/mcp",
      "headers": { "Authorization": "Bearer sk-..." }
    }
  }
}`;

/** 从工具 schema 里猜一份可编辑的参数骨架，调试时不用从零写 */
function sampleArgs(tool: McpTool | undefined): string {
  const props = (tool?.inputSchema as any)?.properties ?? {};
  const required: string[] = (tool?.inputSchema as any)?.required ?? [];
  const out: Record<string, unknown> = {};
  for (const [key, spec] of Object.entries<any>(props)) {
    const type = spec?.type ?? 'string';
    if (type === 'number' || type === 'integer') out[key] = 1;
    else if (type === 'boolean') out[key] = false;
    else if (type === 'array') out[key] = [];
    else if (type === 'object') out[key] = {};
    else out[key] = spec?.enum?.[0] ?? '';
    // 非必填的先给空字符串占位，方便用户按需填
    if (!required.includes(key) && out[key] === '') out[key] = '';
  }
  return JSON.stringify(out, null, 2);
}

function StatusBadge({ server }: { server: McpServer }) {
  if (server.lastStatus === 'ok') {
    return (
      <span className="chip bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 !px-1.5 !py-0">
        <Check size={10} /> {server.toolCount} 个工具
        {server.summaryToolCount > 0 && ` · ${server.summaryToolCount} 个可发纪要`}
      </span>
    );
  }
  if (server.lastStatus === 'error') {
    return (
      <span className="chip bg-red-500/10 text-red-600 dark:text-red-400 !px-1.5 !py-0">
        <AlertCircle size={10} /> 连接失败
      </span>
    );
  }
  return <span className="chip bg-panel2 text-muted !px-1.5 !py-0">未连接</span>;
}

type CapBody = Parameters<typeof setMcpToolCapability>[2];

function ToolRow({
  tool, serverId, onChanged,
}: {
  tool: McpTool;
  serverId: string;
  onChanged: (next: McpServer) => void;
}) {
  const toast = useToast();
  const [saving, setSaving] = useState(false);
  const props = (tool.inputSchema as any)?.properties ?? {};
  const params = Object.keys(props);
  const stringParams = params.filter((k) => {
    const spec = props[k] ?? {};
    const t = spec?.type;
    return !t || t === 'string' || (Array.isArray(t) && t.includes('string')) || Array.isArray(spec?.anyOf);
  });
  const cap = tool.capability;
  const enabled = !!cap?.sendSummary;
  const manual = !!cap?.manual;

  const apply = async (body: CapBody, msg: string) => {
    setSaving(true);
    try {
      onChanged(await setMcpToolCapability(serverId, tool.name, body));
      toast.push(msg, 'success');
    } catch (e: any) {
      toast.push(e?.message ?? '保存失败', 'error');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="rounded-lg border border-line/70 bg-panel px-2.5 py-2">
      <div className="flex items-center gap-2 flex-wrap">
        <code className="text-[11.5px] font-medium text-brand">{tool.name}</code>
        {tool.title && <span className="text-[11px] text-muted">{tool.title}</span>}
        <button
          onClick={() => void apply({ sendSummary: !enabled }, enabled ? '已取消，不再出现在纪要下方' : '已设为可发送纪要')}
          disabled={saving}
          title={enabled ? '点一下取消这个工具' : '不认同自动判定？点一下自己指定'}
          className={cn(
            'chip !px-1.5 !py-0 inline-flex items-center gap-1 disabled:opacity-50',
            enabled ? 'bg-brand/10 text-brand' : 'bg-panel2 text-muted hover:text-ink',
          )}
        >
          {saving ? <Loader2 size={10} className="animate-spin" /> : <Send size={10} />}
          {enabled ? '可发送纪要' : '设为可发送'}
        </button>
        {manual && (
          <span className="chip bg-amber-500/10 text-amber-600 dark:text-amber-400 !px-1.5 !py-0">
            手动{enabled ? '指定' : '排除'}
          </span>
        )}
        {!manual && cap && <span className="text-[10px] text-muted/70">自动判定</span>}
        {params.length > 0 && (
          <span className="text-[10.5px] text-muted/80 font-mono">
            {params.slice(0, 5).join(', ')}
            {params.length > 5 ? ' …' : ''}
          </span>
        )}
        {tool.suggestedArgs && Object.keys(tool.suggestedArgs).length > 0 && (
          <span
            className="chip bg-brand/10 text-brand !px-1.5 !py-0 cursor-help"
            title={Object.entries(tool.suggestedArgs)
              .map(([k, v]) => `${k} = ${JSON.stringify(v)?.slice(0, 60)}`)
              .join('\n')}
          >
            AI 建议参数 {Object.keys(tool.suggestedArgs).length} 项
          </span>
        )}
      </div>
      {tool.description && (
        <p className="mt-1 text-[11.5px] text-muted leading-relaxed line-clamp-2">{tool.description}</p>
      )}
      {enabled && (
        <div className="mt-1.5 flex items-center gap-1.5 flex-wrap text-[10.5px] text-muted/80">
          <span>正文 →</span>
          <select
            value={cap?.bodyField || ''}
            disabled={saving || stringParams.length === 0}
            onChange={(e) => void apply(
              { sendSummary: true, bodyField: e.target.value, titleField: cap?.titleField, dateField: cap?.dateField },
              '正文字段已更新',
            )}
            className="h-6 rounded border border-line bg-panel2 px-1.5 text-[10.5px] font-mono text-ink"
          >
            {!cap?.bodyField && <option value="">自动推断</option>}
            {stringParams.map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
          {cap?.titleField && <> · 标题 → <code className="font-mono">{cap.titleField}</code></>}
        </div>
      )}
      {manual && (
        <button
          onClick={() => void apply({ reset: true }, '已恢复自动判定')}
          disabled={saving}
          className="mt-1.5 inline-flex items-center gap-1 text-[10.5px] text-muted hover:text-ink disabled:opacity-50"
        >
          <RotateCcw size={10} /> 恢复自动判定
          {cap?.autoSendSummary !== undefined && (
            <span>· 自动判定为{cap.autoSendSummary ? '可发送' : '不可发送'}</span>
          )}
        </button>
      )}
    </div>
  );
}

function DebugPanel({
  server,
  onClose,
}: {
  server: McpServer;
  onClose: () => void;
}) {
  const toast = useToast();
  const tools = server.tools ?? [];
  const [toolName, setToolName] = useState(tools[0]?.name ?? '');
  // 首工具优先用存库的建议参数（大模型生成 / 人工改过），没有再用骨架
  const [args, setArgs] = useState(() => {
    const first = tools[0];
    const suggested = first?.suggestedArgs;
    return suggested && Object.keys(suggested).length > 0
      ? JSON.stringify(suggested, null, 2)
      : sampleArgs(first);
  });
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<McpCallResult | null>(null);
  const [logs, setLogs] = useState<McpCallLog[]>([]);
  const [suggesting, setSuggesting] = useState(false);
  const [savingTpl, setSavingTpl] = useState(false);
  const [suggestNote, setSuggestNote] = useState('');

  const selected = useMemo(() => tools.find((t) => t.name === toolName), [tools, toolName]);

  /** 选中工具时：优先用存库的建议参数（大模型生成 / 人工改过），没有再用骨架 */
  const fillArgs = useCallback(
    (tool: McpTool | undefined) => {
      const suggested = tool?.suggestedArgs;
      setArgs(
        suggested && Object.keys(suggested).length > 0
          ? JSON.stringify(suggested, null, 2)
          : sampleArgs(tool),
      );
      setSuggestNote('');
    },
    [],
  );

  const loadLogs = useCallback(async () => {
    try {
      setLogs(await listMcpLogs(server.id, 8));
    } catch {
      /* 日志不是关键路径 */
    }
  }, [server.id]);

  useEffect(() => {
    void loadLogs();
  }, [loadLogs]);

  const run = async () => {
    let parsed: Record<string, unknown>;
    try {
      parsed = args.trim() ? JSON.parse(args) : {};
    } catch {
      toast.push('参数不是合法 JSON', 'error');
      return;
    }
    setRunning(true);
    setResult(null);
    try {
      const res = await callMcpTool(server.id, toolName, parsed);
      setResult(res);
      if (res.ok && !res.isError) toast.push('调用成功', 'success');
      else toast.push(res.error || '工具返回错误', 'error');
      void loadLogs();
    } catch (e: any) {
      toast.push(e?.message ?? '调用失败', 'error');
    } finally {
      setRunning(false);
    }
  };

  /** 现场让大模型按 schema 重新生成一份建议参数 */
  const suggest = async () => {
    setSuggesting(true);
    setSuggestNote('');
    try {
      const res = await suggestMcpToolArgs(server.id, toolName);
      setArgs(JSON.stringify(res.args, null, 2));
      setSuggestNote(res.note);
      toast.push('已按大模型建议更新参数', 'success');
    } catch (e: any) {
      toast.push(e?.message ?? '生成建议参数失败', 'error');
    } finally {
      setSuggesting(false);
    }
  };

  /** 把当前编辑器里的参数存为这个工具的默认值（刷新服务也保留） */
  const saveTemplate = async () => {
    let parsed: Record<string, unknown>;
    try {
      parsed = args.trim() ? JSON.parse(args) : {};
    } catch {
      toast.push('参数不是合法 JSON，先改对再保存', 'error');
      return;
    }
    setSavingTpl(true);
    try {
      await setMcpToolArgs(server.id, toolName, parsed);
      toast.push('已存为默认参数', 'success');
    } catch (e: any) {
      toast.push(e?.message ?? '保存失败', 'error');
    } finally {
      setSavingTpl(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-end md:items-center justify-center bg-black/40 backdrop-blur-sm p-0 md:p-6">
      <div className="w-full md:max-w-2xl max-h-[92vh] overflow-y-auto rounded-t-2xl md:rounded-2xl border border-line bg-panel shadow-2xl">
        <div className="sticky top-0 z-10 flex items-center justify-between gap-3 px-4 py-3 bg-panel border-b border-line">
          <div className="min-w-0">
            <div className="text-[14px] font-semibold flex items-center gap-1.5">
              <Wand2 size={14} className="text-brand" />
              调试 · {server.name}
            </div>
            <div className="text-[11px] text-muted truncate">{server.url}</div>
          </div>
          <button onClick={onClose} className="btn-ghost h-8 w-8 !px-0 shrink-0">
            <X size={15} />
          </button>
        </div>

        <div className="p-4 space-y-3">
          {tools.length === 0 ? (
            <div className="text-[12.5px] text-muted py-6 text-center">
              还没有工具清单，先点「刷新」连一次。
            </div>
          ) : (
            <>
              <div>
                <label className="text-[11.5px] text-muted">工具</label>
                <select
                  value={toolName}
                  onChange={(e) => {
                    setToolName(e.target.value);
                    fillArgs(tools.find((t) => t.name === e.target.value));
                  }}
                  className="field w-full h-9 mt-1 text-[12.5px]"
                >
                  {tools.map((t) => (
                    <option key={t.name} value={t.name}>
                      {t.name}{t.title ? ` · ${t.title}` : ''}
                    </option>
                  ))}
                </select>
                {selected?.description && (
                  <p className="mt-1.5 text-[11px] text-muted leading-relaxed">{selected.description}</p>
                )}
              </div>

              <div>
                <div className="flex items-center justify-between">
                  <label className="text-[11.5px] text-muted">参数（JSON）</label>
                  <div className="flex items-center gap-2">
                    {suggestNote && (
                      <span className="text-[10px] text-brand/80 max-w-[180px] truncate" title={suggestNote}>
                        {suggestNote}
                      </span>
                    )}
                    <button
                      onClick={() => setArgs(sampleArgs(selected))}
                      className="text-[11px] text-brand hover:underline"
                    >
                      重置为模板
                    </button>
                    <button
                      onClick={() => void suggest()}
                      disabled={suggesting}
                      className="text-[11px] text-brand hover:underline inline-flex items-center gap-0.5 disabled:opacity-50"
                      title="让大模型按工具 schema 重新生成一份建议参数"
                    >
                      {suggesting ? <Loader2 size={10} className="animate-spin" /> : <Sparkles size={10} />}
                      AI 生成
                    </button>
                    <button
                      onClick={() => void saveTemplate()}
                      disabled={savingTpl}
                      className="text-[11px] text-brand hover:underline inline-flex items-center gap-0.5 disabled:opacity-50"
                      title="把当前参数存为这个工具的默认值（刷新服务也保留）"
                    >
                      {savingTpl ? <Loader2 size={10} className="animate-spin" /> : <Save size={10} />}
                      存为模板
                    </button>
                  </div>
                </div>
                <textarea
                  value={args}
                  onChange={(e) => setArgs(e.target.value)}
                  rows={6}
                  spellCheck={false}
                  className="field w-full mt-1 font-mono text-[12px] leading-relaxed"
                />
              </div>

              <div className="flex items-center gap-2">
                <button onClick={() => void run()} disabled={running} className="btn-primary h-9 px-3 text-[12.5px]">
                  {running ? <Loader2 size={13} className="animate-spin" /> : <Play size={13} />}
                  调用一次
                </button>
                {result && (
                  <span className={cn('text-[11.5px]', result.ok && !result.isError ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600 dark:text-red-400')}>
                    {result.ok ? (result.isError ? '工具返回错误' : '成功') : '连接失败'} · {result.latencyMs}ms
                    {result.transport ? ` · ${result.transport}` : ''}
                  </span>
                )}
              </div>

              {result && (
                <div>
                  <div className="flex items-center justify-between">
                    <label className="text-[11.5px] text-muted">返回</label>
                    <button
                      onClick={() => void copyText(result.text || JSON.stringify(result.structured, null, 2))}
                      className="text-[11px] text-brand hover:underline inline-flex items-center gap-1"
                    >
                      <Copy size={10} /> 复制
                    </button>
                  </div>
                  <pre className="mt-1 max-h-64 overflow-auto rounded-lg border border-line bg-panel2/60 p-2.5 text-[11.5px] leading-relaxed whitespace-pre-wrap break-words">
                    {result.error
                      ? result.error
                      : result.text || JSON.stringify(result.structured ?? result.content, null, 2)}
                  </pre>
                </div>
              )}
            </>
          )}

          {logs.length > 0 && (
            <div className="pt-1">
              <div className="text-[11.5px] text-muted mb-1.5">最近调用</div>
              <div className="space-y-1">
                {logs.map((l) => (
                  <div key={l.id} className="flex items-center gap-2 text-[11px] text-muted">
                    <span className={cn('h-1.5 w-1.5 rounded-full shrink-0', l.ok ? 'bg-emerald-500' : 'bg-red-500')} />
                    <code className="text-ink/80">{l.tool}</code>
                    <span className="text-muted/70">
                      {l.source === 'push' ? '推送' : l.source === 'chat' ? '对话' : l.source === 'summary' ? '纪要' : '调试'} · {l.durationMs}ms
                    </span>
                    <span className="ml-auto shrink-0">
                      {(l.createdAt ?? '').slice(5, 16).replace('T', ' ')}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default function McpTab() {
  const toast = useToast();
  const [servers, setServers] = useState<McpServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [form, setForm] = useState<FormState | null>(null);
  const [saving, setSaving] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [debugFor, setDebugFor] = useState<McpServer | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [importText, setImportText] = useState('');
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState<{ added: string[]; skipped: { name: string; reason: string }[] } | null>(null);

  const load = useCallback(async () => {
    try {
      setServers(await listMcpServers());
    } catch (e: any) {
      toast.push(e?.message ?? '加载 MCP 配置失败', 'error');
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    void load();
  }, [load]);

  const patch = async (server: McpServer, body: Partial<McpServer>) => {
    try {
      const next = await updateMcpServer(server.id, body);
      setServers((cur) => cur.map((s) => (s.id === next.id ? { ...s, ...next } : s)));
    } catch (e: any) {
      toast.push(e?.message ?? '保存失败', 'error');
    }
  };

  const refresh = async (server: McpServer) => {
    setBusyId(server.id);
    try {
      const next = await refreshMcpServer(server.id);
      setServers((cur) => cur.map((s) => (s.id === next.id ? next : s)));
      setExpanded((cur) => ({ ...cur, [next.id]: true }));
      if (next.probe?.ok) {
        toast.push(
          `已连接，发现 ${next.toolCount} 个工具，其中 ${next.summaryToolCount} 个可一键发送 AI 纪要`,
          'success',
        );
      }
      else toast.push(next.lastError || '连接失败', 'error');
    } catch (e: any) {
      toast.push(e?.message ?? '连接失败', 'error');
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (server: McpServer) => {
    if (!window.confirm(`删除 MCP 服务「${server.name}」？`)) return;
    try {
      await deleteMcpServer(server.id);
      setServers((cur) => cur.filter((s) => s.id !== server.id));
      toast.push('已删除', 'success');
    } catch (e: any) {
      toast.push(e?.message ?? '删除失败', 'error');
    }
  };

  const openEdit = (server: McpServer) => {
    setForm({
      id: server.id,
      name: server.name,
      url: server.url,
      transport: server.transport,
      headersText: server.hasHeaders ? JSON.stringify(server.headers, null, 2) : '',
      enabled: server.enabled,
      useInChat: server.useInChat,
      pushMode: server.pushMode,
      pushHint: server.pushHint || '',
    });
  };

  const save = async () => {
    if (!form) return;
    if (!form.name.trim() || !form.url.trim()) {
      toast.push('名称和地址都要填', 'error');
      return;
    }
    let headers: Record<string, string> | undefined;
    if (form.headersText.trim()) {
      try {
        const parsed = JSON.parse(form.headersText);
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('bad');
        headers = parsed as Record<string, string>;
      } catch {
        toast.push('请求头需要是 JSON 对象，例如 {"Authorization": "Bearer xxx"}', 'error');
        return;
      }
    } else {
      headers = {};
    }

    const body: Partial<McpServer> = {
      name: form.name.trim(),
      url: form.url.trim(),
      transport: form.transport,
      headers,
      enabled: form.enabled,
      useInChat: form.useInChat,
      pushMode: form.pushMode,
      pushHint: form.pushHint.trim(),
    };

    setSaving(true);
    try {
      if (form.id) {
        const next = await updateMcpServer(form.id, body);
        setServers((cur) => cur.map((s) => (s.id === next.id ? next : s)));
        toast.push('已保存', 'success');
      } else {
        const created = await createMcpServer(body);
        setServers((cur) => [...cur, created]);
        setExpanded((cur) => ({ ...cur, [created.id]: true }));
        toast.push(
          created.probe?.ok
            ? `已添加，发现 ${created.toolCount} 个工具，其中 ${created.summaryToolCount} 个可一键发送 AI 纪要`
            : `已添加，但连接失败：${created.lastError}`,
          created.probe?.ok ? 'success' : 'error',
        );
      }
      setForm(null);
    } catch (e: any) {
      toast.push(e?.message ?? '保存失败', 'error');
    } finally {
      setSaving(false);
    }
  };

  const doImport = async () => {
    if (!importText.trim()) return;
    setImporting(true);
    setImportResult(null);
    try {
      const res = await importMcpServers(importText);
      setImportResult({
        added: res.added.map(
          (s) => `${s.name}（${s.toolCount} 个工具，${s.summaryToolCount} 个可发纪要）`,
        ),
        skipped: res.skipped,
      });
      setServers(await listMcpServers());
      if (res.added.length) toast.push(`已导入 ${res.added.length} 个服务`, 'success');
    } catch (e: any) {
      toast.push(e?.message ?? '导入失败', 'error');
    } finally {
      setImporting(false);
    }
  };

  const totalTools = servers.reduce((n, s) => n + s.toolCount, 0);

  return (
    <>
      <Section
        title="MCP 工具"
        desc="接入你自己的 MCP 服务（远程 HTTP / SSE）。添加或刷新时会自动识别能接收 AI 纪要的工具：对话里可用、找寻总结可推送，每份 AI 纪要下方也会出现对应的一键发送按钮。"
        icon={Plug}
        action={
          <div className="flex items-center gap-1.5 shrink-0">
            <button onClick={() => setImportOpen(true)} className="btn-ghost h-8 px-2.5 text-[12px]">
              <Upload size={13} /> 导入 JSON
            </button>
            <button onClick={() => setForm({ ...EMPTY_FORM })} className="btn-ghost h-8 px-2.5 text-[12px]">
              <Plus size={13} /> 添加
            </button>
          </div>
        }
      >
        <div className="space-y-2.5">
          {loading && <div className="text-[12px] text-muted py-4 text-center">加载中…</div>}

          {!loading && servers.length === 0 && (
            <div className="rounded-xl border border-dashed border-line px-4 py-8 text-center">
              <div className="text-[12.5px] text-muted">
                还没有配置 MCP 服务。可以粘贴一段 JSON，或手动填写地址。
              </div>
              <div className="mt-3 flex items-center justify-center gap-2">
                <button onClick={() => setImportOpen(true)} className="btn-outline h-8 px-3 text-[12px]">
                  <Upload size={12} /> 导入 JSON 配置
                </button>
                <button onClick={() => setForm({ ...EMPTY_FORM })} className="btn-outline h-8 px-3 text-[12px]">
                  <Plus size={12} /> 手动添加
                </button>
              </div>
            </div>
          )}

          {servers.map((s) => {
            const open = !!expanded[s.id];
            return (
              <div key={s.id} className="rounded-xl border border-line bg-panel2/30">
                <div className="p-3">
                  <div className="flex items-start gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className={cn('text-[13px] font-medium', !s.enabled && 'text-muted line-through')}>
                          {s.name}
                        </span>
                        <StatusBadge server={s} />
                        <span className="chip bg-panel2 text-muted !px-1.5 !py-0 uppercase">{s.transport}</span>
                        {!s.enabled && <span className="text-[10.5px] text-muted">已停用</span>}
                      </div>
                      <div className="mt-0.5 text-[11px] text-muted truncate font-mono">{s.url}</div>
                      {s.lastStatus === 'error' && s.lastError && (
                        <div className="mt-1 text-[11px] text-red-600 dark:text-red-400 line-clamp-2">
                          {s.lastError}
                        </div>
                      )}
                    </div>
                    <div className="flex items-center gap-1 shrink-0">
                      <button
                        onClick={() => void refresh(s)}
                        disabled={busyId === s.id}
                        className="btn-ghost h-7 w-7 !px-0"
                        title="重新连接并刷新工具"
                      >
                        <RefreshCw size={13} className={cn(busyId === s.id && 'animate-spin')} />
                      </button>
                      <button onClick={() => setDebugFor(s)} className="btn-ghost h-7 w-7 !px-0" title="调试">
                        <Wand2 size={13} />
                      </button>
                      <button onClick={() => openEdit(s)} className="btn-ghost h-7 w-7 !px-0" title="编辑">
                        <ChevronRight size={14} />
                      </button>
                      <button onClick={() => void remove(s)} className="btn-ghost h-7 w-7 !px-0 text-muted hover:text-red-500" title="删除">
                        <Trash2 size={13} />
                      </button>
                    </div>
                  </div>

                  <div className="mt-2.5 flex items-center gap-4 flex-wrap text-[11.5px]">
                    <label className="flex items-center gap-1.5">
                      <Switch checked={s.enabled} onChange={(v) => void patch(s, { enabled: v })} />
                      启用
                    </label>
                    <label className="flex items-center gap-1.5">
                      <Switch checked={s.useInChat} onChange={(v) => void patch(s, { useInChat: v })} />
                      对话中可用
                    </label>
                    <label className="flex items-center gap-1.5">
                      <span className="text-muted">总结</span>
                      <select
                        value={s.pushMode}
                        onChange={(e) => void patch(s, { pushMode: e.target.value as McpPushMode })}
                        className="field h-7 py-0 pr-6 text-[11.5px] w-auto"
                      >
                        {(['off', 'manual', 'auto'] as McpPushMode[]).map((m) => (
                          <option key={m} value={m}>{PUSH_LABEL[m]}</option>
                        ))}
                      </select>
                    </label>
                    {s.pushMode !== 'off' && (
                      <span className="text-muted" title={s.pushHint || undefined}>
                        <Sparkles size={11} className="inline -mt-0.5 mr-0.5 text-brand" />
                        工具由模型判断{s.pushHint ? ` · ${s.pushHint}` : ''}
                      </span>
                    )}
                  </div>
                </div>

                {s.toolCount > 0 && (
                  <div className="px-3 pb-2">
                    <button
                      onClick={() => setExpanded((cur) => ({ ...cur, [s.id]: !open }))}
                      className="inline-flex items-center gap-1 text-[11.5px] text-muted hover:text-ink"
                    >
                      {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                      {open ? '收起工具' : `查看 ${s.toolCount} 个工具`}
                    </button>
                    {open && (
                      <div className="mt-2 space-y-1.5">
                        {(s.tools ?? []).map((t) => (
                          <ToolRow
                            key={t.name}
                            tool={t}
                            serverId={s.id}
                            onChanged={(next) =>
                              setServers((cur) => cur.map((x) => (x.id === next.id ? next : x)))
                            }
                          />
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}

          {servers.length > 0 && (
            <p className="text-[11px] text-muted/80 pt-1">
              共 {servers.length} 个服务、{totalTools} 个工具。「可发送纪要」默认按参数自动判定，每个工具都能点一下手动指定或取消，也能选正文写进哪个参数；手动选择会一直保留，刷新服务、判定规则升级都不会覆盖，点「恢复自动判定」即可还原。「找寻」总结的自动/手动推送仍由大模型按工具说明决定。
            </p>
          )}
        </div>
      </Section>

      {/* 添加 / 编辑 */}
      {form && (
        <div className="fixed inset-0 z-50 flex items-end md:items-center justify-center bg-black/40 backdrop-blur-sm p-0 md:p-6">
          <div className="w-full md:max-w-xl max-h-[92vh] overflow-y-auto rounded-t-2xl md:rounded-2xl border border-line bg-panel shadow-2xl">
            <div className="sticky top-0 z-10 flex items-center justify-between px-4 py-3 bg-panel border-b border-line">
              <div className="text-[14px] font-semibold">{form.id ? '编辑 MCP 服务' : '添加 MCP 服务'}</div>
              <button onClick={() => setForm(null)} className="btn-ghost h-8 w-8 !px-0">
                <X size={15} />
              </button>
            </div>

            <div className="p-4 space-y-3">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div>
                  <label className="text-[11.5px] text-muted">名称</label>
                  <input
                    value={form.name}
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                    placeholder="例如：我的笔记"
                    className="field w-full h-9 mt-1 text-[12.5px]"
                  />
                </div>
                <div>
                  <label className="text-[11.5px] text-muted">传输方式</label>
                  <select
                    value={form.transport}
                    onChange={(e) => setForm({ ...form, transport: e.target.value as FormState['transport'] })}
                    className="field w-full h-9 mt-1 text-[12.5px]"
                  >
                    <option value="auto">自动（先 HTTP 再 SSE）</option>
                    <option value="http">Streamable HTTP</option>
                    <option value="sse">SSE</option>
                  </select>
                </div>
              </div>

              <div>
                <label className="text-[11.5px] text-muted">服务地址</label>
                <input
                  value={form.url}
                  onChange={(e) => setForm({ ...form, url: e.target.value })}
                  placeholder="https://example.com/mcp"
                  spellCheck={false}
                  className="field w-full h-9 mt-1 text-[12.5px] font-mono"
                />
              </div>

              <div>
                <label className="text-[11.5px] text-muted">请求头（JSON，可留空）</label>
                <textarea
                  value={form.headersText}
                  onChange={(e) => setForm({ ...form, headersText: e.target.value })}
                  rows={3}
                  spellCheck={false}
                  placeholder={'{\n  "Authorization": "Bearer sk-..."\n}'}
                  className="field w-full mt-1 font-mono text-[12px]"
                />
                <p className="mt-1 text-[10.5px] text-muted/80">
                  已保存的密钥会显示成掩码；不改动就保持原值。
                </p>
              </div>

              <div className="flex items-center gap-5 pt-1">
                <label className="flex items-center gap-1.5 text-[12px]">
                  <Switch checked={form.enabled} onChange={(v) => setForm({ ...form, enabled: v })} />
                  启用
                </label>
                <label className="flex items-center gap-1.5 text-[12px]">
                  <Switch checked={form.useInChat} onChange={(v) => setForm({ ...form, useInChat: v })} />
                  对话中可用
                </label>
              </div>

              <div className="rounded-xl border border-line bg-panel2/40 p-3 space-y-2.5">
                <div className="flex items-center gap-2">
                  <Send size={13} className="text-muted" />
                  <span className="text-[12.5px] font-medium">总结推送</span>
                  <select
                    value={form.pushMode}
                    onChange={(e) => setForm({ ...form, pushMode: e.target.value as McpPushMode })}
                    className="field h-8 py-0 pr-6 text-[12px] w-auto ml-auto"
                  >
                    {(['off', 'manual', 'auto'] as McpPushMode[]).map((m) => (
                      <option key={m} value={m}>{PUSH_LABEL[m]}</option>
                    ))}
                  </select>
                </div>
                {form.pushMode !== 'off' && (
                  <div>
                    <label className="text-[11px] text-muted">推送偏好（可选，自然语言）</label>
                    <textarea
                      value={form.pushHint}
                      onChange={(e) => setForm({ ...form, pushHint: e.target.value })}
                      rows={3}
                      placeholder="例如：只把包含待办或结论的内容保存下来，标签统一用 recall"
                      className="field w-full mt-1 text-[12px] leading-relaxed"
                    />
                    <p className="mt-1 text-[10.5px] text-muted/80 leading-relaxed">
                      调哪个工具、参数怎么填由大模型根据工具的说明和 schema 决定；这里只写你的偏好，不用填工具名。
                    </p>
                  </div>
                )}
                <p className="text-[10.5px] text-muted/80 leading-relaxed">
                  添加服务时会自动识别能接收纪要的工具；AI 纪要下方的一键按钮是手动点击，不受这里的自动/手动开关影响。
                </p>
              </div>

              <div className="flex items-center justify-end gap-2 pt-1">
                <button onClick={() => setForm(null)} className="btn-outline h-9 px-3.5 text-[12.5px]">
                  取消
                </button>
                <button onClick={() => void save()} disabled={saving} className="btn-primary h-9 px-3.5 text-[12.5px]">
                  {saving ? <Loader2 size={13} className="animate-spin" /> : <Check size={13} />}
                  {form.id ? '保存' : '添加并连接'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* JSON 导入 */}
      {importOpen && (
        <div className="fixed inset-0 z-50 flex items-end md:items-center justify-center bg-black/40 backdrop-blur-sm p-0 md:p-6">
          <div className="w-full md:max-w-xl max-h-[92vh] overflow-y-auto rounded-t-2xl md:rounded-2xl border border-line bg-panel shadow-2xl">
            <div className="sticky top-0 z-10 flex items-center justify-between px-4 py-3 bg-panel border-b border-line">
              <div>
                <div className="text-[14px] font-semibold">导入 JSON 配置</div>
                <div className="text-[11px] text-muted mt-0.5">
                  兼容 Claude Desktop / Cursor 的 mcpServers 写法；本地命令（stdio）会跳过。
                </div>
              </div>
              <button onClick={() => setImportOpen(false)} className="btn-ghost h-8 w-8 !px-0">
                <X size={15} />
              </button>
            </div>
            <div className="p-4 space-y-3">
              <textarea
                value={importText}
                onChange={(e) => setImportText(e.target.value)}
                rows={10}
                spellCheck={false}
                placeholder={EXAMPLE}
                className="field w-full font-mono text-[12px] leading-relaxed"
              />
              <div className="flex items-center gap-2">
                <button onClick={() => void doImport()} disabled={importing} className="btn-primary h-9 px-3.5 text-[12.5px]">
                  {importing ? <Loader2 size={13} className="animate-spin" /> : <Upload size={13} />}
                  解析并导入
                </button>
                <button onClick={() => setImportText(EXAMPLE)} className="btn-ghost h-9 px-3 text-[12px]">
                  填入示例
                </button>
              </div>

              {importResult && (
                <div className="rounded-xl border border-line bg-panel2/40 p-3 space-y-1.5">
                  {importResult.added.map((n) => (
                    <div key={n} className="text-[12px] text-emerald-600 dark:text-emerald-400 flex items-center gap-1.5">
                      <Check size={12} /> {n}
                    </div>
                  ))}
                  {importResult.skipped.map((s) => (
                    <div key={s.name} className="text-[12px] text-amber-600 dark:text-amber-400 flex items-start gap-1.5">
                      <AlertCircle size={12} className="mt-0.5 shrink-0" />
                      <span>{s.name}：{s.reason}</span>
                    </div>
                  ))}
                  {!importResult.added.length && !importResult.skipped.length && (
                    <div className="text-[12px] text-muted">没有解析到服务</div>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {debugFor && (
        <DebugPanel
          server={servers.find((s) => s.id === debugFor.id) ?? debugFor}
          onClose={() => setDebugFor(null)}
        />
      )}
    </>
  );
}
