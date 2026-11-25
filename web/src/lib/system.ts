import { useCallback, useEffect, useRef, useState } from 'react';
import { getTaskBoard, type TaskBoard } from './api';

/** 任务看板轮询：设置页顶部的小提示和「任务」标签共用一份数据 */
export function useTaskBoard(intervalMs = 4000) {
  const [board, setBoard] = useState<TaskBoard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const busy = useRef(false);

  const refresh = useCallback(async () => {
    if (busy.current) return;
    busy.current = true;
    try {
      const next = await getTaskBoard();
      setBoard(next);
      setError(null);
    } catch (e: any) {
      setError(e?.message ?? '读取任务失败');
    } finally {
      busy.current = false;
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const t = window.setInterval(() => {
      // 切到后台就不轮询了，回来立即补一次
      if (!document.hidden) void refresh();
    }, intervalMs);
    const onVisible = () => {
      if (!document.hidden) void refresh();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      window.clearInterval(t);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [refresh, intervalMs]);

  return { board, error, loading, refresh };
}

/** 毫秒 → "12s" / "3m20s" / "1h05m" */
export function fmtElapsed(ms: number | null | undefined): string {
  const total = Math.max(0, Math.round((ms ?? 0) / 1000));
  if (total < 60) return `${total}s`;
  const m = Math.floor(total / 60);
  const s = total % 60;
  if (m < 60) return s ? `${m}m${String(s).padStart(2, '0')}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h${String(m % 60).padStart(2, '0')}m`;
}

/** ISO → 今天 14:03 / 昨天 14:03 / 10-30 14:03 */
export function fmtStamp(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  const today = new Date();
  const sameDay = (a: Date, b: Date) =>
    a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  if (sameDay(d, today)) return hm;
  const y = new Date(today);
  y.setDate(y.getDate() - 1);
  if (sameDay(d, y)) return `昨天 ${hm}`;
  return `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, '0')} ${hm}`;
}
