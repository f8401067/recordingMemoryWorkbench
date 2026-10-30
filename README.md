# 忆录 · Recall Workbench

本地部署的录音记忆库。网页直接录音 → 自动切片上传 → 语音识别转文字 → AI 生成纪要与思维导图 → 在「回忆」里按日 / 周 / 年回看每一天。

数据（音频、文字、纪要）全部保存在你自己机器的 `data/` 目录里，AI 服务商可自由更换。

## 功能

- **网页录音**：浏览器直接录，每 N 秒自动切一片上传并转写，录音没停文字就开始出
- **长音频上传**：拖一个 1 小时的录音文件进去，后端用 ffmpeg 自动切片并排队转写
- **AI 纪要**：自动提炼概要、关键内容、结论、待办，并生成可交互的思维导图
- **回忆回看**：日 / 周 / 年三种视角，打开就看到已经整理好的回忆；某一段还没有整理时会自动开始总结并显示等待状态
- **多服务商**：ASR 与 LLM 都可配置多个、随时切换、一键测连通性；语言模型内置国内各家大模型预设，选中即带出地址、可一键拉取真实模型名
- **本地 ASR 可选**：不联网也能转写——Qwen3-ASR / SenseVoice / Paraformer（sherpa-onnx）、`faster-whisper`、`whisper.cpp`，模型在设置页一键下载
- **开放 API**：给脚本、录音笔或其他设备一个接口，推音频即可拿文字
- **硬件免登录配对**：设备开机播报/显示 6 位数字编号（MAC 换算、固定不变），后台输一次就完成绑定并下发专属 token
- **移动端适配**：手机浏览器打开就是原生 App 的体验，支持加到主屏

## 技术栈

| 层 | 选型 |
| --- | --- |
| 后端 | Python 3.11+ + FastAPI + uvicorn（依赖用 [uv](https://github.com/astral-sh/uv) 管理） |
| 数据库 | 内置 SQLite（默认，开箱即用）/ 外置 PostgreSQL（连接池 + Alembic 迁移），在「设置 → 数据存储」切换 |
| 任务队列 | 数据库表驱动 + 线程池 worker，支持重试与指数退避 |
| 音频处理 | ffmpeg / ffprobe（自动切片、转码为 16k 单声道） |
| 前端 | React 18 + Vite + TailwindCSS |
| 思维导图 | 自研 SVG 树布局，支持缩放、拖拽、折叠 |

> 早期版本的后端用 Node.js + Fastify 实现，已归档在 `legacy/nodejs-server/`，仅作参考，不再维护。
> 两个版本的 HTTP 契约、数据库表结构完全一致，`data/` 目录可直接沿用。

## 快速开始

### 依赖

- Python ≥ 3.11 与 [uv](https://github.com/astral-sh/uv)（`curl -LsSf https://astral.sh/uv/install.sh | sh`）
- Node.js ≥ 20（仅构建/开发前端时需要）
- ffmpeg：`brew install ffmpeg`（macOS）/ `apt install ffmpeg`（Debian 系）

### 本地开发

```bash
npm run dev
```

- 前端热更新：<http://localhost:5173> ← 开发时访问这个
- 后端接口：<http://localhost:8787>（顺带提供 Swagger 文档 <http://localhost:8787/api/docs>）

首次启动会自动创建账号 **admin / admin123**，登录后请到「设置 → 账号」改密码。

### 生产模式（单端口）

```bash
npm run build      # 构建前端到 web/dist
npm start          # FastAPI 同时托管前端静态文件
```

访问 <http://localhost:8787>。

### Docker

```bash
docker compose up -d --build
```

数据持久化在 `./data`，重启容器不丢。镜像内已包含 ffmpeg。

## 数据库：内置 SQLite 或外置 PostgreSQL

默认用内置 SQLite（`data/recall.db`），零配置就能跑。**它只适合调试和单人使用**：
SQLite 同一时刻只有一个写者，多人同时录音/整理时会出现锁等待，也不支持远程访问。

正式部署请换成外置 PostgreSQL，在「设置 → 数据存储」里操作：

1. 选「外置 PostgreSQL」，填主机、端口、数据库名、用户名、密码（SSL 模式、schema 可选）
2. 「测试连接」——只验证服务器可达，库不存在也算通过
3. 「保存并切换」——自动建库（需要建库权限）、建表、补列，然后立刻切过去，**不需要重启进程**
4. 新库里没有账号，页面会自动跳到初始化向导，创建第一个管理员后即进入系统

关于数据：

- **切换不会迁移任何数据**。新库里没有账号、录音记录和纪要，需要用新库的账号重新登录；
  音频文件仍在 `data/uploads/`（文件不在库里）。
- 想切回 SQLite，把选项改回「内置 SQLite」再保存即可，原 `recall.db` 里的数据还在。
- 连接信息保存在 `data/config.json`（密码明文存储，注意该文件的权限），格式：

```json
{
  "database": {
    "driver": "postgres",
    "host": "127.0.0.1",
    "port": 5432,
    "database": "recall",
    "user": "postgres",
    "password": "******",
    "sslmode": "prefer",
    "schema": "public"
  }
}
```

也可以用环境变量 `DATABASE_URL` 指定数据库，优先级高于配置文件（此时界面上只读）：

```bash
DATABASE_URL=postgresql://postgres:secret@127.0.0.1:5432/recall npm start
```

数据库连不上时服务仍然会启动，`/api/health` 会显示 `connected: false`，登录页和 `/setup`
会给出错误原因并提供修正入口——不会把你锁在门外。

一个最小可用的 PostgreSQL（Docker）：

```bash
docker run -d --name recall-pg \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=recall -p 5432:5432 postgres:16
```

想连 compose 里的一起跑，用叠加文件（不用改默认 compose）：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d --build
```

### 结构演进与连接池

- 表结构用 **Alembic** 版本化管理（`server-py/app/migrations/versions/`），启动时自动
  `upgrade head`；迁移脚本写的是两库通用的 SQL，SQLite 与 PostgreSQL 共用同一套历史。
  老库初次升级只会补缺的列和索引，不会动数据。
- PostgreSQL 侧走 **psycopg 连接池**（懒创建，默认最多 8 条连接），切库时旧池在后台线程里
  优雅关闭，不阻塞请求。
- 进程会盯着 `data/config.json` 的 mtime：**别的进程改了配置，本进程 1 秒内自动跟上**，
  多副本部署不用挨个重启。

新增一张表 / 改字段：

```bash
cd server-py
.venv/bin/alembic -c alembic.ini revision -m "add xxx"   # 生成空迁移，然后手写 SQL
```

### 已有数据搬家（SQLite → PostgreSQL）

网页上的切换是**不迁数据**的。要保留旧录音，用自带脚本：

```bash
cd server-py
# 先看一眼会搬多少行
.venv/bin/python -m app.tools.migrate_sqlite_to_pg \
  --url postgresql://postgres:secret@127.0.0.1:5432/recall --dry-run
# 真搬（目标库非空会拒绝，确认覆盖加 --force）
.venv/bin/python -m app.tools.migrate_sqlite_to_pg \
  --url postgresql://postgres:secret@127.0.0.1:5432/recall
```

脚本会按外键顺序逐表复制并校验行数；音频文件不在库里，记得把 `data/uploads/` 一起拷过去。
搬完在「设置 → 数据存储」里切到同一个库即可（账号密码、API Key 都会带过去）。

## 配置 AI 服务商

进入「设置」，分别配置**语音识别**和**语言模型**，各建一个默认项即可。

### 语音识别（ASR）

| 类型 | 说明 |
| --- | --- |
| 本地 · sherpa-onnx（Qwen3-ASR / SenseVoice） | **中文首选**。纯 ONNX、不需要 PyTorch，Qwen3-ASR 中文最准、SenseVoice 最快，模型设置页一键下载 |
| 本地 · faster-whisper | 离线、`pip` 装一个包就能跑，CPU/GPU 通用，多语种覆盖最广 |
| 本地 · whisper.cpp | 离线、单文件二进制，Apple Silicon 走 Metal、N 卡走 CUDA，速度最快 |
| 阿里百炼 · Paraformer 实时 | 云端方案里延迟最好。WebSocket 流式推送本地音频，不需要公网地址，返回逐句时间戳 |
| 阿里百炼 · Qwen3-ASR | 同步接口，准确率高，无句级时间戳 |
| OpenAI 兼容 `/audio/transcriptions` | 可填任意 whisper 协议网关；本机跑的 faster-whisper-server / speaches 可以留空 Key |
| 演示引擎 | 不调用外部服务，返回模拟文本，用于验证流程 |

云端配置示例（百炼）：

```
Base URL   https://dashscope.aliyuncs.com
API Key    sk-xxxxxxxx
模型        paraformer-realtime-v2
```

### 本地开源语音识别（离线）

录音全程不出本机时选这里，三个引擎按机器和语种挑：

| | sherpa-onnx | faster-whisper | whisper.cpp |
| --- | --- | --- | --- |
| 安装 | `uv sync --extra local-asr` | 同左 | `brew install whisper.cpp`（或自行编译） |
| 推理后端 | ONNX Runtime（CPU） | CTranslate2（CPU int8 / CUDA） | ggml（Metal / CUDA / CPU） |
| 可跑模型 | Qwen3-ASR / SenseVoice / Paraformer | Whisper 全系 | Whisper 全系 |
| 适合 | **中文会议，准确率优先** | 多语种、跨平台、容器里最省事 | Mac 上跑 Whisper 最快 |

中文别再用 Whisper 当默认：AISHELL-1 上 Whisper-large-v3 字错率约 5%，Qwen3-ASR 0.6B 约 1.5%，
SenseVoice 约 3~4%，而且 Qwen3-ASR 自带标点、能自动识别语种和方言。

sherpa-onnx 侧的模型（设置页一键下载，纯 ONNX，不需要 PyTorch）：

| 模型 | 体积 | 特点 |
| --- | --- | --- |
| **Qwen3-ASR 0.6B** | 838MB | 中文最准，52 语种 + 22 种中文方言，自动语种、自带标点；CPU 约 9 倍实时 |
| SenseVoice Small | 158MB | 234M 非自回归，约 30 倍实时；中英粤日韩，同时输出情感/音频事件标签 |
| Paraformer 中文 | 218MB | 达摩院中文模型，句级时间戳稳，纯中文会议够用；不含标点 |

装依赖：

```bash
# sherpa-onnx + faster-whisper（一条命令，都不需要额外二进制）
cd server-py && uv sync --extra local-asr

# whisper.cpp（macOS）
brew install whisper.cpp        # 提供 whisper-cli
# 其他平台编译后：export WHISPER_CPP_BIN=/path/to/whisper-cli

# Docker 里带上本地引擎（镜像会大一些）
INSTALL_LOCAL_ASR=1 docker compose up -d --build
```

然后在「设置 → 语音识别服务 → 添加」，选「本地 · sherpa-onnx」「本地 · faster-whisper」或
「本地 · whisper.cpp」，在模型列表里点「下载」，下完选中保存并设为默认即可。
测试按钮会先检查依赖和模型，再用 0.6 秒静音跑一次真实推理，通过才算就绪。

Whisper 侧的模型大小与选择建议：

| 模型 | 体积 | 建议 |
| --- | --- | --- |
| tiny | ~75MB | 只适合试跑，中文错字多 |
| base | ~145MB | 轻量，短句勉强 |
| **small** | ~480MB | Whisper 里中文勉强可用，CPU 也能实时 |
| medium | ~1.5GB | 准确率明显更好，CPU 偏慢 |
| large-v3 | ~3.1GB | Whisper 最准，建议 16GB+ 内存或 GPU |
| large-v3-turbo | ~1.6GB | 接近 large-v3 的质量、速度快数倍 |

几个实现细节：

- 模型统一放在 `data/models/`（`sherpa-onnx/<模型目录>/`、`whisper-cpp/ggml-*.bin`、
  `faster-whisper/<size>/`），删目录即可回收空间，设置页也能单个删除。
- **sherpa-onnx 的模型包从 GitHub Releases 拉**（每个 158MB~838MB）。国内直连慢或断流时设
  `SHERPA_GH_PROXY=https://gh-proxy.com` 换代理源，默认会在直连失败后自动回退 `gh-proxy.com`。
- Whisper 模型默认走 `hf-mirror.com`（huggingface.co 在国内基本连不上）。有外网直连时设
  `HF_DOWNLOAD_SOURCE=hf`；想换成自建镜像设 `HF_ENDPOINT=https://your-mirror`。
- 长音频按 25 秒切块，切点挑能量最低的静音处（不是硬切），避免把一个词劈成两半；
  块内再按标点切句，输出句级时间戳。
- 本地推理默认串行（`LOCAL_ASR_CONCURRENCY=1`），避免多片并发把 CPU 和内存打满；
  机器够强可以调大，但要留意 `ASR_CONCURRENCY` 也别开太高。Qwen3-ASR 0.6B 常驻约 1GB 内存。
- 中文会自动带上「请使用简体中文」的引导提示词，否则 Whisper 系模型经常输出繁体。
- 模型对象在进程内缓存（最多 2 份），换模型才会重新加载，不影响连续录音的转写速度。
- 本地引擎没有说话人分离；要分人只能靠云端服务或后续接 pyannote 之类。

### 语言模型（LLM）

内置国内主流服务商，选中即带出 Base URL 与默认模型，不用记地址：

| 分组 | 服务商 | 内置地址 |
| --- | --- | --- |
| 国内 | 阿里百炼 · 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| 国内 | DeepSeek | `https://api.deepseek.com` |
| 国内 | 智谱 · GLM | `https://open.bigmodel.cn/api/paas/v4` |
| 国内 | 月之暗面 · Kimi | `https://api.moonshot.cn/v1` |
| 国内 | 硅基流动 | `https://api.siliconflow.cn/v1` |
| 国内 | 字节 · 火山方舟（豆包） | `https://ark.cn-beijing.volces.com/api/v3` |
| 国内 | 百度 · 千帆（文心） | `https://qianfan.baidubce.com/v2` |
| 国内 | 腾讯 · 混元 | `https://api.hunyuan.cloud.tencent.com/v1` |
| 国内 | 科大讯飞 · 星火 | `https://spark-api-open.xf-yun.com/v1` |
| 国内 | MiniMax | `https://api.minimaxi.com/v1` |
| 国内 | 阶跃星辰 | `https://api.stepfun.com/v1` |
| 海外 | OpenAI / Claude / OpenRouter | 官方地址 |
| 通用 | 自定义（OpenAI 兼容） | 中转站、vLLM、Ollama、LM Studio 填这里 |

三个按钮把配置成本压到最低：

- **自动获取**：填好 Key 后点一下，直接调服务商的 `/models` 把真实模型名拉成可点的标签，
  不用去官网抄模型名。拉不到的（火山方舟的接入点 ID、星火、混元等）会提示手填。
- **测试连接**：不用先保存，表单里填完就能测，会真发一次「只回复两个字」的最小请求。
- **去获取**：每个服务商旁边直接给控制台的 Key 页面链接。

> 火山方舟多数情况模型名要填**推理接入点 ID**（`ep-` 开头），不是模型名本身。
> 星火填的是控制台里的「API 密码」，不是 AppID。

地址推断规则：只给裸域名（如 `https://api.deepseek.com`）时自动补 `/v1`；
已经带版本段（`/compatible-mode/v1`、`/api/paas/v4`、`/api/v3`）的原样使用。
想新增一家，只要往 `server-py/app/providers/llm_vendors.py` 的 `VENDORS` 里加一条。

### 整理提示词

内置五套风格，在「设置 → 录音与纪要 → 纪要风格」里切换，决定纪要的**章节结构**：

| 风格 | 章节 |
| --- | --- |
| 会议纪要 | 会议信息 / 讨论要点 / 结论与决定 / 待办事项 |
| 课程笔记 | 本节主题 / 核心概念 / 详细知识点 / 案例与例题 / 复习要点 |
| 访谈记录 | 访谈对象与背景 / 核心观点 / 原话摘录 / 事实与数据 / 可追问的方向 |
| 灵感碎片 | 想法清单 / 想法从哪来 / 可行性与风险 / 下一步 |
| 通用 | 核心概要 / 关键内容 / 结论 / 待办事项 |

五套共用同一段硬性规则（只用原文信息、不编造、修正同音错字、待办须有原文依据）和同一份 JSON 契约
（`title / summary / markdown / mindmap / highlights / todos`），所以思维导图与高亮在任何风格下都能正常渲染。
超过 16000 字的录音会先分段提炼，再汇总，分段提示词同样带上风格。

同一位置还能填**自定义整理提示词**（最多 2000 字）。它作为「用户自定义要求」追加，优先于风格描述，
但硬性规则与 JSON 契约仍会保留——所以即使写「用口语化语气，每条结论标注发言人」也不会把输出变成纯文本。

- 风格跟着纪要走：`summaries.template` 会记录生成时用的风格，重新生成时沿用，不会莫名其妙变回默认值。
- 提示词实现在 `server-py/app/services/prompts.py`，想改措辞直接编辑 `STYLES` 里对应风格的 `focus / outline / tips`。

## 使用流程

1. 首页点「开始录音」，或「上传音频」
2. 录音时每隔 N 秒（默认 30，可在设置里改）自动切片上传并转写
3. 结束后自动进入详情页，转写完成后按设置自动生成纪要
4. 在「AI 纪要 / 思维导图 / 全文」三个页签之间切换查看
5. 「回忆」里按日 / 周 / 年回看：日视图是月历 + 当天全部记录，周/年视图会自动把这段时间的录音合并成一份回忆；没有整理过的时段会在打开时自动开始总结

## 开放 API

在「设置 → 开放 API」创建密钥，请求头带 `X-API-Key`。这个密钥是**账号级**的，适合脚本调用；
没有键盘的单片机请用下一节的「设备编号 → token」方式，每台设备一把钥匙。

### 一步转写（推荐）

```bash
curl -X POST "http://localhost:8787/api/open/transcribe?wait=1&title=会议录音" \
  -H "X-API-Key: rk_xxx" \
  -H "Content-Type: audio/mpeg" \
  --data-binary @audio.mp3
```

| 参数 | 说明 |
| --- | --- |
| `wait=1` | 同步等待转写完成（最长 120s，可用 `timeout` 调整）；不加则立即返回 id |
| `title` | 记录标题 |
| `language` | `zh` / `en` / `auto` |

响应：

```json
{
  "id": "rec_xxx",
  "status": "ready",
  "text": "完整转写文本",
  "segments": [{ "startMs": 0, "endMs": 3200, "text": "第一句…" }],
  "summaryStatus": "queued"
}
```

也支持 `multipart/form-data` 上传（字段名 `file`）。

### 分片推送（适合设备端边录边传）

```bash
# 1. 创建会话
curl -X POST http://localhost:8787/api/open/recordings \
  -H "X-API-Key: rk_xxx" -H "Content-Type: application/json" \
  -d '{"title":"录音笔 A"}'
# → {"id":"rec_xxx"}

# 2. 推送分片（可重复，seq 递增）
curl -X POST "http://localhost:8787/api/open/recordings/rec_xxx/chunks?seq=0&durationMs=30000" \
  -H "X-API-Key: rk_xxx" -H "Content-Type: audio/webm" \
  --data-binary @chunk0.webm

# 3. 结束并触发后续处理
curl -X POST http://localhost:8787/api/open/recordings/rec_xxx/finish \
  -H "X-API-Key: rk_xxx"

# 4. 查询状态与全文
curl http://localhost:8787/api/open/recordings/rec_xxx -H "X-API-Key: rk_xxx"
```

## 硬件设备接入（ESP32-C3 / 录音笔）

> 现成固件在 [`esp32/`](esp32/README.md)：长按录音、VAD 跳过静音、Opus 12kbps、先存本地 Flash、
> 再按需上传，支持有屏（SSD1306 / ST7735）和无屏（LED + 蜂鸣器）两种形态，S3 / C3 都能编。
> 联网既能在 menuconfig 里写死，也能现场配网（设备开 `Recall-XXXXXX` 热点，手机开 `192.168.4.1` 填 WiFi）。
> 下面这一节是协议说明和最小示例，想自己接别的主控看这里就够。

单片机没有键盘，输不了账号密码，所以绑定不靠登录，只靠**设备开机时的一串 6 位数字编号**：

```
① 开机播报 / 显示编号（MAC 换算，永远不变）  例：903471
        ↓  后台「设置 → 硬件设备 → 添加设备」，输入这串数字
② 服务端给这台设备生成专属 token
        ↓  设备轮询 /api/open/pair，几秒内自动取回，存进 NVS
③ 之后每段音频只带 X-Device-Token 推流，服务端自己归并成一次会话
```

绑定完成后设备不需要知道任何账号信息；换服务器、换账号只要重新绑定，或后台点一下「换发 token」。

### ① 设备侧：MAC → 6 位编号

**纯数字**是刻意的选择：没有屏幕的录音笔可以直接语音播报「九 零 三 四 七 一」，
人一听就能输，不用分辨字母和大小写。

```cpp
uint64_t mac = ESP.getEfuseMac();              // ESP32-C3 出厂 MAC
uint32_t x   = (uint32_t)(mac & 0xFFFFFFFFULL);
uint32_t code = (x * 2654435761u) % 1000000u;  // 打散后取 6 位，避免相邻 MAC 编号相连
char buf[7];
snprintf(buf, sizeof(buf), "%06u", code);      // 例：903471
```

- 编号只含 `0-9`，口述、按键、TTS 播报都不容易错；后台也做了归一化：
  `903-471`、`903 471`、`九零三四七一`、把 `0` 打成 `O`、`1` 打成 `I/L` 都能认出同一个设备。
- 同一块板子编号永远一样，刷固件、换服务器都不用改。
- 6 位数字共 100 万种组合。绑定是「设备正在播报编号 + 后台正在输入编号」的同一时间窗内的即时动作，
  家用/小批量基本碰不到撞号；万一两台设备真的算出同一个数字，先绑定的那台生效，
  删掉旧设备再给新设备绑一次即可。

### ② 后台：输入编号完成绑定

「设置 → 硬件设备 → 添加设备」，把听到/看到的 6 位数字敲进去（名称可留空，会沿用设备自报的名字），确定即可。

- 设备已经开机在轮询时，区块顶部会出现蓝条「N 台设备正在等待配对」，直接点「绑定」更快。
- 也可以**先绑后上电**（批量备货场景）：设备上线后第一次轮询就能取到 token。
- 编号已被别的账号绑过会返回 409「该设备已绑定到其他账号」，需要原账号先删掉那台设备。
- 设备行里能看到编号（`903-471`）、token 掩码、是否正在录音，以及「间隔 / 静默」两个阈值，按设备单独调。

### ③ 设备侧：取回 token 并推流

未绑定时设备每 3 秒轮询一次；后台一点「绑定」，下一次轮询就拿到 token：

```bash
curl "http://192.168.1.10:8787/api/open/pair?code=903471&name=c3-01&firmware=1.0.3"

# 后台还没绑定
{ "status": "pending", "code": "903471", "pollAfterMs": 3000 }

# 已绑定：轮询返回 token（设备存一次即可；换发 token 后重新轮询就能拿到新的）
{ "status": "bound", "token": "dv_xxx", "deviceId": "dev_xxx",
  "name": "客厅录音盒", "gapSec": 90, "idleSec": 180 }
```

拿到 token 存进 NVS，之后推音频只带它，不再需要账号级密钥：

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

token 无效（被换发过、设备被删过）会返回 401「设备 token 无效，请重新配对」——固件里收到 401
就清掉 NVS 里的 token、回到轮询状态，下一次后台绑定后自动恢复。

### 归并规则

服务端用**到达时间**判断会话边界（设备时钟不准也没关系）：

```
23:00:00  段1 ─┐
23:00:10  段2  ├─ 间隔 10s  < 阈值 → 同一次会话（一条记录，一份纪要）
23:00:20  段3 ─┘
                    ← 静默 5 分钟，超过「间隔」阈值
23:05:20  段4 ─── 新会话 → 新建一条记录
                    ← 之后再没有任何音频，静默超过「空闲」阈值
              ✅ 自动收尾：转写完成 → 生成这一整段聊天的纪要
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `device` | `default` | 设备名，同一账号下唯一。**用 token 鉴权时可省略**，以 token 对应的那台设备为准 |
| `gapSec` | 90 | 相邻两段间隔超过多少秒就断开成新记录 |
| `idleSec` | 180 | 静默多少秒后自动收尾（触发纪要生成） |
| `durationMs` | 自动探测 | 本段音频时长；不传则服务端用 ffprobe 探测 |
| `title` | 自动 | 新会话的标题 |
| `recordedAt` | 服务端时间 | 设备端录制时间（ISO），时钟准的话可以带上 |

`gapSec` / `idleSec` 可以在后台每台设备单独设置，请求里带同名参数可临时覆盖。

### 接口

**配对（不需要任何密钥，靠设备编号本身）**

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET`/`POST` | `/api/open/pair?code=903471` | 设备轮询：`pending` 表示还没绑，`bound` 时下发 token；限每 IP 60 次/分钟 |
| `GET` | `/api/devices/claims` | 后台用（JWT）：列出正在轮询、还没被认领的编号 |
| `POST` | `/api/devices/bind` | 后台用（JWT）：`{code, name?}` 完成绑定并生成 token |
| `POST` | `/api/devices/{id}/reset-token` | 后台用（JWT）：换发 token，旧 token 立刻失效 |

**推流（设备 token 或账号级 API Key 二选一）**

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/open/ingest` | 推一段音频，返回当前是第几片、是否新会话 |
| `GET` | `/api/open/ingest` | 设备探活 + 查询当前会话状态（启动自检用） |
| `POST` | `/api/open/ingest/close` | 设备休眠/关机前主动收尾，不用等超时 |

鉴权写法：

```
X-Device-Token: dv_xxx          ← 绑定后的设备（推荐）
Authorization: Bearer dv_xxx

X-API-Key: rk_xxx               ← 账号级密钥，适合脚本 / 未绑定的临时设备
Authorization: Bearer rk_xxx
?key=rk_xxx
```

音频格式可以是 `audio/wav`、`audio/webm`、`audio/mpeg`，也可以直接推**裸 PCM**
（`audio/l16`、`audio/pcm`、`audio/raw`，默认 16kHz 单声道，可用 `rate`/`channels` 调整）——
服务端会自动补上 WAV 头再送去识别。

### ESP32-C3 示例（Arduino）

开机先按 MAC 算出编号，有 token 就直接用，没有就轮询等后台绑定；采到 10 秒 PCM 推一段。

```cpp
#include <WiFi.h>
#include <HTTPClient.h>
#include <Preferences.h>

const char* SERVER = "http://192.168.1.10:8787";   // 电脑的局域网 IP
Preferences nvs;
String g_token;

// ① MAC → 6 位数字编号
String deviceCode() {
  uint64_t mac = ESP.getEfuseMac();
  uint32_t x = (uint32_t)(mac & 0xFFFFFFFFULL);
  uint32_t code = (x * 2654435761u) % 1000000u;
  char buf[7];
  snprintf(buf, sizeof(buf), "%06u", code);
  return String(buf);
}

// ①' 没有屏幕的设备：开机把 6 位数字逐个播报，比连读成「九十万三千…」清楚
void speakCode() {
  String c = deviceCode();
  for (int i = 0; i < 6; i++) {
    tts_speak_number(c[i] - '0');   // 换成你所用语音模块的播报函数
    delay(450);
  }
}

// ② 轮询取 token（3 秒一次，直到绑上）
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
    Serial.printf("[pair] 等待后台绑定，编号 %s\n", deviceCode().c_str());
    return false;
  }
  int s = i + 9, e = body.indexOf('"', s);
  g_token = body.substring(s, e);
  nvs.putString("token", g_token);
  Serial.println("[pair] 已绑定，token 已存入 NVS");
  return true;
}

// ③ 推流（只带 token）
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

  if (code == 401) {                    // token 被换发/设备被删 → 清掉重新配
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
  while (!g_token.length() && !pairOnce()) delay(3000);   // 等后台输入编号
}
```

几点实践建议：

- **按 10~30 秒一段推**。段太大：失败重传代价高、纪要来得晚；段太小：请求太密集。
- 设备关机前调一次 `POST /api/open/ingest/close`，纪要不用等 3 分钟超时。
- 断网时把 PCM 存在 PSRAM/Flash 里，恢复后按顺序补推即可——服务端按到达时间归并，
  只要补推间隔小于 `gapSec`，仍然会归到同一次会话里。
- 设备只要内网能访问到电脑即可，不需要公网 IP、不需要 HTTPS（ESP32-C3 跑 TLS 很吃内存）。
- 编号印在机身/包装上更省事：同一串数字永远不会变，掉了重新输一遍就能再绑。
  设备被删或 token 泄露时，后台点「换发 token」，老 token 立即失效，设备自动重新配对。
- **没有屏幕的设备**：开机用语音逐个数字播报（"九、零、三、四、七、一"），配一个确认音提示播完；
  后台输入支持中文数字，听到什么输什么即可。
- 编号相当于这台设备的「配对凭证」：知道编号的人可以取到 token，所以别把编号写进对外页面；
  服务本身是内网部署（默认只监听局域网），这个信任边界是够用的。

## 手机访问与录音权限

浏览器只在 **HTTPS 或 localhost** 下允许网页调用麦克风。手机通过局域网 IP（`http://192.168.x.x:8787`）访问时，录音按钮会提示不可用，此时可以：

1. 用「上传音频文件」——从手机文件/录音机里选已有的录音，功能完全可用
2. 或者给服务配一个 HTTPS 入口（Caddy / Nginx + 自签证书 / 内网穿透均可）
3. iOS 可在 Safari 中「添加到主屏幕」，获得接近原生 App 的体验

## 数据与备份

| 路径 | 内容 |
| --- | --- |
| `data/recall.db` | 内置 SQLite 数据库（用户、录音元信息、转写、纪要、任务）；切到 PostgreSQL 后这份文件不再更新 |
| `data/config.json` | 数据库连接配置（切换 SQLite / PostgreSQL 时写入） |
| `data/backups/` | 自动快照，每次启动成功后留一份，保留最近 5 份 |
| `data/uploads/<录音ID>/` | 音频文件，按分片存放，与数据库选型无关 |
| `data/models/` | 本地开源 ASR 模型（只有选了本地引擎才会有），删掉不影响已有录音 |

备份：用 SQLite 时复制整个 `data/` 目录即可；用 PostgreSQL 时用 `pg_dump` 导出数据库，
再加上 `data/uploads/` 的音频。删除某条记录时，对应的音频目录会一起清理。
（用 `docker-compose.postgres.yml` 起库时，数据在 `./pg-data/`。）

### 内置 SQLite 的自我保护

SQLite 有个硬前提：**同一个库文件只能有一个进程写**。两个后端进程（或一边跑服务、
一边用脚本改同一个库）同时打开，`-wal` / `-shm` 状态会对不上，症状是先刷
`disk I/O error`，接着变成 `database disk image is malformed`，等于把库写废。
所以后端启动时会做三件事（只对 SQLite 生效，PostgreSQL 不需要）：

1. **单实例锁**：拿不到 `data/.sqlite.lock` 就直接退出，并打印占用者的 PID。
   进程正常退出时锁自动释放，不会留死文件。也意味着 SQLite 模式下不要开多 worker。
2. **启动体检**：跑一次 `PRAGMA quick_check`，不通过就中止启动并给出恢复指引，
   而不是带着坏库继续写。运行期真碰上 malformed，接口会返回
   `code: db_corrupted` 并明确提示怎么恢复。
3. **自动快照**：体检通过、迁移完成后用 `VACUUM INTO` 导出一份到
   `data/backups/recall-<时间戳>.db`，保留最近 5 份。真空导出是原子的，不会拷到半个 WAL 状态。

恢复步骤（库真的坏了）：

```bash
# 1. 先停服务，避免边恢复边写
lsof -nP -i :8787            # 找到 PID 后 kill

# 2. 用最近一份快照覆盖，并清掉残留的 -wal / -shm
cp data/backups/recall-20261030-140921.db data/recall.db
rm -f data/recall.db-wal data/recall.db-shm

# 3. 想抢救快照之后新增的数据，先用 .recover 导出成 SQL 再重建
sqlite3 坏库文件 ".recover" > dump.sql
sqlite3 新库文件 < dump.sql
```

## 目录结构

```
.
├── server-py/                    FastAPI 后端
│   ├── app/
│   │   ├── main.py              入口：路由注册、静态托管、worker 启动、异常处理
│   │   ├── config.py            配置与路径
│   │   ├── db.py                数据访问门面（all_/one/execute），支持后端热切换 + 配置自动跟随
│   │   ├── db_config.py         data/config.json 读写与 DATABASE_URL 解析
│   │   ├── db_migrate.py        Alembic 入口：启动时 upgrade head
│   │   ├── db_health.py         SQLite 三件套：单实例锁、启动体检、自动快照
│   │   ├── database/            SQLite / PostgreSQL 后端（PG 带连接池）、占位符转换、语句切分
│   │   ├── migrations/          版本化 DDL（versions/0001_initial.py 起）
│   │   ├── tools/               SQLite → PostgreSQL 数据搬家脚本
│   │   ├── security.py          bcrypt + JWT 鉴权依赖
│   │   ├── jobs.py              任务队列与 worker
│   │   ├── audio.py             ffmpeg / ffprobe 封装
│   │   ├── bus.py               进程内事件总线（SSE 用）
│   │   ├── providers/           ASR / LLM 适配层（云端 + 本地开源，可扩展）
│   │   ├── services/            转写、纪要、整理提示词、服务商解析、本地模型仓库、设备配对与会话归并、用户偏好
│   │   └── routes/              HTTP 接口（auth / setup / database / recordings / summaries / providers / devices / open）
│   ├── pyproject.toml           依赖声明（uv）
│   └── .env.example             环境变量模板
├── web/
│   └── src/
│       ├── pages/               登录 / 首页 / 录音 / 详情 / 回忆 / 设置
│       ├── components/          布局、波形、思维导图、Markdown 渲染
│       └── lib/                 API 客户端、鉴权、提示
├── data/                        运行时数据（自动创建）
├── legacy/nodejs-server/        已归档的 Node.js 版本后端
└── scripts/dev.sh               一键开发启动
```

## 扩展新的 ASR 服务商

在 `server-py/app/providers/asr.py` 里继承 `AsrEngine`：

```python
class MyAsr(AsrEngine):
    def transcribe(self, file_path, mime=None, language=None, duration_ms=None) -> AsrResult:
        return {
            "text": "...",
            "segments": [{"startMs": 0, "endMs": 1000, "text": "..."}],
        }
```

然后在 `create_asr_engine` 的分支里注册，并把类型加到 `ASR_KINDS`，设置页会自动出现该选项。
`llm.py` 的 `LlmEngine.chat()` 同理。

本地引擎有额外约定：`ASR_KINDS` 里给条目加
`localBackend: "sherpa-onnx" | "faster-whisper" | "whisper-cpp"` 和 `requiresKey/requiresBaseUrl: false`，
前端就会隐藏 Key / URL 输入框、改成本地模型选择器。
模型文件与下载逻辑都在 `services/local_asr_models.py`，新增 Whisper 尺寸往 `FASTER_WHISPER_SIZES` /
`WHISPER_CPP_SIZES` 加一行；新增 sherpa-onnx 模型（FireRedASR、Fun-ASR-Nano 等预转换包）往
`SHERPA_MODELS` 加一行「id / 显示名 / 体积 / 说明 / 压缩包名」即可，下载解压全自动。

## 已知限制

- 单用户/小团队自用设计，未做多租户隔离与配额限制
- 录音分片上传过程中关闭页面会丢失最后一段（已上传的分片不受影响）
- 长文本纪要使用「分段提炼 + 汇总」策略，极长录音（> 3 小时）质量会略有下降
- 未接入实时说话人分离（取决于 ASR 服务商能力；本地引擎暂不支持）
- 本地 ASR 首次要下载几百 MB 模型，下载源慢时请耐心等或自行放好模型文件
- 本地 ASR 在纯 CPU 上跑 `medium` 以上会明显慢于实时，长录音建议 `small` / `turbo` 或上 GPU
- 网页上的数据库切换不迁移数据：需要保留旧数据时用 `app.tools.migrate_sqlite_to_pg` 手动搬一次
- 内置 SQLite 只适合调试 / 单机体验：不支持多进程或多副本同时写（后端会用锁文件拦住第二个实例），
  也不适合放在网络盘上；要正式长期用，建议切到外置 PostgreSQL
