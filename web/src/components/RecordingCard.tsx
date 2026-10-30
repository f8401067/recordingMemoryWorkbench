import React from 'react';
import { Link } from 'react-router-dom';
import { AlertCircle, CheckCircle2, FileText, Loader2, Mic, Sparkles } from 'lucide-react';
import type { Recording } from '../lib/api';
import { cn, fmtDurationCn, fmtTime } from '../lib/utils';

function StatusPill({ r }: { r: Recording }) {
  if (r.status === 'recording') {
    return (
      <span className="chip bg-red-500/10 text-red-500">
        <span className="h-1.5 w-1.5 rounded-full bg-red-500 animate-pulse" />
        录音中
      </span>
    );
  }
  if (r.status === 'processing') {
    return (
      <span className="chip bg-amber-500/10 text-amber-600 dark:text-amber-400">
        <Loader2 size={11} className="animate-spin" />
        转写 {r.doneChunks}/{r.chunkCount}
      </span>
    );
  }
  if (r.status === 'failed') {
    return (
      <span className="chip bg-red-500/10 text-red-500">
        <AlertCircle size={11} />
        转写失败
      </span>
    );
  }
  return (
    <span className="chip bg-emerald-500/10 text-emerald-600 dark:text-emerald-400">
      <CheckCircle2 size={11} />
      已完成
    </span>
  );
}

export default function RecordingCard({ r }: { r: Recording }) {
  const hasSummary = r.summaryStatus === 'ready';
  const summaryBusy = r.summaryStatus === 'queued' || r.summaryStatus === 'generating';
  const excerpt = (r.excerpt || '').trim();
  const words = r.charCount > 0 ? `约 ${Math.round(r.charCount / 100) / 10} 千字` : '';

  return (
    <Link
      to={`/recordings/${r.id}`}
      className="group block card p-4 hover:shadow-soft hover:border-brand/30 transition-all duration-200 active:scale-[.995]"
    >
      <div className="flex items-start gap-3">
        <div
          className={cn(
            'h-9 w-9 shrink-0 rounded-xl grid place-items-center border',
            hasSummary
              ? 'bg-soft border-brand/20 text-brand'
              : 'bg-panel2 border-line text-muted',
          )}
        >
          {hasSummary ? <Sparkles size={15} /> : <FileText size={15} />}
        </div>

        <div className="flex-1 min-w-0">
          <div className="flex items-start justify-between gap-2">
            <h3 className="text-[14.5px] font-medium leading-snug truncate group-hover:text-brand transition-colors">
              {r.title}
            </h3>
            <div className="shrink-0">
              <StatusPill r={r} />
            </div>
          </div>

          {/* 主要内容：列表里最该看的信息放最前面 */}
          <p
            className={cn(
              'mt-1.5 text-[12.5px] leading-[1.65] line-clamp-2',
              excerpt ? 'text-muted' : 'text-muted/70',
            )}
          >
            {excerpt ||
              (r.status === 'processing'
                ? '正在转写，文字稍后出现在这里…'
                : r.status === 'recording'
                  ? '录音进行中…'
                  : r.status === 'failed'
                    ? '这段录音转写失败，可进入详情重试'
                    : '暂无转写文字')}
          </p>

          <div className="mt-2 flex items-center gap-2 text-[11px] text-muted/80 flex-wrap">
            <span>{fmtDurationCn(r.durationMs)}</span>
            {words && (
              <>
                <span className="opacity-40">·</span>
                <span>{words}</span>
              </>
            )}
            {(r.source === 'api' || r.source === 'device') && (
              <>
                <span className="opacity-40">·</span>
                <span className="chip bg-panel2 text-muted !px-1.5 !py-0">
                  {r.source === 'device' ? '设备' : 'API'}
                </span>
              </>
            )}
            {summaryBusy && (
              <span className="chip bg-amber-500/10 text-amber-600 dark:text-amber-400 !px-1.5 !py-0">
                <Loader2 size={9} className="animate-spin" />
                生成纪要
              </span>
            )}
            <span className="ml-auto tabular-nums opacity-55">{fmtTime(r.startedAt)}</span>
          </div>

          {hasSummary && r.summaryTitle && (
            <div className="mt-1.5 text-[11px] text-muted/70 truncate flex items-center gap-1">
              <Sparkles size={10} className="text-brand/60 shrink-0" />
              {r.summaryTitle}
            </div>
          )}
        </div>
      </div>
    </Link>
  );
}

export function EmptyState({
  title,
  desc,
  action,
}: {
  title: string;
  desc: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="card p-10 flex flex-col items-center text-center">
      <div className="h-14 w-14 rounded-2xl bg-panel2 grid place-items-center mb-4">
        <Mic size={22} className="text-muted" />
      </div>
      <div className="text-sm font-medium">{title}</div>
      <div className="text-xs text-muted mt-1.5 max-w-xs leading-relaxed">{desc}</div>
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}
