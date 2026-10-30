import type { FastifyInstance, FastifyRequest } from 'fastify';
import fs from 'node:fs';
import path from 'node:path';
import { pipeline } from 'node:stream/promises';
import { get, run, type Recording, type User } from '../db/index.js';
import { config, nowIso, uid } from '../config.js';
import { enqueue, drain } from '../services/jobs.js';
import { finalizeRecording, transcriptFor } from '../services/transcribe.js';
import { probeDurationMs } from '../lib/ffmpeg.js';

function readApiKey(req: FastifyRequest): string | null {
  const h = req.headers['x-api-key'];
  if (typeof h === 'string' && h) return h;
  const auth = req.headers.authorization;
  if (typeof auth === 'string' && auth.startsWith('Bearer rk_')) return auth.slice(7);
  return null;
}

async function saveRaw(recordingId: string, seq: number, buf: Buffer, ext = '.webm') {
  const dir = path.join(config.dataDir, 'uploads', recordingId);
  fs.mkdirSync(dir, { recursive: true });
  const filename = `${String(seq).padStart(5, '0')}${ext}`;
  const full = path.join(dir, filename);
  fs.writeFileSync(full, buf);
  return { filename, full, size: buf.length };
}

function extFromMime(mime: string) {
  if (mime.includes('webm')) return '.webm';
  if (mime.includes('mp4') || mime.includes('m4a')) return '.m4a';
  if (mime.includes('mpeg') || mime.includes('mp3')) return '.mp3';
  if (mime.includes('wav')) return '.wav';
  if (mime.includes('ogg')) return '.ogg';
  if (mime.includes('aac')) return '.aac';
  return '.bin';
}

export async function openRoutes(app: FastifyInstance) {
  app.addHook('preHandler', async (req, reply) => {
    if (!req.url.startsWith('/api/open')) return;
    const key = readApiKey(req);
    if (!key) return reply.code(401).send({ error: '缺少 X-API-Key' });
    const row = get<any>(`SELECT * FROM api_keys WHERE key_value = ?`, [key]);
    if (!row) return reply.code(401).send({ error: 'API Key 无效' });
    const user = get<User>(`SELECT * FROM users WHERE id = ?`, [row.user_id]);
    if (!user) return reply.code(401).send({ error: '账号不存在' });
    run(`UPDATE api_keys SET last_used_at = ? WHERE id = ?`, [nowIso(), row.id]);
    (req as any).apiUser = user;
  });

  app.get('/api/open/me', async (req) => {
    const u = (req as any).apiUser as User;
    return { id: u.id, username: u.username, displayName: u.display_name };
  });

  app.post('/api/open/recordings', async (req, reply) => {
    const u = (req as any).apiUser as User;
    const b = (req.body ?? {}) as any;
    const now = new Date();
    const id = uid('rec_');
    const title =
      String(b.title ?? '').trim() ||
      `外部录音 ${now.getMonth() + 1}/${now.getDate()} ${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
    run(
      `INSERT INTO recordings (id, user_id, title, status, source, language, started_at, asr_provider_id, created_at)
       VALUES (?, ?, ?, 'recording', 'api', ?, ?, ?, ?)`,
      [id, u.id, title.slice(0, 100), b.language ?? 'zh', b.startedAt ?? now.toISOString(), b.asrProviderId ?? null, nowIso()],
    );
    return reply.code(201).send({ id, title, status: 'recording' });
  });

  app.post('/api/open/recordings/:id/chunks', { bodyLimit: 64 * 1024 * 1024 }, async (req, reply) => {
    const u = (req as any).apiUser as User;
    const { id } = req.params as any;
    const rec = get<Recording>(`SELECT * FROM recordings WHERE id = ? AND user_id = ?`, [id, u.id]);
    if (!rec) return reply.code(404).send({ error: '录音不存在' });

    const q = req.query as any;
    const seq = Number(q.seq ?? 0);
    let startOffset = Number(q.startOffsetMs ?? 0);
    if (!q.startOffsetMs) {
      const agg = get<{ s: number }>(`SELECT COALESCE(SUM(duration_ms),0) AS s FROM chunks WHERE recording_id = ?`, [id]);
      startOffset = agg?.s ?? 0;
    }

    let saved;
    if (req.isMultipart()) {
      const file = await (req as any).file();
      if (!file) return reply.code(400).send({ error: '缺少音频文件' });
      const ext = path.extname(file.filename || '') || '.webm';
      const dir = path.join(config.dataDir, 'uploads', id);
      fs.mkdirSync(dir, { recursive: true });
      const filename = `${String(seq).padStart(5, '0')}${ext}`;
      const full = path.join(dir, filename);
      await pipeline(file.file, fs.createWriteStream(full));
      saved = { filename, full, size: fs.statSync(full).size, mime: file.mimetype };
    } else {
      const buf = req.body as Buffer;
      if (!Buffer.isBuffer(buf) || buf.length === 0) return reply.code(400).send({ error: '请求体需要是音频二进制' });
      const mime = String(req.headers['content-type'] ?? 'audio/webm');
      const r = await saveRaw(id, seq, buf, extFromMime(mime));
      saved = { ...r, mime };
    }

    const duration = Number(q.durationMs ?? 0) || (await probeDurationMs(saved.full)) || 0;
    const t = nowIso();
    const chunkId = uid('chk_');
    run(
      `INSERT INTO chunks (id, recording_id, seq, filename, path, mime, size, duration_ms, start_offset_ms, status, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
       ON CONFLICT(recording_id, seq) DO UPDATE SET path = excluded.path, size = excluded.size,
         duration_ms = excluded.duration_ms, status = 'queued', transcript = NULL, segments = NULL, updated_at = excluded.updated_at`,
      [chunkId, id, seq, saved.filename, saved.full, saved.mime, saved.size, duration, startOffset, t, t],
    );
    const total = get<{ s: number }>(
      `SELECT COALESCE(MAX(start_offset_ms + duration_ms),0) AS s FROM chunks WHERE recording_id = ?`,
      [id],
    );
    run(`UPDATE recordings SET status = 'processing', duration_ms = ? WHERE id = ?`, [total?.s ?? 0, id]);
    enqueue('transcribe', { chunkId }, { maxAttempts: 3 });
    return reply.code(201).send({ chunkId, seq, status: 'queued', durationMs: duration });
  });

  app.post('/api/open/recordings/:id/finish', async (req, reply) => {
    const u = (req as any).apiUser as User;
    const { id } = req.params as any;
    const rec = get<Recording>(`SELECT * FROM recordings WHERE id = ? AND user_id = ?`, [id, u.id]);
    if (!rec) return reply.code(404).send({ error: '录音不存在' });
    run(`UPDATE recordings SET ended_at = COALESCE(ended_at, ?), status = 'processing' WHERE id = ?`, [nowIso(), id]);
    await finalizeRecording(id);
    await drain();
    const fresh = get<Recording>(`SELECT * FROM recordings WHERE id = ?`, [id])!;
    return { id, status: fresh.status };
  });

  app.get('/api/open/recordings/:id', async (req, reply) => {
    const u = (req as any).apiUser as User;
    const { id } = req.params as any;
    const rec = get<Recording>(`SELECT * FROM recordings WHERE id = ? AND user_id = ?`, [id, u.id]);
    if (!rec) return reply.code(404).send({ error: '录音不存在' });
    const t = transcriptFor(id);
    return {
      id: rec.id,
      title: rec.title,
      status: rec.status,
      durationMs: rec.duration_ms,
      startedAt: rec.started_at,
      endedAt: rec.ended_at,
      summaryStatus: rec.summary_status,
      chunks: { total: t.chunkCount, done: t.doneChunks, failed: t.failedChunks },
      text: t.text,
      segments: t.segments,
    };
  });

  /** 一步到位：上传完整音频 → 建会话 → 分片 → 转写。默认异步返回 id，?wait=1 时等结果 */
  app.post('/api/open/transcribe', { bodyLimit: 64 * 1024 * 1024 }, async (req, reply) => {
    const u = (req as any).apiUser as User;
    const q = req.query as any;

    let buf: Buffer;
    let mime = 'audio/webm';
    let filename = 'audio.bin';
    if (req.isMultipart()) {
      const file = await (req as any).file();
      if (!file) return reply.code(400).send({ error: '缺少音频文件' });
      const parts: Buffer[] = [];
      for await (const p of file.file) parts.push(p as Buffer);
      buf = Buffer.concat(parts);
      mime = file.mimetype;
      filename = file.filename || filename;
    } else {
      buf = req.body as Buffer;
      mime = String(req.headers['content-type'] ?? 'audio/webm');
      filename = String(req.headers['x-filename'] ?? filename);
    }
    if (!Buffer.isBuffer(buf) || buf.length === 0) return reply.code(400).send({ error: '请求体需要是音频二进制' });

    const now = new Date();
    const id = uid('rec_');
    const title =
      String(q.title ?? '').trim() ||
      `外部录音 ${now.getMonth() + 1}/${now.getDate()} ${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;

    run(
      `INSERT INTO recordings (id, user_id, title, status, source, language, started_at, created_at)
       VALUES (?, ?, ?, 'processing', 'api', ?, ?, ?)`,
      [id, u.id, title.slice(0, 100), (q.language as string) ?? 'zh', nowIso(), nowIso()],
    );

    const seq = 0;
    const dir = path.join(config.dataDir, 'uploads', id);
    fs.mkdirSync(dir, { recursive: true });
    const fname = `${String(seq).padStart(5, '0')}${path.extname(filename) || extFromMime(mime)}`;
    const full = path.join(dir, fname);
    fs.writeFileSync(full, buf);
    const duration = (await probeDurationMs(full)) || 0;
    const t = nowIso();
    const chunkId = uid('chk_');
    run(
      `INSERT INTO chunks (id, recording_id, seq, filename, path, mime, size, duration_ms, start_offset_ms, status, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'queued', ?, ?)`,
      [chunkId, id, seq, fname, full, mime, buf.length, duration, t, t],
    );
    run(`UPDATE recordings SET duration_ms = ?, ended_at = ? WHERE id = ?`, [duration, nowIso(), id]);
    enqueue('transcribe', { chunkId }, { maxAttempts: 3 });
    await drain();

    if (q.wait === '1' || q.wait === 'true') {
      const deadline = Date.now() + Math.min(300_000, Number(q.timeout ?? 120_000));
      while (Date.now() < deadline) {
        const c = get<any>(`SELECT status, error FROM chunks WHERE id = ?`, [chunkId]);
        if (c?.status === 'done' || c?.status === 'failed') break;
        await new Promise((r) => setTimeout(r, 500));
      }
      const fresh = get<Recording>(`SELECT * FROM recordings WHERE id = ?`, [id])!;
      const result = transcriptFor(id);
      return {
        id,
        title,
        status: fresh.status,
        durationMs: fresh.duration_ms,
        text: result.text,
        segments: result.segments,
        summaryStatus: fresh.summary_status,
      };
    }

    return reply.code(202).send({ id, title, status: 'processing', durationMs: duration });
  });
}
