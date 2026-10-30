import type { FastifyInstance } from 'fastify';
import fs from 'node:fs';
import path from 'node:path';
import { pipeline } from 'node:stream/promises';
import { all, get, run, type Chunk, type Recording, type Summary } from '../db/index.js';
import { config, nowIso, uid } from '../config.js';
import { enqueue, drain } from '../services/jobs.js';
import { bus, emitRecordingEvent } from '../lib/bus.js';
import { finalizeRecording, fmtClock, parseSegments, transcriptFor } from '../services/transcribe.js';
import { createSummary, localDate, publicSummary } from '../services/summarize.js';
import { probeDurationMs, splitAudio } from '../lib/ffmpeg.js';

export function recordingCard(r: Recording) {
  const agg = get<{ n: number; done: number; failed: number; chars: number }>(
    `SELECT COUNT(*) AS n,
            SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
            SUM(LENGTH(COALESCE(transcript, ''))) AS chars
     FROM chunks WHERE recording_id = ?`,
    [r.id],
  ) ?? { n: 0, done: 0, failed: 0, chars: 0 };

  const summary = get<Summary>(
    `SELECT id, status, title FROM summaries WHERE recording_id = ? ORDER BY created_at DESC LIMIT 1`,
    [r.id],
  );

  return {
    id: r.id,
    title: r.title,
    status: r.status,
    source: r.source,
    language: r.language,
    startedAt: r.started_at,
    endedAt: r.ended_at,
    date: localDate(r.started_at),
    durationMs: r.duration_ms,
    chunkCount: agg.n,
    doneChunks: agg.done ?? 0,
    failedChunks: agg.failed ?? 0,
    charCount: agg.chars ?? 0,
    summaryStatus: r.summary_status,
    summaryId: summary?.id ?? null,
    summaryTitle: summary?.title ?? null,
    memo: r.memo,
  };
}

async function saveUpload(recordingId: string, seq: number, stream: NodeJS.ReadableStream, ext: string) {
  const dir = path.join(config.dataDir, 'uploads', recordingId);
  fs.mkdirSync(dir, { recursive: true });
  const filename = `${String(seq).padStart(5, '0')}${ext}`;
  const full = path.join(dir, filename);
  await pipeline(stream, fs.createWriteStream(full));
  return { filename, full, size: fs.statSync(full).size };
}

export async function recordingRoutes(app: FastifyInstance) {
  /* ---------------- 列表 ---------------- */
  app.get('/api/recordings', { preHandler: [app.authenticate] }, async (req) => {
    const userId = (req.user as any).sub;
    const { date, q, limit, status } = req.query as any;
    const where: string[] = ['user_id = ?'];
    const params: any[] = [userId];

    if (date) {
      const from = new Date(`${date}T00:00:00`).toISOString();
      const to = new Date(`${date}T23:59:59.999`).toISOString();
      where.push('started_at >= ? AND started_at <= ?');
      params.push(from, to);
    }
    if (q) {
      where.push(`(title LIKE ? OR id IN (SELECT recording_id FROM chunks WHERE transcript LIKE ?))`);
      params.push(`%${q}%`, `%${q}%`);
    }
    if (status) {
      where.push('status = ?');
      params.push(status);
    }
    const lim = Math.min(200, Number(limit) || 50);
    const rows = all<Recording>(
      `SELECT * FROM recordings WHERE ${where.join(' AND ')} ORDER BY started_at DESC LIMIT ?`,
      [...params, lim],
    );
    return rows.map(recordingCard);
  });

  /* ---------------- 创建 ---------------- */
  app.post('/api/recordings', { preHandler: [app.authenticate] }, async (req, reply) => {
    const userId = (req.user as any).sub;
    const body = (req.body ?? {}) as any;
    const now = new Date();
    const id = uid('rec_');
    const title =
      String(body.title ?? '').trim() ||
      `录音 ${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
    run(
      `INSERT INTO recordings (id, user_id, title, status, source, language, started_at, asr_provider_id, created_at)
       VALUES (?, ?, ?, 'recording', ?, ?, ?, ?, ?)`,
      [
        id,
        userId,
        title.slice(0, 100),
        body.source ?? 'web',
        body.language ?? 'zh',
        body.startedAt ?? now.toISOString(),
        body.asrProviderId ?? null,
        nowIso(),
      ],
    );
    return reply.code(201).send(recordingCard(get<Recording>(`SELECT * FROM recordings WHERE id = ?`, [id])!));
  });

  /* ---------------- 详情 ---------------- */
  app.get('/api/recordings/:id', { preHandler: [app.authenticate] }, async (req, reply) => {
    const userId = (req.user as any).sub;
    const { id } = req.params as any;
    const rec = get<Recording>(`SELECT * FROM recordings WHERE id = ? AND user_id = ?`, [id, userId]);
    if (!rec) return reply.code(404).send({ error: '录音不存在' });

    const chunks = all<Chunk>(`SELECT * FROM chunks WHERE recording_id = ? ORDER BY seq ASC`, [id]);
    const t = transcriptFor(id);
    const summary = get<Summary>(
      `SELECT * FROM summaries WHERE recording_id = ? ORDER BY created_at DESC LIMIT 1`,
      [id],
    );

    return {
      ...recordingCard(rec),
      memo: rec.memo,
      chunks: chunks.map((c) => ({
        id: c.id,
        seq: c.seq,
        status: c.status,
        durationMs: c.duration_ms,
        startOffsetMs: c.start_offset_ms,
        size: c.size,
        error: c.error,
        text: c.transcript ?? '',
        segmentCount: parseSegments(c).length,
      })),
      transcript: {
        text: t.text,
        segments: t.segments,
        chunkCount: t.chunkCount,
        doneChunks: t.doneChunks,
        failedChunks: t.failedChunks,
      },
      summary: summary ? publicSummary(summary) : null,
    };
  });

  app.patch('/api/recordings/:id', { preHandler: [app.authenticate] }, async (req, reply) => {
    const userId = (req.user as any).sub;
    const { id } = req.params as any;
    const { title, memo } = (req.body ?? {}) as any;
    const rec = get<Recording>(`SELECT * FROM recordings WHERE id = ? AND user_id = ?`, [id, userId]);
    if (!rec) return reply.code(404).send({ error: '录音不存在' });
    if (title !== undefined) run(`UPDATE recordings SET title = ? WHERE id = ?`, [String(title).slice(0, 100), id]);
    if (memo !== undefined) run(`UPDATE recordings SET memo = ? WHERE id = ?`, [String(memo).slice(0, 2000), id]);
    return recordingCard(get<Recording>(`SELECT * FROM recordings WHERE id = ?`, [id])!);
  });

  app.delete('/api/recordings/:id', { preHandler: [app.authenticate] }, async (req) => {
    const userId = (req.user as any).sub;
    const { id } = req.params as any;
    const rec = get<Recording>(`SELECT * FROM recordings WHERE id = ? AND user_id = ?`, [id, userId]);
    if (rec) {
      run(`DELETE FROM recordings WHERE id = ?`, [id]);
      const dir = path.join(config.dataDir, 'uploads', id);
      fs.rmSync(dir, { recursive: true, force: true });
    }
    return { ok: true };
  });

  /* ---------------- 上传分片 ---------------- */
  app.post('/api/recordings/:id/chunks', { preHandler: [app.authenticate] }, async (req, reply) => {
    const userId = (req.user as any).sub;
    const { id } = req.params as any;
    const rec = get<Recording>(`SELECT * FROM recordings WHERE id = ? AND user_id = ?`, [id, userId]);
    if (!rec) return reply.code(404).send({ error: '录音不存在' });

    const q = req.query as any;
    const file = await (req as any).file?.();
    if (!file) return reply.code(400).send({ error: '缺少音频文件' });

    const seq = Number(q.seq ?? 0);
    const declaredDuration = Number(q.durationMs ?? 0);
    let startOffset = Number(q.startOffsetMs ?? 0);

    // 未显式给偏移时，按已有分片时长自动累加
    if (!q.startOffsetMs) {
      const agg = get<{ s: number }>(`SELECT COALESCE(SUM(duration_ms),0) AS s FROM chunks WHERE recording_id = ?`, [id]);
      startOffset = agg?.s ?? 0;
    }

    const ext = path.extname(file.filename || '') || '.webm';
    const saved = await saveUpload(id, seq, file.file, ext);
    const realDuration = declaredDuration || (await probeDurationMs(saved.full)) || 0;
    const t = nowIso();

    const existing = get<Chunk>(`SELECT * FROM chunks WHERE recording_id = ? AND seq = ?`, [id, seq]);
    let chunkId: string;
    if (existing) {
      chunkId = existing.id;
      run(
        `UPDATE chunks SET filename = ?, path = ?, mime = ?, size = ?, duration_ms = ?, start_offset_ms = ?,
           status = 'queued', error = NULL, transcript = NULL, segments = NULL, updated_at = ? WHERE id = ?`,
        [saved.filename, saved.full, file.mimetype, saved.size, realDuration, startOffset, t, chunkId],
      );
    } else {
      chunkId = uid('chk_');
      run(
        `INSERT INTO chunks (id, recording_id, seq, filename, path, mime, size, duration_ms, start_offset_ms, status, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)`,
        [chunkId, id, seq, saved.filename, saved.full, file.mimetype, saved.size, realDuration, startOffset, t, t],
      );
    }

    const total = get<{ s: number; n: number }>(
      `SELECT COALESCE(MAX(start_offset_ms + duration_ms),0) AS s, COUNT(*) AS n FROM chunks WHERE recording_id = ?`,
      [id],
    );
    run(`UPDATE recordings SET status = 'processing', duration_ms = ? WHERE id = ?`, [total?.s ?? 0, id]);

    enqueue('transcribe', { chunkId }, { maxAttempts: 3 });
    emitRecordingEvent(id, 'chunk', { chunkId, seq, status: 'queued' });

    return reply.code(201).send({
      chunkId,
      seq,
      status: 'queued',
      durationMs: realDuration,
      startOffsetMs: startOffset,
      totalDurationMs: total?.s ?? 0,
      chunkCount: total?.n ?? 1,
    });
  });

  /* ---------------- 上传整段音频（后端自动切片） ---------------- */
  app.post('/api/recordings/:id/upload-file', { preHandler: [app.authenticate] }, async (req, reply) => {
    const userId = (req.user as any).sub;
    const { id } = req.params as any;
    const rec = get<Recording>(`SELECT * FROM recordings WHERE id = ? AND user_id = ?`, [id, userId]);
    if (!rec) return reply.code(404).send({ error: '录音不存在' });

    const file = await (req as any).file?.();
    if (!file) return reply.code(400).send({ error: '缺少音频文件' });

    const q = req.query as any;
    const segmentSec = Math.max(30, Math.min(300, Number(q.segmentSec) || 60));
    const dir = path.join(config.dataDir, 'uploads', id);
    fs.mkdirSync(dir, { recursive: true });
    const ext = path.extname(file.filename || '') || '.webm';
    const sourceName = `source${ext}`;
    const sourcePath = path.join(dir, sourceName);
    await pipeline(file.file, fs.createWriteStream(sourcePath));

    const totalMs = await probeDurationMs(sourcePath);
    if (!totalMs) {
      return reply.code(400).send({ error: '无法解析该音频文件，请确认格式是否受支持' });
    }

    const parts = await splitAudio(sourcePath, path.join(dir, 'parts'), segmentSec);
    if (parts.length === 0) return reply.code(400).send({ error: '音频切片失败' });

    let offset = 0;
    let seq = 0;
    for (const part of parts) {
      const chunkId = uid('chk_');
      const t = nowIso();
      const filename = path.basename(part.file);
      run(
        `INSERT INTO chunks (id, recording_id, seq, filename, path, mime, size, duration_ms, start_offset_ms, status, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
         ON CONFLICT(recording_id, seq) DO UPDATE SET path = excluded.path, duration_ms = excluded.duration_ms,
           start_offset_ms = excluded.start_offset_ms, status = 'queued', transcript = NULL, segments = NULL, updated_at = excluded.updated_at`,
        [
          chunkId, id, seq, filename, part.file, 'audio/mpeg',
          fs.statSync(part.file).size, part.durationMs, offset, t, t,
        ],
      );
      enqueue('transcribe', { chunkId }, { maxAttempts: 3 });
      offset += part.durationMs;
      seq += 1;
    }

    run(
      `UPDATE recordings SET status = 'processing', duration_ms = ?, ended_at = COALESCE(ended_at, ?),
         started_at = COALESCE(started_at, ?) WHERE id = ?`,
      [totalMs, nowIso(), nowIso(), id],
    );

    return reply.code(201).send({
      chunks: parts.length,
      durationMs: totalMs,
      segmentSec,
    });
  });

  /* ---------------- 结束录音 ---------------- */
  app.post('/api/recordings/:id/finish', { preHandler: [app.authenticate] }, async (req, reply) => {
    const userId = (req.user as any).sub;
    const { id } = req.params as any;
    const rec = get<Recording>(`SELECT * FROM recordings WHERE id = ? AND user_id = ?`, [id, userId]);
    if (!rec) return reply.code(404).send({ error: '录音不存在' });

    const body = (req.body ?? {}) as any;
    const duration = Number(body.durationMs ?? 0);
    const pending = get<{ n: number }>(`SELECT COUNT(*) AS n FROM chunks WHERE recording_id = ?`, [id]);
    if (!pending?.n) return reply.code(400).send({ error: '还没有上传任何音频分片' });

    run(
      `UPDATE recordings SET ended_at = ?, status = 'processing',
         duration_ms = CASE WHEN ? > 0 THEN ? ELSE duration_ms END WHERE id = ?`,
      [nowIso(), duration, duration, id],
    );
    await finalizeRecording(id);
    await drain();
    return recordingCard(get<Recording>(`SELECT * FROM recordings WHERE id = ?`, [id])!);
  });

  /* ---------------- 重试 ---------------- */
  app.post('/api/recordings/:id/retry', { preHandler: [app.authenticate] }, async (req, reply) => {
    const userId = (req.user as any).sub;
    const { id } = req.params as any;
    const rec = get<Recording>(`SELECT * FROM recordings WHERE id = ? AND user_id = ?`, [id, userId]);
    if (!rec) return reply.code(404).send({ error: '录音不存在' });

    const failed = all<Chunk>(
      `SELECT * FROM chunks WHERE recording_id = ? AND status IN ('failed','pending','queued')`,
      [id],
    );
    for (const c of failed) {
      run(`UPDATE chunks SET status = 'queued', error = NULL, updated_at = ? WHERE id = ?`, [nowIso(), c.id]);
      enqueue('transcribe', { chunkId: c.id }, { maxAttempts: 3 });
    }
    run(`UPDATE recordings SET status = 'processing' WHERE id = ?`, [id]);
    await drain();
    return { requeued: failed.length };
  });

  /* ---------------- 手动触发纪要 ---------------- */
  app.post('/api/recordings/:id/summarize', { preHandler: [app.authenticate] }, async (req, reply) => {
    const userId = (req.user as any).sub;
    const { id } = req.params as any;
    const rec = get<Recording>(`SELECT * FROM recordings WHERE id = ? AND user_id = ?`, [id, userId]);
    if (!rec) return reply.code(404).send({ error: '录音不存在' });
    const body = (req.body ?? {}) as any;
    try {
      const s = await createSummary({
        userId,
        scope: 'recording',
        recordingId: id,
        providerId: body.providerId,
        template: body.template,
        force: body.force ?? true,
      });
      return publicSummary(s);
    } catch (e: any) {
      return reply.code(400).send({ error: e?.message ?? '生成失败' });
    }
  });

  /* ---------------- 实时事件（SSE） ---------------- */
  app.get('/api/recordings/:id/stream', { preHandler: [app.authenticate] }, async (req, reply) => {
    const { id } = req.params as any;
    reply.raw.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache, no-transform',
      Connection: 'keep-alive',
      'X-Accel-Buffering': 'no',
    });
    reply.raw.write(`event: ping\ndata: {}\n\n`);

    const handler = (payload: any) => {
      reply.raw.write(`event: ${payload.event}\ndata: ${JSON.stringify(payload.data)}\n\n`);
    };
    bus.on(`rec:${id}`, handler);
    const hb = setInterval(() => reply.raw.write(`event: ping\ndata: {}\n\n`), 20_000);

    req.raw.on('close', () => {
      clearInterval(hb);
      bus.off(`rec:${id}`, handler);
    });
  });

  /* ---------------- 导出 ---------------- */
  app.get('/api/recordings/:id/export', { preHandler: [app.authenticate] }, async (req, reply) => {
    const userId = (req.user as any).sub;
    const { id } = req.params as any;
    const { format } = req.query as any;
    const rec = get<Recording>(`SELECT * FROM recordings WHERE id = ? AND user_id = ?`, [id, userId]);
    if (!rec) return reply.code(404).send({ error: '录音不存在' });

    const t = transcriptFor(id);
    const summary = get<Summary>(`SELECT * FROM summaries WHERE recording_id = ? ORDER BY created_at DESC LIMIT 1`, [id]);

    if (format === 'txt') {
      const body = t.segments.map((s) => `[${fmtClock(s.startMs)}] ${s.text}`).join('\n');
      return reply
        .header('Content-Type', 'text/plain; charset=utf-8')
        .header('Content-Disposition', `attachment; filename="${encodeURIComponent(rec.title)}.txt"`)
        .send(body);
    }

    const md = [
      `# ${rec.title}`,
      '',
      `> ${localDate(rec.started_at)} · 时长 ${fmtClock(rec.duration_ms)} · ${t.chunkCount} 个片段`,
      '',
      summary?.markdown ?? '',
      '',
      '---',
      '',
      '## 完整转写',
      '',
      t.segments.map((s) => `**${fmtClock(s.startMs)}** ${s.text}`).join('\n\n'),
    ].join('\n');

    return reply
      .header('Content-Type', 'text/markdown; charset=utf-8')
      .header('Content-Disposition', `attachment; filename="${encodeURIComponent(rec.title)}.md"`)
      .send(md);
  });

  /* ---------------- 音频回放 ---------------- */
  app.get('/api/recordings/:id/audio/:seq', { preHandler: [app.authenticate] }, async (req, reply) => {
    const userId = (req.user as any).sub;
    const { id, seq } = req.params as any;
    const rec = get<Recording>(`SELECT * FROM recordings WHERE id = ? AND user_id = ?`, [id, userId]);
    if (!rec) return reply.code(404).send({ error: '录音不存在' });
    const chunk = get<Chunk>(`SELECT * FROM chunks WHERE recording_id = ? AND seq = ?`, [id, Number(seq)]);
    if (!chunk || !fs.existsSync(chunk.path)) return reply.code(404).send({ error: '音频不存在' });
    return reply
      .header('Content-Type', chunk.mime || 'audio/webm')
      .header('Cache-Control', 'private, max-age=3600')
      .send(fs.createReadStream(chunk.path));
  });
}
