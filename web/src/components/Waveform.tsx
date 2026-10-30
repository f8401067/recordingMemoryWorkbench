import React, { useEffect, useRef } from 'react';

/** canvas 不支持 CSS 变量，需要先解析成实际颜色值 */
function resolveColor(input: string): string {
  if (!input.startsWith('var(')) return input;
  const name = input.slice(4, -1).split(',')[0].trim();
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || '#5b5bd6';
}

export default function Waveform({
  stream,
  active,
  color = 'var(--brand)',
  bars = 44,
}: {
  stream: MediaStream | null;
  active: boolean;
  color?: string;
  bars?: number;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const levels = useRef<number[]>(new Array(bars).fill(0));

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx2d = canvas.getContext('2d');
    if (!ctx2d) return;
    const paint = resolveColor(color);

    let raf = 0;
    let audioCtx: AudioContext | null = null;
    let analyser: AnalyserNode | null = null;
    let data: Uint8Array | null = null;

    if (stream && active) {
      try {
        audioCtx = new AudioContext();
        const src = audioCtx.createMediaStreamSource(stream);
        analyser = audioCtx.createAnalyser();
        analyser.fftSize = 512;
        analyser.smoothingTimeConstant = 0.75;
        src.connect(analyser);
        data = new Uint8Array(analyser.frequencyBinCount);
      } catch {
        /* 浏览器不支持时退化为静态波形 */
      }
    }

    const resize = () => {
      const dpr = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      canvas.width = rect.width * dpr;
      canvas.height = rect.height * dpr;
      ctx2d.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(canvas);

    const draw = () => {
      try {
        const rect = canvas.getBoundingClientRect();
        const w = rect.width;
        const h = rect.height;
        ctx2d.clearRect(0, 0, w, h);

        const gap = Math.max(3, w / bars / 2.6);
        const bw = Math.max(2, (w - gap * (bars - 1)) / bars);
        const totalW = bw * bars + gap * (bars - 1);
        let x = (w - totalW) / 2;

        if (analyser && data) analyser.getByteFrequencyData(data as any);

        for (let i = 0; i < bars; i++) {
          let target = 0.1;
          if (analyser && data && active) {
            // 取低频段主体，并按指数压缩动态范围，让小音量也有明显起伏
            const idx = Math.max(1, Math.floor((i / bars) * (data.length * 0.5)));
            const a = data[idx] / 255;
            const b = data[Math.min(data.length - 1, idx + 1)] / 255;
            const energy = Math.pow(Math.max(a, b * 0.85), 0.55);
            target = 0.1 + Math.min(0.9, energy * 0.95);
          } else if (!active) {
            target = 0.1 + Math.sin(i * 0.42) * 0.035 + Math.cos(i * 0.17) * 0.02;
          }
          levels.current[i] += (target - levels.current[i]) * 0.3;
          const bh = Math.max(bw, levels.current[i] * h * 0.94);
          const y = (h - bh) / 2;
          ctx2d.globalAlpha = active ? 0.85 : 0.35;
          ctx2d.fillStyle = paint;
          const r = Math.min(bw / 2, 2);
          ctx2d.beginPath();
          ctx2d.roundRect(x, y, bw, bh, r);
          ctx2d.fill();
          x += bw + gap;
        }
        ctx2d.globalAlpha = 1;
      } catch {
        return;
      }
      if (active) raf = requestAnimationFrame(draw);
    };
    draw();

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      if (audioCtx && audioCtx.state !== 'closed') void audioCtx.close();
    };
  }, [stream, active, color, bars]);

  return <canvas ref={canvasRef} className="w-full h-full block" />;
}
