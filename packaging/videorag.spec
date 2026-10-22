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
4. **原生窗口零 .NET 依赖**：窗口由 ``app/webview2_host.py`` 直连 WebView2 的
   Win32/COM 接口，因此 **pythonnet / clr_loader / pywebview 都不再收集**。
   这曾是最脆弱的一环：``clr_loader`` 在 Windows 上默认走 netfx，
   用 ``Assembly.LoadFrom`` 加载 ``Python.Runtime.dll``；当路径被 .NET 判定为
   「网络位置」时会被 CAS 策略拒绝（``0x80131515``），报
   ``Failed to resolve Python.Runtime.Loader.Initialize``——装多少 .NET
   Desktop Runtime 都没用。去掉整条 CLR 桥即根除该故障面。
   WebView2 只需随包带一份 ``WebView2Loader.dll``（见下方 datas）。
5. **单个应用程序入口**：发布包只产出 ``videorag.exe``（``console=False``，双击即
   出窗口，不弹命令行）。冒烟测试用的控制台入口 ``videorag-cli.exe`` **不进发布包**
   ——它由 ``packaging/build-windows.ps1 -CliOnly`` 单独产出，仅供 CI 与排障。
   实现方式：用环境变量 ``VIDEORAG_BUILD_CLI=1`` 切换本 spec 的入口形态
   （spec 是 Python 脚本，可按环境分支；这样 CE 只需维护一份 spec）。

构建：``pyinstaller packaging/videorag.spec --noconfirm``（建议用 packaging/build-windows.ps1）
"""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

PROJECT_ROOT = Path(SPECPATH).parent  # noqa: F821  (SPECPATH 由 PyInstaller 注入)

# 入口形态：默认发布包（单一窗口 exe）；VIDEORAG_BUILD_CLI=1 时产出控制台冒烟入口
_BUILD_CLI = os.environ.get("VIDEORAG_BUILD_CLI") == "1"

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
# 供**窗口与任务栏**使用：运行时由 app/desktop.py → app/webview2_host.py 加载。
# 与下方 EXE(icon=...) 是两件事——后者只把图标写进 exe 的 PE 资源（资源管理器/快捷方式用），
# 不会自动成为窗口图标。
logo_ico = PROJECT_ROOT / "packaging" / "logo" / "videorag.ico"
if logo_ico.is_file():
    datas += [(str(logo_ico), "assets")]
else:
    print(f"[spec] 警告：未找到应用图标 {logo_ico}，窗口会沿用默认图标")

# ---- WebView2Loader.dll ----
# 原生窗口只依赖这一个原生组件：它负责「找到系统里已安装的 WebView2 Runtime」。
# 随包分发可避免目标机 PATH / 注册表差异导致定位失败。
# 来源：pywebview wheel 自带的 x64 副本（仅构建期使用，运行时不 import pywebview）。
_loader_candidates = [
    PROJECT_ROOT / "packaging" / "vendor" / "webview2" / "WebView2Loader.dll",
    PROJECT_ROOT / "packaging" / "vendor" / "webview2" / "amd64" / "WebView2Loader.dll",
]
try:
    import webview as _wv  # 构建环境通常装了 pywebview（desktop extra）

    _loader_candidates.append(
        Path(_wv.__file__).parent / "lib" / "runtimes" / "win-x64" / "native" / "WebView2Loader.dll"
    )
except Exception:  # noqa: BLE001
    pass

_loader = next((p for p in _loader_candidates if p.is_file()), None)
if _loader is None:
    raise SystemExit(
        "[spec] 未找到 WebView2Loader.dll。\n"
        "       请把 x64 的 WebView2Loader.dll 放到 packaging/vendor/webview2/ 下，\n"
        "       或安装 desktop extra（pywebview 自带该文件）。"
    )
print(f"[spec] WebView2Loader.dll ← {_loader}")
datas += [(str(_loader), "vendor/webview2")]

# ---- 体积裁剪：剔除用不到的原生组件 ----
# 依据「项目代码是否真的用到」逐项核对过：
#
# 1) ``opencv_videoio_ffmpeg*.dll``（约 29MB）：OpenCV 的**视频读写**后端。
#    项目里没有任何 ``cv2.VideoCapture`` / ``VideoWriter`` 调用——抽帧走的是
#    ffmpeg 子进程，OCR 走 rapidocr 的图片输入。cv2 只是 rapidocr 的间接依赖，
#    留着 ``cv2.pyd`` 即可，视频后端 dll 可以不要。
# 2) ``pyarrow/plasma``（约 0.5MB）：Plasma 是**共享内存对象存储服务**，项目里
#    没有用到（lancedb 走的是本地文件 + parquet）。
#
#    **不能动** ``arrow_dataset``：lancedb 的 ``common.py`` 直接
#    ``import pyarrow.dataset``，删掉会让整个应用启动即失败
#    （实测报 "pyarrow installation is not built with support for 'dataset'"）。
#    ``arrow_flight`` 同理保守起见不删——收益仅 14MB，而 lancedb 内部是否间接
#    使用 flight 协议无法从源码轻易确认，不值得为体积冒启动失败的风险。
_REMOVE_BIN_PATTERNS = (
    "cv2/opencv_videoio_ffmpeg",      # 视频编解码后端（项目只用图片路径）
)
_REMOVE_DLL_PATTERNS = (
    "pyarrow/plasma",                 # Plasma 共享内存存储，未使用
)


def _is_trimmed(entry: str) -> bool:
    norm = entry.replace("\\", "/").lower()
    return any(p in norm for p in _REMOVE_BIN_PATTERNS) or any(
        p in norm for p in _REMOVE_DLL_PATTERNS
    )


_before_bin = len(binaries)
binaries = [b for b in binaries if not _is_trimmed(b[0])]
print(f"[spec] 体积裁剪：binaries {_before_bin} → {len(binaries)}")

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
):
    _collect(pkg)

# collect_all 会把上面剔除的组件又加回来，这里再筛一遍
_before_bin2 = len(binaries)
binaries = [b for b in binaries if not _is_trimmed(b[0])]
print(f"[spec] 体积裁剪（collect 后）：binaries {_before_bin2} → {len(binaries)}")

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
    "app.webview2_host",
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
    # ---- 原生窗口不再需要 CLR 桥（见文件头第 4 点）----
    # 排除掉可同时消除「.NET 加载失败」与体积膨胀；窗口改由 app/webview2_host.py
    # 直接调 WebView2 的 COM 接口，不需要任何托管代码。
    "pythonnet", "clr_loader", "clr", "webview",
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

# ---- 入口形态 ----
# 发布包：只产出 videorag.exe（窗口形态，console=False）——双击即出应用窗口，
#         不弹命令行黑框。进程没有可用 stdout，因此打包环境下 yt-dlp 走同进程调用
#         （见 app/core/fetchers/ytdlp.py::_run_ytdlp_inproc）。
# CI/排障：VIDEORAG_BUILD_CLI=1 时改为产出 videorag-cli.exe（console=True），
#         供 --self-test 冒烟使用（窗口形态 stdout 为 None，自检结论打不出来）。
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

if _BUILD_CLI:
    print("[spec] 入口形态：控制台（videorag-cli.exe，供冒烟/排障）")
    exe = EXE(pyz, a.scripts, [], name="videorag-cli", console=True, **_COMMON)
    coll_name = "videoRAG-cli"
else:
    print("[spec] 入口形态：窗口（videorag.exe，发布包唯一入口）")
    exe = EXE(pyz, a.scripts, [], name="videorag", console=False, **_COMMON)
    coll_name = "videoRAG"

# ---- 最终过滤：作用于 Analysis 产出的 binaries ----
#
# 只在 spec 顶部过滤 ``binaries`` 变量是不够的：PyInstaller 的 **hook** 仍会把
# cv2 / pyarrow 的二进制自行加进 ``a.binaries``（它们有自己的 hook）。所以必须
# 在这里对最终列表再筛一遍，否则裁剪静默失效（实测 29MB 的 videoio dll 照旧在包里）。
#
# 注意 entry 是 ``(source, dest_dir)`` 二元组，两者都要看：source 决定「是不是这个
# 文件」，dest 决定「装到包里哪」。任一命中即剔除。
def _entry_is_trimmed(entry) -> bool:
    src = str(entry[0])
    dst = str(entry[1]) if len(entry) > 1 else ""
    return _is_trimmed(src) or _is_trimmed(dst)


_kept_binaries = []
_dropped: list[str] = []
for _e in a.binaries:
    if _entry_is_trimmed(_e):
        _dropped.append(Path(str(_e[0])).name)
    else:
        _kept_binaries.append(_e)
if _dropped:
    print(f"[spec] 最终裁剪：移除 {len(_dropped)} 个原生组件 -> {', '.join(_dropped)}")
a.binaries = _kept_binaries

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=coll_name,
)


# ---- 后置清理：确保用户数据绝不进发布包 ----
#
# 出现过的真实故障：数据目录的降级逻辑在打包目录内运行时，会把 ``data/``
# （SQLite/LanceDB/模型缓存，实测 399MB）和 ``logs/`` 直接写进产物目录。
# 一旦随包分发，等于把用户数据库和几百 MB 模型塞进了安装包。
#
# 这里在 COLLECT 之后显式删除，做到「不论上一轮在产物目录里留下了什么，
# 打出来的包都是干净的」。
import shutil as _shutil

_out_dir = Path(DISTPATH) / coll_name  # noqa: F821  (DISTPATH 由 PyInstaller 注入)
for _stale in ("data", "logs"):
    _p = _out_dir / _stale
    if _p.exists():
        _size = sum(f.stat().st_size for f in _p.rglob("*") if f.is_file())
        print(f"[spec] 清除产物内的用户数据目录 {_stale}/（{_size / 1024 / 1024:.1f}MB）")
        _shutil.rmtree(_p, ignore_errors=True)

# 冒烟测试时生成的临时日志也不该留下
for _pattern in ("*.log", "*.probe"):
    for _f in _out_dir.glob(_pattern):
        try:
            _f.unlink()
        except OSError:
            pass
