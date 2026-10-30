PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
  id            TEXT PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  display_name  TEXT,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
  id           TEXT PRIMARY KEY,
  user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name         TEXT NOT NULL,
  key_value    TEXT NOT NULL UNIQUE,
  created_at   TEXT NOT NULL,
  last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS providers (
  id         TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  category   TEXT NOT NULL,              -- asr | llm
  kind       TEXT NOT NULL,              -- dashscope | openai | mock | custom
  name       TEXT NOT NULL,
  base_url   TEXT,
  api_key    TEXT,
  model      TEXT,
  params     TEXT,                       -- JSON 扩展参数
  is_default INTEGER NOT NULL DEFAULT 0,
  enabled    INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recordings (
  id              TEXT PRIMARY KEY,
  user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title           TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'recording', -- recording|processing|ready|failed
  source          TEXT NOT NULL DEFAULT 'web',       -- web|upload|api
  language        TEXT NOT NULL DEFAULT 'zh',
  started_at      TEXT NOT NULL,
  ended_at        TEXT,
  duration_ms     INTEGER NOT NULL DEFAULT 0,
  asr_provider_id TEXT,
  summary_status  TEXT NOT NULL DEFAULT 'none',      -- none|queued|generating|ready|failed
  memo            TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rec_user_start ON recordings(user_id, started_at DESC);

CREATE TABLE IF NOT EXISTS chunks (
  id              TEXT PRIMARY KEY,
  recording_id    TEXT NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
  seq             INTEGER NOT NULL,
  filename        TEXT NOT NULL,
  path            TEXT NOT NULL,
  mime            TEXT,
  size            INTEGER NOT NULL DEFAULT 0,
  duration_ms     INTEGER NOT NULL DEFAULT 0,
  start_offset_ms INTEGER NOT NULL DEFAULT 0,
  status          TEXT NOT NULL DEFAULT 'pending',   -- pending|queued|transcribing|done|failed
  transcript      TEXT,
  segments        TEXT,
  error           TEXT,
  retries         INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  UNIQUE(recording_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_chunk_rec ON chunks(recording_id, seq);

CREATE TABLE IF NOT EXISTS summaries (
  id           TEXT PRIMARY KEY,
  user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  recording_id TEXT REFERENCES recordings(id) ON DELETE CASCADE,
  scope        TEXT NOT NULL,            -- recording | day | range
  date_from    TEXT NOT NULL,            -- YYYY-MM-DD
  date_to      TEXT NOT NULL,
  title        TEXT NOT NULL,
  markdown     TEXT,
  mindmap      TEXT,
  provider_id  TEXT,
  model        TEXT,
  status       TEXT NOT NULL DEFAULT 'queued',
  error        TEXT,
  stats        TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sum_user_date ON summaries(user_id, date_from DESC);

CREATE TABLE IF NOT EXISTS jobs (
  id          TEXT PRIMARY KEY,
  type        TEXT NOT NULL,             -- transcribe | summarize
  payload     TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'pending', -- pending|running|done|failed
  attempts    INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  run_at      TEXT NOT NULL,
  started_at  TEXT,
  finished_at TEXT,
  error       TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_poll ON jobs(status, run_at);

CREATE TABLE IF NOT EXISTS settings (
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  key     TEXT NOT NULL,
  value   TEXT,
  PRIMARY KEY (user_id, key)
);
