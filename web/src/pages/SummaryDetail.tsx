import React, { Suspense, useCallback, useEffect, useRef, useState } from 'react';
import { Link, useLocation, useParams } from 'react-router-dom';
import {
  ArrowLeft, AlertCircle, Check, Loader2, Pencil, RefreshCw, Sparkles, ListTree, FileText, CalendarDays, Heart, Share2, Wand2,
} from 'lucide-react';
import { api, type Summary } from '../lib/api';
import MarkdownView from '../components/MarkdownView';
import ShareSummary from '../components/ShareSummary';
import SummaryMcpActions from '../components/SummaryMcpActions';
import AiReviseDialog from '../components/AiReviseDialog';
import { useToast } from '../lib/toast';
import { cn, fmtDurationCn } from '../lib/utils';

const MindMap = React.lazy(() => import('../components/MindMap'));

export default function SummaryDetailPage() {
  const { id } = useParams<{ id: string }>();
  const location = useLocation();
  /** 从回忆页点进来时带上来源地址（含日期/视图），返回时回到当时那一屏 */
  const backTo = (location.state as { from?: string } | null)?.from || '/calendar';
  const fromMemory = backTo.startsWith('/calendar');
  const toast = useToast();
  const [data, setData] = useState<Summary | null>(null);
  const [tab, setTab] = useState<'summary' | 'mindmap'>('summary');
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [draftTitle, setDraftTitle] = useState('');
  const [draftMarkdown, setDraftMarkdown] = useState('');
  const [shareOpen, setShareOpen] = useState(false);
  const [reviseOpen, setReviseOpen] = useState(false);
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
    const busy = data.status === 'queued' || data.status === 'generating' || data.status === 'waiting';
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

  const startEdit = () => {
    if (!data) return;
    setDraftTitle(data.title);
    setDraftMarkdown(data.markdown ?? '');
    setEditing(true);
  };

  const saveEdit = async () => {
    if (!data || saving) return;
    const title = draftTitle.trim();
    if (!title) {
      toast.push('标题不能为空', 'error');
      return;
    }
    setSaving(true);
    try {
      const next = await api<Summary>(`/api/summaries/${data.id}`, {
        method: 'PATCH',
        body: { title, markdown: draftMarkdown },
      });
      setData(next);
      setEditing(false);
      toast.push('已保存修改', 'success');
    } catch (e: any) {
      toast.push(e?.message ?? '保存失败', 'error');
    } finally {
      setSaving(false);
    }
  };

  /** 编辑器内的快捷键：⌘S 保存、Esc 取消 */
  const editKeys = (e: React.KeyboardEvent) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
      e.preventDefault();
      void saveEdit();
    } else if (e.key === 'Escape') {
      setEditing(false);
    }
  };

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


  if (loading) {
    return <div className="h-[60vh] grid place-items-center"><Loader2 size={22} className="animate-spin text-muted" /></div>;
  }
  if (!data) {
    return <div className="h-[60vh] grid place-items-center text-sm text-muted">纪要不存在</div>;
  }

  const busy = data.status === 'queued' || data.status === 'generating' || data.status === 'waiting';

  return (
    <div className="max-w-4xl mx-auto px-4 md:px-8 py-4 md:py-8">
      <Link to={backTo} className="inline-flex items-center gap-1.5 text-[13px] text-muted hover:text-ink transition-colors">
        <ArrowLeft size={15} />
        {fromMemory ? '返回回忆' : '返回'}
      </Link>

      <div className="mt-4 flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-1.5 text-[11px] text-brand font-medium mb-2">
            <CalendarDays size={11} />
            {data.scope === 'day' ? '每日回忆' : '时段回忆'} · {data.dateFrom}
            {data.dateTo !== data.dateFrom && ` - ${data.dateTo}`}
          </div>
          {editing ? (
            <input
              value={draftTitle}
              onChange={(e) => setDraftTitle(e.target.value)}
              onKeyDown={editKeys}
              maxLength={200}
              className="w-full bg-transparent text-[22px] md:text-[26px] font-semibold tracking-tight leading-snug outline-none border-b border-line pb-1 focus:border-brand"
            />
          ) : (
            <h1 className="text-[22px] md:text-[26px] font-semibold tracking-tight leading-snug">{data.title}</h1>
          )}
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
          {editing ? (
            <>
              <button
                onClick={() => setEditing(false)}
                disabled={saving}
                className="h-8 px-2.5 rounded-lg text-[12.5px] text-muted hover:bg-panel2 disabled:opacity-40"
              >
                取消
              </button>
              <button
                onClick={() => void saveEdit()}
                disabled={saving}
                className="h-8 px-3 rounded-lg bg-brand text-white text-[12.5px] inline-flex items-center gap-1.5 disabled:opacity-50"
              >
                {saving ? <Loader2 size={13} className="animate-spin" /> : <Check size={13} />}
                保存
              </button>
            </>
          ) : (
            <>
              <button
                onClick={startEdit}
                disabled={busy || data.status !== 'ready'}
                className="h-8 px-2.5 rounded-lg text-[12.5px] text-muted hover:bg-panel2 inline-flex items-center gap-1.5 disabled:opacity-40"
                title="修改标题与正文"
              >
                <Pencil size={13} />
                编辑
              </button>
              <button
                onClick={() => setShareOpen(true)}
                disabled={busy || data.status !== 'ready'}
                className="h-8 px-2.5 rounded-lg text-[12.5px] text-muted hover:bg-panel2 inline-flex items-center gap-1.5 disabled:opacity-40"
                title="分享（Markdown 或长图）"
              >
                <Share2 size={13} />
                分享
              </button>
              <button
                onClick={() => setReviseOpen(true)}
                disabled={busy || data.status !== 'ready'}
                className="h-8 px-2.5 rounded-lg text-[12.5px] text-muted hover:bg-panel2 inline-flex items-center gap-1.5 disabled:opacity-40"
                title="按你的要求让 AI 修改这份纪要"
              >
                <Wand2 size={13} />
                AI 修改
              </button>
              <button
                onClick={regenerate}
                disabled={busy}
                className="h-8 w-8 grid place-items-center rounded-lg text-muted hover:bg-panel2 disabled:opacity-40"
                title="重新生成（会覆盖手工修改）"
              >
                <RefreshCw size={15} className={cn(busy && 'animate-spin')} />
              </button>
            </>
          )}
        </div>
      </div>

      {editing && (
        <div className="mt-6">
          <div className="card overflow-hidden">
            <div className="px-4 h-10 flex items-center justify-between border-b border-line bg-soft/40">
              <span className="text-[11.5px] text-muted">Markdown 原文 · ⌘S 保存 · Esc 取消</span>
              <span className="text-[11px] text-muted/70 tabular-nums">{draftMarkdown.length} 字符</span>
            </div>
            <textarea
              value={draftMarkdown}
              onChange={(e) => setDraftMarkdown(e.target.value)}
              onKeyDown={editKeys}
              spellCheck={false}
              placeholder="正文为空，可以直接写…"
              className="w-full min-h-[52vh] p-4 md:p-5 bg-transparent text-[13px] leading-relaxed font-mono outline-none resize-y"
            />
          </div>
          <div className="mt-2 text-[11.5px] text-muted leading-relaxed">
            保存只改这份纪要的文字，不会重新调用模型；点「重新生成」会覆盖手工修改。
          </div>
        </div>
      )}

      {!editing && (
        <>
      {data.scope !== 'recording' && data.dateFrom !== data.dateTo && (
        <div className="mt-5 text-[11.5px] text-muted leading-relaxed">
          这份回忆由下级回忆（日报 / 月报）逐层汇总，不再重新读原始转写。修改或重新生成下级后，需要再重新整理本份才会同步。
        </div>
      )}
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
            <div className="text-sm text-muted">
              {data.status === 'waiting' ? '正在准备下级回忆…' : '正在整理…'}
            </div>
            {data.status === 'waiting' && (
              <div className="text-[11.5px] text-muted/80 text-center leading-relaxed max-w-sm">
                先把缺的日报 / 月报补齐，再用它们逐层汇总。首次需要等一会儿，之后会快很多。
              </div>
            )}
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
            {data.stats?.moods && data.stats.moods.length > 0 && (
              <div className="card p-4 bg-soft/50">
                <div className="text-[11px] font-medium text-brand mb-2.5 flex items-center gap-1.5">
                  <Heart size={11} /> 人物与心情
                </div>
                <ul className="space-y-2">
                  {data.stats.moods.map((m, i) => (
                    <li key={i} className="text-[13px] leading-relaxed flex gap-2">
                      <span className="text-brand/50 shrink-0">·</span>
                      <span>
                        <span className="font-medium">{m.person}</span>
                        <span className="text-muted"> — {m.mood}</span>
                        {m.evidence && <span className="text-muted/70"> （{m.evidence}）</span>}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            <div className="card p-5 md:p-7">
              <MarkdownView markdown={data.markdown ?? ''} />
            </div>
            <SummaryMcpActions summaryId={data.id} />
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
        </>
      )}
      {shareOpen && (
        <ShareSummary summary={data} onClose={() => setShareOpen(false)} />
      )}
      {reviseOpen && (
        <AiReviseDialog
          summary={data}
          onClose={() => setReviseOpen(false)}
          onRevised={(next) => setData(next)}
        />
      )}
    </div>
  );
}
