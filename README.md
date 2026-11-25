# 忆录 · Recall Workbench

**English** | [中文版 / Chinese](README.zh-CN.md)

A self-hosted recording memory workbench. Record directly in the browser → automatically split into chunks and upload → speech-to-text → AI-generated summaries and mind maps → revisit your days in **Memories** by day / week / year.

All data (audio, transcripts, summaries) lives in your own machine's `data/` directory, and AI providers can be swapped at any time.

## Features

- **Browser recording**: record in the browser; every N seconds a chunk is uploaded and transcribed, so text starts appearing before you stop recording
- **Long audio upload**: drop in a one-hour recording; the backend splits it with ffmpeg and queues it for transcription
- **AI summaries**: automatically extracts overview, key points, conclusions and todos, plus an interactive mind map; supports manual editing and **AI revision** — describe a change in a dialog and the AI revises that summary accordingly
- **Memories**: day / week / year views with already-organized memories; if a period has not been summarized yet, generation starts automatically and a waiting status is shown
- **Multiple providers**: configure several ASR and LLM providers, switch at any time, and test connectivity with one click; built-in presets for major Chinese LLMs auto-fill endpoints, and real model names can be fetched with one click
- **Optional local ASR**: transcribe fully offline with Qwen3-ASR / SenseVoice / Paraformer (sherpa-onnx), `faster-whisper`, or `whisper.cpp`; models can be downloaded in one click from the settings page
- **Local speaker diarization**: after transcription, automatically labels "Speaker 1/2/…" using pyannote segmentation + 3D-Speaker embeddings, pure CPU inference, audio never leaves the machine
- **Open API**: give scripts, recorders or other devices a simple endpoint — push audio, get text
- **MCP tools**: connect your own MCP services, use their tools in chat, and push Find summaries out automatically or manually
- **Outbound MCP service**: expose Recall itself as a token-protected MCP server so AI clients can upload recordings, read AI summaries by date and search memories; the dashboard shows clients and call logs
- **Passwordless hardware pairing**: a device announces/displays a fixed 6-digit code derived from its MAC; enter it once in the dashboard to bind the device and issue a dedicated token
- **Mobile-friendly**: the browser UI feels like a native app on phones and supports "Add to Home Screen"

## Tech stack

| Layer | Choice |
| --- | --- |
| Backend | Python 3.11+ with FastAPI and uvicorn (dependencies managed by [uv](https://github.com/astral-sh/uv)) |
| Database | PostgreSQL only (connection string in `server-py/.env`, connection pool + Alembic migrations) |
| Job queue | Database-table driven with a thread-pool worker, retries and exponential backoff |
| Audio processing | ffmpeg / ffprobe (automatic splitting, transcoding to 16 kHz mono) |
| Frontend | React 18 + Vite + TailwindCSS |
| Mind map | Custom SVG tree layout with zoom, drag and collapse |

> Earlier versions used a Node.js + Fastify backend, archived in `legacy/nodejs-server/` for reference only and no longer maintained.
> Both versions share the same HTTP contract and database schema, so the `data/` directory can be reused directly.

## Quick start

### Requirements

- Python ≥ 3.11 and [uv](https://github.com/astral-sh/uv) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Node.js ≥ 20 (only needed to build or develop the frontend)
- ffmpeg: `brew install ffmpeg` (macOS) / `apt install ffmpeg` (Debian-based Linux)

### One-click start (macOS, recommended for daily use)

Double-click **`启动 Recall.command`** in the project root, or run `bash scripts/start.sh`. The script will:

- install dependencies on first run, and rebuild the frontend automatically when its sources are newer than `web/dist`
- check that PostgreSQL is reachable and try to start a Docker container mapped to that port if it is not
- start the backend and open <http://localhost:8787> automatically (the backend serves the frontend too — **no** separate Vite process is needed)
- only open the browser if the service is already running, instead of starting a duplicate
- **stop the service when you close the terminal window or press Ctrl+C**

Logs go to `data/logs/server.log`.

### Local development

```bash
npm run dev
```

- Frontend with hot reload: <http://localhost:5173> ← use this during development
- Backend API: <http://localhost:8787> (Swagger docs at <http://localhost:8787/api/docs>)

On first start an **admin / admin123** account is created automatically; sign in and change the password under **Settings → Account**.

### Production mode (single port)

```bash
npm run build      # build the frontend into web/dist
npm start          # FastAPI serves the static frontend files as well
```

Visit <http://localhost:8787>.

### Docker

```bash
docker compose up -d --build
```

Data persists in `./data` and survives container restarts. The image already includes ffmpeg.

## Database: PostgreSQL

PostgreSQL is the only supported database. Connection details are **only configured in files** — there is no UI switch; restart the backend after changing them.

For a local installation, edit `server-py/.env`:

```bash
DATABASE_URL=postgresql://postgres:secret@127.0.0.1:5432/recall?sslmode=disable
```

For Docker, edit `DATABASE_URL` for the `recall` service in `docker-compose.yml`. By default it points to the compose `postgres` service (the hostname is the service name); to use an existing database on the host, use `host.docker.internal`:

```yaml
DATABASE_URL: postgresql://postgres:secret@host.docker.internal:5432/recall?sslmode=disable
```

Supported query parameters in the connection string:

| Parameter | Description |
| --- | --- |
| `sslmode` | `disable` / `prefer` / `require` / `verify-full`, defaults to `prefer` |
| `schema` | schema for the tables, defaults to `public`; created automatically if missing |
| `pool_size` | connection pool limit, defaults to 10 |

Three things happen automatically at startup: create the database if it does not exist (requires database-creation privileges) → run Alembic migrations to create tables / add columns → if the database has no accounts, create the first administrator with `ADMIN_USER` / `ADMIN_PASSWORD` (also configured in files).

If the database cannot be reached, the service still starts; `/api/health` reports `connected: false`, and `/setup` shows the current connection string (without the password) plus which file and line to edit.

A minimal PostgreSQL instance:

```bash
docker run -d --name recall-pg \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=recall -p 5432:5432 postgres:16
```

Compose includes a `postgres` service, started together by `docker compose up -d --build`, with data in `./pg-data/`.

### Schema evolution and connection pool

- Table schemas are versioned with **Alembic** (`server-py/app/migrations/versions/`) and automatically `upgrade head` at startup; upgrading an older database only adds missing columns and indexes and never touches existing data.
- A **psycopg connection pool** is used (created lazily, up to 10 connections by default); on shutdown the old pool is closed gracefully in a background thread so requests are not blocked.
- After changing the configuration (`DATABASE_URL`) you **must restart the process** — there is no runtime database switching.

Add a table / change a field:

```bash
cd server-py
.venv/bin/alembic -c alembic.ini revision -m "add xxx"   # generate an empty migration, then write the SQL
```

> The old `data/recall.db` (SQLite) is no longer supported and will not be read. If you need data from it, export it and import it into PostgreSQL with SQL, or simply delete it.

## Configuring AI providers

Open **Settings** and configure **Speech Recognition** and **Language Model** separately; creating one default entry for each is enough.

### Speech recognition (ASR)

| Type | Description |
| --- | --- |
| Local · sherpa-onnx (Qwen3-ASR / SenseVoice) | **Best for Chinese**. Pure ONNX, no PyTorch needed; Qwen3-ASR is the most accurate for Chinese and SenseVoice the fastest; models can be downloaded in one click from the settings page |
| Local · MLX Qwen3-ASR 1.7B | **Apple Silicon only**. 6-bit quantized weights from `mlx-community` running on Metal; noticeably better Chinese accuracy than the 0.6B model |
| Local · faster-whisper | Offline, one `pip` package, works on CPU/GPU, widest language coverage |
| Local · whisper.cpp | Offline single-file binary, Metal on Apple Silicon, CUDA on NVIDIA, fastest |
| Alibaba Bailian · Paraformer realtime | Lowest latency among cloud options; streams local audio over WebSocket, no public address needed, returns per-sentence timestamps |
| Alibaba Bailian · Qwen3-ASR | Synchronous API, high accuracy, no sentence-level timestamps |
| OpenAI-compatible `/audio/transcriptions` | Any whisper-protocol gateway; leave the key empty for a locally running faster-whisper-server / speaches |
| Demo engine | Calls no external service and returns mock text, useful for validating the flow |

Example cloud configuration (Bailian):

```
Base URL   https://dashscope.aliyuncs.com
API Key    sk-xxxxxxxx
Model      paraformer-realtime-v2
```

### Local open-source speech recognition (offline)

Use these when audio must never leave the machine; pick one of three engines based on hardware and language:

| | sherpa-onnx | faster-whisper | whisper.cpp |
| --- | --- | --- | --- |
| Install | `uv sync --extra local-asr` | same | `brew install whisper.cpp` (or build it yourself) |
| Inference backend | ONNX Runtime (CPU) | CTranslate2 (CPU int8 / CUDA) | ggml (Metal / CUDA / CPU) |
| Models | Qwen3-ASR / SenseVoice / Paraformer | all Whisper sizes | all Whisper sizes |
| Best for | **Chinese meetings, accuracy first** | multilingual, cross-platform, easiest in containers | fastest Whisper on Mac |

For Chinese, Whisper is no longer the default: on AISHELL-1, Whisper-large-v3 has a character error rate of about 5%, Qwen3-ASR 0.6B about 1.5%, and SenseVoice about 3–4%; Qwen3-ASR also adds punctuation and detects languages and Chinese dialects automatically.

sherpa-onnx models (one-click download in settings, pure ONNX, no PyTorch):

| Model | Size | Notes |
| --- | --- | --- |
| **Qwen3-ASR 0.6B** | 838 MB | Most accurate for Chinese, 52 languages + 22 Chinese dialects, automatic language detection, built-in punctuation; ~9× realtime on CPU |
| SenseVoice Small | 158 MB | 234M non-autoregressive, ~30× realtime; Chinese, English, Cantonese, Japanese, Korean, with emotion / audio-event tags |
| Paraformer Chinese | 218 MB | DAMO Academy Chinese model, stable sentence-level timestamps, sufficient for Chinese-only meetings; no punctuation |

On Apple Silicon you can also choose **MLX Qwen3-ASR 1.7B (6-bit)** (~1.9 GB):

| Model | Size | Notes |
| --- | --- | --- |
| Qwen3-ASR 1.7B · 6-bit | ~1.9 GB | Metal accelerated, Chinese accuracy a step above 0.6B; ~3.5 GB resident memory, roughly 2× slower |

MLX works only on M-series chips; on Intel Macs, Linux and containers it is reported as unavailable. Weights come from `mlx-community/Qwen3-ASR-1.7B-6bit` and the settings page downloads them **from ModelScope by default** (direct access in China with per-file resume); if ModelScope is unavailable it falls back to `hf-mirror.com`, then huggingface.co.

Install dependencies:

```bash
# sherpa-onnx + faster-whisper (one command, no extra binaries needed)
cd server-py && uv sync --extra local-asr

# whisper.cpp (macOS)
brew install whisper.cpp        # provides whisper-cli
# on other platforms after building: export WHISPER_CPP_BIN=/path/to/whisper-cli

# include local engines in Docker (larger image)
INSTALL_LOCAL_ASR=1 docker compose up -d --build
```

Then open **Settings → Speech Recognition → Add**, choose **Local · sherpa-onnx**, **Local · MLX Qwen3-ASR**, **Local · faster-whisper** or **Local · whisper.cpp**, click **Download** in the model list, select the downloaded model, save and set it as default. The test button first checks dependencies and models, then runs a real inference on 0.6 seconds of silence; it is marked ready only if that succeeds.

Whisper model sizes and recommendations:

| Model | Size | Recommendation |
| --- | --- | --- |
| tiny | ~75 MB | only for a smoke test, many Chinese errors |
| base | ~145 MB | lightweight, barely usable for short sentences |
| **small** | ~480 MB | the smallest Whisper size reasonably usable for Chinese, realtime on CPU |
| medium | ~1.5 GB | noticeably better accuracy, slow on CPU |
| large-v3 | ~3.1 GB | most accurate Whisper, 16 GB+ RAM or a GPU recommended |
| large-v3-turbo | ~1.6 GB | close to large-v3 quality, several times faster |

Implementation details:

- All models live in `data/models/` (`sherpa-onnx/<model dir>/`, `whisper-cpp/ggml-*.bin`, `faster-whisper/<size>/`, `mlx-qwen3-asr/<repo name>/`); deleting a directory reclaims space, and the settings page can delete models individually.
- **Speaker diarization** uses a separate set of local models in `data/models/speaker-diarization/` (pyannote segmentation ~6 MB + 3D-Speaker embedding ~38 MB). Enable **Settings → Recording & Summary → Speaker diarization**, click **Download**, and newly completed recordings are labeled "Speaker 1/2/…"; pure CPU inference, audio stays on the machine. Recordings transcribed before it was enabled are not processed retroactively.
- **sherpa-onnx model archives come from GitHub Releases** (158 MB–838 MB each). If direct access from China is slow or drops, set `SHERPA_GH_PROXY=https://gh-proxy.com` to use a proxy; by default it automatically falls back to `gh-proxy.com` when direct download fails.
- Whisper models use `hf-mirror.com` by default (huggingface.co is generally unreachable from China). With direct internet access, set `HF_DOWNLOAD_SOURCE=hf`; to use your own mirror, set `HF_ENDPOINT=https://your-mirror`.
- MLX models bypass `huggingface_hub` and download per file straight from **ModelScope** repository APIs, falling back to `hf-mirror.com` / huggingface.co on failure; if `HF_ENDPOINT` is set, your own mirror takes priority.
- Long audio is split into 25-second chunks at the lowest-energy silence (not hard cuts), so words are not split in half; chunks are further split into sentences at punctuation with sentence-level timestamps.
- Local inference is serialized by default (`LOCAL_ASR_CONCURRENCY=1`) to avoid saturating CPU and memory with parallel chunks; increase it on a powerful machine, but keep `ASR_CONCURRENCY` moderate too. Qwen3-ASR 0.6B uses about 1 GB of resident memory.
- For Chinese, a "please use Simplified Chinese" instruction is added automatically, since Whisper-family models often output Traditional Chinese otherwise.
- Model objects are cached in-process (up to 2); they are reloaded only when the model changes, so continuous recording transcription stays fast.
- Local engines do not perform diarization themselves; speaker labels come from the independent local model described above, regardless of the ASR provider.

### Language models (LLM)

Major Chinese providers are built in; selecting one fills in the Base URL and default model automatically:

| Group | Provider | Built-in endpoint |
| --- | --- | --- |
| China | Alibaba Bailian · Qwen | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| China | DeepSeek | `https://api.deepseek.com` |
| China | Zhipu · GLM | `https://open.bigmodel.cn/api/paas/v4` |
| China | Moonshot · Kimi | `https://api.moonshot.cn/v1` |
| China | SiliconFlow | `https://api.siliconflow.cn/v1` |
| China | ByteDance · Volcengine Ark (Doubao) | `https://ark.cn-beijing.volces.com/api/v3` |
| China | Baidu · Qianfan (Ernie) | `https://qianfan.baidubce.com/v2` |
| China | Tencent · Hunyuan | `https://api.hunyuan.cloud.tencent.com/v1` |
| China | iFlytek · Spark | `https://spark-api-open.xf-yun.com/v1` |
| China | MiniMax | `https://api.minimaxi.com/v1` |
| China | StepFun | `https://api.stepfun.com/v1` |
| Overseas | OpenAI / Claude / OpenRouter | official endpoints |
| Generic | Custom (OpenAI-compatible) | for gateways, vLLM, Ollama, LM Studio |

Three buttons keep configuration effort minimal:

- **Fetch models**: after entering a key, call the provider's `/models` endpoint and turn real model names into clickable tags — no copying names from the website needed. Providers that cannot be enumerated (Volcengine Ark endpoint IDs, Spark, Hunyuan, etc.) prompt you to enter the name manually.
- **Test connection**: no need to save first; the form sends a minimal "reply with two words" request.
- **Get key**: each provider links directly to its console key page.

> For Volcengine Ark, the model name is usually an **inference endpoint ID** starting with `ep-`, not the model name itself.
> For Spark, enter the console's **API password**, not the AppID.

Endpoint inference: a bare domain (e.g. `https://api.deepseek.com`) gets `/v1` appended; URLs already containing a version segment (`/compatible-mode/v1`, `/api/paas/v4`, `/api/v3`) are used as-is. To add another provider, append an entry to `VENDORS` in `server-py/app/providers/llm_vendors.py`.

### Summarization prompts

Five built-in styles are available under **Settings → Recording & Summary → Summary style**; they determine the **section structure** of a summary:

| Style | Sections |
| --- | --- |
| Meeting minutes | Meeting info / Key discussion / Conclusions & decisions / Todos |
| Course notes | Topic / Core concepts / Detailed notes / Examples & exercises / Review points |
| Interview record | Interviewee & background / Key views / Notable quotes / Facts & figures / Follow-up directions |
| Idea snippets | List of ideas / Where they came from / Feasibility & risks / Next steps |
| General | Core overview / Key content / Conclusions / Todos |

All five share the same hard rules (use only information from the transcript, do not invent, fix homophone typos, todos must be grounded in the transcript) and the same JSON contract (`title / summary / markdown / mindmap / highlights / todos / moods`), so mind maps and highlights render in every style. Recordings over 16,000 characters are summarized in chunks first and then merged, with the same style applied to the chunk prompts.

There are also five **scope-driven memory styles** that are applied automatically and do not appear in the style switcher above:

| Scope | Style | Sections |
| --- | --- | --- |
| Single day (daily) | Daily memory | What we talked about today / Ideas that came up / Todos / People & moods |
| One week (weekly) | Weekly memory | What we worked on this week / Moments worth remembering / Todos / People & moods |
| One month (monthly) | Monthly memory | What we worked on this month / Important events / Lingering todos / People & moods |
| One year (yearly) | Yearly memory | The thread of the year / Key phases / What is still unfinished / People & moods |
| Any range | Period memory | What we worked on in this period / Moments worth remembering / Todos / People & moods |

Scope is determined by the date range (`services/prompts.py::period_of`): Monday–Sunday is weekly, the 1st to month-end is monthly, Jan 1 – Dec 31 is yearly, everything else is an arbitrary range. Memory styles explicitly ban meeting language (no "meeting info / agenda / decisions / attendees") and focus on who talked about what, which todos exist and how people felt; `moods` must be traceable to the original tone or wording, otherwise write "not expressed". Early daily summaries were generated in meeting style; regenerate once to switch to memory style.

### Hierarchical summaries: weeks / months / years no longer rescan transcripts

Each memory level reuses lower-level results bottom-up instead of rereading all transcripts for that period:

| Level | Input | Typical input size |
| --- | --- | --- |
| Single recording / daily | transcript of that recording or day (with timestamps) | that day's transcript |
| Weekly / monthly / arbitrary range | **daily summary markdown** for each day in the range | 7 / 30 files, ~10k–40k characters |
| Yearly | **12 monthly summary markdown** files for that year | 12 files, ~10k–20k characters |

Ranges longer than a year bypass the hierarchy and fall back to reading transcripts. Implementation is in `server-py/app/services/summarize.py`:

- `_collect_children()` inventories children with two queries: `ready` is used directly, `failed` is skipped, windows with no recordings are skipped, the rest are marked `pending`.
- When children are missing, the parent summary enters `waiting`; a background polling task (payload `checkChildren`) idempotently queues missing daily / monthly summaries. Monthly summaries cascade by day, forming a two-level waterfall "yearly → 12 monthly → daily for each day".
- Once children settle (or after 60 minutes) `_kick_parent()` puts the parent job back in the normal queue. Polling only queries the database, never calls a model.
- `_context_from_children()` assembles the input; the prompt states that "these memories are the source material for this summary", forbids day-by-day retelling and asks for merged threads and turning points; `build_system_prompt()` / `build_chunk_prompt()` also switch wording based on input type.
- Result `stats` includes `childSummaries` (how many children were used) and `childMissing` (how many were skipped).

**Benefits and trade-offs**:

- The first yearly summary still needs to fill in all missing daily / monthly summaries at once; that means many jobs and a wait, but each job has a small input, avoiding the old behavior of stuffing a whole year into one call and hitting length limits or truncation.
- Regenerating a weekly / monthly / yearly summary afterwards only reads a dozen generated texts, greatly reducing time and tokens. Opening the calendar day view creates the daily summary as a side effect.
- The hierarchy is lossy: details that lower-level memories did not record are invisible upstream. To keep more detail in the yearly memory, make the daily / monthly custom prompts more specific.
- **Reorganizing a parent only summarizes children that exist now; it does not cascade a recompute of children.** After editing a daily prompt or regenerating a day's summary, click reorganize in the order "daily → monthly → yearly" for changes to reach the top.

Under **Settings → Prompts** you can add custom requirements **per scope** (up to 2,000 characters each):

| Card in the tab | Field | Scope |
| --- | --- | --- |
| Single recording | `summaryPrompt` | summary of one recording (shared by all styles) |
| Daily | `promptDay` | single-day memory |
| Weekly | `promptWeek` | Monday–Sunday |
| Monthly | `promptMonth` | whole month |
| Yearly | `promptYear` | whole year |
| Any range | `promptRange` | custom range that is not a full week / month / year |

Your text is treated as "user custom requirements" and takes priority over the style description, but hard rules and the JSON contract remain — so even "use a conversational tone and label every conclusion with its speaker" will not turn output into plain text. Leave empty to use the built-in prompt. Existing memories need a **reorganize** click to pick up new prompts.

- The style travels with the summary: `summaries.template` records the style used at generation time and reuses it on regeneration, so it does not silently revert to the default.
- Prompts are implemented in `server-py/app/services/prompts.py`; edit `focus / outline / tips` of the corresponding style in `STYLES` to change wording.

### Manual edits and AI revision

Every summary (the **AI Summary** tab on a recording detail page and the summary detail page) supports two kinds of edits:

- **Manual edit**: click **Edit**, change the title and Markdown directly, save with ⌘S. Only this summary's text is touched; no model call is made. Clicking **Regenerate** overwrites manual edits.
- **AI revision**: click **AI revision**, state a request in one sentence (e.g. "make the title a question", "cut todos down to what can be done today"). The AI revises the summary based on it and the original recording material, leaving unmentioned parts unchanged. The page polls automatically and refreshes when done.

AI revision shares the background job queue with initial generation: `POST /api/summaries/{id}/revise` enqueues a `revise_summary` job; the revision prompt is `build_revise_system_prompt` in `server-py/app/services/prompts.py`, and handling is `handle_revise_job` in `services/summarize.py` (single recordings read the transcript, day/week/month/year memories read child summaries, long material is chunked). You cannot request a revision while the summary is still generating, to avoid concurrent overwrites.

## Settings page

Settings are split into tabs (the URL carries `?tab=`, so a tab can be bookmarked):

| Tab | Contents |
| --- | --- |
| AI services | Speech recognition / LLM providers, defaults, connectivity tests |
| MCP tools | Connect external MCP services (HTTP / SSE), list tools, debug calls, summary push, one-click send for AI summaries, manually allow / disallow tools for sending summaries |
| Recording & summary | chunk duration, automatic summaries, summary style, recognition language, UI theme |
| Prompts | extra requirements for single recordings, daily / weekly / monthly / yearly summaries and arbitrary ranges |
| Jobs | running / queued / recently finished background jobs, with duration, heartbeat and retry |
| Logs | live backend logs (print, tracebacks, job events, uvicorn) with level filter, search, copy, download and clear |
| Data & devices | Open API keys, outbound MCP service (token / tools / call logs), hardware device binding and session parameters |
| Account | change password, sign out |

A small badge at the top of settings always shows "N jobs running / N jobs queued"; click it to jump to the Jobs tab.

Job and log APIs are in `server-py/app/routes/system.py`:

- `GET /api/system/tasks`: running / queued / recently finished; payloads are translated into human-readable text (which recording, which chunk, which period)
- `POST /api/system/tasks/{id}/retry`: requeue a failed job
- `GET /api/system/logs?level=&q=&after=`: incremental log fetch (`after` is the previous entry's id)
- `DELETE /api/system/logs`: clear the in-memory buffer

Logs live only in memory (most recent 4,000 entries, cleared on process restart) and are never written to files, avoiding unbounded growth and persisting user content; use **Download** in the page for long-term retention. See `server-py/app/logbuf.py`: it hooks `sys.stdout/stderr` line by line, while structured events such as job start/finish/failure/requeue and model / character counts are written directly via `logbuf.log()`.

## MCP tools

Each user can connect their own [MCP](https://modelcontextprotocol.io) services; their tools are available in the **Find** conversation and can push the final summary text externally (notes, todo apps, chat bots, etc.).

**Only remote HTTP / SSE is supported** — local commands are never executed: configuration comes from a web form and must not be able to start processes on the server.

### Adding a service

Two ways, both under **Settings → MCP tools**:

1. **Manual entry**: name, URL (`https://example.com/mcp`), transport (auto / Streamable HTTP / SSE), headers JSON
2. **Paste JSON**, compatible with Claude Desktop / Cursor format:

```json
{
  "mcpServers": {
    "My Notes": {
      "url": "https://example.com/mcp",
      "headers": { "Authorization": "Bearer sk-..." }
    }
  }
}
```

`command`-style local services are skipped with an explanation. Saving connects once immediately and persists the tool list, so the list shows each tool's name, description and parameters. Saved headers are masked afterward; leaving them untouched during edits keeps the original values.

**Tools that can receive an AI summary are detected automatically on add / refresh**: the `inputSchema` is checked for a text parameter and whether the tool is a write operation (pure query tools are excluded), and the body / title / date fields are inferred. The decision is cached in `mcp_tools.capability`, shown in settings with a "can send summaries" badge and the body field name.

**Suggested arguments are also generated automatically on add / refresh**: the LLM sees only tool names, descriptions and schemas (not summary content or call history) and produces a valid argument set per tool, stored in `mcp_tools.suggested_args`. The debug panel pre-fills these suggestions; if generation fails it falls back to "required-field defaults" by type. Suggestions are only defaults; after a human edit:

- **Debug panel**: click **Save as template** to write back to the database (`PATCH /api/mcp/servers/{id}/tools/{tool}/args`, keys filtered by a schema whitelist and coerced to the expected types); **AI generate** asks the model for a fresh set on the spot (`POST .../suggest-args`, automatically falling back to a backup model with a notice when the default engine does not respond); **Reset to template** returns to the skeleton values.
- **One-click send**: clicking a tool first opens a "confirm before sending" dialog showing the merged "suggested arguments + summary content" (`POST /api/mcp/run-tool-preview`, no actual call), with the body placed in the detected body field; the user can edit it or click **AI regenerate**, then confirm to send (`POST /api/mcp/run-tool` with `args`). Merge priority: **user edits > suggested arguments > semantic summary fill** (title / date fields are not stuffed with the body).
- Refreshing a service does not overwrite human-saved suggestions (`_save_tools` snapshots old values by tool name and restores them).

**Automatic decisions can be overridden manually**: expand the tool list and click "can send summaries / set as sendable" next to any tool to toggle it — designate one yourself if you disagree, or disable one that was auto-detected. Once enabled, a dropdown selects which parameter receives the summary body (only string parameters are listed), a "manual allow / manual exclude" badge is shown, and **Restore automatic** returns to the default rule.

- Manual decisions are stored as `capability.manual: true` and survive service refreshes (`_save_tools` rebuilds the whole tool table) and decision-rule upgrades; only **Restore automatic** clears them.
- If the server-side tool schema changes and a manually selected field no longer exists, refresh automatically falls back to the current inference so buttons never fail.
- API: `PATCH /api/mcp/servers/{id}/tools/{tool}/capability` with body `{"sendSummary": true/false, "bodyField"?: "...", "titleField"?: "...", "dateField"?: "..."}`, or `{"reset": true}` to restore automatic behavior. Returns the updated full server object.
- This changes the "sendable summary" tool list itself; "summary push" below, where the LLM picks tools by description, is a separate path and is unaffected.

### Using tools in chat

- For services with **Available in chat** enabled, tools are written into the Find prompt as `mcp__<service>__<tool>` so the model can save results externally; at most 2 calls, so retrieval rounds are not crowded out.
- The bottom of the Find page shows only **the tools the LLM selected for this Q&A** (4 built-in retrieval tools + recommended MCP tools): after each answer the question, answer and all tool names / descriptions / parameters are passed to the model, which decides which are genuinely useful, have auto-fillable parameters and are not irrelevant or destructive, up to 6; if none fit, nothing is listed. Model output is validated against a numeric whitelist, and another enabled model is tried automatically if the engine does not respond; on failure it prefers recommending nothing over showing every tool. Click "all N" to expand the full list.

### Summary push (decided by the LLM)

Each service has only "no push / send manually / send automatically" — **no tool names or argument templates to fill in**:

| Mode | Behavior |
| --- | --- |
| Automatic | after each Find answer that actually retrieved recordings, ask the model to make one decision |
| Manual | a "Send summary to XX" button appears under the answer; clicking it asks the model in the same way |

The scheduler passes the question, summary, matched recordings and all tool names / descriptions / parameter schemas to the LLM, which decides whether to send, which service, which tool and what each argument should be. Output is validated against a whitelist (service and tool must come from the list), coerced to the right types and filled where required; pure query tools are filtered out; when the model is unavailable or returns invalid output, it falls back to "pick a tool that accepts text and auto-fill it".

You can write a **natural-language preference** (e.g. "only save content with todos or conclusions, tag it recall") and the model uses it as a rule. The debug panel still lets you construct arguments and call individual tools manually.

### One-click send for AI summaries

Under every AI summary (recording detail page, summary detail page, inline previews on the Memories page) the tools in the "can send summaries" list are shown (auto-detected and manually designated, excluding manually excluded ones),
**one button per tool**: click one and it sends with that tool directly, with no model guessing and no arguments to fill in.
Clicking first opens a "confirm before sending" dialog showing the merged parameters of the LLM-generated **suggested arguments** and the summary body; the user can edit them (edited keys win over suggestions) and must confirm before the call.
The server reads the body from the database by `summaryId`, fills the whole Markdown into the detected body field according to the schema, fills title / date fields semantically, and shows the result and duration under the button; call logs mark it as "summary".
With no MCP configured, only a lightweight settings entry is shown so the layout stays clean. This button is a manual action and is independent of the automatic / manual switches of "summary push" above.

### APIs

- `GET/POST /api/mcp/servers`, `PATCH/DELETE /api/mcp/servers/{id}`
- `POST /api/mcp/servers/import`: JSON import
- `POST /api/mcp/servers/{id}/refresh`: reconnect and refresh the tool list (returns protocol version, handshake duration, error reason)
- `GET /api/mcp/servers/{id}/tools`, `POST /api/mcp/servers/{id}/tools/{tool}/call`: debug call
- `PATCH /api/mcp/servers/{id}/tools/{tool}/capability`: manually allow / disallow sending summaries, or `{"reset": true}` to restore automatic
- `POST /api/mcp/servers/{id}/tools/{tool}/suggest-args`: ask the LLM for suggested arguments based on the schema
- `GET/PATCH /api/mcp/servers/{id}/tools/{tool}/args`: read / save suggested arguments (human-edited templates, preserved on refresh)
- `POST /api/mcp/run-tool-preview`: `{summaryId, serverId, tool}` returns merged argument preview without calling
- `POST /api/mcp/run-tool`: `{summaryId, serverId, tool, args?}` one-click send, user-edited keys in `args` win
- `GET /api/mcp/tools`: tools available in chat by default; `?scope=summary` returns tools detected as able to receive AI summaries
- `POST /api/mcp/run-tool`: `{summaryId, serverId, tool}` sends a summary to a tool in one click
- `POST /api/find/push`: manually push a summary (the model also picks the tool)
- `GET /api/mcp/logs?serverId=&limit=`: recent call logs (debug / chat / push / summary, most recent 60 kept)

Implementation: `server-py/app/services/mcp_client.py` (transport layer, synchronous wrapper around the official SDK),
`server-py/app/services/mcp_servers.py` (configuration, tool cache and capability detection),
`server-py/app/services/mcp_dispatch.py` (LLM tool decisions, argument validation, one-click summary calls),
`server-py/app/services/mcp_arg_suggest.py` (schema-based suggestions, human template I/O, pre-generation),
`server-py/app/services/mcp_recommend.py` (tool recommendation on the Find page),
`server-py/app/routes/mcp.py`, `web/src/components/SummaryMcpActions.tsx` (tool buttons under summaries),
`web/src/pages/settings/McpTab.tsx` (debug panel argument pre-fill / AI generation / save as template).

## Usage flow

1. On the home page, click **Start recording** or **Upload audio**
2. While recording, every N seconds (30 by default, configurable in settings) a chunk is uploaded and transcribed
3. When recording ends you land on the detail page; once transcription completes, a summary is generated automatically according to settings
4. Switch between the **AI Summary / Mind map / Transcript** tabs
5. In **Memories**, revisit by day / week / month / year: the day view is a calendar plus all records of that day; week / month / year views aggregate lower-level memories hierarchically (see "Hierarchical summaries"), missing daily / monthly summaries are filled in automatically; the year view still starts only when you click a button, to avoid triggering a large number of jobs by accident; opening an unsummarized period starts summarizing automatically

### Find retrieval and speedups

**Find** does not let the LLM scan recordings directly: the model only decides "which tool to call" and "how to answer from evidence"; retrieval happens server-side through four paths — exact literal match (`LIKE`), approximate semantic match (TF-IDF sparse-vector cosine), list recordings by date, and read a single recording for verification.

Most latency comes from serial LLM calls (each decision or answer is a network round trip), so:

- once the model picks a retrieval path, the server runs complementary literal / semantic / date searches in the **same round** and proceeds with the evidence instead of wasting another decision round (`MAX_ROUNDS=2`); common paths drop from 3–4 LLM calls to 2
- the push decision for "summary push" and "tool recommendation" are independent and now run in parallel
- the TF-IDF index is cached by data signature in `data/finder_index/` (reused after process restart, safe to delete)
- migration `0015_search_trgm` adds `pg_trgm` GIN indexes on `recordings.title/memo`, `chunks.transcript`, and `summaries.title/markdown` so `LIKE '%word%'` no longer scans the whole table (1–2 character queries are still limited by trigrams and will scan — a PostgreSQL limitation)

> "Semantic" here means TF-IDF with word weighting, not neural embeddings. Improving recall of synonyms / paraphrases requires embedding + pgvector hybrid search — that is "searching more accurately", not "searching faster".

## Open API

Create a key under **Settings → Open API** and send it in the `X-API-Key` header. This key is **account-level** and suited to scripts; for microcontrollers without a keyboard, use the "device code → token" flow in the next section, with one key per device.

### One-step transcription (recommended)

```bash
curl -X POST "http://localhost:8787/api/open/transcribe?wait=1&title=Meeting%20recording" \
  -H "X-API-Key: rk_xxx" \
  -H "Content-Type: audio/mpeg" \
  --data-binary @audio.mp3
```

| Parameter | Description |
| --- | --- |
| `wait=1` | synchronously wait for transcription to finish (up to 120 s, adjustable with `timeout`); without it an id is returned immediately |
| `title` | recording title |
| `language` | `zh` / `en` / `auto` |

Response:

```json
{
  "id": "rec_xxx",
  "status": "ready",
  "text": "full transcript",
  "segments": [{ "startMs": 0, "endMs": 3200, "text": "First sentence…" }],
  "summaryStatus": "queued"
}
```

`multipart/form-data` uploads are also supported (field name `file`).

### Chunked push (for devices streaming while recording)

```bash
# 1. Create a session
curl -X POST http://localhost:8787/api/open/recordings \
  -H "X-API-Key: rk_xxx" -H "Content-Type: application/json" \
  -d '{"title":"Recorder A"}'
# → {"id":"rec_xxx"}

# 2. Push chunks (repeatable, seq increases)
curl -X POST "http://localhost:8787/api/open/recordings/rec_xxx/chunks?seq=0&durationMs=30000" \
  -H "X-API-Key: rk_xxx" -H "Content-Type: audio/webm" \
  --data-binary @chunk0.webm

# 3. Finish and trigger downstream processing
curl -X POST http://localhost:8787/api/open/recordings/rec_xxx/finish \
  -H "X-API-Key: rk_xxx"

# 4. Query status and full text
curl http://localhost:8787/api/open/recordings/rec_xxx -H "X-API-Key: rk_xxx"
```

## Outbound MCP service

Under **Settings → Data & devices → MCP service**, an account-level token (prefixed `mcps_`) is **generated automatically** the first time you open it; clicking **Reset** invalidates the old value immediately. The endpoint is `http://<host>:8787/mcp` (Streamable HTTP); older clients use `/sse`. Authentication accepts `Authorization: Bearer <token>`, `X-MCP-Token`, `X-API-Key`, or `?token=`.

Client configuration (works for Claude Desktop / Cursor / WorkBuddy):

```json
{
  "mcpServers": {
    "Recall": {
      "url": "http://localhost:8787/mcp",
      "headers": { "Authorization": "Bearer mcps_xxx" }
    }
  }
}
```

### Tools provided

| Tool | Purpose |
| --- | --- |
| `upload_recording` | upload a recording (`audioBase64` + `mimeType`), same effect as device / Open API upload: create record → transcribe → auto-generate AI summary; `wait=true` returns the full text synchronously |
| `get_daily_summary` | output the AI summary for a date (`YYYY-MM-DD`): full daily memory + each recording's summary for that day + todos / highlights; `generateIfMissing=true` generates it on the spot |
| `list_recordings` | list recordings by date range (title, time, duration, status) |
| `get_recording` | read a single recording's full transcript, timestamps and AI summary |
| `search_memory` | keyword search in titles, notes, full transcripts and summaries |
| `list_summaries` | list existing summaries / memories, filterable by scope and date |
| `get_action_items` | formal todos + AI candidate todos |
| `generate_summary` | trigger / regenerate a memory for a day or range; weekly / monthly / yearly first fill in missing daily / monthly summaries |
| `get_service_status` | overview of recording count, summary count, queue, etc. |

Every query is isolated to the account matching the token, so clients cannot see other users' data. Uploaded audio is capped at 32 MB after base64 decoding; logs replace base64 with a byte-count description, and raw audio is never written into the database.

### Call logs

The `initialize` handshake records `clientInfo` (name / version / protocol version); later requests are grouped into the same client by `Mcp-Session-Id`. Settings shows connected clients (IP, first / last active, call count), the most recent 60 calls (time, client, method, tool, success / failure, duration), and cumulative calls / tool calls / client count. Only the most recent 300 records are kept and can be cleared with one click. Token resets, disabling and authentication failures are also recorded.

### APIs

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/mcp` | MCP Streamable HTTP (JSON-RPC 2.0); also accepts single-event `text/event-stream` responses |
| `GET` | `/sse` | handshake stream for the legacy HTTP/SSE transport |
| `POST` | `/messages?sessionId=` | message endpoint for the legacy SSE transport |
| `DELETE` | `/mcp` | end a session |
| `GET` | `/api/mcp-service` | dashboard (JWT): endpoint, token, tool list, stats |
| `POST` | `/api/mcp-service/reset-token` | dashboard (JWT): reset the token |
| `PATCH` | `/api/mcp-service` | dashboard (JWT): `{enabled}` enable / disable |
| `GET` / `DELETE` | `/api/mcp-service/logs` | dashboard (JWT): clients and call logs / clear |

Implementation: `server-py/app/routes/mcp_service.py` (protocol and endpoints), `services/mcp_service.py` (token, sessions, logs),
`services/mcp_tools.py` (tool definitions and implementations), `web/src/pages/settings/McpServiceCard.tsx` (settings card).
The protocol is hand-written for MCP 2025-06-18 without the SDK's server part, so SDK upgrades cannot break the built-in service.

## Hardware device pairing (ESP32-C3 / voice recorder)

> Ready-made firmware lives in [`esp32/`](esp32/README.md) (Chinese documentation): press-and-hold recording, VAD silence skipping, Opus 12 kbps, local storage first (TF card if inserted), and **upload only when storage is nearly full or the button is double-clicked**, with screen (SSD1306 / ST7735 / e-paper) and screenless (LED + buzzer) variants; builds for both S3 and C3.
> Networking can be hardcoded in menuconfig or configured on site (device opens a `Recall-XXXXXX` hotspot; open `192.168.4.1` on a phone to enter WiFi credentials).
> This section documents the protocol and a minimal example — enough to integrate another MCU.

A microcontroller has no keyboard to type a username and password, so binding uses only a **fixed 6-digit code announced/displayed at boot**:

```
① boot: announce / display code (derived from MAC, never changes)   e.g. 903471
        ↓  dashboard: Settings → Hardware devices → Add device, enter the code
② server issues a dedicated token for this device
        ↓  device polls /api/open/pair and picks it up within seconds, stores it in NVS
③ afterwards each audio chunk carries only X-Device-Token; the server merges chunks into a session
```

Once bound, the device knows nothing about any account; moving to another server or account only requires rebinding, or clicking "Reissue token" in the dashboard.

### ① Device side: MAC → 6-digit code

**Digits only** is deliberate: a screenless recorder can speak "nine zero three four seven one" and a human can type it immediately, without distinguishing letters or case.

```cpp
uint64_t mac = ESP.getEfuseMac();              // ESP32-C3 factory MAC
uint32_t x   = (uint32_t)(mac & 0xFFFFFFFFULL);
uint32_t code = (x * 2654435761u) % 1000000u;  // scramble then take 6 digits, so adjacent MACs are not adjacent codes
char buf[7];
snprintf(buf, sizeof(buf), "%06u", code);      // e.g. 903471
```

- Codes contain only `0-9`, so dictation, keypad entry and TTS are error-resistant; the dashboard also normalizes input: `903-471`, `903 471`, "九零三四七一", `0` typed as `O`, `1` typed as `I/L` all resolve to the same device.
- The same board always has the same code; reflashing or changing servers does not change it.
- Six digits give 1,000,000 combinations. Binding is an immediate action within a window where the device is announcing the code and someone is typing it in the dashboard, so collisions are practically unheard of in home / small-batch use; if two devices really compute the same number, the first bound wins — delete the old device and bind the new one again.

### ② Dashboard: enter the code to bind

Open **Settings → Hardware devices → Add device**, type the 6 digits you heard or saw (name can be left blank to use the device's self-reported name) and confirm.

- While a device is already powered on and polling, a blue bar appears at the top of the section: "N devices waiting to pair"; clicking **Bind** there is faster.
- You can also **bind before powering on** (for stocked units in bulk): the device picks up the token on its first poll after going online.
- A code already bound to another account returns 409 "This device is bound to another account"; the original account must delete it first.
- Each device row shows the code (`903-471`), masked token, whether it is recording, and the per-device `gap` / `idle` thresholds.

### ③ Device side: retrieve the token and stream

Before binding, the device polls every 3 seconds; once you click **Bind**, the next poll returns the token:

```bash
curl "http://192.168.1.10:8787/api/open/pair?code=903471&name=c3-01&firmware=1.0.3"

# not bound in the dashboard yet
{ "status": "pending", "code": "903471", "pollAfterMs": 3000 }

# bound: polling returns the token (store it once; after a reissue, poll again to get the new one)
{ "status": "bound", "token": "dv_xxx", "deviceId": "dev_xxx",
  "name": "Living-room recorder", "gapSec": 90, "idleSec": 180 }
```

Store the token in NVS and send only that token with future audio; no account key is needed:

```bash
curl -X POST "http://192.168.1.10:8787/api/open/ingest?durationMs=10000" \
  -H "X-Device-Token: dv_xxx" \
  -H "Content-Type: audio/l16" \
  --data-binary @chunk.pcm
```

```json
{ "ok": true, "recordingId": "rec_xxx", "chunkSeq": 3,
  "newSession": false, "sessionChunks": 4, "sessionDurationMs": 41000 }
```

An invalid token (reissued, device deleted) returns 401 "Invalid device token, please pair again" — on 401 the firmware clears the NVS token and returns to polling, recovering automatically after the next bind.

### Session merging rules

The server **prefers device recording time** to determine session boundaries and falls back to server arrival time only when `recordedAt` is missing:

```
23:00:00  chunk 1 ─┐
23:00:10  chunk 2  ├─ gap 10s < threshold → same session (one record, one summary)
23:00:20  chunk 3 ─┘
                     ← 5 minutes of silence, over the "gap" threshold
23:05:20  chunk 4 ─── new session → new record
                     ← no more audio, silence over the "idle" threshold
               ✅ auto close: transcription finishes → summary for the whole conversation
```

> **Why recording time is required**: an offline device can accumulate a whole day of audio and push dozens of chunks within seconds after getting WiFi back. Using only arrival time, every gap looks like 1–2 seconds and the whole day becomes one record, breaking the timeline and summary. With `recordedAt`, the server computes the real gap as "this chunk's recording start − previous chunk's recording end". Old firmware without it keeps the previous behavior.

| Parameter | Default | Description |
| --- | --- | --- |
| `device` | `default` | device name, unique per account. **Can be omitted with token auth**, the token's device is used |
| `gapSec` | 90 | gap in seconds between chunks before starting a new record |
| `idleSec` | 180 | seconds of silence before auto-closing (triggers summary generation) |
| `durationMs` | auto-detect | duration of this chunk; if omitted the server probes with ffprobe |
| `title` | automatic | title of a new session |
| `recordedAt` | server time | **recording start** time of this chunk (ISO8601 UTC); strongly recommended for backfills, otherwise sessions merge into one |

`gapSec` / `idleSec` can be set per device in the dashboard and temporarily overridden by the same-named query parameters in a request.

### APIs

**Pairing (no key required, the device code is the credential)**

| Method | Path | Purpose |
| --- | --- | --- |
| `GET`/`POST` | `/api/open/pair?code=903471` | device polling: `pending` means not bound, `bound` issues the token; limited to 60 requests/minute per IP |
| `GET` | `/api/devices/claims` | dashboard (JWT): list codes currently polling and unclaimed |
| `POST` | `/api/devices/bind` | dashboard (JWT): `{code, name?}` binds and issues a token |
| `POST` | `/api/devices/{id}/reset-token` | dashboard (JWT): reissue the token, old token invalid immediately |

**Streaming (device token or account-level API key)**

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/open/ingest` | push one audio chunk, returns the current chunk index and whether it is a new session |
| `GET` | `/api/open/ingest` | device health check + current session state (startup self-test) |
| `POST` | `/api/open/ingest/close` | explicitly close before sleep / shutdown instead of waiting for the timeout |

Authentication forms:

```
X-Device-Token: dv_xxx          ← bound device (recommended)
Authorization: Bearer dv_xxx

X-API-Key: rk_xxx               ← account-level key, for scripts / unbound temporary devices
Authorization: Bearer rk_xxx
?key=rk_xxx
```

Audio can be `audio/wav`, `audio/webm`, `audio/mpeg`, or **raw PCM** (`audio/l16`, `audio/pcm`, `audio/raw`, 16 kHz mono by default, adjustable with `rate` / `channels`); the server adds a WAV header before recognition.

### ESP32-C3 example (Arduino)

At boot, derive the code from the MAC; use a stored token if present, otherwise poll until the dashboard binds the device; push one 10-second PCM chunk at a time.

```cpp
#include <WiFi.h>
#include <HTTPClient.h>
#include <Preferences.h>

const char* SERVER = "http://192.168.1.10:8787";   // computer's LAN IP
Preferences nvs;
String g_token;

// ① MAC → 6-digit code
String deviceCode() {
  uint64_t mac = ESP.getEfuseMac();
  uint32_t x = (uint32_t)(mac & 0xFFFFFFFFULL);
  uint32_t code = (x * 2654435761u) % 1000000u;
  char buf[7];
  snprintf(buf, sizeof(buf), "%06u", code);
  return String(buf);
}

// ①' screenless device: speak the 6 digits one by one at boot, clearer than reading "nine hundred thousand..."
void speakCode() {
  String c = deviceCode();
  for (int i = 0; i < 6; i++) {
    tts_speak_number(c[i] - '0');   // replace with your voice module's speak function
    delay(450);
  }
}

// ② poll for the token (every 3 seconds until bound)
bool pairOnce() {
  if (WiFi.status() != WL_CONNECTED) return false;

  HTTPClient http;
  http.setTimeout(8000);
  http.begin(String(SERVER) + "/api/open/pair?code=" + deviceCode() +
             "&name=c3-01&firmware=1.0.3");
  int code = http.GET();
  String body = http.getString();
  http.end();
  if (code != 200) return false;

  int i = body.indexOf("\"token\":\"");
  if (i < 0) {
    Serial.printf("[pair] waiting for dashboard binding, code %s\n", deviceCode().c_str());
    return false;
  }
  int s = i + 9, e = body.indexOf('"', s);
  g_token = body.substring(s, e);
  nvs.putString("token", g_token);
  Serial.println("[pair] bound, token stored in NVS");
  return true;
}

// ③ stream (token only)
bool pushAudio(const int16_t* pcm, size_t samples) {
  if (!g_token.length() || WiFi.status() != WL_CONNECTED) return false;

  HTTPClient http;
  http.setTimeout(15000);
  http.begin(String(SERVER) + "/api/open/ingest?durationMs=" + String(samples * 1000 / 16000));
  http.addHeader("Content-Type", "audio/l16");
  http.addHeader("X-Device-Token", g_token);
  int code = http.POST((uint8_t*)pcm, samples * sizeof(int16_t));
  String body = http.getString();
  http.end();

  if (code == 401) {                    // token reissued / device deleted → clear and re-pair
    nvs.remove("token");
    g_token = "";
  }
  Serial.printf("[ingest] %d %s\n", code, body.c_str());
  return code == 200;
}

void setup() {
  Serial.begin(115200);
  nvs.begin("recall", false);
  g_token = nvs.getString("token", "");
  WiFi.begin("SSID", "PASSWORD");
  while (WiFi.status() != WL_CONNECTED) delay(300);
  while (!g_token.length() && !pairOnce()) delay(3000);   // wait for the code to be entered in the dashboard
}
```

Practical advice:

- **Push one chunk every 10–30 seconds**. Too large: expensive retries and late summaries; too small: too many requests.
- Call `POST /api/open/ingest/close` once before shutdown so the summary does not wait for the 3-minute timeout.
- While offline, store PCM in PSRAM/Flash and backfill in order once connectivity returns — with `recordedAt`, the server judges gaps by **recording time**, so dozens of chunks sent in seconds will not merge into one; without it, arrival time is used.
- The device only needs LAN access to the computer; no public IP and no HTTPS required (TLS on ESP32-C3 is memory-hungry).
- Printing the code on the case / packaging is convenient: the digits never change, and re-entering them rebinds after a reset. If the device is deleted or the token leaks, click "Reissue token" in the dashboard: the old token dies immediately and the device re-pairs automatically.
- **Single-account servers claim devices automatically**: when the database has only one account, a device polling with its code is registered and issued a token without manual binding or reset; multi-account deployments still require explicit claiming.
- **Screenless devices**: speak digits one at a time at boot ("nine, zero, three, four, seven, one") with a confirmation tone; the dashboard accepts Chinese number words, so type what you hear.
- The code is effectively the device's **pairing credential**: anyone who knows it can obtain the token, so do not publish it on public pages; the service is deployed on a LAN (listening on the local network by default), which is a sufficient trust boundary.

## Mobile access and microphone permission

Browsers only allow microphone access on **HTTPS or localhost**. When a phone opens the app via a LAN IP (`http://192.168.x.x:8787`), the record button will be unavailable; in that case:

1. Use **Upload audio** — pick an existing recording from the phone's files / voice recorder; all features work
2. Or put an HTTPS entry in front of the service (Caddy / Nginx + self-signed certificate / a tunnel)
3. On iOS, use Safari's **Add to Home Screen** for a near-native app experience

## Data and backup

| Path | Contents |
| --- | --- |
| `server-py/.env` | configuration: database connection string (`DATABASE_URL`), JWT secret, initial admin account |
| `data/uploads/<recording id>/` | audio files, stored per chunk |
| `data/models/` | local open-source ASR models (only if a local engine was chosen); deleting them does not affect existing recordings |
| `pg-data/` | PostgreSQL data directory for compose (absent when using an external database) |

Backup: export the database with `pg_dump` and include the whole `data/` directory (audio and local models).
Deleting a record also cleans up its audio directory.

## Directory structure

```
.
├── server-py/                    FastAPI backend
│   ├── app/
│   │   ├── main.py              entry point: route registration, static hosting, worker startup, exception handling
│   │   ├── config.py            configuration and paths
│   │   ├── db.py                data access facade (all_/one/execute), PostgreSQL connection pool
│   │   ├── db_config.py         parse DATABASE_URL and describe the connection
│   │   ├── db_migrate.py        Alembic entry point: upgrade head at startup
│   │   ├── database/            PostgreSQL backend (connection pool), placeholder conversion, statement splitting
│   │   ├── migrations/          versioned DDL (starting with versions/0001_initial.py)
│   │   ├── security.py          bcrypt + JWT auth dependencies
│   │   ├── jobs.py              job queue and workers
│   │   ├── audio.py             ffmpeg / ffprobe wrapper
│   │   ├── bus.py               in-process event bus (for SSE)
│   │   ├── providers/           ASR / LLM adapters (cloud + local open-source, extensible)
│   │   ├── services/            transcription, summaries, prompts, provider resolution, local model repository, device pairing and session merging, user preferences
│   │   └── routes/              HTTP endpoints (auth / setup / database / recordings / summaries / providers / devices / open)
│   ├── pyproject.toml           dependencies (uv)
│   └── .env.example             environment variable template
├── web/
│   └── src/
│       ├── pages/               login / home / recording / detail / memories / settings
│       ├── components/          layout, waveform, mind map, Markdown rendering
│       └── lib/                 API client, auth, toast notifications
├── data/                        runtime data (created automatically)
├── legacy/nodejs-server/        archived Node.js backend
└── scripts/dev.sh               one-command development startup
```

## Adding a new ASR provider

Subclass `AsrEngine` in `server-py/app/providers/asr.py`:

```python
class MyAsr(AsrEngine):
    def transcribe(self, file_path, mime=None, language=None, duration_ms=None) -> AsrResult:
        return {
            "text": "...",
            "segments": [{"startMs": 0, "endMs": 1000, "text": "..."}],
        }
```

Then register it in a `create_asr_engine` branch and add the type to `ASR_KINDS`; the option appears in settings automatically. The same applies to `LlmEngine.chat()` in `llm.py`.

Local engines have extra conventions: add `localBackend: "sherpa-onnx" | "faster-whisper" | "whisper-cpp" | "mlx-qwen3-asr"` and `requiresKey/requiresBaseUrl: false` to the entry in `ASR_KINDS`; the frontend then hides the Key / URL inputs and shows a local model selector instead.
Model files and download logic are in `services/local_asr_models.py`: add a line to `FASTER_WHISPER_SIZES` / `WHISPER_CPP_SIZES` for a new Whisper size; add a line with "id / display name / size / description / archive name" to `SHERPA_MODELS` for a new sherpa-onnx model (pre-converted packages such as FireRedASR or Fun-ASR-Nano), and downloading and extracting are fully automatic.

## Known limitations

- Designed for single-user / small-team use; no multi-tenant isolation or quotas
- Closing the page mid-upload loses the final chunk (already uploaded chunks are unaffected)
- Long summaries use "chunked extraction + merge"; quality drops slightly for very long recordings (> 3 hours)
- Speaker diarization runs offline after transcription, not live; named voice identification (enrolling a person's voice and recognizing them automatically) is not implemented yet
- Local ASR requires downloading hundreds of MB of models on first use; be patient with slow sources or place the model files yourself
- On pure CPU, local ASR with `medium` or larger is noticeably slower than realtime; use `small` / `turbo` or a GPU for long recordings
- PostgreSQL only: changing connection details requires editing a file and restarting; there is no UI switch
- The old SQLite `data/recall.db` is no longer read; migrate historical data yourself

## Troubleshooting: local ASR reports "subprocess exited unexpectedly (code=1)"

Local engines (sherpa-onnx / faster-whisper) run in a separate subprocess. If every chunk fails within seconds and the error also contains `SAFE_DELETE_*` or other `sitecustomize` output, the cause is almost certainly a **`PYTHONPATH` polluted by the external environment**: some AI assistants / containers / security suites set `PYTHONPATH` in the shell to a directory containing `sitecustomize.py`, which hooks `os.remove`; the inference process is blocked while cleaning up a temporary PCM file and exits.

Check and fix:

```bash
python -c "import py_compile,sys;print(sys.path)"   # look for strange shim directories
env -u PYTHONPATH -u NODE_OPTIONS python -m app.main  # start with a clean environment
```

The code already has a safeguard: `PYTHONPATH` is stripped when spawning the subprocess, and the last lines of its stderr are appended to the error message (`Local ASR subprocess exited unexpectedly (code=1): <subprocess output>`), so the cause is visible next time.
