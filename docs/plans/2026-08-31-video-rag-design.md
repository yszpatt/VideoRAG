# videoRAG 设计文档

> 把 B站 / YouTube / 抖音 / 小红书 的视频转成笔记，落入 RAG 知识库，用于视频快速理解与资料速查。
> 单容器 all-in-one，部署在 NAS（x86 纯 CPU）上。

日期：2026-08-31
状态：待评审（规划阶段）

---

## 1. 定位与目标

- **一句话**：粘贴视频链接 → 后台自动「下载 → 转写 → 结构化笔记 → 切片入库」→ 用自然语言提问，返回带时间戳、可跳转原视频的答案。
- **目标用户**：个人（自托管），本地优先、数据不出内网（除摘要/问答走云端 LLM）。
- **成功标准**：
  - 一条 `docker run` 即可在 NAS 上跑通全链路。
  - 从贴链接到可检索，长视频（1h 中文）在纯 CPU 上可控时间内完成（预期几分钟~十几分钟，取决于 whisper 模型档位）。
  - 检索问答答案附「视频名 + 时间段」，可跳回原视频定位。

---

## 2. 关键决策（已与用户对齐）

| 维度 | 决策 |
|------|------|
| AI 算力形态 | **混合**：本地 faster-whisper 转写 + 本地 bge embedding；云端 LLM（DeepSeek，OpenAI 兼容）做摘要/笔记/问答 |
| NAS 硬件 | x86 纯 CPU（faster-whisper 走 int8 量化，不依赖 GPU） |
| 平台 | B站 + YouTube（一期核心）；抖音 + 小红书 + 通用直链（二期） |
| 与既有 RAG 关系 | **完全独立新项目**，与 KnowledgePilot 解耦 |
| 部署形态 | **单容器 all-in-one**（内置 Web UI + SQLite + 轻量向量库） |
| 视频保留 | 转写完成后**删除原始视频**，只留音频/文本/嵌入 |
| 交互 | 完整 Web UI + 预留 REST API |

---

## 3. 架构总览

```
[视频链接/文件]
      │
      ▼
[下载]  yt-dlp（B站/YouTube/直链）+ cookie 适配（抖音/小红书）
      │
      ▼
[音频]  ffmpeg 抽取音频
      │
      ▼
[转写]  faster-whisper（int8，CPU）→ 带时间戳全文
      │
      ▼
[笔记]  云 LLM 生成：摘要 / 章节 / 要点 / 金句 / 术语表
      │
      ▼
[入库]  语义切片 + bge 本地 embedding → 向量库
      │
      ▼
[检索]  提问 → 全文+向量混合检索 → 云 LLM 回答（附时间戳）
```

两条主链路：
1. **摄入链路（ingestion）**：链接 → 笔记 → 入库，后台异步执行，可排队。
2. **检索链路（query）**：提问 → 混合检索 → 生成回答 → 返回来源片段与时间戳。

另有一条**对外能力链路**：通过 MCP 服务把「检索 / 问答 / 笔记读取」能力暴露给外部 agent（Cline CLI、Reasonix 等），实现「agent 直接查视频知识库」。

---

## 4. 技术选型

| 层 | 选型 | 理由 |
|----|------|------|
| 获取（多级） | `yt-dlp` 下载 / 字幕抓取 / Gemini 直读 | 字幕优先→音频→视频；公开 YouTube 可 Gemini 直读零下载 |
| 音频 | `ffmpeg` | 通用抽音轨、重采样为 16kHz 供 whisper |
| 转写（多级） | 字幕直用 → `faster-whisper` → 云 ASR → Gemini | 详见 4.1 节；默认本地兜底，云/Gemini 为可选开关 |
| 转写模型 | `large-v3` / `small`（可配） | 中文效果好；低配 NAS 可切 small 提速 |
| 摘要/问答 LLM | DeepSeek（OpenAI 兼容 `base_url` 可配） | 便宜、中文好；换 Gemini/OpenAI 仅改 env |
| embedding | `bge-small-zh-v1.5`（FlagEmbedding/sentence-transformers） | 中文向量，小模型 CPU 秒级，本地不联网 |
| 向量库 | `LanceDB`（embedded，文件型） | 单容器无需独立服务，原生支持全文(tantivy)+向量混合检索 |
| 后端 | FastAPI | 与用户既有技术栈一致，异步友好 |
| 任务队列 | 内置（SQLite 持久化任务状态 + 进程内并发） | 单容器避免引入 Redis |
| 前端 | React + Vite（SPA，后端静态托管） | 轻量；可后续复用 KnowledgePilot 前端风格 |
| MCP 服务 | `mcp`（FastMCP，Python 官方 SDK） | 挂 FastAPI 同端口 `/mcp`，Streamable HTTP 传输，暴露检索/问答工具给 agent |
| 数据目录 | SQLite（元数据/任务）+ LanceDB（向量） | 全文件型，`/data` 卷持久化即可 |

**备选**：向量库也可用 `sqlite-vec`（更极简）或 `ChromaDB`（生态熟）；若后续要并发/多实例再迁 Qdrant。

### 4.1 多级获取/转写策略（可插拔 provider）

视频获取与转写不写死单一路径，拆成「获取层」+「转写层」两个可插拔 provider，按优先级自动降级。核心原则：**尽量不下视频、能不下音频就不下音频、有字幕绝不跑 ASR**。

#### 获取层（拿什么）

| 优先级 | provider | 产物 | 成本 | 适用 |
|--------|----------|------|------|------|
| 1 | 字幕抓取（yt-dlp `--write-subs`） | `.srt` / `.vtt` | ≈0，秒级 | B站/YouTube 有字幕 |
| 2 | 音频下载（yt-dlp `-x`） | 音频轨 | 下载量小 | 所有平台、无字幕 |
| 3 | 视频下载 | 视频文件（临时） | 下载量大 | 需画面/兜底 |
| 4 | Gemini 直读 | 无（直传 URL） | token 计费 | 仅公开 YouTube URL / 本地上传 |

#### 转写层（怎么转）

| 优先级 | provider | 说明 | 成本 | 隐私 |
|--------|----------|------|------|------|
| 1 | 字幕直用 | 抓到字幕直接当全文，跳过 ASR | 0 | 全本地 |
| 2 | 本地 whisper | faster-whisper int8 | 0（算力） | 全本地 |
| 3 | 云 ASR | 阿里百炼 FunASR ¥0.288/h、百度免费额度等 | 低 | 音频出内网 |
| 4 | Gemini 多模态 | 直接理解视频，出摘要/时间戳/转录 | 中 | 数据给 Google |

#### 默认降级链

```
有字幕？ ──是──▶ 抓字幕直用（跳过 ASR）
   │否
   ▼
下音频 ──▶ 本地 whisper
   │可选（显式开启）
   ▼
云 ASR / Gemini 直读
```

- 默认只启用前两级（字幕 + 本地 whisper），**零成本、全本地**。
- 云 ASR / Gemini 作为「可选加速/覆盖」：需显式配置对应 API key 才启用。
- 平台覆盖：B站/抖音/小红书无公开 URL 直读能力，Gemini 读不了其 URL，故这三家**必须下「字幕或音频」其一**；仅公开 YouTube 可走 Gemini 零下载。

#### provider 抽象

```python
# 获取层
class Fetcher(Protocol):
    async def fetch(self, url: str) -> FetchedMedia: ...

# 转写层
class Transcriber(Protocol):
    async def transcribe(self, media: FetchedMedia) -> Transcript: ...
```

每个平台/方式一个实现，注册进 registry，按上表优先级 fallback；失败自动降级到下一档并记录原因。

---

## 5. 数据模型（核心表）

- `videos`：视频元信息（平台、URL、标题、作者、时长、状态、笔记路径）
- `tasks`：任务队列（类型=download/transcribe/note/embed，状态、进度、错误）
- `segments`：转写分段（起止时间戳、文本、说话人）
- `chunks`：入库切片（文本、embedding 引用、来源 segment 区间）
- `notes`：结构化笔记（摘要、章节、要点、金句、术语，markdown + JSON）

向量侧由 LanceDB 管理，`chunks` 表通过 `chunk_id` 关联。

---

## 6. 目录 / 卷结构

```
/data（docker 挂载卷）
├── downloads/    # 临时下载，转写后清理
├── audio/        # 抽取的音频（可选保留）
├── transcripts/  # 转写 JSON（全文 + 时间戳）
├── notes/        # 结构化笔记 markdown
├── db/           # SQLite（元数据/任务）
├── lancedb/      # 向量库
└── models/       # whisper + embedding 模型缓存（持久化，避免每次重下）
```

---

## 7. Docker 形态

- **基础镜像**：`python:3.11-slim` + `ffmpeg` + `yt-dlp`，多阶段构建前端产物。
- **架构**：先出 `linux/amd64`，后续补 `linux/arm64`。
- **一条命令启动**：
  ```bash
  docker run -d --name videorag \
    -p 8080:8080 \
    -v /path/to/videorag-data:/data \
    -e LLM_API_KEY=sk-xxx \
    -e LLM_BASE_URL=https://api.deepseek.com \
    -e LLM_MODEL=deepseek-chat \
    -e WHISPER_MODEL=large-v3 \
    --restart unless-stopped \
    yszpatt/videorag:latest
  ```
- **关键环境变量**：`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`、`WHISPER_MODEL`、`EMBED_MODEL`、`COOKIE_DIR`（抖音/小红书 cookie）、`MCP_API_KEY`（MCP 鉴权，可选，缺省则 MCP 不鉴权）。
- API key 只走 env，不落库、不出现在前端。
- MCP 端点与 Web 同端口：`http://<nas-ip>:8080/mcp`（Streamable HTTP），供外部 agent 连接。

---

## 8. 分阶段路线图

| 阶段 | 内容 | 产出 |
|------|------|------|
| **P0 骨架** | FastAPI + 空 UI + SQLite + 任务表，Docker 跑通 | 可启动的最小服务 |
| **P1 转写** | B站/YouTube 下载 + ffmpeg + faster-whisper 转写 | 带时间戳全文 |
| **P2 笔记** | 云 LLM 生成结构化笔记 + markdown 导出 | 可读笔记 |
| **P3 入库** | 语义切片 + bge embedding + LanceDB 入库 | 可检索向量库 |
| **P4 问答** | 混合检索 + RAG 生成 + 来源时间戳 | 核心价值闭环 |
| **P5 平台扩展** | 抖音/小红书 cookie 适配 + 通用直链 | 全平台覆盖 |
| **P6 打磨** | Web UI 完善（进度/搜索/问答/管理）+ API 文档 | 可交付 |
| **P7 MCP 外挂** | `/mcp` Streamable HTTP 端点 + 工具集 + 鉴权/限流/审计 | agent 可查知识库 |

P0–P4 即可形成「贴链接 → 查资料」的完整闭环，P5/P6/P7 为扩展与打磨。MCP 不依赖 UI，P4 问答闭环后即可先行。

---

## 9. MCP 服务（agent 外挂）

把 RAG 知识库的能力通过 MCP 暴露给外部 AI agent（Cline CLI、Reasonix 等），让 agent 在编码 / 研究时直接检索视频资料。

### 9.1 传输模式

| 模式 | 适用 | 选择 |
|------|------|------|
| **Streamable HTTP** | 远程（NAS 上的 videoRAG，本地 agent 连） | ✅ 主推，`/mcp` 端点 |
| stdio | 同机（agent 与 videoRAG 同机 spawn） | 可选，独立入口复用同一 `/data` |
| SSE（HTTP+SSE） | 老客户端兼容 | ❌ 已废弃（2025-03），不做 |

### 9.2 MCP 工具集（tools）

| 工具 | 参数 | 返回 |
|------|------|------|
| `search_knowledge_base` | `query`, `top_k` | 相关片段（文本、来源视频、时间戳、相关度），全文+向量混合检索 |
| `ask_video_rag` | `question` | RAG 生成的答案 + 引用（来源片段 + 时间戳） |
| `list_videos` | `platform?`, `keyword?`, `limit` | 已入库视频清单 |
| `get_video_note` | `video_id` | 结构化笔记（摘要/章节/要点/金句） |
| `get_transcript` | `video_id`, `start?`, `end?` | 带时间戳的转写文本（分段返回，避免超长） |
| `submit_video`（可选） | `url` | 提交视频链接进处理队列 |

`search_knowledge_base` 与 `ask_video_rag` 是 agent 最常用的两个；`get_*` 系列供 agent 深入引用。

### 9.3 鉴权与安全（MCP 协议本身不鉴权，需自建）

- **鉴权**：`Authorization: Bearer <MCP_API_KEY>`；缺省 `MCP_API_KEY` 时允许内网匿名（建议默认设 key）。
- **审计**：记录每次 `tools/call`（时间、工具名、参数摘要、调用来源）。
- **限流**：防 agent 循环把服务打爆（如每工具 N 次/分钟）。
- **输入校验**：query 长度上限、参数白名单。

### 9.4 客户端配置示例

Cline CLI（`mcpServers` 配置）：
```json
{
  "mcpServers": {
    "video-rag": {
      "url": "http://<nas-ip>:8080/mcp",
      "headers": { "Authorization": "Bearer <MCP_API_KEY>" }
    }
  }
}
```

Reasonix / 其他支持 Streamable HTTP 的 agent 同理，指向 `http://<nas-ip>:8080/mcp` 即可。

---

## 10. 风险与注意点

1. **抖音/小红书接口易变、cookie 易失效**：做成「插件化下载器」，单一平台失效不影响整体；提示用户定期更新 cookie。
2. **纯 CPU 转写耗时**：`large-v3` int8 转写 1h 中文约需几分钟~十几分钟；NAS 内存建议 ≥ 8GB（large-v3 峰值约 3–4GB），低配切 `small`。
3. **向量库切换成本**：LanceDB 文件型、字段 schema 简单，后续迁 Qdrant 成本低，但建议 P0 就固定 chunk schema。
4. **版权合规**：仅供个人学习/资料整理用途，不提供公开分发。
5. **模型首次拉取**：whisper + embedding 模型约 1–3GB，`/data/models` 持久化避免反复下载。
6. **MCP 端点暴露**：`/mcp` 会把检索能力暴露到内网，务必设 `MCP_API_KEY` + 限流 + 审计，避免被任意 agent 调用。

---

## 11. 待确认项

- 云 LLM 默认 provider 是否就用 DeepSeek？（当前按 DeepSeek 设计，env 可换）
- 是否需要「说话人分离（diarization）」？（首期不做，仅留扩展点）
- 云 ASR / Gemini 直读是否纳入一期？（默认一期只做「字幕 + 本地 whisper」，云/Gemini 作为二期可选开关）
- MCP 是否默认启用鉴权 `MCP_API_KEY`？（建议内网也默认开，可关）

---

*本文档为规划阶段产物，P0 开发前建议先确认「待确认项」。*
