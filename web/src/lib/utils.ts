import clsx, { type ClassValue } from 'clsx';

export const cn = (...args: ClassValue[]) => clsx(...args);

/** 毫秒 → 01:23 / 1:02:03 */
export function fmtDuration(ms: number, withHours = false): string {
  const total = Math.max(0, Math.floor((ms || 0) / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const p = (n: number) => String(n).padStart(2, '0');
  if (h > 0 || withHours) return `${h}:${p(m)}:${p(s)}`;
  return `${p(m)}:${p(s)}`;
}

/** 毫秒 → 12 分钟 / 1 小时 20 分钟 */
export function fmtDurationCn(ms: number): string {
  const total = Math.floor((ms || 0) / 1000);
  if (total < 60) return `${total} 秒`;
  const m = Math.round(total / 60);
  if (m < 60) return `${m} 分钟`;
  const h = Math.floor(m / 60);
  const rest = m % 60;
  return rest ? `${h} 小时 ${rest} 分钟` : `${h} 小时`;
}

export function fmtTime(iso: string): string {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/** 一段录音的开始 → 结束时间；还在录时显示「录音中」 */
export function fmtTimeRange(r: {
  startedAt: string;
  endedAt?: string | null;
  durationMs?: number;
  status?: string;
}): string {
  const start = fmtTime(r.startedAt);
  if (r.status === 'recording') return `${start} – 录音中`;
  const endIso = r.endedAt
    ?? (r.durationMs ? new Date(new Date(r.startedAt).getTime() + r.durationMs).toISOString() : null);
  if (!endIso) return start;
  const end = fmtTime(endIso);
  return end === start ? start : `${start} – ${end}`;
}

export function fmtDate(iso: string): string {
  const d = new Date(iso);
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

export function todayStr(d = new Date()): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

export function friendlyDay(dateStr: string): string {
  const today = todayStr();
  const y = new Date();
  y.setDate(y.getDate() - 1);
  const yesterday = todayStr(y);
  if (dateStr === today) return '今天';
  if (dateStr === yesterday) return '昨天';
  const d = new Date(`${dateStr}T00:00:00`);
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

export function weekdayCn(dateStr: string): string {
  const d = new Date(`${dateStr}T00:00:00`);
  return ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][d.getDay()];
}

export function statusText(s: string): string {
  switch (s) {
    case 'recording': return '录音中';
    case 'processing': return '转写中';
    case 'ready': return '已完成';
    case 'failed': return '失败';
    default: return s;
  }
}

export function statusTone(s: string): 'gray' | 'amber' | 'green' | 'red' | 'brand' {
  switch (s) {
    case 'recording': return 'red';
    case 'processing': return 'amber';
    case 'ready': return 'green';
    case 'failed': return 'red';
    default: return 'gray';
  }
}

export function copyText(text: string) {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text);
  const ta = document.createElement('textarea');
  ta.value = text;
  document.body.appendChild(ta);
  ta.select();
  document.execCommand('copy');
  document.body.removeChild(ta);
  return Promise.resolve();
}

export function download(filename: string, content: string, type = 'text/markdown') {
  const blob = new Blob([content], { type: `${type};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
