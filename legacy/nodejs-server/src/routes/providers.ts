import type { FastifyInstance } from 'fastify';
import { all, get, run, type Provider } from '../db/index.js';
import { listProviders, providerCatalog, publicProvider, testProvider, findProvider } from '../services/providers.js';
import { readSettings, writeSettings } from '../services/settings.js';
import { nowIso, uid } from '../config.js';

export async function providerRoutes(app: FastifyInstance) {
  app.get('/api/providers/catalog', async () => providerCatalog());

  app.get('/api/providers', { preHandler: [app.authenticate] }, async (req) => {
    const userId = (req.user as any).sub;
    const { category } = req.query as any;
    return listProviders(userId, category);
  });

  app.post('/api/providers', { preHandler: [app.authenticate] }, async (req, reply) => {
    const userId = (req.user as any).sub;
    const b = (req.body ?? {}) as any;
    if (!b.category || !b.kind || !b.name) return reply.code(400).send({ error: '缺少 category / kind / name' });
    if (!['asr', 'llm'].includes(b.category)) return reply.code(400).send({ error: 'category 只能是 asr 或 llm' });

    const id = uid('prv_');
    const isFirst = !get(`SELECT id FROM providers WHERE user_id = ? AND category = ?`, [userId, b.category]);
    run(
      `INSERT INTO providers (id, user_id, category, kind, name, base_url, api_key, model, params, is_default, enabled, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)`,
      [
        id,
        userId,
        b.category,
        b.kind,
        String(b.name).slice(0, 40),
        b.baseUrl ?? null,
        b.apiKey ?? null,
        b.model ?? null,
        b.params ? JSON.stringify(b.params) : null,
        isFirst || b.isDefault ? 1 : 0,
        nowIso(),
      ],
    );
    if (b.isDefault) run(`UPDATE providers SET is_default = 0 WHERE user_id = ? AND category = ? AND id <> ?`, [userId, b.category, id]);
    return reply.code(201).send(publicProvider(findProvider(userId, id)!));
  });

  app.patch('/api/providers/:id', { preHandler: [app.authenticate] }, async (req, reply) => {
    const userId = (req.user as any).sub;
    const { id } = req.params as any;
    const b = (req.body ?? {}) as any;
    const p = findProvider(userId, id);
    if (!p) return reply.code(404).send({ error: '服务商不存在' });

    const sets: string[] = [];
    const params: any[] = [];
    if (b.name !== undefined) { sets.push('name = ?'); params.push(String(b.name).slice(0, 40)); }
    if (b.baseUrl !== undefined) { sets.push('base_url = ?'); params.push(b.baseUrl || null); }
    if (b.apiKey !== undefined && b.apiKey !== '') {
      // 传入空字符串表示"保留原 Key"，未传表示不变
      sets.push('api_key = ?');
      params.push(b.apiKey);
    }
    if (b.model !== undefined) { sets.push('model = ?'); params.push(b.model || null); }
    if (b.params !== undefined) { sets.push('params = ?'); params.push(b.params ? JSON.stringify(b.params) : null); }
    if (b.enabled !== undefined) { sets.push('enabled = ?'); params.push(b.enabled ? 1 : 0); }
    if (b.isDefault) {
      sets.push('is_default = 1');
      run(`UPDATE providers SET is_default = 0 WHERE user_id = ? AND category = ? AND id <> ?`, [userId, p.category, id]);
    }
    if (sets.length) {
      params.push(id, userId);
      run(`UPDATE providers SET ${sets.join(', ')} WHERE id = ? AND user_id = ?`, params);
    }
    return publicProvider(findProvider(userId, id)!);
  });

  app.delete('/api/providers/:id', { preHandler: [app.authenticate] }, async (req) => {
    const userId = (req.user as any).sub;
    const { id } = req.params as any;
    run(`DELETE FROM providers WHERE id = ? AND user_id = ?`, [id, userId]);
    return { ok: true };
  });

  app.post('/api/providers/:id/test', { preHandler: [app.authenticate] }, async (req, reply) => {
    const userId = (req.user as any).sub;
    const { id } = req.params as any;
    const p = findProvider(userId, id);
    if (!p) return reply.code(404).send({ error: '服务商不存在' });
    return testProvider(p);
  });

  /* ---------------- 用户偏好 ---------------- */
  app.get('/api/settings', { preHandler: [app.authenticate] }, async (req) => {
    return readSettings((req.user as any).sub);
  });

  app.patch('/api/settings', { preHandler: [app.authenticate] }, async (req) => {
    const userId = (req.user as any).sub;
    const patch = (req.body ?? {}) as Record<string, unknown>;
    return writeSettings(userId, patch);
  });

  /* ---------------- 概览统计 ---------------- */
  app.get('/api/stats', { preHandler: [app.authenticate] }, async (req) => {
    const userId = (req.user as any).sub;
    const totals = get<any>(
      `SELECT COUNT(*) AS recordings, COALESCE(SUM(duration_ms),0) AS durationMs,
              COALESCE(SUM(LENGTH(COALESCE(memo,''))),0) AS memoChars
       FROM recordings WHERE user_id = ?`,
      [userId],
    );
    const chunkAgg = get<any>(
      `SELECT COALESCE(SUM(LENGTH(COALESCE(c.transcript,''))),0) AS chars, COUNT(*) AS chunks,
              SUM(CASE WHEN c.status='done' THEN 1 ELSE 0 END) AS done
       FROM chunks c JOIN recordings r ON r.id = c.recording_id WHERE r.user_id = ?`,
      [userId],
    );
    const summaryCount = get<any>(`SELECT COUNT(*) AS n FROM summaries WHERE user_id = ?`, [userId]);
    const days = get<any>(`SELECT COUNT(DISTINCT substr(started_at,1,10)) AS n FROM recordings WHERE user_id = ?`, [userId]);
    const activeJobs = get<any>(
      `SELECT COUNT(*) AS n FROM jobs j WHERE j.status IN ('pending','running')`,
    );
    return {
      recordings: totals.recordings,
      durationMs: totals.durationMs,
      chars: chunkAgg.chars,
      chunks: chunkAgg.chunks,
      doneChunks: chunkAgg.done ?? 0,
      summaries: summaryCount.n,
      activeDays: days.n,
      activeJobs: activeJobs.n,
    };
  });
}

export async function calendarRoutes(app: FastifyInstance) {
  app.get('/api/calendar', { preHandler: [app.authenticate] }, async (req) => {
    const userId = (req.user as any).sub;
    const { year, month } = req.query as any;
    const y = Number(year) || new Date().getFullYear();
    const m = Number(month) || new Date().getMonth() + 1;
    const start = new Date(y, m - 1, 1);
    const end = new Date(y, m, 0, 23, 59, 59, 999);

    const recs = all<any>(
      `SELECT id, title, started_at, duration_ms, status FROM recordings
       WHERE user_id = ? AND started_at >= ? AND started_at <= ? ORDER BY started_at ASC`,
      [userId, start.toISOString(), end.toISOString()],
    );
    const sums = all<any>(
      `SELECT id, title, date_from, status, scope FROM summaries
       WHERE user_id = ? AND date_from >= ? AND date_from <= ?`,
      [userId, `${y}-${String(m).padStart(2, '0')}-01`, `${y}-${String(m).padStart(2, '0')}-31`],
    );

    const map = new Map<string, any>();
    const keyOf = (iso: string) => {
      const d = new Date(iso);
      return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    };
    for (const r of recs) {
      const k = keyOf(r.started_at);
      const cur = map.get(k) ?? { date: k, recordings: 0, durationMs: 0, titles: [], summaryId: null, summaryStatus: null };
      cur.recordings += 1;
      cur.durationMs += r.duration_ms ?? 0;
      if (cur.titles.length < 3) cur.titles.push(r.title);
      map.set(k, cur);
    }
    for (const s of sums) {
      const cur = map.get(s.date_from) ?? { date: s.date_from, recordings: 0, durationMs: 0, titles: [], summaryId: null, summaryStatus: null };
      if (s.scope !== 'recording') {
        cur.summaryId = s.id;
        cur.summaryStatus = s.status;
        cur.summaryTitle = s.title;
      }
      map.set(s.date_from, cur);
    }

    return {
      year: y,
      month: m,
      days: [...map.values()].sort((a, b) => a.date.localeCompare(b.date)),
    };
  });
}
