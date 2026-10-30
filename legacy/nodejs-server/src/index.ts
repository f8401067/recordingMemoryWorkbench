import Fastify from 'fastify';
import cors from '@fastify/cors';
import jwt from '@fastify/jwt';
import multipart from '@fastify/multipart';
import fastifyStatic from '@fastify/static';
import fs from 'node:fs';
import { config } from './config.js';
import { get, run } from './db/index.js';
import { authRoutes, createUser } from './routes/auth.js';
import { recordingRoutes } from './routes/recordings.js';
import { summaryRoutes } from './routes/summaries.js';
import { providerRoutes, calendarRoutes } from './routes/providers.js';
import { openRoutes } from './routes/open.js';
import { registerHandler, startWorker, queueStats } from './services/jobs.js';
import { handleTranscribeJob } from './services/transcribe.js';
import { handleSummarizeJob } from './services/summarize.js';

declare module 'fastify' {
  interface FastifyInstance {
    authenticate: (req: any, reply: any) => Promise<void>;
  }
}

const app = Fastify({
  logger: { level: process.env.LOG_LEVEL ?? 'info', transport: undefined },
  bodyLimit: 64 * 1024 * 1024,
  disableRequestLogging: true,
});

await app.register(cors, { origin: true, credentials: true });
await app.register(jwt, { secret: config.jwtSecret });
await app.register(multipart, {
  limits: { fileSize: config.maxChunkBytes, files: 1 },
});
app.addContentTypeParser(
  /^audio\//,
  { parseAs: 'buffer' },
  (_req, body, done) => done(null, body),
);
app.addContentTypeParser(
  ['application/octet-stream', 'binary/octet-stream'],
  { parseAs: 'buffer' },
  (_req, body, done) => done(null, body),
);

app.decorate('authenticate', async (req: any, reply: any) => {
  try {
    await req.jwtVerify();
  } catch (e: any) {
    app.log.warn(
      {
        url: req.url,
        method: req.method,
        hasAuth: !!req.headers.authorization,
        authPrefix: String(req.headers.authorization ?? '').slice(0, 12),
        reason: e?.message,
      },
      '鉴权失败',
    );
    reply.code(401).send({ error: '未登录或登录已过期' });
  }
});

app.get('/api/health', async () => ({
  ok: true,
  version: '1.0.0',
  uptime: Math.round(process.uptime()),
  queue: queueStats(),
}));

await app.register(authRoutes);
await app.register(recordingRoutes);
await app.register(summaryRoutes);
await app.register(providerRoutes);
await app.register(calendarRoutes);
await app.register(openRoutes);

/* 静态前端 */
if (fs.existsSync(config.webDist)) {
  await app.register(fastifyStatic, { root: config.webDist, prefix: '/' });
  app.setNotFoundHandler((req, reply) => {
    if (req.url.startsWith('/api/')) return reply.code(404).send({ error: '接口不存在' });
    return reply.sendFile('index.html');
  });
} else {
  app.get('/', async () => ({
    message: '前端尚未构建，请在 web/ 下执行 npm run build，或用 npm run dev 启动开发服务器。',
  }));
}

/* 任务处理器 */
registerHandler('transcribe', (p) => handleTranscribeJob(p), config.asrConcurrency);
registerHandler('summarize', (p) => handleSummarizeJob(p), 2);
startWorker(1200);

/* 首次启动初始化管理员 */
const userCount = (get(`SELECT COUNT(*) AS n FROM users`) as { n: number }).n;
if (userCount === 0) {
  const username = process.env.ADMIN_USER ?? 'admin';
  const password = process.env.ADMIN_PASSWORD ?? 'admin123';
  createUser(username, password, '管理员');
  app.log.info(`已创建初始账号：${username} / ${password}（请尽快修改密码）`);
}

try {
  await app.listen({ port: config.port, host: config.host });
  app.log.info(`忆录服务已启动 → http://localhost:${config.port}`);
} catch (e) {
  app.log.error(e);
  process.exit(1);
}

for (const sig of ['SIGINT', 'SIGTERM'] as const) {
  process.on(sig, async () => {
    app.log.info('正在关闭服务…');
    await app.close();
    run(`PRAGMA wal_checkpoint(TRUNCATE)`);
    process.exit(0);
  });
}
