import React, {
  forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState,
} from 'react';
import { AlertCircle, Download, Loader2, Pause, Play } from 'lucide-react';
import { getToken } from '../lib/api';
import { cn, fmtDuration } from '../lib/utils';

export type AudioInfo = {
  mode: 'source' | 'chunks' | 'none';
  url: string | null;
  mime: string | null;
  size: number;
  filename: string | null;
  durationMs: number;
};

export type AudioChunk = { seq: number; startMs: number; durationMs: number };

export type AudioHandle = {
  seek: (ms: number) => void;
  toggle: () => void;
};

type Props = {
  recordingId: string;
  audio: AudioInfo;
  chunks: AudioChunk[];
  onTime?: (ms: number) => void;
  onPlayingChange?: (playing: boolean) => void;
};

const SPEEDS = [1, 1.25, 1.5, 2];

/** <audio src> 发不了自定义请求头，token 只能走 query（后端 media_user 支持） */
function withToken(url: string) {
  const token = getToken();
  if (!token) return url;
  return `${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}`;
}

const clamp = (v: number, min: number, max: number) => Math.min(max, Math.max(min, v));

/**
 * 录音播放器。
 *
 * - 上传的整段音频：直接播原始文件，交给浏览器原生 seek。
 * - 网页/设备录音：按 startOffsetMs 顺序播放分片，对外仍是同一条时间轴，
 *   上层只关心全局毫秒数——转写高亮、进度条都用它。
 */
const AudioPlayer = forwardRef<AudioHandle, Props>(function AudioPlayer(
  { recordingId, audio, chunks, onTime, onPlayingChange },
  ref,
) {
  const elRef = useRef<HTMLAudioElement | null>(null);
  const idxRef = useRef(0);
  const loadedSeqRef = useRef<number | null>(null);
  const pendingRef = useRef<{ offsetSec: number; autoplay: boolean } | null>(null);
  const onTimeRef = useRef(onTime);
  onTimeRef.current = onTime;

  const [ms, setMs] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [buffering, setBuffering] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [failed, setFailed] = useState(false);

  const list = useMemo(
    () =>
      chunks
        .filter((c) => typeof c.startMs === 'number' && c.durationMs >= 0)
        .slice()
        .sort((a, b) => a.startMs - b.startMs || a.seq - b.seq),
    [chunks],
  );

  const totalMs = useMemo(() => {
    if (audio.durationMs > 0) return audio.durationMs;
    const last = list[list.length - 1];
    return last ? last.startMs + last.durationMs : 0;
  }, [audio.durationMs, list]);

  const setPlayingState = useCallback(
    (next: boolean) => {
      setPlaying(next);
      onPlayingChange?.(next);
    },
    [onPlayingChange],
  );

  const reportTime = useCallback((value: number) => {
    const safe = Math.max(0, value);
    setMs(safe);
    onTimeRef.current?.(safe);
  }, []);

  /** src 换掉后浏览器才知道能不能 seek，等 metadata 到了再落位置 */
  const applyPending = useCallback(() => {
    const el = elRef.current;
    const pending = pendingRef.current;
    if (!el || !pending || el.readyState < 1) return;
    pendingRef.current = null;
    try {
      el.currentTime = pending.offsetSec;
    } catch {
      /* 个别格式不接受 seek */
    }
    if (pending.autoplay) void el.play().catch(() => setPlayingState(false));
  }, [setPlayingState]);

  const loadChunk = useCallback(
    (index: number, offsetSec: number, autoplay: boolean) => {
      const el = elRef.current;
      const target = list[index];
      if (!el || !target) return;
      idxRef.current = index;
      pendingRef.current = { offsetSec, autoplay };
      if (loadedSeqRef.current === target.seq) {
        applyPending();
        return;
      }
      loadedSeqRef.current = target.seq;
      el.src = withToken(`/api/recordings/${recordingId}/audio/${target.seq}`);
      el.load();
      applyPending();
    },
    [applyPending, list, recordingId],
  );

  const seek = useCallback(
    (targetMs: number) => {
      const el = elRef.current;
      if (!el) return;
      const value = clamp(targetMs, 0, Math.max(0, totalMs));
      if (audio.mode === 'source') {
        try {
          el.currentTime = value / 1000;
        } catch {
          /* ignore */
        }
        reportTime(value);
        return;
      }
      if (!list.length) return;
      let index = 0;
      for (let i = 0; i < list.length; i += 1) {
        if (list[i].startMs <= value) index = i;
      }
      const wasPlaying = !el.paused;
      const offset = Math.max(0, (value - list[index].startMs) / 1000);
      loadChunk(index, offset, wasPlaying);
      reportTime(value);
    },
    [audio.mode, list, loadChunk, reportTime, totalMs],
  );

  const toggle = useCallback(() => {
    const el = elRef.current;
    if (!el) return;
    if (el.paused) void el.play().catch(() => setPlayingState(false));
    else el.pause();
  }, [setPlayingState]);

  useImperativeHandle(ref, () => ({ seek, toggle }), [seek, toggle]);

  // 换录音 / 换播放模式时重置（处理中的新分片不打断当前播放）
  useEffect(() => {
    const el = elRef.current;
    if (!el) return;
    setFailed(false);
    reportTime(0);
    if (audio.mode === 'source') {
      loadedSeqRef.current = -1;
      el.src = withToken(audio.url ?? '');
      el.load();
      return;
    }
    if (audio.mode === 'none') {
      loadedSeqRef.current = null;
      el.removeAttribute('src');
      return;
    }
    if (list.length) {
      loadedSeqRef.current = null;
      loadChunk(0, 0, false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [audio.mode, audio.url, recordingId]);

  useEffect(() => {
    const el = elRef.current;
    if (el) el.playbackRate = speed;
  }, [speed]);

  const handleTimeUpdate = () => {
    const el = elRef.current;
    if (!el) return;
    if (pendingRef.current) applyPending();

    if (audio.mode === 'source') {
      reportTime(el.currentTime * 1000);
      return;
    }
    const current = list[idxRef.current];
    if (!current || loadedSeqRef.current !== current.seq || el.readyState < 1) return;
    const offsetMs = el.currentTime * 1000;
    reportTime(current.startMs + offsetMs);
    // 一片快播完就接下一片，留点余量让切换没有明显空档
    if (!el.paused && current.durationMs > 0 && offsetMs >= current.durationMs - 220) {
      const next = idxRef.current + 1;
      if (next < list.length) loadChunk(next, 0, true);
    }
  };

  const handleEnded = () => {
    if (audio.mode === 'chunks') {
      const next = idxRef.current + 1;
      if (next < list.length) {
        loadChunk(next, 0, true);
        return;
      }
    }
    setPlayingState(false);
    reportTime(totalMs);
  };

  const progress = totalMs > 0 ? clamp((ms / totalMs) * 100, 0, 100) : 0;
  const playable = audio.mode !== 'none';

  return (
    <div className="card px-3 py-2.5 md:px-4 md:py-3">
      <audio
        ref={elRef}
        preload="metadata"
        onTimeUpdate={handleTimeUpdate}
        onLoadedMetadata={applyPending}
        onCanPlay={() => {
          applyPending();
          setBuffering(false);
        }}
        onWaiting={() => setBuffering(true)}
        onPlaying={() => {
          setBuffering(false);
          setPlayingState(true);
        }}
        onPlay={() => setPlayingState(true)}
        onPause={() => setPlayingState(false)}
        onEnded={handleEnded}
        onError={() => {
          if (!elRef.current?.getAttribute('src')) return;
          setFailed(true);
          setBuffering(false);
          setPlayingState(false);
        }}
      />

      <div className="flex items-center gap-3">
        <button
          onClick={toggle}
          disabled={!playable || failed}
          aria-label={playing ? '暂停' : '播放'}
          className={cn(
            'h-10 w-10 shrink-0 grid place-items-center rounded-full text-white transition-all active:scale-95',
            'bg-gradient-to-br from-[var(--brand)] to-[var(--brand-2)]',
            'shadow-[0_8px_18px_-8px_var(--brand)] disabled:opacity-40 disabled:shadow-none',
          )}
        >
          {buffering && playing ? (
            <Loader2 size={16} className="animate-spin" />
          ) : playing ? (
            <Pause size={16} fill="currentColor" />
          ) : (
            <Play size={16} fill="currentColor" className="ml-0.5" />
          )}
        </button>

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-[11.5px] tabular-nums text-ink/80 font-medium shrink-0">
              {fmtDuration(ms)}
            </span>
            <input
              type="range"
              min={0}
              max={Math.max(1, Math.round(totalMs))}
              value={Math.round(clamp(ms, 0, Math.max(1, totalMs)))}
              onChange={(e) => seek(Number(e.target.value))}
              disabled={!playable || failed || totalMs <= 0}
              aria-label="播放进度"
              className="wave-seek flex-1 min-w-0 disabled:opacity-50"
              style={{ ['--seek' as any]: `${progress}%` }}
            />
            <span className="text-[11.5px] tabular-nums text-muted shrink-0">
              {fmtDuration(totalMs)}
            </span>
          </div>

          <div className="mt-1 flex items-center gap-2 text-[11px] text-muted min-w-0">
            <span className="truncate">
              {audio.mode === 'source'
                ? `原始音频${audio.size ? ` · ${(audio.size / 1024 / 1024).toFixed(1)} MB` : ''}`
                : audio.mode === 'chunks'
                  ? `分片音频 · ${list.length} 段拼接播放`
                  : '这段录音没有可播放的音频文件'}
            </span>
            {failed && (
              <span className="inline-flex items-center gap-1 text-red-500 shrink-0">
                <AlertCircle size={10} /> 加载失败
              </span>
            )}
          </div>
        </div>

        <div className="flex items-center gap-1 shrink-0">
          <button
            onClick={() => setSpeed((s) => SPEEDS[(SPEEDS.indexOf(s) + 1) % SPEEDS.length])}
            disabled={!playable}
            className="h-8 px-2 rounded-lg text-[11.5px] tabular-nums text-muted hover:text-ink hover:bg-panel2 transition-colors disabled:opacity-40"
            title="调整播放速度"
          >
            {speed}×
          </button>
          {audio.mode === 'source' && audio.url && (
            <a
              href={withToken(`${audio.url}?download=1`)}
              download={audio.filename ?? true}
              className="h-8 w-8 grid place-items-center rounded-lg text-muted hover:text-brand hover:bg-panel2 transition-colors"
              title="下载原始音频"
            >
              <Download size={14} />
            </a>
          )}
        </div>
      </div>
    </div>
  );
});

export default AudioPlayer;
