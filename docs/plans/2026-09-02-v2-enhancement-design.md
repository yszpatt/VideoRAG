# videoRAG v2 增强方案设计

> 五大方向：视频元数据采集 / 提示词可配置 / 无语音视频采集 / UI 与移动端优化 / 历史记录。
> 目标：本文档可直接作为 v2 开发的执行依据，含功能设计、技术设计、验证方法与实施计划。

日期：2026-09-02
状态：待评审
关联文档：
- `docs/plans/2026-08-31-video-rag-design.md`（v1 高层设计）
- `docs/plans/2026-08-31-video-rag-technical-design.md`（v1 技术设计）

---

## 0. 概述

### 0.1 模块一览

| 编号 | 模块 | 一句话目标 | 里程碑 |
|------|------|-----------|--------|
| E1 | 视频元数据采集与展示 | 采集标题/简介/封面/标签/评论等，视频库可识别"哪条笔记值得看" | M2 |
| E2 | 提示词可配置 | 笔记生成与问答的提示词在设置页可改，支持变量注入 | M1 |
| E3 | 无语音视频信息采集 | 无对白视频走「关键帧 + OCR + VLM」视觉旁路管线 | M5 |
| E4 | UI 优化与移动端适配 | 亮/暗双主题、移动端底部导航、阅读体验全面升级 | M4 |
| E5 | 历史提问与检索记录 | 服务端记录问答/检索历史，可回看、回填、防重复提问 | M3 |

### 0.2 现状核对结论（写文档前已逐一核实）

- **元数据完全未采集**：`app/core/fetchers/ytdlp.py:106-113` 的 `run_ytdlp()` 子进程 stdout（`--dump-json` 可产出的完整 info dict）被直接丢弃；`Video` 表虽有 `title/author/duration_sec` 字段（`app/models.py:25-42`），但 `video_service.py:15` 创建时只写 platform/url，笔记用的 title 实为 URL 兜底（`pipeline.py:74`）。
- **提示词全部硬编码**：笔记单次/map/reduce 三套 prompt 在 `app/core/notes.py:32-93`，问答 prompt 在 `app/core/rag/qa.py:14-29`，无任何配置出口。
- **配置热更新机制已具备**：`runtime.env` + `apply_runtime()`（`app/config.py:105-119`，通用 env→字段映射）+ `PUT /api/settings` 组件热替换（`app/api/settings.py:78-115`）。但 runtime.env 每键一行（`runtime_config.py:34`），**存不了多行长文本**，prompt 不能走这条路。
- **无数据库迁移机制**：`init_db` 仅 `create_all`（`app/db.py:30-33`），对已存在的表不会加列。E1/E5 涉及 `Video` 加列，必须先补轻量迁移。
- **前端现状**：无路由库（`App.jsx` 内 useState 切视图，URL 不反映状态）；`styles.css`（1737 行）仅暗色一套 token（`:root`，1-37 行），无亮色主题；4 个 `@media` 断点均为桌面优先；问答答案为纯文本 `pre-wrap`（AskView），未走 Markdown 渲染；移动端存在 `100vh` 地址栏、无底部导航、触控目标 <44px、hover-only 元素触屏不可见等问题。
- **依赖极简**：前端仅 react / react-dom / react-markdown / remark-gfm；后端已有 openai SDK、httpx、yt-dlp。

### 0.3 总体原则

1. **向后兼容**：所有新列可空、新表独立、新配置有默认值；老数据/老库升级后功能不回退。
2. **渐进降级**：新增的采集步骤（评论、视觉旁路）失败一律只记日志不 fail 整个视频，与现有 fetch 降级链哲学一致。
3. **单容器约束**：不新增常驻服务；OCR 走 ONNX CPU 推理；VLM 为可选云 API。
4. **配置三层**：环境变量/.env → runtime.env（Web 在线改）→ 代码默认值。prompt 走独立文件（见 E2）。

### 0.4 公共基础设施（M0，先行）

两项被多个模块依赖的机制先做：

**(a) 轻量列迁移** —— `app/db.py` 的 `init_db` 后追加：

```python
async def _ensure_columns(engine):
    """SQLite 轻量迁移：对已存在的表补齐 ORM 中新增的列（幂等）。"""
    _REQUIRED = {
        "videos": {
            "description": "TEXT",
            "cover_url": "TEXT",
            "upload_date": "TEXT",
            "view_count": "INTEGER",
            "like_count": "INTEGER",
            "tags": "JSON",
            "comment_count": "INTEGER",
            "meta_source": "TEXT",
        },
    }
    async with engine.begin() as conn:
        for table, cols in _REQUIRED.items():
            existing = await conn.execute(text(f"PRAGMA table_info({table})"))
            names = {r[1] for r in existing}
            for col, ddl in cols.items():
                if col not in names:
                    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
```

新表（`Comment`、`QueryHistory`）由 `create_all` 自动创建，无需迁移。迁移表驱动而非硬编码 SQL 文件：字段与 ORM 定义一一对应，缺失即补，幂等可重跑。

**(b) PromptRegistry** —— 见 E2。

---

## 1. E1：视频元数据采集与展示

### 1.1 功能设计

**采集范围**（一次 yt-dlp 调用全部拿齐）：

| 字段 | 来源（yt-dlp info dict） | 用途 |
|------|--------------------------|------|
| 标题 title | `title` | 视频库卡片主标题；笔记/检索的 title 来源（替代 URL 兜底） |
| 作者 author | `uploader` / `channel` | 卡片与详情页展示 |
| 简介 description | `description` | 详情页展示；笔记生成上下文（E2 变量） |
| 封面 cover_url | `thumbnail` | 视频库卡片缩略图（本地缓存） |
| 时长 duration_sec | `duration` | 已有字段，补写 |
| 发布日期 upload_date | `upload_date`（YYYYMMDD） | 卡片/详情展示 |
| 观看数 view_count | `view_count` | 详情页展示 |
| 点赞数 like_count | `like_count` | 详情页展示 |
| 标签 tags | `tags`（list） | 详情页展示；可选进检索 meta |
| 评论 comments | `comments`（需 `--write-comments`） | 热评 top N 进笔记上下文（E3 无语音视频的补充层） |

**展示设计**：

1. **视频库（LibraryView）卡片升级**：
   - 左侧 16:9 封面缩略图（96×54，本地缓存，无封面时平台色占位块显示平台首字母）；
   - 标题 2 行截断（已有）+ 作者 / 时长 / 发布日期 + 观看数（有则显示）；
   - 状态徽章与进度条保留。
   - 卡片信息密度提升后，用户扫一眼即可判断"哪条笔记值得点开"。
2. **详情页（VideoDetail）新增「简介与热评」区块**（Note Tab 顶部）：
   - 简介折叠展示（默认收起，超 4 行省略）；
   - 热评 top 5（按点赞排序）：作者 / 点赞数 / 内容（超长省略）；
   - tags 以 chip 形式展示。
3. **笔记生成增强**：笔记 prompt 注入 `{{title}} {{author}} {{description}} {{top_comments}}` 变量（E2 提供），标题不再是 URL。
4. **同类信息的延伸设计（一并纳入）**：
   - `Chunk.meta`（已有 JSON 列）写入时补充 `author/tags`，检索命中可展示作者（**不改 LanceDB 表 schema**——老表新列会导致 schema 不一致，meta 走 SQLite 侧即可）；
   - 详情页头部加「原视频 ↗」跳转（已有 watchUrl 能力，入口前移）；
   - 采集失败（如 B 站风控）时 `meta_source` 标记 `none`，前端显示"元数据未采集"，不影响笔记与检索。

**评论的使用策略（明确边界）**：
- **进笔记上下文**：top 5 热评（按 like_count 降序）注入笔记 prompt——评论常包含纠错、补充、高光时间点等众包信息；
- **不默认进向量库**：评论噪声大、与时间轴无关，入 RAG 会污染检索质量。预留配置 `COMMENTS_TO_INDEX`（默认 0 = 不入库），v2 只实现笔记上下文用途，入库留作后续实验开关（代码留 hook，不实现）。

### 1.2 技术设计

**(a) fetcher 层：新增元数据抓取函数**

`app/core/fetchers/ytdlp.py` 新增：

```python
async def fetch_metadata(url: str, cookie_dir: str) -> dict:
    """一次 yt-dlp 调用抓取视频元数据 + 限量评论。

    - 子进程：yt-dlp --dump-json --skip-download --no-playlist
              --write-comments --max-comments 100
              --cookies <douyin/xhs 时，复用现有 _cookie_args>
    - --no-playlist：B站多分P URL 只取当前分P（dump-json 只输出一行）
    - --max-comments 100：限制评论翻页量（B站评论翻页是 fetch 阶段最慢环节）
    - 解析 stdout 每行 JSON，取第一个对象
    - 超时 120s（超时/失败抛 FetchMetadataError，调用方隔离）
    """
```

实现要点：
- 复用现有 `run_ytdlp()` 子进程通道与 cookie 逻辑（`ytdlp.py:125-139`），仅参数不同；
- 返回归一化 dict：`{title, author, duration, description, thumbnail, upload_date, view_count, like_count, tags, comments: [{author, text, like_count, published_at}], comment_count}`；各平台字段缺失时置 None；
- description 做 8000 字符截断（B站部分视频简介极长）。

**(b) 数据模型**（`app/models.py`）

`Video` 追加列（迁移见 0.4a）：

```python
description: Mapped[str | None] = mapped_column(Text, default=None)
cover_url: Mapped[str | None] = mapped_column(default=None)
upload_date: Mapped[str | None] = mapped_column(default=None)   # "YYYYMMDD"
view_count: Mapped[int | None] = mapped_column(default=None)
like_count: Mapped[int | None] = mapped_column(default=None)
tags: Mapped[list | None] = mapped_column(JSON, default=None)
comment_count: Mapped[int | None] = mapped_column(default=None)
meta_source: Mapped[str | None] = mapped_column(default=None)    # ytdlp|none
```

新表：

```python
class Comment(TimestampMixin, Base):
    __tablename__ = "comments"
    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), index=True)
    author: Mapped[str | None] = mapped_column(default=None)
    text: Mapped[str] = mapped_column(Text)
    like_count: Mapped[int] = mapped_column(default=0)
    published_at: Mapped[str | None] = mapped_column(default=None)
    video: Mapped[Video] = relationship(back_populates="comments", lazy="selectin")
# Video 侧加 comments relationship
```

**(c) pipeline 集成**（`app/jobs/pipeline.py`）

`process_video` 在 `fetch_with_fallback` **之前**插入元数据步骤（同属 `fetching` 状态，不动 progress.py 的 STAGES）：

```python
await _save(session_factory, video_id, status="fetching")
meta = None
try:
    meta = await fetch_metadata(url, cookie_dir)          # 新增
    await _save_metadata(session_factory, video_id, meta)  # Video 更新 + Comment 批量插入
except Exception as e:
    log.warning("metadata fetch failed for %s: %s", video_id, e)  # 隔离：不 fail 视频
# 后续 fetch/transcribe 降级链照旧
```

`_save_metadata` 要点：Comment 插入前先删除该 video_id 旧评论（幂等，支持将来重试）；`title` 为空时才回填 URL 兜底逻辑保持不变。

**(d) 缩略图本地缓存（绕 B 站图床防盗链）**

B 站等平台图床校验 Referer，前端 `<img>` 直接外链会 403。设计为后端代理 + 磁盘缓存：

- 新端点 `GET /api/videos/{id}/thumbnail`（`app/api/videos.py`）：
  1. 查 `data_dir/thumbnails/{video_id}.jpg`，命中直接 `FileResponse`；
  2. 未命中且 `Video.cover_url` 存在 → `httpx` 下载（按平台设置 Referer：bilibili → `https://www.bilibili.com`，其余不设），写盘后返回；
  3. 无 cover_url / 下载失败 → 404，前端显示 SVG 占位（平台色 + 首字母，纯 CSS/SVG 实现，不请求网络）。
- 下载加 10s 超时，失败缓存空文件 24h（避免每次列表都打外网）——存 `thumbnails/{id}.miss` 标记文件，过期重试。
- `data_dir` 子目录注册：`app/db.py:15` 的 `SUBDIRS` 加 `thumbnails`。

**(e) API 变更**（`app/api/videos.py`）

- 列表/详情序列化 dict（`videos.py:40-54`、`:64-75`）追加字段：`description, cover_url, upload_date, view_count, like_count, tags, comment_count, has_thumbnail`（bool，前端决定 img src 用 `/api/videos/{id}/thumbnail` 还是占位）；
- 详情端点追加 `comments`（top 5，按 like_count 排序）：
  `GET /api/videos/{id}` → `{"...", "comments": [{"author","text","like_count","published_at"}]}`。

**(f) 笔记生成接线**（依赖 E2）

`pipeline.py:86` 调用 `generate_note` 时，从 Video 行读取 `description` + top 5 评论，构造 `note_context` 传入（详见 E2 的变量表）。

### 1.3 验证方法

**单元测试**（新增 `tests/test_metadata.py`）：

| 用例 | 断言 |
|------|------|
| dump-json stdout 解析 | 归一化 dict 各字段映射正确（B站/YouTube 两种样例 JSON fixture） |
| 字段缺失容错 | 无 like_count/tags 的平台返回 None，不抛错 |
| `_save_metadata` 幂等 | 重复调用不产生重复 Comment |
| 元数据失败隔离 | `fetch_metadata` 抛错后 `process_video` 继续走 fetch 链，视频状态不为 failed |
| 迁移幂等 | 老库（无新列）跑 `init_db` 后新列存在且默认 NULL；跑两次不报错 |
| thumbnail 端点 | 无 cover → 404；有 cover mock 下载 → 200 + 文件落盘；二次请求命中缓存 |

**手动验证**：
1. 提交一个 B 站视频 + 一个 YouTube 视频，全流程跑完后检查：视频库卡片显示封面/标题/作者/发布日期；详情页显示简介与热评；
2. `sqlite3 data/db/videorag.db "select title, author, view_count from videos"` 确认落库；
3. 生成的笔记 markdown 中体现标题（而非 URL）；
4. 提交一个 cookie 失效的抖音链接：确认元数据失败但转写照常、前端有"元数据未采集"标记；
5. 断网状态下看视频库：封面占位块正常，无请求阻塞。

---

## 2. E2：提示词可配置

### 2.1 功能设计

**可配置模板（5 个键）**：

| 键 | 作用 | 默认值 |
|----|------|--------|
| `note_system` | 笔记生成的系统指令（风格/角色/输出要求） | `notes.py:33-41` 现有内容 |
| `note_context` | 笔记附加上下文模板，支持变量，渲染后拼入 user 段 | 见下方默认模板 |
| `note_map_system` | 长文分块笔记（map 阶段）系统指令，高级项 | `notes.py:54-65` 现有内容 |
| `note_reduce_system` | 长文合并（reduce 阶段）系统指令，高级项 | `notes.py:77-87` 现有内容 |
| `qa_system` | RAG 问答系统指令（回答风格） | `qa.py:15-18` 现有内容 |

**变量表（占位符 `{{name}}`，仅 note_context / 各 system 尾部可用）**：

| 变量 | 含义 | 渲染时机 |
|------|------|----------|
| `{{title}}` | 视频标题 | 笔记 |
| `{{author}}` | 作者 | 笔记 |
| `{{description}}` | 视频简介（截断 2000 字符） | 笔记 |
| `{{top_comments}}` | 热评 top 5（作者+内容+点赞） | 笔记 |
| `{{question}}` | 用户问题 | 问答 |
| `{{n_references}}` | 本次检索命中条数 | 问答 |

转写正文与参考资料**不作为可编辑模板变量**（体量大、格式复杂，仍由代码拼接），模板只控制"指令与上下文"。

**设置页交互**（SettingsView 新增「提示词」标签页）：
- 左侧模板列表（5 项 + "高级"折叠收纳 map/reduce 两项），右侧 textarea（等宽字体、行号可选）；
- 变量说明常驻侧栏；「插入变量」点击插入光标处；
- **预览**：用假数据渲染当前模板，展示"最终发给 LLM 的完整 prompt"（system + user 拼接效果），保存前可确认；
- **恢复默认**：单模板恢复 / 全部恢复；
- 保存成功后即时生效（下一次提问/生成笔记即用新模板），无需重启。

### 2.2 技术设计

**(a) 存储：`$DATA_DIR/prompts.json`（不走 runtime.env）**

理由：runtime.env 每键单行（`runtime_config.py:34`），无法存多行 prompt；JSON 文件天然支持长文本、原子写（临时文件 + `os.replace`）、与"由 Web 界面维护"的既有约定一致。

```json
{
  "note_system": "你是视频笔记助手。……（覆盖时才出现该键）",
  "note_context": "",
  "qa_system": ""
}
```

- 文件不存在 / 键缺失 / 值为空串 → **回落代码内置默认值**（默认值常量集中在 `app/core/prompts.py` 的 `DEFAULTS`）；
- 只存用户改过的键，"恢复默认" = 删除该键。

**(b) 新模块 `app/core/prompts.py`：PromptRegistry**

```python
class PromptRegistry:
    """提示词模板注册表：默认值回落 + {{var}} 渲染 + 热更新。"""
    def __init__(self, data_dir: str): ...
    def get(self, key: str) -> str: ...           # 自定义 or DEFAULTS[key]
    def set(self, key: str, value: str): ...      # 校验后写 prompts.json
    def reset(self, key: str): ...
    def render(self, key: str, **vars) -> str: ...  # {{var}} 替换；未知占位符原样保留
```

渲染实现用正则替换而非 `str.format`（prompt 内含大量 JSON 花括号示例，format 会冲突）：

```python
def render(self, key, **vars):
    tpl = self.get(key)
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(vars.get(m.group(1), m.group(0))), tpl)
```

未知变量原样保留（不静默吞掉，便于用户在预览中发现拼写错误）。

**(c) 接线：调用点从 components 现取（与现有热替换模式一致）**

- `main.py` 的 `components` dict 加 `"prompts": PromptRegistry(settings.data_dir)`；`process_handler` 把它传给 `process_video`；
- `notes.py` 的 `build_note_prompt / build_note_map_prompt / build_note_reduce_prompt` 各加 `prompts: PromptRegistry | None = None` 参数：
  - system 文本改为 `prompts.render("note_system", ...) if prompts else DEFAULTS[...]`；
  - user 段在正文前拼接 `note_context` 渲染结果（非空时）；
  - 保持函数签名向后兼容（测试可不传）；
- `qa.py` 的 `build_qa_prompt` 同理加 `prompts` 参数，system 用 `qa_system` + 尾部拼 `qa_context` 渲染（`{{question}}/{{n_references}}`）；
- `app/api/search.py:36-40`（/ask 调用点）从 `request.app.state.components["prompts"]` 现取——**每次请求读最新值**，天然热更新（PromptRegistry 内部做 mtime 缓存判断，文件被手工编辑也能生效）。

**(d) API：`app/api/prompts.py` 新路由**

| 端点 | 说明 |
|------|------|
| `GET /api/prompts` | 返回 `{templates: {key: {value: 当前生效, default: 默认值, customized: bool}}}` |
| `PUT /api/prompts` | body `{key: value}` 部分键更新；校验：值 ≤ 8000 字符、非纯空白 |
| `POST /api/prompts/reset` | body `{key}` 或 `{}`（全部），删除自定义键 |
| `POST /api/prompts/preview` | body `{key, value}`，用样例数据渲染并返回拼接后的完整 prompt |

**(e) 笔记默认上下文模板**（`note_context` 默认值，体现 E1 元数据注入）：

```
视频信息：
- 标题：{{title}}
- 作者：{{author}}
- 简介：{{description}}

热门评论（供理解视频背景与观众关注点参考，笔记中不要直接罗列评论）：
{{top_comments}}
```

### 2.3 验证方法

**单元测试**（新增 `tests/test_prompts.py`）：

| 用例 | 断言 |
|------|------|
| 默认回落 | 空 prompts.json / 缺键 → get 返回 DEFAULTS |
| render 变量替换 | `{{title}}` 替换正确；未知 `{{foo}}` 原样保留 |
| render 与 JSON 示例共存 | 模板含 `{"summary": ...}` 花括号不被破坏 |
| set/reset 持久化 | 写入→新实例读取生效；reset 后回落默认 |
| 并发写安全 | 原子替换（os.replace），无半截文件 |
| notes/qa 接线 | 传 prompts 与不传（None）输出等价于旧行为（回归保护） |
| PUT 校验 | 超长/空白值 422 |

**手动验证**：
1. 设置页把 `qa_system` 改为"用英文、口语化风格回答"→ 提问 → 回答风格变化；
2. `note_context` 保持默认 → 生成笔记 → 笔记 markdown 标题正确（非 URL）；
3. 预览按钮：改动 map 模板后预览显示完整拼接效果；
4. 恢复默认 → 重启容器 → 行为回到内置模板；
5. 运行期手改 `prompts.json`（模拟用户手工编辑）→ 下一次请求生效。

---

## 3. E3：无语音视频信息采集（视觉旁路管线）

### 3.1 调研结论（决策依据）

| 方案 | 成熟度 | 成本 | CPU/NAS 可跑 | 结论 |
|------|--------|------|-------------|------|
| Gemini Flash 直读视频（File API ≤2GB / YouTube URL 直读） | 高 | ~$0.28–0.46 / 小时视频（258 token/秒） | 无需本地算力 | **云增强首选**（海外网络） |
| 阿里 Qwen-VL / 智谱 GLM-4.6V（OpenAI 兼容图片/视频输入） | 高 | qwen-vl-flash ≤0.0015 元/千 token；GLM-4.6V-Flash 免费 | 无需本地算力 | **云增强首选**（国内网络，GLM-4.6V-Flash 可零成本试跑） |
| ffmpeg 场景检测抽关键帧 + RapidOCR（ONNX） | 很高 | 0 | **是**（RapidOCR 比 Paddle CPU 快 4–5 倍） | **本地层核心** |
| Qwen2.5/3-VL 本地部署（vLLM） | 模型成熟/部署门槛高 | 0 API 费但需 GPU（16–20GB 显存） | 否 | 排除（无 GPU 的 NAS） |
| OpenAI GPT / Claude 原生视频输入 | 无此能力 | — | — | 排除（仅能抽帧当图片） |
| Twelve Labs（$2.50/小时索引 + 存储/查询费） | 高 | Gemini 的 5–8 倍，且向量数据锁在第三方 | 无需 | 排除（与自托管冲突） |

行业共识（Video-RAG 论文 arXiv:2411.13093 等）：无语音视频最有效的信息载体是 **OCR 文本 + 画面描述文本**，即"视觉对齐辅助文本"——恰好能拼回本项目现有的「带时间戳文本 → 笔记 → 向量」管线。

**选型定案（方案 A 混合管线）**：本地层（抽帧 + OCR，零成本、CPU 可跑、对教程/PPT/代码类视频信息回收率高）为必选；云 VLM 画面描述为可选增强（配置开关，OpenAI 兼容接口，一套代码适配 Qwen-VL/GLM-4V/任意视觉模型）；热评/弹幕作第三层补充（复用 E1 评论）。

### 3.2 功能设计

**触发**：转写完成后检测"有效语音密度"：

```
语音密度 = segments 文本总字数 / duration_sec * 60
判定无语音：density < 10 字/分钟（可配 VISUAL_MIN_WPM）
             或转写结果为空且时长 > 60s
```

（阈值对齐现有经验：whisper 对静音段会幻觉出"字幕由 Amara.org 提供"等短文案，10 字/分钟以下基本可判非语音内容。）

**三层旁路**（检测结果为"无语音"时依次执行，每层独立开关、独立容错）：

1. **本地视觉层（默认开）**：
   - ffmpeg 场景检测抽关键帧（`select=gt(scene,0.35)`）+ 每 30s 固定兜底帧（教程类画面变化慢，纯场景检测帧数不足）；
   - 感知哈希（pHash）去重相邻相似帧（同一 PPT 页只留一帧）；
   - RapidOCR 逐帧识别画面文字，帧间文本 diff 去重；
   - 产出：`[{start_sec, end_sec, text, src:"ocr"}]`。
2. **云 VLM 层（可选，默认关）**：
   - 对去重后的关键帧（上限 60 帧，超出均匀采样）逐帧/批量发 OpenAI 兼容 `/v1/chat/completions`（图片 base64），生成画面描述；
   - 产出：`[{start_sec, end_sec, text, src:"vlm"}]`。
3. **评论补充层（默认开，依赖 E1）**：
   - 已采集的热评交给笔记 LLM 作背景上下文（无需单独处理，走 E2 的 `{{top_comments}}`）。

**合并入主流水线**：OCR/VLM 结果按时间戳排序，与（稀疏的）语音转写合并为统一 segments 流 → 现有 chunker / 笔记 / 向量入库**零改动复用**。Segment 不加列，OCR/VLM 来源记在 `Chunk.meta.src`（笔记与检索可区分来源；检索排序暂不区分，观察后再定加权）。

**状态与可见性**：视频详情页转写 Tab 中，视觉段显示 `🖼`（OCR）/ `💬`（VLM）标记；无语音视频处理时长会显著增加（抽帧+OCR），进度仍走 transcribing 阶段。

**配置项**（`config.py` 新增，进 `.env.example`）：

| 变量 | 默认 | 说明 |
|------|------|------|
| `VISUAL_PIPELINE` | `auto` | `off` / `auto`（检测到无语音才走）/ `always`（强制，用于调试） |
| `VISUAL_MIN_WPM` | `10` | 无语音判定阈值（字/分钟） |
| `VISUAL_MAX_FRAMES` | `60` | 送 OCR/VLM 的最大帧数 |
| `VLM_BASE_URL` / `VLM_API_KEY` / `VLM_MODEL` | 空 | OpenAI 兼容视觉模型三件套（如百炼 qwen-vl-flash、智谱 GLM-4.6V-Flash）；留空 = 云层关闭 |

### 3.3 技术设计

**新模块 `app/core/vision/`**：

```
app/core/vision/
├── __init__.py
├── detect.py      # is_speechless(transcript) -> bool（密度判定）
├── keyframes.py   # extract_keyframes(video_path, workdir, max_frames) -> list[KeyFrame{path, pts_sec}]
├── phash.py       # dhash 图像指纹（Pillow 实现，~20 行，不引第三方 imagehash）
├── ocr.py         # RapidOCR 懒加载单例 + ocr_frame(path) -> str
└── vlm.py         # describe_frame(path, client) -> str（OpenAI 兼容，base64 image_url）
```

**关键实现细节**：

1. **抽帧命令**（`keyframes.py`，两级）：

```bash
# 场景切换帧（带时间戳）
ffmpeg -i in.mp4 -vf "select='gt(scene,0.35)',showinfo" -vsync vfr -frame_pts true frames_%04d.jpg
# 日志中 grep pts_time 取每帧时间戳
# 兜底：每 30s 一帧
ffmpeg -i in.mp4 -vf "fps=1/30" -frame_pts true fixed_%04d.jpg
```

两路合并 → pHash 去重（汉明距离 > 8 视为不同）→ 均匀采样至 `max_frames`。兜底帧时间戳用 `-frame_pts` 从文件名/PacketPts 还原，或直接按序号 × 30s 计算（实现取简单者）。

2. **OCR**（`ocr.py`）：`rapidocr-onnxruntime` 加入 pyproject 依赖（约 15MB，模型内置于包）；进程内懒加载单例（首次调用下载/加载，与 fastembed 同模式）；纯函数 `ocr_frame` 返回去空格拼接文本。**帧间去重**：与上一帧 OCR 结果做 `difflib.SequenceMatcher` 相似度 > 0.9 则跳过（同一 PPT 页）。

3. **VLM**（`vlm.py`）：复用现有 `openai` SDK，`AsyncOpenAI(base_url, api_key)`；消息格式：

```python
[{"role": "user", "content": [
    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    {"type": "text", "text": "用一两句话描述这帧画面中的关键信息（操作、物体、场景）。"
                             "若有屏幕文字请摘录要点。直接输出描述，不要客套。"},
]}]
```

并发 4 路（`asyncio.Semaphore`），单帧超时 30s，失败跳过该帧（不 fail 视频）。

4. **pipeline 接线**（`app/jobs/pipeline.py` 的 `transcribe_with_fallback` 之后）：

```python
transcript, _used = await transcribe_with_fallback(media, transcribers)
if settings.visual_pipeline != "off":
    forced = settings.visual_pipeline == "always"
    if forced or is_speechless(transcript):
        visual_segs = await run_visual_pipeline(media, workdir, settings)  # 抽帧→OCR→VLM
        transcript = merge_transcripts(transcript, visual_segs)  # 按时间戳排序合并
await _store_segments(...)
```

`run_visual_pipeline` 内部各层独立 try/except（OCR 失败但 VLM 可用则只用 VLM，反之亦然）；仅当 media 为视频文件且有 ffmpeg 时可用（音频降级链抓到的纯音频无画面，直接跳过）。

5. **merge 语义**：OCR/VLM 段的 `end_sec = 下一视觉段的 start_sec`（或视频时长），与现有 ASR segments end 修正逻辑（commit d20aa67）一致；`Chunk.meta` 写入 `{"title", "platform", "author", "src"}`。

6. **prompt 适配**：`note_system` 默认模板追加一句"转写中标注 [OCR]/[VLM] 的行来自画面文字与画面描述，没有语音时间轴精度，时间引用以行首秒数为准"。

### 3.4 验证方法

**单元测试**（新增 `tests/test_vision.py`）：

| 用例 | 断言 |
|------|------|
| 无语音判定 | 空 segments / 密度 5 字/分 → True；正常转写 → False |
| pHash 去重 | 相同图 → 距离小；不同图 → 距离大（用测试图片 fixture） |
| OCR 帧间去重 | 相似度 0.95 的两段文本 → 第二帧跳过 |
| merge_transcripts | 语音段与视觉段按时间排序、end_sec 修正正确 |
| VLM 失败隔离 | mock 客户端抛错 → 视觉段为空，视频不 failed |
| VISUAL_PIPELINE=off | 不触发旁路（mock extract_keyframes 断言未调用） |

**手动验证**：
1. 准备样本：纯音乐 MV（无对白）、PPT 录屏教程（大量画面文字）、正常口播视频（对照）；
2. 提交后检查：转写 Tab 出现 OCR/VLM 标记段且时间戳合理；笔记包含画面文字要点；检索"画面里的关键词"能命中；
3. 配 `VISUAL_PIPELINE=off` 重跑 → 无视觉段，回归原行为；
4. 配 VLM 三件套（GLM-4.6V-Flash 免费）重跑 → 画面描述出现，检查 API 费用/耗时日志；
5. 对照组：正常口播视频（密度 > 10 字/分）确认不触发旁路、耗时无回归。

---

## 4. E4：UI 优化与移动端适配

### 4.1 功能设计

**设计目标**：视觉更亲和（亮色主题、更软的形态）、手机可用（底部导航 + 触控友好）、阅读舒适（排版与行宽）。

**(a) 主题系统**
- 亮 / 暗 / 跟随系统三态切换（侧栏底部 + 移动端顶栏入口，`localStorage` 持久化）；
- 默认跟随系统（`prefers-color-scheme`）；`index.html` 加内联引导脚本防刷新闪白（FOUC）；
- 亮色主题基调：`bg #f6f7fb / surface #ffffff / text #1a2233 / accent #4f6ef7`；暗色沿用现有色板微调（accent 提亮至 `#6d9bff`）。

**(b) 移动端布局（<768px）**
- 顶栏：品牌 + 主题切换 + 状态点；
- **底部 Tab Bar**：5 个入口（复用现有 `NAV` 数组），`safe-area-inset-bottom` 适配手势条；
- 内容区单列全滚动；侧边栏与右侧 Aside 在手机上移除（Aside 的"处理中"信息移入各视图顶部）；
- 触控目标统一 ≥44px；`@media (hover: none)` 下 hover-only 元素（卡片"查看笔记"、引用跳转按钮等）改为常显。

**(c) 阅读体验**
- **问答答案改走 Markdown 渲染**（复用现有 `<Markdown>` 组件，AskView 唯一大的体验缺口）；
- 笔记/答案排版：正文 15px、行高 1.75、段落间距 1em、容器行宽 `max-width: 72ch`、标题层级字号差拉大（h1 22 / h2 18 / h3 16）；
- `VideoDetail` 弹窗改造：头部 sticky + 内容区独立滚动（现在 backdrop 整体滚，长笔记头部跟着消失）；窄屏改为全屏 sheet（`inset: 0` + 底部滑入动画）；转写列表 460px 固定高度改弹性；
- 引用时间戳跳转、复制、下载等操作保持。

**(d) 信息架构与导航**
- **hash 路由**：`#/submit /#/ask /#/search /#/library /#/settings` + `#/v/{video_id}`（详情弹窗），自写 ~30 行 `useHash` hook；刷新保持视图、手机返回键关弹窗而非退出、详情可分享链接；
- 视图切换加淡入过渡（`prefers-reduced-motion` 时关闭所有动画）；
- 图标从文本字形升级为内联 SVG（`components/Icon.jsx`，手写 10 个左右路径，不引图标库）。

**(e) 视觉亲和力细节**
- 卡片圆角 12px（现 8px）、间距刻度统一（4/8/12/16/24/32）、柔和阴影（亮色主题 `0 1px 3px rgba(16,24,40,.08)`）；
- 空状态：SVG 插画感图形 + 引导文案（现在只有文字）；
- 加载态：视频库卡片骨架屏（CSS shimmer，替代空白）；
- Toast 加成功/错误图标。

### 4.2 技术设计

**文件改动清单**（按工作量排序）：

| 文件 | 改动 |
|------|------|
| `web/src/styles.css` | 主战场：token 扩充、亮色主题块、移动端媒体查询重写（移动优先）、`.md` 排版、sheet/modal、tab bar |
| `web/src/App.jsx` | 布局重构：主题 state、hash 路由、移动端顶栏/底部 TabBar、Aside 响应式 |
| `web/src/components/VideoDetail.jsx` | 全屏 sheet 化、sticky 头、内滚动、`#/v/{id}` 接线 |
| `web/src/views/AskView.jsx` | 答案 `<Markdown>` 渲染；历史面板（E5） |
| `web/src/views/SettingsView.jsx` | 「提示词」标签页（E2）；分组导航响应式 |
| `web/src/views/LibraryView.jsx` | 卡片信息升级（封面/元数据，E1）、骨架屏 |
| `web/src/components/Markdown.jsx` | 可选：标题锚点 |
| `web/src/components/Icon.jsx` | 新增：内联 SVG 图标集 |
| `web/src/hooks/useHash.js` | 新增：hash 路由 hook |
| `web/index.html` | `theme-color` meta、主题引导脚本 |

**设计 token 扩充**（`:root` 在现有 37 行基础上补齐，不推翻现有变量名）：

```css
:root {
  /* 现有颜色/radius 变量保留为暗色基底 */
  --space-1: 4px;  --space-2: 8px;  --space-3: 12px;
  --space-4: 16px; --space-5: 24px; --space-6: 32px;
  --text-xs: 12px; --text-sm: 13px; --text-md: 14px;
  --text-base: 15px; --text-lg: 17px; --text-xl: 20px;
  --measure: 72ch;
  --shadow-sm: 0 1px 2px rgba(16,24,40,.06);
  --shadow-md: 0 2px 8px rgba(16,24,40,.10);
  --z-nav: 40; --z-modal: 100; --z-toast: 200;
  --tap: 44px;
}
[data-theme="light"] { /* 覆盖颜色变量 */ }
```

**关键实现要点**：
- 视口高度：`100dvh` + `@supports not (height: 100dvh) { height: 100vh }` 兜底；
- 主题切换：`document.documentElement.dataset.theme`，引导脚本在 `index.html` `<head>` 内联（读 localStorage / matchMedia，先于 CSS 渲染）；
- hash 路由：`useHash()` 返回 `[hash, setHash]`，监听 `hashchange`；`#/v/{id}` 打开 VideoDetail（复用现有 activeId 状态，open 时 `setHash`，Esc/关闭时回退）；
- 底部 TabBar 与 Sidebar 共用 `NAV` 配置，CSS 控制各自显示（`@media (max-width: 768px)` 切换），JS 不重复实现；
- 触控目标：`.icon-btn` 等统一 `min-height/width: var(--tap)`；
- 骨架屏：`.vc-skeleton` 类，`animation: shimmer`，`prefers-reduced-motion` 下静态。

**不做的事**（明确边界）：不引 UI 框架/Tailwind/路由库/图标库；不做 PWA 离线；不改 api.js 数据层。

### 4.3 验证方法

**手动 + 浏览器验证**（结合项目已有 browser-use 截图习惯，存 `screenshots/v2-*.png`）：
1. **三档视口截图回归**：1920px / 768px / 375px 各视图（Submit/Ask/Search/Library/Settings/详情弹窗/设置提示词页），亮暗两主题，逐张与 v1 截图对比；
2. **手机模拟**：DevTools iPhone SE 尺寸——底部 Tab 可达、弹窗全屏、内容滚动、地址栏收展不破版（dvh）；
3. **真机抽验**：局域网内手机访问 NAS 实例，走完提交→看进度→读笔记→提问全流程；
4. **阅读体验**：长笔记弹窗滚动时头部固定；问答答案渲染 markdown（列表/代码块/表格）；行宽不超 72ch；
5. **无障碍/偏好**：`prefers-reduced-motion` 下无动画；键盘快捷键回归（Ctrl+K / Ctrl+Enter / Alt+1-5 / Esc / ?）；
6. **hash 路由**：`#/library` 刷新停留；`#/v/{id}` 直开详情；手机返回键关弹窗。
7. 亮暗切换无闪烁（刷新页面观察首帧）。

---

## 5. E5：历史提问与检索记录

### 5.1 功能设计

- **记录范围**：`/api/ask`（问答）与 `/api/search`（检索）两类操作，服务端持久化（跨设备、清缓存不丢）；
- **记录内容**：问题原文、类型、top_k、答案摘要（ask）、命中数（search）、时间、使用次数；
- **交互**：
  - Ask / Search 页输入框聚焦时下方浮现「最近提问/检索」面板（默认 5 条，可展开全部）；
  - 点击历史项 → 回填输入框并自动执行；
  - 每条可单独删除；设置页提供「清空历史」；
  - **防重复提示**：输入时本地前缀/包含匹配已有历史，在面板顶部高亮"你之前问过：…"（点击直接看当时的答案，不发新请求）；
- **去重规则**：同类型 + 完全相同 query 再次执行 → 不新增记录，更新 `hit_count + last_used_at`（列表按 last_used_at 排序，高频问题上浮）；
- **保留策略**：每类默认保留 200 条，超限删除最旧（环形淘汰），上限可配 `HISTORY_LIMIT`。

### 5.2 技术设计

**(a) 数据模型**（`app/models.py` 新表，create_all 自动建，无需迁移）：

```python
class QueryHistory(TimestampMixin, Base):
    __tablename__ = "query_history"
    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column()          # ask | search
    query: Mapped[str] = mapped_column(Text)
    top_k: Mapped[int | None] = mapped_column(default=None)
    answer: Mapped[str | None] = mapped_column(Text, default=None)      # ask：答案前 2000 字
    citations_json: Mapped[list | None] = mapped_column(JSON, default=None)  # ask：citations 原样
    hits_count: Mapped[int | None] = mapped_column(default=None)        # search：命中条数
    hit_count: Mapped[int] = mapped_column(default=1)                   # 相同问题执行次数
    last_used_at: Mapped[datetime] = mapped_column(default=utcnow)
```

索引：`(kind, last_used_at)` 复合索引（列表查询路径）；`(kind, query)` 唯一索引（去重依据，query 截断前 500 字符入唯一键——SQLite 唯一索引对长 TEXT 可行，但为稳妥对 `query_key = kind + "\x00" + query[:500]` 建普通列 + 唯一索引）。

**(b) API**（`app/api/history.py` 新路由）：

| 端点 | 说明 |
|------|------|
| `GET /api/history?kind=ask&limit=50` | 按 last_used_at 倒序；返回 `{id, kind, query, top_k, answer(截断), hits_count, hit_count, last_used_at, created_at}` |
| `GET /api/history/{id}` | 单条完整记录（含 citations_json，用于"看当时的答案"） |
| `DELETE /api/history/{id}` | 删除单条 |
| `DELETE /api/history?kind=ask` | 清空某类 |

**(c) 写入点**：`app/api/search.py` 的 `/ask` 与 `/search` handler 成功返回前写入（try/except 包裹，历史写入失败不影响主请求）；去重：`SELECT id FROM query_history WHERE kind=? AND query_key=?` 命中则 UPDATE hit_count/last_used_at/answer；超限清理在同事务内 `DELETE ... ORDER BY last_used_at LIMIT -1 OFFSET 200`。

**(d) 前端**：
- `web/src/api.js` 加 `listHistory/getHistory/deleteHistory/clearHistory`；
- 新组件 `components/HistoryPanel.jsx`（Ask/Search 共用，props: kind, onPick）：
  - 输入框聚焦 + 内容为空时展示最近 5 条；展开按钮看全部（modal 列表）；
  - 防重复提示：当前输入非空时，按包含匹配历史 query，顶部显示匹配项；
  - 点击历史项：ask 类直接调 `GET /api/history/{id}` 展示当时的答案 + citations（不发新 LLM 请求），同时提供"重新提问"按钮；
- AskView 的答案区结构不变（历史回看复用同一渲染组件，标注"来自历史记录 · 2 小时前"）。

**(e) MCP 不暴露历史**（外部 agent 无此需求，v2 不做）。

### 5.3 验证方法

**单元测试**（新增 `tests/test_history.py`）：

| 用例 | 断言 |
|------|------|
| ask/search 写入 | 请求后 query_history 出现对应 kind 记录，answer/citations 正确 |
| 去重 | 同一问题问两次 → 单条记录，hit_count=2，last_used_at 更新 |
| 淘汰 | 插入 201 条 → 最旧被删 |
| 删除/清空 | 单条 DELETE、按类 DELETE 生效 |
| 写入失败隔离 | mock DB 抛错 → 主请求仍 200 |

**手动验证**：
1. 提问两次相同问题 → 历史面板一条、显示"问过 2 次"；
2. 点击历史项 → 显示历史答案与引用（无新请求，Network 面板确认）→ 点"重新提问"发起新请求；
3. Search 页同样验证；删除单条、清空生效；
4. 换浏览器/清 localStorage 后历史仍在（服务端存储）。

---

## 6. 实施计划

### 6.1 里程碑与依赖

```
M0 基础设施（0.5d）── 迁移机制 + PromptRegistry 骨架
  ├─ M1 E2 提示词配置（1d）       依赖 M0
  │    └─（note_context 变量就绪，供 M2 注入）
  ├─ M2 E1 元数据采集（2d）       依赖 M0（迁移）；笔记注入依赖 M1
  │    └─（评论就绪，供 M5 补充层）
  ├─ M3 E5 历史记录（1d）         依赖 M0（新表，其实仅依赖 create_all，可与 M1 并行）
  ├─ M4 E4 UI 改版（2.5d）        独立；其中设置页提示词 Tab 依赖 M1，
  │                                 Library 卡片升级依赖 M2 的 API 字段
  └─ M5 E3 视觉旁路（2.5d）       依赖 M2（评论补充层 + Chunk.meta 扩展）；本地层可先行
```

### 6.2 每里程碑任务分解

| 里程碑 | 任务 | 产出 |
|--------|------|------|
| M0 | ① `_ensure_columns` 迁移 + 测试 ② `app/core/prompts.py` DEFAULTS 抽取（把 notes.py/qa.py 现有 prompt 原样搬入，行为零变化） | 迁移可跑；prompt 有了唯一出处 |
| M1 | ① prompts.json 读写 + 渲染 ② `/api/prompts` 四端点 ③ notes.py/qa.py 接线（加 prompts 参数）④ SettingsView 提示词 Tab（基础版，预览可后置到 M4 打磨） | E2 全功能可用 |
| M2 | ① `fetch_metadata` + 归一化 ② Video 加列迁移 + Comment 表 ③ pipeline 接线 + 异常隔离 ④ thumbnail 代理端点 ⑤ videos API 字段扩展 ⑥ VideoCard/VideoDetail 前端展示 | E1 全功能可用 |
| M3 | ① QueryHistory 表 ② history API + 写入/去重/淘汰 ③ HistoryPanel 组件 + Ask/Search 接线 | E5 全功能可用 |
| M4 | ① token 扩充 + 亮色主题 + 主题切换 ② 移动端布局（TabBar/dvh/safe-area/触控） ③ `.md` 排版 + Ask 答案 Markdown 化 + VideoDetail sheet 化 ④ hash 路由 ⑤ Icon/骨架屏/空状态/预览打磨 | E4 全功能可用 |
| M5 | ① `vision/detect.py` + pipeline 触发 ② keyframes + phash ③ RapidOCR + 帧间去重 ④ merge + Chunk.meta.src ⑤ vlm.py 云层（可选配置） ⑥ 转写 Tab 来源标记 | E3 全功能可用 |

**总计约 9.5 人日**。每个里程碑独立可交付、可单独发布（后端 API 先行，前端随后），建议按 M0→M1→M2→M3→M4→M5 串行推进，M3 可与 M2 并行。

### 6.3 配置项汇总（进 `.env.example` 与 README）

| 新增变量 | 默认 | 模块 |
|----------|------|------|
| `VISUAL_PIPELINE` | `auto` | E3 |
| `VISUAL_MIN_WPM` | `10` | E3 |
| `VISUAL_MAX_FRAMES` | `60` | E3 |
| `VLM_BASE_URL` / `VLM_API_KEY` / `VLM_MODEL` | 空 | E3 |
| `HISTORY_LIMIT` | `200` | E5 |
| `COMMENTS_TO_INDEX` | `0` | E1（预留，v2 不实现） |

### 6.4 新增依赖

- 后端：`rapidocr-onnxruntime`（E3，M5 时才加，避免提前膨胀镜像）；
- 前端：无新增。

---

## 7. 风险与注意事项

| 风险 | 影响 | 缓解 |
|------|------|------|
| 评论抓取慢（B站翻页）/ 被风控 | fetch 阶段变长 | `--max-comments 100` + 120s 超时 + 异常隔离（不影响主流程）；已有 cookie 机制复用 |
| B站图床防盗链 | 前端外链封面 403 | 后端代理下载 + 本地缓存 + Referer 设置（已设计） |
| `Video` 加列后 ORM 与老库不一致 | 升级用户查询报错 | M0 轻量迁移先行，幂等可重跑；发布说明注明"升级前备份数据卷" |
| LanceDB 老表 schema 不兼容 | 新字段入库失败 | **不动 LanceDB 行 schema**，扩展信息全走 SQLite 侧（Chunk.meta JSON） |
| prompts.json 被手工编辑坏 JSON | 启动/读取失败 | 读取 try/except 回落默认值 + 日志告警；原子写防半截文件 |
| yt-dlp 平台改版导致 dump-json 字段变化 | 元数据缺失 | 归一化层全部字段可空；`meta_source` 标记采集成败，前端有兜底展示 |
| RapidOCR 首次加载/依赖体积 | 镜像变大、首次转写变慢 | 懒加载单例（同 fastembed 模式）；依赖仅在 M5 加入 |
| VLM 云调用费用/网络 | 用户意外花费 | 默认关闭；README 明示计费量级（百帧 ≈ 几美分） |
| 移动端 `100dvh` 兼容旧浏览器 | 布局异常 | `@supports` 兜底 100vh |
| 历史记录含敏感提问 | 隐私 | 数据在本机 SQLite；提供清空入口（设置页 + 面板内） |

---

## 8. 测试与验证总表

| 模块 | 单测文件 | 核心用例数 | 手动验证入口 |
|------|----------|-----------|--------------|
| M0 迁移 | `tests/test_db_migrate.py` | 3 | 老数据卷挂载启动 |
| E2 提示词 | `tests/test_prompts.py` | 8 | 设置页改 prompt → 提问/生成笔记 |
| E1 元数据 | `tests/test_metadata.py` | 6 | B站/YouTube 真实视频全流程 |
| E5 历史 | `tests/test_history.py` | 5 | 重复提问/回看/删除 |
| E4 UI | —（项目前端无测试基建，不引入） | — | 三视口截图 + 真机 |
| E3 视觉 | `tests/test_vision.py` | 6 | 无语音样本视频 × 三种配置 |
| 回归 | 现有 110 个测试全绿 | 110 | `pytest` 每里程碑跑 |

**发布验收（全量）**：
1. 老数据卷（v1 库）挂载启动 → 迁移日志出现、老视频可看、新功能可用；
2. 全新数据卷从提交到提问完整走通一个 B 站视频 + 一个无语音视频（视觉旁路开启）；
3. 375px 视口完整走通「提交→进度→笔记→提问→历史回看」；
4. `pytest` 全绿 + `npm run build` 产物由后端托管正常。
