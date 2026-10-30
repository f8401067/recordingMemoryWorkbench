import { all, get, run, type Chunk, type Recording } from '../db/index.js';
import { nowIso } from '../config.js';
import { buildAsr, resolveAsr } from './providers.js';
import { emitRecordingEvent } from '../lib/bus.js';
import { readSettings } from './settings.js';
import { enqueue } from './jobs.js';

export type Segment = { startMs: number; endMs: number; text: string };

export function parseSegments(chunk: Chunk): Segment[] {
  if (!chunk.segments) return [];
  try {
    const arr = JSON.parse(chunk.segments);
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

export function transcriptFor(recordingId: string) {
  const chunks = all<Chunk>(`SELECT * FROM chunks WHERE recording_id = ? ORDER BY seq ASC`, [recordingId]);
  const segments: Segment[] = [];
  for (const c of chunks) segments.push(...parseSegments(c));
  segments.sort((a, b) => a.startMs - b.startMs);
  const text = segments.map((s) => s.text).join('\n');
  const done = chunks.filter((c) => c.status === 'done').length;
  const failed = chunks.filter((c) => c.status === 'failed').length;
  return { segments, text, chunkCount: chunks.length, doneChunks: done, failedChunks: failed };
}

/** 把转写渲染成带时间戳的纯文本，供 LLM 阅读 */
export function transcriptWithTimestamps(recordingId: string): string {
  const { segments } = transcriptFor(recordingId);
  return segments
    .map((s) => `[${fmtClock(s.startMs)}] ${s.text}`)
    .join('\n');
}

export function fmtClock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = String(m).padStart(2, '0');
  const ss = String(s).padStart(2, '0');
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

/* ------------------------------------------------------------------ */
/* 转写任务                                                             */
/* ------------------------------------------------------------------ */
export async function handleTranscribeJob(payload: { chunkId: string }) {
  const chunk = get<Chunk>(`SELECT * FROM chunks WHERE id = ?`, [payload.chunkId]);
  if (!chunk) return; // 已被删除
  if (chunk.status === 'done') return;

  const rec = get<Recording>(`SELECT * FROM recordings WHERE id = ?`, [chunk.recording_id]);
  if (!rec) return;

  run(`UPDATE chunks SET status = 'transcribing', updated_at = ? WHERE id = ?`, [nowIso(), chunk.id]);
  emitRecordingEvent(rec.id, 'chunk', { chunkId: chunk.id, seq: chunk.seq, status: 'transcribing' });

  const provider = resolveAsr(rec.user_id, rec.asr_provider_id);
  const engine = buildAsr(provider);

  try {
    const res = await engine.transcribe({
      filePath: chunk.path,
      mime: chunk.mime ?? undefined,
      language: rec.language,
      durationMs: chunk.duration_ms || undefined,
    });

    const offset = chunk.start_offset_ms;
    const segments: Segment[] = res.segments.map((s) => ({
      startMs: s.startMs + offset,
      endMs: (s.endMs || s.startMs) + offset,
      text: s.text.trim(),
    }));

    run(
      `UPDATE chunks SET status = 'done', transcript = ?, segments = ?, error = NULL, updated_at = ? WHERE id = ?`,
      [res.text, JSON.stringify(segments), nowIso(), chunk.id],
    );
    emitRecordingEvent(rec.id, 'chunk', {
      chunkId: chunk.id,
      seq: chunk.seq,
      status: 'done',
      preview: res.text.slice(0, 120),
    });
  } catch (e: any) {
    run(
      `UPDATE chunks SET status = 'failed', error = ?, updated_at = ? WHERE id = ?`,
      [(e?.message ?? String(e)).slice(0, 500), nowIso(), chunk.id],
    );
    emitRecordingEvent(rec.id, 'chunk', {
      chunkId: chunk.id,
      seq: chunk.seq,
      status: 'failed',
      error: e?.message,
    });
    await finalizeRecording(rec.id);
    throw e;
  }

  await finalizeRecording(rec.id);
}

/** 全部片子处理完 → 更新录音状态，必要时触发自动纪要 */
export async function finalizeRecording(recordingId: string) {
  const rec = get<Recording>(`SELECT * FROM recordings WHERE id = ?`, [recordingId]);
  if (!rec) return;

  const chunks = all<Chunk>(`SELECT * FROM chunks WHERE recording_id = ?`, [recordingId]);
  if (chunks.length === 0) return;

  const busy = chunks.some((c) => ['pending', 'queued', 'transcribing'].includes(c.status));
  if (busy) {
    run(`UPDATE recordings SET status = 'processing' WHERE id = ?`, [recordingId]);
    return;
  }

  const anyDone = chunks.some((c) => c.status === 'done');
  const status = anyDone ? 'ready' : 'failed';
  const duration = chunks.reduce(
    (max, c) => Math.max(max, c.start_offset_ms + (c.duration_ms || 0)),
    0,
  );
  const endedAt = rec.ended_at ?? nowIso();
  run(`UPDATE recordings SET status = ?, duration_ms = ?, ended_at = COALESCE(ended_at, ?) WHERE id = ?`, [
    status,
    duration,
    endedAt,
    recordingId,
  ]);
  emitRecordingEvent(recordingId, 'recording', { status, durationMs: duration });

  if (status !== 'ready') return;

  const settings = readSettings(rec.user_id);
  if (!settings.autoSummarize) return;
  if (rec.summary_status === 'ready' || rec.summary_status === 'generating' || rec.summary_status === 'queued') return;

  const { createSummary } = await import('./summarize.js');
  await createSummary({
    userId: rec.user_id,
    recordingId,
    scope: 'recording',
    providerId: settings.summaryProviderId ?? undefined,
    template: settings.summaryTemplate,
  });
}

/** 重新转写某个分片 */
export function requeueChunk(chunkId: string) {
  const chunk = get<Chunk>(`SELECT * FROM chunks WHERE id = ?`, [chunkId]);
  if (!chunk) throw new Error('分片不存在');
  run(`UPDATE chunks SET status = 'queued', error = NULL, updated_at = ? WHERE id = ?`, [nowIso(), chunk.id]);
  run(`UPDATE recordings SET status = 'processing', summary_status = 'none' WHERE id = ?`, [chunk.recording_id]);
  emitRecordingEvent(chunk.recording_id, 'chunk', { chunkId, seq: chunk.seq, status: 'queued' });
  enqueue('transcribe', { chunkId }, { maxAttempts: 3 });
}
