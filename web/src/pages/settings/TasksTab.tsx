import React, { useEffect, useMemo, useState } from 'react';
import {
  AlertCircle, CheckCircle2, Clock3, Loader2, RefreshCw, RotateCw, Timer, Zap,
} from 'lucide-react';
import { retryTask, type SystemTask, type TaskBoard } from '../../lib/api';
import { useToast } from '../../lib/toast';
import { cn } from '../../lib/utils';
import { fmtElapsed, fmtStamp } from '../../lib/system';
import Section from './Section';

function TaskRow({
  task,
  now,
  onRetry,
  retrying,
}: {
  task: SystemTask;
  now: number;
  onRetry?: (t: SystemTask) => void;
  retrying?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const running = task.status === 'running';
  const failed = task.status === 'failed';

  // 服务端时间与本地时钟可能有偏差，统一用 board.now 校正
  const started = task.startedAt ? new Date(task.startedAt).getTime() : null;
  const elapsed = running && started ? Math.max(0, now - started) : task.elapsedMs;
  const heartbeatAge = running ? now - (task.heartbeatAt ? new Date(task.heartbeatAt).getTime() : now) : null;

  return (
    <div
      className={cn(
        'rounded-xl border px-3 py-2.5 transition-colors',
        failed ? 'border-red-500/30 bg-red-500/[0.04]' : 'border-line bg-panel',
      )}
    >
      <div className="flex items-start gap-2.5">
        <div className="mt-0.5 shrink-0">
          {running ? (
            <Loader2 size={14} className="animate-spin text-brand" />
          ) : failed ? (
            <AlertCircle size={14} className="text-red-500" />
          ) : task.status === 'pending' ? (
            <Clock3 size={14} className="text-amber-500" />
          ) : (
            <CheckCircle2 size={14} className="text-emerald-500" />
          )}
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-[12.5px] font-medium truncate max-w-[60%]">{task.title}</span>
            <span className="chip bg-panel2 text-muted !py-0 !px-1.5 text-[10px]">{task.typeLabel}</span>
            {task.attempts > 1 && (
              <span className="chip bg-amber-500/10 text-amber-600 dark:text-amber-400 !py-0 !px-1.5 text-[10px]">
                第 {task.attempts} 次
              </span>
            )}
          </div>
          <div className="mt-0.5 text-[11px] text-muted flex items-center gap-x-2 gap-y-0.5 flex-wrap">
            <span className="truncate">{task.detail}</span>
            {running && (
              <>
                <span className="opacity-40">·</span>
                <span className="tabular-nums">已跑 {fmtElapsed(elapsed)}</span>
                {heartbeatAge !== null && (
                  <>
                    <span className="opacity-40">·</span>
                    <span className={cn('tabular-nums', heartbeatAge > 30_000 && 'text-amber-500')}>
                      心跳 {fmtElapsed(heartbeatAge)} 前
                    </span>
                  </>
                )}
              </>
            )}
            {!running && task.finishedAt && (
              <>
                <span className="opacity-40">·</span>
                <span>{fmtStamp(task.finishedAt)} 结束</span>
                {elapsed != null && (
                  <>
                    <span className="opacity-40">·</span>
                    <span className="tabular-nums">耗时 {fmtElapsed(elapsed)}</span>
                  </>
                )}
              </>
            )}
            {task.status === 'pending' && task.runAt && (
              <>
                <span className="opacity-40">·</span>
                <span>计划 {fmtStamp(task.runAt)}</span>
              </>
            )}
          </div>

          {task.error && (
            <div className="mt-1.5">
              <button
                onClick={() => setOpen((v) => !v)}
                className="text-[11px] text-red-500 hover:underline"
              >
                {open ? '收起错误' : '查看错误'}
              </button>
              {open && (
                <pre className="mt-1 max-h-40 overflow-auto rounded-lg bg-red-500/5 px-2 py-1.5 text-[11px] leading-relaxed whitespace-pre-wrap break-words text-red-600 dark:text-red-400">
                  {task.error}
                </pre>
              )}
            </div>
          )}
        </div>

        {onRetry && task.retryable && (
          <button
            onClick={() => onRetry(task)}
            disabled={retrying}
            title="重新排队执行"
            className="shrink-0 h-7 px-2 rounded-lg text-[11px] text-muted hover:text-brand hover:bg-panel2 inline-flex items-center gap-1 disabled:opacity-40"
          >
            {retrying ? <Loader2 size={11} className="animate-spin" /> : <RotateCw size={11} />}
            重跑
          </button>
        )}
      </div>
    </div>
  );
}

export default function TasksTab({
  board,
  loading,
  error,
  onRefresh,
}: {
  board: TaskBoard | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => Promise<void> | void;
}) {
  const toast = useToast();
  const [tickCount, setTickCount] = useState(0);
  const [retryingId, setRetryingId] = useState<string | null>(null);

  useEffect(() => {
    const t = window.setInterval(() => setTickCount((n) => n + 1), 1000);
    return () => window.clearInterval(t);
  }, []);

  // 用服务端 now 校正本地时钟偏差
  const now = useMemo(() => {
    if (!board?.now) return Date.now();
    const drift = new Date(board.now).getTime() - Date.now();
    return Date.now() + drift;
  }, [board?.now, tickCount]);

  const retry = async (task: SystemTask) => {
    setRetryingId(task.id);
    try {
      await retryTask(task.id);
      toast.push('已重新排队', 'success');
      await onRefresh();
    } catch (e: any) {
      toast.push(e?.message ?? '重跑失败', 'error');
    } finally {
      setRetryingId(null);
    }
  };

  const counts = board?.counts;
  const concurrency = board?.concurrency ?? {};
  const worker = board?.worker;

  return (
    <div className="space-y-3">
      <Section
        title="后台任务"
        desc="转写和整理都在后台线程里跑，这里能看到实时进度"
        icon={Zap}
        action={
          <button onClick={() => void onRefresh()} className="btn-ghost h-8 px-2.5 text-[12px] shrink-0">
            <RefreshCw size={12} className={cn(loading && 'animate-spin')} /> 刷新
          </button>
        }
      >
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          {[
            { label: '运行中', value: counts?.running ?? 0, tone: 'text-brand' },
            { label: '排队中', value: counts?.queued ?? 0, tone: 'text-amber-500' },
            { label: '最近完成', value: counts?.done ?? 0, tone: 'text-emerald-600 dark:text-emerald-400' },
            { label: '最近失败', value: counts?.failed ?? 0, tone: 'text-red-500' },
          ].map((s) => (
            <div key={s.label} className="rounded-xl bg-panel2 px-3 py-2">
              <div className={cn('text-[18px] font-semibold tabular-nums leading-tight', s.tone)}>{s.value}</div>
              <div className="text-[10.5px] text-muted mt-0.5">{s.label}</div>
            </div>
          ))}
        </div>

        <div className="mt-2.5 flex items-center gap-x-3 gap-y-1 flex-wrap text-[10.5px] text-muted">
          {Object.entries(concurrency).map(([type, n]) => (
            <span key={type} className="inline-flex items-center gap-1">
              <Timer size={10} />
              {type === 'transcribe' ? '转写' : type === 'summarize' ? '整理' : type} 并发 {n}
            </span>
          ))}
          {worker && (
            <span className="opacity-80">
              心跳 {worker.heartbeatSeconds}s / 超过 {worker.staleSeconds}s 没心跳自动重排
            </span>
          )}
        </div>

        {error && (
          <div className="mt-2 text-[11.5px] text-red-500 flex items-start gap-1">
            <AlertCircle size={12} className="mt-0.5" /> {error}
          </div>
        )}
      </Section>

      <Section title="正在运行" desc="每 4 秒自动刷新；切到后台会暂停轮询" icon={Loader2}>
        <div className="space-y-2">
          {board?.running?.length ? (
            board.running.map((t) => <TaskRow key={t.id} task={t} now={now} />)
          ) : (
            <div className="text-[12px] text-muted text-center py-5">
              {loading && !board ? '读取中…' : '现在没有正在跑的任务'}
            </div>
          )}
        </div>

        {!!board?.queued?.length && (
          <>
            <div className="mt-4 mb-2 flex items-center gap-2">
              <span className="text-[11.5px] text-muted">排队中 {board.queued.length}</span>
              <div className="flex-1 h-px bg-line" />
            </div>
            <div className="space-y-2">
              {board.queued.map((t) => (
                <TaskRow key={t.id} task={t} now={now} />
              ))}
            </div>
          </>
        )}
      </Section>

      <Section title="最近结束" desc="包含成功和失败；失败的任务可以点「重跑」再排一次" icon={CheckCircle2}>
        <div className="space-y-2">
          {board?.recent?.length ? (
            board.recent.map((t) => (
              <TaskRow
                key={t.id}
                task={t}
                now={now}
                onRetry={(task) => void retry(task)}
                retrying={retryingId === t.id}
              />
            ))
          ) : (
            <div className="text-[12px] text-muted text-center py-5">还没有已结束的任务</div>
          )}
        </div>
      </Section>
    </div>
  );
}
