import Database from 'better-sqlite3';
import fs from 'node:fs';
import path from 'node:path';
import { config } from '../config.js';

const dbPath = path.join(config.dataDir, 'recall.db');
export const db = new Database(dbPath);
db.pragma('journal_mode = WAL');

const schema = fs.readFileSync(path.join(import.meta.dirname, 'schema.sql'), 'utf8');
db.exec(schema);

/** 轻量类型化查询助手 */
export function all<T = any>(sql: string, params: any[] = []): T[] {
  return db.prepare(sql).all(...params) as T[];
}
export function get<T = any>(sql: string, params: any[] = []): T | undefined {
  return db.prepare(sql).get(...params) as T | undefined;
}
export function run(sql: string, params: any[] = []) {
  return db.prepare(sql).run(...params);
}

export type User = {
  id: string;
  username: string;
  password_hash: string;
  display_name: string | null;
  created_at: string;
};

export type Provider = {
  id: string;
  user_id: string;
  category: 'asr' | 'llm';
  kind: string;
  name: string;
  base_url: string | null;
  api_key: string | null;
  model: string | null;
  params: string | null;
  is_default: number;
  enabled: number;
  created_at: string;
};

export type Recording = {
  id: string;
  user_id: string;
  title: string;
  status: string;
  source: string;
  language: string;
  started_at: string;
  ended_at: string | null;
  duration_ms: number;
  asr_provider_id: string | null;
  summary_status: string;
  memo: string | null;
  created_at: string;
};

export type Chunk = {
  id: string;
  recording_id: string;
  seq: number;
  filename: string;
  path: string;
  mime: string | null;
  size: number;
  duration_ms: number;
  start_offset_ms: number;
  status: string;
  transcript: string | null;
  segments: string | null;
  error: string | null;
  retries: number;
  created_at: string;
  updated_at: string;
};

export type Summary = {
  id: string;
  user_id: string;
  recording_id: string | null;
  scope: 'recording' | 'day' | 'range';
  date_from: string;
  date_to: string;
  title: string;
  markdown: string | null;
  mindmap: string | null;
  provider_id: string | null;
  model: string | null;
  status: string;
  error: string | null;
  stats: string | null;
  created_at: string;
  updated_at: string;
};

export type Job = {
  id: string;
  type: string;
  payload: string;
  status: string;
  attempts: number;
  max_attempts: number;
  run_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  created_at: string;
};
