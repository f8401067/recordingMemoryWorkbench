import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  AlertCircle, AudioLines, BrainCircuit, Check, CheckCircle2, Copy, Cpu,
  Eye, EyeOff, FileText, KeyRound, Link2, ListChecks, Loader2, Plug, Plus, Power, Radio, RefreshCw,
  Server, Settings2, Sparkles, Star, Trash2, X, Zap,
} from 'lucide-react';
import {
  api, bindDevice, closeDeviceSession, deleteDiarizationModel, deleteDevice, deleteLocalAsrModel,
  deleteVoiceProfile, downloadDiarization, downloadLocalAsrModel, fetchProviderModels, getDiarization,
  getLocalAsr, getVoiceProfiles, listDeviceClaims, listDevices, probeProvider, resetDeviceToken,
  updateDevice,
  type Device, type DeviceClaim, type DiarizationStatus, type LocalAsrBackend, type LocalAsrPayload,
  type ProviderItem, type UserSettings, type VoiceProfile,
} from '../lib/api';
import { useAuth } from '../lib/auth';
import { useToast } from '../lib/toast';
import { cn, copyText, fmtDurationCn, fmtTime } from '../lib/utils';
import { useTaskBoard } from '../lib/system';
import Section, { Switch } from './settings/Section';
import PromptsTab from './settings/PromptsTab';
import McpTab from './settings/McpTab';
import McpServiceCard from './settings/McpServiceCard';
import TasksTab from './settings/TasksTab';
import LogsTab from './settings/LogsTab';

type KindMeta = {
  kind: string;
  label: string;
  short?: string;
  hint: string;
  /** 本地引擎不需要 Key / Base URL，模型从本地目录选 */
  requiresKey?: boolean;
  requiresBaseUrl?: boolean;
  localBackend?: LocalAsrBackend;
  /** 语言模型预设：分组、内置地址、默认模型、示例模型、能否自动拉取 */
  group?: string;
  protocol?: string;
  baseUrl?: string;
  defaultModel?: string;
  models?: string[];
  modelsApi?: boolean;
  keysUrl?: string;
};
type Catalog = { asr: KindMeta[]; llm: KindMeta[] };

/** 列表里显示的短名字，比原始的 kind 友好 */
const KIND_LABELS: Record<string, string> = {
  'local-sherpa-onnx': '本地 sherpa-onnx',
  'local-mlx-qwen3-asr': '本地 MLX Qwen3-ASR',
  'local-faster-whisper': '本地 faster-whisper',
  'local-whisper-cpp': '本地 whisper.cpp',
  'dashscope-paraformer': '百炼 Paraformer',
  'dashscope-qwen-asr': '百炼 Qwen3-ASR',
  'openai-whisper': 'OpenAI 兼容',
  dashscope: '通义千问',
  deepseek: 'DeepSeek',
  zhipu: '智谱 GLM',
  moonshot: 'Kimi',
  siliconflow: '硅基流动',
  ark: '豆包',
  qianfan: '文心一言',
  hunyuan: '混元',
  spark: '星火',
  minimax: 'MiniMax',
  stepfun: '阶跃',
  openai: 'OpenAI',
  anthropic: 'Claude',
  openrouter: 'OpenRouter',
  'openai-compat': 'OpenAI 兼容',
  mock: '演示引擎',
};

/** 903471 → 903-471，只影响显示，提交前会去掉分隔符；兼容老的 8 位十六进制 */
const fmtCode = (c: string) => {
  if (c.length === 6) return `${c.slice(0, 3)}-${c.slice(3)}`;
  if (c.length === 8) return `${c.slice(0, 4)}-${c.slice(4)}`;
  return c;
};

/** 口述/语音输入常见写法：中文数字、念错的字母都折成数字 */
const CN_DIGITS: Record<string, string> = {
  零: '0', 〇: '0', 洞: '0', 一: '1', 幺: '1', 二: '2', 两: '2',
  三: '3', 四: '4', 五: '5', 六: '6', 七: '7', 八: '8', 九: '9',
};
const LOOKALIKE: Record<string, string> = { O: '0', I: '1', L: '1', Z: '2', S: '5', B: '8' };

const cleanCodeInput = (raw: string) => {
  const base = raw
    .split('')
    .map((ch) => CN_DIGITS[ch] ?? ch)
    .join('')
    .toUpperCase()
    .replace(/[^0-9A-Z]/g, '');
  // 老固件的 8 位十六进制编号（含 A/C/D/E/F）原样保留，方便升级后重新绑定
  if (/[ACDEF]/.test(base)) return base.slice(0, 8);
  return base
    .split('')
    .map((ch) => LOOKALIKE[ch] ?? ch)
    .join('')
    .slice(0, 6);
};

const PRESETS: Record<string, { baseUrl: string; model: string }> = {
  'local-sherpa-onnx': { baseUrl: '', model: 'sensevoice-small' },
  'local-mlx-qwen3-asr': { baseUrl: '', model: 'qwen3-asr-1.7b-6bit' },
  'local-faster-whisper': { baseUrl: '', model: 'small' },
  'local-whisper-cpp': { baseUrl: '', model: 'small' },
  'dashscope-paraformer': { baseUrl: 'https://dashscope.aliyuncs.com', model: 'paraformer-realtime-v2' },
  'dashscope-qwen-asr': { baseUrl: 'https://dashscope.aliyuncs.com', model: 'qwen3-asr-flash' },
  'openai-whisper': { baseUrl: 'https://api.openai.com/v1', model: 'whisper-1' },
  'openai-compat': { baseUrl: 'https://api.deepseek.com/v1', model: 'deepseek-chat' },
  anthropic: { baseUrl: 'https://api.anthropic.com', model: 'claude-sonnet-4-20250514' },
  mock: { baseUrl: '', model: 'mock' },
};

/** 预设来自后端的 catalog（LLM 各家服务商），本地/老类型仍查上面的表 */
const presetOf = (meta?: KindMeta) => {
  if (!meta) return { baseUrl: '', model: '' };
  if (meta.group) return { baseUrl: meta.baseUrl ?? '', model: meta.defaultModel ?? '' };
  const p = PRESETS[meta.kind] ?? { baseUrl: '', model: '' };
  return { baseUrl: p.baseUrl, model: p.model };
};

const LLM_GROUPS = ['国内', '海外', '通用'];

type FormState = {
  id?: string;
  category: 'asr' | 'llm';
  kind: string;
  name: string;
  baseUrl: string;
  apiKey: string;
  model: string;
  params: Record<string, any>;
  isDefault: boolean;
  hasApiKey: boolean;
};

/** 本地开源模型：依赖状态 + 模型清单 + 一键下载（带进度） */
function LocalModelPicker({
  backend,
  value,
  onPick,
}: {
  backend: LocalAsrBackend;
  value: string;
  onPick: (id: string) => void;
}) {
  const toast = useToast();
  const [data, setData] = useState<LocalAsrPayload | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const autoPicked = useRef(false);

  const load = useCallback(async () => {
    try {
      const next = await getLocalAsr(backend);
      setData(next);
      // 打开表单时默认选中已经下好的那个，省得用户自己找
      if (!autoPicked.current && next.models.length) {
        autoPicked.current = true;
        const installed = next.models.filter((m) => m.installed);
        const cur = next.models.find((m) => m.id === value);
        if (!cur?.installed && installed.length) onPick(installed[0].id);
      }
    } catch {
      /* 拉不到就保持上一份，不打扰用户 */
    }
  }, [backend, value, onPick]);

  useEffect(() => {
    autoPicked.current = false;
  }, [backend]);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 1500);
    return () => clearInterval(t);
  }, [load]);

  const download = async (id: string) => {
    setBusy(id);
    try {
      await downloadLocalAsrModel(backend, id);
      toast.push('已开始下载，完成后自动可用', 'success');
      await load();
    } catch (e: any) {
      toast.push(e?.message ?? '下载失败', 'error');
    } finally {
      setBusy(null);
    }
  };

  const remove = async (id: string) => {
    if (!confirm(`删除本地模型 ${id}？删除后需要重新下载。`)) return;
    try {
      await deleteLocalAsrModel(backend, id);
      toast.push('已删除', 'success');
      await load();
    } catch (e: any) {
      toast.push(e?.message ?? '删除失败', 'error');
    }
  };

  const info = data?.backends?.[backend];
  const models = data?.models ?? [];

  return (
    <div className="space-y-2">
      {info && !info.available && (
        <div className="rounded-xl border border-amber-500/40 bg-amber-500/5 px-3 py-2.5">
          <div className="text-[12px] text-amber-600 dark:text-amber-400 font-medium">引擎还没装好</div>
          <div className="text-[11px] text-muted mt-1 leading-relaxed">{info.detail}</div>
          <code className="block mt-1.5 text-[11px] font-mono text-ink/80 break-all">{info.install}</code>
        </div>
      )}
      {info?.available && (
        <div className="text-[11px] text-emerald-600 dark:text-emerald-400">{info.detail}</div>
      )}

      <div className="space-y-1.5">
        {models.map((m) => {
          const dl = data?.downloads.find((d) => d.modelId === m.id && d.status === 'downloading');
          const pct = dl && dl.total > 0 ? Math.min(100, Math.round((dl.received / dl.total) * 100)) : null;
          const selected = value === m.id;
          return (
            <div
              key={m.id}
              className={cn(
                'rounded-xl border px-3 py-2.5 transition-all',
                selected ? 'border-brand bg-soft/60' : 'border-line hover:border-brand/30',
              )}
            >
              <div className="flex items-center gap-2">
                <button onClick={() => onPick(m.id)} className="flex-1 min-w-0 text-left">
                  <div className="flex items-center gap-1.5">
                    <span className="text-[13px] font-medium">{m.label}</span>
                    <span className="text-[10.5px] text-muted font-mono">≈{m.sizeMB}MB</span>
                    {m.installed && <Check size={12} className="text-emerald-500 shrink-0" />}
                  </div>
                  <div className="text-[11px] text-muted mt-0.5 leading-relaxed">{m.hint}</div>
                </button>
                {m.installed ? (
                  <button
                    onClick={() => void remove(m.id)}
                    className="text-muted hover:text-red-500 px-1 shrink-0"
                    title="删除本地模型"
                  >
                    <Trash2 size={12} />
                  </button>
                ) : dl ? (
                  <span className="text-[11px] font-mono text-brand shrink-0 w-10 text-right">
                    {pct === null ? '…' : `${pct}%`}
                  </span>
                ) : (
                  <button
                    onClick={() => void download(m.id)}
                    disabled={busy === m.id || !info?.available}
                    className="btn-ghost h-7 px-2 text-[11px] shrink-0 disabled:opacity-40"
                  >
                    {busy === m.id ? <Loader2 size={11} className="animate-spin" /> : '下载'}
                  </button>
                )}
              </div>
              {dl && (
                <div className="mt-2 h-1 rounded-full bg-line overflow-hidden">
                  <div
                    className="h-full bg-brand transition-all"
                    style={{ width: `${pct ?? 8}%` }}
                  />
                </div>
              )}
              {dl?.status === 'downloading' && dl.message && (
                <div className="text-[10.5px] text-muted mt-1">{dl.message}</div>
              )}
            </div>
          );
        })}
      </div>

      {data?.modelsDir && (
        <div className="text-[10.5px] text-muted font-mono break-all">模型目录：{data.modelsDir}</div>
      )}
      {(() => {
        const cur = models.find((m) => m.id === value);
        if (!cur || cur.installed) return null;
        return (
          <div className="text-[11px] text-amber-600 dark:text-amber-400">
            所选模型还没下载，下载完成后才能开始转写
          </div>
        );
      })()}
    </div>
  );
}

/** 说话人分离模型：约 45MB，下载后本地推理，音频不出本机 */
function DiarizationModel() {
  const toast = useToast();
  const [data, setData] = useState<DiarizationStatus | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setData(await getDiarization());
    } catch {
      /* 拉不到就保持上一份 */
    }
  }, []);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 1500);
    return () => clearInterval(t);
  }, [load]);

  const download = async () => {
    setBusy(true);
    try {
      await downloadDiarization();
      toast.push('已开始下载，完成后自动可用', 'success');
      await load();
    } catch (e: any) {
      toast.push(e?.message ?? '下载失败', 'error');
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!confirm('删除说话人分离模型？删除后需要重新下载。')) return;
    try {
      await deleteDiarizationModel();
      toast.push('已删除', 'success');
      await load();
    } catch (e: any) {
      toast.push(e?.message ?? '删除失败', 'error');
    }
  };

  const dl = data?.download;
  const downloading = dl?.status === 'downloading';
  const pct = dl && dl.total > 0 ? Math.min(100, Math.round((dl.received / dl.total) * 100)) : null;

  return (
    <div className="rounded-xl border border-line px-3 py-2.5">
      <div className="flex items-center gap-2">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5">
            <span className="text-[12.5px] font-medium">说话人分离模型</span>
            <span className="text-[10.5px] text-muted font-mono">≈45MB</span>
            {data?.installed && <Check size={12} className="text-emerald-500 shrink-0" />}
          </div>
          <div className="text-[11px] text-muted mt-0.5 leading-relaxed">
            pyannote 分割 + 3D-Speaker 声纹嵌入，CPU 本地推理，音频不出本机
          </div>
        </div>
        {data?.installed ? (
          <button onClick={() => void remove()} className="text-muted hover:text-red-500 px-1 shrink-0" title="删除模型">
            <Trash2 size={12} />
          </button>
        ) : downloading ? (
          <span className="text-[11px] font-mono text-brand shrink-0 w-10 text-right">
            {pct === null ? '…' : `${pct}%`}
          </span>
        ) : (
          <button onClick={() => void download()} disabled={busy} className="btn-ghost h-7 px-2 text-[11px] shrink-0 disabled:opacity-40">
            {busy ? <Loader2 size={11} className="animate-spin" /> : '下载'}
          </button>
        )}
      </div>
      {downloading && (
        <div className="mt-2 h-1 rounded-full bg-line overflow-hidden">
          <div className="h-full bg-brand transition-all" style={{ width: `${pct ?? 8}%` }} />
        </div>
      )}
      {downloading && dl?.message && <div className="text-[10.5px] text-muted mt-1">{dl.message}</div>}
      {dl?.status === 'error' && dl.message && (
        <div className="text-[10.5px] text-red-500 mt-1 break-all">{dl.message}</div>
      )}
    </div>
  );
}

/** 已录入的声纹角色；录入入口在录音详情页，这里只做查看和删除 */
function VoiceProfiles() {
  const toast = useToast();
  const [profiles, setProfiles] = useState<VoiceProfile[]>([]);

  const load = useCallback(async () => {
    try {
      setProfiles((await getVoiceProfiles()).profiles);
    } catch {
      /* 拉不到就保持上一份 */
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const remove = async (p: VoiceProfile) => {
    if (!confirm(`删除角色「${p.name}」？删除后，已标注为这个名字的录音会恢复成「说话人 N」。`)) return;
    try {
      const r = await deleteVoiceProfile(p.id);
      setProfiles(r.profiles);
      toast.push('已删除角色', 'success');
    } catch (e: any) {
      toast.push(e?.message ?? '删除失败', 'error');
    }
  };

  if (!profiles.length) {
    return (
      <div className="rounded-xl border border-dashed border-line px-3 py-2.5 text-[11px] text-muted leading-relaxed">
        还没有声纹角色。打开一条已分离的录音，在「全文」上方点「这是我」或「命名…」即可录入；
        录入后，之后的对话会自动显示名字，并判断你是否参与。
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-line px-3 py-2.5">
      <div className="text-[12.5px] font-medium mb-1.5">声纹角色</div>
      <div className="space-y-1.5">
        {profiles.map((p) => (
          <div key={p.id} className="flex items-center gap-2">
            <span className="text-[12px] flex-1 min-w-0 truncate">
              {p.name}
              {p.isSelf && <span className="chip bg-brand/10 text-brand !px-1.5 !py-0 ml-1.5">我</span>}
            </span>
            <span className="text-[10.5px] text-muted/70 shrink-0">{p.sampleCount} 条样本</span>
            <button
              onClick={() => void remove(p)}
              className="text-muted hover:text-red-500 px-1 shrink-0"
              title="删除角色"
            >
              <Trash2 size={12} />
            </button>
          </div>
        ))}
      </div>
      <div className="text-[10.5px] text-muted mt-2 leading-relaxed">
        删除「我」后，录音不再判定你是否参与；重新在任意录音里点「这是我」即可恢复。
      </div>
    </div>
  );
}

function ProviderForm({
  initial,
  catalog,
  onClose,
  onSaved,
}: {
  initial: FormState;
  catalog: Catalog;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [f, setF] = useState<FormState>(initial);
  const [saving, setSaving] = useState(false);
  const [showKey, setShowKey] = useState(false);
  const kinds = f.category === 'asr' ? catalog.asr : catalog.llm;
  const [models, setModels] = useState<string[]>(
    () => (initial.category === 'llm' ? catalog.llm.find((k) => k.kind === initial.kind)?.models ?? [] : []),
  );
  const [fetching, setFetching] = useState(false);
  const [probing, setProbing] = useState(false);
  const [probe, setProbe] = useState<{ ok: boolean; message: string; latencyMs: number } | null>(null);

  const pickKind = (kind: string) => {
    const m = kinds.find((k) => k.kind === kind);
    const p = presetOf(m);
    setModels(m?.models ?? []);
    setProbe(null);
    setF((prev) => ({
      ...prev,
      kind,
      baseUrl: p.baseUrl,
      model: p.model,
      name: prev.id ? prev.name : m?.label ?? kind,
    }));
  };

  const meta = kinds.find((k) => k.kind === f.kind);
  const localBackend = meta?.localBackend;
  const isLocal = !!localBackend;
  const needsKey = meta?.requiresKey !== false;
  const needsBaseUrl = meta?.requiresBaseUrl !== false;
  const isCpp = localBackend === 'whisper-cpp';

  const probeBody = () => ({
    providerId: f.id,
    category: f.category,
    kind: f.kind,
    name: f.name,
    baseUrl: needsBaseUrl ? f.baseUrl : '',
    apiKey: f.apiKey,
    model: f.model,
    params: isLocal ? { ...f.params, model: f.model } : f.params,
  });

  const doProbe = async () => {
    setProbing(true);
    setProbe(null);
    try {
      const r = await probeProvider(probeBody());
      setProbe(r);
      toast.push(r.ok ? `连接正常（${r.latencyMs}ms）` : r.message, r.ok ? 'success' : 'error');
    } catch (e: any) {
      setProbe({ ok: false, message: e?.message ?? '测试失败', latencyMs: 0 });
    } finally {
      setProbing(false);
    }
  };

  const doFetchModels = async () => {
    setFetching(true);
    try {
      const r = await fetchProviderModels(probeBody());
      setModels(r.models);
      toast.push(`拉到 ${r.count} 个模型`, 'success');
      if (!f.model.trim() && r.models.length) setF((prev) => ({ ...prev, model: r.models[0] }));
    } catch (e: any) {
      toast.push(e?.message ?? '拉取失败', 'error');
    } finally {
      setFetching(false);
    }
  };

  const save = async () => {
    if (!f.name.trim()) return toast.push('请填写名称', 'error');
    if (needsKey && !f.id && !f.apiKey.trim()) return toast.push('请填写 API Key', 'error');
    if (isLocal && !f.model.trim()) return toast.push('请选择一个已下载的模型', 'error');
    setSaving(true);
    try {
      const body: any = {
        category: f.category,
        kind: f.kind,
        name: f.name.trim(),
        baseUrl: needsBaseUrl ? f.baseUrl.trim() || null : null,
        model: f.model.trim() || null,
        isDefault: f.isDefault,
      };
      if (f.category === 'asr' && isLocal) {
        body.params = { ...(f.params ?? {}), model: f.model.trim() };
      }
      if (f.apiKey.trim()) body.apiKey = f.apiKey.trim();
      if (f.id) {
        await api(`/api/providers/${f.id}`, { method: 'PATCH', body });
      } else {
        await api('/api/providers', { method: 'POST', body });
      }
      toast.push('已保存', 'success');
      onSaved();
      onClose();
    } catch (e: any) {
      toast.push(e?.message ?? '保存失败', 'error');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center">
      <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" onClick={onClose} />
      <div className="relative w-full sm:max-w-lg bg-panel rounded-t-3xl sm:rounded-2xl border border-line shadow-lift max-h-[92vh] overflow-y-auto animate-fade-up">
        <div className="sticky top-0 bg-panel/95 backdrop-blur px-5 py-4 border-b border-line flex items-center justify-between">
          <div className="text-[15px] font-semibold">
            {f.id ? '编辑服务商' : f.category === 'asr' ? '添加语音识别服务' : '添加语言模型服务'}
          </div>
          <button onClick={onClose} className="h-8 w-8 grid place-items-center rounded-lg text-muted hover:bg-panel2">
            <X size={16} />
          </button>
        </div>

        <div className="p-5 space-y-4">
          <div>
            <label className="label">{f.category === 'llm' ? '服务商' : '接口类型'}</label>
            {f.category === 'llm' ? (
              <div className="space-y-2.5">
                {LLM_GROUPS.map((group) => {
                  const list = kinds.filter((k) => (k.group ?? '通用') === group);
                  if (!list.length) return null;
                  return (
                    <div key={group}>
                      <div className="text-[10.5px] text-muted mb-1.5">{group}</div>
                      <div className="grid grid-cols-2 gap-1.5">
                        {list.map((k) => (
                          <button
                            key={k.kind}
                            onClick={() => pickKind(k.kind)}
                            className={cn(
                              'text-left rounded-lg border px-2.5 py-2 transition-all',
                              f.kind === k.kind
                                ? 'border-brand bg-soft/60'
                                : 'border-line hover:border-brand/30 hover:bg-panel2',
                            )}
                          >
                            <div className="flex items-center justify-between gap-1">
                              <span className={cn('text-[12px] truncate', f.kind === k.kind ? 'font-medium' : '')}>
                                {k.label}
                              </span>
                              {f.kind === k.kind && <Check size={12} className="text-brand shrink-0" />}
                            </div>
                          </button>
                        ))}
                      </div>
                    </div>
                  );
                })}
                {meta?.hint && (
                  <div className="text-[11px] text-muted leading-relaxed px-0.5">{meta.hint}</div>
                )}
              </div>
            ) : (
              <div className="space-y-1.5">
                {kinds.map((k) => (
                  <button
                    key={k.kind}
                    onClick={() => pickKind(k.kind)}
                    className={cn(
                      'w-full text-left rounded-xl border px-3 py-2.5 transition-all',
                      f.kind === k.kind
                        ? 'border-brand bg-soft/60'
                        : 'border-line hover:border-brand/30 hover:bg-panel2',
                    )}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-[13px] font-medium">{k.label}</span>
                      {f.kind === k.kind && <Check size={14} className="text-brand shrink-0" />}
                    </div>
                    <div className="text-[11px] text-muted mt-0.5 leading-relaxed">{k.hint}</div>
                  </button>
                ))}
              </div>
            )}
          </div>

          <div>
            <label className="label">显示名称</label>
            <input className="field" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="例如：百炼主力" />
          </div>

          {isLocal && localBackend ? (
            <>
              <div>
                <label className="label">
                  本地模型
                  <span className="ml-2 text-muted font-normal">音频不上传，识别在本机完成</span>
                </label>
                <LocalModelPicker
                  key={localBackend}
                  backend={localBackend}
                  value={f.model}
                  onPick={(id) => setF({ ...f, model: id, params: { ...f.params, model: id } })}
                />
              </div>
              {isCpp && (
                <div>
                  <label className="label">
                    whisper-cli 路径
                    <span className="ml-2 text-muted font-normal">留空自动查找</span>
                  </label>
                  <input
                    className="field font-mono text-[12.5px]"
                    value={f.params?.binaryPath ?? ''}
                    onChange={(e) =>
                      setF({ ...f, params: { ...f.params, binaryPath: e.target.value } })
                    }
                    placeholder="/opt/homebrew/bin/whisper-cli"
                  />
                </div>
              )}
            </>
          ) : f.kind !== 'mock' ? (
            <>
              {needsBaseUrl && (
                <div>
                  <label className="label">
                    Base URL
                    {meta?.group && meta?.baseUrl && (
                      <span className="ml-2 text-muted font-normal">已按服务商预填，可改</span>
                    )}
                  </label>
                  <input
                    className="field font-mono text-[12.5px]"
                    value={f.baseUrl}
                    onChange={(e) => setF({ ...f, baseUrl: e.target.value })}
                    placeholder="https://api.deepseek.com"
                  />
                </div>
              )}
              <div>
                <label className="label">
                  API Key
                  {f.hasApiKey && <span className="ml-2 text-emerald-500 font-normal">已配置，留空则不修改</span>}
                  {meta?.keysUrl && (
                    <a
                      href={meta.keysUrl}
                      target="_blank"
                      rel="noreferrer"
                      className="ml-2 text-brand font-normal inline-flex items-center gap-0.5"
                    >
                      去获取 <Link2 size={10} />
                    </a>
                  )}
                </label>
                <div className="relative">
                  <input
                    className="field pr-10 font-mono text-[12.5px]"
                    type={showKey ? 'text' : 'password'}
                    value={f.apiKey}
                    onChange={(e) => setF({ ...f, apiKey: e.target.value })}
                    placeholder={
                      f.hasApiKey ? '••••••••（不修改请留空）' : needsKey ? 'sk-...' : '可选，本机服务一般留空'
                    }
                  />
                  <button
                    onClick={() => setShowKey((v) => !v)}
                    className="absolute right-2.5 top-1/2 -translate-y-1/2 text-muted hover:text-ink"
                  >
                    {showKey ? <EyeOff size={15} /> : <Eye size={15} />}
                  </button>
                </div>
              </div>
              <div>
                <div className="flex items-center justify-between mb-1">
                  <label className="label !mb-0">模型</label>
                  {meta?.modelsApi !== false && (
                    <button
                      onClick={doFetchModels}
                      disabled={fetching}
                      className="text-[11px] text-brand inline-flex items-center gap-1 disabled:opacity-50"
                    >
                      {fetching ? <Loader2 size={11} className="animate-spin" /> : <RefreshCw size={11} />}
                      自动获取
                    </button>
                  )}
                </div>
                <input
                  className="field font-mono text-[12.5px]"
                  value={f.model}
                  onChange={(e) => setF({ ...f, model: e.target.value })}
                  placeholder="填好 Key 后点「自动获取」，或手填模型名"
                />
                {models.length > 0 && (
                  <div className="mt-1.5 max-h-24 overflow-y-auto flex flex-wrap gap-1">
                    {models.map((m) => (
                      <button
                        key={m}
                        onClick={() => setF({ ...f, model: m })}
                        className={cn(
                          'text-[10.5px] px-1.5 py-0.5 rounded border font-mono transition-colors',
                          f.model === m
                            ? 'border-brand text-brand bg-soft/60'
                            : 'border-line text-muted hover:text-ink hover:border-brand/30',
                        )}
                      >
                        {m}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </>
          ) : null}

          {f.kind !== 'mock' && (
            <div className="flex items-start gap-2">
              <button
                onClick={doProbe}
                disabled={probing}
                className="btn-outline h-9 px-3 text-[12px] shrink-0 inline-flex items-center gap-1.5"
              >
                {probing ? <Loader2 size={13} className="animate-spin" /> : <Zap size={13} />}
                测试连接
              </button>
              {probe ? (
                <div
                  className={cn(
                    'text-[11px] leading-relaxed pt-1.5 flex items-start gap-1',
                    probe.ok ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-500',
                  )}
                >
                  {probe.ok ? <CheckCircle2 size={11} className="mt-0.5 shrink-0" /> : <AlertCircle size={11} className="mt-0.5 shrink-0" />}
                  <span>
                    {probe.message}
                    {probe.latencyMs ? `（${probe.latencyMs}ms）` : ''}
                  </span>
                </div>
              ) : (
                <div className="text-[11px] text-muted pt-2">不用先保存，填完直接测</div>
              )}
            </div>
          )}

          <label className="flex items-center gap-2.5 cursor-pointer select-none pt-1">
            <input
              type="checkbox"
              checked={f.isDefault}
              onChange={(e) => setF({ ...f, isDefault: e.target.checked })}
              className="h-4 w-4 rounded accent-[var(--brand)]"
            />
            <span className="text-[13px]">设为该类别的默认服务</span>
          </label>
        </div>

        <div className="sticky bottom-0 bg-panel/95 backdrop-blur px-5 py-3.5 border-t border-line flex gap-2">
          <button onClick={onClose} className="btn-outline flex-1 h-10">取消</button>
          <button onClick={save} disabled={saving} className="btn-primary flex-1 h-10">
            {saving ? <Loader2 size={15} className="animate-spin" /> : '保存'}
          </button>
        </div>
      </div>
    </div>
  );
}

function ProviderRow({
  p,
  onEdit,
  onChanged,
}: {
  p: ProviderItem;
  onEdit: () => void;
  onChanged: () => void;
}) {
  const toast = useToast();
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; message: string; latencyMs: number } | null>(null);

  const test = async () => {
    setTesting(true);
    setResult(null);
    try {
      const r = await api<any>(`/api/providers/${p.id}/test`, { method: 'POST' });
      setResult(r);
      toast.push(r.ok ? `连接正常（${r.latencyMs}ms）` : r.message, r.ok ? 'success' : 'error');
    } catch (e: any) {
      toast.push(e?.message ?? '测试失败', 'error');
    } finally {
      setTesting(false);
    }
  };

  const setDefault = async () => {
    await api(`/api/providers/${p.id}`, { method: 'PATCH', body: { isDefault: true } });
    onChanged();
  };

  const remove = async () => {
    if (!confirm(`删除服务商「${p.name}」？`)) return;
    await api(`/api/providers/${p.id}`, { method: 'DELETE' });
    onChanged();
  };

  return (
    <div className="rounded-xl border border-line p-3.5 bg-panel">
      <div className="flex items-start gap-3">
        <div className="h-8 w-8 shrink-0 rounded-lg bg-panel2 grid place-items-center">
          {p.category === 'asr' ? <AudioLines size={14} className="text-muted" /> : <BrainCircuit size={14} className="text-muted" />}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-[13.5px] font-medium truncate">{p.name}</span>
            {p.isDefault && (
              <span className="chip bg-soft text-brand !py-0 !px-1.5">
                <Star size={9} /> 默认
              </span>
            )}
            {!p.enabled && <span className="chip bg-panel2 text-muted !py-0 !px-1.5">已停用</span>}
          </div>
          <div className="text-[11px] text-muted mt-1 font-mono truncate">
            {p.kind === 'mock' ? '不调用外部服务' : KIND_LABELS[p.kind] ?? p.kind}
            {p.model && p.kind !== 'mock' ? ` · ${p.model}` : ''}
            {p.hasApiKey ? ` · ${p.apiKeyMasked}` : ''}
          </div>

          {result && (
            <div className={cn('mt-2 text-[11px] flex items-start gap-1', result.ok ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-500')}>
              {result.ok ? <CheckCircle2 size={11} className="mt-0.5" /> : <AlertCircle size={11} className="mt-0.5" />}
              <span className="leading-relaxed">{result.message}</span>
            </div>
          )}
        </div>
      </div>

      <div className="mt-2.5 flex items-center gap-1.5 pl-11">
        <button onClick={test} disabled={testing} className="text-[11.5px] h-7 px-2 rounded-md text-muted hover:text-brand hover:bg-panel2 transition-colors inline-flex items-center gap-1">
          {testing ? <Loader2 size={11} className="animate-spin" /> : <Zap size={11} />}
          测试
        </button>
        <button onClick={onEdit} className="text-[11.5px] h-7 px-2 rounded-md text-muted hover:text-ink hover:bg-panel2 transition-colors">
          编辑
        </button>
        {!p.isDefault && (
          <button onClick={setDefault} className="text-[11.5px] h-7 px-2 rounded-md text-muted hover:text-brand hover:bg-panel2 transition-colors">
            设为默认
          </button>
        )}
        <button onClick={remove} className="text-[11.5px] h-7 px-2 rounded-md text-muted hover:text-red-500 hover:bg-red-500/10 transition-colors ml-auto">
          <Trash2 size={11} />
        </button>
      </div>
    </div>
  );
}

/* 卡片外壳见 ./settings/Section.tsx（任务 / 日志 / 提示词几个标签共用） */

/* ---------------- 标签页 ---------------- */

type TabKey = 'providers' | 'mcp' | 'prefs' | 'prompts' | 'tasks' | 'logs' | 'data' | 'account';

const TABS: { key: TabKey; label: string; icon: any; hint: string }[] = [
  { key: 'providers', label: 'AI 服务', icon: BrainCircuit, hint: '语音识别与语言模型' },
  { key: 'mcp', label: 'MCP 工具', icon: Plug, hint: '接入外部 MCP 服务，对话可用、总结可推送' },
  { key: 'prefs', label: '录音与纪要', icon: Settings2, hint: '切片、风格、语言、主题' },
  { key: 'prompts', label: '提示词', icon: Sparkles, hint: '日报 / 周报 / 月报 / 年报分别附加' },
  { key: 'tasks', label: '任务', icon: ListChecks, hint: '正在跑什么、排队多少' },
  { key: 'logs', label: '日志', icon: FileText, hint: '后端实时输出与异常' },
  { key: 'data', label: '数据与设备', icon: Cpu, hint: '开放 API、MCP 服务、硬件设备' },
  { key: 'account', label: '账号', icon: Power, hint: '密码与登录状态' },
];

const TAB_KEYS = new Set(TABS.map((t) => t.key));
const normalizeTab = (raw: string | null): TabKey =>
  raw && TAB_KEYS.has(raw as TabKey) ? (raw as TabKey) : 'providers';

export default function SettingsPage() {
  const { user, settings, patchSettings, logout } = useAuth();
  const toast = useToast();
  const [providers, setProviders] = useState<ProviderItem[]>([]);
  const [catalog, setCatalog] = useState<Catalog>({ asr: [], llm: [] });
  const [form, setForm] = useState<FormState | null>(null);
  const [keys, setKeys] = useState<any[]>([]);
  const [newKey, setNewKey] = useState<string | null>(null);
  const [devices, setDevices] = useState<Device[]>([]);
  const [claims, setClaims] = useState<DeviceClaim[]>([]);
  const [bindOpen, setBindOpen] = useState(false);
  const [bindCode, setBindCode] = useState('');
  const [bindName, setBindName] = useState('');
  const [binding, setBinding] = useState(false);
  const [pwd, setPwd] = useState('');
  const [pwd2, setPwd2] = useState('');
  const [savingPwd, setSavingPwd] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const [params, setParams] = useSearchParams();
  const tab = normalizeTab(params.get('tab'));
  const setTab = (next: TabKey) => {
    const p = new URLSearchParams(params);
    p.set('tab', next);
    setParams(p, { replace: true });
  };

  // 运行中的任务：设置在顶部常驻一个提示，任务标签里看细节
  const { board, error: taskError, loading: taskLoading, refresh: refreshTasks } = useTaskBoard(5000);

  const loadDevices = useCallback(async () => {
    try {
      const [d, c] = await Promise.all([listDevices(), listDeviceClaims()]);
      setDevices(d);
      setClaims(c);
    } catch {
      /* 忽略：设备列表不是关键路径 */
    }
  }, []);

  const load = useCallback(async () => {
    try {
      const [p, c, k] = await Promise.all([
        api<ProviderItem[]>('/api/providers'),
        api<Catalog>('/api/providers/catalog'),
        api<any[]>('/api/api-keys'),
      ]);
      setProviders(p);
      setCatalog(c);
      setKeys(k);
      void loadDevices();
    } catch (e: any) {
      toast.push(e?.message ?? '加载失败', 'error');
    } finally {
      setLoaded(true);
    }
  }, [toast, loadDevices]);

  useEffect(() => { void load(); }, [load]);

  // 设备状态会自己变（静默超时自动收尾），停留在设置页时轻量轮询一下
  useEffect(() => {
    const t = setInterval(() => void loadDevices(), 10_000);
    return () => clearInterval(t);
  }, [loadDevices]);

  const asr = useMemo(() => providers.filter((p) => p.category === 'asr'), [providers]);
  const llm = useMemo(() => providers.filter((p) => p.category === 'llm'), [providers]);

  const openNew = (category: 'asr' | 'llm') => {
    const kinds = category === 'asr' ? catalog.asr : catalog.llm;
    const first = kinds.find((k) => k.kind !== 'mock') ?? kinds[0];
    const preset = presetOf(first);
    setForm({
      category,
      kind: first?.kind ?? 'mock',
      name: first?.label ?? '',
      baseUrl: preset.baseUrl,
      model: preset.model,
      params: {},
      apiKey: '',
      isDefault: false,
      hasApiKey: false,
    });
  };

  const openEdit = (p: ProviderItem) => {
    setForm({
      id: p.id,
      category: p.category,
      kind: p.kind,
      name: p.name,
      baseUrl: p.baseUrl ?? '',
      model: p.model ?? '',
      params: p.params ?? {},
      apiKey: '',
      isDefault: p.isDefault,
      hasApiKey: p.hasApiKey,
    });
  };

  const createKey = async () => {
    try {
      const r = await api<any>('/api/api-keys', { method: 'POST', body: { name: '我的设备' } });
      setNewKey(r.key);
      await load();
    } catch (e: any) {
      toast.push(e?.message ?? '创建失败', 'error');
    }
  };

  const saveDeviceNum = async (d: Device, key: 'gapSec' | 'idleSec', raw: string) => {
    const v = Math.round(Number(raw));
    if (!Number.isFinite(v) || v === d[key]) return;
    try {
      const next = await updateDevice(d.id, { [key]: v } as any);
      setDevices((prev) => prev.map((x) => (x.id === next.id ? next : x)));
      toast.push('已保存', 'success');
    } catch (e: any) {
      toast.push(e?.message ?? '保存失败', 'error');
    }
  };

  const endDeviceSession = async (d: Device) => {
    try {
      const r = await closeDeviceSession(d.id);
      toast.push(r.closed ? '已收尾，正在生成纪要' : '当前没有进行中的会话', 'success');
      await loadDevices();
    } catch (e: any) {
      toast.push(e?.message ?? '操作失败', 'error');
    }
  };

  const removeDevice = async (d: Device) => {
    if (!window.confirm(`删除设备「${d.name}」？进行中的会话会先收尾。`)) return;
    try {
      await deleteDevice(d.id);
      await loadDevices();
    } catch (e: any) {
      toast.push(e?.message ?? '删除失败', 'error');
    }
  };

  const doBind = async (rawCode: string, name?: string) => {
    const code = rawCode.trim();
    if (code.length < 6) {
      toast.push('请输入设备播报的 6 位编号', 'error');
      return;
    }
    setBinding(true);
    try {
      const d = await bindDevice(code, name);
      toast.push(`已绑定「${d.name}」，设备会在几秒内拿到 token`, 'success');
      setBindOpen(false);
      setBindCode('');
      setBindName('');
      await loadDevices();
    } catch (e: any) {
      toast.push(e?.message ?? '绑定失败', 'error');
    } finally {
      setBinding(false);
    }
  };

  const resetToken = async (d: Device) => {
    if (!window.confirm(`给「${d.name}」换发新 token？旧 token 立即失效，设备下次轮询会自动取回新的。`)) return;
    try {
      await resetDeviceToken(d.id);
      toast.push('已换发，等待设备重新拉取', 'success');
      await loadDevices();
    } catch (e: any) {
      toast.push(e?.message ?? '操作失败', 'error');
    }
  };

  const deleteKey = async (id: string) => {
    if (!confirm('删除该密钥后，使用它的设备将无法上传录音。')) return;
    await api(`/api/api-keys/${id}`, { method: 'DELETE' });
    await load();
  };

  const changePwd = async () => {
    if (pwd.length < 6) return toast.push('新密码至少 6 位', 'error');
    if (!pwd2) return toast.push('请再输入一遍新密码', 'error');
    if (pwd !== pwd2) return toast.push('两次输入的密码不一致', 'error');
    setSavingPwd(true);
    try {
      await api('/api/auth/me', { method: 'PATCH', body: { password: pwd } });
      toast.push('密码已修改', 'success');
      setPwd('');
      setPwd2('');
    } catch (e: any) {
      toast.push(e?.message ?? '修改失败', 'error');
    } finally {
      setSavingPwd(false);
    }
  };

  const patch = (p: Partial<UserSettings>) => void patchSettings(p);

  const origin = window.location.origin;

  return (
    <div className="max-w-4xl mx-auto px-4 md:px-8 py-6 md:py-10">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-[24px] md:text-[28px] font-semibold tracking-tight">设置</h1>
          <p className="text-[12.5px] text-muted mt-1">配置你自己的 AI 服务，数据全部保存在本机</p>
        </div>
        {!!board?.counts.running && (
          <button
            onClick={() => setTab('tasks')}
            className="mt-1 shrink-0 inline-flex items-center gap-1.5 h-8 px-2.5 rounded-full bg-brand/10 text-brand text-[11.5px] hover:bg-brand/15"
          >
            <Loader2 size={12} className="animate-spin" />
            {board.counts.running} 个任务在跑
          </button>
        )}
        {!board?.counts.running && !!board?.counts.queued && (
          <button
            onClick={() => setTab('tasks')}
            className="mt-1 shrink-0 inline-flex items-center gap-1.5 h-8 px-2.5 rounded-full bg-amber-500/10 text-amber-600 dark:text-amber-400 text-[11.5px] hover:bg-amber-500/15"
          >
            {board.counts.queued} 个任务排队
          </button>
        )}
      </div>

      {/* 标签栏：手机上横向滚动，桌面上平铺 */}
      <div className="mt-4 -mx-4 px-4 md:mx-0 md:px-0 overflow-x-auto">
        <div className="flex items-center gap-1 min-w-max md:min-w-0 md:flex-wrap">
          {TABS.map((t) => {
            const Icon = t.icon;
            const active = tab === t.key;
            return (
              <button
                key={t.key}
                onClick={() => setTab(t.key)}
                title={t.hint}
                className={cn(
                  'h-9 px-3 rounded-xl text-[12.5px] font-medium inline-flex items-center gap-1.5 transition-colors shrink-0',
                  active
                    ? 'bg-soft text-brand'
                    : 'text-muted hover:text-ink hover:bg-panel2',
                )}
              >
                <Icon size={13} />
                {t.label}
                {t.key === 'tasks' && !!board?.counts.running && (
                  <span className="ml-0.5 min-w-[16px] h-4 px-1 rounded-full bg-brand text-white text-[10px] grid place-items-center tabular-nums">
                    {board.counts.running}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      </div>

      <div className="mt-4 space-y-4">
      {tab === 'providers' && (
        <>
      {/* ASR */}
      <Section
        title="语音识别服务"
        desc="负责把录音转成文字。云端 API 或本地开源模型都可以，指定一个默认。"
        icon={AudioLines}
        action={
          <button onClick={() => openNew('asr')} className="btn-ghost h-8 px-2.5 text-[12px] shrink-0 whitespace-nowrap">
            <Plus size={13} /> 添加
          </button>
        }
      >
        <div className="space-y-2">
          {!loaded && <div className="text-[12px] text-muted py-4 text-center">加载中…</div>}
          {asr.map((p) => (
            <ProviderRow key={p.id} p={p} onEdit={() => openEdit(p)} onChanged={load} />
          ))}
          {loaded && asr.length === 0 && (
            <div className="text-[12px] text-muted text-center py-6">还没有配置，当前使用演示引擎</div>
          )}
        </div>
      </Section>

      {/* LLM */}
      <Section
        title="语言模型服务"
        desc="负责生成纪要、思维导图。支持任意 OpenAI 兼容接口。"
        icon={BrainCircuit}
        action={
          <button onClick={() => openNew('llm')} className="btn-ghost h-8 px-2.5 text-[12px] shrink-0 whitespace-nowrap">
            <Plus size={13} /> 添加
          </button>
        }
      >
        <div className="space-y-2">
          {llm.map((p) => (
            <ProviderRow key={p.id} p={p} onEdit={() => openEdit(p)} onChanged={load} />
          ))}
          {loaded && llm.length === 0 && (
            <div className="text-[12px] text-muted text-center py-6">还没有配置，当前使用演示引擎</div>
          )}
        </div>
      </Section>
        </>
      )}

      {tab === 'mcp' && <McpTab />}

      {tab === 'prefs' && (
      <>{/* 录音偏好 */}
      <Section title="录音与纪要" desc="影响录音切片方式与自动整理行为" icon={Settings2}>
        <div className="space-y-4">
          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-[13px]">切片时长</div>
              <div className="text-[11px] text-muted mt-0.5">越短转写越快出字，越长请求越少</div>
            </div>
            <select
              value={settings?.chunkSeconds ?? 120}
              onChange={(e) => patch({ chunkSeconds: Number(e.target.value) })}
              className="field w-auto h-9 pr-8 text-[12.5px]"
            >
              {[15, 30, 45, 60, 90, 120].map((s) => (
                <option key={s} value={s}>{s} 秒</option>
              ))}
            </select>
          </div>

          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-[13px]">自动生成纪要</div>
              <div className="text-[11px] text-muted mt-0.5">录音转写完成后自动调用语言模型整理</div>
            </div>
            <Switch
              checked={settings?.autoSummarize ?? true}
              onChange={(next) => patch({ autoSummarize: next })}
            />
          </div>

          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-[13px]">说话人分离</div>
              <div className="text-[11px] text-muted mt-0.5">
                本地识别多人对话，转写里标出「说话人 1/2/…」；音频不出本机
              </div>
            </div>
            <Switch
              checked={settings?.speakerDiarization ?? false}
              onChange={(next) => patch({ speakerDiarization: next })}
            />
          </div>
          {settings?.speakerDiarization && <DiarizationModel />}
          {settings?.speakerDiarization && <VoiceProfiles />}

          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-[13px]">纪要风格</div>
              <div className="text-[11px] text-muted mt-0.5">决定纪要的章节结构与侧重点</div>
            </div>
            <select
              value={settings?.summaryTemplate ?? 'meeting'}
              onChange={(e) => patch({ summaryTemplate: e.target.value as any })}
              className="field w-auto h-9 pr-8 text-[12.5px]"
            >
              <option value="meeting">会议纪要</option>
              <option value="lecture">课程笔记</option>
              <option value="interview">访谈记录</option>
              <option value="idea">灵感碎片</option>
              <option value="general">通用</option>
            </select>
          </div>

          <div className="rounded-xl border border-line bg-panel2/50 px-3 py-2.5 flex items-start justify-between gap-3">
            <div>
              <div className="text-[12.5px]">自定义提示词</div>
              <div className="text-[11px] text-muted mt-0.5 leading-relaxed">
                单条录音、日报、周报、月报、年报的附加要求已经拆到「提示词」标签里分别设置
              </div>
            </div>
            <button onClick={() => setTab('prompts')} className="btn-outline h-8 px-2.5 text-[12px] shrink-0">
              去设置
            </button>
          </div>

          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-[13px]">识别语言</div>
              <div className="text-[11px] text-muted mt-0.5">中文场景建议保持中文或自动</div>
            </div>
            <select
              value={settings?.language ?? 'zh'}
              onChange={(e) => patch({ language: e.target.value })}
              className="field w-auto h-9 pr-8 text-[12.5px]"
            >
              <option value="zh">中文</option>
              <option value="en">英文</option>
              <option value="auto">自动检测</option>
            </select>
          </div>

          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-[13px]">界面主题</div>
              <div className="text-[11px] text-muted mt-0.5">深色模式适合夜间查看</div>
            </div>
            <div className="flex rounded-lg bg-panel2 p-0.5">
              {[
                { k: 'light', l: '浅色' },
                { k: 'dark', l: '深色' },
                { k: 'system', l: '自动' },
              ].map((t) => (
                <button
                  key={t.k}
                  onClick={() => patch({ theme: t.k as any })}
                  className={cn(
                    'px-2.5 h-7 rounded-md text-[11.5px] transition-all',
                    (settings?.theme ?? 'system') === t.k ? 'bg-panel text-ink shadow-sm' : 'text-muted',
                  )}
                >
                  {t.l}
                </button>
              ))}
            </div>
          </div>
        </div>
      </Section>
      </>
      )}

      {tab === 'prompts' && <PromptsTab />}

      {tab === 'tasks' && (
        <TasksTab board={board} loading={taskLoading} error={taskError} onRefresh={refreshTasks} />
      )}

      {tab === 'logs' && <LogsTab />}

      {tab === 'data' && (
        <>
      {/* 开放 API */}
      <Section
        title="开放 API"
        desc="让脚本、录音笔或其他设备直接把音频推过来"
        icon={Server}
        action={
          <button onClick={createKey} className="btn-ghost h-8 px-2.5 text-[12px] shrink-0 whitespace-nowrap">
            <KeyRound size={13} /> 新建密钥
          </button>
        }
      >
        <div className="space-y-2">
          {keys.map((k) => (
            <div key={k.id} className="flex items-center gap-3 rounded-xl border border-line px-3 py-2.5">
              <KeyRound size={14} className="text-muted shrink-0" />
              <div className="flex-1 min-w-0">
                <div className="text-[12.5px] font-medium truncate">{k.name}</div>
                <div className="text-[11px] text-muted font-mono truncate">{k.key}</div>
              </div>
              <button onClick={() => deleteKey(k.id)} className="p-1.5 rounded-md text-muted hover:text-red-500 hover:bg-red-500/10">
                <Trash2 size={13} />
              </button>
            </div>
          ))}
          {keys.length === 0 && (
            <div className="text-[12px] text-muted text-center py-5">还没有创建密钥</div>
          )}
        </div>

        <div className="mt-3 rounded-xl bg-panel2 p-3">
          <div className="text-[11px] text-muted mb-1.5">示例：推送一段音频并直接拿到文字</div>
          <pre className="text-[10.5px] leading-relaxed overflow-x-auto text-ink/80 font-mono whitespace-pre">
{`curl -X POST "${origin}/api/open/transcribe?wait=1&title=会议录音" \\
  -H "X-API-Key: <你的密钥>" \\
  -H "Content-Type: audio/mpeg" \\
  --data-binary @audio.mp3`}
          </pre>
          <button
            onClick={() => {
              void copyText(`curl -X POST "${origin}/api/open/transcribe?wait=1&title=会议录音" -H "X-API-Key: <你的密钥>" -H "Content-Type: audio/mpeg" --data-binary @audio.mp3`);
              toast.push('已复制', 'success');
            }}
            className="mt-2 inline-flex items-center gap-1 text-[11px] text-brand hover:underline"
          >
            <Copy size={11} /> 复制命令
          </button>
        </div>
      </Section>

      {/* 对外提供 MCP 服务 */}
      <McpServiceCard />

      {/* 硬件设备 */}
      <Section
        title="硬件设备"
        desc="设备开机会播报 6 位编号，在这里输入即可绑定"
        icon={Cpu}
        action={
          <div className="flex items-center gap-1.5 shrink-0">
            <button
              onClick={() => {
                setBindCode('');
                setBindName('');
                setBindOpen(true);
              }}
              className="btn-primary h-8 px-2.5 text-[12px] whitespace-nowrap"
            >
              <Plus size={13} /> 添加设备
            </button>
            <button
              onClick={() => void loadDevices()}
              title="刷新"
              className="btn-ghost h-8 w-8 !px-0 justify-center"
            >
              <RefreshCw size={13} />
            </button>
          </div>
        }
      >
        {/* 正在轮询、还没被认领的设备 */}
        {claims.length > 0 && (
          <div className="mb-2 rounded-xl border border-brand/25 bg-brand/5 px-3 py-2.5">
            <div className="flex items-center gap-1.5 text-[11.5px] font-medium text-brand">
              <Radio size={12} />
              {claims.length} 台设备正在等待配对
            </div>
            <div className="mt-2 space-y-1.5">
              {claims.map((c) => (
                <div key={c.code} className="flex items-center gap-2 text-[11.5px]">
                  <span className="font-mono tracking-wider text-ink">{fmtCode(c.code)}</span>
                  <span className="text-muted truncate flex-1">
                    {c.name || '未命名设备'}
                    {c.firmware ? ` · ${c.firmware}` : ''}
                  </span>
                  <button
                    onClick={() => void doBind(c.code, c.name || undefined)}
                    disabled={binding}
                    className="text-brand hover:underline shrink-0 disabled:opacity-50"
                  >
                    绑定
                  </button>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="space-y-2">
          {devices.map((d) => {
            const live = !!d.openRecordingId;
            return (
              <div key={d.id} className="rounded-xl border border-line px-3 py-2.5">
                <div className="flex items-center gap-2">
                  <span
                    className={cn(
                      'h-2 w-2 rounded-full shrink-0',
                      live ? 'bg-emerald-500 animate-pulse' : 'bg-line',
                    )}
                  />
                  <div className="text-[12.5px] font-medium truncate flex-1">{d.name}</div>
                  {live ? (
                    <span className="chip bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 !px-1.5 !py-0 shrink-0">
                      录音中
                    </span>
                  ) : (
                    <span className="chip bg-panel2 text-muted !px-1.5 !py-0 shrink-0">空闲</span>
                  )}
                  {live && (
                    <button
                      onClick={() => void endDeviceSession(d)}
                      className="text-[11px] text-brand hover:underline shrink-0"
                    >
                      收尾
                    </button>
                  )}
                  {d.paired && (
                    <button
                      onClick={() => void resetToken(d)}
                      title="换发 token"
                      className="p-1 rounded-md text-muted hover:text-brand hover:bg-brand/10 shrink-0"
                    >
                      <KeyRound size={12} />
                    </button>
                  )}
                  <button
                    onClick={() => void removeDevice(d)}
                    className="p-1 rounded-md text-muted hover:text-red-500 hover:bg-red-500/10 shrink-0"
                  >
                    <Trash2 size={12} />
                  </button>
                </div>

                <div className="mt-1.5 flex items-center gap-1.5 text-[11px] text-muted flex-wrap">
                  <span className="font-mono tracking-wider text-ink/70">
                    {d.code ? fmtCode(d.code) : '未配对'}
                  </span>
                  <span className="opacity-40">·</span>
                  <span className="font-mono" title="设备持有的 token（掩码）">
                    {d.tokenMasked || '未下发 token'}
                  </span>
                  <span className="opacity-40">·</span>
                  {live ? (
                    <>
                      <span>{d.openChunks} 片</span>
                      <span className="opacity-40">·</span>
                      <span>{fmtDurationCn(d.openDurationMs)}</span>
                      <span className="opacity-40">·</span>
                    </>
                  ) : null}
                  <span>{d.lastSeenAt ? `最后活跃 ${fmtTime(d.lastSeenAt)}` : '还没推过音频'}</span>
                </div>

                <div className="mt-2 flex items-center gap-x-4 gap-y-1.5 text-[11px] text-muted flex-wrap">
                  <label className="flex items-center gap-1">
                    间隔
                    <input
                      type="number"
                      defaultValue={d.gapSec}
                      onBlur={(e) => void saveDeviceNum(d, 'gapSec', e.target.value)}
                      className="w-12 h-6 px-1 rounded-md bg-panel2 border border-line text-center tabular-nums text-ink"
                    />
                    秒算新会话
                  </label>
                  <label className="flex items-center gap-1">
                    静默
                    <input
                      type="number"
                      defaultValue={d.idleSec}
                      onBlur={(e) => void saveDeviceNum(d, 'idleSec', e.target.value)}
                      className="w-12 h-6 px-1 rounded-md bg-panel2 border border-line text-center tabular-nums text-ink"
                    />
                    秒自动收尾
                  </label>
                </div>
              </div>
            );
          })}
          {devices.length === 0 && claims.length === 0 && (
            <div className="text-[12px] text-muted text-center py-5">还没有设备，点右上角「添加设备」</div>
          )}
        </div>

        <div className="mt-3 rounded-xl bg-panel2 p-3">
          <div className="text-[11px] text-muted mb-1.5">设备端流程：播报编号 → 轮询等待 → 拿到 token → 推音频</div>
          <pre className="text-[10.5px] leading-relaxed overflow-x-auto text-ink/80 font-mono whitespace-pre-wrap break-words">
{`GET ${origin}/api/open/pair?code=903471
  → { "status": "pending" }            后台还没绑定
  → { "status": "bound", "token": "dv_..." }   绑定后自动下发

POST ${origin}/api/open/ingest
  X-Device-Token: dv_...
  Content-Type: audio/l16
  body: 音频二进制`}
          </pre>
          <p className="mt-2 text-[11px] leading-relaxed text-muted">
            编号是 6 位数字（如 <span className="font-mono">903471</span>），由设备用 MAC 换算，
            开机会播报或显示；念「9 0 3 4 7 1」或直接输 <span className="font-mono">903-471</span> 都能绑定。
            后台绑定后设备几秒内取回 token，之后推音频只带 token，不再需要账号级密钥。
          </p>
        </div>
      </Section>
        </>
      )}

      {tab === 'account' && (
        <>
      {/* 账号 */}
      <Section title="账号" desc={`当前登录：${user?.username}`} icon={Power}>
        <div className="space-y-3">
          <div>
            <label className="label">修改密码</label>
            <div className="space-y-2">
              <input
                className="field w-full"
                type="password"
                value={pwd}
                onChange={(e) => setPwd(e.target.value)}
                placeholder="新密码（至少 6 位）"
                autoComplete="new-password"
              />
              <input
                className="field w-full"
                type="password"
                value={pwd2}
                onChange={(e) => setPwd2(e.target.value)}
                placeholder="再输入一遍新密码"
                autoComplete="new-password"
              />
              {!!pwd2 && pwd !== pwd2 && (
                <div className="flex items-center gap-1 text-[11px] text-red-500">
                  <AlertCircle size={11} className="shrink-0" /> 两次输入的密码不一致
                </div>
              )}
              <button
                onClick={changePwd}
                disabled={savingPwd || !pwd || !pwd2 || pwd !== pwd2}
                className="btn-outline h-9 px-3 text-[12.5px]"
              >
                {savingPwd ? <Loader2 size={14} className="animate-spin" /> : '保存新密码'}
              </button>
            </div>
          </div>
          <button onClick={logout} className="btn-danger h-9 px-3 text-[12.5px]">
            退出登录
          </button>
        </div>
      </Section>
        </>
      )}
      </div>

      <div className="text-center text-[11px] text-muted/70 pt-2 pb-4">
        忆录 · 本地部署的录音记忆库
      </div>

      {form && (
        <ProviderForm
          initial={form}
          catalog={catalog}
          onClose={() => setForm(null)}
          onSaved={load}
        />
      )}

      {bindOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" onClick={() => setBindOpen(false)} />
          <div className="relative w-full max-w-md bg-panel rounded-2xl border border-line p-5 shadow-lift animate-fade-up">
            <div className="flex items-center justify-between">
              <div className="text-[15px] font-semibold">添加设备</div>
              <button
                onClick={() => setBindOpen(false)}
                className="p-1 rounded-md text-muted hover:bg-panel2"
              >
                <X size={15} />
              </button>
            </div>
            <p className="text-[12px] text-muted mt-1.5">
              输入设备开机后播报（或显示）的 6 位编号，绑定后会自动把 token 下发给它。
            </p>

            <div className="mt-4 space-y-3">
              <div>
                <label className="label">设备编号</label>
                <input
                  className="field w-full font-mono tracking-[0.2em] text-center text-[15px]"
                  value={bindCode}
                  onChange={(e) => setBindCode(cleanCodeInput(e.target.value))}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && bindCode.length >= 6) void doBind(bindCode, bindName || undefined);
                  }}
                  placeholder="903471"
                  inputMode="numeric"
                  maxLength={8}
                  autoFocus
                />
                {bindCode.length >= 6 && (
                  <div className="mt-1 text-[11px] text-muted">
                    将绑定为 <span className="font-mono tracking-wider">{fmtCode(bindCode)}</span>
                  </div>
                )}
              </div>
              <div>
                <label className="label">名称（可选）</label>
                <input
                  className="field w-full"
                  value={bindName}
                  onChange={(e) => setBindName(e.target.value)}
                  placeholder="客厅录音盒"
                  maxLength={40}
                />
              </div>
            </div>

            <div className="mt-4 flex gap-2">
              <button
                onClick={() => void doBind(bindCode, bindName || undefined)}
                disabled={binding || bindCode.length < 6}
                className="btn-primary flex-1 h-9 text-[13px] disabled:opacity-50"
              >
                {binding ? <Loader2 size={14} className="animate-spin" /> : <Link2 size={13} />} 绑定
              </button>
              <button onClick={() => setBindOpen(false)} className="btn-outline flex-1 h-9 text-[13px]">
                取消
              </button>
            </div>
          </div>
        </div>
      )}

      {newKey && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" onClick={() => setNewKey(null)} />
          <div className="relative w-full max-w-md bg-panel rounded-2xl border border-line p-5 shadow-lift animate-fade-up">
            <div className="text-[15px] font-semibold">密钥已创建</div>
            <p className="text-[12px] text-muted mt-1.5">请立即复制保存，之后只会显示掩码。</p>
            <div className="mt-3 rounded-xl bg-panel2 px-3 py-2.5 font-mono text-[12px] break-all">
              {newKey}
            </div>
            <div className="mt-4 flex gap-2">
              <button
                onClick={() => { void copyText(newKey); toast.push('已复制', 'success'); }}
                className="btn-primary flex-1 h-9 text-[13px]"
              >
                <Copy size={13} /> 复制
              </button>
              <button onClick={() => setNewKey(null)} className="btn-outline flex-1 h-9 text-[13px]">关闭</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
