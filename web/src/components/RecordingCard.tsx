import React from 'react';
import { Link, useLocation } from 'react-router-dom';
import { AlertCircle, CheckCircle2, FileText, GitMerge, Loader2, Mic, Sparkles, Trash2, VolumeX } from 'lucide-react';
import type { Recording } from '../lib/api';
import { cn, fmtDurationCn, fmtTimeRange } from '../lib/utils';

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

export default function RecordingCard({
  r,
  onDelete,
  highlight = false,
}: {
  r: Recording;
  onDelete?: (r: Recording) => void;
  /** 从时间滑点跳过来时闪一下，方便一眼看到是哪段 */
  highlight?: boolean;
}) {
  // 记住从哪个页面点进来的，详情页返回时才能回到原处（如回忆页的具体日期/视图）
  const location = useLocation();
  const from = `${location.pathname}${location.search}`;
  const hasSummary = r.summaryStatus === 'ready';
  const summaryBusy = r.summaryStatus === 'queued' || r.summaryStatus === 'generating';
  const highlights = hasSummary ? (r.summaryHighlights ?? []).filter(Boolean).slice(0, 3) : [];
  const highlightsHint = r.lowValue
    ? '没有可整理的关键信息'
    : summaryBusy
      ? 'AI 正在整理关键信息…'
      : r.status === 'processing'
        ? '正在转写与整理，关键信息稍后出现在这里…'
        : r.status === 'recording'
          ? '录音进行中…'
          : r.status === 'failed'
            ? '这段录音转写失败，可进入详情重试'
            : hasSummary
              ? '这次整理没有提取出关键信息'
              : '还没有 AI 整理的关键信息';
  const words = r.charCount > 0 ? `约 ${Math.round(r.charCount / 100) / 10} 千字` : '';
  // 纪要已生成但没拿到关键信息时，用 AI 标题兜底（也兼容尚未重启的旧后端）
  const noHighlightsText =
    hasSummary && r.summaryTitle && !r.lowValue ? r.summaryTitle : highlightsHint;

  return (
    <div
      className={cn('group relative rounded-2xl transition-shadow duration-300', highlight && 'ring-2 ring-brand/45')}
      data-recording-id={r.id}
    >
      <Link
        to={`/recordings/${r.id}`}
        state={{ from }}
        className="block card p-4 hover:shadow-soft hover:border-brand/30 transition-all duration-200 active:scale-[.995]"
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

          {/* 列表只展示 AI 整理后的关键信息，不再显示转写原文 */}
          {highlights.length > 0 ? (
            <ul className="mt-1.5 space-y-1">
              {highlights.map((h, i) => (
                <li
                  key={i}
                  className="flex items-start gap-1.5 text-[12.5px] leading-[1.55] text-muted"
                >
                  <span className="mt-[6px] h-1 w-1 shrink-0 rounded-full bg-brand/55" />
                  <span className="line-clamp-1">{h}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-1.5 text-[12.5px] leading-[1.65] text-muted/70">{noHighlightsText}</p>
          )}

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
            {r.mergedCount > 0 && (
              <span
                className="chip bg-panel2 text-muted/80 !px-1.5 !py-0"
                title={`由相隔不到 5 分钟的 ${r.mergedCount + 1} 段录音合并而来，音频仍分条保留`}
              >
                <GitMerge size={9} />
                合并 {r.mergedCount + 1} 段
              </span>
            )}
            {r.userPresent === false && (
              <span
                className="chip bg-rose-500/10 text-rose-600 dark:text-rose-400 !px-1.5 !py-0"
                title="已录入我的声纹，但这段录音里没有认出我"
              >
                我未参与
              </span>
            )}
            {r.lowValue && (
              <span
                className="chip bg-panel2 text-muted/80 !px-1.5 !py-0"
                title={r.lowReason ?? '没有实际内容'}
              >
                <VolumeX size={9} />
                {r.lowReason ?? '无有效内容'}
              </span>
            )}
            <span className="ml-auto whitespace-nowrap tabular-nums opacity-55">{fmtTimeRange(r)}</span>
            {onDelete && (
              <button
                type="button"
                aria-label="删除这条录音"
                title="删除这条录音"
                onClick={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  onDelete(r);
                }}
                className={cn(
                  '-mr-1 h-6 w-6 grid place-items-center rounded-md text-muted/70',
                  'transition-all hover:bg-red-500/10 hover:text-red-500',
                  'sm:opacity-0 sm:group-hover:opacity-100 focus:opacity-100',
                )}
              >
                <Trash2 size={13} />
              </button>
            )}
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
    </div>
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
