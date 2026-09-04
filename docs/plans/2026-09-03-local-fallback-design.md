# videoRAG 本地降级（Local Fallback）功能技术设计

> 目标：当 ASR / Embedding 的**远程服务参数未配置**时，自动降级到本仓库可自持的本地服务与模型；
> 在设置页提供本地服务开关、模型下载 / 重试 / 取消 / 进度展示、手动指定本地模型文件能力；
> 全部规划以 **Docker 为主要运行环境**，同时兼容裸机 / 开发环境。

日期：2026-09-03（v2：ASR 运行时改为 sherpa-onnx int8，已实测验收）
状态：已评审定稿（含 L0–L3 开发授权）
关联文档：
- `docs/plans/2026-08-31-video-rag-design.md`（v1 高层设计）
- `docs/plans/2026-08-31-video-rag-technical-design.md`（v1 技术设计）
- `docs/plans/2026-09-02-v2-enhancement-design.md`（v2 增强设计，引用其「单容器约束」原则）
- `deploy/asr/`（sherpa-onnx SenseVoice OpenAI 兼容服务，本设计 L3 部署资产）
- `deploy/sensevoice/README.md`（旧 FunASR 集中式服务部署说明，仅远程形态保留）

---

## 0. 概述

### 0.1 一句话目标

| 维度 | 现状（已核实） | 本设计目标 |
|------|---------------|-----------|
| ASR 转写 | `factory.py` 注释明确"集中式宕机→本地回退未实现，规划中"；未配远程时装配的是本地 **faster-whisper large-v3**（`whisper.py`，下载数 GB、CPU 慢），且**无任何模型下载管理 UI** | 未配置远程时降级到 **sherpa-onnx + SenseVoiceSmall int8**（`deploy/asr/`，OpenAI 兼容，模型 229 MB，**已实测：RTF 0.078 / RSS ~670 MB / 句级 segments**），提供下载/进度/重试/手动路径管理 |
| Embedding | `config.py:36` 默认即本地 `fastembed` + `BAAI/bge-small-zh-v1.5`（ONNX ≈92 MB），但当前 compose 被覆盖为远程 ollama bge-m3；`embedder.py` 注释"远程不可用→本地回退未实现" | 明确"未配置远程 → 本地 fastembed"决策逻辑；模型可下载/预置/手动指定；切换 embedding 模型时给库重建指引 |
| 配置/设置页 | 设置页有 llm/asr/embedding/retrieval 四组在线配置（`SettingsView.jsx`），无模型管理入口 | 新增「本地模型」Tab：开关、下载/重试/取消、进度、手动模型路径、生效链路预览 |
| 部署 | 单容器镜像 `python:3.11-slim` + ffmpeg，`/data` 卷；models 目录已预留（`config.py:84 models_dir=/data/models`，现为空目录） | docker-compose 新增**可选**本地 ASR sidecar 服务与共享模型卷；裸机照旧跑 `deploy/sensevoice` |

### 0.2 需求映射（用户原话 → 设计落点）

| # | 用户需求 | 设计章节 |
|---|---------|---------|
| 1 | 未配置 asr / embedding 服务参数时，使用本地相关服务降级 | §1 决策选型、§3.1 运行档位判定、§3.2 装配 |
| 2 | ASR 本地服务（最初建议 FunASR+SenseVoice；评审后改为更轻量的 sherpa-onnx int8 运行时） | §1.1、§3.6 部署资产、`deploy/asr/` |
| 3 | Embedding 自行选择常见、资源占用小的模型 | §1.2（推荐 bge-small-zh-v1.5） |
| 4 | 设置功能：本地服务开关 / 模型下载 / 重试 / 进度展示 / 手动指定本地模型文件 | §2 功能设计、§3.3~§3.5 |
| 5 | 项目主要跑 Docker，注意兼容性规划 | §1.3、§3.6、§4 |

### 0.3 范围界定（Important）

- **本设计"降级"的触发条件 = 未配置远程参数**（`CLOUD_ASR_*` / 远程 `EMBED_*` 为空）。
  已配置但远程**运行期宕机**的自动 failover 属 L2 扩展（见 §7.4），不在本版范围，但装配结构为其预留位置。
- 本地 ASR 采用 **OpenAI 兼容 HTTP 服务**形态（进程/容器），复用现有 `CloudASRTranscriber`（`cloud_asr.py` 已实现 60s 分块、二分重试、无 segments 兜底、ffmpeg 转 wav），**不改转写客户端链路**。
- Embedding 本地 = fastembed ONNX CPU 推理，**进程内**加载（现有 `Embedder` 已支持）。
- 不动 LLM 与检索策略。

### 0.4 现状核对（写文档前已逐一核实，开发前应复查）

| # | 事实 | 位置 |
|---|------|------|
| 1 | 远程 ASR 配置存在 → 链路 `[Subtitle, CloudASR]`；否则 → `[Subtitle, WhisperTranscriber(large-v3)]`；注释自述"自动降级机制尚未实现，规划中" | `app/core/factory.py:14-34` |
| 2 | Embedder 二选一：`provider=fastembed` 本地 ONNX / 其它走远程 OpenAI 兼容；注释自述"远程不可用→本地 fastembed 回退未实现" | `app/core/embed/embedder.py:48-49` |
| 3 | 默认配置即本地：`embed_provider=fastembed`、`embed_model=BAAI/bge-small-zh-v1.5`、`models_dir=/data/models`（models 目录已存在且为空） | `app/config.py:36-39, 84-85` |
| 4 | Settings 便捷属性 `embed_enabled`：fastembed 恒 True；openai 需 base_url。`cloud_asr_provider` 空串为"未配置"判据 | `app/config.py:107-117` |
| 5 | settings API 组字段：asr 组直连 `CLOUD_ASR_*`，embedding 组直连 `EMBED_*`；PUT 写 runtime.env + 热重建组件 | `app/api/settings.py:18-22, 112-162` |
| 6 | runtime.env 持久化与优先级：环境变量 > runtime.env > 默认值；`apply_runtime` env→字段通用映射 | `app/core/runtime_config.py`、`app/config.py:119-145` |
| 7 | 云端 ASR 客户端已具备健壮性：切 ≤60s 块、失败二分重试、`{"error":...}` 显式检查、无 segments 整段兜底（用 ffprobe 时长） | `app/core/transcribers/cloud_asr.py:219-344` |
| 8 | 现有本地 ASR 档是 faster-whisper large-v3（`faster_whisper.WhisperModel`，int8 CPU，`download_root=models_dir`），无下载管理/进度出口，首次调用才下载（卡事件循环风险已被 to_thread 规避） | `app/core/transcribers/whisper.py:30-54` |
| 9 | 视频流水线对 transcriber 链"按序尝试，空结果/异常自动降级下一档"已实现 | `app/jobs/pipeline.py:48-65` |
| 10 | compose 中 videorag 为远程三件套显式配置（LLM ollama / EMBED openai bge-m3 / CLOUD_ASR sensevoice@9991 Windows 机） | `docker-compose.yml:12-52` |
| 11 | 前端设置页：TABS=服务配置/提示词；服务组由 GROUPS 数组驱动渲染 | `web/src/views/SettingsView.jsx:5-39, 41-44` |
| 12 | `deploy/sensevoice/` = 上游 jackuh105/openai-sensevoice-stt 的**打了时间戳补丁**版服务（Windows/GPU 资产，ModelScope 缓存 D:\…）；上游仓库本身**已带 Dockerfile + docker-compose** | `deploy/sensevoice/README.md`、上游 GitHub |

---

## 1. 决策与选型（含依据）

### D1. ASR 本地引擎：sherpa-onnx + SenseVoiceSmall int8（OpenAI 兼容服务）

- 运行时：**sherpa-onnx**（k2-fsa，纯 C++/ONNX 推理，无 torch/funasr/modelscope 依赖，Python wheel 自带 onnxruntime 内核，完全离线）。
- 模型：**SenseVoiceSmall int8 ONNX**（`model.int8.onnx` = **229 MB** + `tokens.txt` 309 KB，ModelScope `poloniumrock/SenseVoiceSmallOnnx` 提供 int8 转换镜像；官方同源 GitHub `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17`）。
  - 中文/粤语/英/日/韩，自带 ITN（标点与逆文本正则化，实测"二零二四"→"2024"）。
  - **词级 timestamps**（`result.timestamps` 逐 token 对齐，冒烟实测 4.2s 音频全量词级时间戳）——服务端据此聚合句级 segments（见下），不依赖 fsmn-vad。
- **选型对比（替代 FunASR 决策依据）**：

| 候选 | 模型体积 | 依赖/镜像 | CPU RTF（实测） | 时间戳 | 结论 |
|------|---------|----------|----------------|--------|------|
| **sherpa-onnx + SenseVoice int8（采用）** | **229 MB** | 仅 sherpa-onnx wheel（自带 onnx 内核），镜像可 <600 MB | **0.078**（6 线程，24.9min 真实音频实测） | 词级原生 | ✅ 最轻、零网络依赖、无 SDK 联网检查坑 |
| FunASR + SenseVoiceSmall | 940 MB + VAD | torch CPU 3~4 GB 镜像、modelscope SDK（有启动联网检查的已知坑） | ~0.06（社区标称，未实测） | 需 fork 补丁 | 否决：重、部署慢 |
| faster-whisper large-v3（现状） | 数 GB | faster-whisper + ctranslate2 | 慢（大模型 CPU） | 句级 | 保留为 `ASR_FALLBACK=whisper` 可选档 |

- OpenAI 兼容实现：**本仓 `deploy/asr/server.py`（已编写并实测通过）**——FastAPI 外壳 + `sherpa_onnx.OfflineRecognizer.from_sense_voice()`：
  - `POST /v1/audio/transcriptions`（multipart，`response_format=json|verbose_json`），verbose_json 返回带 `segments[{start,end,text}]` 的**句级**结果；
  - 句级 segments 由词级时间戳聚合：句末标点（。！？!?；;…）或静音 gap > 0.6s 断句（与旧补丁口径一致）；
  - 长音频服务端按 ≤60s block 分块解码、偏移合并——与 `CloudASRTranscriber` 客户端 60s 分块策略天然兼容；
  - `GET /health`、`GET /v1/models`；上传音频统一经 ffmpeg 转 wav 16k mono f32。
- **实测验收（2026-09-03，24.9min 真实中文口播视频音频）**：295 个句级 segments / 8650 字，无坏段无重复，60s block 边界衔接无缝；服务端解码 117s → RTF=0.078；稳态 RSS ~670 MB（峰值 ~900 MB）、22 线程、模型加载 2.49s；无 torch、无 GPU。质量/性能/资源三项达标，作为 L0 装配的本地降级运行时。
- **与 `CloudASRTranscriber` 的兼容**：本地端点同样被 `CloudASRTranscriber` 调用（provider=`local-sensevoice`，不含 "whisper" → 不发送 faster-whisper 专有参数），客户端 60s 分块/二分重试/无 segments 整段兜底全部继承，**转写客户端链路零改动**。

### D2. Embedding 本地引擎：fastembed + `BAAI/bge-small-zh-v1.5`

选型对比（均要求资源占用小、中文可用、可离线）：

| 模型 | 维度 | ONNX 体积 | 中文表现 | 许可证 | fastembed 一等支持 |
|------|-----|----------|---------|--------|-------------------|
| **BAAI/bge-small-zh-v1.5（推荐）** | 512 | ≈92 MB（`model_optimized.onnx` 92,560 KB） | C-MTEB 中文第一梯队 | MIT | ✅（`hf: Qdrant/bge-small-zh-v1.5`） |
| bge-base-zh-v1.5 | 768 | ≈400 MB | 略好 | MIT | ✅ |
| paraphrase-multilingual-MiniLM-L12-v2 | 384 | ≈220 MB | 通用 50+ 语种，中文一般 | Apache-2.0 | ✅ |

- 结论：**维持 `config.py` 既有默认 `BAAI/bge-small-zh-v1.5` 不动**——它已是本项目选型（512 维对 RAG 足够、92 MB 最小档、中文强、MIT），无需更换，设计重点在**下载管理与本地加载落地**。
- 本地加载能力（fastembed Python 版，需核实现装版本，`pyproject.toml:17` 约束 `fastembed>=0.5`）：
  - ≥0.2.7：`local_files_only=True` 可禁用联网校验，从 cache 加载；
  - ≥0.6.0：官方 `specific_model_path=...` 直接指定本地模型目录（`config.json`+`model.onnx`/`model_optimized.onnx`+`tokenizer.json`+`tokenizer_config.json`+`special_tokens_map.json`）；
  - 缓存命中法（最稳、版本无关）：把模型快照预置到 `cache_dir/models--Qdrant--bge-small-zh-v1.5/`（HF hub 标准结构），fastembed 探测到即不联网。
  - 设计默认采用**缓存命中法**（下载器产出 HF hub 结构），并升级依赖下限 `fastembed>=0.6` 以获得 `specific_model_path` 作为"手动指定路径"的兜底（见 §3.4）。

### D3. 本地 ASR 的部署形态（Docker 兼容性核心决策）

| 候选 | 说明 | 评估 |
|------|------|------|
| **A. docker-compose sidecar（推荐，Docker 形态）** | 新增可选 `asr` 服务（独立轻量镜像：python:slim + sherpa-onnx，无 torch），经 compose profile 按需拉起；与 videorag 共享模型卷 | ✅ 主镜像保持轻量、服务可单独重启/扩并发；贴合"项目主要跑 Docker"。sherpa-onnx 使 sidecar 镜像从 FunASR 方案的 3~4 GB 降到 ~600 MB 级 |
| B. 主容器内嵌（同镜像内 spawn 服务子进程） | 保持"单容器"，`docker run` 也能用 | ❌ 主镜像被 sherpa-onnx+onnxruntime 污染且服务与 API 进程耦合，违背 v2「镜像极简」；sherpa-onnx wheel 体积虽小但无必要内嵌。**否决** |
| C. 维持纯远程集中式（现状） | 依赖局域网 192.168.x.x 那台 Windows 机 | 保留为"已配置远程"时的主用路径，不因本功能移除 |

- **双形态结论**：
  - Docker 环境：`docker compose --profile local-asr up` 拉起 `videorag + asr` 两容器，videorag 内部经 `http://asr:9991` 访问本地 SenseVoice；**不暴露宿主端口**（容器网络内访问），避免局域网裸奔。
  - 裸机/开发环境：用本仓 `deploy/asr/`（已实测可跑），videorag 经 `http://127.0.0.1:9991` 访问。
  - videorag 侧**不区分**两种形态，只认 `LOCAL_ASR_BASE_URL`（见 §3.1）。

### D4. 模型下载实现：自研 HTTP 流式下载器（httpx），不引入 modelscope/huggingface SDK

| 项 | 结论 | 理由 |
|----|------|------|
| ASR 模型源 | ModelScope 仓库 `poloniumrock/SenseVoiceSmallOnnx`（`model.int8.onnx` + `tokens.txt` 两个文件） | 官方同源 int8 转换，国内可达（本项目现网即从 ModelScope 拉取）；无需 VAD（sherpa-onnx 词级时间戳不依赖 fsmn-vad） |
| 文件清单 | 静态清单即可（2 文件，不随仓库演进漂移）；启动下载时仍可 `GET https://modelscope.cn/api/v1/models/{repo}/repo/files?Recursive=true&PageSize=100` 复核 | int8 转换仓文件稳定，不硬编码百级文件清单；Sha256 用于完整性校验 |
| 下载链路 | `https://modelscope.cn/models/{repo}/resolve/master/{Path}` 直链，httpx 流式落盘 `<dest>.part`，完成后改名 | 与项目既有 httpx 依赖一致；可精确字节级进度；`.part` 断点续传（服务端支持 Range，206） |
| Embedding 模型源 | HuggingFace `Qdrant/bge-small-zh-v1.5`（fastembed 官方 hf 源），`HF_ENDPOINT` 可切镜像（预设 `https://hf-mirror.com`） | 国内直连 HF 不稳，镜像可配 |
| Embedding 下载器 | 首推 `huggingface_hub.snapshot_download`（fastembed 传递依赖已存在）到 `models_dir` 的 HF hub 缓存；进度按"阶段+文件数"汇报（小模型 92 MB 大头仅一个文件） | 产出的缓存结构与 fastembed 命中判定天然一致，避免手写 blob/snapshot 结构出错 |
| 不引入 modelscope SDK | ASR 下载自研直链；避免主镜像被 modelscope（含较多传递依赖）污染 | 与"镜像极简"一致；SDK 版本冲突风险最低 |

> sherpa-onnx 无 FunASR 那类"启动联网检查"的坑，模型文件就绪即可离线加载（冒烟实测断网可用）。

### D5. 模型目录与共享卷规划

- 统一落点：`<DATA_DIR>/models`（即 `config.py models_dir`，容器内 `/data/models`）。
- 子目录布局（两类缓存结构天然共存，无冲突）：

```
videorag-data/models/
├── asr/
│   ├── model.int8.onnx                        # SenseVoice int8（229 MB）
│   └── tokens.txt                             # BPE tokens
└── models--Qdrant--bge-small-zh-v1.5/         # HF hub 缓存结构（embedding，fastembed 命中即用）
    ├── blobs/ snapshots/ refs/
```

- Docker：该目录由 videorag 容器以 rw 挂载（下载写入）；asr sidecar 以 `--model /models/asr/model.int8.onnx --tokens /models/asr/tokens.txt` 指向**同一磁盘目录**（ro 亦可，保守给 rw），离线加载不联网。
- 语义边界：**谁负责下载谁写**（videorag）；asr sidecar 只在启动时发现模型缺失则报可读错误并退出（提示去设置页下载），不自行联网下载（避免双写竞争）。

---

## 2. 功能设计（设置页「本地模型」）

### 2.1 入口与页面结构

- 设置页 TABS 从 2 个扩为 3 个：`服务配置 / 本地模型 / 提示词`（新 Tab 插在中间）。`SettingsView.jsx:41-44` 扩展；独立组件 `LocalModelsPanel.jsx` 承载。
- 顶部说明条（对当前**保存后**配置的实时推导，见 §3.1 档位判定）：
  - 例："ASR：当前使用 **远程** SenseVoice@192.168.x.x → 未配置远程参数时自动降级本地 SenseVoice；Embedding：当前使用 **远程** bge-m3 → 未配置时使用本地 fastembed bge-small-zh-v1.5"。

### 2.2 本地 ASR 卡片

| 元素 | 行为 |
|------|------|
| 状态徽章 | 就绪（模型已装 + 服务可达）/ 未下载 / 下载中 x%（可并发显示）/ 校验失败 / 服务不可达（侧车未启） |
| 开关"启用本地降级" | 关 = 永不降级（未配置远程时直接报"未配置 ASR"）；开（默认）= 未配置远程时使用本地。写 `ASR_FALLBACK`（见 §3.1），保存后热重建 transcribers |
| 模型行 | `SenseVoice int8（poloniumrock/SenseVoiceSmallOnnx，model.int8.onnx 229 MB + tokens.txt）`；按钮：下载 / 取消 / 重试 / 删除 |
| 下载进度条 | 字节级（% + 已下/总量 + 速度）；失败红条 + 原因（网络/磁盘/校验 sha 不符），可重试（续传 `.part`） |
| 手动模型路径 | 输入框 + 保存："手动指定本地模型文件目录"（如 `/data/models/asr` 或外部 NFS 挂载目录）。有值时跳过内置下载管理，仅做完整性校验（`model.int8.onnx` + `tokens.txt` 存在） |
| 服务地址（只读） | 显示生效的本地端点：`LOCAL_ASR_BASE_URL`（Docker=asr:9991 / 裸机=127.0.0.1:9991）；带"测试连通性"按钮（GET `/v1/models` 或 `/health`） |

### 2.3 本地 Embedding 卡片

| 元素 | 行为 |
|------|------|
| 状态徽章 | 就绪 / 未下载 / 下载中 / 校验失败 |
| 模型行 | `bge-small-zh-v1.5（Qdrant ONNX）` + 维度 512；显示已装大小；按钮：下载/取消/重试/删除 |
| 下载进度 | 文件级进度（3 个文件左右，大头 model_optimized.onnx 单文件 92 MB）；失败可重试 |
| 手动模型路径 | 保存后 Embedder 优先 `specific_model_path`（fastembed≥0.6）加载；路径校验必需文件存在 |
| 切库提示（重要） | 若当前向量库由其它 embedding 模型建（如远程 bge-m3 1024 维），显示黄色警示："本地 bge-small-zh 与现有向量库模型不一致，切换后**新入库会失败/检索不可用**，需重建知识库"。提供"前往知识库重建"入口（v1 至少给指引文案；重建 API 属 §7 扩展） |

### 2.4 全局规则

- **下载触发**：仅手动（按钮）触发；不做后台自动偷跑大下载（ASR 229 MB / Embedding 92 MB）。流水线运行时若发现"已启用本地降级但模型未装"，转写/入库前给出可读错误："请先在 设置→本地模型 下载 SenseVoice 模型"（不静默失败）。
- **幂等/并发**：同一 kind 同一时间只有一个下载任务（服务端互斥），按钮随之禁用。
- **开关与参数联动**：开启"本地降级"且当前无远程配置时，保存即重建组件并立即生效（沿用 `settings.py` PUT 热重建机制）。
- **删除模型**：二次确认对话框（误删需重新下载 ~229 MB）。

---

## 3. 技术设计

### 3.1 配置层（Settings 扩展）

新增 env 字段（全部小写映射进 pydantic Settings，`apply_runtime` 通用映射无需改）：

```python
# config.py 新增（注释与现有风格一致）
asr_fallback: str = "sensevoice"     # 本地降级档位：sensevoice（默认）/ whisper（保留档）/ none（禁用降级）
local_asr_base_url: str = ""         # 本地 SenseVoice OpenAI 兼容端点。空=按形态默认：
                                     #   环境变量可注入（compose 给 http://asr:9991）；
                                     #   空且形态未知时回退 http://127.0.0.1:9991（见 asr_local_endpoint 属性）
local_asr_model_dir: str = ""        # 手动指定本地 SenseVoice 模型目录（空=用 models_dir 自动管理）
local_embed_model_dir: str = ""      # 手动指定本地 fastembed 模型目录（空=用 cache_dir 自动管理）
embed_download_endpoint: str = ""    # HF 镜像端点；空=官方 huggingface.co（预设选项 hf-mirror.com）
```

派生属性（`config.py` 增，供装配与 UI 统一使用）：

```python
@property
def asr_mode(self) -> str:            # "cloud" | "local" | "none"
    if self.cloud_asr_provider and self.cloud_asr_base_url:
        return "cloud"
    if self.asr_fallback == "none":
        return "none"
    return "local"                   # sensevoice / whisper 均为 local 形态

@property
def asr_local_endpoint(self) -> str:
    return (self.local_asr_base_url or "").rstrip("/") or "http://127.0.0.1:9991"

@property
def embed_remote_configured(self) -> bool:
    """远程 embedding 已配置：provider 非 fastembed 且 base_url 非空（沿用 embed_enabled 判定）。"""
    return self.embed_provider != "fastembed" and bool(self.embed_base_url)

@property
def embed_mode(self) -> str:          # "remote" | "local" | "none"
    if self.embed_remote_configured:
        return "remote"
    if self.embed_provider == "" and not self.embed_model:
        return "none"
    return "local"                    # fastembed 本地
```

- 语义对照：远程参数**一旦配置**（哪怕只是 provider+base_url）即优先远程——**"已配置优先，未配置降级"**，与用户需求完全一致，也兼容现有 docker-compose（远程三件套配齐 → 行为与今天相同，本地降级静默待命）。
- `whisper_model` / `WHISPER_MODEL` 保留兼容；默认 `asr_fallback=sensevoice` 意味着**本地默认档从 faster-whisper large-v3 换成 SenseVoice**（行为变更，见 §4）。

### 3.2 装配与降级决策（factory / embedder）

```python
# factory.build_transcribers 改造后（示意）：
chain = [SubtitleTranscriber()]
if settings.asr_mode == "cloud":
    chain.append(CloudASRTranscriber(remote…))                 # 现状路径不变
elif settings.asr_mode == "local":
    if settings.asr_fallback == "whisper":
        chain.append(WhisperTranscriber(settings.whisper_model, model_dir=settings.models_dir))
    else:  # sensevoice —— 本地端点复用 CloudASRTranscriber（分块/重试/segments 兜底全部继承）
        chain.append(CloudASRTranscriber(
            settings.asr_local_endpoint,
            api_key="local", model="sensevoice", provider="local-sensevoice",
        ))
# asr_mode == "none"：仅字幕档（视频无字幕时 pipeline 报"all transcribe providers failed"，可读化）
```

- `provider="local-sensevoice"` 不含 `whisper` → `cloud_asr.py:_is_faster_whisper()` 返回 False，不会发送 faster-whisper 专有参数，与现网 SenseVoice 行为一致。
- **自动 failover 扩展位**：将来做"远程宕机→本地"（§7.4）时，只需把 cloud 与 local 两个 transcriber **同时放入链**并让 `transcribe_with_fallback`（`pipeline.py:48-65`）按序尝试即可，本设计装配结构已兼容（两档并存时顺序 cloud → local → whisper）。
- Embedder：保持 `build_embedder` 构造签名；`Embedder._get_model` 加载优先级改为：
  1. `local_embed_model_dir` 非空 → `TextEmbedding(model_name, specific_model_path=…)`；
  2. 否则 `cache_dir=models_dir` 正常加载（缓存命中则离线，未命中且本地降级启用时抛"模型未下载，请到设置下载"的可读错误而非静默触发网络下载 —— 通过下载器先行保证命中）。
  - `embed_texts` 不再"远程失败即抛"：远程路径仅当 `embed_mode=remote` 才进入（装配期已保证），消除注释中"未实现降级"的中间态。

### 3.3 本地 ASR 服务进程生命周期

**Docker（推荐形态）**——`docker-compose.yml` 增加：

```yaml
services:
  videorag:            # 原有，保持
    …
  asr:                 # 本地降级用 sherpa SenseVoice（OpenAI 兼容）
    profiles: ["local-asr"]        # 默认不拉起；需要本地降级时：docker compose --profile local-asr up -d
    build: ./deploy/asr
    image: videorag-asr:latest
    restart: unless-stopped
    volumes:
      - ./videorag-data/models:/models:ro   # 与 videorag 共享模型（videorag 负责下载/写）
    command: ["--model", "/models/asr/model.int8.onnx",
              "--tokens", "/models/asr/tokens.txt",
              "--port", "9991", "--num-threads", "6"]
    # 不映射宿主端口：仅 videorag 经容器网络 http://asr:9991 访问（安全）
```

- videorag 环境补一行 `LOCAL_ASR_BASE_URL: "http://asr:9991"`（compose 注入；裸机不设则回落 127.0.0.1:9991）。
- compose `--profile` 语义：远程配置齐全的用户 `docker compose up -d` 不产生额外容器；需要全离线/本地降级的用户加 `--profile local-asr`。文档/README 给两种起法。
- 主容器**不需** changes：模型下载在 videorag 内完成，asr 容器只读模型卷。

**裸机**：`deploy/asr/`（仓库已带可跑 server.py 与 models/）`./.venv/bin/python server.py --model models/model.int8.onnx --tokens models/tokens.txt --port 9991 --num-threads 6`。

**服务健康与模型缺失语义**：
- asr 容器提供 `/health`（官方 server 自带）与 `/v1/models`；videorag 的"测试连通性"按钮调之。
- asr 启动时模型缺失 → 打印可读错误并退出（restart: unless-stopped 会循环，README 提示先下载模型）；videorag 下载完成后 `docker compose restart asr`（文档给出命令，v1 不做"容器控制 API"）。

### 3.4 本地模型下载管理器（后端）

**目录**：`app/core/local_models/`（新包）：`registry.py`（模型定义）、`downloader.py`（httpx 流式 / snapshot 封装）、`manager.py`（任务状态机 + 互斥 + 持久化）、`api.py`（路由）。

**模型注册表（registry，静态表）**：

```python
MODELS = {
  "asr": {
    "kind": "asr", "label": "SenseVoice int8（sherpa-onnx）",
    "source": "modelscope", "repo": "poloniumrock/SenseVoiceSmallOnnx",
    "files": [  # 静态清单（2 文件，均已实测可 resolve 直链下载）
      {"path": "model.int8.onnx", "to": "asr/model.int8.onnx", "size": 239_233_841,
       "sha256": "…（下载后以 files API 复核为准）"},
      {"path": "tokens.txt", "to": "asr/tokens.txt", "size": 315_894},
    ],
  },
  "embedding": {
    "kind": "embedding", "label": "bge-small-zh-v1.5（fastembed ONNX）",
    "hf_repo": "Qdrant/bge-small-zh-v1.5", "dim": 512,
    "required_files": ["model_optimized.onnx", "tokenizer.json", "config.json"],
  },
}
```

**下载任务状态机**：

```
idle → downloading → (done | failed | cancelled)
        └── downloading ── 校验中(sha256) ──> done
failed ── retry ──> downloading（.part 续传）
```

- 任务对象：`{job_id, kind, state, stage(枚举文件/下载中/校验), file_current, file_total,
  bytes_done, bytes_total, speed_bps, error, started_at, updated_at}`。
- 进程内单例 Registry（uvicorn 单进程，与现有组件模式一致）；**下载中断重启容器 → 任务丢失但 `.part` 保留**，用户重试即续传（不落库，README 说明；如需跨重启任务恢复列 §7 扩展）。
- 互斥：同 kind 并发下载拒绝（409）；取消 = 取消当前文件下载并清理该文件 `.part`（保留已完成文件，可续传其余）。
- 完整性：ASR 逐文件比对 files API 的 `Sha256`（下载后、改名落定前）；embedding 校验必需文件存在 + 非空。
- 磁盘空间预检：下载前检查目标分区剩余空间 ≥ 清单总大小 ×1.1（避免 229 MB 下载到一半爆盘）。

**API 草案（`/api/models`，注册进 main router）**：

| 方法 | 路径 | 说明 | 响应/错误 |
|------|------|------|----------|
| GET | `/api/models` | 全部 kind 状态：installed / manual_path / job（若有）/ 版本大小 / 生效档位 | 200 |
| POST | `/api/models/{kind}/download` | 启动下载（kind=asr\|embedding） | 202 {job_id}；409 已有任务；400 已装或已配 manual_path |
| POST | `/api/models/{kind}/cancel` | 取消进行中任务 | 200；404 无任务 |
| POST | `/api/models/{kind}/retry` | 失败任务重试（续传 .part） | 202；400 非 failed 态 |
| DELETE | `/api/models/{kind}` | 删除已下载模型文件 | 200；400 正在下载/手动路径不可删 |
| PUT | `/api/models/{kind}/path` | 手动指定模型目录 `{path}` | 200；400 路径校验失败（写 runtime.env 持久化） |
| GET | `/api/models/jobs/{job_id}` | 进度轮询 | 200 job 对象 |

- 进度通道：**轮询**（复用现有 3s 轮询节奏；229 MB 下载粒度足够）。SSE 列为后续增强（见 §7），不阻塞 v1。
- settings API 扩展：`asr`/`embedding` 组保持不变（远程三件套），本地字段独立走 `/api/models` 与 PUT settings 新增可选 `local` 子组（asr_fallback / local_asr_base_url / local_asr_model_dir / local_embed_model_dir）——不混淆"远程服务配置"与"本地模型管理"两组语义。
- GET `/api/settings` 增加 `local` 组回显（同 MASK 处理不需要，无密钥）。

### 3.5 前端

- `LocalModelsPanel.jsx`：两份模型卡 + 顶部链路预览；状态由 `GET /api/models` 驱动；下载/取消/重试调对应端点后进入 3s 轮询刷新进度（`setInterval`，离开页面清理）；沿用现有 `.alert / .btn / .field / .input / .spinner` 样式类。
- `SettingsView.jsx`：TABS 常量加 `{ key: "models", label: "本地模型" }`；local 子组字段并入"服务配置"Tab 的 asr/embedding 组（折叠区"本地降级设置"）或独立小节（实现取舍：**放本地模型 Tab 内最直观**——开关、端点、手动路径都进该 Tab，避免服务配置页膨胀）。
- 移动端：进度条/按钮保持触控目标 ≥44px（沿用 v2 E4 规范）。
- `api.js` 增补 `getModels / downloadModel / cancelModel / retryModel / setModelPath`。

### 3.6 部署资产与 Docker 兼容性规划

**新增 `deploy/asr/`**（本仓库资产，sherpa-onnx 运行时）：

```
deploy/asr/
├── Dockerfile          # 单段：python:3.11-slim + pip install -i 清华 sherpa-onnx fastapi uvicorn python-multipart numpy
│                       #   拷贝 server.py；ENTRYPOINT 指向 server.py（模型路径经 command/环境注入）
├── server.py           # 已编写并实测通过的 OpenAI 兼容服务（sherpa-onnx + SenseVoice int8）
├── models/             # 已下载模型（model.int8.onnx 229MB + tokens.txt；不入库，见 .gitignore）
├── pyproject.toml 或 requirements.txt  # 运行时依赖锁定（sherpa-onnx>=1.13）
└── README.md           # Docker + 裸机两形态起法、模型预置/下载、与 videorag 联调验证
```

**Docker 兼容性检查清单**：

| 项 | 规划 |
|----|------|
| 镜像分层 | videorag 主镜像**不装** sherpa-onnx（保持轻量）；ASR 依赖全部隔离在 `videorag-asr` 镜像 |
| 平台 | 基础镜像 `python:3.11-slim`（现有同款）；sherpa-onnx 提供 x86_64/aarch64 Linux wheel（含 macOS/Windows，覆盖 Apple Silicon Docker）；默认 CPU 保证开箱即跑 |
| 网络 | 国内 pip 源（清华）写死在 Dockerfile（与现有 Dockerfile 用清华一致）；模型下载出网由 videorag 容器承担 |
| 卷 | `videorag-data/models` 为唯一模型真源：videorag rw、asr ro；换机器/升级容器模型不重下 |
| 端口 | asr 不暴露宿主端口；仅容器网络内访问（`http://asr:9991`）；裸机形态才绑 127.0.0.1 |
| 重启 | asr `restart: unless-stopped`；模型缺失时打印指引后退出（避免静默挂起） |
| compose profile | `local-asr` profile 使远程用户零额外资源；README 明确 `--profile` 用法 |
| 环境变量优先 | 所有新配置遵守"环境变量 > runtime.env > 默认值"；`.env.example` 同步新增（含 LOCAL_ASR_BASE_URL 双形态示例注释） |

**`.env.example` 新增示意**：

```env
# ============ 本地降级（未配置远程时启用）============
ASR_FALLBACK=sensevoice           # sensevoice(默认) | whisper | none
# Docker（compose --profile local-asr）时注入：LOCAL_ASR_BASE_URL=http://asr:9991
# 裸机默认 127.0.0.1:9991
LOCAL_ASR_BASE_URL=
# 手动指定本地模型目录（留空=自动下载管理到 $DATA_DIR/models）
LOCAL_ASR_MODEL_DIR=
LOCAL_EMBED_MODEL_DIR=
# Embedding 下载镜像端点（留空=官方 huggingface.co；国内可设 https://hf-mirror.com）
EMBED_DOWNLOAD_ENDPOINT=
```

---

## 4. 兼容性与迁移

1. **行为变更（需在发布说明标注）**：`asr_fallback` 默认 `sensevoice` → 未配置远程 ASR 的存量部署，本地档从 faster-whisper large-v3 变为 sherpa-onnx SenseVoice int8（更快、更小、带句级时间戳；服务端返回带 start/end 的句级 segments，切片粒度已实测达标）。依赖 whisper 本地档的用户显式设 `ASR_FALLBACK=whisper`。
2. **远程配置优先语义不变**：现网 compose（远程三件套齐全）重启后行为完全一致——本地降级仅在远程**缺省**时出现，不破坏任何现有部署。
3. **runtime.env 存量**：新增 env 无默认覆盖风险；`apply_runtime` 未知键忽略（`config.py:132`），老 runtime.env 无冲突。
4. **Embedding 切换与向量库**：`check_model_compat`（`pipeline.py:256-265`）已拦截"异模型入库"；本地 bge-small-zh（512d）与远程 bge-m3（1024d）互切 = 换库事件：
   - v1：前端警示 + 指引"清空 lancedb/与 chunks 表后重新摄入"（提供手工 SQL/说明，见 README）；
   - 扩展：提供「知识库重建」任务 API（重拉存量视频 embedding 流水线），见 §7。
5. **deploy/sensevoice 定位**：保持为"远程集中式（Windows 现网）专用"资产；sherpa-onnx 本地形态独立走 `deploy/asr/`（两套服务 schema 一致，videorag 客户端无感知）。
6. **faster-whisper 依赖**：保留（`ASR_FALLBACK=whisper` 档仍需）；可选后续拆 optional extra（§7）。

---

## 5. 验证与测试

### 5.1 单元/集成测试（tests/ 新增）

| 用例 | 断言 |
|------|------|
| `config` 档位判定 | 远程齐全→cloud/remote；仅 provider 无 base_url→local；`ASR_FALLBACK=none`→none |
| `factory` 装配 | 各档位返回链正确；local-sensevoice 用 `asr_local_endpoint`；whisper 档保留 |
| ModelScope files 解析 | 返回文件树→过滤文档目录→清单正确（mock 响应含 IsLFS 大文件）；sherpa 静态清单与 files API 复核一致 |
| 下载器 | httpx mock：进度回调字节递增、`.part` 中断后 retry 续传（Range）、sha256 不符→failed |
| manager 状态机 | 互斥 409、cancel 清 .part、retry 仅 failed 可调、磁盘预检不足→400 |
| `/api/models` | 全端点契约（mock 管理器）：安装状态、manual_path 校验、删除保护 |
| Embedder 本地加载 | cache 命中不联网（local_files_only）、manual dir 优先级、模型缺失可读错误 |
| 兼容回归 | 现有 settings/test_metadata 全量通过（改动不触碰） |

### 5.1b sherpa-onnx 服务实测（已完成，2026-09-03）

- 24.9min 真实中文音频：295 句级 segments / 8650 字 / 无坏段 / 60s block 边界无缝；RTF=0.078（6 线程）；RSS ~670MB（峰值 ~900MB）；加载 2.49s。详见 §D1。

### 5.2 手动验收清单（docker 形态端到端）

1. `docker compose --profile local-asr up -d --build` 两容器就绪；videorag `GET /api/settings` 显示 asr_mode=local。
2. 设置→本地模型：ASR 未下载 → 点下载 → 进度条字节级推进 → done（229 MB，按网速）；期间取消 → .part 保留 → retry 续传成功。
3. Embedding 下载 done（≈92 MB）；两卡状态"就绪"。
4. 提交一条真实 B 站视频 URL → 流水线 fetching→transcribing→noting→embedding→done 全绿；详情页 segments 带 start/end，切片跳转可用。
5. 断网后：下载失败红条→恢复网络→重试成功。
6. 手动路径：把模型目录指到 NFS/外部目录 → 状态"就绪(手动)" → 转写正常；删除按钮禁用。
7. 关闭开关 `ASR_FALLBACK=none` → 提交视频 → 可读错误提示未配置 ASR。
8. 远程三件套配齐（模拟现网 compose）→ 行为与改造前一致（回归）。
9. 重启 videorag 容器 → runtime.env 持久、模型仍在（卷），无重复下载。
10. 裸机形态：`deploy/asr` 说明文档照做一遍（uv run server + LOCAL_ASR_BASE_URL 缺省 127.0.0.1）。

---

## 6. 里程碑与任务拆分

| # | 里程碑 | 内容 | 依赖 | 验证出口 |
|---|--------|------|------|---------|
| L0 | 配置与装配 | config 新字段/派生属性 + factory/embedder 改造 + 错误文案 | — | 单测：档位/装配表 |
| L1 | 模型下载器后端 | registry/downloader/manager + `/api/models` | L0 | 单测（mock 网络）+ curl 验收 |
| L2 | 前端本地模型面板 | LocalModelsPanel + SettingsView Tab + api.js | L1 | 手动验收 2/3/6/7 |
| L3 | 部署资产 | deploy/asr Dockerfile/requirements/README + compose profile + .env.example | L1 | 手动验收 1/4/5/8/9/10 |
| L4 | 打磨（可选迭代） | sha 校验策略、磁盘预检、SSE 进度、重建知识库 API、faster-whisper 拆 optional | L1-L3 | 按 §7 决策 |

owner / deadline：已授权开发（sherpa-onnx 运行时先行验收通过，本设计 v2 定稿）。

---

## 7. 未决问题（评审需拍板）

1. ~~官方 server vs 补丁版~~ **已解决**：运行时定为 sherpa-onnx + SenseVoice int8，`deploy/asr/server.py` 自研 OpenAI 兼容服务已实测（句级 segments 达切片质量门槛，无 A/B 争议）。
2. ~~funasr 版本兼容~~ **已解决**：sherpa-onnx 1.13.7 + Python 3.14/3.11 均兼容，无 `--model_dir` 形态问题（模型路径经 `--model/--tokens` 直传）。
3. ~~fsmn-vad 必须文件~~ **已解决**：sherpa-onnx 词级时间戳不依赖 VAD，无需 fsmn-vad。
4. **fastembed 版本能力**：现 lock 版本是否含 `specific_model_path`（需 ≥0.6）；若否 → 提升依赖下限或仅用缓存命中法。**建议升级 fastembed>=0.6**（breaking 面小，list_supported_models 返回同构）。
5. **Embedding 重建库 API 是否纳入 v1**：警示+指引（v1 保底）vs 一键重建（P1）。**建议 v1 只做警示+指引**，重建 API 单独评估。
6. **本地 ASR 鉴权**：容器网络内访问无鉴权可接受（不暴露宿主端口）；裸机形态建议仅绑 127.0.0.1（server.py 默认 host=0.0.0.0，README 提示裸机绑 127.0.0.1）。**倾向后者**。
7. **`.part` 续传依赖服务端 Range**：ModelScope resolve 实测支持 206 即可；不支持的降级为整文件重下（下载器自适应）。
8. ~~镜像平台~~ **已解决**：sherpa-onnx 提供 x86_64/aarch64 Linux wheel（无 torch 重依赖），arm64 Docker 天然可用。

---

## 附录 A：参考资源

- SenseVoice int8 ONNX（sherpa-onnx 运行时采用的模型）：ModelScope `poloniumrock/SenseVoiceSmallOnnx`（`model.int8.onnx` 229 MB + `tokens.txt`）；官方同源 GitHub `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17`（k2-fsa）
- sherpa-onnx（k2-fsa）：`OfflineRecognizer.from_sense_voice(model, tokens, num_threads, use_itn, language)`；词级 `timestamps`；提供 x86_64/aarch64 Linux / macOS / Windows wheel
- ModelScope files API（本设计实测可用）：`GET https://modelscope.cn/api/v1/models/{repo}/repo/files?Recursive=true&PageSize=100&Revision=master` → `Data.Files[] {Path, Size, Sha256, Type, IsLFS}`
- FastEmbed（qdrant）：`bge-small-zh-v1.5` dim=512 / ONNX ≈92 MB / `hf: Qdrant/bge-small-zh-v1.5`；本地加载 `specific_model_path`（≥0.6）/ `local_files_only`（≥0.2.7）/ cache 命中
- 旧 FunASR 集中式服务（仅远程形态保留）：`deploy/sensevoice/`（fork + 时间戳补丁版，Windows/GPU）
