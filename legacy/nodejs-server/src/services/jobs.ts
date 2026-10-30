import { db, get, all, run, type Job } from '../db/index.js';
import { nowIso, uid } from '../config.js';

type Handler = (payload: any, job: Job) => Promise<void>;

const handlers = new Map<string, { fn: Handler; concurrency: number }>();
const running = new Map<string, number>();
let timer: NodeJS.Timeout | null = null;
let draining = false;

export function registerHandler(type: string, fn: Handler, concurrency = 2) {
  handlers.set(type, { fn, concurrency });
  if (!running.has(type)) running.set(type, 0);
}

export function enqueue(
  type: string,
  payload: unknown,
  opts: { runAt?: Date; maxAttempts?: number; dedupeKey?: string } = {},
): string {
  const id = uid('job_');
  run(
    `INSERT INTO jobs (id, type, payload, status, attempts, max_attempts, run_at, created_at)
     VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)`,
    [
      id,
      type,
      JSON.stringify(payload),
      opts.maxAttempts ?? 3,
      (opts.runAt ?? new Date()).toISOString(),
      nowIso(),
    ],
  );
  void drain();
  return id;
}

/** 清理某条记录下的同类型未完成任务（用于重试时避免堆积） */
export function cancelPending(type: string, match: (payload: any) => boolean) {
  const rows = all<Job>(`SELECT * FROM jobs WHERE type = ? AND status = 'pending'`, [type]);
  for (const row of rows) {
    let p: any;
    try { p = JSON.parse(row.payload); } catch { continue; }
    if (match(p)) run(`UPDATE jobs SET status = 'failed', error = 'cancelled' WHERE id = ?`, [row.id]);
  }
}

function backoffMs(attempts: number) {
  return Math.min(60_000, 3000 * Math.pow(2, attempts));
}

export async function drain() {
  if (draining) return;
  draining = true;
  try {
    const now = nowIso();
    const pending = all<Job>(
      `SELECT * FROM jobs WHERE status = 'pending' AND run_at <= ? ORDER BY created_at ASC LIMIT 40`,
      [now],
    );
    for (const job of pending) {
      const h = handlers.get(job.type);
      if (!h) {
        run(`UPDATE jobs SET status = 'failed', error = ? WHERE id = ?`, ['no handler', job.id]);
        continue;
      }
      const cur = running.get(job.type) ?? 0;
      if (cur >= h.concurrency) continue;

      const claimed = run(
        `UPDATE jobs SET status = 'running', started_at = ?, attempts = attempts + 1 WHERE id = ? AND status = 'pending'`,
        [nowIso(), job.id],
      );
      if (claimed.changes === 0) continue;

      running.set(job.type, cur + 1);
      const fresh = get<Job>(`SELECT * FROM jobs WHERE id = ?`, [job.id])!;

      void (async () => {
        try {
          const payload = JSON.parse(job.payload);
          await h.fn(payload, fresh);
          run(`UPDATE jobs SET status = 'done', finished_at = ? WHERE id = ?`, [nowIso(), job.id]);
        } catch (e: any) {
          const msg = (e?.message ?? String(e)).slice(0, 800);
          const attempts = fresh.attempts;
          if (attempts < fresh.max_attempts) {
            run(
              `UPDATE jobs SET status = 'pending', run_at = ?, error = ? WHERE id = ?`,
              [new Date(Date.now() + backoffMs(attempts)).toISOString(), msg, job.id],
            );
          } else {
            run(`UPDATE jobs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?`, [
              nowIso(),
              msg,
              job.id,
            ]);
          }
        } finally {
          running.set(job.type, Math.max(0, (running.get(job.type) ?? 1) - 1));
        }
      })();
    }
  } finally {
    draining = false;
  }
}

export function startWorker(intervalMs = 1200) {
  // 服务重启时，把卡在 running 的任务放回队列
  run(`UPDATE jobs SET status = 'pending', run_at = ? WHERE status = 'running'`, [nowIso()]);
  if (timer) clearInterval(timer);
  timer = setInterval(() => void drain(), intervalMs);
  void drain();
}

export function stopWorker() {
  if (timer) clearInterval(timer);
  timer = null;
}

export function queueStats() {
  return all(`SELECT type, status, COUNT(*) AS n FROM jobs GROUP BY type, status`);
}

export { db };
