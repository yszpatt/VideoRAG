# videoRAG Windows 本地应用打包 —— 可行性与方案

- 日期：2026-09-17
- 状态：方案待评审（尚未落地代码）
- 目标：在保留现有 Docker 分发链路不变的前提下，新增一条「Windows 本地应用」分发链路
  —— 用户既能 `docker compose up -d`，也能直接安装一个 Release 应用双击使用。

---

## 1. 结论

**可行，且难度低于预期。**

三条关键结论（均已实测，非推断）：

1. **全部 Python 依赖在 Windows 上都有可用 wheel**：以 `uv pip compile --python-platform x86_64-pc-windows-msvc` 实测解析，
   87 个包全部成功锁定，零编译需求。
2. **`app/` 业务代码几乎零平台耦合**：全仓扫描无 `fork` / `chmod` / POSIX 路径 / `sys.platform` 分支，
   唯一的平台外部依赖是 **ffmpeg 走 PATH 调用**（可随包捆绑解决）。
3. **原本最大的两个风险点已排除**：
   - `lancedb 0.38.0` 有官方 `win_amd64` wheel（uv.lock 里没有是因为该 lock 是在 macOS arm64 上生成的，不是不支持 Windows）；
   - `sherpa-onnx 1.13.8` 有 `cp311~cp314 win_amd64` wheel → **SenseVoice 可以进程内跑，不必再起第二个进程做 sidecar**。

预期工作量：MVP（可双击运行的 zip）约 2~3 天；带安装包的 Release 约 4~6 天。Docker 链路全程零改动。

---

## 2. 实测验证证据

### 2.1 Windows 平台依赖解析（决定性证据）

```bash
uv pip compile pyproject.toml --extra dev \
  --python-platform x86_64-pc-windows-msvc \
  --python-version 3.11 --no-header -o win-resolve.txt
# → exit 0，87 个包全部解析成功
```

解析结果中的原生扩展包（全部命中 Windows wheel）：

| 包 | 版本 | Windows 可用性 |
|---|---|---|
| lancedb | 0.38.0 | ✅ `cp310-abi3-win_amd64` |
| pyarrow | 25.0.1 | ✅ `cp311-win_amd64` |
| onnxruntime | 1.30.0 | ✅ `cp311-win_amd64` |
| opencv-python | 5.0.0.93 | ✅ `cp37-abi3-win_amd64` |
| rapidocr-onnxruntime | 1.4.4 | ✅ 纯 Python（`py3-none-any`） |
| sherpa-onnx | 1.13.8 | ✅ `cp311~cp314 win_amd64` |
| ctranslate2（faster-whisper 档） | 4.8.2 | ✅ `cp311-win_amd64` |
| av（PyAV） | 18.1.0 | ✅ `cp311-abi3-win_amd64` |
| tokenizers | 0.23.2 | ✅ 有 win wheel |

OCR 链路的次级依赖（`pyclipper 1.4.0` / `shapely 2.1.2`）亦均有 win_amd64 wheel。

### 2.2 代码平台耦合扫描

```
Grep app/  →  sys.platform | os.name | posix | chmod | fork( | /tmp
结果：仅 app/core/local_models/downloader.py 使用 tempfile（跨平台）
```

- 无 POSIX 专用调用，无 `pwd`/`grp`/`os.fork`，无硬编码 `/tmp`
- `web/src/api.js` 全部使用**相对路径** `/api/...` → 服务端口可自由更换，前端无需改动
- `app/security` 无（本机单用户场景无需鉴权）

### 2.3 本机环境实况

| 项 | 状态 |
|---|---|
| Python | 3.13.14（managed）/ 3.14（系统） |
| uv | ✅ `~/.local/bin/uv` |
| node | ✅ v22.22.2 |
| git | ✅ 2.55.0 |
| **docker** | ❌ **未安装**（说明 Windows 本机跑的就是裸机形态，Docker 链路在 NAS 侧） |
| ffmpeg | ✅ winget 安装 9.0.1-full_build（**开发可用，但 Release 不能依赖它**） |
| CI | ❌ 无 `.github/` 目录，需新建 |

### 2.4 仓库卫生问题（打包前需处理）

- `uv.lock` 是在 **macOS arm64** 上生成的（只记录了 macosx_arm64 + manylinux_aarch64 wheel），
  **且不包含 `rapidocr-onnxruntime`**（E3 视觉旁路加的依赖未重新 lock）→ 在 Windows 上 `uv sync` 会失败。
- 处置：在 Windows 上重新 `uv lock` 并提交；或为桌面链路单独维护 `requirements-windows.txt`。

---

## 3. 打包的 6 个耦合点与解法

这是本方案的全部实质工作，逐个列出。

### ① ffmpeg / ffprobe（必须捆绑）

**现状**：`shutil.which("ffmpeg")` 探测 + `subprocess(["ffmpeg", ...])` 直接按名字调用，
出现在 `app/core/transcribers/cloud_asr.py`（音频转 wav / 切片 / 时长探测）、
`app/core/vision/keyframes.py`（抽帧），以及 **yt-dlp 的 `--merge-output-format` / `--audio-format`**（全部依赖 PATH）。

**解法**：新增 `app/runtime_env.py` 做启动引导，把捆绑的 ffmpeg 目录**前置**插入 `os.environ["PATH"]`：

```python
def bootstrap(bundle_dir: Path) -> None:
    ff = bundle_dir / "vendor" / "ffmpeg"
    if (ff / "ffmpeg.exe").exists():
        os.environ["PATH"] = str(ff) + os.pathsep + os.environ.get("PATH", "")
```

- 必须在 `create_app()` **之前**执行（yt-dlp 与 ffmpeg 子进程都读 `os.environ`）
- ffmpeg 二进制**不入库**：由构建脚本在 CI 里下载（gyan.dev / BtbN Windows 静态构建）
- ⚠️ 许可：静态构建多为 GPL。做法是「构建时下载、随安装包分发、README 注明」，仓库本身不分发二进制。

### ② 数据目录默认值 `/data`（Windows 非法）

**现状**：`app/config.py` 中 `data_dir: str = "/data"`、`cookie_dir: str = "/data/cookies"`。
Windows 下会落到当前盘根目录（`C:\data`），权限与语义都不对。

**解法**：`config.py` 按平台给默认值，**Docker 行为不变**（compose 已显式注入 `DATA_DIR`）：

```python
def _default_data_dir() -> str:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return str(Path(base) / "videoRAG")
    return "/data"
```

优先级：`DATA_DIR` 环境变量 > CLI `--data-dir` > 平台默认。另需支持**便携模式**
（`VIDEORAG_PORTABLE=1` → 用 exe 同级 `./data`），满足「放 U 盘就能用」的场景。

### ③ 本地 ASR 侧车 → 改为进程内（关键简化）

**现状**：`asr_fallback=sensevoice`（默认档）时，`factory.py` 构造
`CloudASRTranscriber(settings.asr_local_endpoint, provider="local-sensevoice")`，
即 HTTP 调 `http://127.0.0.1:9991` —— 这个端点在 Windows 桌面包里**没人监听**。

**两条路**：

| 方案 | 说明 | 评价 |
|---|---|---|
| B1. 打包 sidecar 为第二个 exe 并由主程序拉起 | 零业务代码改动，但多一套进程生命周期管理（拉起/探活/随主进程退出/端口冲突/僵尸进程） | 不推荐 |
| B2. **进程内跑 SenseVoice** | 新增 `app/core/transcribers/sensevoice.py`，直接 `sherpa_onnx.OfflineRecognizer`，复用现成的分句/分块逻辑 | ✅ **推荐** |

B2 的复用要点：`deploy/asr/server.py` 里的 `_split_blocks` / `_build_segments` / `_median_gap`
是**纯函数**、已有测试覆盖（`tests/test_transcribers.py` 相关）。应把这几个函数**下沉**到
`app/core/transcribers/sensevoice_segments.py`，让 sidecar 与本机模式**共用同一份实现**，避免双份代码漂移。

档位开关（保证 Docker 行为零变化）：

```env
ASR_LOCAL_BACKEND=http    # 默认：走 127.0.0.1:9991 sidecar —— Docker 现状不变
ASR_LOCAL_BACKEND=inproc  # 桌面版：进程内 sherpa-onnx
```

`factory.py` 据此在 `CloudASRTranscriber` 与 `SenseVoiceTranscriber` 之间选择。

### ④ Web 前端构建产物

**现状**：`main.py::_mount_static` 挂载 `<repo>/web/dist`；`web/dist` 已被 gitignore。
**解法**：CI 里 `npm ci && npm run build` 后，spec 把它作为 datas 打进包，并在启动时把
`static_dir` 指向 PyInstaller 解包目录（`sys._MEIPASS`）。`_mount_static` 已支持传入 `static_dir`，改动极小。

### ⑤ LLM Key / 模型下载的首次运行体验

**现状已有**：设置页（`Alt+5`）可在线配 LLM/Embedding/ASR、下载 SenseVoice（240MB）与
bge-small（190MB）、重建知识库，配置持久化到 `$DATA_DIR/runtime.env`。**这部分完全复用，无需新做。**

需要补的只有：
- 首次启动若未配 key，页面上给一条明确的引导（现在只在提问时报 `LLM_API_KEY is not configured`）
- 模型下载镜像：桌面版默认 `HF_ENDPOINT=https://hf-mirror.com`（国内体验）
- 下载进度条已有（进度轮询 API 现成）

### ⑥ 端口占用

**现状**：写死 8080。桌面机上 8080 被占的概率不低。
**解法**：启动器先探测空闲端口（从 8080 起递增），以 `--port` 传给 uvicorn，并打开 `http://127.0.0.1:<实际端口>`。
前端用相对路径，**无需改动**。

---

## 4. 推荐方案（分阶段）

### 阶段 A — MVP：可双击运行的绿色包（建议先做）

产物：`videorag-windows-x64.zip`，解压即用。

```
videoRAG/
├── videorag.exe            # PyInstaller onedir 主程序（同时是启动器）
├── _internal/              # PyInstaller 运行时（含 app/、web/dist、全部依赖）
│   ├── web/dist/           # 前端构建产物
│   ├── app/                # 后端代码
│   └── (onnxruntime / lancedb / opencv ... 原生库)
├── vendor/
│   └── ffmpeg/
│       ├── ffmpeg.exe
│       └── ffprobe.exe
└── README-Windows.txt
```

行为：
1. `videorag.exe` 启动 → 引导 PATH / 数据目录 → 探测空闲端口 → uvicorn 起在 `127.0.0.1`
2. 轮询 `/health` 就绪 → 自动用默认浏览器打开界面
3. 控制台保留（可见日志），`Ctrl+C` 优雅退出
4. 数据落在 `%LOCALAPPDATA%\videoRAG`（可被 `--data-dir` / 便携模式覆盖）

为什么先做这个：不引入安装器、不引入 WebView2、不引入前端外壳框架，
把风险全部压在「**PyInstaller 能否正确收集这套原生依赖**」这一个未知上，快速证伪。

### 阶段 B — Release 安装包

用 **Inno Setup**（纯 Windows 工具链、无额外语言运行时）产出 `videoRAG-<ver>-setup.exe`：

- 安装到 `%LOCALAPPDATA%\Programs\videoRAG`（免管理员）或 `Program Files`（需管理员）
- 开始菜单 + 桌面快捷方式、卸载器、可选「开机自启」
- 升级时**保留数据目录**（数据在 `%LOCALAPPDATA%\videoRAG`，与安装目录分离）
- 可选：内嵌模型变体（+430MB）供离线用户，作为单独 release asset 而非默认包
- 可选：代码签名证书（消除 SmartScreen 告警）

### 阶段 C — 桌面外壳（可选，看体验要求）

| 方案 | 观感 | 成本 | 风险 |
|---|---|---|---|
| 系统浏览器（阶段 A/B 现状） | 有地址栏，像「本地服务」 | 0 | 无 |
| **pywebview** 原生窗口 | 真·桌面窗口 | 低（~50 行） | 需 WebView2 运行时（Win10 1803+ 通常自带，Win11 必带）+ pythonnet，PyInstaller 收集有中等坑 |
| **Tauri 2** 外壳 + Python sidecar | 最佳（托盘/更新器/小安装包） | 高（CI 引入 Rust 工具链，后端要拆成 sidecar exe） | 高 |
| Electron 外壳 | 最佳观感 | 最高（安装包 +150MB） | 中 |

**建议**：阶段 A/B 先用系统浏览器；若确需「像个真应用」，再上 pywebview（性价比最高）。
不建议为此引入 Tauri/Electron —— 为一层窗口把安装包从 ~300MB 拉到 ~450MB 不划算。

---

## 5. 改动文件清单

| 文件 | 类型 | 说明 |
|---|---|---|
| `app/runtime_env.py` | 新增 | 启动引导：PATH 注入 ffmpeg、数据目录解析、空闲端口探测、HF 镜像默认值 |
| `app/desktop.py` | 新增 | 桌面入口：解析 CLI 参数 → 引导 → 起 uvicorn → 就绪后打开浏览器 → 优雅退出 |
| `app/core/transcribers/sensevoice.py` | 新增 | 进程内 SenseVoice 转写（`sherpa_onnx.OfflineRecognizer`） |
| `app/core/transcribers/sensevoice_segments.py` | 新增 | 从 `deploy/asr/server.py` 下沉的纯函数（分块 / 分句 / 词时长估计），sidecar 与 in-proc 共用 |
| `app/config.py` | 改 | `data_dir` / `cookie_dir` 按平台给默认值；新增 `asr_local_backend` 字段 |
| `app/core/factory.py` | 改 | 按 `ASR_LOCAL_BACKEND` 选 in-proc 或 HTTP transcribers |
| `deploy/asr/server.py` | 改 | 改为 import 下沉后的纯函数（去重复实现） |
| `packaging/videorag.spec` | 新增 | PyInstaller onedir 配置（collect_all / hiddenimports / datas） |
| `packaging/vendor-ffmpeg.ps1` | 新增 | 下载 ffmpeg/ffprobe Windows 静态构建到 `packaging/vendor/ffmpeg/` |
| `packaging/installer.iss` | 新增 | Inno Setup 安装脚本 |
| `packaging/requirements-windows.txt` | 新增 | 桌面链路附加依赖（`sherpa-onnx`、`pyinstaller`） |
| `.github/workflows/build-windows.yml` | 新增 | windows-latest 构建 → PyInstaller → Inno Setup → 上传 Release |
| `README.md` | 改 | 新增「Windows 本地安装」章节；Docker 章节保持不变 |
| `uv.lock` | 改 | 在 Windows 上重新 lock（含 rapidocr） |

**Docker 链路改动量：0**（`Dockerfile` / `docker-compose.yml` / `deploy/asr/Dockerfile` 全部不动）

---

## 6. 体积预估与裁剪

| 组成 | 实测（解压后） | 可裁否 |
|---|---|---|
| lancedb（`_lancedb.pyd` **单文件**） | 297 MB | ❌ 上游 Rust 扩展固有体积 |
| ffmpeg + ffprobe（essentials 9.0.1） | 196 MB | ⚠️ 可换精简构建或改为首启下载 |
| cv2（opencv-python 5.0） | 112 MB | ⚠️ headless 收益有限（`cv2.pyd` 本体 82MB） |
| pyarrow | 81 MB | ❌ lancedb 依赖 |
| av.libs（PyAV） | 63 MB | ✅ **仅 faster-whisper 档需要** |
| ctranslate2 | 59 MB | ✅ **仅 faster-whisper 档需要** |
| onnxruntime | 40 MB | ❌ OCR/embedding 需要 |
| sherpa-onnx | 28 MB | ❌ 本地 ASR 本体 |
| videorag.exe（bootloader + PYZ） | 26 MB | ❌ |
| `_internal` 其余（stdlib/依赖） | 23 MB | ❌ |
| numpy.libs | 20 MB | ❌ |
| rapidocr 内置 OCR 模型 | 16 MB | ❌ |
| PIL | 13 MB | ⚠️ `_avif.pyd` 7.5MB 可裁，收益小 |
| cryptography / hf_xet / tokenizers 等 | ~28 MB | ❌ |
| **合计** | **≈1030 MB** | |

> **实测校正**：方案初稿预估 ~680MB，实测 1030MB。差距主要来自
> `_lancedb.pyd`（预估把 lancedb 与 pyarrow 合记为 150MB，实际 lancedb 一个 .pyd 就 297MB）
> 与 ffmpeg（essentials 构建 196MB，比预估的 170MB 大）。
>
> **能真正省下的**：把 `faster-whisper` 移出基础依赖可省 **约 122MB**（av + ctranslate2），
> 但它同时改变 Docker 镜像内容，需单独评估——因此本阶段**不动**，留作后续可选项。
> 换 ffmpeg 精简构建（或首启下载）可再省 ~100–190MB，代价是首启体验或兼容性。

裁剪策略（按性价比排序）：
- 把 `faster-whisper` 移出基础依赖（省 ~122MB）——**需同步评估 Docker 镜像影响**，不建议顺手做
- ffmpeg 换更小构建 / 首启下载（省 ~100–190MB）——桌面包首启需联网，需权衡
- `opencv-python` → `opencv-python-headless`（省 ~10–20MB，非方案初稿估的 40MB）
- `videorag.spec` 里 `excludes=['torch','tensorflow','matplotlib','IPython','pytest','tkinter',...]`（已在用）
- **不要用 `--onefile`**：每次启动都要解包约 1GB，冷启动不可接受；onedir 只解压一次

---

## 7. 风险与对策

| 风险 | 概率 | 影响 | 对策 |
|---|---|---|---|
| **PyInstaller 收集原生包不全**（lancedb 的 Rust 扩展 / onnxruntime providers / rapidocr 模型数据 / fastembed tokenizer 数据） | 中 | 构建失败或运行时崩 | `collect_all()` 逐包处理 + 逐包冒烟测试；**阶段 A 就是为验证这一点而设** |
| `mcp`(FastMCP) 动态导入被漏掉 | 中 | `/mcp` 端点 500 | 显式 `hiddenimports` + 打包后 curl 探针（README 里已有现成探针脚本） |
| onnxruntime / opencv 触发的杀软误报 | 中 | 用户不敢装 | 代码签名；README 说明 |
| 中文用户名路径（`C:\Users\张三`） | 中 | 模型/向量库读写异常 | 数据目录可用 CLI/环境变量改；纳入冒烟测试用例 |
| ffmpeg GPL 分发合规 | 低 | 许可问题 | 不入库，构建时下载；README 注明许可与来源 |
| 首次启动下载 430MB 模型 | 高 | 体验落差 | 复用现有设置页下载器；默认 HF 镜像；另发「含模型」的离线包 |
| 双分发链路功能漂移 | 中 | 维护成本 | 共用同一份 `app/` 与 `web/`，差异只在入口（`main.py` vs `desktop.py`）与 ASR backend |
| `uv.lock` 平台不一致导致 CI 失败 | 高 | 构建阻塞 | 在 Windows 重锁；或桌面链路改用 `requirements-windows.txt` |

---

## 8. CI/CD

新增 `.github/workflows/build-windows.yml`（`windows-latest`），与 Docker 链路完全独立：

```
1. actions/setup-python@v5  (3.11)
2. actions/setup-node@v4    (20)
3. cd web && npm ci && npm run build          → web/dist
4. pip install -r packaging/requirements-windows.txt
5. pwsh packaging/vendor-ffmpeg.ps1           → packaging/vendor/ffmpeg/
6. pyinstaller packaging/videorag.spec        → dist/videoRAG/
7. 冒烟：启动 exe → curl /health + /api/videos + MCP tools/list → 关闭
8. ISCC packaging/installer.iss               → videoRAG-<ver>-setup.exe
9. softprops/action-gh-release 上传 zip + setup.exe
```

触发：`v*` tag。Docker 用户继续走 `docker compose up -d --build`，互不干扰。

---

## 9. 与 Docker 共存的约束（必须守住）

1. `Dockerfile` / `docker-compose.yml` / `deploy/asr/*` **零改动**
2. `app/config.py` 的默认值变更必须保证：容器内由 compose 注入 `DATA_DIR=/data`，行为完全不变
3. `ASR_LOCAL_BACKEND` 默认 `http` → 不设该变量时 Docker 行为与今天一模一样
4. 数据目录结构（`db/ lancedb/ notes/ models/ cookies/`）两种形态**完全一致** → 同一份数据可在 Docker 与桌面包之间互换使用
5. `runtime.env`（Web 设置页持久化）机制复用，不新增平行配置体系

---

## 10. 待确认的决策点

1. **桌面包的 ASR 默认档位**：进程内 SenseVoice（推荐，零额外进程）？还是同时保留 faster-whisper 档（+150MB）？
2. **是否需要原生窗口**：系统浏览器够用，还是必须 pywebview 窗口（阶段 C）？
3. **是否内嵌模型**：默认不嵌（安装包 ~300MB，首启下载 430MB）？还是提供「离线完整包」变体？
4. **是否买代码签名证书**：不签会有 SmartScreen「未知发布者」告警，签的话是持续成本。
5. **CI 平台**：GitHub Actions 可用吗？还是需要走别的构建机器（当前仓库无 CI 配置）？

---

## 11. 建议的下一步

按「先证伪、后完善」的顺序，阶段 A 内部再拆成 4 个 spike：

| # | Spike | 通过标准 |
|---|---|---|
| A1 | 建 Windows venv 装全部依赖，跑通 `pytest` + `app.main:app` 裸机启动 | `/health` 200，`pytest` 全绿 |
| A2 | 捆绑 ffmpeg 后跑通一条真实视频（B站/YouTube）→ 转写 → 笔记 → 入库 → 提问 | 端到端成功，进度 UI 正常 |
| A3 | 新增进程内 SenseVoice transcribers，与 sidecar 结果对齐 | 同一音频两条路径输出段落一致 |
| A4 | PyInstaller onedir 构建 + 冒烟脚本 | 打包产物在**干净 Windows** 上双击可用（无 Python 环境） |

A1~A3 是代码工作（可立即开始），A4 是真正的未知数，建议尽早执行以暴露收集问题。

---

## 附：本文档的验证方式

- 依赖可用性：`uv pip compile --python-platform x86_64-pc-windows-msvc`（实测 exit 0，87 包）
- 单包 wheel 复核：PyPI JSON API 查询 `lancedb / sherpa-onnx / ctranslate2 / rapidocr-onnxruntime / opencv-python / pyclipper / shapely / av / onnxruntime / pyarrow`
- 代码耦合：`Grep app/` 扫描 `sys.platform|os.name|posix|chmod|fork(|/tmp|tempfile`
- 前端路径：`Grep web/src` 确认 `/api` 全为相对路径
- 体积：实测统计产物目录构成（见 §6，已按实测校正）

---

## 12. 实施结果（2026-09-17）

阶段 A 已全部落地并通过验证。

| 项 | 结果 |
|---|---|
| A1 环境 | `.venv` Python **3.11.16**（与 Docker 基础镜像一致），88 包全部安装成功 |
| 依赖导入 | `PyInstaller 6.22.3` / `sherpa-onnx 1.13.8` / `onnxruntime 1.30.0` / `cv2 5.0.0` / `lancedb 0.38.0` / `fastembed` 全部可导入 |
| 测试 | **423 passed / 3 failed**（3 个失败为改动前既有，见下） |
| 裸机启动 | `python -m app.desktop --self-test` → **4/4 PASS** |
| 打包 | PyInstaller onedir 成功，2120 个 binary/data 条目 |
| 产物自检 | `dist/videoRAG/videorag.exe --self-test` → **4/4 PASS**，`mode: packaged`，捆绑 ffmpeg 被识别并注入 PATH |
| 产物体积 | 解压 ≈1030MB，zip 见 `dist/videoRAG-*-windows-x64.zip` |
| Docker 链路 | `Dockerfile` / `docker-compose.yml` / `deploy/asr/*` **零改动** |

### 落地过程中修掉的真实缺陷

这几个都是「写完自测才发现」，值得记下来：

1. **f-string 不能含反斜杠**（Python 3.11 语法限制，Docker 也是 3.11）：
   `f"{d.rstrip('/\\')}"` 直接 `SyntaxError` → 字符集提为模块常量。
2. **`Path.resolve()` 在 Windows 上给 POSIX 路径加盘符**：`/data` → `C:\data`，
   破坏容器语义字符串 → 改为 `_normalize_path()`：绝对路径原样保留，只对相对路径转绝对。
3. **`cookie_dir` 用独立默认值时不会跟随 `data_dir`**：显式传 `data_dir=/data` 时
   cookie 目录仍按环境变量推导 → 改为 `model_validator` 从 `data_dir` 派生
   （显式 `COOKIE_DIR` 仍优先），保证「一个数据根」。
4. **PyInstaller `collect_submodules("mcp")` 会崩**：它导入 `mcp.cli`，而后者需要可选依赖
   `typer`（`mcp[cli]`），未装时 `sys.exit(1)` 导致整个收集失败 → spec 里用 `filter`
   排除 `mcp.cli*`（本项目只用 `mcp.server.fastmcp`）。

### 环境侧坑（与本项目无关，但会反复遇到）

5. **uv 在 Windows 上装包报 `Failed to update Windows PE resources … 拒绝访问`**：
   uv 为 console script 创建 trampoline 时需改写 PE 资源，受限环境会被拒 →
   改用 pip（用预编译 launcher 直接复制）；`build-windows.ps1` 已内置回落逻辑。
6. **`pip install -e` 重装时可能清空 `site-packages`**：卸载自身旧版本的动作在文件
   过滤驱动环境下会异常，导致整个 site-packages 消失（连 pip 都没了）→
   恢复 `python -m ensurepip --upgrade` 后重装；构建脚本已加「依赖齐备则跳过安装」。
7. **curl 是原生 Windows 程序，不认 MSYS 路径**：`-o /c/tmp/x.zip` 会写失败
   （`Warning: Failed to open the file`）→ 传 Windows 路径 `C:/tmp/x.zip`。
   `vendor-ffmpeg.ps1` 用 PowerShell，无此问题。

### 既有失败（与本次改动无关，勿误判）

`tests/test_local_models.py::test_api_models_health_asr_down`、
`tests/test_settings_probe.py::test_probe_connection_refused` /
`test_probe_asr_tries_health_then_models` 三个测试**在本次改动前就失败**，
已用 `git stash` 回到改动前状态复现确认。原因是本机用 pip 安装了**最新版**依赖
（httpx 0.28.1 等），与 `uv.lock` 锁定的版本存在行为差异——探活把 mock 返回的
HTTP 502 判为「已连通」。

> 顺带暴露一个仓库问题：`uv.lock` 是在 macOS arm64 上生成的（只记录
> macosx_arm64 + manylinux_aarch64 wheel），且**不含** `rapidocr-onnxruntime`，
> 与 `pyproject.toml` 已脱节。建议在 Windows 上重新 `uv lock` 并提交。

### 尚未做的（第一轮结束时）

- 阶段 B：Inno Setup 安装包（开始菜单 / 卸载器 / 数据与安装目录分离）
- ~~阶段 C：pywebview 原生窗口~~ → **第二轮已提升为默认形态**，见 §13
- 体积优化：见 §6，最有效的一项是移出 `faster-whisper`（省 ~122MB），但会动到 Docker 镜像，需单独评估
- 真实端到端：跑一条真实视频（转写 → 笔记 → 入库 → 提问）验证捆绑 ffmpeg 与进程内 ASR

---

## 13. 第二轮：线上问题修复与形态调整（2026-09-17）

用户在打包产物上实测报了两个问题。**两个都是「源码跑得通、打包就废」的类型**，
根因都在打包环境特有的差异上——这也说明第一轮的冒烟测试覆盖不足。

### 问题 1：导入视频报「all fetch providers failed」

**根因（两层，缺一不可）**：

1. `app/core/fetchers/ytdlp.py` 用 `[sys.executable, "-m", "yt_dlp", ...]` 起子进程调用 yt-dlp。
   打包后 `sys.executable` 是 **`videorag.exe`** 而不是 Python 解释器 ——
   `videorag.exe -m yt_dlp ...` 会被本程序的 argparse 当作未知参数、打印 usage 后退出，
   于是字幕 / 音频 / 视频三级策略全部失败。
2. 更隐蔽的一层：`yt_dlp` 在产物里**根本不存在**。抓取层只用 subprocess 调它，代码里
   没有任何 `import yt_dlp`，PyInstaller 静态分析自然发现不了。

**修复**：

- `run_ytdlp` 在打包形态下改为**同进程**调用（`_run_ytdlp_inproc`），用
  `redirect_stdout/stderr` 捕获输出。选同进程而非「再做一个代理 exe」是因为窗口形态
  （`console=False`）**没有可用的 stdout 句柄**，子进程方案在该形态下拿不到任何输出；
  顺带还省掉了每次抓取重启一个 PyInstaller exe 的秒级开销。
- spec 里显式 `collect_all("yt_dlp")`（942 个 extractor）。

### 问题 2：本地模型服务不可用

**根因**：`ASR_LOCAL_BACKEND` 默认 `http`，指的是容器侧车端点 `http://asr:9991`。
桌面包里没有这个容器，于是转写一直连不上 `127.0.0.1:9991`。
第一轮实现里做了 `inproc` 档位，却**忘了在桌面入口把它设为默认**。

**修复**：`app/desktop.py::_apply_desktop_defaults()` 在 bootstrap 后把档位切到 `inproc`，
两条保险：用户显式配置时不覆盖；`sherpa_onnx` 不可导入时保持 `http`。

### 形态调整：窗口化从「可选」改为默认

用户明确指出期望「直接运行的窗口，而不是先开命令行、再开浏览器」。这个反馈是对的：
把窗口放进「阶段 C 可选」是替用户做的、不该由我做的取舍。

调整后：

- 默认形态 = **pywebview 原生窗口**（Windows 走 Edge WebView2），双击即出窗口；
- `--browser` 保留系统浏览器（无 WebView2 时的后备）；`--headless` 只起服务；
- 产物改为**双入口**：`videorag.exe`（窗口，`console=False`，无黑框）+
  `videorag-cli.exe`（控制台，供 CI 自检与命令行排障）——窗口形态没有 stdout，
  自检结论必须由控制台版输出；
- 窗口形态没有控制台，日志落 `<数据目录>/logs/videorag.log`，启动失败弹系统对话框并指向日志。

**技术前提（本机已验证）**：WebView2 Runtime（`EdgeWebView/Application/152.0.4191.66`）、
.NET 6/7/8/9、`pythonnet 3.1.0` 均具备。缺失时自动回落系统浏览器，功能不受影响。

### 冒烟测试补强

第一轮的 self-test 只查 HTTP 端点，**恰好漏掉了这次翻车的两个环节**。补强后覆盖：

| 检查项 | 对应的历史故障 |
|---|---|
| `ffmpeg` | 抽音 / 抽帧 / 合并全废 |
| `yt-dlp (--version)` | 本次问题 1 |
| `sherpa-onnx` | 本次问题 2 / 原生库收集不全 |
| `webview backend` | 窗口起不来 |
| `/health`、`/api/videos`、`/`、`/mcp tools/list` | ASGI / LanceDB / 静态资源 / MCP 动态导入 |

### 验证结果

```
[self-test] 8/8 passed
  PASS  ffmpeg              ffmpeg version 9.0.1-essentials
  PASS  yt-dlp (--version)  2026.08.19
  PASS  sherpa-onnx         1.13.8
  PASS  webview backend     EdgeChromium (WebView2)
  PASS  /health  /api/videos  /  /mcp tools/list
```

真实链路实测（同一份业务代码）：

- **抓取**：B 站真实链接 → `kind=audio`，下载 7.94MB mp3 成功
- **转写**：进程内 SenseVoice 处理上述 8 分钟音频 → **46 段 / 888 字 / 29.6 秒**（rtf≈0.06），
  时间戳与分句正常

单元测试：**455 passed / 2 failed**（2 个为改动前既有的 httpx 版本漂移问题）。

### 顺带修掉的一个测试基础设施问题

`bootstrap()` 与 `_apply_desktop_defaults()` 会**直接写 `os.environ`**（设计使然——必须让
`Settings` 读到同一份路径与档位），而这类写入不受 `monkeypatch` 管辖，导致
`ASR_LOCAL_BACKEND=inproc` 与 `DATA_DIR` 泄漏到同进程的后续测试文件，
让「默认应为 http」这类断言误判为失败。已在两个测试文件加显式快照/还原的 autouse fixture。

---

## 14. 第三轮：元数据采集修复 + 端口与体积优化（2026-09-17）

用户报三件事：① 未采集到视频元数据；② 默认端口改 8566 减少冲突；③ 包体优化策略。

### 问题 A：元数据采集失败 —— 是第二轮修复引入的 bug

**现象**：`meta_source=none`，前端显示「该视频元数据未采集，仅显示基础信息」。

**根因**：第二轮把 `run_ytdlp` 在打包形态下改成同进程调用时写成了：

```python
sys.argv = ["yt-dlp", *args]
code = int(yt_dlp.main(sys.argv) or 0)     # ← 错在这里
```

`yt_dlp.main(argv)` 内部直接走 `argparse.parse_args(argv)`，期望的是**不含程序名**的
参数列表。多出来的那个 `"yt-dlp"` 被当成第二个 URL：

```
ERROR: [CommonMistakes] You've asked yt-dlp to download the URL "yt-dlp".
```

抓取（`fetch` 的三级策略）用的是另一组参数，恰好不受影响，所以只有元数据采集踩到。

**为什么自检没发现（值得记住的教训）**：self-test 的探针是
`run_ytdlp(["--version"])`，而 yt-dlp 处理 `--version` 就直接退出（exit 0），
根本走不到 URL 解析那一步。**探针能证明「能调用」，证明不了「参数语义正确」**。

**修复**：改为 `yt_dlp.main(list(args))`（`sys.argv` 仍保持含程序名的完整形式，
yt-dlp 内部有读它的逻辑），并补回归测试
`tests/test_fetchers.py::test_inproc_passes_args_without_program_name` 钉死该语义。

**验证**：端到端提交真实 B 站视频 → `meta_source=ytdlp`，标题 / 作者 / 时长 / 播放 / 点赞 /
评论全部采到。

### 问题 B：默认端口 8080 → 8566

- `app/runtime_env.py::DEFAULT_PORT`、`app/config.py::port` 默认值改为 8566；
- `web/vite.config.js` 代理目标改为可配置的 `VITE_API_TARGET`（默认 8566），
  这样前端 dev 既能配裸机（8566）也能配 Docker（8080）；
- **Docker 链路不受影响**：容器内端口由 `Dockerfile` CMD 与 compose 端口映射显式指定，
  与这两个默认值无关。

### 问题 C：体积优化

本轮已做第一项（**省 124.4MB**，解压 1085.2 → 960.8MB），其余按性价比列出待定：

| 策略 | 可省 | 状态 / 代价 |
|---|---|---|
| 排除 faster-whisper 档（连带 `ctranslate2` + `av`） | **-124MB** | ✅ 已做（`av`/`ctranslate2` 实测只被 faster-whisper 使用） |
| 排除视觉旁路（`cv2` + `rapidocr`） | -128MB | 待定：失去无语音视频的画面文字识别（MV / PPT 录屏） |
| ffmpeg 改首启下载（不随包） | -196MB | 待定：首次启动需联网，离线环境不可用 |
| 不打包 `ffprobe.exe` | -98MB | 待定：时长探测降级（桌面默认走进程内 ASR，影响面较小） |
| 只保留窗口版 exe | -26MB | 不推荐：CI 自检与命令行排障会失去入口 |
| lancedb `_lancedb.pyd` | 0 | ❌ 上游 Rust 扩展固有体积（单文件 297MB），配置层面无解 |

全部叠加约可降至 514MB（解压）/ zip 约 200MB。

代价说明：排除后 `ASR_FALLBACK=whisper` 在桌面包中不可用，`WhisperTranscriber` 会抛出
带指引的可读错误；**Docker 形态仍保留该档位**（容器内照常安装 faster-whisper）。

### 验证

端到端（打包产物 + 真实 B 站链接）：

```
[ok] 服务就绪 http://127.0.0.1:8566
  [ 2s] status=fetching     title=纯血鸿蒙第三方应用侧载安装教程--HoKit使用教程
  [ 8s] status=transcribing
---- 元数据 ----
  title  纯血鸿蒙第三方应用侧载安装教程--HoKit使用教程
  author 四月的小烤鱼   duration 301.0   meta_source ytdlp
  view_count 23685   like_count 381   comment_count 10
---- 转写 ---- 46 段
```

> 任务最终状态为 `failed`，原因是 `LLM_API_KEY is not configured`（笔记阶段需要 LLM key），
> 属预期行为；转写与检索不受影响。

单元测试：**456 passed / 2 failed**（2 个为既有 httpx 版本漂移问题）。
