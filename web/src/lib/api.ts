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

/* ---------------- 录音记录 ---------------- */

export const deleteRecording = (id: string) =>
  api<{ ok: boolean }>(`/api/recordings/${id}`, { method: 'DELETE' });

/** 补跑说话人分离（不重转写）；force 用于旧版本没保存声纹质心的录音 */
export const diarizeRecording = (id: string, force = false) =>
  api<{ ok: boolean; queued: boolean }>(
    `/api/recordings/${id}/diarize${force ? '?force=true' : ''}`,
    { method: 'POST' },
  );

/** 手动修订第 index 句转写文字（index 就是全文列表里的下标） */
export const updateTranscriptSegment = (id: string, index: number, text: string) =>
  api<{ ok: boolean; transcript: RecordingDetail['transcript'] }>(
    `/api/recordings/${id}/transcript`,
    { method: 'PATCH', body: { index, text } },
  );

/* ---------------- 找寻 ---------------- */

export type FindMatch = {
  recordingId: string | null;
  title: string;
  date: string;
  time: string;
  startedAt: string | null;
  durationMs: number;
  snippet: string;
  kind: string;
  /** 命中句在录音内的时间点（毫秒），用于直接定位到原话 */
  startMs: number | null;
  score: number;
  matchedBy: string;
  cited: boolean;
  /** 跳到「那一天」的录音列表 */
  jumpUrl: string;
  recordingUrl: string | null;
};

export type FindResult = {
  answer: string;
  matches: FindMatch[];
  evidence: {
    index: number;
    date: string;
    time: string;
    title: string;
    kind: string;
    text: string;
  }[];
  trace: {
    round?: number;
    tool: string;
    args?: Record<string, unknown>;
    reason?: string;
    label?: string;
    hits: number;
    /** 'mcp' 表示这是外部 MCP 工具的调用 */
    kind?: string;
    ok?: boolean;
  }[];
  mode: 'llm' | 'demo' | 'fallback';
  note: string;
  provider: { id: string | null; name: string | null; model: string | null };
  mcp?: {
    tools: {
      key: string;
      name: string;
      title: string;
      description: string;
      params: string[];
      serverId: string;
      serverName: string;
    }[];
    pushes: McpPushResult[];
    pushNote?: string;
    manualTargets: { serverId: string; serverName: string; tool?: string }[];
    /** 大模型按本次问答挑出来的工具；界面默认只展示这些 */
    recommendedTools?: McpRecommendedTool[];
    recommendBy?: 'llm' | 'mock' | 'none';
    recommendReason?: string;
    recommendNote?: string;
  };
  tookMs: number;
};

export type McpRecommendedTool = {
  key: string;
  name: string;
  title: string;
  description: string;
  params: string[];
  serverId: string;
  serverName: string;
  reason: string;
};

export const findInRecordings = (
  question: string,
  history: { role: 'user' | 'assistant'; content: string }[] = [],
  providerId?: string,
) =>
  api<FindResult>('/api/find', {
    method: 'POST',
    body: { question, history, providerId },
  });

/* ---------------- 行动（从各份纪要汇总出来的待办） ---------------- */

export type ActionItem = {
  id: string;
  text: string;
  tag: string;
  owner: string;
  due: string;
  date: string;
  summaryId: string;
  summaryTitle: string;
  source: string;
  status: 'pending' | 'done';
  createdAt: string;
  completedAt?: string | null;
};

/** AI 抽出来的候选：还没落成正式待办，点 + 之后才会变成 ActionItem */
export type ActionSuggestion = {
  originKey: string;
  text: string;
  tag: string;
  owner: string;
  due: string;
  date: string;
  dateTo: string;
  summaryId: string;
  summaryTitle: string;
  scope: 'recording' | 'day' | 'range';
  source: string;
  createdAt: string;
};

export type ActionsPayload = {
  items: ActionItem[];
  suggestions: ActionSuggestion[];
  total: number;
  counts: Record<string, number>;
  tags: string[];
  today: string;
};

export const listActions = (tag?: string) =>
  api<ActionsPayload>(
    `/api/actions${tag && tag !== '全部' ? `?tag=${encodeURIComponent(tag)}` : ''}`,
  );

/** 把一条「猜你想做」变成正式待办 */
export const acceptAction = (s: ActionSuggestion) =>
  api<{ ok: boolean; created: boolean; item: ActionItem }>('/api/actions/accept', {
    method: 'POST',
    body: {
      text: s.text,
      tag: s.tag,
      owner: s.owner,
      due: s.due,
      date: s.date,
      summaryId: s.summaryId,
    },
  });

/** 勾选 / 取消勾选正式待办 */
export const setActionStatus = (id: string, status: 'pending' | 'done') =>
  api<{ ok: boolean; item: ActionItem }>(`/api/actions/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    body: { status },
  });

/* ---------------- 纪要 ---------------- */

/** 手动修改纪要标题 / 正文（只改文字，不重新调用模型） */
export const updateSummary = (id: string, patch: { title?: string; markdown?: string; mindmap?: string }) =>
  api<Summary>(`/api/summaries/${id}`, { method: 'PATCH', body: patch });

/** AI 修改纪要：提一句要求，后台排队让模型按当前内容修订 */
export const reviseSummary = (id: string, instruction: string) =>
  api<Summary>(`/api/summaries/${id}/revise`, { method: 'POST', body: { instruction } });

/* ---------------- 本地开源 ASR ---------------- */

export type LocalAsrBackend = 'sherpa-onnx' | 'faster-whisper' | 'whisper-cpp' | 'mlx-qwen3-asr';

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
  backend: LocalAsrBackend | 'speaker-diarization';
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

export type DiarizationStatus = {
  installed: boolean;
  modelsDir: string;
  download: LocalAsrDownload | null;
};

export const getDiarization = () => api<DiarizationStatus>('/api/asr/local/diarization');

export const downloadDiarization = () =>
  api<LocalAsrDownload>('/api/asr/local/diarization/download', { method: 'POST' });

export const deleteDiarizationModel = () =>
  api<{ ok: boolean }>('/api/asr/local/diarization/model', { method: 'DELETE' });

/* ---------------- 声纹角色 ---------------- */
export type VoiceProfile = {
  id: string;
  name: string;
  /** 「我」是特殊角色：用来判断这段录音我有没有参与 */
  isSelf: boolean;
  sampleCount: number;
  sourceRecordingId: string | null;
  sourceSpeaker: number | null;
  createdAt: string;
  updatedAt: string;
};

export const getVoiceProfiles = () =>
  api<{ profiles: VoiceProfile[] }>('/api/voice-profiles');

export const enrollVoiceProfile = (body: {
  recordingId: string;
  speaker: number;
  name?: string;
  isSelf?: boolean;
}) =>
  api<{ ok: boolean; profile: VoiceProfile; profiles: VoiceProfile[] }>(
    '/api/voice-profiles/from-recording',
    { method: 'POST', body },
  );

export const deleteVoiceProfile = (id: string) =>
  api<{ ok: boolean; profiles: VoiceProfile[] }>(`/api/voice-profiles/${id}`, { method: 'DELETE' });

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

/* ---------------- 数据库状态（只读：连接信息写在配置文件里） ---------------- */
export type DbStatus = {
  target: string;
  connected: boolean;
  initialized: boolean;
  needsAdmin: boolean;
  userCount: number;
  serverVersion: string | null;
  error: string | null;
  /** 配置文件路径，连不上时提示用户去改哪一行 */
  configFile?: string;
  configKey?: string;
};

export const getDbStatus = () => api<DbStatus>('/api/setup/status');

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
  /** 转写正文摘要：仅用于搜索匹配，列表卡片展示的是 AI 关键信息 */
  excerpt: string;
  /** 没有人声 / 没有实际内容：列表排最后并默认收起 */
  lowValue: boolean;
  lowReason: string | null;
  /** 由相隔不到 5 分钟的几段碎片合并而来时，原始条数（0/1 表示没合并过） */
  mergedCount: number;
  summaryStatus: 'none' | 'queued' | 'generating' | 'ready' | 'failed';
  summaryId: string | null;
  summaryTitle: string | null;
  /** AI 整理出的关键信息，列表卡片优先展示这些而不是转写原文 */
  summaryHighlights: string[];
  memo: string | null;
  /** 是否跑过说话人分离 */
  diarized?: boolean;
  speakerCount?: number | null;
  /** true 我参与了 / false 我未参与 / null 未判定（没录「我」或处在灰区） */
  userPresent?: boolean | null;
  userSimilarity?: number | null;
};

export type Segment = {
  startMs: number;
  endMs: number;
  text: string;
  speaker?: number;
  /** 声纹匹配到的角色名；没有录入角色时为空 */
  speakerName?: string | null;
  speakerIsSelf?: boolean;
};

export type SpeakerMatch = {
  speaker: number;
  profileId: string;
  name: string;
  isSelf: boolean;
  score: number;
  speechMs: number;
};

export type Summary = {
  id: string;
  recordingId: string | null;
  scope: 'recording' | 'day' | 'range';
  dateFrom: string;
  dateTo: string;
  title: string;
  markdown: string | null;
  mindmap: string | null;
  status: 'queued' | 'generating' | 'waiting' | 'ready' | 'failed';
  error: string | null;
  model: string | null;
  stats: {
    recordings?: number;
    chunks?: number;
    chars?: number;
    durationMs?: number;
    childSummaries?: number;
    childMissing?: number;
    highlights?: string[];
    todos?: { text: string; tag?: string; owner?: string; due?: string }[];
    moods?: { person: string; mood: string; evidence?: string }[];
  } | null;
  createdAt: string;
  updatedAt: string;
};

export type RecordingDetail = Recording & {
  speakers: SpeakerMatch[];
  /** 这条录音有没有可用于录入角色的声纹；旧版本分离的录音需要重新转写 */
  voiceprintReady?: boolean;
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
  /** 本地说话人分离：转写完成后自动标注「说话人 1/2/…」 */
  speakerDiarization: boolean;
  diarizationThreshold: number;
  /** 声纹角色匹配阈值（余弦相似度） */
  voiceprintThreshold?: number;
  summaryProviderId: string | null;
  summaryTemplate: 'meeting' | 'lecture' | 'interview' | 'idea' | 'general';
  /** 单条录音的自定义整理要求，留空则用所选风格的内置提示词 */
  summaryPrompt: string;
  /** 日报 / 周报 / 月报 / 年报 / 任意时段：各自附加的提示词 */
  promptDay: string;
  promptWeek: string;
  promptMonth: string;
  promptYear: string;
  promptRange: string;
  language: string;
  theme: 'light' | 'dark' | 'system';
};

/* ---------------- 系统状态：任务与日志 ---------------- */

export type SystemTask = {
  id: string;
  type: string;
  typeLabel: string;
  status: 'pending' | 'running' | 'done' | 'failed';
  title: string;
  detail: string;
  scope: string | null;
  recordingId: string | null;
  attempts: number;
  maxAttempts: number;
  error: string | null;
  createdAt: string | null;
  startedAt: string | null;
  heartbeatAt: string | null;
  finishedAt: string | null;
  runAt: string | null;
  elapsedMs: number | null;
  heartbeatAgeMs: number | null;
  ownerId: string | null;
  retryable: boolean;
};

export type TaskBoard = {
  now: string;
  running: SystemTask[];
  queued: SystemTask[];
  recent: SystemTask[];
  counts: { running: number; queued: number; failed: number; done: number };
  concurrency: Record<string, number>;
  worker: { heartbeatSeconds: number; staleSeconds: number };
};

export const getTaskBoard = () => api<TaskBoard>('/api/system/tasks');

export const retryTask = (id: string) =>
  api<{ ok: boolean }>(`/api/system/tasks/${id}/retry`, { method: 'POST' });

export type LogEntry = {
  id: number;
  ts: string;
  level: 'debug' | 'info' | 'warn' | 'error';
  source: string;
  message: string;
};

export type LogPage = { entries: LogEntry[]; total: number; capacity: number };

export const getLogs = (opts: { limit?: number; level?: string; q?: string; after?: number } = {}) => {
  const q = new URLSearchParams();
  if (opts.limit) q.set('limit', String(opts.limit));
  if (opts.level && opts.level !== 'all') q.set('level', opts.level);
  if (opts.q) q.set('q', opts.q);
  if (opts.after) q.set('after', String(opts.after));
  return api<LogPage>(`/api/system/logs?${q.toString()}`);
};

export const clearLogs = () => api<{ ok: boolean }>('/api/system/logs', { method: 'DELETE' });

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

/* ---------------- MCP 工具 ---------------- */

export type McpTransport = 'auto' | 'http' | 'sse';
export type McpPushMode = 'off' | 'manual' | 'auto';

/** 添加 / 刷新服务时由 inputSchema 推断出来的能力，存库缓存 */
export type McpToolCapability = {
  /** 判定规则版本，升级后旧缓存会自动重算 */
  v?: number;
  sendSummary: boolean;
  bodyField: string;
  titleField: string;
  dateField: string;
  reason: string;
  /** 人工指定/排除过；刷新服务、规则版本升级都不覆盖 */
  manual?: boolean;
  /** 人工覆盖前的自动判定结果，用于「恢复自动」前给个参照 */
  autoSendSummary?: boolean;
};

export type McpTool = {
  name: string;
  title: string;
  description: string;
  inputSchema: Record<string, any>;
  capability?: McpToolCapability;
  /** 大模型按 schema 生成的建议参数（调试面板预填，可人工改） */
  suggestedArgs?: Record<string, unknown>;
};

export type McpServer = {
  id: string;
  name: string;
  url: string;
  transport: McpTransport;
  enabled: boolean;
  useInChat: boolean;
  pushMode: McpPushMode;
  pushTool: string;
  pushArgs: string;
  pushHint: string;
  headers: Record<string, string>;
  hasHeaders: boolean;
  lastStatus: '' | 'ok' | 'error';
  lastError: string;
  lastConnectedAt: string | null;
  toolCount: number;
  /** 其中被识别为「可发送 AI 纪要」的工具数 */
  summaryToolCount: number;
  createdAt: string | null;
  tools?: McpTool[];
  probe?: {
    ok: boolean;
    error?: string | null;
    latencyMs?: number;
    transport?: string | null;
    protocolVersion?: string;
    serverInfo?: { name?: string; version?: string };
  };
};

export type McpCallLog = {
  id: string;
  serverId: string | null;
  tool: string;
  args: Record<string, unknown>;
  ok: boolean;
  result: string;
  error: string;
  durationMs: number;
  source: 'debug' | 'chat' | 'push' | 'summary';
  createdAt: string;
};

export type McpCallResult = {
  ok: boolean;
  isError: boolean;
  error: string | null;
  text: string;
  content: { type: string; text?: string; data?: unknown }[];
  structured: unknown;
  latencyMs: number;
  transport: string | null;
};

export const listMcpServers = () => api<McpServer[]>('/api/mcp/servers');

export const createMcpServer = (body: Partial<McpServer>) =>
  api<McpServer>('/api/mcp/servers', { method: 'POST', body });

export const updateMcpServer = (id: string, body: Partial<McpServer>) =>
  api<McpServer>(`/api/mcp/servers/${id}`, { method: 'PATCH', body });

export const deleteMcpServer = (id: string) =>
  api<{ ok: boolean }>(`/api/mcp/servers/${id}`, { method: 'DELETE' });

export const refreshMcpServer = (id: string) =>
  api<McpServer>(`/api/mcp/servers/${id}/refresh`, { method: 'POST' });

export const importMcpServers = (json: string) =>
  api<{ added: McpServer[]; skipped: { name: string; reason: string }[] }>(
    '/api/mcp/servers/import',
    { method: 'POST', body: { json } },
  );

/** 手动指定/取消「可发送纪要」，或 { reset: true } 恢复自动判定 */
export const setMcpToolCapability = (
  serverId: string,
  tool: string,
  body: { sendSummary?: boolean; bodyField?: string; titleField?: string; dateField?: string; reset?: boolean },
) =>
  api<McpServer>(`/api/mcp/servers/${serverId}/tools/${encodeURIComponent(tool)}/capability`, {
    method: 'PATCH',
    body,
  });

export const callMcpTool = (serverId: string, tool: string, args: Record<string, unknown>) =>
  api<McpCallResult>(`/api/mcp/servers/${serverId}/tools/${encodeURIComponent(tool)}/call`, {
    method: 'POST',
    body: { args },
  });

export const listMcpLogs = (serverId?: string, limit = 20) =>
  api<McpCallLog[]>(
    `/api/mcp/logs?limit=${limit}${serverId ? `&serverId=${encodeURIComponent(serverId)}` : ''}`,
  );

export const listChatTools = () =>
  api<{
    count: number;
    tools: {
      key: string;
      name: string;
      title: string;
      description: string;
      params: string[];
      serverId: string;
      serverName: string;
    }[];
  }>('/api/mcp/tools');

/** scope=summary：添加 MCP 时识别出来的、能接收 AI 纪要正文的工具 */
export type McpSummaryTool = {
  key: string;
  name: string;
  title: string;
  description: string;
  params: string[];
  serverId: string;
  serverName: string;
  bodyField: string;
  titleField: string;
  sendSummary: boolean;
  /** 大模型按 schema 生成的建议参数（发送前的默认值，可人工改） */
  suggestedArgs?: Record<string, unknown>;
  /** 工具的完整 inputSchema（参数预览弹窗里按它渲染字段） */
  schema?: Record<string, any>;
};

export const listMcpSummaryTools = () =>
  api<{ count: number; tools: McpSummaryTool[] }>('/api/mcp/tools?scope=summary');

/** AI 纪要下方的一键发送：点哪个工具就直接用哪个，参数由服务端按 schema 填好 */
export const runMcpSummaryTool = (body: {
  summaryId: string;
  serverId: string;
  tool: string;
  /** 用户在预览里改过的键；会覆盖建议参数，未声明的键会被服务端丢掉 */
  args?: Record<string, unknown>;
}) =>
  api<{
    ok: boolean;
    results: McpPushResult[];
    message: string;
    by?: string;
  }>('/api/mcp/run-tool', { method: 'POST', body });

/** 纪要发送前：建议参数 + 纪要内容合并后的参数预览（不真正调用） */
export const previewMcpSummaryTool = (body: {
  summaryId: string;
  serverId: string;
  tool: string;
}) =>
  api<{
    tool: string;
    serverId: string;
    serverName: string;
    args: Record<string, unknown>;
    schema: Record<string, any>;
    bodyField: string;
    note: string;
  }>('/api/mcp/run-tool-preview', { method: 'POST', body });

/** 现场让大模型按工具 schema 重新生成一份建议参数 */
export const suggestMcpToolArgs = (
  serverId: string,
  tool: string,
  body?: { providerId?: string },
) =>
  api<{ args: Record<string, unknown>; note: string }>(
    `/api/mcp/servers/${serverId}/tools/${encodeURIComponent(tool)}/suggest-args`,
    { method: 'POST', body: body ?? {} },
  );

/** 把人工改过的参数存为这个工具的默认值（刷新服务也保留） */
export const setMcpToolArgs = (
  serverId: string,
  tool: string,
  args: Record<string, unknown>,
) =>
  api<McpServer>(
    `/api/mcp/servers/${serverId}/tools/${encodeURIComponent(tool)}/args`,
    { method: 'PATCH', body: { args } },
  );

/** 读库里存的工具建议参数（调试面板预填用） */
export const getMcpToolArgs = (serverId: string, tool: string) =>
  api<{ args: Record<string, unknown> }>(
    `/api/mcp/servers/${serverId}/tools/${encodeURIComponent(tool)}/args`,
  );

export type McpPushResult = {
  serverId: string;
  serverName: string;
  tool: string;
  ok: boolean;
  message: string;
  durationMs: number;
  reason?: string;
  by?: 'llm' | 'fallback' | 'none' | 'direct';
};

export const pushFindAnswer = (
  question: string,
  answer: string,
  opts: { serverIds?: string[]; matches?: string } = {},
) =>
  api<{ ok: boolean; results: McpPushResult[]; message: string; by?: string; reason?: string }>('/api/find/push', {
    method: 'POST',
    body: { question, answer, serverIds: opts.serverIds, matches: opts.matches },
  });
