import 'dotenv/config';
import path from 'node:path';
import fs from 'node:fs';
import crypto from 'node:crypto';

const root = path.resolve(import.meta.dirname, '../..');

export const config = {
  port: Number(process.env.PORT ?? 8787),
  host: process.env.HOST ?? '0.0.0.0',
  dataDir: process.env.DATA_DIR ?? path.join(root, 'data'),
  webDist: path.join(root, 'web', 'dist'),
  jwtSecret: process.env.JWT_SECRET ?? 'recall-dev-secret-change-me',
  /** 分片上传单文件大小上限 */
  maxChunkBytes: 64 * 1024 * 1024,
  /** 单个用户同时执行的转写任务数 */
  asrConcurrency: Number(process.env.ASR_CONCURRENCY ?? 2),
  /** 演示模式：不配置任何服务商时，用 mock 引擎跑通全链路 */
  demoMode: process.env.DEMO_MODE !== 'false',
};

fs.mkdirSync(config.dataDir, { recursive: true });
fs.mkdirSync(path.join(config.dataDir, 'uploads'), { recursive: true });
fs.mkdirSync(path.join(config.dataDir, 'tmp'), { recursive: true });

export const nowIso = () => new Date().toISOString();
export const uid = (prefix = '') =>
  prefix + crypto.randomUUID().replace(/-/g, '').slice(0, 20);
