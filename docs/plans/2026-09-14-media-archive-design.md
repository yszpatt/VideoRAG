# videoRAG 已下载媒体归档（Media Archive）功能设计

> 目标：把摄入流水线中**已实际下载到本地**的音频/视频留存一份副本到可配置目录，
> 便于用户取用原始素材；默认关闭，未配置时行为与旧版**完全一致**。
> 规划以 **Docker 为主要运行环境**，同时兼容裸机 / 开发环境。

日期：2026-09-14
状态：已实现（含单元测试）
关联文档：
- `docs/plans/2026-08-31-video-rag-design.md`（v1 高层设计）
- `docs/plans/2026-08-31-video-rag-technical-design.md`（v1 技术设计）
- `app/core/fetchers/ytdlp.py`（下载落点，本设计的改动前提）
- `app/jobs/pipeline.py`（摄入流水线与临时文件清理）

---

## 0. 概述

### 0.1 背景与现状（已核实）

| 维度 | 现状 | 本设计目标 |
|------|------|-----------|
| 下载链路 | `YtdlpFetcher.fetch()` 降级顺序 **字幕 → 音频 → 视频**（`ytdlp.py:317`），调用方 `pipeline.py:115` 传空 workdir，依赖 `db.ensure_dirs()` 的 `os.chdir(data_dir)`，产物落在 `/data/{sub.*,audio.mp3,video.mp4}` | 不改下载参数与降级顺序 |
| 视频是否一定下载 | **否**。只有「无字幕且无音频」才会真正下视频；有语音的绝大多数视频只下 mp3 | 只归档「已下载到的」媒体，不额外下载 |
| 视觉旁路 | E3 命中无语音时经 `fetch_video()` 另下 `bestvideo*+bestaudio` 到 `/data/frames/<video_id>/` | 该文件同样纳入归档（仅"另行下载"的情形） |
| 留存 | **无**。`process_video` 的 `finally` 删音频/视频（`pipeline.py:155`）、`rmtree` frames；启动 `sweep_temp_media()` 清空 `downloads/audio/transcripts/frames` | 在删除前复制一份副本 |
| 配置/部署 | 仅 `DATA_DIR` / `COOKIE_DIR`；compose 只挂 `./data:/data` | 新增 `MEDIA_SAVE_DIR` + compose `/media` 挂载 |

### 0.2 需求映射（用户原话 → 设计落点）

| # | 用户需求 | 决策 | 落点 |
|---|---------|------|------|
| 1 | 把下载之后的视频保存在指定文件夹 | 保留**已下载媒体**（音频 mp3／视频 mp4），不额外强制下载完整视频 | §2.1、§3.2 |
| 2 | 配置中增加一个下载目录选项 | 新增 `MEDIA_SAVE_DIR`（env/compose，空=关闭） | §3.1 |
| 3 | Docker 挂载增加一个本地磁盘映射 | compose 增加 `${MEDIA_SAVE_DIR_HOST:-./downloads}:/media` | §3.5 |
| 4 | 命名/去重 | 按标题命名 + video_id 保底；已存在则跳过 | §2.2 |
| 5 | 设置页可见 | 只读回显，不提供在线编辑 | §3.4 |

### 0.3 范围界定（Important）

**做**：归档已下载的音频/视频；文件名清洗；存在即跳过；异常隔离；容器挂载；设置页只读回显；文档。

**不做**：额外/强制下载完整视频（用户明确选择，避免带宽与耗时翻倍）；把归档写入数据库（文件内嵌 video_id 已足够反查，避免表结构变更与迁移）；设置页在线修改该配置（需重启容器，走 env）；清理/轮转策略（由用户自行管理宿主目录）。

---

## 1. 核心决策与理由

1. **配置项命名与落点**：`app/config.py` 的「目录与运行」段新增 `media_save_dir: str = ""`（env `MEDIA_SAVE_DIR`）。空 = 关闭，与 `cookie_dir` 同形态，便于 `main.py` 从 `components["settings"]` 现取透传。
2. **独立归档模块**：新建 `app/core/media_archive.py`，把「文件名清洗 + 目标路径计算 + 存在即跳过 + 复制 + 异常隔离 + 临时目录冲突防护」收敛为纯函数，便于单测，且不污染 `pipeline.py` 的流程逻辑。
3. **归档时机 = 临时文件删除前**：主管道放在 `process_video` 的 `finally` 中、`os.remove` 之前——这样**成功与失败路径**下已完整下载的文件都能留存，且峰值磁盘占用只在末尾短暂翻倍（复制完立刻删临时）。视觉旁路则在 `fetch_video` 之后、`run_visual_pipeline` 之前归档，只处理「另行下载」的情形，避免与主路径重复。
4. **复制而非移动**：使用 `shutil.copy2`（保留 mtime）。播放器/转写/抽帧仍需要原临时文件，不做「移动 + 改 `media.path`」的侵入式改造。
5. **不落库**：不新增表/列，不触碰 `db.py` 的 `SUBDIRS`（避免默认场景多建空目录）。文件名内嵌 `video_id` 保证可反查。
6. **只读回显不接入可写组**：`GET /api/settings` 在 `GROUPS` 遍历之外追加独立 `storage` 段，**不加进 `GROUPS`**；`PUT` 天然忽略该键（`SettingsUpdate` 未声明 `storage`，pydantic v2 默认 extra ignore），无需改动 PUT 分支与请求模型。

---

## 2. 功能设计

### 2.1 归档范围与时机

| 场景 | 下载产物 | 是否归档 |
|------|---------|---------|
| 有字幕 | `sub.vtt/srt` | 否（字幕文件保留在 `/data`，不进归档目录） |
| 无字幕有语音 | `audio.mp3`（`AUDIO_EXTS`） | 是（`process_video` finally） |
| 无音频需视频 | `video.mp4`（`VIDEO_EXTS`） | 是（`process_video` finally） |
| E3 视觉旁路无语音 | `/data/frames/<id>/video.mp4` | 是（`_visual_bypass`，仅另行下载时） |

### 2.2 命名与去重

- 文件名：`<sanitize(标题)>-<video_id><源扩展名>`；标题为空 → `<video_id><扩展名>`
- `sanitize_filename()`：非法字符（`<>:"/\|?*` + 控制符）→ `_`；连续空白折叠；截断 80 字符；去首尾空白与点号
- 扩展名取**源文件真实后缀**，使 mp3/mp4/webm 不会互相覆盖
- 目标文件已存在 → 跳过（`logger.info`），重复导入/重试不重复占盘

### 2.3 异常隔离与防护

- 源文件缺失、目录创建失败、复制失败、路径非法 → `logger.warning` 并返回 `None`，**绝不影响**转写/笔记/入库
- **临时目录冲突防护**：若归档目录解析后位于 `data_dir` 下的 `downloads`/`audio`/`transcripts`/`frames` 内，拒绝归档并告警——否则下次启动 `sweep_temp_media()` 会把归档删光
- 启动时（`main.py` lifespan）若已配置目录，则 `mkdir` + 写探针校验，提前暴露「卷没挂 / 属主不对」

---

## 3. 实现要点

### 3.1 配置层（`app/config.py`）

```python
media_save_dir: str = ""   # 空=不保存（默认）；建议容器内绝对路径（如 /media）
```

### 3.2 归档工具（`app/core/media_archive.py`，新增）

```python
TEMP_MEDIA_SUBDIRS = ("downloads", "audio", "transcripts", "frames")

def sanitize_filename(title, max_len=80) -> str: ...
def build_archive_path(save_dir, video_id, title, src_path) -> Path: ...
def is_in_temp_media_dir(target_dir, data_dir) -> bool: ...
def resolve_archive_dir(save_dir, data_dir=None) -> Path | None: ...
def ensure_archive_dir(save_dir, data_dir=None) -> bool: ...          # 启动校验
def archive_media(src_path, save_dir, video_id, title, data_dir=None) -> str | None: ...
```

> `TEMP_MEDIA_SUBDIRS` 与 `pipeline._TEMP_MEDIA_DIRS` 内容一致但**独立定义**：pipeline 依赖本模块，反向 import 会形成循环依赖。

### 3.3 流水线接线（`app/jobs/pipeline.py`）

- `process_video` 新增 keyword 参数 `media_save_dir: str | None = None`（置于 `cookie_dir` 之后，**不进入前 6 个位置参数**，保证既有调用点与测试零改动兼容）
- `finally` 中在 `os.remove(media.path)` **之前**调用 `archive_media(...)`
- `_visual_bypass` 新增 `media_save_dir` / `title` 参数，`downloaded_here` 为真时归档

### 3.4 设置接口（`app/api/settings.py` + `web/src/views/SettingsView.jsx`）

- `GET /api/settings` 追加 `out["storage"] = {"media_save_dir": s.media_save_dir, "enabled": bool(...)}`，不加入 `GROUPS`
- `SettingsUpdate` 与 `PUT` 分支**不改动**
- 前端在设置分组下方新增只读展示区（读取 `form.storage`，不参与 `buildServicePayload` 快照差异）

### 3.5 部署（`docker-compose.yml` / `.env.example`）

```yaml
volumes:
  - ./data:/data
  - ${MEDIA_SAVE_DIR_HOST:-./downloads}:/media
environment:
  MEDIA_SAVE_DIR: "${MEDIA_SAVE_DIR:-}"
```

宿主目录需与 `VIDEORAG_UID/VIDEORAG_GID` 属主一致（`mkdir -p ./downloads && chown "$(id -u)":"$(id -g)" ./downloads`）；默认不设 `MEDIA_SAVE_DIR` 时功能关闭，挂载仅产生一个空目录。

---

## 4. 影响面（Blast Radius）

| 类别 | 位置 | 变更 |
|------|------|------|
| 新增 | `app/core/media_archive.py`、`tests/test_media_archive.py`、本设计文档 | 全新 |
| 修改 | `app/config.py`（+1 字段）、`app/jobs/pipeline.py`（参数 + finally + 视觉旁路）、`app/main.py`（透传 + 启动校验）、`app/api/settings.py`（GET +storage）、`web/src/views/SettingsView.jsx`（只读展示区） | 小改 |
| 修改 | `docker-compose.yml`、`.env.example`、`README.md` | 部署与文档 |
| **必须同步** | `tests/test_settings_api.py:8` 顶层键全等断言 | 追加 `storage` 后需更新 |
| 不受影响 | `YtdlpFetcher` 下载参数与降级顺序、`sweep_temp_media()`、`db.SUBDIRS`、既有 `process_video` 调用点与测试（新增参数带默认值） | 零变化 |

**默认关闭（`MEDIA_SAVE_DIR=""`）时**：下载落点、清理逻辑、启动清扫、接口契约（除 GET 多一个只读段）、既有测试行为全部不变。

---

## 5. 回滚方式

清空 `MEDIA_SAVE_DIR`（并 `docker compose up -d` 重启）即回到旧行为；无数据迁移、无表结构变更、无既有文件被改写。归档目录内的历史文件可自行删除。
