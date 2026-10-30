import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  AlertTriangle, ArrowLeft, Check, FileUp, Loader2, Mic, Pause, Play, Square, Upload,
} from 'lucide-react';
import Waveform from '../components/Waveform';
import { api, uploadChunk, uploadFile, type RecordingDetail } from '../lib/api';
import { useAuth } from '../lib/auth';
import { useToast } from '../lib/toast';
import { cn, fmtDuration } from '../lib/utils';

type Phase = 'idle' | 'recording' | 'paused' | 'finishing';

function pickMimeType(): string {
  const candidates = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/mp4',
    'audio/ogg;codecs=opus',
  ];
  for (const c of candidates) {
    if (typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported?.(c)) return c;
  }
  return '';
}

export default function RecordPage() {
  const navigate = useNavigate();
  const toast = useToast();
  const { settings } = useAuth();

  const [phase, setPhase] = useState<Phase>('idle');
  const [elapsed, setElapsed] = useState(0);
  const [title, setTitle] = useState('');
  const [stream, setStream] = useState<MediaStream | null>(null);
  const [segments, setSegments] = useState(0);
  const [uploading, setUploading] = useState(0);
  const [preview, setPreview] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [secure, setSecure] = useState(true);
  const [recordedId, setRecordedId] = useState<string | null>(null);

  const phaseRef = useRef<Phase>('idle');
  const recRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const recIdRef = useRef<string | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const seqRef = useRef(0);
  const uploadedMsRef = useRef(0);
  const segStartRef = useRef(0);
  const startAtRef = useRef(0);
  const pausedAccumRef = useRef(0);
  const pausedAtRef = useRef(0);
  const rotateTimerRef = useRef<number | null>(null);
  const tickRef = useRef<number | null>(null);
  const pollRef = useRef<number | null>(null);
  const chainRef = useRef<Promise<void>>(Promise.resolve());
  const mimeRef = useRef('');
  const wakeLockRef = useRef<any>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const chunkSeconds = Math.max(10, Math.min(120, settings?.chunkSeconds ?? 30));

  const setPhaseBoth = (p: Phase) => {
    phaseRef.current = p;
    setPhase(p);
  };

  useEffect(() => {
    setSecure(typeof navigator.mediaDevices?.getUserMedia === 'function' && window.isSecureContext);
    return () => {
      void stopAll();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const stopAll = async () => {
    if (rotateTimerRef.current) window.clearTimeout(rotateTimerRef.current);
    if (tickRef.current) window.clearInterval(tickRef.current);
    if (pollRef.current) window.clearInterval(pollRef.current);
    try {
      if (recRef.current && recRef.current.state !== 'inactive') recRef.current.stop();
    } catch { /* noop */ }
    streamRef.current?.getTracks().forEach((t) => t.stop());
    if (wakeLockRef.current) {
      try { await wakeLockRef.current.release(); } catch { /* noop */ }
      wakeLockRef.current = null;
    }
  };

  const pushUpload = useCallback((blob: Blob, durationMs: number, startOffsetMs: number) => {
    const recId = recIdRef.current;
    if (!recId) return;
    const seq = seqRef.current++;
    setUploading((n) => n + 1);
    chainRef.current = chainRef.current
      .then(async () => {
        await uploadChunk(recId, seq, blob, { durationMs, startOffsetMs });
        setSegments((n) => n + 1);
      })
      .catch((e) => {
        setError(`第 ${seq + 1} 段上传失败：${e?.message ?? '网络异常'}`);
        toast.push(`第 ${seq + 1} 段上传失败，已跳过`, 'error');
      })
      .finally(() => setUploading((n) => Math.max(0, n - 1)));
  }, [toast]);

  const startSegment = useCallback(() => {
    const s = streamRef.current;
    if (!s) return;
    const opts: MediaRecorderOptions = mimeRef.current ? { mimeType: mimeRef.current } : {};
    let rec: MediaRecorder;
    try {
      rec = new MediaRecorder(s, opts);
    } catch {
      rec = new MediaRecorder(s);
    }

    chunksRef.current = [];
    segStartRef.current = Date.now();

    rec.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) chunksRef.current.push(e.data);
    };

    rec.onstop = () => {
      const d = Date.now() - segStartRef.current;
      const parts = chunksRef.current;
      chunksRef.current = [];
      if (parts.length > 0) {
        const blob = new Blob(parts, { type: mimeRef.current || 'audio/webm' });
        const offset = uploadedMsRef.current;
        uploadedMsRef.current += d;
        pushUpload(blob, d, offset);
      }
      if (phaseRef.current === 'recording') startSegment();
    };

    rec.onerror = (e: any) => {
      setError(`录音出错：${e?.error?.message ?? '未知错误'}`);
      setPhaseBoth('idle');
    };

    rec.start();
    recRef.current = rec;

    if (rotateTimerRef.current) window.clearTimeout(rotateTimerRef.current);
    rotateTimerRef.current = window.setTimeout(() => {
      if (phaseRef.current === 'recording' && recRef.current?.state === 'recording') {
        recRef.current.stop();
      }
    }, chunkSeconds * 1000);
  }, [chunkSeconds, pushUpload]);

  const beginPolling = (id: string) => {
    if (pollRef.current) window.clearInterval(pollRef.current);
    pollRef.current = window.setInterval(async () => {
      try {
        const d = await api<RecordingDetail>(`/api/recordings/${id}`);
        const text = d.transcript.text;
        if (text) setPreview(text.slice(-320));
      } catch { /* noop */ }
    }, 3500);
  };

  const start = async () => {
    setError(null);
    try {
      const s = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
        },
      });
      streamRef.current = s;
      setStream(s);
      mimeRef.current = pickMimeType();

      const d = new Date();
      const defaultTitle = `录音 ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
      const finalTitle = title.trim() || defaultTitle;
      setTitle(finalTitle);

      const rec = await api<{ id: string }>('/api/recordings', {
        method: 'POST',
        body: { title: finalTitle, source: 'web', language: settings?.language ?? 'zh', startedAt: new Date().toISOString() },
      });
      recIdRef.current = rec.id;
      setRecordedId(rec.id);
      seqRef.current = 0;
      uploadedMsRef.current = 0;
      setSegments(0);
      setPreview('');

      startAtRef.current = Date.now();
      pausedAccumRef.current = 0;
      setPhaseBoth('recording');
      startSegment();
      beginPolling(rec.id);

      tickRef.current = window.setInterval(() => {
        if (phaseRef.current === 'recording') {
          setElapsed(Date.now() - startAtRef.current - pausedAccumRef.current);
        }
      }, 200);

      if ('wakeLock' in navigator) {
        try {
          wakeLockRef.current = await (navigator as any).wakeLock.request('screen');
        } catch { /* noop */ }
      }
    } catch (e: any) {
      const msg =
        e?.name === 'NotAllowedError'
          ? '麦克风权限被拒绝，请在浏览器设置中允许访问麦克风'
          : e?.name === 'NotFoundError'
            ? '没有检测到可用的麦克风设备'
            : e?.message ?? '无法开始录音';
      setError(msg);
      toast.push(msg, 'error');
      await stopAll();
      setPhaseBoth('idle');
    }
  };

  const pause = () => {
    if (phaseRef.current !== 'recording') return;
    pausedAtRef.current = Date.now();
    setPhaseBoth('paused');
    if (rotateTimerRef.current) window.clearTimeout(rotateTimerRef.current);
    if (recRef.current && recRef.current.state === 'recording') recRef.current.stop();
    else if (recRef.current) recRef.current.stop();
  };

  const resume = () => {
    if (phaseRef.current !== 'paused') return;
    pausedAccumRef.current += Date.now() - pausedAtRef.current;
    setPhaseBoth('recording');
    startSegment();
  };

  const finish = async () => {
    const id = recIdRef.current;
    if (!id) return;
    setPhaseBoth('finishing');
    if (rotateTimerRef.current) window.clearTimeout(rotateTimerRef.current);
    if (tickRef.current) window.clearInterval(tickRef.current);
    if (pollRef.current) window.clearInterval(pollRef.current);

    try {
      if (recRef.current && recRef.current.state !== 'inactive') {
        await new Promise<void>((resolve) => {
          const rec = recRef.current!;
          const prev = rec.onstop;
          rec.onstop = (ev: Event) => {
            try { prev?.call(rec, ev); } catch { /* noop */ }
            // 等 pushUpload 把最后一段任务放进队列
            setTimeout(resolve, 0);
          };
          rec.stop();
        });
      }
    } catch { /* noop */ }

    await chainRef.current.catch(() => {});
    const total = uploadedMsRef.current;
    streamRef.current?.getTracks().forEach((t) => t.stop());
    if (wakeLockRef.current) {
      try { await wakeLockRef.current.release(); } catch { /* noop */ }
      wakeLockRef.current = null;
    }

    try {
      if (seqRef.current === 0) {
        await api(`/api/recordings/${id}`, { method: 'DELETE' });
        toast.push('没有录到内容，已取消', 'info');
        setPhaseBoth('idle');
        setRecordedId(null);
        recIdRef.current = null;
        return;
      }
      await api(`/api/recordings/${id}/finish`, { method: 'POST', body: { durationMs: total } });
      toast.push('录音已保存，正在后台转写', 'success');
      navigate(`/recordings/${id}`, { replace: true });
    } catch (e: any) {
      toast.push(e?.message ?? '结束录音失败', 'error');
      setPhaseBoth('paused');
    }
  };

  const handleFile = async (file: File) => {
    try {
      setPhaseBoth('finishing');
      const rec = await api<{ id: string }>('/api/recordings', {
        method: 'POST',
        body: { title: title.trim() || file.name.replace(/\.[^.]+$/, ''), source: 'upload' },
      });
      toast.push('正在上传并切片…', 'info');
      await uploadFile(rec.id, file, 60);
      toast.push('已上传，正在后台转写', 'success');
      navigate(`/recordings/${rec.id}`, { replace: true });
    } catch (e: any) {
      toast.push(e?.message ?? '上传失败', 'error');
      setPhaseBoth('idle');
    }
  };

  const active = phase === 'recording';
  const running = phase === 'recording' || phase === 'paused';

  return (
    <div className="min-h-[100dvh] flex flex-col bg-bg">
      {/* 顶栏 */}
      <div className="flex items-center gap-3 px-4 md:px-8 h-14 shrink-0 pt-safe">
        <button
          onClick={() => (running ? toast.push('请先结束当前录音', 'info') : navigate(-1))}
          className="h-9 w-9 grid place-items-center rounded-xl hover:bg-panel2 text-muted transition-colors"
        >
          <ArrowLeft size={18} />
        </button>
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="给这段录音起个名字"
          disabled={running}
          className="flex-1 bg-transparent text-[15px] font-medium outline-none placeholder:text-muted/60 disabled:opacity-70"
        />
        {running && (
          <span className="chip bg-red-500/10 text-red-500">
            <span className="h-1.5 w-1.5 rounded-full bg-red-500 animate-pulse" />
            {phase === 'paused' ? '已暂停' : '录音中'}
          </span>
        )}
      </div>

      {/* 主体 */}
      <div className="flex-1 flex flex-col items-center justify-center px-6 pb-8 -mt-6">
        {/* 计时 */}
        <div className="text-center">
          <div
            className={cn(
              'font-semibold tabular-nums tracking-tight transition-colors',
              'text-[56px] md:text-[64px] leading-none',
              running ? 'text-ink' : 'text-muted/45',
            )}
          >
            {fmtDuration(elapsed, true)}
          </div>
          <div className="mt-3 h-5 text-[12.5px] text-muted">
            {phase === 'idle' && '点击下方按钮开始录音'}
            {phase === 'recording' && (
              <span className="inline-flex items-center gap-1.5">
                每 {chunkSeconds} 秒自动切片上传
                {uploading > 0 && <span className="text-brand">· 上传中 {uploading}</span>}
              </span>
            )}
            {phase === 'paused' && '已暂停，点击继续'}
            {phase === 'finishing' && (
              <span className="inline-flex items-center gap-1.5">
                <Loader2 size={12} className="animate-spin" />
                正在保存并提交转写…
              </span>
            )}
          </div>
        </div>

        {/* 波形 */}
        <div className="w-full max-w-lg h-24 md:h-28 my-7">
          <Waveform stream={stream} active={active} color={active ? 'var(--brand)' : 'var(--muted)'} />
        </div>

        {/* 控制区 */}
        <div className="flex items-center gap-5">
          {running && (
            <button
              onClick={phase === 'paused' ? resume : pause}
              className="h-14 w-14 rounded-full border border-line bg-panel grid place-items-center text-ink hover:bg-panel2 transition-all active:scale-95"
            >
              {phase === 'paused' ? <Play size={20} className="ml-0.5" /> : <Pause size={20} />}
            </button>
          )}

          {!running ? (
            <button
              onClick={start}
              disabled={phase === 'finishing' || !secure}
              className={cn(
                'relative h-[86px] w-[86px] rounded-full grid place-items-center transition-all',
                'bg-gradient-to-br from-[var(--brand)] to-[var(--brand-2)] text-white',
                'shadow-[0_16px_40px_-14px_var(--brand)] active:scale-95',
                (!secure || phase === 'finishing') && 'opacity-40 pointer-events-none',
              )}
            >
              {phase === 'finishing' ? <Loader2 size={28} className="animate-spin" /> : <Mic size={30} strokeWidth={2.2} />}
            </button>
          ) : (
            <button
              onClick={finish}
              className="h-[86px] w-[86px] rounded-full grid place-items-center bg-red-500 text-white shadow-[0_16px_40px_-14px_rgba(239,68,68,.7)] active:scale-95 transition-all"
            >
              <Square size={26} fill="currentColor" strokeWidth={0} />
            </button>
          )}
        </div>

        {/* 分段状态 */}
        {running && (
          <div className="mt-6 flex items-center gap-2 text-[11.5px] text-muted">
            <span className="chip bg-panel2">
              <Check size={10} className="text-emerald-500" />
              已上传 {segments} 段
            </span>
            {uploading > 0 && (
              <span className="chip bg-amber-500/10 text-amber-600">
                <Upload size={10} />
                {uploading} 段传输中
              </span>
            )}
          </div>
        )}

        {/* 实时文字 */}
        {running && preview && (
          <div className="mt-7 w-full max-w-lg">
            <div className="text-[11px] text-muted mb-1.5 flex items-center gap-1.5">
              <span className="h-1 w-1 rounded-full bg-emerald-500 animate-pulse" />
              实时转写
            </div>
            <div className="card p-3.5 max-h-32 overflow-y-auto text-[13px] leading-relaxed text-ink/85 whitespace-pre-wrap">
              {preview}
            </div>
          </div>
        )}

        {/* 不支持录音时的降级 */}
        {!secure && (
          <div className="mt-6 w-full max-w-md rounded-xl border border-amber-500/30 bg-amber-500/10 p-3.5 flex gap-2.5">
            <AlertTriangle size={16} className="text-amber-500 shrink-0 mt-0.5" />
            <div className="text-[12px] leading-relaxed text-amber-700 dark:text-amber-300">
              当前页面通过 HTTP 访问，浏览器禁止网页调用麦克风。
              可以改用下方「上传音频文件」，或为服务配置 HTTPS 后再录音。
            </div>
          </div>
        )}

        {error && (
          <div className="mt-5 w-full max-w-md rounded-xl border border-red-500/25 bg-red-500/10 px-3.5 py-2.5 text-[12px] text-red-600 dark:text-red-400">
            {error}
          </div>
        )}

        {/* 上传入口 */}
        {!running && (
          <button
            onClick={() => fileRef.current?.click()}
            className="mt-8 inline-flex items-center gap-2 text-[12.5px] text-muted hover:text-brand transition-colors"
          >
            <FileUp size={14} />
            或上传已有音频文件
          </button>
        )}
        <input
          ref={fileRef}
          type="file"
          accept="audio/*,video/mp4"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) void handleFile(f);
            e.target.value = '';
          }}
        />
      </div>
    </div>
  );
}
