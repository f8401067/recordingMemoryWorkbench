import { all, get, run, type Provider } from '../db/index.js';
import { nowIso, uid } from '../config.js';
import { createAsrEngine, ASR_KINDS } from '../providers/asr.js';
import { createLlmEngine, LLM_KINDS } from '../providers/llm.js';

export function maskKey(key: string | null): string {
  if (!key) return '';
  if (key.length <= 10) return '****';
  return `${key.slice(0, 4)}...${key.slice(-4)}`;
}

export function publicProvider(p: Provider) {
  return {
    id: p.id,
    category: p.category,
    kind: p.kind,
    name: p.name,
    baseUrl: p.base_url,
    model: p.model,
    params: safeParse(p.params),
    isDefault: !!p.is_default,
    enabled: !!p.enabled,
    apiKeyMasked: maskKey(p.api_key),
    hasApiKey: !!p.api_key,
  };
}

function safeParse(s: string | null) {
  if (!s) return {};
  try { return JSON.parse(s); } catch { return {}; }
}

/** 新用户初始化：各建一个演示 provider，保证零配置可用 */
export function seedDemoProviders(userId: string) {
  const existing = get(`SELECT COUNT(*) AS n FROM providers WHERE user_id = ?`, [userId]) as { n: number };
  if (existing.n > 0) return;
  const t = nowIso();
  run(
    `INSERT INTO providers (id, user_id, category, kind, name, base_url, api_key, model, params, is_default, enabled, created_at)
     VALUES (?, ?, 'asr', 'mock', ?, NULL, NULL, 'mock', NULL, 1, 1, ?)`,
    [uid('prv_'), userId, '演示引擎', t],
  );
  run(
    `INSERT INTO providers (id, user_id, category, kind, name, base_url, api_key, model, params, is_default, enabled, created_at)
     VALUES (?, ?, 'llm', 'mock', ?, NULL, NULL, 'mock', NULL, 1, 1, ?)`,
    [uid('prv_'), userId, '演示引擎', t],
  );
}

export function findProvider(userId: string, id: string): Provider | undefined {
  return get<Provider>(`SELECT * FROM providers WHERE id = ? AND user_id = ?`, [id, userId]);
}

export function defaultProvider(userId: string, category: 'asr' | 'llm'): Provider | undefined {
  return (
    get<Provider>(
      `SELECT * FROM providers WHERE user_id = ? AND category = ? AND enabled = 1 AND is_default = 1 LIMIT 1`,
      [userId, category],
    ) ??
    get<Provider>(
      `SELECT * FROM providers WHERE user_id = ? AND category = ? AND enabled = 1 ORDER BY created_at ASC LIMIT 1`,
      [userId, category],
    )
  );
}

/** 解析 ASR：优先指定 → 用户默认 → 演示引擎 */
export function resolveAsr(userId: string, preferredId?: string | null): Provider {
  if (preferredId) {
    const p = findProvider(userId, preferredId);
    if (p && p.enabled) return p;
  }
  const def = defaultProvider(userId, 'asr');
  if (def) return def;
  return virtualMock('asr', userId);
}

export function resolveLlm(userId: string, preferredId?: string | null): Provider {
  if (preferredId) {
    const p = findProvider(userId, preferredId);
    if (p && p.enabled) return p;
  }
  const def = defaultProvider(userId, 'llm');
  if (def) return def;
  return virtualMock('llm', userId);
}

function virtualMock(category: 'asr' | 'llm', userId: string): Provider {
  return {
    id: `virtual-mock-${category}`,
    user_id: userId,
    category,
    kind: 'mock',
    name: '演示引擎',
    base_url: null,
    api_key: null,
    model: 'mock',
    params: null,
    is_default: 1,
    enabled: 1,
    created_at: nowIso(),
  };
}

export function buildAsr(p: Provider) {
  return createAsrEngine(p);
}
export function buildLlm(p: Provider) {
  return createLlmEngine(p);
}

export function providerCatalog() {
  return { asr: ASR_KINDS, llm: LLM_KINDS };
}

/** 连通性测试：ASR 用 1 秒静音 PCM 走一遍，LLM 发一句最短对话 */
export async function testProvider(p: Provider): Promise<{ ok: boolean; message: string; latencyMs: number }> {
  const t0 = Date.now();
  try {
    if (p.category === 'asr') {
      const engine = createAsrEngine(p);
      if (p.kind === 'mock') {
        return { ok: true, message: '演示引擎可用（不调用外部服务）', latencyMs: Date.now() - t0 };
      }
      const fs = await import('node:fs');
      const path = await import('node:path');
      const { config } = await import('../config.js');
      // 生成 0.6 秒 16k 静音 wav 做探测
      const sr = 16000;
      const samples = Math.floor(sr * 0.6);
      const dataLen = samples * 2;
      const buf = Buffer.alloc(44 + dataLen);
      buf.write('RIFF', 0); buf.writeUInt32LE(36 + dataLen, 4); buf.write('WAVE', 8);
      buf.write('fmt ', 12); buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20);
      buf.writeUInt16LE(1, 22); buf.writeUInt32LE(sr, 24); buf.writeUInt32LE(sr * 2, 28);
      buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34);
      buf.write('data', 36); buf.writeUInt32LE(dataLen, 40);
      const tmp = path.join(config.dataDir, 'tmp', `probe-${Date.now()}.wav`);
      fs.writeFileSync(tmp, buf);
      try {
        await engine.transcribe({ filePath: tmp, mime: 'audio/wav', durationMs: 600 });
        return { ok: true, message: '连接成功，识别接口可正常调用', latencyMs: Date.now() - t0 };
      } finally {
        fs.rmSync(tmp, { force: true });
      }
    }
    const llm = createLlmEngine(p);
    if (p.kind === 'mock') {
      return { ok: true, message: '演示引擎可用（不调用外部服务）', latencyMs: Date.now() - t0 };
    }
    const text = await llm.chat([{ role: 'user', content: '只回复两个字：正常' }], { maxTokens: 16 });
    return { ok: true, message: `连接成功，模型回复：${text.slice(0, 40)}`, latencyMs: Date.now() - t0 };
  } catch (e: any) {
    return { ok: false, message: (e?.message ?? String(e)).slice(0, 400), latencyMs: Date.now() - t0 };
  }
}

export function listProviders(userId: string, category?: string) {
  const rows = category
    ? all<Provider>(`SELECT * FROM providers WHERE user_id = ? AND category = ? ORDER BY is_default DESC, created_at ASC`, [userId, category])
    : all<Provider>(`SELECT * FROM providers WHERE user_id = ? ORDER BY category, is_default DESC, created_at ASC`, [userId]);
  return rows.map(publicProvider);
}
