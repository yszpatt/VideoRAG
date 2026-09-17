# -*- mode: python ; coding: utf-8 -*-
"""videoRAG Windows 桌面包 —— PyInstaller 配置（onedir）。

设计要点：

1. **onedir 而非 onefile**：onefile 每次启动都要把数百 MB 解包到临时目录，冷启动
   轻松超过 10s；onedir 只解压一次。桌面包体积换来的稳定性更值。
2. **web/dist 打进 ``_internal/web/dist``**：与 ``app/main.py::_mount_static`` 的默认
   推导（``<app 包上级>/web/dist``）天然吻合，无需改业务代码。
3. **ffmpeg 不进 EXE**：放在 ``<exe 同级>/vendor/ffmpeg``，由
   ``app/runtime_env.py::apply_vendor_path()`` 在启动时前置插入 PATH。
   这样 ffmpeg 可独立升级、许可边界清晰（构建时下载，不进仓库）。
4. **原生包逐个 collect_all**：lancedb(Rust) / onnxruntime / av / pyarrow /
   tokenizers / sherpa_onnx 都带二进制或数据文件，PyInstaller 的静态分析不足以
   完整收集——这是本配置最容易出问题的部分，构建后务必跑 ``--self-test`` 冒烟。

构建：``pyinstaller packaging/videorag.spec --noconfirm``（建议用 packaging/build-windows.ps1）
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

PROJECT_ROOT = Path(SPECPATH).parent  # noqa: F821  (SPECPATH 由 PyInstaller 注入)

datas = []
binaries = []
hiddenimports = []


def _collect(pkg: str, required: bool = False) -> None:
    """collect_all 单个包；包不存在时告警（``required`` 则直接失败）。"""
    try:
        d, b, h = collect_all(pkg)
    except Exception as e:  # noqa: BLE001
        if required:
            raise SystemExit(f"[spec] 必需依赖 {pkg} 收集失败：{e}") from e
        print(f"[spec] 跳过 {pkg}（未安装/收集失败：{e}）")
        return
    datas.extend(d)
    binaries.extend(b)
    hiddenimports.extend(h)

# ---- 前端构建产物 ----
web_dist = PROJECT_ROOT / "web" / "dist"
if not (web_dist / "index.html").is_file():
    raise SystemExit(
        f"[spec] 前端产物缺失：{web_dist}/index.html\n"
        f"       请先执行：cd web && npm ci && npm run build"
    )
datas += [(str(web_dist), "web/dist")]

# ---- 应用图标 ----
# 供**窗口与任务栏**使用：运行时由 app/desktop.py 经 pywebview 的 icon= 设置。
# 与下方 EXE(icon=...) 是两件事——后者只把图标写进 exe 的 PE 资源（资源管理器/快捷方式用），
# 不会自动成为窗口图标。
logo_ico = PROJECT_ROOT / "packaging" / "logo" / "videorag.ico"
if logo_ico.is_file():
    datas += [(str(logo_ico), "assets")]
else:
    print(f"[spec] 警告：未找到应用图标 {logo_ico}，窗口会沿用默认图标")

# ---- 带二进制/数据文件的原生与半原生包（必须显式收集）----
# lancedb 0.38 由 Rust 扩展 + lance-namespace 客户端组成，漏收集会在建库时报
# 「找不到原生库」；onnxruntime 的 provider dll、av 的 FFmpeg dll 同理。
for pkg in (
    "lancedb",
    "lance_namespace",
    "lance_namespace_urllib3_client",
    "pyarrow",
    "onnxruntime",
    "tokenizers",
    "av",
    "cv2",                      # rapidocr 依赖 opencv
    "shapely",
    "pyclipper",
    "fastembed",                # 含模型配置/分词器数据
    "rapidocr_onnxruntime",     # 含内置 OCR onnx 模型与配置 yaml（漏了则 OCR 静默失效）
    "huggingface_hub",
    # yt-dlp 必须显式收集：抓取层是通过**子进程**调用它的（app/core/fetchers/ytdlp.py），
    # 本进程内只有 desktop.py 的代理分支 import 它，静态分析覆盖不到它的大量
    # extractor 子模块。历史故障：产物里完全没有 yt_dlp，导致「所有抓取策略都失败」。
    "yt_dlp",
    # 原生窗口（pywebview）：走 Edge WebView2 后端需要 CLR 桥
    "webview",
    "pythonnet",
    "clr_loader",
):
    _collect(pkg)

# sherpa-onnx 仅桌面 extra 安装（Docker 主镜像刻意不装）；缺失只告警，
# 此时 ASR_LOCAL_BACKEND 应保持 http（远端/侧车形态）
_collect("sherpa_onnx")

# ---- 动态导入：静态分析覆盖不到的运行时装配路径 ----
#
# 注意 mcp.cli 必须排除：它 import typer（属于可选依赖 `mcp[cli]`，本项目未装），
# 未装时该模块导入即 sys.exit(1)，会让 collect_submodules 整个失败。
# 本项目只用 mcp.server.fastmcp，不涉及 CLI 入口。
def _skip_mcp_cli(name: str) -> bool:
    return not (name == "mcp.cli" or name.startswith("mcp.cli."))


hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("mcp", filter=_skip_mcp_cli)
hiddenimports += collect_submodules("app")

# uvicorn 的 loop/protocol 实现按字符串在运行期选择，必须显式声明
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "anyio._backends._asyncio",
    "aiosqlite",
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.dialects.sqlite.aiosqlite",
    "app.desktop",
]

# ---- 明确排除：既减体积也避免误收集 ----
excludes = [
    "torch", "torchvision", "torchaudio",
    "tensorflow", "keras",
    "matplotlib", "IPython", "notebook", "jupyter",
    "pytest", "_pytest",
    "tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    # 桌面版不提供 faster-whisper 保留档（本地转写统一走进程内 SenseVoice）。
    # 这三者实测合计约 122MB（av.libs 63MB + ctranslate2 59MB），且 av / ctranslate2
    # **只**被 faster-whisper 使用（tokenizers 被 fastembed 也用，故保留不排除）。
    # 代价：ASR_FALLBACK=whisper 在桌面包里不可用——WhisperTranscriber 会抛可读错误。
    # Docker 形态不受影响（容器内仍装 faster-whisper，保留 whisper 档）。
    "faster_whisper", "ctranslate2", "av",
]

a = Analysis(
    [str(PROJECT_ROOT / "app" / "desktop.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# ---- 双入口 ----
# videorag.exe      窗口形态（console=False）：双击直接出现原生窗口，不弹命令行黑框。
#                   代价是进程没有可用的 stdout，因此打包环境下 yt-dlp 走同进程调用
#                   （见 app/core/fetchers/ytdlp.py::_run_ytdlp_inproc）。
# videorag-cli.exe  控制台形态：CI 冒烟（--self-test）、命令行排障、--headless 当服务器用。
#                   窗口形态下 stdout 为 None，self-test 的结论根本打不出来，所以必须留它。
_COMMON = dict(
    exclude_binaries=True,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX 对 onnxruntime/opencv 等 dll 常导致加载失败，禁用
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # 应用图标（多尺寸 ICO，Windows 会按 DPI 自动选合适的档）：
    # 由 packaging/make_logo.py 生成，窗口/任务栏/资源管理器都用它
    icon=str(PROJECT_ROOT / "packaging" / "logo" / "videorag.ico"),
)

exe_windowed = EXE(pyz, a.scripts, [], name="videorag", console=False, **_COMMON)
exe_console = EXE(pyz, a.scripts, [], name="videorag-cli", console=True, **_COMMON)

coll = COLLECT(
    exe_windowed,
    exe_console,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="videoRAG",
)
