import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  ArrowUpRight, FileUp, History, Loader2, Mic, Search, Sparkles, X,
} from 'lucide-react';
import { api, uploadFile, type Recording, type Summary } from '../lib/api';
import { useAuth } from '../lib/auth';
import { useToast } from '../lib/toast';
import RecordingCard, { EmptyState } from '../components/RecordingCard';
import { cn, fmtDurationCn, friendlyDay, todayStr, weekdayCn } from '../lib/utils';

type Stats = {
  recordings: number;
  durationMs: number;
  chars: number;
  summaries: number;
  activeDays: number;
  activeJobs: number;
};

function greeting() {
  const h = new Date().getHours();
  if (h < 5) return '夜深了';
  if (h < 11) return '早上好';
  if (h < 13) return '中午好';
  if (h < 18) return '下午好';
  return '晚上好';
}

export default function Home() {
  const { user } = useAuth();
  const toast = useToast();
  const navigate = useNavigate();
  const [stats, setStats] = useState<Stats | null>(null);
  const [list, setList] = useState<Recording[]>([]);
  const [todaySummary, setTodaySummary] = useState<Summary | null>(null);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState('');
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      const today = todayStr();
      const [s, r, sums] = await Promise.all([
        api<Stats>('/api/stats'),
        api<Recording[]>('/api/recordings?limit=80'),
        api<any[]>(`/api/summaries?scope=day&from=${today}&to=${today}&limit=5`),
      ]);
      setStats(s);
      setList(r);
      const ready = (sums ?? []).find((x) => x.status === 'ready');
      setTodaySummary(ready ?? null);
    } catch (e: any) {
      toast.push(e?.message ?? '加载失败', 'error');
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    void load();
  }, [load]);

  const filtered = useMemo(() => {
    if (!q.trim()) return list;
    const k = q.trim().toLowerCase();
    return list.filter((r) => `${r.title} ${r.excerpt ?? ''}`.toLowerCase().includes(k));
  }, [list, q]);

  const grouped = useMemo(() => {
    const map = new Map<string, Recording[]>();
    for (const r of filtered) {
      const arr = map.get(r.date) ?? [];
      arr.push(r);
      map.set(r.date, arr);
    }
    return [...map.entries()].sort((a, b) => b[0].localeCompare(a[0]));
  }, [filtered]);

  const todayCount = list.filter((r) => r.date === todayStr()).length;

  const handleFile = async (file: File) => {
    setUploading(true);
    try {
      const rec = await api<Recording>('/api/recordings', {
        method: 'POST',
        body: { title: file.name.replace(/\.[^.]+$/, ''), source: 'upload' },
      });
      toast.push('正在上传并切片…', 'info');
      await uploadFile(rec.id, file, 60);
      toast.push('已上传，正在后台转写', 'success');
      navigate(`/recordings/${rec.id}`);
    } catch (e: any) {
      toast.push(e?.message ?? '上传失败', 'error');
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = '';
    }
  };

  return (
    <div className="max-w-4xl mx-auto px-4 md:px-8 py-6 md:py-10">
      {/* 问候 */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-[26px] md:text-[30px] font-semibold tracking-tight leading-tight">
            {greeting()}，{user?.displayName || user?.username}
          </h1>
          <p className="text-[13px] text-muted mt-1.5">
            {todayCount > 0
              ? `今天已经记录了 ${todayCount} 段内容`
              : '今天还没有录音，点一下开始吧'}
          </p>
        </div>
        <Link to="/record" className="btn-primary h-10 px-4 shrink-0 hidden sm:inline-flex">
          <Mic size={16} />
          开始录音
        </Link>
      </div>

      {/* 统计 */}
      <div className="card mt-6 grid grid-cols-3 divide-x divide-line overflow-hidden">
        {[
          { label: '累计录音', value: stats ? `${stats.recordings}` : '—', unit: '段' },
          { label: '录音时长', value: stats ? fmtDurationCn(stats.durationMs) : '—', unit: '' },
          { label: '转写字数', value: stats ? `${(stats.chars / 1000).toFixed(1)}` : '—', unit: '千字' },
        ].map((item) => (
          <div key={item.label} className="px-4 py-4">
            <div className="text-[11px] text-muted">{item.label}</div>
            <div className="mt-1 flex items-baseline gap-1">
              <span className="text-[19px] font-semibold tabular-nums tracking-tight">{item.value}</span>
              {item.unit && <span className="text-[11px] text-muted">{item.unit}</span>}
            </div>
          </div>
        ))}
      </div>

      {/* 今日日报 */}
      {todaySummary && (
        <Link
          to={`/summaries/${todaySummary.id}`}
          className="mt-4 card p-4 flex items-start gap-3 hover:border-brand/30 hover:shadow-soft transition-all group"
        >
          <div className="h-9 w-9 shrink-0 rounded-xl bg-soft grid place-items-center">
            <Sparkles size={15} className="text-brand" />
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center justify-between gap-2">
              <h3 className="text-[14px] font-medium truncate">{todaySummary.title}</h3>
              <ArrowUpRight size={15} className="text-muted shrink-0 group-hover:text-brand transition-colors" />
            </div>
            <p className="text-[12px] text-muted mt-1 line-clamp-2 leading-relaxed">
              {todaySummary.stats?.highlights?.length
                ? todaySummary.stats.highlights.join(' · ')
                : '今天的 AI 日报已生成'}
            </p>
          </div>
        </Link>
      )}

      {/* 快捷操作 */}
      <div className="mt-4 grid grid-cols-2 gap-3">
        <Link
          to="/record"
          className="card p-4 flex items-center gap-3 hover:border-brand/30 hover:shadow-soft transition-all"
        >
          <div className="h-9 w-9 rounded-xl bg-gradient-to-br from-[var(--brand)] to-[var(--brand-2)] grid place-items-center shadow-[0_6px_14px_-8px_var(--brand)]">
            <Mic size={16} className="text-white" />
          </div>
          <div className="min-w-0">
            <div className="text-[13px] font-medium">网页录音</div>
            <div className="text-[11px] text-muted truncate">边录边转写</div>
          </div>
        </Link>

        <button
          onClick={() => fileRef.current?.click()}
          disabled={uploading}
          className="card p-4 flex items-center gap-3 hover:border-brand/30 hover:shadow-soft transition-all text-left"
        >
          <div className="h-9 w-9 rounded-xl bg-panel2 border border-line grid place-items-center">
            {uploading ? <Loader2 size={16} className="animate-spin text-muted" /> : <FileUp size={16} className="text-muted" />}
          </div>
          <div className="min-w-0">
            <div className="text-[13px] font-medium">上传音频</div>
            <div className="text-[11px] text-muted truncate">自动切片转写</div>
          </div>
        </button>
        <input
          ref={fileRef}
          type="file"
          accept="audio/*,video/mp4"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) void handleFile(f);
          }}
        />
      </div>

      {/* 搜索 */}
      <div className="mt-8 flex items-center gap-2">
        <div className="relative flex-1">
          <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="搜索标题或内容…"
            className="field pl-9 pr-9 h-9 text-[13px]"
          />
          {q && (
            <button
              onClick={() => setQ('')}
              className="absolute right-2.5 top-1/2 -translate-y-1/2 text-muted hover:text-ink"
            >
              <X size={14} />
            </button>
          )}
        </div>
        <Link to="/calendar" className="btn-ghost h-9 px-3 text-[12.5px]">
          <History size={14} />
          回忆
        </Link>
      </div>

      {/* 列表 */}
      <div className="mt-5 space-y-7">
        {loading && (
          <div className="py-12 grid place-items-center">
            <Loader2 size={20} className="animate-spin text-muted" />
          </div>
        )}

        {!loading && grouped.length === 0 && (
          <EmptyState
            title={q ? '没有找到相关记录' : '还没有任何录音'}
            desc={
              q
                ? '换个关键词试试'
                : '点击「开始录音」直接录，或者上传一段已有的音频文件，系统会自动切片并转写成文字。'
            }
            action={
              !q && (
                <Link to="/record" className="btn-primary h-9 px-4 text-[13px]">
                  <Mic size={15} />
                  开始第一段录音
                </Link>
              )
            }
          />
        )}

        {grouped.map(([date, items]) => (
          <section key={date}>
            <div className="flex items-center gap-2 mb-2.5 px-0.5">
              <h2 className="text-[12.5px] font-medium text-muted">
                {friendlyDay(date)}
                <span className="ml-1.5 opacity-60">{weekdayCn(date)}</span>
              </h2>
              <div className="flex-1 h-px bg-line" />
              <span className="text-[11px] text-muted/70">{items.length} 段</span>
            </div>
            <div className="space-y-2.5">
              {items.map((r) => (
                <RecordingCard key={r.id} r={r} />
              ))}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}
