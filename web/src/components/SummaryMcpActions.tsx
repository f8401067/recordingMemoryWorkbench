import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { Check, Loader2, Plug, RefreshCw, Send, Sparkles, X } from 'lucide-react';
import {
  listMcpSummaryTools, previewMcpSummaryTool, runMcpSummaryTool, suggestMcpToolArgs,
  type McpSummaryTool,
} from '../lib/api';
import { useToast } from '../lib/toast';
import { cn } from '../lib/utils';

type Props = {
  /** 纪要 id：正文由服务端按 id 读取，不把整篇 markdown 传来传去 */
  summaryId: string;
  /** 日历页的内联预览用紧凑样式 */
  compact?: boolean;
  className?: string;
};

type LastRun = {
  ok: boolean;
  label: string;
  message: string;
};

/**
 * 每条 AI 纪要下方的「一键发送」工具条。
 *
 * 添加 / 刷新 MCP 服务时，后端已经让大模型按工具 schema 生成好建议参数并存库；
 * 点工具后先弹出一版「建议参数 + 纪要内容」合并出的参数预览，用户可以改、
 * 可以让大模型重新生成，确认后才真正发送。
 */
export default function SummaryMcpActions({ summaryId, compact = false, className }: Props) {
  const toast = useToast();
  const [tools, setTools] = useState<McpSummaryTool[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [running, setRunning] = useState<string | null>(null);
  const [last, setLast] = useState<LastRun | null>(null);

  // 参数预览弹窗
  const [previewFor, setPreviewFor] = useState<McpSummaryTool | null>(null);
  const [previewArgs, setPreviewArgs] = useState<string>('');
  const [previewNote, setPreviewNote] = useState('');
  const [previewBusy, setPreviewBusy] = useState(false);
  const [suggesting, setSuggesting] = useState(false);

  const load = useCallback(async () => {
    try {
      const res = await listMcpSummaryTools();
      setTools(res.tools ?? []);
    } catch {
      // 没配置 MCP / 接口异常都不该打扰阅读纪要
      setTools([]);
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    setLast(null);
    void load();
  }, [load, summaryId]);

  /** 点工具：先拉服务端合并好的参数预览（建议参数 + 纪要正文），可改可发 */
  const openPreview = useCallback(async (tool: McpSummaryTool) => {
    setPreviewFor(tool);
    setPreviewBusy(true);
    setPreviewNote('');
    setPreviewArgs('');
    try {
      const res = await previewMcpSummaryTool({ summaryId, serverId: tool.serverId, tool: tool.name });
      setPreviewArgs(JSON.stringify(res.args, null, 2));
      setPreviewNote(res.note);
    } catch (e: any) {
      setPreviewNote(e?.message ?? '生成参数预览失败');
    } finally {
      setPreviewBusy(false);
    }
  }, [summaryId]);

  const closePreview = useCallback(() => {
    setPreviewFor(null);
    setSuggesting(false);
  }, []);

  /** 大模型按工具 schema 重新生成一份建议参数（不带纪要内容，纯 schema 视角） */
  const resuggest = useCallback(async () => {
    if (!previewFor) return;
    setSuggesting(true);
    try {
      const res = await suggestMcpToolArgs(previewFor.serverId, previewFor.name);
      setPreviewArgs(JSON.stringify(res.args, null, 2));
      setPreviewNote(res.note);
    } catch (e: any) {
      toast.push(e?.message ?? '重新生成参数失败', 'error');
    } finally {
      setSuggesting(false);
    }
  }, [previewFor, toast]);

  const confirmSend = useCallback(async () => {
    if (!previewFor) return;
    let parsed: Record<string, unknown>;
    try {
      parsed = previewArgs.trim() ? JSON.parse(previewArgs) : {};
    } catch {
      toast.push('参数不是合法 JSON', 'error');
      return;
    }
    setRunning(previewFor.key);
    setLast(null);
    const label = `${previewFor.serverName} · ${previewFor.title || previewFor.name}`;
    try {
      const res = await runMcpSummaryTool({
        summaryId,
        serverId: previewFor.serverId,
        tool: previewFor.name,
        args: parsed,
      });
      const first = res.results?.[0];
      const ok = !!res.ok;
      const message = first?.message || res.message || (ok ? '已发送' : '工具没有返回结果');
      setLast({ ok, label, message });
      toast.push(
        ok ? `已发送到 ${label}` : `发送失败：${message}`,
        ok ? 'success' : 'error',
      );
      if (ok) closePreview();
    } catch (e: any) {
      const message = e?.message ?? '发送失败';
      setLast({ ok: false, label, message });
      toast.push(message, 'error');
    } finally {
      setRunning(null);
    }
  }, [previewFor, previewArgs, summaryId, toast, closePreview]);

  // 弹窗里按 schema 渲染可编辑字段（string / number / boolean / array / object）
  const previewFields = useMemo(() => {
    if (!previewFor) return [];
    const props = (previewFor.schema as any)?.properties ?? {};
    return Object.entries<any>(props).map(([key, spec]) => ({
      key,
      type: spec?.type ?? 'string',
      required: (previewFor as any).schema?.required?.includes(key) ?? false,
      enum: spec?.enum,
    }));
  }, [previewFor]);

  if (!loaded) return null;

  // 没配 MCP：给一条安静的入口，别在每份纪要下面堆空卡片
  if (tools.length === 0) {
    return (
      <Link
        to="/settings?tab=mcp"
        className={cn(
          'inline-flex items-center gap-1.5 text-[11px] text-muted/70 hover:text-brand transition-colors',
          className,
        )}
        title="在设置里添加 MCP 服务，纪要下方就会出现一键发送按钮"
      >
        <Plug size={11} /> 添加 MCP 工具，一键发送纪要
      </Link>
    );
  }

  return (
    <>
      <div
        className={cn(
          'rounded-xl border border-line bg-panel2/30',
          compact ? 'px-3 py-2.5' : 'px-3.5 py-3',
          className,
        )}
      >
        <div className="flex items-center gap-2 flex-wrap">
          <span className="inline-flex items-center gap-1.5 text-[11.5px] text-muted shrink-0">
            <Send size={11} className="text-brand" />
            发送到 MCP
          </span>
          <span className="text-[10.5px] text-muted/70">
            已识别 {tools.length} 个可接收纪要的工具 · 参数由大模型建议、发送前可改
          </span>
          <div className="flex-1" />
          <button
            onClick={async () => {
              setRefreshing(true);
              await load();
              setRefreshing(false);
            }}
            disabled={refreshing || !!running}
            className="btn-ghost h-6 w-6 !px-0 text-muted"
            title="重新读取可用的 MCP 工具（在设置里改过配置后点这里）"
          >
            <RefreshCw size={11} className={cn(refreshing && 'animate-spin')} />
          </button>
          <Link
            to="/settings?tab=mcp"
            className="btn-ghost h-6 w-6 !px-0 text-muted grid place-items-center"
            title="管理 MCP 工具"
          >
            <Plug size={11} />
          </Link>
        </div>

        <div className="mt-2 flex items-center gap-1.5 flex-wrap">
          {tools.map((t) => {
            const busy = running === t.key;
            const label = t.title || t.name;
            return (
              <button
                key={t.key}
                onClick={() => void openPreview(t)}
                disabled={!!running}
                title={[
                  `${t.serverName} · ${t.name}`,
                  t.description,
                  `点开后先看大模型建议的参数，可改再发`,
                ]
                  .filter(Boolean)
                  .join('\n')}
                className={cn(
                  'inline-flex items-center gap-1.5 rounded-lg border border-line bg-panel px-2.5 h-8',
                  'text-[11.5px] text-ink hover:border-brand/60 hover:text-brand transition-colors',
                  'disabled:opacity-50 disabled:cursor-not-allowed',
                  compact && 'h-7 px-2 text-[11px]',
                )}
              >
                {busy ? <Loader2 size={12} className="animate-spin" /> : <Send size={11} className="text-brand" />}
                <span className="text-muted">{t.serverName}</span>
                <span className="text-muted/50">/</span>
                <span className="font-medium">{label}</span>
              </button>
            );
          })}
        </div>

        {last && (
          <div
            className={cn(
              'mt-2 flex items-start gap-1.5 text-[11px] leading-relaxed',
              last.ok ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600 dark:text-red-400',
            )}
          >
            {last.ok ? <Check size={11} className="mt-0.5 shrink-0" /> : <X size={11} className="mt-0.5 shrink-0" />}
            <span className="min-w-0 break-words">
              {last.ok ? '已发送：' : '发送失败：'}
              {last.label}
              {last.message && <span className="text-muted"> · {last.message}</span>}
            </span>
          </div>
        )}
      </div>

      {/* 参数预览弹窗：建议参数 + 纪要内容合并结果，可编辑、可重新生成、确认后发送 */}
      {previewFor && (
        <div className="fixed inset-0 z-50 flex items-end md:items-center justify-center bg-black/40 backdrop-blur-sm p-0 md:p-6">
          <div className="w-full md:max-w-xl max-h-[92vh] overflow-y-auto rounded-t-2xl md:rounded-2xl border border-line bg-panel shadow-2xl">
            <div className="sticky top-0 z-10 flex items-center justify-between gap-3 px-4 py-3 bg-panel border-b border-line">
              <div className="min-w-0">
                <div className="text-[14px] font-semibold flex items-center gap-1.5">
                  <Send size={14} className="text-brand" />
                  发送前确认 · {previewFor.title || previewFor.name}
                </div>
                <div className="text-[11px] text-muted truncate">{previewFor.serverName}</div>
              </div>
              <button onClick={closePreview} className="btn-ghost h-8 w-8 !px-0 shrink-0">
                <X size={15} />
              </button>
            </div>

            <div className="p-4 space-y-3">
              {previewBusy ? (
                <div className="flex items-center gap-2 text-[12px] text-muted py-6 justify-center">
                  <Loader2 size={14} className="animate-spin" /> 大模型按工具 schema 生成参数中…
                </div>
              ) : (
                <>
                  {previewNote && (
                    <p className="text-[11px] text-muted leading-relaxed">{previewNote}</p>
                  )}
                  <div>
                    <div className="flex items-center justify-between">
                      <label className="text-[11.5px] text-muted">参数（JSON，可直接改）</label>
                      <button
                        onClick={() => void resuggest()}
                        disabled={suggesting}
                        className="text-[11px] text-brand hover:underline inline-flex items-center gap-0.5 disabled:opacity-50"
                        title="让大模型按工具 schema 重新生成一份建议参数"
                      >
                        {suggesting ? <Loader2 size={10} className="animate-spin" /> : <Sparkles size={10} />}
                        AI 重新生成
                      </button>
                    </div>
                    <textarea
                      value={previewArgs}
                      onChange={(e) => setPreviewArgs(e.target.value)}
                      rows={Math.min(10, Math.max(4, previewArgs.split('\n').length))}
                      spellCheck={false}
                      className="field w-full mt-1 font-mono text-[11.5px] leading-relaxed"
                    />
                    {previewFields.length > 0 && (
                      <p className="mt-1 text-[10px] text-muted/70">
                        schema 字段：{previewFields.map((f) => `${f.key}${f.required ? '*' : ''}`).join('、')}
                      </p>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => void confirmSend()}
                      disabled={running === previewFor.key}
                      className="btn-primary h-9 px-3 text-[12.5px] inline-flex items-center gap-1.5"
                    >
                      {running === previewFor.key ? <Loader2 size={13} className="animate-spin" /> : <Send size={13} />}
                      确认发送
                    </button>
                    <span className="text-[10.5px] text-muted/70">
                      只提交 schema 里声明的字段；纪要正文会进识别出的正文字段
                    </span>
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
