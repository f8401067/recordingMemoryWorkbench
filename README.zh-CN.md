# 忆录 · Recall Workbench

本地部署的录音记忆库。网页直接录音 → 自动切片上传 → 语音识别转文字 → AI 生成纪要与思维导图 → 在「回忆」里按日 / 周 / 年回看每一天。

数据（音频、文字、纪要）全部保存在你自己机器的 `data/` 目录里，AI 服务商可自由更换。

## 功能

- **网页录音**：浏览器直接录，每 N 秒自动切一片上传并转写，录音没停文字就开始出
- **长音频上传**：拖一个 1 小时的录音文件进去，后端用 ffmpeg 自动切片并排队转写
- **AI 纪要**：自动提炼概要、关键内容、结论、待办，并生成可交互的思维导图；支持手动编辑，也支持「AI 修改」——弹框提一句要求，AI 按要求修订这一篇
- **回忆回看**：日 / 周 / 年三种视角，打开就看到已经整理好的回忆；某一段还没有整理时会自动开始总结并显示等待状态
- **多服务商**：ASR 与 LLM 都可配置多个、随时切换、一键测连通性；语言模型内置国内各家大模型预设，选中即带出地址、可一键拉取真实模型名
- **本地 ASR 可选**：不联网也能转写——Qwen3-ASR / SenseVoice / Paraformer（sherpa-onnx）、`faster-whisper`、`whisper.cpp`，模型在设置页一键下载
- **本地说话人分离**：转写完成后自动区分「说话人 1/2/…」，pyannote 分割 + 3D-Speaker 声纹嵌入，纯 CPU 本地推理，音频不出本机
- **开放 API**：给脚本、录音笔或其他设备一个接口，推音频即可拿文字
- **MCP 工具**：接入自己的 MCP 服务，对话时可用其工具，也能把找寻得到的总结自动/手动推送出去
- **MCP 服务（对外）**：把忆录自己开放成一台带 Token 的 MCP 服务器，AI 客户端可上传录音、按日期读 AI 纪要、检索回忆，后台能看到客户端与调用记录
- **硬件免登录配对**：设备开机播报/显示 6 位数字编号（MAC 换算、固定不变），后台输一次就完成绑定并下发专属 token
- **移动端适配**：手机浏览器打开就是原生 App 的体验，支持加到主屏

## 技术栈

| 层 | 选型 |
| --- | --- |
| 后端 | Python 3.11+ + FastAPI + uvicorn（依赖用 [uv](https://github.com/astral-sh/uv) 管理） |
| 数据库 | PostgreSQL（只支持这一种；连接串写在 `server-py/.env`，连接池 + Alembic 迁移） |
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

### 一键启动（macOS，推荐日常使用）

双击项目根目录的 **`启动 Recall.command`**，或用命令行 `bash scripts/start.sh`。脚本会：

- 首次运行自动装依赖；前端源码比 `web/dist` 新时自动重新构建
- 检查 PostgreSQL 是否可连，连不上会尝试启动映射了该端口的 Docker 容器
- 启动后端并自动打开 <http://localhost:8787>（后端同时托管前端页面，**不需要**再单独起 vite）
- 已在运行时再次双击，只打开浏览器，不会重复启动
- **关闭终端窗口或按 Ctrl+C 即停止服务**

日志写到 `data/logs/server.log`。

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

## 数据库：PostgreSQL

只支持 PostgreSQL。连接信息**只写在配置文件里**，网页上不再提供修改入口，改完重启后端生效。

本机直跑改 `server-py/.env`：

```bash
DATABASE_URL=postgresql://postgres:secret@127.0.0.1:5432/recall?sslmode=disable
```

Docker 部署改 `docker-compose.yml` 里 recall 服务的 `DATABASE_URL`。默认连 compose 里的
`postgres` 服务（主机名就是服务名）；想连宿主机上已有的库，用 `host.docker.internal`：

```yaml
DATABASE_URL: postgresql://postgres:secret@host.docker.internal:5432/recall?sslmode=disable
```

连接串支持的查询参数：

| 参数 | 说明 |
| --- | --- |
| `sslmode` | `disable` / `prefer` / `require` / `verify-full`，默认 `prefer` |
| `schema` | 表放在哪个 schema，默认 `public`，不存在会自动创建 |
| `pool_size` | 连接池上限，默认 10 |

启动时会自动完成三件事：库不存在就建库（需要建库权限）→ 跑 Alembic 迁移建表/补列 →
库里没有账号就用 `ADMIN_USER` / `ADMIN_PASSWORD` 建第一个管理员（这两个变量也在配置文件里）。

数据库连不上时服务仍然会启动，`/api/health` 显示 `connected: false`，
打开 `/setup` 会显示当前连接串（不含密码）以及该改哪个文件、改哪一行。

一个最小可用的 PostgreSQL：

```bash
docker run -d --name recall-pg \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=recall -p 5432:5432 postgres:16
```

compose 自带一个 `postgres` 服务，`docker compose up -d --build` 会一起起来，数据在 `./pg-data/`。

### 结构演进与连接池

- 表结构用 **Alembic** 版本化管理（`server-py/app/migrations/versions/`），启动时自动
  `upgrade head`；老库初次升级只会补缺的列和索引，不会动数据。
- 走 **psycopg 连接池**（懒创建，默认最多 10 条连接），关服时旧池在后台线程里优雅关闭，
  不阻塞请求。
- 改配置文件（`DATABASE_URL`）后**需要重启进程**：没有运行时切库了。

新增一张表 / 改字段：

```bash
cd server-py
.venv/bin/alembic -c alembic.ini revision -m "add xxx"   # 生成空迁移，然后手写 SQL
```

> 旧版本的 `data/recall.db`（SQLite）已不再支持，也不会再被读取。需要里面的数据请自行导出后
> 用 SQL 导入 PostgreSQL，或直接删除。

## 配置 AI 服务商

进入「设置」，分别配置**语音识别**和**语言模型**，各建一个默认项即可。

### 语音识别（ASR）

| 类型 | 说明 |
| --- | --- |
| 本地 · sherpa-onnx（Qwen3-ASR / SenseVoice） | **中文首选**。纯 ONNX、不需要 PyTorch，Qwen3-ASR 中文最准、SenseVoice 最快，模型设置页一键下载 |
| 本地 · MLX Qwen3-ASR 1.7B | **Apple Silicon 专属**。`mlx-community` 的 6bit 量化权重走 Metal，中文准确率比 0.6B 高一档 |
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

Apple Silicon 上还可以选 **MLX Qwen3-ASR 1.7B（6bit）**（约 1.9GB）：

| 模型 | 体积 | 特点 |
| --- | --- | --- |
| Qwen3-ASR 1.7B · 6bit | ~1.9GB | Metal 加速，中文准确率比 0.6B 高一档；常驻内存约 3.5GB，速度慢约 2 倍 |

MLX 只支持 M 系列芯片；Intel Mac、Linux、容器里会提示不可用。权重来自
`mlx-community/Qwen3-ASR-1.7B-6bit`，设置页一键下载，**默认从 ModelScope 拉**
（国内直连，逐文件断点续传），ModelScope 不可用时依次回退 `hf-mirror.com`、huggingface.co。

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

然后在「设置 → 语音识别服务 → 添加」，选「本地 · sherpa-onnx」「本地 · MLX Qwen3-ASR」
「本地 · faster-whisper」或「本地 · whisper.cpp」，在模型列表里点「下载」，下完选中保存并设为默认即可。
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
  `faster-whisper/<size>/`、`mlx-qwen3-asr/<repo 名>/`），删目录即可回收空间，设置页也能单个删除。
- **说话人分离**用一套独立的本地模型：`data/models/speaker-diarization/`（pyannote 分割约 6MB +
  3D-Speaker 声纹嵌入约 38MB）。在「设置 → 录音与纪要 → 说话人分离」打开开关并点「下载」后，
  新完成的录音会自动标注「说话人 1/2/…」；纯 CPU 推理，音频不出本机。开启之前已转录完成的录音不会自动补跑。
- **sherpa-onnx 的模型包从 GitHub Releases 拉**（每个 158MB~838MB）。国内直连慢或断流时设
  `SHERPA_GH_PROXY=https://gh-proxy.com` 换代理源，默认会在直连失败后自动回退 `gh-proxy.com`。
- Whisper 模型默认走 `hf-mirror.com`（huggingface.co 在国内基本连不上）。有外网直连时设
  `HF_DOWNLOAD_SOURCE=hf`；想换成自建镜像设 `HF_ENDPOINT=https://your-mirror`。
- MLX 模型不走 `huggingface_hub`，直接从 **ModelScope** 的仓库接口按文件下载，失败才回退
  `hf-mirror.com` / huggingface.co；设了 `HF_ENDPOINT` 则自建镜像优先。
- 长音频按 25 秒切块，切点挑能量最低的静音处（不是硬切），避免把一个词劈成两半；
  块内再按标点切句，输出句级时间戳。
- 本地推理默认串行（`LOCAL_ASR_CONCURRENCY=1`），避免多片并发把 CPU 和内存打满；
  机器够强可以调大，但要留意 `ASR_CONCURRENCY` 也别开太高。Qwen3-ASR 0.6B 常驻约 1GB 内存。
- 中文会自动带上「请使用简体中文」的引导提示词，否则 Whisper 系模型经常输出繁体。
- 模型对象在进程内缓存（最多 2 份），换模型才会重新加载，不影响连续录音的转写速度。
- 本地引擎自身不做说话人分离；说话人标注由独立的本地模型完成（见下），与 ASR 服务商无关。

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
（`title / summary / markdown / mindmap / highlights / todos / moods`），所以思维导图与高亮在任何风格下都能正常渲染。
超过 16000 字的录音会先分段提炼，再汇总，分段提示词同样带上风格。

除此之外还有五套**按范围自动启用**的回忆风格，不出现在上面的风格切换里：

| 范围 | 风格 | 章节 |
| --- | --- | --- |
| 单日（日报） | 每日回忆 | 今天都聊了什么 / 冒出来的想法 / 待办事项 / 人物与心情 |
| 一周（周报） | 每周回忆 | 这周在忙什么 / 值得记住的片段 / 待办事项 / 人物与心情 |
| 整月（月报） | 每月回忆 | 这个月在忙什么 / 几件重要的事 / 一直挂着的待办 / 人物与心情 |
| 整年（年报） | 年度回忆 | 这一年的主线 / 几个重要阶段 / 还没做完的事 / 人物与心情 |
| 任意区间 | 时段回忆 | 这段时间在忙什么 / 值得记住的片段 / 待办事项 / 人物与心情 |

范围由日期区间判断（`services/prompts.py::period_of`）：周一到周日算周报，1 号到月末算月报，
1/1 到 12/31 算年报，其余按任意区间处理。回忆风格明确禁止会议措辞（不出现「会议信息 / 议题 / 决议 / 与会者」），
侧重写清楚跟谁聊了什么、有哪些待办、当事人的心情如何；心情（`moods`）必须能追溯到原文语气或原话，
判断不了就写「未表露」。早期生成的日报默认是会议风格，重新生成一次即可切到回忆风格。

### 层级式汇总：周 / 月 / 年不再重扫原文

各级回忆不是各自把整段时间的转写重新读一遍，而是自底向上复用：

| 层级 | 输入 | 典型输入量 |
| --- | --- | --- |
| 单条录音 / 日报 | 该录音或当天的转写原文（带时间戳） | 当天原文 |
| 周报 / 月报 / 任意时段 | 区间内各天的**日报 markdown** | 7 / 30 份，约 1–4 万字 |
| 年报 | 该年 12 份**月报 markdown** | 12 份，约 1–2 万字 |

跨度超过一年的区间不走层级，回退到读转写原文。实现在 `server-py/app/services/summarize.py`：

- `_collect_children()` 用两次查询盘点下级：`ready` 直接用，`failed` 跳过，没有录音的窗口跳过，其余记成 `pending`。
- 缺下级时父纪要进入 `waiting`，后台轮询任务（payload 里的 `checkChildren`）幂等地把缺的日报 / 月报排队生成；月报自己还会按天级联，形成「年报 → 12 月报 → 各天日报」两级瀑布。
- 下级结算完（或等满 60 分钟）`_kick_parent()` 把父任务放回正常队列。轮询只查库、不调模型。
- `_context_from_children()` 拼输入，提示词里明确「这些回忆就是本次的原文依据」，禁止逐天复述，要求合并主线与转折；`build_system_prompt()` / `build_chunk_prompt()` 也会按输入类型切换措辞。
- 结果 `stats` 里带 `childSummaries`（用了几份下级）与 `childMissing`（跳过几份）。

**收益与代价**：

- 首次生成年报仍要一次性补齐缺的日报 / 月报，任务数多、需要等一会儿；但每个任务输入都很小，不会像以前那样把全年转写塞进一次调用导致超长或截断。
- 之后重新整理周 / 月 / 年只读十几份已生成的文本，耗时与 token 大幅下降。日常打开日历日视图就会顺手把日报建好。
- 层级是有损压缩：下级回忆没写进去的细节，上级看不到。想让年报保留更多细节，先把日报 / 月报的自定义提示词调细。
- **重新整理父级只汇总当前已有的下级，不会级联重算下级**。改了日报提示词或重新生成了某天日报后，需要按「日报 → 月报 → 年报」的顺序各自点一次重新整理，改动才会传到最上层。

「设置 → 提示词」里可以给**每个范围分别**填自定义要求（最多 2000 字/条）：

| 标签页里的卡片 | 字段 | 作用范围 |
| --- | --- | --- |
| 单条录音 | `summaryPrompt` | 单条录音的纪要（几套风格共用） |
| 日报 | `promptDay` | 单日回忆 |
| 周报 | `promptWeek` | 周一到周日 |
| 月报 | `promptMonth` | 整月 |
| 年报 | `promptYear` | 整年 |
| 任意时段 | `promptRange` | 非整周/整月/整年的自定义区间 |

填写的内容作为「用户自定义要求」优先于风格描述，但硬性规则与 JSON 契约仍会保留——
所以即使写「用口语化语气，每条结论标注发言人」也不会把输出变成纯文本。留空即用内置提示词。
已经生成的回忆需要点「重新整理」才会用上新提示词。

- 风格跟着纪要走：`summaries.template` 会记录生成时用的风格，重新生成时沿用，不会莫名其妙变回默认值。
- 提示词实现在 `server-py/app/services/prompts.py`，想改措辞直接编辑 `STYLES` 里对应风格的 `focus / outline / tips`。

### 手动修改与 AI 修改

每份纪要（录音详情页的「AI 纪要」页签、纪要详情页）都支持两种改法：

- **手动修改**：点「编辑」，直接改标题和 Markdown 原文，⌘S 保存。只动这份纪要的文字，不重新调用模型；点「重新生成」会覆盖手工修改。
- **AI 修改**：点「AI 修改」，弹出对话框用一句话提要求（如「把标题改成疑问句」「把待办删到只剩今天能做的」），AI 会基于这份纪要和原始录音材料按要求修订，没提到的部分保持原样。提交后页面自动轮询，改完即刷新。

AI 修改与初次生成共用后台任务队列：`POST /api/summaries/{id}/revise` 入队 `revise_summary` 任务，
修订提示词在 `server-py/app/services/prompts.py` 的 `build_revise_system_prompt`，处理逻辑在
`services/summarize.py` 的 `handle_revise_job`（单条录音读转写原文、日/周/月/年回忆读下级纪要，
长材料走分段提炼）。纪要正在生成中时不能再提修改，避免并发互相覆盖。

## 设置页

设置拆成了几个标签（地址栏带 `?tab=`，可直接收藏某个标签）：

| 标签 | 内容 |
| --- | --- |
| AI 服务 | 语音识别 / 语言模型服务商，默认项、连通性测试 |
| MCP 工具 | 接入外部 MCP 服务（HTTP / SSE），列工具、调试调用、总结推送、AI 纪要一键发送，可手动指定/取消可发纪要的工具 |
| 录音与纪要 | 切片时长、自动纪要、纪要风格、识别语言、界面主题 |
| 提示词 | 单条录音、日报、周报、月报、年报、任意时段分别附加的要求 |
| 任务 | 正在运行 / 排队 / 最近结束的后台任务，含耗时、心跳、失败重跑 |
| 日志 | 后端实时日志（print、异常栈、任务事件、uvicorn），支持级别筛选、搜索、复制、下载、清空 |
| 数据与设备 | 开放 API 密钥、对外 MCP 服务（Token / 工具 / 调用记录）、硬件设备绑定与会话参数 |
| 账号 | 修改密码、退出登录 |

设置页顶部常驻一个「N 个任务在跑 / N 个任务排队」的小徽标，点一下跳到任务标签。

任务与日志的接口在 `server-py/app/routes/system.py`：

- `GET /api/system/tasks`：运行中 / 排队 / 最近结束，payload 会翻译成人话（哪条录音第几片、哪段时间的回忆）
- `POST /api/system/tasks/{id}/retry`：把失败任务重新排队
- `GET /api/system/logs?level=&q=&after=`：日志增量拉取（`after` 传上一条的 id）
- `DELETE /api/system/logs`：清空内存缓冲

日志只存在内存里（最近 4000 条，进程重启即清空），不写文件，避免无限增长和把用户内容落盘；
需要长期留存就在页面里点「下载」。实现见 `server-py/app/logbuf.py`：劫持 `sys.stdout/stderr` 按行入库，
任务开始/完成/失败/重排、整理的模型与字数等结构化事件由 `logbuf.log()` 直接写入。

## MCP 工具

每个用户可以接入自己的 [MCP](https://modelcontextprotocol.io) 服务，工具在「找寻」对话里可用，
也能把最终的总结文字推送到外部（笔记、待办、消息机器人等）。

**只支持远程 HTTP / SSE**，不执行本地命令 —— 配置来自网页表单，不应该有能力在服务器上起进程。

### 添加服务

两种方式，都在「设置 → MCP 工具」：

1. **手动填写**：名称、地址（`https://example.com/mcp`）、传输方式（自动 / Streamable HTTP / SSE）、请求头 JSON
2. **粘贴 JSON**，兼容 Claude Desktop / Cursor 的写法：

```json
{
  "mcpServers": {
    "我的笔记": {
      "url": "https://example.com/mcp",
      "headers": { "Authorization": "Bearer sk-..." }
    }
  }
}
```

`command` 形式的本地服务会被跳过并提示原因。保存时会立刻连一次，连上就把工具清单落库，
所以在列表里直接能看到每个工具的名称、说明和参数。请求头保存后只显示掩码，编辑时不动就保持原值。

**添加 / 刷新时会自动识别「能接收 AI 纪要」的工具**：根据 `inputSchema` 判断有没有文本入参、
是不是写入类工具（纯查询工具会排除），并推断正文 / 标题 / 日期分别填到哪个字段。
判定结果缓存在 `mcp_tools.capability`，设置页会打上「可发送纪要」标记并显示正文字段名。

**添加 / 刷新时还会自动生成建议参数**：大模型只看着工具的名称、说明和 schema
（不看纪要内容、不看历史调用），给每个工具生成一份合法入参，存进 `mcp_tools.suggested_args`。
调试面板选中工具时直接预填这份建议参数；没生成成功时退回「必填字段兜底」（按类型给默认值）。
建议参数只是默认值，人工改过之后：

- **调试面板**：改完点「存为模板」写回数据库（接口 `PATCH /api/mcp/servers/{id}/tools/{tool}/args`，
  键按 schema 白名单过滤、类型对齐）；「AI 生成」现场让大模型重新生成一份（`POST .../suggest-args`，
  默认引擎无响应时自动换备用模型并提示）；「重置为模板」回到骨架值。
- **纪要一键发送**：点工具后先弹「发送前确认」，展示「建议参数 + 纪要内容」合并后的参数
  （`POST /api/mcp/run-tool-preview`，不真正调用），正文进识别出的正文字段；
  用户可编辑、可「AI 重新生成」，确认后发送（`POST /api/mcp/run-tool` 带 `args`）。
  合并优先级：**用户改的键 > 建议参数 > 纪要语义补全**（标题 / 日期字段不会重复塞正文）。
- 刷新服务不会覆盖人工存过的建议参数（`_save_tools` 会按工具名快照旧值并恢复）。

**自动判定可以人工改**：展开工具列表后，每个工具右侧的「可发送纪要 / 设为可发送」按钮点一下即可切换——
不认同判定就自己指定一个，也可以把自动判定为可发送的工具取消掉。开启后还能用下拉框选纪要正文写进哪个参数
（只列字符串参数），并会显示「手动指定 / 手动排除」徽标；点「恢复自动判定」回到默认规则。

- 人工决策写成 `capability.manual: true`，刷新服务（`_save_tools` 会重建整张工具表）和判定规则版本升级都不会覆盖；
  只有「恢复自动判定」会清掉它。
- 服务端的工具 schema 变了、人工选的字段已不存在时，刷新会自动回退到当前推断值，不会让按钮点了报错。
- 接口：`PATCH /api/mcp/servers/{id}/tools/{tool}/capability`，body 传
  `{"sendSummary": true/false, "bodyField"?: "...", "titleField"?: "...", "dateField"?: "..."}`，
  或 `{"reset": true}` 恢复自动。返回更新后的完整服务对象。
- 这里改的是「可发送纪要」清单本身；「总结推送」里由大模型按工具说明挑工具是另一条链路，不受影响。

### 对话里怎么用

- 「对话中可用」打开的服务，其工具会以 `mcp__<服务>__<工具>` 的名字写进「找寻」的提示词，
  模型可以用它们把结果存出去；最多调用 2 次，不会挤掉检索轮次。
- 找寻页底部只展示**大模型按本次问答挑出来的工具**（内置检索 4 个 + 推荐出的 MCP 工具）：
  每次回答后把「问题 + 回答 + 全部工具的名称/说明/参数」交给模型，让它判断哪些现在真用得上、
  参数能自动填出、不是无关或破坏性操作，最多 6 个；一个都不合适就不列。
  模型输出按编号白名单校验，引擎无响应时自动换另一台已启用的模型重试，失败则宁可不推荐，
  不会退回把全部工具摆出来。想看全量清单可点「全部 N 个」展开。

### 总结推送（由大模型决策）

每台服务只设「不推送 / 手动发送 / 自动发送」，**不用填工具名和参数模板**：

| 模式 | 行为 |
| --- | --- |
| 自动发送 | 每次找寻得到答案且确实检索到录音时，让模型判断一次 |
| 手动发送 | 答案下方出现「发送总结到 XX」按钮，点了之后同样交给模型判断 |

调度器把「问题 + 总结 + 命中录音 + 全部工具的名称/说明/参数 schema」交给语言模型，
由它决定：发不发、发给哪台服务、调哪个工具、每个参数填什么。
输出会做白名单校验（服务与工具必须来自清单）、类型强制、必填字段补全；
纯查询类工具会被过滤；模型不可用或输出非法时，退回「挑一个能接收文本的工具自动填充」。

可以写一句**自然语言偏好**（例如「只把含待办或结论的内容保存，标签用 recall」），
模型会把它当规则参考。调试面板里仍可手动构造参数调用单个工具。

### AI 纪要一键发送

每条 AI 纪要（录音详情页、纪要详情页、回忆页的内联预览）下方都会列出「可发送纪要」清单里的工具
（自动判定 + 人工指定的都在，人工排除的不在），
**一个工具一个按钮**：点哪个就直接用哪个发送，不再让模型猜，也不用填任何参数。
点完后先弹「发送前确认」：展示大模型按 schema 生成的**建议参数**与纪要正文合并后的参数，
用户可以直接改（改过的键优先于建议值），确认后才真正调用。
正文由服务端按 `summaryId` 从数据库读取，按 schema 把整篇 Markdown 填进识别出的正文字段，
标题 / 日期等字段按语义补齐，调用结果和耗时显示在按钮下方，调用记录里标为「纪要」。
没配置 MCP 时只显示一条很轻的设置入口，不会占版面。这个按钮是手动点击，
不受上面「总结推送」的自动 / 手动开关影响。

### 接口

- `GET/POST /api/mcp/servers`、`PATCH/DELETE /api/mcp/servers/{id}`
- `POST /api/mcp/servers/import`：JSON 导入
- `POST /api/mcp/servers/{id}/refresh`：重新连接并刷新工具清单（返回协议版本、握手耗时、错误原因）
- `GET /api/mcp/servers/{id}/tools`、`POST /api/mcp/servers/{id}/tools/{tool}/call`：调试调用
- `PATCH /api/mcp/servers/{id}/tools/{tool}/capability`：手动指定/取消可发送纪要，或 `{"reset": true}` 恢复自动
- `POST /api/mcp/servers/{id}/tools/{tool}/suggest-args`：现场让大模型按 schema 生成一份建议参数
- `GET/PATCH /api/mcp/servers/{id}/tools/{tool}/args`：读 / 存工具的建议参数（人工改过的模板，刷新服务也保留）
- `POST /api/mcp/run-tool-preview`：`{summaryId, serverId, tool}` 返回合并后的参数预览（不真正调用）
- `POST /api/mcp/run-tool`：`{summaryId, serverId, tool, args?}` 一键发送，`args` 里用户改过的键优先
- `GET /api/mcp/tools`：默认返回对话可用工具；`?scope=summary` 返回识别出的可发送 AI 纪要工具
- `POST /api/mcp/run-tool`：`{summaryId, serverId, tool}` 一键把纪要发给指定工具
- `POST /api/find/push`：手动推送一段总结（同样由模型选工具）
- `GET /api/mcp/logs?serverId=&limit=`：最近调用记录（调试 / 对话 / 推送 / 纪要，保留最近 60 条）

实现：`server-py/app/services/mcp_client.py`（传输层，同步封装官方 SDK）、
`server-py/app/services/mcp_servers.py`（配置、工具缓存与能力识别）、
`server-py/app/services/mcp_dispatch.py`（大模型工具决策、参数校验、纪要一键调用）、
`server-py/app/services/mcp_arg_suggest.py`（按 schema 生成建议参数、人工模板读写、预生成）、
`server-py/app/services/mcp_recommend.py`（找寻页的工具推荐）、
`server-py/app/routes/mcp.py`、`web/src/components/SummaryMcpActions.tsx`（纪要下方的工具按钮）、
`web/src/pages/settings/McpTab.tsx`（调试面板的参数预填 / AI 生成 / 存为模板）。

## 使用流程

1. 首页点「开始录音」，或「上传音频」
2. 录音时每隔 N 秒（默认 30，可在设置里改）自动切片上传并转写
3. 结束后自动进入详情页，转写完成后按设置自动生成纪要
4. 在「AI 纪要 / 思维导图 / 全文」三个页签之间切换查看
5. 「回忆」里按日 / 周 / 月 / 年回看：日视图是月历 + 当天全部记录；周 / 月 / 年视图基于下级回忆逐层汇总（见「层级式汇总」），缺的日报 / 月报会自动补齐；年视图仍只在点按钮后才开始，避免误触发大量整理任务；打开没有整理过的时段会自动开始总结

### 找寻的检索与提速

「找寻」不是让大模型直接翻录音：模型只负责「调哪个工具」和「基于证据作答」，
真正的检索在服务端完成，一共四条路——字面精确（`LIKE`）、语义近似（TF-IDF 稀疏向量余弦）、
按日期列录音、读单条录音核实。

延迟主要在串行 LLM 调用上（每次决策 / 作答都是一个网络往返），所以：

- 模型选定一条检索路径后，服务端在**同一轮**把互补的字面 / 语义 / 日期检索一起跑掉，
  拿到证据就不再浪费一轮决策（`MAX_ROUNDS=2`），常见路径从 3~4 次 LLM 调用降到 2 次；
- 「总结推送」的推送决策与「工具推荐」互相独立，改为并行执行；
- TF-IDF 索引按数据签名缓存在 `data/finder_index/`（进程重启后可直接复用，删掉无妨）；
- 迁移 `0015_search_trgm` 给 `recordings.title/memo`、`chunks.transcript`、
  `summaries.title/markdown` 建 `pg_trgm` GIN 索引，让 `LIKE '%词%'` 不再整表扫描
  （1~2 个字的查询受 trigram 限制仍会扫描，属 PostgreSQL 固有限制）。

> 这里的「语义」是词面加权的 TF-IDF，不是神经 embedding。要提升同义 / 改述的召回率，
> 应引入 embedding + pgvector 混合检索——那是「搜得更准」，不是「搜得更快」。

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

## 对外 MCP 服务

在「设置 → 数据与设备 → MCP 服务」，第一次打开会**自动生成**一把账号级 Token（`mcps_` 开头），
点「重置」后旧值立即失效。服务地址是 `http://<主机>:8787/mcp`（Streamable HTTP），
旧版客户端用 `/sse`。鉴权支持 `Authorization: Bearer <token>`、`X-MCP-Token`、`X-API-Key`、`?token=`。

客户端配置（Claude Desktop / Cursor / WorkBuddy 通用）：

```json
{
  "mcpServers": {
    "忆录": {
      "url": "http://localhost:8787/mcp",
      "headers": { "Authorization": "Bearer mcps_xxx" }
    }
  }
}
```

### 提供的工具

| 工具 | 作用 |
| --- | --- |
| `upload_recording` | 上传录音（`audioBase64` + `mimeType`），效果与设备/开放 API 上传一致：建记录 → 转写 → 自动生成 AI 纪要；`wait=true` 可同步拿全文 |
| `get_daily_summary` | 输出指定日期（`YYYY-MM-DD`）的 AI 纪要：每日回忆全文 + 当天每条录音的纪要 + 待办 / 高亮；`generateIfMissing=true` 可当天现生成 |
| `list_recordings` | 按日期区间列出录音（标题、时间、时长、状态） |
| `get_recording` | 读单条录音的全文转写、时间戳和 AI 纪要 |
| `search_memory` | 在标题、备注、转写全文、纪要里做关键词检索 |
| `list_summaries` | 列出已有纪要 / 回忆，可按范围与日期筛选 |
| `get_action_items` | 正式待办 + AI 候选待办 |
| `generate_summary` | 触发 / 重新生成某天或某区间的回忆；周 / 月 / 年会先自动补齐下级日报 / 月报 |
| `get_service_status` | 录音数、纪要数、队列等概览 |

所有查询都按 Token 对应的账号隔离，客户端拿不到别人的数据。上传时音频上限 32MB（base64 解码后），
日志里会把 base64 替换成字节数描述，不会把音频原文写进数据库。

### 调用记录

`initialize` 握手会记住 `clientInfo`（名称 / 版本 / 协议版本），后续请求靠 `Mcp-Session-Id` 归到同一客户端。
设置页能看到：连接过的客户端（IP、首次/最近活跃、次数）、最近 60 条调用（时间、客户端、方法、工具、成功/失败、耗时），
以及累计调用 / 工具调用 / 客户端数。记录只保留最近 300 条，可一键清空。Token 重置、停用、鉴权失败也会留痕。

### 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/mcp` | MCP Streamable HTTP（JSON-RPC 2.0）；同时兼容 `text/event-stream` 单事件响应 |
| `GET` | `/sse` | 旧版 HTTP/SSE 传输的握手流 |
| `POST` | `/messages?sessionId=` | 旧版 SSE 的消息入口 |
| `DELETE` | `/mcp` | 结束会话 |
| `GET` | `/api/mcp-service` | 后台（JWT）：端点、Token、工具清单、统计 |
| `POST` | `/api/mcp-service/reset-token` | 后台（JWT）：重置 Token |
| `PATCH` | `/api/mcp-service` | 后台（JWT）：`{enabled}` 启用 / 停用 |
| `GET` / `DELETE` | `/api/mcp-service/logs` | 后台（JWT）：客户端与调用记录 / 清空 |

实现：`server-py/app/routes/mcp_service.py`（协议与接口）、`services/mcp_service.py`（Token、会话、日志）、
`services/mcp_tools.py`（工具定义与实现）、`web/src/pages/settings/McpServiceCard.tsx`（设置页卡片）。
协议按 MCP 2025-06-18 手写实现，不依赖 SDK 的服务端部分，避免 SDK 升级把内置服务带崩。

## 硬件设备接入（ESP32-C3 / 录音笔）

> 现成固件在 [`esp32/`](esp32/README.md)：长按录音、VAD 跳过静音、Opus 12kbps、先存本地（插了 TF 卡就用卡）、
> **存储快满或双击按键时才上传**，支持有屏（SSD1306 / ST7735 / 墨水屏）和无屏（LED + 蜂鸣器）两种形态，S3 / C3 都能编。
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

服务端**优先用设备录制时间**判断会话边界，没带 `recordedAt` 才退回服务器到达时间：

```
23:00:00  段1 ─┐
23:00:10  段2  ├─ 间隔 10s  < 阈值 → 同一次会话（一条记录，一份纪要）
23:00:20  段3 ─┘
                    ← 静默 5 分钟，超过「间隔」阈值
23:05:20  段4 ─── 新会话 → 新建一条记录
                    ← 之后再没有任何音频，静默超过「空闲」阈值
              ✅ 自动收尾：转写完成 → 生成这一整段聊天的纪要
```

> **为什么必须用录制时间**：设备出门断网时会攒一整天，回家连上 WiFi 后几十个分片几秒钟内全部推完。
> 只看到达时间的话，间隔永远是 1~2 秒，一整天会被并成一条记录，时间轴和纪要全错位。
> 带上 `recordedAt` 后，服务端按"本片开始录制 − 上一片结束录制"算真实间隔。不带的旧固件行为不变。

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `device` | `default` | 设备名，同一账号下唯一。**用 token 鉴权时可省略**，以 token 对应的那台设备为准 |
| `gapSec` | 90 | 相邻两段间隔超过多少秒就断开成新记录 |
| `idleSec` | 180 | 静默多少秒后自动收尾（触发纪要生成） |
| `durationMs` | 自动探测 | 本段音频时长；不传则服务端用 ffprobe 探测 |
| `title` | 自动 | 新会话的标题 |
| `recordedAt` | 服务端时间 | 本段**开始录制**的时间（ISO8601 UTC），补传场景强烈建议带上，否则会话会被并成一条 |

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
- 断网时把 PCM 存在 PSRAM/Flash 里，恢复后按顺序补推即可——带上 `recordedAt` 时服务端按
  **录制时间**判断间隔，所以补推时几秒钟发几十片也不会被并成一条；不带才退回到达时间。
- 设备只要内网能访问到电脑即可，不需要公网 IP、不需要 HTTPS（ESP32-C3 跑 TLS 很吃内存）。
- 编号印在机身/包装上更省事：同一串数字永远不会变，掉了重新输一遍就能再绑。
  设备被删或 token 泄露时，后台点「换发 token」，老 token 立即失效，设备自动重新配对。
- **单账号服务器会自动认领设备**：库里只有一个账号时，设备带着编号轮询就自动建档下发 token，
  恢复出厂后自己就回来了，不需要人工在后台绑定或重置 token；多账号部署仍要求显式认领。
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
| `server-py/.env` | 配置文件：数据库连接串（`DATABASE_URL`）、JWT 密钥、初始管理员账号 |
| `data/uploads/<录音ID>/` | 音频文件，按分片存放 |
| `data/models/` | 本地开源 ASR 模型（只有选了本地引擎才会有），删掉不影响已有录音 |
| `pg-data/` | compose 里 PostgreSQL 的数据目录（用外部库时没有这个目录） |

备份：`pg_dump` 导出数据库，再加上整个 `data/`（音频与本地模型）。
删除某条记录时，对应的音频目录会一起清理。

## 目录结构

```
.
├── server-py/                    FastAPI 后端
│   ├── app/
│   │   ├── main.py              入口：路由注册、静态托管、worker 启动、异常处理
│   │   ├── config.py            配置与路径
│   │   ├── db.py                数据访问门面（all_/one/execute），PostgreSQL 连接池
│   │   ├── db_config.py         解析 DATABASE_URL，给出连接描述
│   │   ├── db_migrate.py        Alembic 入口：启动时 upgrade head
│   │   ├── database/            PostgreSQL 后端（连接池）、占位符转换、语句切分
│   │   ├── migrations/          版本化 DDL（versions/0001_initial.py 起）
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
`localBackend: "sherpa-onnx" | "faster-whisper" | "whisper-cpp" | "mlx-qwen3-asr"` 和 `requiresKey/requiresBaseUrl: false`，
前端就会隐藏 Key / URL 输入框、改成本地模型选择器。
模型文件与下载逻辑都在 `services/local_asr_models.py`，新增 Whisper 尺寸往 `FASTER_WHISPER_SIZES` /
`WHISPER_CPP_SIZES` 加一行；新增 sherpa-onnx 模型（FireRedASR、Fun-ASR-Nano 等预转换包）往
`SHERPA_MODELS` 加一行「id / 显示名 / 体积 / 说明 / 压缩包名」即可，下载解压全自动。

## 已知限制

- 单用户/小团队自用设计，未做多租户隔离与配额限制
- 录音分片上传过程中关闭页面会丢失最后一段（已上传的分片不受影响）
- 长文本纪要使用「分段提炼 + 汇总」策略，极长录音（> 3 小时）质量会略有下降
- 说话人分离在转写完成后离线跑，不是实时标注；具名声纹识别（提前录入某人声纹并自动认人）尚未实现
- 本地 ASR 首次要下载几百 MB 模型，下载源慢时请耐心等或自行放好模型文件
- 本地 ASR 在纯 CPU 上跑 `medium` 以上会明显慢于实时，长录音建议 `small` / `turbo` 或上 GPU
- 数据库只支持 PostgreSQL：改连接信息要改配置文件并重启，页面上没有切换入口
- 旧版 SQLite 的 `data/recall.db` 不再被读取，历史数据需要自行迁移

## 排障：本地 ASR 报「子进程意外退出（code=1）」

本地引擎（sherpa-onnx / faster-whisper）跑在独立子进程里。如果每片都秒失败、错误信息里
还跟着一段 `SAFE_DELETE_*` 或者别的 `sitecustomize` 输出，基本就是 **`PYTHONPATH` 被外部环境
污染了**：某些 AI 助手 / 容器 / 安全套件的 shell 会把 `PYTHONPATH` 指向带 `sitecustomize.py`
的目录，那脚本会给 `os.remove` 挂钩子，推理进程清理临时 pcm 时被拦 → 直接退出。

排查与处理：

```bash
python -c "import py_compile,sys;print(sys.path)"   # 看有没有奇怪的 shim 目录
env -u PYTHONPATH -u NODE_OPTIONS python -m app.main  # 用干净环境启动
```

代码里已经兜了一层：拉子进程时会剔除 `PYTHONPATH`，并把它 stderr 的最后几行拼进错误信息
（`本地 ASR 子进程意外退出（code=1）：<子进程输出>`），下次再遇到能直接看出原因。
