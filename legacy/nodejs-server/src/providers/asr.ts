import WebSocket from 'ws';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import type { Provider } from '../db/index.js';
import { toPcm16k, toWav16k, probeDurationMs, hasFfmpeg } from '../lib/ffmpeg.js';

export type AsrSegment = { startMs: number; endMs: number; text: string; speaker?: string };
export type AsrResult = { text: string; segments: AsrSegment[]; meta?: Record<string, unknown> };
export type AsrInput = { filePath: string; mime?: string; language?: string; durationMs?: number };

export interface AsrEngine {
  transcribe(input: AsrInput): Promise<AsrResult>;
}

function params(p: Provider): Record<string, any> {
  try {
    return p.params ? JSON.parse(p.params) : {};
  } catch {
    return {};
  }
}

function normalizeBase(baseUrl: string) {
  return baseUrl.replace(/\/+$/, '');
}

/* ------------------------------------------------------------------ */
/* 1. 阿里百炼 Paraformer 实时语音识别（WebSocket，逐句时间戳）          */
/* ------------------------------------------------------------------ */
class ParaformerRealtime implements AsrEngine {
  constructor(private p: Provider) {}

  async transcribe(input: AsrInput): Promise<AsrResult> {
    if (!this.p.api_key) throw new Error('缺少 DashScope API Key');
    if (!(await hasFfmpeg())) throw new Error('未检测到 ffmpeg，请先安装：brew install ffmpeg');

    const { pcm } = await toPcm16k(input.filePath);
    const cfg = params(this.p);
    const model = this.p.model || 'paraformer-realtime-v2';
    const url = cfg.wsUrl || 'wss://dashscope.aliyuncs.com/api-ws/v1/inference';
    const lang = input.language && input.language !== 'auto' ? [input.language] : ['zh', 'en'];

    const sentences = new Map<string, AsrSegment>();
    const taskId = crypto.randomUUID();
    const audioMs = Math.round(pcm.length / 2 / 16);

    return new Promise<AsrResult>((resolve, reject) => {
      let settled = false;
      let started = false;
      let sentAll = false;
      const ws = new WebSocket(url, {
        headers: {
          Authorization: `bearer ${this.p.api_key}`,
          'X-DashScope-DataInspection': 'enable',
        },
      });

      const timer = setTimeout(
        () => finish(new Error(`ASR 超时（${Math.round(audioMs / 1000)}s 音频）`)),
        Math.max(60_000, audioMs * 3 + 30_000),
      );

      const finish = (err?: Error) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        try { ws.close(); } catch { /* noop */ }
        if (err) return reject(err);
        const list = [...sentences.values()]
          .filter((s) => s.text.trim())
          .sort((a, b) => a.startMs - b.startMs);
        resolve({
          text: list.map((s) => s.text).join(''),
          segments: list,
          meta: { engine: 'paraformer-realtime', model },
        });
      };

      const sendAudio = async () => {
        const frameBytes = 3200; // 100ms @16k mono s16le
        for (let off = 0; off < pcm.length; off += frameBytes) {
          if (settled) return;
          while (ws.bufferedAmount > 2 * 1024 * 1024) {
            await new Promise((r) => setTimeout(r, 30));
          }
          ws.send(pcm.subarray(off, Math.min(off + frameBytes, pcm.length)));
          await new Promise((r) => setTimeout(r, 20)); // ≈5 倍速推送
        }
        sentAll = true;
        if (!settled) {
          ws.send(JSON.stringify({ header: { action: 'finish-task', task_id: taskId } }));
        }
      };

      ws.on('open', () => {
        ws.send(
          JSON.stringify({
            header: { action: 'run-task', task_id: taskId, streaming: 'duplex' },
            payload: {
              task_group: 'audio',
              task: 'asr',
              function: 'recognition',
              model,
              parameters: {
                format: 'pcm',
                sample_rate: 16000,
                language_hints: lang,
                disfluency_removal_enabled: false,
                punctuation_prediction_enabled: true,
                inverse_text_normalization_enabled: true,
              },
              input: {},
            },
          }),
        );
      });

      ws.on('message', (raw, isBinary) => {
        if (isBinary) return;
        let msg: any;
        try {
          msg = JSON.parse(raw.toString());
        } catch {
          return;
        }
        const event = msg?.header?.event;
        if (event === 'task-started') {
          if (!started) {
            started = true;
            void sendAudio().catch((e) => finish(e as Error));
          }
          return;
        }
        if (event === 'result-generated') {
          const s = msg?.payload?.output?.sentence;
          if (!s) return;
          const key = String(s.sentence_id ?? `${s.begin_time}-${s.end_time}`);
          sentences.set(key, {
            startMs: Number(s.begin_time ?? 0),
            endMs: Number(s.end_time ?? 0),
            text: String(s.text ?? ''),
          });
          return;
        }
        if (event === 'task-finished') {
          finish();
          return;
        }
        if (event === 'task-failed') {
          finish(new Error(msg?.header?.error_message || 'ASR 任务失败'));
        }
      });

      ws.on('error', (e) => finish(new Error(`ASR 连接失败：${e.message}`)));
      ws.on('close', () => {
        if (!settled) finish(sentAll ? undefined : new Error('ASR 连接意外关闭'));
      });
    });
  }
}

/* ------------------------------------------------------------------ */
/* 2. 阿里百炼 Qwen3-ASR（同步 HTTP，准确率高，无句级时间戳）            */
/* ------------------------------------------------------------------ */
class DashscopeQwenAsr implements AsrEngine {
  constructor(private p: Provider) {}

  async transcribe(input: AsrInput): Promise<AsrResult> {
    if (!this.p.api_key) throw new Error('缺少 DashScope API Key');
    if (!(await hasFfmpeg())) throw new Error('未检测到 ffmpeg，请先安装：brew install ffmpeg');

    const { wav, durationMs } = await toWav16k(input.filePath);
    const base = this.p.base_url || 'https://dashscope.aliyuncs.com';
    const cfg = params(this.p);
    const body: any = {
      model: this.p.model || 'qwen3-asr-flash',
      input: {
        messages: [
          { role: 'system', content: [{ text: '' }] },
          {
            role: 'user',
            content: [{ audio: `data:audio/wav;base64,${wav.toString('base64')}` }],
          },
        ],
      },
      parameters: {
        asr_options: {
          enable_lid: true,
          enable_itn: true,
          ...(cfg.asrOptions ?? {}),
        },
      },
    };
    if (input.language && input.language !== 'auto') body.parameters.asr_options.language = input.language;

    const res = await fetch(
      `${normalizeBase(base)}/api/v1/services/aigc/multimodal-generation/generation`,
      {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${this.p.api_key}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(body),
      },
    );
    const json: any = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(`ASR 请求失败 ${res.status}: ${JSON.stringify(json?.message ?? json).slice(0, 300)}`);
    }
    const content = json?.output?.choices?.[0]?.message?.content;
    let text = '';
    if (typeof content === 'string') text = content;
    else if (Array.isArray(content)) {
      text = content.map((c: any) => c?.text ?? '').join('');
    }
    text = text.trim();
    return {
      text,
      segments: text ? [{ startMs: 0, endMs: durationMs, text }] : [],
      meta: { engine: 'qwen3-asr', model: this.p.model || 'qwen3-asr-flash', usage: json?.usage },
    };
  }
}

/* ------------------------------------------------------------------ */
/* 3. OpenAI 兼容 /audio/transcriptions（whisper-1、faster-whisper 网关） */
/* ------------------------------------------------------------------ */
class OpenAiCompatibleAsr implements AsrEngine {
  constructor(private p: Provider) {}

  async transcribe(input: AsrInput): Promise<AsrResult> {
    if (!this.p.api_key) throw new Error('缺少 API Key');
    const base = normalizeBase(this.p.base_url || 'https://api.openai.com/v1');
    const root = base.endsWith('/v1') ? base : `${base}/v1`;
    const cfg = params(this.p);
    const buf = fs.readFileSync(input.filePath);
    const ext = path.extname(input.filePath) || '.webm';
    const mime = input.mime || guessMime(ext);

    const form = new FormData();
    form.append('file', new Blob([new Uint8Array(buf)], { type: mime }), `audio${ext}`);
    form.append('model', this.p.model || 'whisper-1');
    form.append('response_format', cfg.responseFormat || 'verbose_json');
    if (input.language && input.language !== 'auto') form.append('language', input.language);

    const res = await fetch(`${root}/audio/transcriptions`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${this.p.api_key}` },
      body: form,
    });
    const raw = await res.text();
    if (!res.ok) throw new Error(`ASR 请求失败 ${res.status}: ${raw.slice(0, 300)}`);

    let json: any;
    try {
      json = JSON.parse(raw);
    } catch {
      return { text: raw.trim(), segments: [], meta: { engine: 'openai-compatible' } };
    }
    const segments: AsrSegment[] = Array.isArray(json.segments)
      ? json.segments.map((s: any) => ({
          startMs: Math.round(Number(s.start ?? 0) * 1000),
          endMs: Math.round(Number(s.end ?? 0) * 1000),
          text: String(s.text ?? '').trim(),
        }))
      : [];
    return {
      text: String(json.text ?? '').trim(),
      segments,
      meta: { engine: 'openai-compatible', model: this.p.model },
    };
  }
}

/* ------------------------------------------------------------------ */
/* 4. 演示引擎：没有可用密钥时也能跑通全流程                              */
/* ------------------------------------------------------------------ */
const DEMO_LINES = [
  '这是一段由演示引擎生成的转写结果。',
  '配置真实的语音识别服务商之后，这里会显示真实的录音内容。',
  '当前使用的是模拟数据，用于验证上传、转写、纪要和思维导图的完整链路。',
];

class MockAsr implements AsrEngine {
  async transcribe(input: AsrInput): Promise<AsrResult> {
    const dur = input.durationMs || (await probeDurationMs(input.filePath)) || 6000;
    const per = Math.max(1500, Math.floor(dur / DEMO_LINES.length));
    const segments = DEMO_LINES.map((text, i) => ({
      startMs: i * per,
      endMs: Math.min(dur, (i + 1) * per),
      text,
    }));
    await new Promise((r) => setTimeout(r, 400));
    return {
      text: segments.map((s) => s.text).join(''),
      segments,
      meta: { engine: 'mock' },
    };
  }
}

function guessMime(ext: string) {
  switch (ext.toLowerCase()) {
    case '.webm': return 'audio/webm';
    case '.mp4':
    case '.m4a': return 'audio/mp4';
    case '.mp3': return 'audio/mpeg';
    case '.wav': return 'audio/wav';
    case '.ogg': return 'audio/ogg';
    default: return 'application/octet-stream';
  }
}

export function createAsrEngine(p: Provider): AsrEngine {
  switch (p.kind) {
    case 'dashscope-paraformer':
      return new ParaformerRealtime(p);
    case 'dashscope-qwen-asr':
      return new DashscopeQwenAsr(p);
    case 'openai-whisper':
      return new OpenAiCompatibleAsr(p);
    case 'mock':
      return new MockAsr();
    default:
      throw new Error(`未知的 ASR 引擎类型：${p.kind}`);
  }
}

export const ASR_KINDS = [
  { kind: 'dashscope-paraformer', label: '阿里百炼 · Paraformer 实时', hint: '逐句时间戳，流式推送本地音频，无需公网地址。推荐。' },
  { kind: 'dashscope-qwen-asr', label: '阿里百炼 · Qwen3-ASR', hint: '同步接口，识别准确率高，无句级时间戳。' },
  { kind: 'openai-whisper', label: 'OpenAI 兼容 /audio/transcriptions', hint: '可填任意支持 whisper 协议的网关或自建服务。' },
  { kind: 'mock', label: '演示引擎（模拟数据）', hint: '不调用外部服务，用于验证整体流程。' },
];
