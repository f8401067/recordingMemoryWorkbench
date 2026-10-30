import type { FastifyInstance } from 'fastify';
import bcrypt from 'bcryptjs';
import { all, get, run, type User } from '../db/index.js';
import { nowIso, uid } from '../config.js';
import { seedDemoProviders } from '../services/providers.js';
import { readSettings } from '../services/settings.js';

export function publicUser(u: User) {
  return { id: u.id, username: u.username, displayName: u.display_name, createdAt: u.created_at };
}

export function createUser(username: string, password: string, displayName?: string): User {
  const id = uid('usr_');
  const hash = bcrypt.hashSync(password, 10);
  run(`INSERT INTO users (id, username, password_hash, display_name, created_at) VALUES (?, ?, ?, ?, ?)`, [
    id,
    username,
    hash,
    displayName ?? username,
    nowIso(),
  ]);
  seedDemoProviders(id);
  return get<User>(`SELECT * FROM users WHERE id = ?`, [id])!;
}

export async function authRoutes(app: FastifyInstance) {
  app.post('/api/auth/register', async (req, reply) => {
    const { username, password, displayName } = (req.body ?? {}) as any;
    if (!username || String(username).length < 2) return reply.code(400).send({ error: '用户名至少 2 个字符' });
    if (!password || String(password).length < 6) return reply.code(400).send({ error: '密码至少 6 位' });
    const exists = get(`SELECT id FROM users WHERE username = ?`, [username]);
    if (exists) return reply.code(409).send({ error: '用户名已存在' });

    const user = createUser(username, password, displayName);
    const token = app.jwt.sign({ sub: user.id, username: user.username }, { expiresIn: '30d' });
    return { token, user: publicUser(user), settings: readSettings(user.id) };
  });

  app.post('/api/auth/login', async (req, reply) => {
    const { username, password } = (req.body ?? {}) as any;
    const user = get<User>(`SELECT * FROM users WHERE username = ?`, [username ?? '']);
    if (!user || !bcrypt.compareSync(String(password ?? ''), user.password_hash)) {
      return reply.code(401).send({ error: '用户名或密码错误' });
    }
    const token = app.jwt.sign({ sub: user.id, username: user.username }, { expiresIn: '30d' });
    return { token, user: publicUser(user), settings: readSettings(user.id) };
  });

  app.get('/api/auth/me', { preHandler: [app.authenticate] }, async (req) => {
    const user = get<User>(`SELECT * FROM users WHERE id = ?`, [(req.user as any).sub]);
    return { user: user ? publicUser(user) : null, settings: user ? readSettings(user.id) : null };
  });

  app.patch('/api/auth/me', { preHandler: [app.authenticate] }, async (req) => {
    const userId = (req.user as any).sub;
    const { displayName, password } = (req.body ?? {}) as any;
    if (displayName) run(`UPDATE users SET display_name = ? WHERE id = ?`, [String(displayName).slice(0, 40), userId]);
    if (password && String(password).length >= 6) {
      run(`UPDATE users SET password_hash = ? WHERE id = ?`, [bcrypt.hashSync(String(password), 10), userId]);
    }
    const user = get<User>(`SELECT * FROM users WHERE id = ?`, [userId])!;
    return { user: publicUser(user) };
  });

  /* ---------------- API Key（开放接口凭证） ---------------- */
  app.get('/api/api-keys', { preHandler: [app.authenticate] }, async (req) => {
    const userId = (req.user as any).sub;
    const rows = all<any>(`SELECT * FROM api_keys WHERE user_id = ? ORDER BY created_at DESC`, [userId]);
    return rows.map((r) => ({
      id: r.id,
      name: r.name,
      key: `${r.key_value.slice(0, 8)}...${r.key_value.slice(-4)}`,
      createdAt: r.created_at,
      lastUsedAt: r.last_used_at,
    }));
  });

  app.post('/api/api-keys', { preHandler: [app.authenticate] }, async (req) => {
    const userId = (req.user as any).sub;
    const { name } = (req.body ?? {}) as any;
    const keyValue = 'rk_' + uid('').padEnd(32, '0');
    const id = uid('key_');
    run(`INSERT INTO api_keys (id, user_id, name, key_value, created_at) VALUES (?, ?, ?, ?, ?)`, [
      id,
      userId,
      String(name ?? '默认密钥').slice(0, 40),
      keyValue,
      nowIso(),
    ]);
    return { id, name, key: keyValue };
  });

  app.delete('/api/api-keys/:id', { preHandler: [app.authenticate] }, async (req) => {
    const userId = (req.user as any).sub;
    const { id } = req.params as any;
    run(`DELETE FROM api_keys WHERE id = ? AND user_id = ?`, [id, userId]);
    return { ok: true };
  });
}
