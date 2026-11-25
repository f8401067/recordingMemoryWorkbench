import React, { Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import {
  ArrowLeft, Check, Copy, Download, FileText, Headphones, ListTree, Loader2, MoreHorizontal,
  Pencil, RefreshCw, Sparkles, Trash2, AlertCircle, RotateCw, Clock, Wand2,
} from 'lucide-react';
import {
  api, deleteRecording, diarizeRecording, enrollVoiceProfile, getVoiceProfiles,
  subscribeRecording, updateTranscriptSegment, type RecordingDetail, type VoiceProfile,
} from '../lib/api';
import MarkdownView from '../components/MarkdownView';
import SummaryMcpActions from '../components/SummaryMcpActions';
import AiReviseDialog from '../components/AiReviseDialog';
import AudioPlayer, { type AudioHandle } from '../components/AudioPlayer';
import { useToast } from '../lib/toast';
import {
  cn, copyText, fmtDate, fmtDuration, fmtDurationCn, fmtTimeRange, statusText,
} from '../lib/utils';

const MindMap = React.lazy(() => import('../components/MindMap'));

/** 说话人标签配色，按编号循环；纯色块方便一眼区分谁在说 */
const SPEAKER_CHIPS = [
  'bg-sky-500/10 text-sky-600 dark:text-sky-400',
  'bg-amber-500/10 text-amber-600 dark:text-amber-400',
  'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  'bg-violet-500/10 text-violet-600 dark:text-violet-400',
  'bg-rose-500/10 text-rose-600 dark:text-rose-400',
];

type Tab = 'summary' | 'mindmap' | 'transcript';

export default function RecordingDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  /** 从列表页点进来时带上的来源路径，返回时回到原页面而不是首页 */
  const backTo = (location.state as { from?: string } | null)?.from || '/';
  const toast = useToast();
  const [params] = useSearchParams();
  /** 从「找寻」跳进来时带的命中时间点（毫秒），用来定位原话 */
  const targetMs = useMemo(() => {
    const n = Number(params.get('t'));
    return Number.isFinite(n) && n > 0 ? n : null;
  }, [params]);

  const [data, setData] = useState<RecordingDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<Tab>('summary');
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState('');
  const [menuOpen, setMenuOpen] = useState(false);
  const [reviseOpen, setReviseOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [liveChunks, setLiveChunks] = useState<Record<number, string>>({});
  const pollRef = useRef<number | null>(null);

  // 声纹角色：给这段录音里的说话人标「我」或熟悉的角色
  const [voiceProfiles, setVoiceProfiles] = useState<VoiceProfile[]>([]);
  const [labelTarget, setLabelTarget] = useState<number | null>(null);
  const [labelName, setLabelName] = useState('');
  const [labelBusy, setLabelBusy] = useState(false);
  const [diarizing, setDiarizing] = useState(false);

  // 全文手动修订
  const [editing, setEditing] = useState<{ i: number; startMs: number } | null>(null);
  const [draft, setDraft] = useState('');
  const [savingSeg, setSavingSeg] = useState(false);

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
      if (d.diarized) setDiarizing(false);
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

  const loadVoiceProfiles = useCallback(async () => {
    try {
      setVoiceProfiles((await getVoiceProfiles()).profiles);
    } catch {
      /* 没有角色库也能正常看录音 */
    }
  }, []);

  useEffect(() => {
    void loadVoiceProfiles();
  }, [loadVoiceProfiles]);

  const runDiarize = async (force = false) => {
    if (!id) return;
    setDiarizing(true);
    try {
      await diarizeRecording(id, force);
      toast.push(force ? '已重新开始说话人分离' : '已开始补跑说话人分离', 'success');
      // 分离是排队任务，先拉一次让界面进入等待态，轮询会接管后续刷新
      window.setTimeout(() => void load(true), 1200);
    } catch (e: any) {
      setDiarizing(false);
      toast.push(e?.message ?? '补跑失败', 'error');
    }
  };

  const markSpeaker = async (speaker: number, name: string, isSelf = false) => {
    if (!id) return;
    setLabelBusy(true);
    try {
      await enrollVoiceProfile({ recordingId: id, speaker, name, isSelf });
      setLabelTarget(null);
      setLabelName('');
      toast.push(isSelf ? '已记住你的声音' : `已标记为「${name}」`, 'success');
      await Promise.all([load(true), loadVoiceProfiles()]);
    } catch (e: any) {
      toast.push(e?.message ?? '标记失败', 'error');
    } finally {
      setLabelBusy(false);
    }
  };

  // 处理中时轮询
  useEffect(() => {
    if (!data) return;
    const busyState =
      data.status === 'processing' ||
      data.status === 'recording' ||
      data.summaryStatus === 'queued' ||
      data.summaryStatus === 'generating' ||
      diarizing;
    // 编辑期间冻结轮询，避免新到的句子把手上的序号挤偏
    if (!busyState || editing) {
      if (pollRef.current) window.clearInterval(pollRef.current);
      pollRef.current = null;
      return;
    }
    if (pollRef.current) return;
    pollRef.current = window.setInterval(() => void load(true), 2200);
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
      pollRef.current = null;
    };
  }, [data, load, editing, diarizing]);

  // SSE 实时片段
  useEffect(() => {
    if (!id) return;
    const unsub = subscribeRecording(id, (ev, payload) => {
      if (ev === 'chunk' && payload?.seq !== undefined) {
        setLiveChunks((m) => ({ ...m, [payload.seq]: payload.status }));
      }
        const mergedPayload = payload?.id;
        if (ev === 'merged' && mergedPayload) {
          // 这条被并到相邻的录音里去了，跟着切过去，别停在已经不存在的页面上
          navigate(`/recordings/${mergedPayload}`, { replace: true, state: location.state });
          return;
        }
        if (ev === 'discarded') {
          // 转写确认没人声后被服务端自动丢弃，别停在一个已经不存在的录音页
          navigate('/', { replace: true });
          return;
        }
        if (ev === 'summary' || ev === 'recording') void load(true);
      });
      return unsub;
    }, [id, load, navigate, location.state]);


  // ---------------- 音频 / 文字联动 ----------------
  const segments = data?.transcript.segments ?? [];
  const selfProfile = voiceProfiles.find((p) => p.isSelf);
  const labelMatch =
    labelTarget != null ? (data?.speakers ?? []).find((x) => x.speaker === labelTarget) : undefined;

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

  // 命中句：从「找寻」带 t 参数进来时，定位并高亮那句
  const focusIndex = useMemo(() => {
    if (targetMs == null || !segments.length) return -1;
    let idx = 0;
    for (let i = 0; i < segments.length; i += 1) {
      if ((segments[i].startMs ?? 0) <= targetMs) idx = i;
    }
    return idx;
  }, [segments, targetMs]);

  const jumpedRef = useRef(false);
  useEffect(() => {
    if (!data || targetMs == null || jumpedRef.current) return;
    jumpedRef.current = true;
    setTab('transcript');
    // 音频元数据可能还没就绪，稍等一下再定位
    window.setTimeout(() => playerRef.current?.seek(targetMs), 400);
    window.setTimeout(() => {
      if (focusIndex >= 0) {
        segRefs.current[focusIndex]?.scrollIntoView({ block: 'center', behavior: 'smooth' });
      }
    }, 650);
  }, [data, targetMs, focusIndex]);

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

  const startEdit = (i: number) => {
    const seg = data?.transcript.segments[i];
    if (!seg) return;
    setEditing({ i, startMs: seg.startMs ?? 0 });
    setDraft(seg.text ?? '');
  };

  const cancelEdit = () => {
    setEditing(null);
    setDraft('');
  };

  const saveEdit = async () => {
    if (!data || !editing) return;
    const segs = data.transcript.segments;
    // 期间可能插入过新句子，用 startMs 重新定位，实在找不到再用原下标兜底
    let idx = segs.findIndex((s) => (s.startMs ?? 0) === editing.startMs);
    if (idx < 0) idx = editing.i;
    const old = segs[idx]?.text ?? '';
    const text = draft.trim();
    if (text === old) {
      cancelEdit();
      return;
    }
    setSavingSeg(true);
    try {
      await updateTranscriptSegment(data.id, idx, text);
      await load(true);
      setEditing(null);
      setDraft('');
      toast.push('已保存修改', 'success');
    } catch (e: any) {
      toast.push(e?.message ?? '保存失败', 'error');
    } finally {
      setSavingSeg(false);
    }
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
      await deleteRecording(data.id);
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
          to={backTo}
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
          <span>{fmtDate(data.startedAt)} {fmtTimeRange(data)}</span>
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
          {data.userPresent === false && (
            <span
              className="chip bg-rose-500/10 text-rose-600 dark:text-rose-400"
              title={`已录入你的声纹，但这段录音里没有认出你${data.userSimilarity != null ? `（最高相似度 ${data.userSimilarity.toFixed(2)}）` : ''}`}
            >
              我未参与
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
                  <div className="flex items-center gap-3">
                    <button
                      onClick={() => setReviseOpen(true)}
                      className="inline-flex items-center gap-1 hover:text-brand transition-colors"
                      title="按你的要求让 AI 修改这份纪要"
                    >
                      <Wand2 size={11} /> AI 修改
                    </button>
                    <button
                      onClick={() => { void copyText(summary.markdown ?? ''); toast.push('已复制纪要', 'success'); }}
                      className="inline-flex items-center gap-1 hover:text-brand transition-colors"
                    >
                      <Copy size={11} /> 复制全文
                    </button>
                  </div>
                </div>

                <SummaryMcpActions summaryId={summary.id} />
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

            {data.transcript.segments.length > 0 && !data.diarized && (
              <div className="flex items-center justify-between gap-3 rounded-xl border border-dashed border-line px-3 py-2.5">
                <div className="text-[11px] text-muted leading-relaxed">
                  这条录音还没做说话人分离，所以不能标记「我」或人物。
                </div>
                <button
                  onClick={() => void runDiarize(false)}
                  disabled={diarizing}
                  className="btn-ghost h-7 px-2.5 text-[11.5px] shrink-0 disabled:opacity-40"
                >
                  {diarizing ? '分离中…' : '补跑分离'}
                </button>
              </div>
            )}

            {data.transcript.segments.length > 0 && data.diarized && !data.voiceprintReady && (
              <div className="flex items-center justify-between gap-3 rounded-xl border border-dashed border-line px-3 py-2.5">
                <div className="text-[11px] text-muted leading-relaxed">
                  这条录音是旧版本分离的，没有保存声纹，重新分离一次就能标记人物。
                </div>
                <button
                  onClick={() => void runDiarize(true)}
                  disabled={diarizing}
                  className="btn-ghost h-7 px-2.5 text-[11.5px] shrink-0 disabled:opacity-40"
                >
                  {diarizing ? '分离中…' : '重新分离'}
                </button>
              </div>
            )}

            {data.transcript.segments.length > 0 && (
              <div className="card p-4 md:p-6">
                <div className="flex items-center justify-between gap-2 mb-3">
                  <div className="text-[11px] text-muted flex items-center gap-1.5">
                    <Headphones size={11} />
                    共 {data.transcript.segments.length} 句 · 点时间跳转，点铅笔可修改文字
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
                    const isEditing = editing?.i === i;
                    return (
                      <div
                        key={i}
                        ref={(el) => { segRefs.current[i] = el; }}
                        className={cn(
                          'group/seg flex gap-3 -mx-2 px-2 py-1.5 rounded-xl transition-colors',
                          active
                            ? 'bg-soft'
                            : targetMs != null && i === focusIndex
                              ? 'bg-brand/[0.055] ring-1 ring-brand/25'
                              : 'hover:bg-panel2/70',
                        )}
                      >
                        <button
                          onClick={() => jumpTo(s.startMs ?? 0)}
                          title="跳到这里播放"
                          className={cn(
                            'shrink-0 mt-0.5 text-[11px] tabular-nums font-mono w-11 text-left transition-colors hover:text-brand',
                            active
                              ? 'text-brand font-medium'
                              : passed
                                ? 'text-muted/80'
                                : 'text-muted/55',
                          )}
                        >
                          {fmtDuration(s.startMs)}
                        </button>

                        {isEditing ? (
                          <div className="flex-1">
                            <textarea
                              autoFocus
                              value={draft}
                              onChange={(e) => setDraft(e.target.value)}
                              rows={Math.max(2, Math.ceil(draft.length / 34) + 1)}
                              onKeyDown={(e) => {
                                e.stopPropagation();
                                if (e.key === 'Enter' && !e.shiftKey) {
                                  e.preventDefault();
                                  void saveEdit();
                                }
                                if (e.key === 'Escape') cancelEdit();
                              }}
                              className="field h-auto min-h-[64px] w-full resize-y text-[14px] leading-[1.75] py-2"
                            />
                            <div className="mt-1.5 flex items-center gap-2">
                              <button
                                onClick={() => void saveEdit()}
                                disabled={savingSeg}
                                className="btn-primary h-7 px-3 text-[12px]"
                              >
                                {savingSeg ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
                                保存
                              </button>
                              <button
                                onClick={cancelEdit}
                                className="btn-ghost h-7 px-3 text-[12px]"
                              >
                                取消
                              </button>
                              <span className="text-[11px] text-muted/70">Enter 保存 · Esc 取消</span>
                            </div>
                          </div>
                        ) : (
                          <>
                            <p
                              onClick={() => jumpTo(s.startMs ?? 0)}
                              className={cn(
                                'text-[14px] leading-[1.75] flex-1 cursor-pointer transition-colors',
                                active ? 'text-ink' : 'text-ink/85',
                              )}
                            >
                              {s.speaker != null && (
                                <button
                                  type="button"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    setLabelTarget(s.speaker!);
                                    setLabelName('');
                                  }}
                                  title="点击标记「我」或熟悉的人物"
                                  className={cn(
                                    'inline-block mr-1.5 -translate-y-px rounded-md px-1.5 py-0.5 text-[10.5px] font-medium align-middle hover:ring-1 hover:ring-brand/40 cursor-pointer',
                                    s.speakerIsSelf
                                      ? 'bg-brand/10 text-brand'
                                      : SPEAKER_CHIPS[((s.speaker ?? 1) - 1) % SPEAKER_CHIPS.length],
                                  )}
                                >
                                  {s.speakerName ?? `说话人 ${s.speaker}`}
                                </button>
                              )}
                              {s.text}
                            </p>
                            <button
                              onClick={() => startEdit(i)}
                              title="修改这句"
                              aria-label="修改这句"
                              className={cn(
                                'shrink-0 h-6 w-6 self-start grid place-items-center rounded-md text-muted/60',
                                'transition-all hover:bg-panel hover:text-brand',
                                'opacity-100 sm:opacity-0 sm:group-hover/seg:opacity-100 focus:opacity-100',
                              )}
                            >
                              <Pencil size={12} />
                            </button>
                          </>
                        )}
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

      {reviseOpen && summary && (
        <AiReviseDialog
          summary={summary}
          onClose={() => setReviseOpen(false)}
          onRevised={() => void load(true)}
        />
      )}

      {data && labelTarget != null && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 px-4"
          onClick={() => {
            setLabelTarget(null);
            setLabelName('');
          }}
        >
          <div
            className="card w-full max-w-sm p-4 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center gap-2">
              <span
                className={cn(
                  'rounded-md px-1.5 py-0.5 text-[10.5px] font-medium',
                  labelMatch?.isSelf
                    ? 'bg-brand/10 text-brand'
                    : SPEAKER_CHIPS[(labelTarget - 1) % SPEAKER_CHIPS.length],
                )}
              >
                说话人 {labelTarget}
              </span>
              {labelMatch && (
                <span className="text-[12px] text-ink flex items-center gap-1.5 min-w-0">
                  {labelMatch.isSelf ? '我' : labelMatch.name}
                  <span className="text-[10.5px] text-muted/70 tabular-nums">
                    相似度 {labelMatch.score.toFixed(2)}
                  </span>
                </span>
              )}
              <button
                onClick={() => {
                  setLabelTarget(null);
                  setLabelName('');
                }}
                className="ml-auto text-muted hover:text-ink text-[16px] leading-none px-1"
                title="关闭"
              >
                ×
              </button>
            </div>

            {labelMatch ? (
              <div className="mt-3 text-[11.5px] text-muted leading-relaxed">
                这段对话里已经认出「{labelMatch.isSelf ? '我' : labelMatch.name}」。
                想换名字的话，可以到「设置 → 录音与纪要 → 声纹角色」删除后重新标记。
              </div>
            ) : !data.voiceprintReady ? (
              <div className="mt-3 space-y-3">
                <div className="text-[11.5px] text-muted leading-relaxed">
                  这条录音是旧版本分离的，没有保存声纹，暂时不能标记人物。
                </div>
                <button
                  onClick={() => {
                    setLabelTarget(null);
                    void runDiarize(true);
                  }}
                  className="btn-primary w-full h-8 text-[12px]"
                >
                  重新分离
                </button>
              </div>
            ) : (
              <div className="mt-3 space-y-3">
                {!selfProfile && (
                  <button
                    onClick={() => void markSpeaker(labelTarget, '', true)}
                    disabled={labelBusy}
                    className="btn-primary w-full h-8 text-[12px] disabled:opacity-40"
                  >
                    这是我
                  </button>
                )}

                {voiceProfiles.filter((p) => !p.isSelf).length > 0 && (
                  <div>
                    <div className="text-[10.5px] text-muted mb-1.5">标记为已录入的角色</div>
                    <div className="flex flex-wrap gap-1.5">
                      {voiceProfiles
                        .filter((p) => !p.isSelf)
                        .map((p) => (
                          <button
                            key={p.id}
                            onClick={() => void markSpeaker(labelTarget, p.name)}
                            disabled={labelBusy}
                            className="btn-ghost h-7 px-2 text-[11.5px] disabled:opacity-40"
                          >
                            {p.name}
                          </button>
                        ))}
                    </div>
                  </div>
                )}

                <div>
                  <div className="text-[10.5px] text-muted mb-1.5">或新建一个角色</div>
                  <div className="flex gap-1.5">
                    <input
                      autoFocus
                      value={labelName}
                      onChange={(e) => setLabelName(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' && labelName.trim()) {
                          void markSpeaker(labelTarget, labelName.trim());
                        }
                        if (e.key === 'Escape') {
                          setLabelTarget(null);
                          setLabelName('');
                        }
                      }}
                      placeholder="名字，如 张三"
                      className="field h-8 flex-1 px-2 text-[12px]"
                    />
                    <button
                      onClick={() => void markSpeaker(labelTarget, labelName.trim())}
                      disabled={!labelName.trim() || labelBusy}
                      className="btn-outline h-8 px-3 text-[12px] disabled:opacity-40"
                    >
                      保存
                    </button>
                  </div>
                  <div className="text-[10px] text-muted/70 mt-1">同名会合并到已有角色</div>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
