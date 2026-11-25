import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  ArrowDownToLine, Copy, Loader2, RefreshCw, Search, Trash2,
} from 'lucide-react';
import { clearLogs, getLogs, type LogEntry } from '../../lib/api';
import { useToast } from '../../lib/toast';
import { cn, copyText, download } from '../../lib/utils';
import Section, { Switch } from './Section';

const LEVELS: { key: 'all' | 'debug' | 'info' | 'warn' | 'error'; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'info', label: '信息' },
  { key: 'warn', label: '警告' },
  { key: 'error', label: '错误' },
  { key: 'debug', label: '调试' },
];

const LEVEL_STYLE: Record<string, string> = {
  error: 'bg-red-500/10 text-red-500',
  warn: 'bg-amber-500/10 text-amber-600 dark:text-amber-400',
  info: 'bg-brand/10 text-brand',
  debug: 'bg-panel2 text-muted',
};

function fmtLine(e: LogEntry) {
  const time = e.ts.slice(11, 23);
  return `${time}  ${e.level.toUpperCase().padEnd(5)} [${e.source}] ${e.message}`;
}

export default function LogsTab() {
  const toast = useToast();
  const [level, setLevel] = useState<'all' | 'debug' | 'info' | 'warn' | 'error'>('all');
  const [query, setQuery] = useState('');
  const [debounced, setDebounced] = useState('');
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [auto, setAuto] = useState(true);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  const cursor = useRef(0);
  const boxRef = useRef<HTMLDivElement>(null);
  const stickBottom = useRef(true);

  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(query.trim()), 300);
    return () => window.clearTimeout(t);
  }, [query]);

  const load = useCallback(
    async (reset: boolean) => {
      setLoading(true);
      try {
        const page = await getLogs({
          limit: reset ? 400 : 300,
          level,
          q: debounced || undefined,
          after: reset ? undefined : cursor.current || undefined,
        });
        setTotal(page.total);
        setErr(null);
        setEntries((prev) => {
          if (reset) return page.entries;
          if (!page.entries.length) return prev;
          const merged = [...prev, ...page.entries];
          return merged.length > 1200 ? merged.slice(-1200) : merged;
        });
        const last = page.entries[page.entries.length - 1];
        if (last) cursor.current = last.id;
      } catch (e: any) {
        setErr(e?.message ?? '读取日志失败');
      } finally {
        setLoading(false);
      }
    },
    [level, debounced],
  );

  // 切换筛选 / 关键字：从头拉一份
  useEffect(() => {
    cursor.current = 0;
    stickBottom.current = true;
    void load(true);
  }, [load]);

  // 自动刷新：只取游标之后的增量
  useEffect(() => {
    if (!auto) return;
    const t = window.setInterval(() => {
      if (!document.hidden) void load(false);
    }, 2000);
    return () => window.clearInterval(t);
  }, [auto, load]);

  useEffect(() => {
    const box = boxRef.current;
    if (box && stickBottom.current) box.scrollTop = box.scrollHeight;
  }, [entries]);

  const onScroll = () => {
    const box = boxRef.current;
    if (!box) return;
    stickBottom.current = box.scrollTop + box.clientHeight >= box.scrollHeight - 48;
  };

  const text = () => entries.map(fmtLine).join('\n');

  const doClear = async () => {
    if (!window.confirm('清空服务端的内存日志？只影响日志显示，不影响数据。')) return;
    try {
      await clearLogs();
      cursor.current = 0;
      setEntries([]);
      setTotal(0);
      toast.push('已清空', 'success');
    } catch (e: any) {
      toast.push(e?.message ?? '清空失败', 'error');
    }
  };

  const errorCount = entries.filter((e) => e.level === 'error').length;

  return (
    <div className="space-y-3">
      <Section
        title="运行日志"
        desc="后端实时输出（print、异常栈、任务事件、uvicorn）。只在内存里保留最近 4000 条，重启即清空。"
        icon={ArrowDownToLine}
        action={
          <div className="flex items-center gap-1.5 shrink-0">
            <span className="text-[11px] text-muted hidden sm:inline">自动刷新</span>
            <Switch checked={auto} onChange={setAuto} />
            <button
              onClick={() => {
                cursor.current = 0;
                void load(true);
              }}
              className="btn-ghost h-8 w-8 !px-0 justify-center"
              title="刷新"
            >
              <RefreshCw size={12} className={cn(loading && 'animate-spin')} />
            </button>
          </div>
        }
      >
        <div className="flex items-center gap-2 flex-wrap">
          <div className="flex rounded-lg bg-panel2 p-0.5">
            {LEVELS.map((l) => (
              <button
                key={l.key}
                onClick={() => setLevel(l.key)}
                className={cn(
                  'px-2 h-7 rounded-md text-[11.5px] transition-all',
                  level === l.key ? 'bg-panel text-ink shadow-sm' : 'text-muted hover:text-ink',
                )}
              >
                {l.label}
              </button>
            ))}
          </div>

          <div className="relative flex-1 min-w-[160px]">
            <Search size={12} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="搜关键字 / 来源，例如 summarize、失败"
              className="field h-8 !pl-8 text-[12px]"
            />
          </div>

          <div className="flex items-center gap-1 ml-auto">
            <button
              onClick={() => {
                void copyText(text());
                toast.push(`已复制 ${entries.length} 行`, 'success');
              }}
              disabled={!entries.length}
              className="h-8 px-2 rounded-lg text-[11.5px] text-muted hover:text-ink hover:bg-panel2 inline-flex items-center gap-1 disabled:opacity-40"
            >
              <Copy size={11} /> 复制
            </button>
            <button
              onClick={() =>
                download(`recall-log-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '')}.log`, text(), 'text/plain')
              }
              disabled={!entries.length}
              className="h-8 px-2 rounded-lg text-[11.5px] text-muted hover:text-ink hover:bg-panel2 inline-flex items-center gap-1 disabled:opacity-40"
            >
              <ArrowDownToLine size={11} /> 下载
            </button>
            <button
              onClick={() => void doClear()}
              className="h-8 px-2 rounded-lg text-[11.5px] text-muted hover:text-red-500 hover:bg-red-500/10 inline-flex items-center gap-1"
            >
              <Trash2 size={11} /> 清空
            </button>
          </div>
        </div>

        <div className="mt-2 flex items-center gap-2 text-[10.5px] text-muted">
          <span className="tabular-nums">
            显示 {entries.length} 行 / 缓冲共 {total}
          </span>
          {errorCount > 0 && <span className="text-red-500">其中 {errorCount} 条错误</span>}
          {auto ? (
            <span className="inline-flex items-center gap-1 text-brand">
              <span className="h-1.5 w-1.5 rounded-full bg-brand animate-pulse" /> 实时
            </span>
          ) : (
            <span className="text-amber-500">已暂停（手动刷新）</span>
          )}
          {err && <span className="text-red-500">{err}</span>}
          {loading && !entries.length && <Loader2 size={10} className="animate-spin" />}
        </div>

        <div
          ref={boxRef}
          onScroll={onScroll}
          className="mt-2 h-[56vh] min-h-[280px] overflow-auto rounded-xl border border-line bg-panel2/40 p-2 font-mono text-[11.5px] leading-[1.7]"
        >
          {entries.length === 0 ? (
            <div className="h-full grid place-items-center text-[12px] text-muted font-sans">
              {loading ? '读取中…' : '没有匹配的日志'}
            </div>
          ) : (
            entries.map((e) => (
              <div
                key={e.id}
                className={cn(
                  'flex gap-2 px-1 py-[1px] rounded',
                  e.level === 'error' && 'bg-red-500/[0.06]',
                )}
              >
                <span className="text-muted/60 tabular-nums shrink-0 select-none">{e.ts.slice(11, 23)}</span>
                <span
                  className={cn(
                    'shrink-0 w-[38px] text-center rounded text-[9.5px] leading-[15px] mt-[2px] font-sans font-medium',
                    LEVEL_STYLE[e.level] ?? LEVEL_STYLE.info,
                  )}
                >
                  {e.level === 'warn' ? 'WARN' : e.level.toUpperCase()}
                </span>
                <span className="shrink-0 text-brand/70 max-w-[110px] truncate" title={e.source}>
                  {e.source}
                </span>
                <span className="whitespace-pre-wrap break-words text-ink/85 min-w-0">{e.message}</span>
              </div>
            ))
          )}
        </div>

        <div className="mt-1.5 text-[10.5px] text-muted/70">
          日志不落盘（避免无限增长）。需要长期留存时点「下载」保存到本地。
        </div>
      </Section>
    </div>
  );
}
