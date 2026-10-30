import { all, run } from '../db/index.js';

export type UserSettings = {
  /** 分片目标时长（秒），前端录音切片用 */
  chunkSeconds: number;
  /** 录音结束后自动生成纪要 */
  autoSummarize: boolean;
  /** 自动生成纪要使用的 LLM provider id */
  summaryProviderId: string | null;
  /** 纪要模板 */
  summaryTemplate: 'meeting' | 'lecture' | 'interview' | 'idea' | 'general';
  /** 默认语言 */
  language: string;
  /** 界面主题 */
  theme: 'light' | 'dark' | 'system';
};

export const DEFAULT_SETTINGS: UserSettings = {
  chunkSeconds: 30,
  autoSummarize: true,
  summaryProviderId: null,
  summaryTemplate: 'meeting',
  language: 'zh',
  theme: 'system',
};

export function readSettings(userId: string): UserSettings {
  const result: any = { ...DEFAULT_SETTINGS };
  for (const row of all<{ key: string; value: string }>(
    `SELECT key, value FROM settings WHERE user_id = ?`,
    [userId],
  )) {
    try {
      result[row.key] = JSON.parse(row.value);
    } catch {
      result[row.key] = row.value;
    }
  }
  return result as UserSettings;
}

export function writeSettings(userId: string, patch: Record<string, unknown>): UserSettings {
  for (const [key, value] of Object.entries(patch)) {
    if (value === undefined) continue;
    run(
      `INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)
       ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value`,
      [userId, key, JSON.stringify(value)],
    );
  }
  return readSettings(userId);
}
