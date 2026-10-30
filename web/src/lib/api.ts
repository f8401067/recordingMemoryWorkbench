export const TOKEN_KEY = 'recall.token';

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

type Options = Omit<RequestInit, 'body'> & { body?: unknown; raw?: boolean };

export async function api<T = any>(path: string, opts: Options = {}): Promise<T> {
  const headers = new Headers(opts.headers);
  const token = getToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);

  let body: BodyInit | undefined;
  if (opts.body instanceof FormData) {
    body = opts.body;
  } else if (opts.body !== undefined) {
    headers.set('Content-Type', 'application/json');
    body = JSON.stringify(opts.body);
  }

  let res: Response;
  try {
    res = await fetch(path, { ...opts, headers, body });
  } catch (err) {
    // 进程没起来 / 正在重启 / 网络断了：浏览器只会给一句 NetworkError，翻译成人话
    if ((err as Error)?.name === 'AbortError') throw err;
    throw new ApiError('连不上后端服务（可能正在启动或重启），请稍后重试', 0);
  }
  // 只有「带着 token 去请求却被打回」才算登录过期；
  // 登录接口本身也是 401（密码错），那时要把服务端的原话透出去。
  if (res.status === 401 && token) {
    setToken(null);
    window.dispatchEvent(new CustomEvent('recall:unauthorized'));
    throw new ApiError('登录已过期，请重新登录', 401);
  }
  const text = await res.text();
  let data: any = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!res.ok) {
    throw new ApiError(data?.error || data?.message || `请求失败 (${res.status})`, res.status);
  }
  return data as T;
}

export function uploadChunk(
  recordingId: string,
  seq: number,
  blob: Blob,
  opts: { durationMs: number; startOffsetMs: number },
) {
  const form = new FormData();
  const ext = blob.type.includes('mp4') ? '.m4a' : '.webm';
  form.append('file', blob, `chunk${seq}${ext}`);
  const q = new URLSearchParams({
    seq: String(seq),
    durationMs: String(Math.round(opts.durationMs)),
    startOffsetMs: String(Math.round(opts.startOffsetMs)),
  });
  return api<{
    chunkId: string;
    seq: number;
    status: string;
    durationMs: number;
    totalDurationMs: number;
    chunkCount: number;
  }>(`/api/recordings/${recordingId}/chunks?${q}`, { method: 'POST', body: form });
}

export function uploadFile(recordingId: string, file: File, segmentSec = 60) {
  const form = new FormData();
  form.append('file', file, file.name);
  return api<{ chunks: number; durationMs: number; segmentSec: number }>(
    `/api/recordings/${recordingId}/upload-file?segmentSec=${segmentSec}`,
    { method: 'POST', body: form },
  );
}

/* ---------------- 本地开源 ASR ---------------- */

export type LocalAsrBackend = 'sherpa-onnx' | 'faster-whisper' | 'whisper-cpp';

export type LocalAsrModel = {
  id: string;
  backend: LocalAsrBackend;
  label: string;
  sizeMB: number;
  hint: string;
  installed: boolean;
  downloadedMB: number | null;
};

export type LocalAsrDownload = {
  id: string;
  backend: LocalAsrBackend;
  modelId: string;
  status: 'downloading' | 'done' | 'error';
  received: number;
  total: number;
  message?: string | null;
};

export type LocalAsrPayload = {
  models: LocalAsrModel[];
  modelsDir: string;
  hfEndpoint?: string | null;
  downloads: LocalAsrDownload[];
  backends: Record<LocalAsrBackend, { available: boolean; detail: string; install: string }>;
};

export const getLocalAsr = (backend: LocalAsrBackend) =>
  api<LocalAsrPayload>(`/api/asr/local/models?backend=${backend}`);

export const downloadLocalAsrModel = (backend: LocalAsrBackend, model: string) =>
  api<LocalAsrDownload>('/api/asr/local/models/download', { method: 'POST', body: { backend, model } });

export const deleteLocalAsrModel = (backend: LocalAsrBackend, model: string) =>
  api<{ ok: boolean }>(`/api/asr/local/models/${backend}/${model}`, { method: 'DELETE' });

/* ---------------- 硬件设备 ---------------- */
export const listDevices = () => api<Device[]>('/api/devices');

export const listDeviceClaims = () => api<DeviceClaim[]>('/api/devices/claims');

export const bindDevice = (code: string, name?: string) =>
  api<Device>('/api/devices/bind', { method: 'POST', body: { code, name: name || undefined } });

export const resetDeviceToken = (id: string) =>
  api<Device>(`/api/devices/${id}/reset-token`, { method: 'POST' });

export const updateDevice = (id: string, patch: Partial<Pick<Device, 'name' | 'gapSec' | 'idleSec'>>) =>
  api<Device>(`/api/devices/${id}`, { method: 'PATCH', body: patch });

export const closeDeviceSession = (id: string) =>
  api<{ ok: boolean; closed: boolean; recordingId?: string | null }>(`/api/devices/${id}/close`, {
    method: 'POST',
  });

export const deleteDevice = (id: string) => api<{ ok: boolean }>(`/api/devices/${id}`, { method: 'DELETE' });


/* ---------------- SSE 订阅（带鉴权头，EventSource 不支持自定义 header） ---------------- */
export function subscribeRecording(
  id: string,
  onEvent: (event: string, data: any) => void,
): () => void {
  const ctrl = new AbortController();
  const token = getToken();

  void (async () => {
    try {
      const res = await fetch(`/api/recordings/${id}/stream`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        signal: ctrl.signal,
      });
      if (!res.ok || !res.body) return;
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx: number;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const raw = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          let ev = 'message';
          let data = '';
          for (const line of raw.split('\n')) {
            if (line.startsWith('event:')) ev = line.slice(6).trim();
            else if (line.startsWith('data:')) data += line.slice(5).trim();
          }
          if (ev !== 'ping') {
            try {
              onEvent(ev, data ? JSON.parse(data) : null);
            } catch {
              /* ignore malformed frame */
            }
          }
        }
      }
    } catch {
      /* aborted */
    }
  })();

  return () => ctrl.abort();
}

/* ---------------- 数据存储（内置 SQLite / 外置 PostgreSQL） ---------------- */
export type DbDriver = 'sqlite' | 'postgres';

export type DbConfig = {
  driver: DbDriver;
  host: string;
  port: number;
  database: string;
  user: string;
  sslmode: string;
  schema: string;
  hasPassword: boolean;
  target: string;
  configPath: string;
  sqlitePath: string;
  fromEnv: boolean;
};

export type DbStatus = {
  driver: DbDriver;
  target: string;
  connected: boolean;
  initialized: boolean;
  needsAdmin: boolean;
  userCount: number;
  serverVersion: string | null;
  error: string | null;
  config?: DbConfig;
};

export type DbPayload = Partial<Omit<DbConfig, 'hasPassword' | 'target' | 'configPath' | 'sqlitePath' | 'fromEnv'>> & {
  password?: string;
};

export const getDbStatus = () => api<DbStatus>('/api/setup/status');

export const getDbConfig = () => api<{ config: DbConfig; status: DbStatus }>('/api/database/config');

export const testDbConnection = (body: DbPayload) =>
  api<{ ok: boolean; message: string; serverVersion: string | null; databaseExists: boolean }>(
    '/api/database/test',
    { method: 'POST', body },
  );

export const applyDbConfig = (body: DbPayload) =>
  api<{
    ok: boolean;
    message: string;
    createdDatabase: boolean;
    needsAdmin: boolean;
    status: DbStatus;
    config: DbConfig;
  }>('/api/database/apply', { method: 'POST', body });

export const createFirstAdmin = (username: string, password: string) =>
  api<{ token: string; user: { id: string; username: string }; settings: UserSettings }>(
    '/api/setup/admin',
    { method: 'POST', body: { username, password } },
  );

/* ---------------- 类型 ---------------- */
export type Recording = {
  id: string;
  title: string;
  status: 'recording' | 'processing' | 'ready' | 'failed';
  source: string;
  language: string;
  startedAt: string;
  endedAt: string | null;
  date: string;
  durationMs: number;
  chunkCount: number;
  doneChunks: number;
  failedChunks: number;
  charCount: number;
  /** 转写正文摘要，列表里当"主要内容"用 */
  excerpt: string;
  summaryStatus: 'none' | 'queued' | 'generating' | 'ready' | 'failed';
  summaryId: string | null;
  summaryTitle: string | null;
  memo: string | null;
};

export type Segment = { startMs: number; endMs: number; text: string };

export type Summary = {
  id: string;
  recordingId: string | null;
  scope: 'recording' | 'day' | 'range';
  dateFrom: string;
  dateTo: string;
  title: string;
  markdown: string | null;
  mindmap: string | null;
  status: 'queued' | 'generating' | 'ready' | 'failed';
  error: string | null;
  model: string | null;
  stats: {
    recordings?: number;
    chunks?: number;
    chars?: number;
    durationMs?: number;
    highlights?: string[];
    todos?: { text: string; owner?: string; due?: string }[];
  } | null;
  createdAt: string;
  updatedAt: string;
};

export type RecordingDetail = Recording & {
  audio: {
    mode: 'source' | 'chunks' | 'none';
    url: string | null;
    mime: string | null;
    size: number;
    filename: string | null;
    durationMs: number;
  };
  chunks: {
    id: string;
    seq: number;
    status: string;
    durationMs: number;
    startOffsetMs: number;
    size: number;
    error: string | null;
    text: string;
    segmentCount: number;
  }[];
  transcript: {
    text: string;
    segments: Segment[];
    chunkCount: number;
    doneChunks: number;
    failedChunks: number;
  };
  summary: Summary | null;
};

export type ProviderItem = {
  id: string;
  category: 'asr' | 'llm';
  kind: string;
  name: string;
  baseUrl: string | null;
  model: string | null;
  params: Record<string, any>;
  isDefault: boolean;
  enabled: boolean;
  apiKeyMasked: string;
  hasApiKey: boolean;
};

/** 表单里还没保存的那份配置，用来先测连通 / 先拉模型 */
export type ProviderProbeInput = {
  providerId?: string;
  category: 'asr' | 'llm';
  kind: string;
  name?: string;
  baseUrl?: string;
  apiKey?: string;
  model?: string;
  params?: Record<string, any>;
};

export type ProviderProbeResult = { ok: boolean; message: string; latencyMs: number };

export const probeProvider = (body: ProviderProbeInput) =>
  api<ProviderProbeResult>('/api/providers/probe', { method: 'POST', body });

export const fetchProviderModels = (body: ProviderProbeInput) =>
  api<{ models: string[]; count: number }>('/api/providers/models', { method: 'POST', body });

export type UserSettings = {
  chunkSeconds: number;
  autoSummarize: boolean;
  summaryProviderId: string | null;
  summaryTemplate: 'meeting' | 'lecture' | 'interview' | 'idea' | 'general';
  /** 自定义整理要求，留空则用所选风格的内置提示词 */
  summaryPrompt: string;
  language: string;
  theme: 'light' | 'dark' | 'system';
};

export type CalendarDay = {
  date: string;
  recordings: number;
  durationMs: number;
  titles: string[];
  summaryId: string | null;
  summaryStatus: string | null;
  summaryTitle?: string;
};

/** 硬件设备：设备只推音频，会话边界由服务端按时间间隔判断 */
export type Device = {
  id: string;
  name: string;
  code: string | null;
  tokenMasked: string;
  paired: boolean;
  boundAt: string | null;
  gapSec: number;
  idleSec: number;
  openRecordingId: string | null;
  openRecordingTitle: string | null;
  openDurationMs: number;
  openChunks: number;
  sessionChunks: number;
  lastChunkAt: string | null;
  lastSeenAt: string | null;
  lastPollAt: string | null;
  createdAt: string;
};

/** 已经开机在轮询、但还没被认领的设备 */
export type DeviceClaim = {
  code: string;
  name: string | null;
  firmware: string | null;
  remoteIp: string | null;
  firstSeenAt: string;
  lastPollAt: string;
  pollCount: number;
};
