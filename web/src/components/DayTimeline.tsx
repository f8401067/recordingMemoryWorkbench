import React, { useMemo, useRef, useState } from 'react';
import {
  Clock3, Coffee, MoonStar, Soup, Sunrise, UtensilsCrossed, type LucideIcon,
} from 'lucide-react';
import type { Recording } from '../lib/api';
import { cn, fmtTimeRange } from '../lib/utils';

/** 轨道横轴：05:00 → 次日 05:00（凌晨算「后半夜」，排在轨道末尾） */
const AXIS_START = 5;
const AXIS_HOURS = 24;
const AXIS_MINUTES = AXIS_HOURS * 60;

type Mark = { key: string; label: string; icon: LucideIcon; hour: number };

/** 一天里的几个生活地标：只作参照，不能点击 */
const DAY_MARKS: Mark[] = [
  { key: 'wake', label: '起床', icon: Sunrise, hour: 6 },
  { key: 'breakfast', label: '早餐', icon: Coffee, hour: 8.5 },
  { key: 'lunch', label: '中餐', icon: UtensilsCrossed, hour: 12.5 },
  { key: 'dinner', label: '晚餐', icon: Soup, hour: 18.5 },
  { key: 'sleep', label: '睡觉', icon: MoonStar, hour: 23 },
];

/** 钟点 → 轨道位置（0 = 05:00，1 = 次日 05:00） */
const ratioOfHour = (hour: number) =>
  ((((hour - AXIS_START) % AXIS_HOURS) + AXIS_HOURS) % AXIS_HOURS) / AXIS_HOURS;

const ratioOfIso = (iso: string) => {
  const d = new Date(iso);
  // 精确到毫秒：几十秒的短录音如果只按分钟取整，起止会算出同一个位置
  return ratioOfHour(d.getHours() + d.getMinutes() / 60 + d.getSeconds() / 3600 + d.getMilliseconds() / 3.6e6);
};

/** 距 05:00 多少分钟，用来算离滑点最近的那段录音 */
const minutesOfIso = (iso: string) => {
  const d = new Date(iso);
  return (((d.getHours() + d.getMinutes() / 60 - AXIS_START) % AXIS_HOURS) + AXIS_HOURS) % AXIS_HOURS * 60;
};

function clockOf(ratio: number) {
  const minutes = (((AXIS_START + ratio * AXIS_HOURS) % AXIS_HOURS) * 60);
  const snapped = Math.round(minutes / 5) * 5;
  const h = Math.floor(snapped / 60) % 24;
  const m = snapped % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

/** 首尾留半个滑点的空白，免得圆形贴到卡片边框 */
const INSET = 15;
const posAt = (ratio: number) => `calc(${INSET}px + (100% - ${INSET * 2}px) * ${ratio})`;
const widthAt = (ratio: number) => `calc((100% - ${INSET * 2}px) * ${ratio})`;

/** 一段录音在轨道上占的时间区间（跨过 05:00 轴点时会被拆成两段） */
type Span = { key: string; from: number; to: number };

function spansOf(items: Recording[]): Span[] {
  const out: Span[] = [];
  for (const r of items) {
    const startMs = new Date(r.startedAt).getTime();
    const endMs = r.endedAt ? new Date(r.endedAt).getTime() : startMs + (r.durationMs || 0);
    const from = ratioOfIso(r.startedAt);
    const to = ratioOfIso(new Date(endMs).toISOString());
    const crossesAxis = from > 0.5 && to < 0.5;
    if (endMs <= startMs || (to <= from && !crossesAxis)) {
      // 时长为 0 / 起止落在同一毫秒：留一个最小宽度的点
      out.push({ key: r.id, from, to: from });
    } else if (!crossesAxis) {
      out.push({ key: r.id, from, to });
    } else {
      // 起点在 05:00 之前、终点在之后：分成「尾段 + 头段」
      out.push({ key: `${r.id}-tail`, from, to: 1 });
      out.push({ key: `${r.id}-head`, from: 0, to });
    }
  }
  return out;
}

type Props = {
  /** 当天的录音：用来在轨道上标点，并找离滑点最近的那一段 */
  items: Recording[];
  /** 松手 / 键盘调整后滚到对应录音 */
  onJump: (r: Recording) => void;
};

export default function DayTimeline({ items, onJump }: Props) {
  const [ratio, setRatio] = useState(() => {
    const now = new Date();
    return ratioOfHour(now.getHours() + now.getMinutes() / 60);
  });
  const [dragging, setDragging] = useState(false);
  const railRef = useRef<HTMLDivElement>(null);

  const spans = useMemo(() => spansOf(items), [items]);

  /** 离滑点最近的一段正常录音（没内容的不参与，免得总是跳到「无有效内容」） */
  const nearest = (r: number): Recording | null => {
    const normal = items.filter((x) => !x.lowValue);
    const list = normal.length ? normal : items;
    if (!list.length) return null;
    const target = r * AXIS_MINUTES;
    let best = list[0];
    let bestGap = Infinity;
    for (const x of list) {
      const gap = Math.abs(minutesOfIso(x.startedAt) - target);
      if (gap < bestGap) {
        bestGap = gap;
        best = x;
      }
    }
    return best;
  };

  const target = nearest(ratio);

  const pick = (clientX: number) => {
    const box = railRef.current?.getBoundingClientRect();
    if (!box || box.width <= INSET * 2) return;
    const r = (clientX - box.left - INSET) / (box.width - INSET * 2);
    setRatio(Math.min(1, Math.max(0, r)));
  };

  const commit = () => {
    setDragging(false);
    const t = nearest(ratio);
    if (t) onJump(t);
  };

  const handleKey = (e: React.KeyboardEvent) => {
    const step = e.key === 'ArrowLeft' ? -1 : e.key === 'ArrowRight' ? 1 : 0;
    if (!step) return;
    e.preventDefault();
    const next = Math.min(1, Math.max(0, ratio + step * (0.5 / AXIS_HOURS)));
    setRatio(next);
    const t = nearest(next);
    if (t) onJump(t);
  };

  return (
    <div className="rounded-2xl border border-line bg-panel2/50 px-3.5 pb-2.5 pt-2.5">
      <div className="flex items-center justify-between gap-2">
        <span className="inline-flex items-center gap-1.5 text-[11px] text-muted">
          <Clock3 size={11} />
          拖动滑块，跳到对应时间的录音
        </span>
        <span className="text-[11px] font-medium tabular-nums text-ink">{clockOf(ratio)}</span>
      </div>

      <div
        ref={railRef}
        role="slider"
        tabIndex={0}
        aria-label="拖动到某段录音所在的时间"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(ratio * 100)}
        aria-valuetext={`${clockOf(ratio)}${target ? `，最近一段是 ${fmtTimeRange(target)}` : ''}`}
        className={cn(
          'relative mt-2 h-[68px] touch-none select-none cursor-grab rounded-lg outline-none',
          'focus-visible:ring-2 focus-visible:ring-brand/20',
          dragging && 'cursor-grabbing',
        )}
        onPointerDown={(e) => {
          e.currentTarget.setPointerCapture(e.pointerId);
          setDragging(true);
          pick(e.clientX);
        }}
        onPointerMove={(e) => {
          if (dragging) pick(e.clientX);
        }}
        onPointerUp={commit}
        onPointerCancel={() => setDragging(false)}
        onKeyDown={handleKey}
      >
        {/* 轨道底色：没有录音的时间段统一显示为中性灰（不用主题色，避免被染成偏紫） */}
        <div
          className="absolute top-[7px] h-[6px] -translate-y-1/2 rounded-full bg-zinc-400/70 dark:bg-zinc-500/60"
          style={{ left: INSET, right: INSET }}
        />

        {/* 每段录音在轨道上占的时间区间（开始 → 结束），有录音的才上色 */}
        {spans.map((s) => (
          <span
            key={s.key}
            className="absolute top-[7px] h-[6px] -translate-y-1/2 rounded-full bg-gradient-to-r from-brand to-brand2"
            style={{ left: posAt(s.from), width: widthAt(s.to - s.from), minWidth: 4 }}
          />
        ))}

        {/* 滑点 */}
        <span
          className={cn(
            'absolute top-[7px] h-[15px] w-[15px] -translate-x-1/2 -translate-y-1/2 rounded-full',
            'border-2 border-panel bg-brand shadow-[0_1px_5px_rgba(16,18,28,.28)] transition-[width,height] duration-150',
            dragging && 'h-[17px] w-[17px]',
          )}
          style={{ left: posAt(ratio) }}
        />

        {/* 生活地标（纯展示） */}
        {DAY_MARKS.map((m) => {
          const Icon = m.icon;
          return (
            <div
              key={m.key}
              aria-hidden
              className="pointer-events-none absolute top-[20px] flex w-14 -translate-x-1/2 flex-col items-center"
              style={{ left: posAt(ratioOfHour(m.hour)) }}
            >
              <div className="grid h-7 w-7 place-items-center rounded-full border border-line bg-panel2 text-muted/70">
                <Icon size={13} />
              </div>
              <span className="mt-1 whitespace-nowrap text-[10.5px] leading-none text-muted/65">
                {m.label}
              </span>
            </div>
          );
        })}
      </div>

      <div className="flex min-w-0 items-center gap-1.5 text-[11px] text-muted">
        <span className="shrink-0">最近一段</span>
        {target ? (
          <>
            <span className="shrink-0 tabular-nums text-ink/80">{fmtTimeRange(target)}</span>
            <span className="truncate">{target.title}</span>
          </>
        ) : (
          <span>今天还没有录音</span>
        )}
      </div>
    </div>
  );
}
