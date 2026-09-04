# videoRAG 技术设计文档

> 本文档是 videoRAG 的详细技术设计，指导后续实现。高层设计见 `2026-08-31-video-rag-design.md`。
> 读者假设：有 Python/后端经验，但不了解本项目与「视频→笔记→RAG」领域。

**Goal**：把 B站/YouTube/抖音/小红书/通用直链的视频转成结构化笔记，落入可检索的 RAG 知识库，通过 Web UI 与 MCP 服务对外提供「快速理解 + 资料速查」。

**Architecture**：单容器 all-in-one（FastAPI + 内置任务队列 + React/Vite SPA + SQLite + LanceDB）。摄入链路「获取→转写→笔记→切片→入库」后台异步执行；检索链路「混合检索 + 云 LLM 生成」同步服务；两者共用同一套 `service` 层，MCP 端点复用检索/问答能力。

**Tech Stack**：Python 3.11 · FastAPI · SQLAlchemy 2.0 (async) · faster-whisper · sentence-transformers (bge) · LanceDB · yt-dlp · ffmpeg · `mcp` (FastMCP) · OpenAI 兼容 LLM 客户端 · React + Vite。

---

## 1. 系统架构

单容器内部分层（进程内，无独立服务）：

```
┌───────────────────────── 容器 8080 ─────────────────────────┐
│  Web SPA (React/Vite 静态文件)                              │
│  FastAPI                                                   │
│   ├─ REST API  (/api/*)                                    │
│   ├─ MCP 端点  (/mcp, Streamable HTTP)                     │
│   └─ 内置任务队列 (asyncio + SQLite 持久化)                  │
│        └─ 摄入流水线 pipeline (fetch→transcribe→note→embed) │
│  service 层                                                 │
│   ├─ fetchers（获取 provider 多级）                         │
│   ├─ transcribers（转写 provider 多级）                     │
│   ├─ notes（LLM 笔记生成）                                  │
│   ├─ embed（切片 + 向量化）                                 │
│   ├─ vector（LanceDB）                                     │
│   └─ rag（检索 + 问答）                                     │
│  数据：SQLite（元数据/任务） + LanceDB（向量）                │
└────────────────────────────────────────────────────────────┘
```

关键约束：
- **单进程并发**：任务队列用 `asyncio` 协程 + SQLite 持久化状态，不引入 Redis/Celery。
- **CPU 密集任务（whisper/embedding）**：用 `asyncio.to_thread` 丢到线程池，避免阻塞事件循环；并发数由 `MAX_CONCURRENT_TASKS` 限制（纯 CPU NAS 默认 1）。
- **模型懒加载 + 常驻**：whisper 与 embedding 模型首次用才加载，之后常驻内存复用。

---

## 2. 代码目录结构

```
videoRAG/
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml            # 可选，方便调试
├── .env.example
├── README.md
├── app/
│   ├── __init__.py
│   ├── main.py                   # FastAPI 入口：挂 REST + /mcp + 静态前端 + 启动队列
│   ├── config.py                 # pydantic-settings 读 env
│   ├── db.py                     # async engine + session 工厂
│   ├── models.py                 # SQLAlchemy ORM
│   ├── schemas.py                # Pydantic 请求/响应
│   ├── api/
│   │   ├── __init__.py
│   │   ├── videos.py             # POST/GET 视频
│   │   ├── search.py             # POST /search /ask
│   │   └── tasks.py              # GET 任务状态
│   ├── core/
│   │   ├── llm.py                # OpenAI 兼容 LLM 客户端（封装重试）
│   │   ├── queue.py              # 内置任务队列
│   │   ├── fetchers/
│   │   │   ├── base.py           # Fetcher Protocol + FetchedMedia
│   │   │   ├── registry.py       # 按平台/优先级 fallback
│   │   │   ├── ytdlp.py          # 字幕/音频/视频下载
│   │   │   └── gemini_direct.py  # Gemini 直读 URL
│   │   ├── transcribers/
│   │   │   ├── base.py           # Transcriber Protocol + Transcript
│   │   │   ├── subtitle.py       # 字幕直用
│   │   │   ├── whisper.py        # faster-whisper
│   │   │   ├── cloud_asr.py      # 云 ASR（可插拔）
│   │   │   └── gemini.py         # Gemini 多模态转录
│   │   ├── notes.py              # LLM 结构化笔记生成
│   │   ├── embed/
│   │   │   ├── chunker.py        # 语义切片
│   │   │   └── embedder.py       # bge embedding
│   │   ├── vector_store.py       # LanceDB 封装
│   │   └── rag/
│   │       ├── retriever.py      # 混合检索
│   │       └── qa.py             # RAG 问答
│   ├── mcp_server.py             # FastMCP + tools + 鉴权/限流
│   └── jobs/
│       └── pipeline.py           # 摄入流水线编排（fetch→…→embed）
├── web/                          # React + Vite 前端（构建产物由后端托管）
└── tests/
    ├── conftest.py
    ├── test_chunker.py
    ├── test_fetchers.py
    ├── test_transcribers.py
    ├── test_retriever.py
    ├── test_qa.py
    ├── test_api.py
    └── test_mcp.py
```

---

## 3. 数据模型

### 3.1 SQLite（SQLAlchemy，`/data/db/videorag.db`）

```sql
CREATE TABLE videos (
  id            TEXT PRIMARY KEY,        -- uuid4 hex
  platform      TEXT NOT NULL,           -- bilibili|youtube|douyin|xhs|generic
  url           TEXT NOT NULL,
  title         TEXT,
  author        TEXT,
  duration_sec  REAL,
  status        TEXT NOT NULL DEFAULT 'pending',  -- pending|fetching|transcribing|noting|embedding|done|failed
  note_path     TEXT,                    -- /data/notes/<id>.md
  error         TEXT,
  created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE tasks (
  id            TEXT PRIMARY KEY,
  video_id      TEXT NOT NULL REFERENCES videos(id),
  type          TEXT NOT NULL,           -- fetch|transcribe|note|embed
  status        TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|failed
  progress      REAL NOT NULL DEFAULT 0,
  error         TEXT,
  payload       TEXT,                    -- JSON（如 provider 名、参数）
  created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE segments (
  id            TEXT PRIMARY KEY,
  video_id      TEXT NOT NULL REFERENCES videos(id),
  start_sec     REAL NOT NULL,
  end_sec       REAL NOT NULL,
  text          TEXT NOT NULL,
  speaker       TEXT                     -- 预留 diarization
);

CREATE TABLE chunks (
  id            TEXT PRIMARY KEY,
  video_id      TEXT NOT NULL REFERENCES videos(id),
  content       TEXT NOT NULL,
  start_sec     REAL NOT NULL,
  end_sec       REAL NOT NULL,
  metadata      TEXT,                    -- JSON
  lancedb_id    TEXT                     -- 关联向量行
);

CREATE TABLE notes (
  id            TEXT PRIMARY KEY,
  video_id      TEXT NOT NULL REFERENCES videos(id),
  summary       TEXT,
  chapters      TEXT,                    -- JSON [{title,start_sec,end_sec,points}]
  key_points    TEXT,                    -- JSON [str]
  quotes        TEXT,                    -- JSON [{text,start_sec}]
  glossary      TEXT,                    -- JSON [{term,explanation}]
  markdown      TEXT
);
```

索引：`tasks(video_id)`, `segments(video_id, start_sec)`, `chunks(video_id)`。

### 3.2 LanceDB（`/data/lancedb`）

表 `chunks`：

| 字段 | 类型 | 说明 |
|------|------|------|
| id | string | 与 SQLite `chunks.id` 一致 |
| video_id | string | |
| content | string | 切片文本 |
| embedding | vector(512) | bge-small-zh-v1.5 维度 |
| start_sec | float | |
| end_sec | float | |
| title | string | 视频标题（冗余，检索展示用） |
| platform | string | |

启用全文索引（tantivy）以支持混合检索。

---

## 4. 多级获取/转写 provider 设计

### 4.1 接口定义

```python
# app/core/fetchers/base.py
@dataclass
class FetchedMedia:
    kind: Literal["subtitle", "audio", "video", "direct"]
    path: str | None = None        # 字幕/音频/视频本地路径
    subtitle_text: str | None = None
    url: str | None = None         # direct 模式下给 Gemini 的原始 URL
    meta: dict = field(default_factory=dict)

class Fetcher(Protocol):
    name: str
    async def fetch(self, url: str, workdir: Path) -> FetchedMedia: ...

# app/core/transcribers/base.py
@dataclass
class Transcript:
    segments: list[Segment]        # Segment(id,start_sec,end_sec,text,speaker)
    raw_text: str
    source: str                    # 记录用了哪个 provider（字幕/whisper/...）

class Transcriber(Protocol):
    name: str
    async def transcribe(self, media: FetchedMedia) -> Transcript: ...
```

### 4.2 降级策略（registry）

```python
FETCH_CHAIN = [
    ("subtitle", SubtitleFetcher),     # yt-dlp --write-subs
    ("audio",    AudioFetcher),        # yt-dlp -x + ffmpeg
    ("video",    VideoFetcher),        # yt-dlp 完整视频（兜底）
    ("direct",   GeminiDirectFetcher), # 仅公开 YouTube，且 GEMINI_API_KEY 已配
]

TRANSCRIBE_CHAIN = [
    ("subtitle", SubtitleTranscriber), # media.kind == subtitle 时直用
    ("whisper",  WhisperTranscriber),
    # 以下仅当对应 env 已配置才注册
    ("cloud_asr", CloudAsrTranscriber),
    ("gemini",    GeminiTranscriber),
]
```

规则：
- 沿 `FETCH_CHAIN` 顺序尝试，`fetch` 抛异常则记录原因并降级下一档。
- `media.kind` 决定 `TRANSCRIBE_CHAIN` 起点：`subtitle` → 从 `subtitle` 起；`audio`/`video` → 从 `whisper` 起；`direct` → 从 `gemini` 起。
- 未配置 key 的 provider 不注册，直接从链中剔除。

---

## 5. 核心模块设计

### 5.1 LLM 客户端 `core/llm.py`

```python
class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str): ...
    async def chat(self, messages: list[dict], temperature: float = 0.2,
                   max_tokens: int = 4096) -> str: ...
    async def chat_json(self, messages: list[dict], schema: dict) -> dict: ...
```

- 用 `openai.AsyncOpenAI`（兼容 DeepSeek/Gemini/OpenAI 等）。
- 重试：3 次，指数退避（1s/2s/4s），仅对 429/5xx/超时。
- `chat_json` 走 JSON mode，供结构化笔记生成。

### 5.2 内置任务队列 `core/queue.py`

- `enqueue(video_id, type, payload)` → 写入 `tasks`，加入 `asyncio.Queue`。
- worker 协程（`MAX_CONCURRENT_TASKS` 个）消费队列，执行 `jobs/pipeline.py` 对应阶段。
- 每个任务开始/结束更新 `tasks.status/progress/error`。
- **崩溃恢复**：启动时把 `running` 状态的遗留任务置回 `pending` 重新入队。

### 5.3 摄入流水线 `jobs/pipeline.py`

```python
async def process_video(video_id: str) -> None:
    # 1. fetch（多级降级）
    media = await fetch_chain(video.url)
    # 2. transcribe
    transcript = await transcribe_chain(media)
    await save_segments(video_id, transcript.segments)
    # 3. note（云 LLM）
    note = await generate_note(transcript)
    await save_note(video_id, note)
    # 4. embed
    chunks = chunk(transcript.segments)
    await embed_and_store(video_id, chunks)
    # 5. 清理临时视频/音频
    cleanup(media)
    mark_done(video_id)
```

### 5.4 切片 `embed/chunker.py`

- 优先按「章节/句子」边界切分，目标每片 200–500 字，带重叠 50 字。
- 用转写的 `end_sec` 对齐，保证每个 chunk 有准确时间区间。
- 输出 `Chunk(content, start_sec, end_sec, video_id)`。

### 5.5 embedding `embed/embedder.py`

- 模型：`BAAI/bge-small-zh-v1.5`（512 维），首次加载后常驻。
- 文本按 `BGE` 规范：检索侧 query 加前缀 `为这个句子生成表示以用于检索相关文章：`。

### 5.6 检索 `rag/retriever.py`

```python
async def retrieve(query: str, top_k: int = 8) -> list[Hit]:
    # Hit(id, video_id, content, start_sec, end_sec, title, score)
```

- 混合检索：LanceDB 向量相似度 + 全文(tantivy) 打分，做 RRF（Reciprocal Rank Fusion）合并。
- 可选 rerank（bge-reranker，二期）。

### 5.7 问答 `rag/qa.py`

```python
async def answer(question: str, top_k: int = 8) -> Answer:
    # Answer(answer, citations: list[Hit])
```

Prompt 模板（要点）：

```
你是视频知识库助手。仅依据下方「参考资料」回答；资料不相关则如实说不知道。
回答要简洁、分点；每条结论后标注来源 [n]，最后列出引用的时间戳。

参考资料：
[1] (视频《{title}》 {start_sec}s–{end_sec}s) {content}
...
```

- 返回 answer + citations（供前端/MCP 展示可跳转时间戳）。

### 5.8 MCP 服务 `mcp_server.py`

```python
mcp = FastMCP("video-rag")

@mcp.tool()
async def search_knowledge_base(query: str, top_k: int = 5) -> str: ...
@mcp.tool()
async def ask_video_rag(question: str) -> str: ...
@mcp.tool()
async def list_videos(platform: str | None = None, keyword: str | None = None,
                      limit: int = 20) -> str: ...
@mcp.tool()
async def get_video_note(video_id: str) -> str: ...
@mcp.tool()
async def get_transcript(video_id: str, start: float | None = None,
                         end: float | None = None) -> str: ...
@mcp.tool()
async def submit_video(url: str) -> str: ...
```

- 挂载：`app.mount("/mcp", mcp.streamable_http_app())`（Streamable HTTP）。
- 鉴权中间件：校验 `Authorization: Bearer <MCP_API_KEY>`（未设 key 则放行，但打 warning）。
- 限流：每工具独立滑动窗口（如 60 次/分钟），超限返回错误。
- 审计：每次 `tools/call` 记日志（时间、工具名、参数摘要、来源 IP）。

---

## 6. API 设计

### 6.1 REST（前缀 `/api`）

| 方法 | 路径 | 说明 | 请求 | 响应 |
|------|------|------|------|------|
| POST | `/api/videos` | 提交视频 | `{url}` | `{video_id, status}` |
| GET | `/api/videos` | 列表 | `?platform=&keyword=&limit=` | `[{...}]` |
| GET | `/api/videos/{id}` | 详情+状态 | - | 视频对象 |
| GET | `/api/videos/{id}/note` | 结构化笔记 | - | 笔记 JSON + markdown |
| GET | `/api/videos/{id}/transcript` | 转写 | `?start=&end=` | `{segments:[...]}` |
| POST | `/api/search` | 检索 | `{query, top_k}` | `{hits:[...]}` |
| POST | `/api/ask` | RAG 问答 | `{question, top_k}` | `{answer, citations:[...]}` |
| GET | `/api/tasks/{id}` | 任务状态 | - | 任务对象 |
| GET | `/health` | 健康检查 | - | `{status:"ok"}` |

### 6.2 MCP tools

见 5.8，字段与 REST 对应，返回 JSON 字符串（MCP 工具返回文本）。

---

## 7. 配置（环境变量）

| 变量 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `LLM_API_KEY` | ✅ | - | 云 LLM key |
| `LLM_BASE_URL` | ✅ | `https://api.deepseek.com` | OpenAI 兼容 base_url |
| `LLM_MODEL` | - | `deepseek-chat` | |
| `WHISPER_MODEL` | - | `large-v3` | `small`/`medium`/`large-v3` |
| `EMBED_MODEL` | - | `BAAI/bge-small-zh-v1.5` | |
| `MCP_API_KEY` | - | 空 | 空则 MCP 不鉴权 |
| `GEMINI_API_KEY` | - | 空 | 配了才启用 Gemini 直读/转录 |
| `CLOUD_ASR_PROVIDER` | - | 空 | `aliyun`/`baidu`/… 配了才启用云 ASR |
| `CLOUD_ASR_KEY` | - | 空 | |
| `COOKIE_DIR` | - | `/data/cookies` | 抖音/小红书 cookie |
| `DATA_DIR` | - | `/data` | 数据根目录 |
| `PORT` | - | `8080` | |
| `MAX_CONCURRENT_TASKS` | - | `1` | CPU 密集任务并发 |

---

## 8. 关键数据流

### 8.1 摄入（异步）

```
POST /api/videos {url}
  → videos(id, pending) + tasks(fetch, pending)
  → queue worker 取出
  → fetch_chain：字幕?→音频?→视频?→Gemini直读?
  → transcribe_chain → segments
  → generate_note(LLM) → notes
  → chunker → embedder → LanceDB
  → cleanup(临时媒体) → videos(status=done)
```

### 8.2 问答（同步）

```
POST /api/ask {question}  或  MCP ask_video_rag
  → retrieve(question, top_k)      # LanceDB 混合检索
  → llm.chat(prompt + 引用片段)     # 云 LLM 生成
  → {answer, citations:[{title,start_sec,end_sec,content}]}
```

---

## 9. 错误处理策略

- **fetch 失败**：降级下一 provider；全部失败 → `videos.status=failed` + error 记录，任务不重试（平台侧问题，重试无益）。
- **transcribe 失败**：`tasks.status=failed`，支持手动重试。
- **LLM 调用失败**：`llm.py` 内重试 3 次；仍失败则该任务 failed，不阻塞队列。
- **任务崩溃/重启**：启动时把 `running` 任务重置为 `pending` 重新入队。
- **磁盘不足**：下载前检查剩余空间，不足则跳过视频下载档、强制字幕/音频档。
- **模型加载失败**：启动时惰性加载，失败给出明确日志（提示检查 `/data/models` 与网络）。

---

## 10. 测试策略

- **单元**：`chunker`（边界/重叠/时间对齐）、`retriever`（RRF 合并，mock LanceDB）、`qa`（prompt 拼接 + 引用格式）。
- **provider**：fetcher/transcriber 用 mock（不真连外网），验证降级链顺序。
- **集成**：`pipeline` 用一段本地短音频 fixture 走完「转写→笔记(mock LLM)→入库」。
- **API**：`httpx.AsyncClient` + `ASGITransport` 测 REST 端点。
- **MCP**：官方 `mcp` client 连 streamable HTTP 应用，验证 tool 调用与鉴权。

---

## 11. 分阶段实施任务

| 阶段 | 任务 | 验收 |
|------|------|------|
| P0 骨架 | 项目初始化、config/db/models、`/health`、任务队列、Dockerfile 跑通 | `docker run` 后 `/health` 返回 ok |
| P1 转写 | ytdlp fetcher（字幕/音频/视频）+ whisper transcriber + pipeline | 贴 B站/YouTube 链接出带时间戳全文 |
| P2 笔记 | llm client + notes 生成（JSON mode） | 出摘要/章节/要点/金句 markdown |
| P3 入库 | chunker + embedder + LanceDB store | 视频可被向量检索 |
| P4 问答 | retriever（混合）+ qa | `/api/ask` 返回带时间戳引用答案 |
| P5 平台 | 抖音/小红书 fetcher（cookie）+ 通用直链 | 全平台可摄入 |
| P6 打磨 | Web UI（提交/进度/搜索/问答/管理） | 浏览器完整可用 |
| P7 MCP | mcp_server + tools + 鉴权/限流/审计 | agent 能连 `/mcp` 查知识库 |

P0–P4 形成核心闭环；MCP（P7）可在 P4 完成后提前做，不依赖 P6 前端。

---

*本文档随实现推进持续更新；具体到文件与代码级的 bite-sized 任务，在进入编码阶段时用 `writing-plans` 逐阶段展开。*
