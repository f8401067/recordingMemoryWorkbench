import React, { useMemo, useState } from 'react';
import { ChevronDown, ChevronRight, VolumeX } from 'lucide-react';
import type { Recording } from '../lib/api';
import RecordingCard from './RecordingCard';
import { cn } from '../lib/utils';

type Props = {
  items: Recording[];
  onDelete?: (r: Recording) => void;
  /** 时间滑点跳过来的那一段，短暂高亮 */
  highlightId?: string | null;
};

/**
 * 当天录音列表：正常记录按时间排前面，没内容 / 只有噪声的排到最后并默认折叠，
 * 免得"空录那一下"把真内容挤下去。
 */
export default function RecordingList({ items, onDelete, highlightId }: Props) {
  const [expanded, setExpanded] = useState(false);

  const { normal, low } = useMemo(() => {
    const n: Recording[] = [];
    const l: Recording[] = [];
    for (const r of items) (r.lowValue ? l : n).push(r);
    return { normal: n, low: l };
  }, [items]);

  return (
    <div className="space-y-2.5">
      {normal.map((r) => (
        <RecordingCard key={r.id} r={r} onDelete={onDelete} highlight={r.id === highlightId} />
      ))}

      {low.length > 0 && (
        <div className="mt-1">
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className={cn(
              'w-full flex items-center gap-2 px-3 py-2 rounded-xl border border-dashed border-line',
              'text-[11.5px] text-muted/80 hover:text-ink hover:border-brand/30 transition-colors',
            )}
          >
            {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            <VolumeX size={12} className="shrink-0" />
            <span>
              {low.length} 段没有录到人声或没有实际内容
              <span className="opacity-60">{low[0]?.lowReason ? ` · ${low[0].lowReason}` : ''}</span>
            </span>
            <span className="ml-auto">{expanded ? '收起' : '展开'}</span>
          </button>

          {expanded && (
            <div className="mt-2.5 space-y-2.5 animate-fade-up">
              {low.map((r) => (
                <RecordingCard key={r.id} r={r} onDelete={onDelete} highlight={r.id === highlightId} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
