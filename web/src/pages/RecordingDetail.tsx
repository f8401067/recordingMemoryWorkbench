import React, { Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import {
  ArrowLeft, Check, Copy, Download, FileText, Headphones, ListTree, Loader2, MoreHorizontal,
  RefreshCw, Sparkles, Trash2, AlertCircle, RotateCw, Clock,
} from 'lucide-react';
import { api, subscribeRecording, type RecordingDetail } from '../lib/api';
import MarkdownView from '../components/MarkdownView';
import AudioPlayer, { type AudioHandle } from '../components/AudioPlayer';
import { useToast } from '../lib/toast';
import {
  cn, copyText, fmtDate, fmtDuration, fmtDurationCn, fmtTime, statusText,
} from '../lib/utils';

const MindMap = React.lazy(() => import('../components/MindMap'));

type Tab = 'summary' | 'mindmap' | 'transcript';

export default function RecordingDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const toast = useToast();

  const [data, setData] = useState<RecordingDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<Tab>('summary');
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState('');
  const [menuOpen, setMenuOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [liveChunks, setLiveChunks] = useState<Record<number, string>>({});
  const pollRef = useRef<number | null>(null);

  // 播放位置（全局毫秒），用于文字实时定位
  const [currentMs, setCurrentMs] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [follow, setFollow] = useState(true);
  const playerRef = useRef<AudioHandle>(null);
  const segRefs = useRef<(HTMLDivElement | null)[]>([]);

  const load = useCallback(async (silent = false) => {
    if (!id) return;
    try {
      const d = await api<RecordingDetail>(`/api/recordings/${id}`);
      setData(d);
      setTitleDraft(d.title);
      return d;
    } catch (e: any) {
      if (!silent) toast.push(e?.message ?? '加载失败', 'error');
    } finally {
      setLoading(false);
    }
  }, [id, toast]);

  useEffect(() => {
    void load();
  }, [load]);

  // 处理中时轮询
  useEffect(() => {
    if (!data) return;
    const busyState =
      data.status === 'processing' ||
      data.status === 'recording' ||
      data.summaryStatus === 'queued' ||
      data.summaryStatus === 'generating';
    if (!busyState) {
      if (pollRef.current) window.clearInterval(pollRef.current);
      return;
    }
    if (pollRef.current) return;
    pollRef.current = window.setInterval(() => void load(true), 2200);
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
      pollRef.current = null;
    };
  }, [data, load]);

  // SSE 实时片段
  useEffect(() => {
    if (!id) return;
    const unsub = subscribeRecording(id, (ev, payload) => {
      if (ev === 'chunk' && payload?.seq !== undefined) {
        setLiveChunks((m) => ({ ...m, [payload.seq]: payload.status }));
      }
      if (ev === 'summary' || ev === 'recording') void load(true);
    });
    return unsub;
  }, [id, load]);

  // ---------------- 音频 / 文字联动 ----------------
  const segments = data?.transcript.segments ?? [];

  // 播放位置落在第几句上：找到最后一个 startMs <= 当前进度的句子
  const activeIndex = useMemo(() => {
    if (!segments.length) return -1;
    let idx = -1;
    for (let i = 0; i < segments.length; i += 1) {
      const start = segments[i].startMs ?? 0;
      if (start > currentMs) break;
      idx = i;
    }
    return idx;
  }, [segments, currentMs]);

  const audioChunks = useMemo(
    () =>
      (data?.chunks ?? []).map((c) => ({
        seq: c.seq,
        startMs: c.startOffsetMs ?? 0,
        durationMs: c.durationMs ?? 0,
      })),
    [data?.chunks],
  );

  // 播放到新的一句时滚到可视区，暂停或手动关掉跟随就不打扰
  useEffect(() => {
    if (!playing || !follow || tab !== 'transcript' || activeIndex < 0) return;
    segRefs.current[activeIndex]?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }, [activeIndex, playing, follow, tab]);

  const jumpTo = (ms: number) => {
    playerRef.current?.seek(ms);
  };

  const saveTitle = async () => {
    if (!data || !titleDraft.trim() || titleDraft === data.title) {
      setEditingTitle(false);
      return;
    }
    try {
      await api(`/api/recordings/${data.id}`, { method: 'PATCH', body: { title: titleDraft.trim() } });
      setEditingTitle(false);
      await load(true);
      toast.push('标题已更新', 'success');
    } catch (e: any) {
      toast.push(e?.message ?? '保存失败', 'error');
    }
  };

  const genSummary = async (force = false) => {
    if (!data) return;
    setBusy('summary');
    try {
      await api(`/api/recordings/${data.id}/summarize`, { method: 'POST', body: { force } });
      toast.push('已提交生成任务', 'success');
      setTab('summary');
      await load(true);
    } catch (e: any) {
      toast.push(e?.message ?? '生成失败', 'error');
    } finally {
      setBusy(null);
    }
  };

  const retry = async () => {
    if (!data) return;
    setBusy('retry');
    try {
      const r = await api<{ requeued: number }>(`/api/recordings/${data.id}/retry`, { method: 'POST' });
      toast.push(`已重新排队 ${r.requeued} 个片段`, 'success');
      await load(true);
    } catch (e: any) {
      toast.push(e?.message ?? '重试失败', 'error');
    } finally {
      setBusy(null);
    }
  };

  const remove = async () => {
    if (!data) return;
    if (!confirm('删除后无法恢复，音频与文字都会被清除。确定删除吗？')) return;
    try {
      await api(`/api/recordings/${data.id}`, { method: 'DELETE' });
      toast.push('已删除', 'success');
      navigate('/', { replace: true });
    } catch (e: any) {
      toast.push(e?.message ?? '删除失败', 'error');
    }
  };

  const exportMd = async () => {
    if (!data) return;
    try {
      const token = localStorage.getItem('recall.token');
      const res = await fetch(`/api/recordings/${data.id}/export?format=md`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      const text = await res.text();
      const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${data.title}.md`;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      toast.push('导出失败', 'error');
    }
  };

  if (loading) {
    return (
      <div className="h-[60vh] grid place-items-center">
        <Loader2 size={22} className="animate-spin text-muted" />
      </div>
    );
  }
  if (!data) {
    return (
      <div className="h-[60vh] grid place-items-center text-sm text-muted">记录不存在或已被删除</div>
    );
  }

  const summary = data.summary;
  const summaryReady = summary?.status === 'ready';
  const summaryBusy = summary?.status === 'generating' || summary?.status === 'queued';
  const processing = data.status === 'processing' || data.status === 'recording';

  const tabs: { key: Tab; label: string; icon: any }[] = [
    { key: 'summary', label: 'AI 纪要', icon: Sparkles },
    { key: 'mindmap', label: '思维导图', icon: ListTree },
    { key: 'transcript', label: '全文', icon: FileText },
  ];

  return (
    <div className="max-w-4xl mx-auto px-4 md:px-8 py-4 md:py-8">
      {/* 顶部导航 */}
      <div className="flex items-center justify-between">
        <Link
          to="/"
          className="inline-flex items-center gap-1.5 text-[13px] text-muted hover:text-ink transition-colors"
        >
          <ArrowLeft size={15} />
          返回
        </Link>
        <div className="relative">
          <button
            onClick={() => setMenuOpen((v) => !v)}
            className="h-8 w-8 grid place-items-center rounded-lg text-muted hover:bg-panel2 transition-colors"
          >
            <MoreHorizontal size={17} />
          </button>
          {menuOpen && (
            <>
              <div className="fixed inset-0 z-30" onClick={() => setMenuOpen(false)} />
              <div className="absolute right-0 top-9 z-40 w-44 card p-1 shadow-lift animate-fade-up">
                <button
                  onClick={() => { setMenuOpen(false); void exportMd(); }}
                  className="w-full flex items-center gap-2 px-2.5 h-9 rounded-lg text-[13px] hover:bg-panel2"
                >
                  <Download size={14} /> 导出 Markdown
                </button>
                <button
                  onClick={() => { setMenuOpen(false); void genSummary(true); }}
                  className="w-full flex items-center gap-2 px-2.5 h-9 rounded-lg text-[13px] hover:bg-panel2"
                >
                  <RefreshCw size={14} /> 重新生成纪要
                </button>
                <button
                  onClick={() => { setMenuOpen(false); void retry(); }}
                  className="w-full flex items-center gap-2 px-2.5 h-9 rounded-lg text-[13px] hover:bg-panel2"
                >
                  <RotateCw size={14} /> 重试转写
                </button>
                <div className="h-px bg-line my-1" />
                <button
                  onClick={() => { setMenuOpen(false); void remove(); }}
                  className="w-full flex items-center gap-2 px-2.5 h-9 rounded-lg text-[13px] text-red-500 hover:bg-red-500/10"
                >
                  <Trash2 size={14} /> 删除记录
                </button>
              </div>
            </>
          )}
        </div>
      </div>

      {/* 标题与元信息 */}
      <div className="mt-4">
        {editingTitle ? (
          <input
            autoFocus
            value={titleDraft}
            onChange={(e) => setTitleDraft(e.target.value)}
            onBlur={saveTitle}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void saveTitle();
              if (e.key === 'Escape') { setTitleDraft(data.title); setEditingTitle(false); }
            }}
            className="w-full bg-panel2 border border-brand/40 rounded-xl px-3 py-2 text-[22px] font-semibold outline-none"
          />
        ) : (
          <h1
            onClick={() => setEditingTitle(true)}
            className="text-[22px] md:text-[26px] font-semibold tracking-tight leading-snug cursor-text hover:text-brand/90 transition-colors"
          >
            {data.title}
          </h1>
        )}

        <div className="mt-2.5 flex items-center gap-2 flex-wrap text-[12px] text-muted">
          <span>{fmtDate(data.startedAt)} {fmtTime(data.startedAt)}</span>
          <span className="opacity-40">·</span>
          <span>{fmtDurationCn(data.durationMs)}</span>
          <span className="opacity-40">·</span>
          <span>{data.chunkCount} 个片段</span>
          {data.status === 'ready' && (
            <span className="chip bg-emerald-500/10 text-emerald-600 dark:text-emerald-400">
              <Check size={10} />
              已完成
            </span>
          )}
          {processing && (
            <span className="chip bg-amber-500/10 text-amber-600 dark:text-amber-400">
              <Loader2 size={10} className="animate-spin" />
              转写中 {data.doneChunks}/{data.chunkCount}
            </span>
          )}
          {data.status === 'failed' && (
            <span className="chip bg-red-500/10 text-red-500">
              <AlertCircle size={10} />
              转写失败
            </span>
          )}
        </div>
      </div>

      {/* 录音源文件 / 播放器（吸顶，切到任何 tab 都能听到、都能定位） */}
      <div className="sticky top-14 md:top-0 z-20 -mx-4 md:-mx-8 px-4 md:px-8 py-2.5 bg-bg/85 backdrop-blur-xl">
        <AudioPlayer
          ref={playerRef}
          recordingId={data.id}
          audio={data.audio ?? { mode: 'none', url: null, mime: null, size: 0, filename: null, durationMs: 0 }}
          chunks={audioChunks}
          onTime={setCurrentMs}
          onPlayingChange={setPlaying}
        />
      </div>

      {/* Tabs */}
      <div className="mt-6 flex items-center gap-1 border-b border-line">
        {tabs.map((t) => (
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
            {tab === t.key && (
              <span className="absolute inset-x-2 -bottom-px h-[2px] rounded-full bg-brand" />
            )}
          </button>
        ))}
        <div className="flex-1" />
        {tab === 'summary' && summaryReady && (
          <button
            onClick={() => void genSummary(true)}
            disabled={busy === 'summary'}
            className="hidden sm:inline-flex items-center gap-1.5 text-[12px] text-muted hover:text-brand px-2 h-8 rounded-lg transition-colors"
          >
            <RefreshCw size={12} className={cn(busy === 'summary' && 'animate-spin')} />
            重新生成
          </button>
        )}
      </div>

      {/* 内容 */}
      <div className="mt-5">
        {tab === 'summary' && (
          <div className="animate-fade-up">
            {summaryBusy && (
              <div className="card p-8 flex flex-col items-center gap-3">
                <div className="relative h-11 w-11">
                  <div className="absolute inset-0 rounded-full border-2 border-brand/20" />
                  <div className="absolute inset-0 rounded-full border-2 border-brand border-t-transparent animate-spin" />
                </div>
                <div className="text-sm font-medium">AI 正在整理这段内容…</div>
                <div className="text-[12px] text-muted">通常需要 5-30 秒，可以先切到「全文」查看转写结果</div>
              </div>
            )}

            {summary?.status === 'failed' && (
              <div className="card p-6">
                <div className="flex items-start gap-3">
                  <AlertCircle size={18} className="text-red-500 mt-0.5" />
                  <div className="flex-1">
                    <div className="text-sm font-medium">纪要生成失败</div>
                    <div className="text-[12px] text-muted mt-1 leading-relaxed">{summary.error}</div>
                    <button onClick={() => void genSummary(true)} className="btn-outline h-8 px-3 text-[12px] mt-3">
                      <RefreshCw size={13} />
                      重试
                    </button>
                  </div>
                </div>
              </div>
            )}

            {summaryReady && summary && (
              <div className="space-y-4">
                {summary.stats?.highlights && summary.stats.highlights.length > 0 && (
                  <div className="card p-4 bg-soft/50">
                    <div className="text-[11px] font-medium text-brand mb-2 flex items-center gap-1.5">
                      <Sparkles size={11} />
                      关键信息
                    </div>
                    <ul className="space-y-1.5">
                      {summary.stats.highlights.map((h, i) => (
                        <li key={i} className="text-[13px] leading-relaxed flex gap-2">
                          <span className="text-brand/50 shrink-0">·</span>
                          <span>{h}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                <div className="card p-5 md:p-7">
                  <MarkdownView markdown={summary.markdown ?? ''} />
                </div>

                <div className="flex items-center justify-between text-[11px] text-muted px-1">
                  <span>{summary.model ? `由 ${summary.model} 生成` : '已生成'}</span>
                  <button
                    onClick={() => { void copyText(summary.markdown ?? ''); toast.push('已复制纪要', 'success'); }}
                    className="inline-flex items-center gap-1 hover:text-brand transition-colors"
                  >
                    <Copy size={11} /> 复制全文
                  </button>
                </div>
              </div>
            )}

            {!summary && !processing && (
              <div className="card p-10 flex flex-col items-center text-center">
                <div className="h-12 w-12 rounded-2xl bg-soft grid place-items-center mb-3">
                  <Sparkles size={20} className="text-brand" />
                </div>
                <div className="text-sm font-medium">还没有生成纪要</div>
                <div className="text-[12px] text-muted mt-1.5 max-w-xs leading-relaxed">
                  基于完整转写内容，自动提炼要点、结论与待办事项，并生成思维导图。
                </div>
                <button
                  onClick={() => void genSummary()}
                  disabled={busy === 'summary'}
                  className="btn-primary h-9 px-4 text-[13px] mt-4"
                >
                  {busy === 'summary' ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
                  生成 AI 纪要
                </button>
              </div>
            )}

            {!summary && processing && (
              <div className="card p-8 text-center text-[13px] text-muted">
                转写完成后会自动生成纪要
              </div>
            )}
          </div>
        )}

        {tab === 'mindmap' && (
          <div className="animate-fade-up">
            {summaryReady && summary?.mindmap ? (
              <Suspense
                fallback={
                  <div className="h-[62vh] rounded-2xl border border-line bg-panel grid place-items-center">
                    <Loader2 size={18} className="animate-spin text-muted" />
                  </div>
                }
              >
                <MindMap markdown={summary.mindmap} />
              </Suspense>
            ) : (
              <div className="card p-10 flex flex-col items-center text-center">
                <div className="h-12 w-12 rounded-2xl bg-panel2 grid place-items-center mb-3">
                  <ListTree size={20} className="text-muted" />
                </div>
                <div className="text-sm font-medium">暂无可用的思维导图</div>
                <div className="text-[12px] text-muted mt-1.5">
                  {summaryBusy ? '正在生成中…' : '生成 AI 纪要后会同时产出思维导图'}
                </div>
              </div>
            )}
          </div>
        )}

        {tab === 'transcript' && (
          <div className="animate-fade-up space-y-4">
            {data.transcript.segments.length === 0 && processing && (
              <div className="card p-8 text-center text-[13px] text-muted flex items-center justify-center gap-2">
                <Loader2 size={14} className="animate-spin" />
                正在转写，文字会陆续出现在这里
              </div>
            )}

            {data.transcript.segments.length > 0 && (
              <div className="card p-4 md:p-6">
                <div className="flex items-center justify-between gap-2 mb-3">
                  <div className="text-[11px] text-muted flex items-center gap-1.5">
                    <Headphones size={11} />
                    共 {data.transcript.segments.length} 句 · 点任意一句跳到对应位置
                  </div>
                  <button
                    onClick={() => setFollow((v) => !v)}
                    className={cn(
                      'chip !py-0.5 !px-2 transition-colors',
                      follow
                        ? 'bg-soft text-brand'
                        : 'bg-panel2 text-muted hover:text-ink',
                    )}
                    title="播放时自动滚动到当前句"
                  >
                    {follow ? '跟随播放' : '未跟随'}
                  </button>
                </div>
                <div className="space-y-0.5">
                  {data.transcript.segments.map((s, i) => {
                    const active = i === activeIndex && currentMs > 0;
                    const passed = i < activeIndex;
                    return (
                      <div
                        key={i}
                        ref={(el) => { segRefs.current[i] = el; }}
                        onClick={() => jumpTo(s.startMs ?? 0)}
                        className={cn(
                          'flex gap-3 -mx-2 px-2 py-1.5 rounded-xl cursor-pointer transition-colors',
                          active ? 'bg-soft' : 'hover:bg-panel2/70',
                        )}
                      >
                        <span
                          className={cn(
                            'shrink-0 mt-0.5 text-[11px] tabular-nums font-mono w-11 transition-colors',
                            active
                              ? 'text-brand font-medium'
                              : passed
                                ? 'text-muted/80'
                                : 'text-muted/55',
                          )}
                        >
                          {fmtDuration(s.startMs)}
                        </span>
                        <p
                          className={cn(
                            'text-[14px] leading-[1.75] flex-1 transition-colors',
                            active ? 'text-ink' : 'text-ink/85',
                          )}
                        >
                          {s.text}
                        </p>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {data.failedChunks > 0 && (
              <div className="card p-4 border-red-500/25 flex items-start gap-3">
                <AlertCircle size={16} className="text-red-500 mt-0.5 shrink-0" />
                <div className="flex-1">
                  <div className="text-[13px] font-medium">有 {data.failedChunks} 个片段转写失败</div>
                  <div className="text-[11.5px] text-muted mt-0.5">
                    可能是网络或服务商限流导致，可以重新排队。
                  </div>
                </div>
                <button onClick={() => void retry()} className="btn-outline h-8 px-3 text-[12px] shrink-0">
                  <RotateCw size={12} className={cn(busy === 'retry' && 'animate-spin')} />
                  重试
                </button>
              </div>
            )}

            {/* 分片进度 */}
            <div className="card p-4">
              <div className="text-[11px] text-muted mb-2.5 flex items-center gap-1.5">
                <Clock size={11} />
                分片处理状态
              </div>
              <div className="flex flex-wrap gap-1.5">
                {data.chunks.map((c) => {
                  const st = liveChunks[c.seq] ?? c.status;
                  return (
                    <div
                      key={c.id}
                      title={`第 ${c.seq + 1} 段 · ${fmtDuration(c.durationMs)} · ${st}`}
                      className={cn(
                        'h-7 px-2 rounded-md text-[10.5px] grid place-items-center tabular-nums border',
                        st === 'done' && 'bg-emerald-500/10 border-emerald-500/20 text-emerald-600 dark:text-emerald-400',
                        st === 'failed' && 'bg-red-500/10 border-red-500/20 text-red-500',
                        (st === 'queued' || st === 'transcribing') && 'bg-amber-500/10 border-amber-500/25 text-amber-600 dark:text-amber-400',
                        st === 'pending' && 'bg-panel2 border-line text-muted',
                      )}
                    >
                      {c.seq + 1}
                    </div>
                  );
                })}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
