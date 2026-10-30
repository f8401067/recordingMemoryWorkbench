import { all, get, run, type Recording, type Summary } from '../db/index.js';
import { nowIso, uid } from '../config.js';
import { buildLlm, resolveLlm } from './providers.js';
import { emitRecordingEvent } from '../lib/bus.js';
import { enqueue } from './jobs.js';
import { transcriptWithTimestamps } from './transcribe.js';

export type SummaryTemplate = 'meeting' | 'lecture' | 'interview' | 'idea' | 'general';

const MAX_INPUT_CHARS = 16_000;
const MAP_CHUNK_CHARS = 12_000;

const TEMPLATE_LABEL: Record<SummaryTemplate, string> = {
  meeting: '会议纪要',
  lecture: '课程/讲座笔记',
  interview: '访谈记录',
  idea: '灵感与想法',
  general: '内容纪要',
};

function pad(n: number) {
  return String(n).padStart(2, '0');
}

export function localDate(iso: string): string {
  const d = new Date(iso);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function localTime(iso: string): string {
  const d = new Date(iso);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function dayRange(dateFrom: string, dateTo: string) {
  return {
    from: new Date(`${dateFrom}T00:00:00`).toISOString(),
    to: new Date(`${dateTo}T23:59:59.999`).toISOString(),
  };
}

/* ------------------------------------------------------------------ */
/* 创建纪要任务                                                         */
/* ------------------------------------------------------------------ */
export type CreateSummaryOptions = {
  userId: string;
  scope: 'recording' | 'day' | 'range';
  recordingId?: string;
  dateFrom?: string;
  dateTo?: string;
  providerId?: string;
  template?: SummaryTemplate;
  force?: boolean;
  title?: string;
};

export async function createSummary(opts: CreateSummaryOptions): Promise<Summary> {
  let dateFrom = opts.dateFrom ?? '';
  let dateTo = opts.dateTo ?? dateFrom;

  if (opts.scope === 'recording') {
    const rec = get<Recording>(`SELECT * FROM recordings WHERE id = ? AND user_id = ?`, [
      opts.recordingId,
      opts.userId,
    ]);
    if (!rec) throw new Error('录音不存在');
    dateFrom = localDate(rec.started_at);
    dateTo = dateFrom;
  }
  if (opts.scope === 'day') dateTo = dateFrom;

  const existing =
    opts.scope === 'recording'
      ? get<Summary>(`SELECT * FROM summaries WHERE recording_id = ?`, [opts.recordingId])
      : get<Summary>(
          `SELECT * FROM summaries WHERE user_id = ? AND scope = ? AND date_from = ? AND date_to = ?`,
          [opts.userId, opts.scope, dateFrom, dateTo],
        );

  const template = opts.template ?? 'meeting';
  const t = nowIso();

  let summary: Summary;
  if (existing) {
    if (!opts.force && ['queued', 'generating', 'ready'].includes(existing.status)) {
      return existing;
    }
    run(
      `UPDATE summaries SET status = 'queued', error = NULL, provider_id = ?, title = ?, updated_at = ? WHERE id = ?`,
      [
        opts.providerId ?? null,
        opts.title ?? existing.title,
        t,
        existing.id,
      ],
    );
    summary = get<Summary>(`SELECT * FROM summaries WHERE id = ?`, [existing.id])!;
  } else {
    const id = uid('sum_');
    const title =
      opts.title ??
      (opts.scope === 'recording'
        ? ''
        : opts.scope === 'day'
          ? `${dateFrom} 日报`
          : `${dateFrom} 至 ${dateTo} 汇总`);
    run(
      `INSERT INTO summaries (id, user_id, recording_id, scope, date_from, date_to, title, provider_id, status, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)`,
      [id, opts.userId, opts.recordingId ?? null, opts.scope, dateFrom, dateTo, title, opts.providerId ?? null, t, t],
    );
    summary = get<Summary>(`SELECT * FROM summaries WHERE id = ?`, [id])!;
  }

  if (opts.scope === 'recording' && opts.recordingId) {
    run(`UPDATE recordings SET summary_status = 'queued' WHERE id = ?`, [opts.recordingId]);
    emitRecordingEvent(opts.recordingId, 'summary', { status: 'queued', summaryId: summary.id });
  }

  enqueue('summarize', { summaryId: summary.id, template, force: !!opts.force }, { maxAttempts: 2 });
  return summary;
}

/* ------------------------------------------------------------------ */
/* 生成纪要                                                             */
/* ------------------------------------------------------------------ */
type ContextBlock = {
  label: string;
  rangeLabel: string;
  body: string;
  stats: { recordings: number; chunks: number; chars: number; durationMs: number };
  fallbackTitle: string;
};

function buildContext(summary: Summary, template: SummaryTemplate): ContextBlock {
  if (summary.scope === 'recording' && summary.recording_id) {
    const rec = get<Recording>(`SELECT * FROM recordings WHERE id = ?`, [summary.recording_id]);
    if (!rec) throw new Error('录音不存在');
    const text = transcriptWithTimestamps(rec.id);
    if (!text.trim()) throw new Error('该录音还没有可用的转写内容');
    const chunkCount = (get(`SELECT COUNT(*) AS n FROM chunks WHERE recording_id = ?`, [rec.id]) as { n: number }).n;
    return {
      label: TEMPLATE_LABEL[template],
      rangeLabel: `${localDate(rec.started_at)} ${localTime(rec.started_at)}${
        rec.ended_at ? ` - ${localTime(rec.ended_at)}` : ''
      }`,
      body: text,
      stats: {
        recordings: 1,
        chunks: chunkCount,
        chars: text.length,
        durationMs: rec.duration_ms,
      },
      fallbackTitle: rec.title,
    };
  }

  const { from, to } = dayRange(summary.date_from, summary.date_to);
  const recs = all<Recording>(
    `SELECT * FROM recordings WHERE user_id = ? AND started_at >= ? AND started_at <= ?
       AND status IN ('ready','processing') ORDER BY started_at ASC`,
    [summary.user_id, from, to],
  );
  if (recs.length === 0) throw new Error('所选时间段内没有可整理的录音');

  const parts: string[] = [];
  let chars = 0;
  let chunks = 0;
  let duration = 0;
  for (const rec of recs) {
    const text = transcriptWithTimestamps(rec.id);
    if (!text.trim()) continue;
    parts.push(
      `### 录音：${rec.title}（${localDate(rec.started_at)} ${localTime(rec.started_at)}${
        rec.ended_at ? `-${localTime(rec.ended_at)}` : ''
      }）\n${text}`,
    );
    chars += text.length;
    duration += rec.duration_ms;
    chunks += (get(`SELECT COUNT(*) AS n FROM chunks WHERE recording_id = ?`, [rec.id]) as { n: number }).n;
  }
  if (parts.length === 0) throw new Error('所选时间段内没有可用的转写内容');

  return {
    label: TEMPLATE_LABEL[template],
    rangeLabel:
      summary.date_from === summary.date_to
        ? summary.date_from
        : `${summary.date_from} 至 ${summary.date_to}`,
    body: parts.join('\n\n'),
    stats: { recordings: recs.length, chunks, chars, durationMs: duration },
    fallbackTitle: summary.title,
  };
}

const SYSTEM_PROMPT = `你是「忆录」的资深内容整理助手。输入是语音识别得到的口语文本，可能包含错别字、口头禅、重复和断句错误。
请把它整理成高质量的结构化纪要，并同时输出思维导图大纲。

硬性要求：
1. 只使用原文真实出现的信息，绝不编造人名、数字、时间、结论。原文没有的内容就不写。
2. 修正明显的同音错别字，删除口语重复（"就是说""那个"），但保留原意和说话人立场。
3. 待办事项必须有原文依据；有明确负责人时写入 owner，没有则留空字符串。
4. 只输出 JSON，不要代码围栏，不要任何解释性文字。

JSON 结构：
{
  "title": "不超过 20 字的标题",
  "summary": "80-180 字的核心概要，一句话说清这段内容在讲什么、得出了什么",
  "markdown": "完整纪要（Markdown）。使用 ## 二级标题分节，建议包含：## 核心概要、## 关键内容、## 结论与决定、## 待办事项。要点用 - 列表，待办用 - [ ]。关键数字和结论加粗。",
  "mindmap": "思维导图大纲。第一行 # 主题；第二行起用 ## 表示 3-7 个主分支；每个主分支下用 ### 列 2-5 个要点。只使用 # 标题层级，禁止使用 - 或 * 列表符号。层级不超过三级。",
  "highlights": ["3-6 条最值得记住的信息，每条不超过 30 字"],
  "todos": [{"text": "待办内容", "owner": "", "due": ""}]
}`;

function userPrompt(ctx: ContextBlock, extraNote = '') {
  return `【整理类型】${ctx.label}
【时间范围】${ctx.rangeLabel}
【输入说明】以下为语音转写原文，方括号内是时间戳。
${extraNote}
--- 转写原文开始 ---
${ctx.body}
--- 转写原文结束 ---`;
}

function chunkText(text: string, size: number): string[] {
  if (text.length <= size) return [text];
  const lines = text.split('\n');
  const out: string[] = [];
  let cur = '';
  for (const line of lines) {
    if (cur.length + line.length > size && cur) {
      out.push(cur);
      cur = '';
    }
    cur += (cur ? '\n' : '') + line;
  }
  if (cur) out.push(cur);
  return out;
}

function extractJson(raw: string): any {
  let s = raw.trim();
  const fence = s.match(/```(?:json)?\s*([\s\S]*?)```/);
  if (fence) s = fence[1].trim();
  const start = s.indexOf('{');
  const end = s.lastIndexOf('}');
  if (start >= 0 && end > start) s = s.slice(start, end + 1);
  return JSON.parse(s);
}

function fallbackMindmap(title: string, markdown: string): string {
  const lines = [`# ${title || '纪要'}`];
  for (const line of markdown.split('\n')) {
    const h2 = line.match(/^##\s+(.+)/);
    if (h2) {
      lines.push(`## ${h2[1].trim()}`);
      continue;
    }
    const item = line.match(/^\s*[-*]\s+(?:\[.\]\s*)?(.+)/);
    if (item) lines.push(`### ${item[1].replace(/\*\*/g, '').slice(0, 40)}`);
  }
  return lines.join('\n');
}

export async function handleSummarizeJob(payload: {
  summaryId: string;
  template?: SummaryTemplate;
  force?: boolean;
}) {
  const summary = get<Summary>(`SELECT * FROM summaries WHERE id = ?`, [payload.summaryId]);
  if (!summary) return;
  if (summary.status === 'ready' && !payload.force) return;

  const template = (payload.template ?? 'meeting') as SummaryTemplate;

  run(`UPDATE summaries SET status = 'generating', error = NULL, updated_at = ? WHERE id = ?`, [
    nowIso(),
    summary.id,
  ]);
  if (summary.recording_id) {
    run(`UPDATE recordings SET summary_status = 'generating' WHERE id = ?`, [summary.recording_id]);
    emitRecordingEvent(summary.recording_id, 'summary', { status: 'generating', summaryId: summary.id });
  }

  try {
    const ctx = buildContext(summary, template);
    const provider = resolveLlm(summary.user_id, summary.provider_id);
    const llm = buildLlm(provider);

    let finalUser = userPrompt(ctx);
    if (ctx.body.length > MAX_INPUT_CHARS) {
      // 长文本：先分段提炼要点，再汇总
      const parts = chunkText(ctx.body, MAP_CHUNK_CHARS);
      const notes: string[] = [];
      for (let i = 0; i < parts.length; i++) {
        const note = await llm.chat(
          [
            { role: 'system', content: '你是会议内容提炼助手。请把这段口语转写浓缩成条目化要点，保留所有人名、数字、决定、待办和时间点。不要编造。只输出要点列表。' },
            { role: 'user', content: `第 ${i + 1}/${parts.length} 段：\n${parts[i]}` },
          ],
          { temperature: 0.2 },
        );
        notes.push(note.trim());
      }
      finalUser = userPrompt(
        { ...ctx, body: notes.join('\n\n') },
        '（说明：原始文本较长，以上是分段提炼后的要点，请据此整理完整纪要。）\n',
      );
    }

    const raw = await llm.chat(
      [
        { role: 'system', content: SYSTEM_PROMPT },
        { role: 'user', content: finalUser },
      ],
      { json: true, temperature: 0.3 },
    );

    let parsed: any;
    try {
      parsed = extractJson(raw);
    } catch {
      parsed = {
        title: ctx.fallbackTitle,
        summary: '',
        markdown: raw,
        mindmap: fallbackMindmap(ctx.fallbackTitle, raw),
        highlights: [],
        todos: [],
      };
    }

    const title = String(parsed.title || ctx.fallbackTitle || '未命名纪要').slice(0, 60);
    const markdown = String(parsed.markdown || parsed.summary || '');
    const mindmap = String(parsed.mindmap || fallbackMindmap(title, markdown));
    const stats = { ...ctx.stats, highlights: parsed.highlights ?? [], todos: parsed.todos ?? [] };

    run(
      `UPDATE summaries SET title = ?, markdown = ?, mindmap = ?, model = ?, provider_id = ?, status = 'ready',
         error = NULL, stats = ?, updated_at = ? WHERE id = ?`,
      [
        title,
        markdown,
        mindmap,
        provider.model ?? '',
        provider.id,
        JSON.stringify(stats),
        nowIso(),
        summary.id,
      ],
    );

    if (summary.recording_id) {
      run(`UPDATE recordings SET summary_status = 'ready' WHERE id = ?`, [summary.recording_id]);
      emitRecordingEvent(summary.recording_id, 'summary', {
        status: 'ready',
        summaryId: summary.id,
      });
    }
  } catch (e: any) {
    const msg = (e?.message ?? String(e)).slice(0, 500);
    run(`UPDATE summaries SET status = 'failed', error = ?, updated_at = ? WHERE id = ?`, [
      msg,
      nowIso(),
      summary.id,
    ]);
    if (summary.recording_id) {
      run(`UPDATE recordings SET summary_status = 'failed' WHERE id = ?`, [summary.recording_id]);
      emitRecordingEvent(summary.recording_id, 'summary', { status: 'failed', error: msg });
    }
    throw e;
  }
}

export function publicSummary(s: Summary) {
  let stats: any = null;
  try { stats = s.stats ? JSON.parse(s.stats) : null; } catch { stats = null; }
  return {
    id: s.id,
    recordingId: s.recording_id,
    scope: s.scope,
    dateFrom: s.date_from,
    dateTo: s.date_to,
    title: s.title,
    markdown: s.markdown,
    mindmap: s.mindmap,
    status: s.status,
    error: s.error,
    model: s.model,
    stats,
    createdAt: s.created_at,
    updatedAt: s.updated_at,
  };
}
