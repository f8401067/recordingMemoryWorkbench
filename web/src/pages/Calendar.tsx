import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useLocation, useSearchParams } from 'react-router-dom';
import {
  AlertCircle, ArrowUpRight, BookOpen, CalendarDays, ChevronDown, ChevronLeft, ChevronRight,
  Clock, Loader2, Plus, RefreshCw, Sparkles,
} from 'lucide-react';
import { api, deleteRecording, type CalendarDay, type Recording, type Summary } from '../lib/api';
import { useToast } from '../lib/toast';
import RecordingList from '../components/RecordingList';
import MarkdownView from '../components/MarkdownView';
import SummaryMcpActions from '../components/SummaryMcpActions';
import { cn, fmtDurationCn, fmtTime, todayStr, weekdayCn } from '../lib/utils';

type ViewMode = 'day' | 'week' | 'month' | 'year';

const VIEWS: { key: ViewMode; label: string }[] = [
  { key: 'day', label: '日' },
  { key: 'week', label: '周' },
  { key: 'month', label: '月' },
  { key: 'year', label: '年' },
];

const WEEK_LABELS = ['一', '二', '三', '四', '五', '六', '日'];

const pad = (n: number) => String(n).padStart(2, '0');
const toStr = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const parseDate = (s: string) => new Date(`${s}T00:00:00`);
const shiftDate = (s: string, n: number) => {
  const d = parseDate(s);
  d.setDate(d.getDate() + n);
  return toStr(d);
};
const mondayOf = (s: string) => {
  const d = parseDate(s);
  d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
  return toStr(d);
};
const monthEndOf = (s: string) => {
  const d = parseDate(s);
  return toStr(new Date(d.getFullYear(), d.getMonth() + 1, 0));
};

function buildMonthGrid(year: number, month: number) {
  const first = new Date(year, month - 1, 1);
  const offset = (first.getDay() + 6) % 7; // 周一为首
  const daysInMonth = new Date(year, month, 0).getDate();
  const cells: (string | null)[] = [];
  for (let i = 0; i < offset; i++) cells.push(null);
  for (let d = 1; d <= daysInMonth; d++) {
    cells.push(`${year}-${pad(month)}-${pad(d)}`);
  }
  while (cells.length % 7 !== 0) cells.push(null);
  return cells;
}

function periodOf(view: ViewMode, anchor: string) {
  if (view === 'day') return { from: anchor, to: anchor };
  if (view === 'week') {
    const monday = mondayOf(anchor);
    return { from: monday, to: shiftDate(monday, 6) };
  }
  if (view === 'month') {
    return { from: `${anchor.slice(0, 7)}-01`, to: monthEndOf(anchor) };
  }
  const y = anchor.slice(0, 4);
  return { from: `${y}-01-01`, to: `${y}-12-31` };
}

function labelOf(view: ViewMode, from: string, to: string) {
  const a = parseDate(from);
  if (view === 'year') return `${a.getFullYear()} 年`;
  if (view === 'month') return `${a.getFullYear()} 年 ${a.getMonth() + 1} 月`;
  if (view === 'day') {
    return `${a.getFullYear()} 年 ${a.getMonth() + 1} 月 ${a.getDate()} 日 ${weekdayCn(from)}`;
  }
  const b = parseDate(to);
  const tail = b.getFullYear() === a.getFullYear() ? '' : ` ${b.getFullYear()} 年`;
  return `${a.getFullYear()} 年 ${a.getMonth() + 1} 月 ${a.getDate()} 日 -${tail} ${b.getMonth() + 1} 月 ${b.getDate()} 日`;
}

type MemState = 'loading' | 'ready' | 'waiting' | 'failed' | 'none';

export default function CalendarPage() {
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  // 当前页的完整地址（带 view/date），详情页返回时用它回到同一屏
  const here = `${location.pathname}${location.search}`;

  const [view, setView] = useState<ViewMode>(() => {
    const v = params.get('view');
    return v === 'day' || v === 'week' || v === 'month' || v === 'year' ? v : 'day';
  });
  const [anchor, setAnchor] = useState(() => {
    const d = params.get('date');
    return d && /^\d{4}-\d{2}-\d{2}$/.test(d) ? d : todayStr();
  });
  // 日历默认收起，点击标题栏展开
  const [calOpen, setCalOpen] = useState(false);
  // 切换时用左右滑入动画，key 用于每次切换重新触发动画
  const [slide, setSlide] = useState<{ dir: 1 | -1; key: number } | null>(null);

  const period = useMemo(() => periodOf(view, anchor), [view, anchor]);
  const periodKey = `${view}:${period.from}:${period.to}`;

  // 日视图需要整月的格子，周/年视图就是当前区间
  const calRange = useMemo(
    () => (view === 'day' ? { from: `${anchor.slice(0, 7)}-01`, to: monthEndOf(anchor) } : period),
    [view, anchor, period],
  );

  const [days, setDays] = useState<CalendarDay[]>([]);
  const [calLoading, setCalLoading] = useState(true);
  const [records, setRecords] = useState<Recording[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [memState, setMemState] = useState<MemState>('loading');
  const [related, setRelated] = useState<Summary[]>([]);
  const [busy, setBusy] = useState(false);

  const autoTried = useRef<Set<string>>(new Set());

  /* ---------------- 日历数据 ---------------- */
  const loadCalendar = useCallback(async () => {
    setCalLoading(true);
    try {
      const res = await api<{ days: CalendarDay[] }>(
        `/api/calendar?from=${calRange.from}&to=${calRange.to}`,
      );
      setDays(res.days ?? []);
    } catch (e: any) {
      toast.push(e?.message ?? '加载失败', 'error');
    } finally {
      setCalLoading(false);
    }
  }, [calRange.from, calRange.to, toast]);

  useEffect(() => { void loadCalendar(); }, [loadCalendar]);

  const removeRecording = useCallback(
    async (r: Recording) => {
      if (!confirm(`删除「${r.title}」？\n删除后无法恢复，音频与转写文字都会被清除。`)) return;
      try {
        await deleteRecording(r.id);
        setRecords((cur) => cur.filter((x) => x.id !== r.id));
        void loadCalendar();
        toast.push('已删除', 'success');
      } catch (e: any) {
        toast.push(e?.message ?? '删除失败', 'error');
      }
    },
    [loadCalendar, toast],
  );

  const dayMap = useMemo(() => {
    const m = new Map<string, CalendarDay>();
    for (const d of days) m.set(d.date, d);
    return m;
  }, [days]);

  const stats = useMemo(() => {
    let count = 0;
    let duration = 0;
    let activeDays = 0;
    for (const d of days) {
      if (d.date < period.from || d.date > period.to) continue;
      count += d.recordings ?? 0;
      duration += d.durationMs ?? 0;
      if ((d.recordings ?? 0) > 0) activeDays += 1;
    }
    return { count, duration, activeDays };
  }, [days, period.from, period.to]);

  /* ---------------- 当日录音明细（仅日视图） ---------------- */
  useEffect(() => {
    if (view !== 'day') {
      setRecords([]);
      return;
    }
    let alive = true;
    void (async () => {
      try {
        const list = await api<Recording[]>(`/api/recordings?date=${anchor}&limit=100`);
        if (alive) setRecords(list);
      } catch {
        if (alive) setRecords([]);
      }
    })();
    return () => { alive = false; };
  }, [view, anchor, periodKey]);

  /* ---------------- 回忆（纪要） ---------------- */
  const findSummary = useCallback(async (from: string, to: string) => {
    const list = await api<any[]>(`/api/summaries?from=${from}&to=${to}&limit=80`);
    return (list ?? []).find(
      (s) => s.dateFrom === from && s.dateTo === to && s.scope !== 'recording',
    ) ?? null;
  }, []);

  const createSummary = useCallback(async (force: boolean) => {
    if (view === 'day') {
      await api('/api/summaries/day', { method: 'POST', body: { date: period.from, force } });
    } else {
      await api('/api/summaries/range', {
        method: 'POST',
        body: { from: period.from, to: period.to, force },
      });
    }
  }, [view, period.from, period.to]);

  const loadMemory = useCallback(async () => {
    const { from, to } = period;
    setMemState('loading');
    try {
      const hit = await findSummary(from, to);
      if (hit) {
        if (hit.status === 'ready') {
          const full = await api<Summary>(`/api/summaries/${hit.id}`);
          setSummary(full);
          setMemState('ready');
        } else if (hit.status === 'failed') {
          setSummary(hit as Summary);
          setMemState('failed');
        } else {
          setSummary(hit as Summary);
          setMemState('waiting');
        }
        return;
      }

      setSummary(null);
      if (stats.count === 0) {
        setMemState('none');
        return;
      }
      // 年视图只在用户点了按钮后生成，避免打开页面就把全年转写跑一遍
      if (view === 'year') {
        setMemState('none');
        return;
      }
      if (autoTried.current.has(periodKey)) {
        setMemState('none');
        return;
      }
      autoTried.current.add(periodKey);
      setMemState('waiting');
      await createSummary(false);
      const created = await findSummary(from, to);
      if (created) setSummary(created as Summary);
    } catch (e: any) {
      setMemState('none');
      toast.push(e?.message ?? '整理失败', 'error');
    }
  }, [period, findSummary, createSummary, stats.count, view, periodKey, toast]);

  useEffect(() => { void loadMemory(); }, [loadMemory]);

  // 生成中轮询
  useEffect(() => {
    if (memState !== 'waiting' || !period.from) return;
    const timer = window.setInterval(() => {
      void (async () => {
        const hit = await findSummary(period.from, period.to);
        if (!hit) return;
        if (hit.status === 'ready') {
          const full = await api<Summary>(`/api/summaries/${hit.id}`);
          setSummary(full);
          setMemState('ready');
          void loadCalendar();
        } else if (hit.status === 'failed') {
          setSummary(hit as Summary);
          setMemState('failed');
        } else {
          setSummary(hit as Summary);
        }
      })();
    }, 2500);
    return () => window.clearInterval(timer);
  }, [memState, period.from, period.to, findSummary, loadCalendar]);

  // 周 / 年视图下的其他回忆
  useEffect(() => {
    if (view === 'day') {
      setRelated([]);
      return;
    }
    let alive = true;
    void (async () => {
      try {
        const list = await api<any[]>(
          `/api/summaries?from=${period.from}&to=${period.to}&limit=80`,
        );
        if (alive) {
          setRelated(
            (list ?? []).filter(
              (s) => !(s.dateFrom === period.from && s.dateTo === period.to),
            ),
          );
        }
      } catch {
        if (alive) setRelated([]);
      }
    })();
    return () => { alive = false; };
  }, [view, period.from, period.to]);

  /* ---------------- 交互 ---------------- */
  const regenerate = async (force = true) => {
    setBusy(true);
    try {
      await createSummary(force);
      setSummary(null);
      setMemState('waiting');
      toast.push('已提交整理任务', 'success');
    } catch (e: any) {
      toast.push(e?.message ?? '操作失败', 'error');
    } finally {
      setBusy(false);
    }
  };

  const shift = (dir: number) => {
    if (view === 'day') setAnchor(shiftDate(anchor, dir));
    else if (view === 'week') setAnchor(shiftDate(anchor, dir * 7));
    else if (view === 'month') shiftMonth(dir);
    else setAnchor(`${Number(anchor.slice(0, 4)) + dir}-01-01`);
    setSlide((s) => ({ dir: dir > 0 ? 1 : -1, key: (s?.key ?? 0) + 1 }));
  };

  const shiftMonth = (delta: number) => {
    const d = parseDate(anchor);
    d.setDate(1);
    d.setMonth(d.getMonth() + delta);
    setAnchor(toStr(d));
  };

  const openDay = (date: string) => {
    setAnchor(date);
    setView('day');
  };

  /* ---------------- 移动端左右滑动切换 ---------------- */
  const touchStart = useRef<{ x: number; y: number } | null>(null);

  const onTouchStart = (e: React.TouchEvent) => {
    const t = e.touches[0];
    touchStart.current = t ? { x: t.clientX, y: t.clientY } : null;
  };

  const onTouchEnd = (e: React.TouchEvent) => {
    const start = touchStart.current;
    touchStart.current = null;
    const t = e.changedTouches[0];
    if (!start || !t) return;
    const dx = t.clientX - start.x;
    const dy = t.clientY - start.y;
    // 只在明显的横向滑动时翻页，避免和纵向滚动、点按冲突
    if (Math.abs(dx) < 50 || Math.abs(dx) < Math.abs(dy) * 1.5) return;
    shift(dx < 0 ? 1 : -1);
  };

  useEffect(() => {
    const next = new URLSearchParams(params);
    next.set('view', view);
    next.set('date', anchor);
    setParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, anchor]);

  /* ---------------- 日历面板 ---------------- */
  const anchorDate = parseDate(anchor);
  const monthCells = useMemo(
    () => buildMonthGrid(anchorDate.getFullYear(), anchorDate.getMonth() + 1),
    [anchor],
  );

  const legend = (
    <div className="mt-3 pt-3 border-t border-line flex items-center gap-3 text-[10.5px] text-muted px-1">
      <span className="inline-flex items-center gap-1">
        <span className="h-1.5 w-1.5 rounded-full bg-brand/60" /> 有录音
      </span>
      <span className="inline-flex items-center gap-1">
        <Sparkles size={9} className="text-brand" /> 已整理
      </span>
    </div>
  );

  const renderCalendar = () => {
    if (view === 'day') {
      return (
        <div className="card p-3 md:p-4">
          <div className="flex items-center justify-between px-1 pb-2">
            <div className="text-[12.5px] font-medium tabular-nums">
              {anchorDate.getFullYear()} 年 {anchorDate.getMonth() + 1} 月
            </div>
            <div className="flex items-center gap-0.5">
              <button
                onClick={() => shiftMonth(-1)}
                className="h-7 w-7 grid place-items-center rounded-lg hover:bg-panel2 text-muted transition-colors"
              >
                <ChevronLeft size={15} />
              </button>
              <button
                onClick={() => shiftMonth(1)}
                className="h-7 w-7 grid place-items-center rounded-lg hover:bg-panel2 text-muted transition-colors"
              >
                <ChevronRight size={15} />
              </button>
            </div>
          </div>

          <div className="grid grid-cols-7 mb-1">
            {WEEK_LABELS.map((w) => (
              <div key={w} className="text-center text-[10.5px] text-muted/70 py-1.5 font-medium">
                {w}
              </div>
            ))}
          </div>
          <div className="grid grid-cols-7 gap-1">
            {monthCells.map((date, i) => {
              if (!date) return <div key={`e${i}`} className="aspect-square" />;
              const info = dayMap.get(date);
              const isToday = date === todayStr();
              const isSel = date === anchor;
              const hasRec = (info?.recordings ?? 0) > 0;
              const hasSum = info?.summaryStatus === 'ready';
              return (
                <button
                  key={date}
                  onClick={() => setAnchor(date)}
                  className={cn(
                    'relative aspect-square rounded-xl flex flex-col items-center justify-center transition-all',
                    isSel
                      ? 'bg-gradient-to-br from-[var(--brand)] to-[var(--brand-2)] text-white shadow-[0_8px_18px_-10px_var(--brand)]'
                      : isToday
                        ? 'bg-soft text-brand'
                        : hasRec
                          ? 'hover:bg-panel2 text-ink'
                          : 'text-muted/60 hover:bg-panel2',
                  )}
                >
                  <span className={cn('text-[13px] tabular-nums', isToday && !isSel && 'font-semibold')}>
                    {Number(date.slice(-2))}
                  </span>
                  {hasRec && (
                    <span className="absolute bottom-1.5 flex items-center gap-0.5">
                      {hasSum ? (
                        <Sparkles size={8} className={isSel ? 'text-white/90' : 'text-brand'} />
                      ) : (
                        <span className={cn('h-1 w-1 rounded-full', isSel ? 'bg-white/90' : 'bg-brand/60')} />
                      )}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
          {legend}
        </div>
      );
    }

    if (view === 'week') {
      const cells = Array.from({ length: 7 }, (_, i) => shiftDate(period.from, i));
      return (
        <div className="card p-3 md:p-4">
          <div className="grid grid-cols-7 gap-1">
            {cells.map((date) => {
              const info = dayMap.get(date);
              const isToday = date === todayStr();
              const isSel = date === anchor;
              const hasRec = (info?.recordings ?? 0) > 0;
              const hasSum = info?.summaryStatus === 'ready';
              return (
                <button
                  key={date}
                  onClick={() => openDay(date)}
                  title={info?.titles?.join(' · ') || '这天还没有录音'}
                  className={cn(
                    'rounded-xl py-2.5 flex flex-col items-center gap-1 transition-all',
                    isSel
                      ? 'bg-gradient-to-br from-[var(--brand)] to-[var(--brand-2)] text-white shadow-[0_8px_18px_-10px_var(--brand)]'
                      : isToday
                        ? 'bg-soft text-brand'
                        : hasRec
                          ? 'hover:bg-panel2 text-ink'
                          : 'text-muted/60 hover:bg-panel2',
                  )}
                >
                  <span className={cn('text-[10px]', isSel ? 'text-white/70' : 'text-muted')}>
                    {weekdayCn(date).replace('周', '')}
                  </span>
                  <span className={cn('text-[14px] tabular-nums leading-none', isToday && !isSel && 'font-semibold')}>
                    {Number(date.slice(-2))}
                  </span>
                  <span className="h-2 flex items-center">
                    {hasRec ? (
                      hasSum ? (
                        <Sparkles size={8} className={isSel ? 'text-white/90' : 'text-brand'} />
                      ) : (
                        <span className={cn('h-1 w-1 rounded-full', isSel ? 'bg-white/90' : 'bg-brand/60')} />
                      )
                    ) : null}
                  </span>
                </button>
              );
            })}
          </div>

          <div className="mt-3 pt-2 border-t border-line">
            {calLoading ? (
              <div className="py-3 grid place-items-center">
                <Loader2 size={14} className="animate-spin text-muted" />
              </div>
            ) : (
              <div className="space-y-0.5">
                {cells
                  .map((d) => dayMap.get(d))
                  .filter((d): d is CalendarDay => !!d && (d.recordings ?? 0) > 0)
                  .map((d) => (
                    <button
                      key={d.date}
                      onClick={() => openDay(d.date)}
                      className="w-full text-left flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-panel2 transition-colors"
                    >
                      <span className="text-[11.5px] text-muted tabular-nums shrink-0 w-10">
                        {Number(d.date.slice(5, 7))}/{Number(d.date.slice(8, 10))}
                      </span>
                      <span className="text-[12px] truncate flex-1">
                        {d.titles?.length ? d.titles.join(' · ') : `${d.recordings} 段录音`}
                      </span>
                      {d.summaryStatus === 'ready' && <Sparkles size={10} className="text-brand shrink-0" />}
                      <span className="text-[10.5px] text-muted shrink-0">{d.recordings} 段</span>
                    </button>
                  ))}
                {cells.every((d) => (dayMap.get(d)?.recordings ?? 0) === 0) && (
                  <div className="py-3 text-center text-[11.5px] text-muted">这一周还没有录音</div>
                )}
              </div>
            )}
          </div>
          <div className="mt-1 text-[10.5px] text-muted px-1">点某一天可以跳到那天的回忆</div>
        </div>
      );
    }

    if (view === 'month') {
      return (
        <div className="card p-3 md:p-4">
          <div className="flex items-center justify-between px-1 pb-2">
            <div className="text-[12.5px] font-medium tabular-nums">
              {anchorDate.getFullYear()} 年 {anchorDate.getMonth() + 1} 月
            </div>
            <div className="flex items-center gap-0.5">
              <button
                onClick={() => shiftMonth(-1)}
                className="h-7 w-7 grid place-items-center rounded-lg hover:bg-panel2 text-muted transition-colors"
              >
                <ChevronLeft size={15} />
              </button>
              <button
                onClick={() => shiftMonth(1)}
                className="h-7 w-7 grid place-items-center rounded-lg hover:bg-panel2 text-muted transition-colors"
              >
                <ChevronRight size={15} />
              </button>
            </div>
          </div>

          <div className="grid grid-cols-7 mb-1">
            {WEEK_LABELS.map((w) => (
              <div key={w} className="text-center text-[10.5px] text-muted/70 py-1.5 font-medium">
                {w}
              </div>
            ))}
          </div>
          <div className="grid grid-cols-7 gap-1">
            {monthCells.map((date, i) => {
              if (!date) return <div key={`e${i}`} className="aspect-square" />;
              const info = dayMap.get(date);
              const isToday = date === todayStr();
              const hasRec = (info?.recordings ?? 0) > 0;
              const hasSum = info?.summaryStatus === 'ready';
              return (
                <button
                  key={date}
                  onClick={() => openDay(date)}
                  title={info?.titles?.join(' · ') || '这天还没有录音'}
                  className={cn(
                    'relative aspect-square rounded-xl flex flex-col items-center justify-center transition-all',
                    isToday
                      ? 'bg-soft text-brand font-semibold'
                      : hasRec
                        ? 'hover:bg-panel2 text-ink'
                        : 'text-muted/60 hover:bg-panel2',
                  )}
                >
                  <span className="text-[13px] tabular-nums">{Number(date.slice(-2))}</span>
                  {hasRec && (
                    <span className="absolute bottom-1.5 flex items-center gap-0.5">
                      {hasSum ? (
                        <Sparkles size={8} className="text-brand" />
                      ) : (
                        <span className="h-1 w-1 rounded-full bg-brand/60" />
                      )}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
          {legend}
          <div className="mt-1 text-[10.5px] text-muted px-1">点某一天可以跳到那天的回忆</div>
        </div>
      );
    }

    // 年视图
    const year = Number(anchor.slice(0, 4));
    const perMonth = Array.from({ length: 12 }, (_, i) => {
      const prefix = `${year}-${pad(i + 1)}`;
      let count = 0;
      let duration = 0;
      let summarized = 0;
      for (const d of days) {
        if (!d.date.startsWith(prefix)) continue;
        count += d.recordings ?? 0;
        duration += d.durationMs ?? 0;
        if (d.summaryStatus === 'ready') summarized += 1;
      }
      return { month: i + 1, count, duration, summarized };
    });

    return (
      <div className="card p-3 md:p-4">
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
          {perMonth.map((m) => {
            const cells = buildMonthGrid(year, m.month);
            return (
              <button
                key={m.month}
                onClick={() => {
                  const first = days.find(
                    (d) => d.date.startsWith(`${year}-${pad(m.month)}`) && (d.recordings ?? 0) > 0,
                  );
                  openDay(first?.date ?? `${year}-${pad(m.month)}-01`);
                }}
                className={cn(
                  'rounded-xl border border-line/70 p-2.5 text-left transition-all hover:border-brand/40 hover:shadow-soft',
                  m.count === 0 && 'opacity-55',
                )}
              >
                <div className="flex items-baseline justify-between">
                  <span className="text-[12.5px] font-medium tabular-nums">{m.month} 月</span>
                  <span className="text-[10.5px] text-muted tabular-nums">{m.count} 段</span>
                </div>
                <div className="grid grid-cols-7 gap-[3px] mt-2">
                  {cells.map((date, i) => {
                    if (!date) return <span key={`e${i}`} className="h-[5px]" />;
                    const info = dayMap.get(date);
                    const hasRec = (info?.recordings ?? 0) > 0;
                    const hasSum = info?.summaryStatus === 'ready';
                    return (
                      <span
                        key={date}
                        className={cn(
                          'h-[5px] w-[5px] rounded-full',
                          hasSum ? 'bg-brand' : hasRec ? 'bg-brand/35' : 'bg-line',
                        )}
                      />
                    );
                  })}
                </div>
              </button>
            );
          })}
        </div>
        {legend}
      </div>
    );
  };

  /* ---------------- 回忆正文 ---------------- */
  const scopeLabel =
    view === 'year' ? '年度回忆' : view === 'month' ? '这个月的回忆' : view === 'week' ? '这一周的回忆' : '这一天的回忆';

  const renderMemory = () => {
    if (memState === 'loading') {
      return (
        <div className="card p-5 space-y-2.5 animate-pulse">
          <div className="h-2.5 w-24 rounded-full bg-panel2" />
          <div className="h-2.5 w-3/4 rounded-full bg-panel2" />
          <div className="h-2.5 w-2/3 rounded-full bg-panel2" />
          <div className="h-2.5 w-1/2 rounded-full bg-panel2" />
        </div>
      );
    }

    if (memState === 'waiting') {
      return (
        <div className="card p-5">
          <div className="flex items-center gap-2 text-[12px] font-medium text-brand">
            <Loader2 size={13} className="animate-spin" />
            {view === 'day' ? '正在整理这段回忆' : '正在准备下级回忆'}
          </div>
          <div className="mt-3.5 space-y-2 animate-pulse">
            <div className="h-2.5 rounded-full bg-panel2 w-[82%]" />
            <div className="h-2.5 rounded-full bg-panel2 w-[68%]" />
            <div className="h-2.5 rounded-full bg-panel2 w-[74%]" />
            <div className="h-2.5 rounded-full bg-panel2 w-[46%]" />
          </div>
          <p className="text-[11.5px] text-muted mt-4 leading-relaxed">
            {view === 'day'
              ? stats.count > 0
                ? `这段时间有 ${stats.count} 段录音，AI 正在读完转写并归纳要点。`
                : 'AI 正在归纳要点。'
              : '周/月/年回忆由下级回忆逐层汇总：先把缺的日报、月报补齐，再用它们归纳。第一次需要等一会儿，之后重新整理会快很多。'}
            生成完成后会自动出现在这里，你可以先去别处，稍后回来查看。
          </p>
        </div>
      );
    }

    if (memState === 'failed') {
      return (
        <div className="card p-5">
          <div className="flex items-start gap-2.5">
            <AlertCircle size={16} className="text-red-500 mt-0.5 shrink-0" />
            <div className="min-w-0">
              <div className="text-[13px] font-medium">这次没有整理成功</div>
              <div className="text-[11.5px] text-muted mt-1 break-words leading-relaxed">
                {summary?.error || '未知错误'}
              </div>
              <button
                onClick={() => void regenerate(true)}
                disabled={busy}
                className="mt-2.5 inline-flex items-center gap-1.5 text-[12px] text-brand hover:underline disabled:opacity-50"
              >
                <RefreshCw size={12} /> 重新整理
              </button>
            </div>
          </div>
        </div>
      );
    }

    if (memState === 'ready' && summary) {
      const hl = summary.stats?.highlights ?? [];
      return (
        <div className="card overflow-hidden animate-fade-up">
          <div className="p-4 md:p-5 border-b border-line/70">
            <div className="flex items-start justify-between gap-2">
              <div className="flex items-center gap-1.5 text-[11px] font-medium text-brand">
                <Sparkles size={11} /> {scopeLabel}
              </div>
              <div className="flex items-center gap-0.5 shrink-0">
                <button
                  onClick={() => void regenerate(true)}
                  disabled={busy}
                  title="重新整理"
                  className="h-7 w-7 grid place-items-center rounded-lg text-muted hover:bg-panel2 hover:text-ink disabled:opacity-40"
                >
                  <RefreshCw size={13} className={cn(busy && 'animate-spin')} />
                </button>
                <Link
                  to={`/summaries/${summary.id}`}
                  state={{ from: here }}
                  title="全文"
                  className="h-7 w-7 grid place-items-center rounded-lg text-muted hover:bg-panel2 hover:text-ink"
                >
                  <ArrowUpRight size={14} />
                </Link>
              </div>
            </div>
            <h2 className="mt-2 text-[16px] font-semibold leading-snug">{summary.title}</h2>
            <div className="mt-1.5 flex items-center gap-1.5 text-[11.5px] text-muted flex-wrap">
              <span>{summary.stats?.recordings ?? stats.count} 段录音</span>
              <span className="opacity-40">·</span>
              <span>{fmtDurationCn(summary.stats?.durationMs ?? stats.duration)}</span>
              <span className="opacity-40">·</span>
              <span>更新于 {fmtTime(summary.updatedAt)}</span>
            </div>
          </div>

          <div className="p-4 md:p-5">
            {hl.length > 0 && (
              <div className="rounded-xl bg-soft/50 p-3 mb-4">
                <ul className="space-y-1.5">
                  {hl.slice(0, 5).map((h, i) => (
                    <li key={i} className="text-[12.5px] leading-relaxed flex gap-2">
                      <span className="text-brand/50 shrink-0">·</span>
                      <span>{h}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <div className="relative">
              <div className="max-h-[340px] overflow-hidden">
                <MarkdownView markdown={summary.markdown ?? ''} />
              </div>
              <div className="pointer-events-none absolute inset-x-0 bottom-0 h-24 bg-gradient-to-t from-panel via-panel/80 to-transparent" />
              <Link
                to={`/summaries/${summary.id}`}
                state={{ from: here }}
                className="absolute inset-x-0 bottom-1 flex justify-center"
              >
                <span className="inline-flex items-center gap-1.5 h-8 px-3.5 rounded-full bg-panel/95 border border-line text-[12px] text-ink shadow-soft backdrop-blur">
                  阅读全文 <ArrowUpRight size={12} />
                </span>
              </Link>
            </div>

            <SummaryMcpActions summaryId={summary.id} compact className="mt-4" />
          </div>
        </div>
      );
    }

    // 还没有记录（日视图以实际明细兜底，避免日历聚合与明细不一致时出现
    // 「这一天没有录音」却下面列着录音的自相矛盾）
    const empty = view === 'day' ? stats.count === 0 && records.length === 0 : stats.count === 0;
    if (empty) {
      return (
        <div className="card p-8 text-center">
          <BookOpen size={20} className="text-muted mx-auto mb-2" />
          <div className="text-[13px] text-muted">
            {view === 'day' ? '这一天没有录音' : view === 'week' ? '这一周没有录音' : view === 'month' ? '这个月没有录音' : '这一年还没有录音'}
          </div>
          <Link
            to="/record"
            className="inline-flex items-center gap-1 text-[12px] text-brand mt-2 hover:underline"
          >
            <Plus size={12} /> 录一段
          </Link>
        </div>
      );
    }

    // 有录音、但还没整理（年视图需要手动触发）
    return (
      <div className="card p-5">
        <div className="flex items-center gap-1.5 text-[11px] font-medium text-brand">
          <Sparkles size={11} /> {scopeLabel}
        </div>
        <div className="mt-2 text-[14px] font-medium">
          {view === 'year' ? '还没有生成年度回忆' : '这段时间还没有整理过'}
        </div>
        <p className="text-[12px] text-muted mt-1.5 leading-relaxed">
          {view === 'year'
            ? `${anchor.slice(0, 4)} 年共有 ${stats.count} 段录音。年度回忆基于 12 份月报汇总，缺的月报和日报会先自动补齐；首次需要等一会儿，之后重新整理很快。需要手动触发。`
            : '点击下面的按钮开始整理。'}
        </p>
        <button
          onClick={() => void regenerate(true)}
          disabled={busy}
          className="btn-primary h-9 px-4 text-[12.5px] mt-3"
        >
          {busy ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
          开始整理
        </button>
      </div>
    );
  };

  const relatedVisible = related.filter((s) => s.status !== 'generating');

  return (
    <div
      className="max-w-5xl mx-auto px-4 md:px-8 py-6 md:py-10"
      onTouchStart={onTouchStart}
      onTouchEnd={onTouchEnd}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-[24px] md:text-[28px] font-semibold tracking-tight">回忆</h1>
          <p className="text-[12.5px] text-muted mt-1">
            {stats.count} 段录音 · {fmtDurationCn(stats.duration)} · {stats.activeDays} 天有记录
          </p>
        </div>
        <div className="flex rounded-xl bg-panel2 p-0.5 shrink-0">
          {VIEWS.map((v) => (
            <button
              key={v.key}
              onClick={() => setView(v.key)}
              className={cn(
                'h-7 px-3 rounded-[9px] text-[12.5px] font-medium transition-colors',
                view === v.key ? 'bg-panel text-ink shadow-sm' : 'text-muted hover:text-ink',
              )}
            >
              {v.label}
            </button>
          ))}
        </div>
      </div>

      <div className="mt-5 flex items-center gap-1">
        <button
          onClick={() => shift(-1)}
          className="h-8 w-8 grid place-items-center rounded-lg hover:bg-panel2 text-muted transition-colors"
        >
          <ChevronLeft size={17} />
        </button>
        <div className="text-[13px] font-medium tabular-nums px-1">{labelOf(view, period.from, period.to)}</div>
        <button
          onClick={() => shift(1)}
          className="h-8 w-8 grid place-items-center rounded-lg hover:bg-panel2 text-muted transition-colors"
        >
          <ChevronRight size={17} />
        </button>
        <button
          onClick={() => setAnchor(todayStr())}
          className="ml-1.5 h-7 px-2.5 rounded-lg text-[12px] text-muted hover:text-ink hover:bg-panel2 transition-colors"
        >
          今天
        </button>
        <button
          onClick={() => setCalOpen((v) => !v)}
          title={calOpen ? '收起日历' : '展开日历'}
          className={cn(
            'ml-auto h-7 px-2.5 rounded-lg text-[12px] inline-flex items-center gap-1.5 transition-colors',
            calOpen ? 'text-ink bg-panel2' : 'text-muted hover:text-ink hover:bg-panel2',
          )}
        >
          <CalendarDays size={13} />
          日历
          <ChevronDown size={13} className={cn('transition-transform', calOpen && 'rotate-180')} />
        </button>
      </div>

      <div
        key={`page-${slide?.key ?? 0}`}
        className={cn(
          'mt-4 grid gap-5 items-start',
          calOpen && 'lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]',
          slide && (slide.dir > 0 ? 'animate-slide-in-right' : 'animate-slide-in-left'),
        )}
      >
        {calOpen && <div>{renderCalendar()}</div>}

        <div className="space-y-3">
          {renderMemory()}

          {view === 'day' && records.length > 0 && (
            <RecordingList items={records} onDelete={removeRecording} />
          )}

          {view === 'day' && records.length > 0 && (
            <div className="text-[11px] text-muted text-center pt-0.5 flex items-center justify-center gap-1">
              <Clock size={10} />
              共 {fmtDurationCn(records.reduce((a, r) => a + r.durationMs, 0))}
            </div>
          )}

          {view !== 'day' && relatedVisible.length > 0 && (
            <div className="space-y-2">
              <div className="flex items-center gap-2 px-1">
                <span className="text-[11.5px] text-muted">
                  {view === 'year' ? '这一年已经整理过的回忆' : view === 'month' ? '这个月已经整理过的回忆' : '这一周已经整理过的回忆'}
                </span>
                <div className="flex-1 h-px bg-line" />
              </div>
              {relatedVisible.slice(0, 12).map((s) => (
                <Link
                  key={s.id}
                  to={`/summaries/${s.id}`}
                  state={{ from: here }}
                  className="card p-3 flex items-center gap-2.5 hover:border-brand/30 hover:shadow-soft transition-all"
                >
                  <Sparkles size={12} className="text-brand shrink-0" />
                  <div className="min-w-0 flex-1">
                    <div className="text-[12.5px] font-medium truncate">{s.title}</div>
                    <div className="text-[10.5px] text-muted mt-0.5 tabular-nums">
                      {s.dateFrom === s.dateTo ? s.dateFrom : `${s.dateFrom} - ${s.dateTo}`}
                      {s.status === 'ready' ? '' : ' · 生成中'}
                    </div>
                  </div>
                  <ArrowUpRight size={13} className="text-muted shrink-0" />
                </Link>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
