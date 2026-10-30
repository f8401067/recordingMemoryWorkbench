import React, { Suspense, useCallback, useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import {
  ArrowLeft, AlertCircle, Download, Loader2, RefreshCw, Sparkles, ListTree, FileText, CalendarDays,
} from 'lucide-react';
import { api, type Summary } from '../lib/api';
import MarkdownView from '../components/MarkdownView';
import { useToast } from '../lib/toast';
import { cn, fmtDurationCn } from '../lib/utils';

const MindMap = React.lazy(() => import('../components/MindMap'));

export default function SummaryDetailPage() {
  const { id } = useParams<{ id: string }>();
  const toast = useToast();
  const [data, setData] = useState<Summary | null>(null);
  const [tab, setTab] = useState<'summary' | 'mindmap'>('summary');
  const [loading, setLoading] = useState(true);
  const pollRef = useRef<number | null>(null);

  const load = useCallback(async (silent = false) => {
    if (!id) return;
    try {
      const s = await api<Summary>(`/api/summaries/${id}`);
      setData(s);
    } catch (e: any) {
      if (!silent) toast.push(e?.message ?? '加载失败', 'error');
    } finally {
      setLoading(false);
    }
  }, [id, toast]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!data) return;
    const busy = data.status === 'queued' || data.status === 'generating';
    if (!busy) {
      if (pollRef.current) { window.clearInterval(pollRef.current); pollRef.current = null; }
      return;
    }
    if (pollRef.current) return;
    pollRef.current = window.setInterval(() => void load(true), 2200);
    return () => {
      if (pollRef.current) { window.clearInterval(pollRef.current); pollRef.current = null; }
    };
  }, [data, load]);

  const regenerate = async () => {
    if (!data) return;
    try {
      if (data.scope === 'day') {
        setData({ ...data, status: 'queued' });
        await api('/api/summaries/day', { method: 'POST', body: { date: data.dateFrom, force: true } });
      } else if (data.scope === 'range') {
        setData({ ...data, status: 'queued' });
        await api('/api/summaries/range', { method: 'POST', body: { from: data.dateFrom, to: data.dateTo, force: true } });
      } else {
        await api(`/api/summaries/${data.id}`, { method: 'DELETE' });
        toast.push('请在对应录音页面重新生成', 'info');
        return;
      }
      toast.push('已提交重新生成', 'success');
      void load(true);
    } catch (e: any) {
      toast.push(e?.message ?? '操作失败', 'error');
    }
  };

  const exportMd = () => {
    if (!data) return;
    const md = `# ${data.title}\n\n> ${data.dateFrom}${data.dateTo !== data.dateFrom ? ` - ${data.dateTo}` : ''}\n\n${data.markdown ?? ''}\n\n---\n\n## 思维导图大纲\n\n\`\`\`markdown\n${data.mindmap ?? ''}\n\`\`\`\n`;
    const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${data.title}.md`;
    a.click();
    URL.revokeObjectURL(url);
  };

  if (loading) {
    return <div className="h-[60vh] grid place-items-center"><Loader2 size={22} className="animate-spin text-muted" /></div>;
  }
  if (!data) {
    return <div className="h-[60vh] grid place-items-center text-sm text-muted">纪要不存在</div>;
  }

  const busy = data.status === 'queued' || data.status === 'generating';

  return (
    <div className="max-w-4xl mx-auto px-4 md:px-8 py-4 md:py-8">
      <Link to="/calendar" className="inline-flex items-center gap-1.5 text-[13px] text-muted hover:text-ink transition-colors">
        <ArrowLeft size={15} />
        返回回忆
      </Link>

      <div className="mt-4 flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-1.5 text-[11px] text-brand font-medium mb-2">
            <CalendarDays size={11} />
            {data.scope === 'day' ? '每日回忆' : '时段回忆'} · {data.dateFrom}
            {data.dateTo !== data.dateFrom && ` - ${data.dateTo}`}
          </div>
          <h1 className="text-[22px] md:text-[26px] font-semibold tracking-tight leading-snug">{data.title}</h1>
          <div className="mt-2 flex items-center gap-2 text-[12px] text-muted flex-wrap">
            {data.stats?.recordings !== undefined && <span>{data.stats.recordings} 段录音</span>}
            {data.stats?.durationMs !== undefined && (
              <>
                <span className="opacity-40">·</span>
                <span>{fmtDurationCn(data.stats.durationMs)}</span>
              </>
            )}
            {data.stats?.chars !== undefined && (
              <>
                <span className="opacity-40">·</span>
                <span>{(data.stats.chars / 1000).toFixed(1)} 千字</span>
              </>
            )}
          </div>
        </div>

        <div className="flex items-center gap-1.5 shrink-0">
          <button onClick={exportMd} className="h-8 w-8 grid place-items-center rounded-lg text-muted hover:bg-panel2" title="导出">
            <Download size={15} />
          </button>
          <button
            onClick={regenerate}
            disabled={busy}
            className="h-8 w-8 grid place-items-center rounded-lg text-muted hover:bg-panel2 disabled:opacity-40"
            title="重新生成"
          >
            <RefreshCw size={15} className={cn(busy && 'animate-spin')} />
          </button>
        </div>
      </div>

      <div className="mt-6 flex items-center gap-1 border-b border-line">
        {[
          { key: 'summary' as const, label: '纪要', icon: FileText },
          { key: 'mindmap' as const, label: '思维导图', icon: ListTree },
        ].map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={cn(
              'relative flex items-center gap-1.5 px-3.5 h-10 text-[13px] font-medium transition-colors',
              tab === t.key ? 'text-brand' : 'text-muted hover:text-ink',
            )}
          >
            <t.icon size={14} />
            {t.label}
            {tab === t.key && <span className="absolute inset-x-2 -bottom-px h-[2px] rounded-full bg-brand" />}
          </button>
        ))}
      </div>

      <div className="mt-5">
        {busy && (
          <div className="card p-8 flex flex-col items-center gap-3">
            <Loader2 size={20} className="animate-spin text-brand" />
            <div className="text-sm text-muted">正在整理…</div>
          </div>
        )}

        {data.status === 'failed' && (
          <div className="card p-6 flex items-start gap-3">
            <AlertCircle size={18} className="text-red-500 mt-0.5" />
            <div>
              <div className="text-sm font-medium">生成失败</div>
              <div className="text-[12px] text-muted mt-1">{data.error}</div>
            </div>
          </div>
        )}

        {data.status === 'ready' && tab === 'summary' && (
          <div className="space-y-4 animate-fade-up">
            {data.stats?.highlights && data.stats.highlights.length > 0 && (
              <div className="card p-4 bg-soft/50">
                <div className="text-[11px] font-medium text-brand mb-2 flex items-center gap-1.5">
                  <Sparkles size={11} /> 关键信息
                </div>
                <ul className="space-y-1.5">
                  {data.stats.highlights.map((h, i) => (
                    <li key={i} className="text-[13px] leading-relaxed flex gap-2">
                      <span className="text-brand/50 shrink-0">·</span>
                      <span>{h}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            <div className="card p-5 md:p-7">
              <MarkdownView markdown={data.markdown ?? ''} />
            </div>
          </div>
        )}

        {data.status === 'ready' && tab === 'mindmap' && (
          <div className="animate-fade-up">
            <Suspense
              fallback={
                <div className="h-[62vh] rounded-2xl border border-line bg-panel grid place-items-center">
                  <Loader2 size={18} className="animate-spin text-muted" />
                </div>
              }
            >
              <MindMap markdown={data.mindmap ?? ''} />
            </Suspense>
          </div>
        )}
      </div>
    </div>
  );
}
