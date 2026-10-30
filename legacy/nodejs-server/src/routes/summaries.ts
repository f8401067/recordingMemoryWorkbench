import type { FastifyInstance } from 'fastify';
import { all, get, run, type Summary } from '../db/index.js';
import { createSummary, publicSummary, type SummaryTemplate } from '../services/summarize.js';
import { drain } from '../services/jobs.js';

function parseStats(s: Summary) {
  try {
    return s.stats ? JSON.parse(s.stats) : null;
  } catch {
    return null;
  }
}

export async function summaryRoutes(app: FastifyInstance) {
  app.get('/api/summaries', { preHandler: [app.authenticate] }, async (req) => {
    const userId = (req.user as any).sub;
    const { scope, from, to, limit } = req.query as any;
    const where = ['user_id = ?'];
    const params: any[] = [userId];
    if (scope) {
      where.push('scope = ?');
      params.push(scope);
    }
    if (from) {
      where.push('date_to >= ?');
      params.push(from);
    }
    if (to) {
      where.push('date_from <= ?');
      params.push(to);
    }
    const rows = all<Summary>(
      `SELECT * FROM summaries WHERE ${where.join(' AND ')} ORDER BY date_from DESC, created_at DESC LIMIT ?`,
      [...params, Math.min(200, Number(limit) || 60)],
    );
    return rows.map((r) => {
      const p = publicSummary(r);
      return { ...p, markdown: undefined, mindmap: undefined, highlights: parseStats(r)?.highlights ?? [] };
    });
  });

  app.get('/api/summaries/:id', { preHandler: [app.authenticate] }, async (req, reply) => {
    const userId = (req.user as any).sub;
    const { id } = req.params as any;
    const s = get<Summary>(`SELECT * FROM summaries WHERE id = ? AND user_id = ?`, [id, userId]);
    if (!s) return reply.code(404).send({ error: '纪要不存在' });
    return publicSummary(s);
  });

  app.post('/api/summaries/day', { preHandler: [app.authenticate] }, async (req, reply) => {
    const userId = (req.user as any).sub;
    const body = (req.body ?? {}) as any;
    const date = body.date;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(date ?? ''))) return reply.code(400).send({ error: '日期格式应为 YYYY-MM-DD' });
    try {
      const s = await createSummary({
        userId,
        scope: 'day',
        dateFrom: date,
        dateTo: date,
        providerId: body.providerId,
        template: body.template as SummaryTemplate,
        force: body.force ?? true,
      });
      await drain();
      return publicSummary(s);
    } catch (e: any) {
      return reply.code(400).send({ error: e?.message ?? '生成失败' });
    }
  });

  app.post('/api/summaries/range', { preHandler: [app.authenticate] }, async (req, reply) => {
    const userId = (req.user as any).sub;
    const body = (req.body ?? {}) as any;
    const { from, to } = body;
    if (!from || !to) return reply.code(400).send({ error: '需要 from 与 to' });
    try {
      const s = await createSummary({
        userId,
        scope: 'range',
        dateFrom: from,
        dateTo: to,
        providerId: body.providerId,
        template: body.template as SummaryTemplate,
        force: body.force ?? true,
      });
      await drain();
      return publicSummary(s);
    } catch (e: any) {
      return reply.code(400).send({ error: e?.message ?? '生成失败' });
    }
  });

  app.delete('/api/summaries/:id', { preHandler: [app.authenticate] }, async (req) => {
    const userId = (req.user as any).sub;
    const { id } = req.params as any;
    run(`DELETE FROM summaries WHERE id = ? AND user_id = ?`, [id, userId]);
    return { ok: true };
  });

  app.get('/api/summaries/:id/export', { preHandler: [app.authenticate] }, async (req, reply) => {
    const userId = (req.user as any).sub;
    const { id } = req.params as any;
    const s = get<Summary>(`SELECT * FROM summaries WHERE id = ? AND user_id = ?`, [id, userId]);
    if (!s) return reply.code(404).send({ error: '纪要不存在' });
    const md = [
      `# ${s.title}`,
      '',
      `> ${s.date_from}${s.date_to !== s.date_from ? ` - ${s.date_to}` : ''}`,
      '',
      s.markdown ?? '',
      '',
      '---',
      '',
      '## 思维导图大纲',
      '',
      '```markdown',
      s.mindmap ?? '',
      '```',
    ].join('\n');
    return reply
      .header('Content-Type', 'text/markdown; charset=utf-8')
      .header('Content-Disposition', `attachment; filename="${encodeURIComponent(s.title)}.md"`)
      .send(md);
  });
}
