import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import fs from 'node:fs';
import path from 'node:path';
import { config } from '../config.js';

const exec = promisify(execFile);

let ffmpegOk: boolean | null = null;
export async function hasFfmpeg(): Promise<boolean> {
  if (ffmpegOk !== null) return ffmpegOk;
  try {
    await exec('ffmpeg', ['-version']);
    ffmpegOk = true;
  } catch {
    ffmpegOk = false;
  }
  return ffmpegOk;
}

/** 读取音频时长（毫秒），失败返回 0 */
export async function probeDurationMs(file: string): Promise<number> {
  try {
    const { stdout } = await exec('ffprobe', [
      '-v', 'error',
      '-show_entries', 'format=duration',
      '-of', 'default=noprint_wrappers=1:nokey=1',
      file,
    ]);
    const sec = Number(stdout.trim());
    return Number.isFinite(sec) ? Math.round(sec * 1000) : 0;
  } catch {
    return 0;
  }
}

/** 统一转码为 16kHz 单声道 PCM（s16le），供流式 ASR 使用 */
export async function toPcm16k(input: string): Promise<{ pcm: Buffer; durationMs: number }> {
  const out = path.join(config.dataDir, 'tmp', `${path.basename(input)}.${Date.now()}.pcm`);
  await exec('ffmpeg', [
    '-hide_banner', '-loglevel', 'error', '-y',
    '-i', input,
    '-ar', '16000', '-ac', '1', '-f', 's16le',
    out,
  ], { maxBuffer: 1024 * 1024 * 8 });
  const pcm = fs.readFileSync(out);
  fs.unlinkSync(out);
  return { pcm, durationMs: Math.round((pcm.length / 2 / 16000) * 1000) };
}

/** 转码为 16kHz 单声道 WAV，供同步 ASR / base64 上传使用 */
export async function toWav16k(input: string): Promise<{ wav: Buffer; durationMs: number }> {
  const out = path.join(config.dataDir, 'tmp', `${path.basename(input)}.${Date.now()}.wav`);
  await exec('ffmpeg', [
    '-hide_banner', '-loglevel', 'error', '-y',
    '-i', input,
    '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le',
    out,
  ], { maxBuffer: 1024 * 1024 * 8 });
  const wav = fs.readFileSync(out);
  fs.unlinkSync(out);
  return { wav, durationMs: Math.round(((wav.length - 44) / 2 / 16000) * 1000) };
}

/**
 * 把长音频切成若干片段，供分段转写。
 * 先尝试无损切分（-c copy，快），失败则转码为 mp3（兼容性好）。
 */
export async function splitAudio(
  input: string,
  outDir: string,
  segmentSec = 60,
): Promise<{ file: string; durationMs: number }[]> {
  fs.mkdirSync(outDir, { recursive: true });
  const ext = path.extname(input).toLowerCase() || '.bin';
  const pattern = path.join(outDir, `part%04d${ext}`);

  try {
    await exec('ffmpeg', [
      '-hide_banner', '-loglevel', 'error', '-y',
      '-i', input,
      '-f', 'segment',
      '-segment_time', String(segmentSec),
      '-reset_timestamps', '1',
      '-c', 'copy',
      pattern,
    ], { maxBuffer: 1024 * 1024 * 8 });
  } catch {
    /* 回退到转码切分 */
  }

  let files = fs.readdirSync(outDir).filter((f) => f.startsWith('part')).sort();
  if (files.length === 0) {
    for (const f of fs.readdirSync(outDir)) {
      if (f.startsWith('part')) fs.rmSync(path.join(outDir, f), { force: true });
    }
    const mp3pattern = path.join(outDir, 'part%04d.mp3');
    await exec('ffmpeg', [
      '-hide_banner', '-loglevel', 'error', '-y',
      '-i', input,
      '-f', 'segment',
      '-segment_time', String(segmentSec),
      '-reset_timestamps', '1',
      '-c:a', 'libmp3lame', '-b:a', '64k', '-ar', '16000', '-ac', '1',
      mp3pattern,
    ], { maxBuffer: 1024 * 1024 * 8 });
    files = fs.readdirSync(outDir).filter((f) => f.startsWith('part')).sort();
  }

  const out: { file: string; durationMs: number }[] = [];
  for (const f of files) {
    const full = path.join(outDir, f);
    const d = await probeDurationMs(full);
    const size = fs.statSync(full).size;
    if (size < 1024) continue;
    out.push({ file: full, durationMs: d });
  }
  return out;
}
