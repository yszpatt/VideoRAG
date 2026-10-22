"""桌面入口：起本地服务并**以原生窗口打开界面**。

形态说明（与 ``app/main.py`` 的分工）
--------------------------------------
``app/main.py`` 是 ASGI 应用工厂；本模块是**启动器**，负责环境引导、端口选择、
日志落盘、就绪等待、窗口呈现与优雅退出。真正的应用仍是同一个 ``create_app()``，
因此 Docker 形态（``uvicorn app.main:app``）与桌面形态共用全部业务代码。

呈现方式：**只有一个原生窗口**
-----------------------------
Windows 桌面包的目标形态是「双击就出一个应用窗口」，不再经过命令行、也不再
打开浏览器。窗口由 :mod:`app.webview2_host` 提供——它直接调用 WebView2 的
Win32/COM 接口，**不依赖 pythonnet / .NET Framework / pywebview**，因此不会再
被 CLR 加载策略或 Desktop Runtime 缺失拖垮。

``--headless`` 仅保留给 CI 冒烟与「当本地服务器用」的场景，不是终端用户的形态。

启动顺序有硬性要求：**先 bootstrap、后 import 应用**。
``app/main.py`` 在模块级执行 ``app = create_app()``，那一刻就会实例化 ``Settings``
并读取 ``DATA_DIR``——晚于引导就晚了，数据目录会落到默认位置。

线程模型：窗口（GUI）必须占用主线程，因此 uvicorn 跑在后台线程，主线程负责
等就绪 → 开窗口（阻塞至关闭）→ 通知服务退出。

用法::

    videorag                                # 原生窗口（默认，也是唯一面向用户的形态）
    videorag --headless                      # 只起服务（CI / 当服务器用）
    videorag --port 9000                     # 指定起始端口（被占则顺延）
    videorag --data-dir D:\\vr                # 指定数据目录
    videorag --self-test                     # 启动 → 探活 → 退出（打包冒烟）
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
import time
from pathlib import Path

# 允许 `python app/desktop.py` 直接运行（不经 -m）
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 窗口模式（PyInstaller console=False）下 stdout/stderr 可能是 None，
# 任何 print / logging.StreamHandler 都会直接抛 AttributeError。
# 必须在最早时机兜底成 devnull。
if sys.stdout is None:  # pragma: no cover - 仅打包窗口模式命中
    sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
if sys.stderr is None:  # pragma: no cover
    sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115

from app import runtime_env  # noqa: E402  （刻意早于应用代码导入：本模块不 import 应用）

log = logging.getLogger("videorag.desktop")

# 窗口默认尺寸（留出侧边栏 + 主内容区的常见笔记本分辨率余量）
WINDOW_SIZE = (1360, 900)
WINDOW_MIN_SIZE = (1000, 640)
# 前端为深色界面：窗口底色用深色可避免加载瞬间的白闪
WINDOW_BG = "#14171c"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="videorag",
        description="videoRAG 本地应用（默认打开原生窗口）",
    )
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址（默认仅本机 127.0.0.1；如需局域网访问设为 0.0.0.0）")
    ap.add_argument("--port", type=int, default=runtime_env.DEFAULT_PORT,
                    help=f"起始端口，被占用会顺延（默认 {runtime_env.DEFAULT_PORT}）")
    ap.add_argument("--data-dir", default=None,
                    help="数据目录（默认 %%LOCALAPPDATA%%\\videoRAG；便携模式见 VIDEORAG_PORTABLE）")
    ap.add_argument("--static-dir", default=None, help="前端构建产物目录（默认自动定位）")

    ui = ap.add_mutually_exclusive_group()
    ui.add_argument("--headless", "--no-browser", dest="headless", action="store_true",
                    help="只起服务、不开界面（CI 冒烟 / 当本地服务器用）")

    ap.add_argument("--window-size", default=f"{WINDOW_SIZE[0]}x{WINDOW_SIZE[1]}",
                    help=f"窗口尺寸，如 1600x1000（默认 {WINDOW_SIZE[0]}x{WINDOW_SIZE[1]}）")
    ap.add_argument("--log-file", default=None,
                    help="日志文件（默认 <数据目录>/logs/videorag.log；传空串可禁用）")
    ap.add_argument("--hf-mirror", action="store_true",
                    help="设置 HF_ENDPOINT=https://hf-mirror.com 加速模型下载（国内网络）")
    ap.add_argument("--open-timeout", type=float, default=90.0,
                    help="等待服务就绪的秒数（默认 90）")
    ap.add_argument("--log-level", default="info",
                    choices=["critical", "error", "warning", "info", "debug"], help="日志级别")
    ap.add_argument("--self-test", action="store_true",
                    help="启动后探活抓取链路与各端点，打印结果并退出（打包冒烟用）")
    return ap.parse_args(argv)


def _build_app(args: argparse.Namespace):
    """构造 ASGI 应用（在 bootstrap 之后调用）。"""
    from app.config import Settings
    from app.main import create_app

    static_dir = runtime_env.find_static_dir(args.static_dir)
    if static_dir is None:
        log.warning("未找到 web/dist 前端产物，将以纯 API 模式启动（界面不可用）")
    settings = Settings()
    _preflight_vector_store(settings)
    return create_app(settings=settings, static_dir=static_dir)


def _preflight_vector_store(settings) -> None:  # noqa: ANN001
    """启动前探测 LanceDB 能否真正打开库，失败时给可读错误。

    为什么需要这一步
    ----------------
    LanceDB 的 Rust 层在 ``connect()`` 时就要写 spill 临时文件。若临时目录不可写，
    它会在 **Rust 里 panic**，Python 侧只能看到
    ``RustPanic: rust future panicked: unknown error``；而这段代码跑在
    ``lifespan`` 启动阶段、uvicorn 尚未监听端口，于是桌面端表现为
    「窗口白屏 / 服务在 90s 内未就绪」，日志里几乎没有线索。

    在这里提前、显式地试一次连库：成功则继续（并缓存连接，避免重复打开），
    失败则抛出带处置建议的 RuntimeError，让用户当场看到原因而不是等 90 秒。
    """
    try:
        import lancedb

        db_path = Path(settings.data_dir) / "lancedb"
        db_path.mkdir(parents=True, exist_ok=True)
        # 只做连接+列表，不建表：足以触发 Rust 侧的临时目录初始化
        db = lancedb.connect(str(db_path))
        # table_names() 在 lancedb 0.38 已弃用，但旧版本只有它；优先用新 API
        if hasattr(db, "list_tables"):
            db.list_tables()
        else:  # pragma: no cover - 兼容旧版 lancedb
            db.table_names()
    except ImportError:
        # 没装 lancedb 的形态（如纯 API 部署）不阻塞启动
        log.debug("lancedb 未安装，跳过向量库预检")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(_vector_store_error_text(settings, e)) from e


def _vector_store_error_text(settings, exc: Exception) -> str:
    """拼出用户能照着做的排查指引。"""
    tried = ""
    # bootstrap 阶段记录过「已尝试的临时目录」，这里把它带上：
    # 用户不必自己猜，直接照着改环境变量即可。
    temp_error = _LAST_TEMP_ERROR.get("text", "")
    if temp_error:
        tried = "\n\n" + temp_error
    return (
        "向量库（LanceDB）初始化失败，服务无法启动。\n\n"
        f"错误：{type(exc).__name__}: {exc}\n\n"
        f"当前 TEMP：{os.environ.get('TEMP')}\n"
        f"数据目录：{settings.data_dir}\n"
        "最常见原因是**临时目录不可写**（LanceDB 需要在那里创建 spill 文件）。\n"
        "解决办法（任选其一）：\n"
        "  1. 新建一个空目录（如 D:\\videorag-temp），设置环境变量\n"
        "     VIDEORAG_TEMP_DIR 指向它，再重新启动本程序\n"
        "  2. 或把 TEMP / TMP 指向可写目录后重新启动\n"
        "  3. 检查磁盘空间，以及杀毒软件 / 受控文件夹访问是否拦截了这些目录\n"
        "  4. 若数据目录本身不可写，请检查 "
        f"{settings.data_dir} 的权限" + tried
    )


# ---------- 日志 ----------

def _resolve_log_file(args: argparse.Namespace, data_dir: str) -> Path | None:
    """日志落点：默认 <data_dir>/logs/videorag.log；显式传空串则禁用。"""
    if args.log_file is not None:
        return Path(args.log_file).expanduser() if args.log_file else None
    return Path(data_dir) / "logs" / "videorag.log"


def _log_file_candidates(primary: Path) -> list[Path]:
    """日志落点的候选列表（按优先级）。

    为什么要有多个候选
    ------------------
    窗口形态没有控制台，**日志是唯一的可观测性来源**。若数据目录恰好不可写
    （受限的企业环境、被安全软件接管、目录 ACL 异常），日志也会一起丢，
    用户就只剩一个弹窗——上一轮排查「90 秒未就绪」时正是这种情形。

    所以除了主路径，再准备几个通常可写的退路：
    数据目录同级 → exe 同级 → 当前工作目录。
    """
    cands = [primary]
    try:
        from app import runtime_env

        cands.append(runtime_env.bundle_root() / "logs" / "videorag.log")
    except Exception:  # noqa: BLE001
        pass
    try:
        cands.append(primary.parent.parent / (primary.parent.name + "-logs") / "videorag.log")
    except Exception:  # noqa: BLE001
        pass
    cands.append(Path.cwd() / "videorag.log")
    return cands


def _configure_logging(level: str, log_file: Path | None, to_console: bool) -> Path | None:
    """统一配置 root logger。

    窗口模式没有控制台，日志必须落文件——否则「应用起不来」对用户就是个黑盒。
    uvicorn 侧用 ``log_config=None`` 接入同一套配置（见 _serve_in_thread）。

    返回**实际生效**的日志路径（用于在错误提示里告诉用户去哪看）。
    """
    handlers: list[logging.Handler] = []
    if to_console and sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    effective: Path | None = None
    if log_file is not None:
        for cand in _log_file_candidates(log_file):
            try:
                cand.parent.mkdir(parents=True, exist_ok=True)
                # 真正打开一次，确认可写（mkdir 成功不代表能建文件）
                probe = cand.parent / (cand.name + ".probe")
                probe.write_bytes(b"")
                probe.unlink(missing_ok=True)
                handlers.append(logging.FileHandler(cand, encoding="utf-8"))
                effective = cand
                break
            except OSError:
                continue
        if effective is None:
            # 落盘失败不应阻断启动：退回控制台
            handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers or [logging.NullHandler()],
        force=True,
    )
    if log_file is not None and effective is None:
        logging.getLogger("videorag.desktop").warning(
            "日志文件不可写（%s），仅输出到控制台", log_file
        )
    return effective


def _fatal(message: str, log_file: Path | None) -> None:
    """致命错误提示：窗口模式没有控制台，用系统弹窗保证用户看得见。"""
    log.error(message)
    text = message + (f"\n\n详细日志：{log_file}" if log_file else "")
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, "videoRAG 启动失败", 0x10)
        except Exception:  # noqa: BLE001  弹窗失败不影响退出码
            pass


# ---------- 就绪等待 ----------

def _probe_host(host: str) -> str:
    """0.0.0.0/:: 不是合法请求目标，探测时换成 127.0.0.1。"""
    return "127.0.0.1" if host in ("0.0.0.0", "::") else host


def _existing_instance(host: str, port: int) -> str | None:
    """探测该端口上是否已经跑着一个 videoRAG 实例；命中则返回它的界面地址。

    为什么要查：启动第二个实例会让两个进程同时打开同一个数据目录，而 SQLite / LanceDB
    是单实例文件库，并发读写有损坏风险。实际发生过——原生窗口失败后进程没退，
    用户以为没打开就又启动了一次，结果两个实例同跑一个数据目录。

    判据用「``/health`` 与 ``/api/videos`` 同时返回 200」：同一端口上同时具备这两个端点
    的服务，基本只可能是本应用；这样就不必改动 ``/health`` 的返回契约。
    """
    import urllib.error
    import urllib.request

    base = f"http://{_probe_host(host)}:{port}"
    for path in ("/health", "/api/videos"):
        try:
            with urllib.request.urlopen(f"{base}{path}", timeout=1.5) as resp:  # noqa: S310
                if resp.status != 200:
                    return None
        except (urllib.error.URLError, OSError, ValueError):
            return None
    return f"{base}/"


def _open_existing(url: str, args: argparse.Namespace) -> int:
    """复用已在运行的实例：只把界面窗口打开，不再起第二个服务。

    不在这里等窗口关闭——服务是别人的，我们只是「打开界面」这一次动作，
    窗口关闭后本进程即退出。
    """
    return _run_window(url, args, None, None)


def _wait_ready_sync(host: str, port: int, timeout: float) -> bool:
    """同步轮询 /health（主线程用，避免在窗口线程里起事件循环）。"""
    import urllib.error
    import urllib.request

    url = f"http://{_probe_host(host)}:{port}/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310 固定本机 http
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.25)
    return False


# ---------- 冒烟探针 ----------

def _probe_ytdlp() -> tuple[str, bool, str]:
    """检查 yt-dlp 是否真的可被调用——打包环境下最容易失效的一环。

    历史故障：抓取层用 ``sys.executable -m yt_dlp``，打包后 ``sys.executable``
    是应用 exe，于是「所有抓取策略都失败」；同时 yt_dlp 因为没有任何 ``import``
    语句而根本没被 PyInstaller 收集。这两点都必须有自检兜住。
    """
    from app.core.fetchers.ytdlp import run_ytdlp

    try:
        proc = run_ytdlp(["--version"], timeout=60)
    except Exception as e:  # noqa: BLE001
        return ("yt-dlp (--version)", False, f"{type(e).__name__}: {e}")
    out = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.strip()]
    if proc.returncode == 0 and out:
        return ("yt-dlp (--version)", True, out[0][:48])
    err = [ln for ln in (proc.stderr or "").strip().splitlines() if ln.strip()]
    detail = err[-1][:90] if err else ""
    return ("yt-dlp (--version)", False, f"exit {proc.returncode} {detail}".strip())


def _probe_ffmpeg() -> tuple[str, bool, str]:
    """检查 ffmpeg 能否按名字调用（yt-dlp 合并/抽音、抽帧都依赖它走 PATH）。"""
    import subprocess
    from shutil import which

    exe = which("ffmpeg")
    if not exe:
        return ("ffmpeg", False, "not found on PATH")
    try:
        proc = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=30)
    except Exception as e:  # noqa: BLE001
        return ("ffmpeg", False, f"{type(e).__name__}: {e}")
    first = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    return ("ffmpeg", proc.returncode == 0, (first[0][:48] if first else exe))


def _probe_webview() -> tuple[str, bool, str]:
    """检查原生窗口是否**真的能起来**。

    历史教训：早期实现走 pywebview → pythonnet → .NET Framework，探针只验证
    「模块能否 import」就报 PASS，而真机上窗口根本起不来——失败点其实在 CLR
    加载（netfx 的 CAS 策略会拒绝加载被判定为「网络位置」的
    ``Python.Runtime.dll``，报 ``Failed to resolve Python.Runtime.Loader.Initialize``；
    改走 CoreCLR 又因为 .NET 5+ 移除了 ``System.Windows.Forms.ContextMenu``
    而无法实例化 WinForms 版 WebView2 控件）。

    现在窗口走 :mod:`app.webview2_host`，判据因此必须是**能不能真的建出环境**：
    只查 loader 与版本号还不够——真机上出现过「版本号读得到、目录也在，但任何进程
    都建不出环境（``0x80070002``）」，那正是窗口白屏的直接原因。所以这里做一次
    真实的 ``CreateCoreWebView2EnvironmentWithOptions``。
    """
    try:
        from app import webview2_host
    except Exception as e:  # noqa: BLE001
        return ("webview2 backend", False, f"模块不可用: {type(e).__name__}: {e}")

    if os.name != "nt":
        return ("webview2 backend", False, "原生窗口仅支持 Windows")

    loader = webview2_host.find_loader_dll()
    if loader is None:
        return ("webview2 backend", False, "未找到 WebView2Loader.dll（应随包在 vendor/webview2）")

    version = webview2_host.webview2_runtime_version()
    if not version:
        return (
            "webview2 backend",
            False,
            f"未安装 WebView2 Runtime（下载：{webview2_host.WEBVIEW2_DOWNLOAD_URL}）",
        )
    ok, detail = webview2_host.probe_environment()
    if not ok:
        return ("webview2 backend", False, f"Runtime {version} 不可用：{detail}")
    return ("webview2 backend", True, f"WebView2 Runtime {version}（{detail}）")


def _probe_sherpa() -> tuple[str, bool, str]:
    """检查进程内 ASR 依赖能否加载（打包后 DLL 缺失会在这里暴露）。

    桌面包的本地转写走进程内 sherpa-onnx，若它的原生库没被完整收集，
    表现是「转写一提交就失败」——比抓取失败更隐蔽，因此纳入构建门禁。
    """
    try:
        import sherpa_onnx
    except Exception as e:  # noqa: BLE001
        return ("sherpa-onnx", False, f"{type(e).__name__}: {e}")
    version = getattr(sherpa_onnx, "__version__", "")
    return ("sherpa-onnx", True, version or "import ok")


def _probe_app_icon() -> tuple[str, bool, str]:
    """检查应用图标是否随包分发（漏了的话窗口会顶着解释器默认图标）。

    exe 的 PE 资源图标由构建时嵌入，与这个检查是两件事：那个只影响资源管理器/
    快捷方式显示的图标，而窗口与任务栏图标要靠运行时读这个 .ico 文件去设置。
    """
    icon = runtime_env.find_app_icon()
    if icon is None:
        return ("app icon", False, "未找到 assets/videorag.ico（窗口将用默认图标）")
    return ("app icon", True, f"{icon.name} ({icon.stat().st_size / 1024:.1f}KB)")


async def _probe_endpoints(host: str, port: int) -> list[tuple[str, bool, str]]:
    """冒烟探测：外部进程 + GUI 后端 + ASR 依赖 + 图标 + 各 HTTP 端点。

    覆盖关键依赖面：外部进程（yt-dlp、ffmpeg）、桌面 GUI 后端、进程内 ASR 原生库、
    应用图标资源、SQLite/ASGI 路由、LanceDB（列表读取）、打包进去的静态资源、MCP 动态导入。
    """
    import httpx

    base = f"http://{_probe_host(host)}:{port}"

    # 前几项失败时后面 HTTP 检查再全绿也没有意义，放在最前
    checks: list[tuple[str, bool, str]] = [
        await asyncio.to_thread(_probe_ffmpeg),
        await asyncio.to_thread(_probe_ytdlp),
        await asyncio.to_thread(_probe_sherpa),
        await asyncio.to_thread(_probe_webview),
        await asyncio.to_thread(_probe_app_icon),
    ]

    async with httpx.AsyncClient(timeout=15.0) as client:
        for path in ("/health", "/api/videos", "/"):
            try:
                resp = await client.get(f"{base}{path}")
                checks.append((path, resp.status_code == 200, f"HTTP {resp.status_code}"))
            except Exception as e:  # noqa: BLE001
                checks.append((path, False, f"{type(e).__name__}: {e}"))

        # MCP：JSON-RPC over Streamable HTTP，动态导入最容易在打包后出问题
        try:
            resp = await client.post(
                f"{base}/mcp",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            ok = resp.status_code == 200 and "search_knowledge_base" in resp.text
            checks.append(("/mcp tools/list", ok, f"HTTP {resp.status_code}"))
        except Exception as e:  # noqa: BLE001
            checks.append(("/mcp tools/list", False, f"{type(e).__name__}: {e}"))

    return checks


# ---------- 桌面默认档位 ----------

def _apply_desktop_defaults() -> str:
    """桌面形态的默认档位：本地 ASR 走**进程内**，不再依赖侧车端口。

    这是桌面版与 Docker 版最关键的运行差异：Docker 形态下本地 SenseVoice 是独立
    侧车容器（``http://asr:9991``），而桌面包里没有这个容器，若沿用默认的
    ``http`` 后端，转写会一直连不上 127.0.0.1:9991，表现为「本地模型服务不可用」。

    两条保险：
    - 仅在用户**未显式配置** ``ASR_LOCAL_BACKEND`` 时生效（环境变量 / .env /
      runtime.env 仍优先，尊重用户自备 sidecar 的场景）；
    - 仅在 ``sherpa_onnx`` 确实可导入时切换，否则保持 ``http`` 并把选择权留给用户。

    返回实际生效的后端档位（供启动日志展示）。
    """
    import importlib.util

    if os.environ.get("ASR_LOCAL_BACKEND"):
        return os.environ["ASR_LOCAL_BACKEND"]
    try:
        available = importlib.util.find_spec("sherpa_onnx") is not None
    except (ImportError, ValueError):
        available = False
    if available:
        os.environ["ASR_LOCAL_BACKEND"] = "inproc"
        return "inproc"
    return "http"


# ---------- 服务线程 ----------

def _serve_in_thread(app, args: argparse.Namespace, port: int, holder: dict, done: threading.Event) -> None:
    """后台线程里跑 uvicorn（GUI 需要占用主线程）。"""
    import uvicorn

    config = uvicorn.Config(
        app,
        host=args.host,
        port=port,
        log_level=args.log_level,
        access_log=not args.self_test,
        # None = 不改动 logging 配置，直接接入我们已装好的 root handler（写文件）
        log_config=None,
    )
    server = uvicorn.Server(config)
    holder["server"] = server
    try:
        asyncio.run(server.serve())
    except Exception:  # noqa: BLE001
        log.exception("服务线程异常退出")
    finally:
        done.set()


def _shutdown(holder: dict, done: threading.Event, timeout: float = 10.0) -> None:
    server = holder.get("server")
    if server is not None:
        server.should_exit = True
    if not done.wait(timeout=timeout):
        log.warning("服务未在 %.0fs 内退出", timeout)


# ---------- 呈现方式 ----------

def _parse_window_size(text: str) -> tuple[int, int]:
    try:
        w, h = text.lower().split("x", 1)
        return max(int(w), 600), max(int(h), 400)
    except (ValueError, AttributeError):
        log.warning("窗口尺寸 %r 无法解析，使用默认 %sx%s", text, *WINDOW_SIZE)
        return WINDOW_SIZE


def _wait_until_stopped(holder: dict, done: threading.Event) -> int:
    """驻留等待服务结束（Ctrl+C / 被终止），然后收尾。"""
    try:
        done.wait()
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown(holder, done)
    return 0


def _run_window(url: str, args: argparse.Namespace, holder: dict | None,
                done: threading.Event | None) -> int:
    """打开原生应用窗口，阻塞到窗口关闭。

    窗口由 :mod:`app.webview2_host` 提供（直连 WebView2 的 Win32/COM 接口），
    因此**不需要** pythonnet、.NET Framework 或 pywebview。窗口关闭后，如果本进程
    还带着本地服务（``holder`` 非空），一并通知它退出。

    刻意**不再降级到浏览器**：桌面版的目标形态就是「一个应用程序窗口」。窗口起不来
    时直接给出可读错误（含 WebView2 下载地址）并写日志，而不是偷偷换成浏览器——
    后者会让用户以为「装错了东西」。
    """
    from app import webview2_host

    size = _parse_window_size(args.window_size)
    storage = Path(runtime_env.resolve_data_dir(args.data_dir)) / "webview"
    icon = runtime_env.find_app_icon()
    if icon is None:
        log.warning("未找到应用图标（assets/videorag.ico），窗口将沿用默认图标")

    try:
        storage.mkdir(parents=True, exist_ok=True)
        rc = webview2_host.run_window(
            url,
            title="videoRAG",
            width=size[0],
            height=size[1],
            min_size=WINDOW_MIN_SIZE,
            background=_bg_rgb(),
            icon=icon,
            storage_dir=storage,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("原生窗口启动失败：%s", e)
        # 文案与宿主内的失败弹窗共用同一份组装逻辑（含诊断 + 处置步骤），
        # 避免「同一个故障从这里起和从宿主起说法不一样」。
        _fatal(webview2_host.window_failure_text(f"{type(e).__name__}: {e}"), None)
        return 1

    # 窗口已关闭：若本地服务仍在本进程里，通知它退出
    if holder is not None and done is not None:
        _shutdown(holder, done)
    return rc


def _bg_rgb() -> tuple[int, int, int]:
    """把 ``#RRGGBB`` 形式的窗口底色转成 RGB 三元组。"""
    text = WINDOW_BG.lstrip("#")
    if len(text) == 6:
        try:
            return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
        except ValueError:
            pass
    return (0x14, 0x17, 0x1C)


# ---------- 主流程 ----------

#: bootstrap 阶段「临时目录探测失败」的详情，供错误提示复用。
#: 单独记一份是因为 preflight 发生在更后面，那时已经拿不到当时的上下文。
_LAST_TEMP_ERROR: dict[str, str] = {}


def _run(args: argparse.Namespace) -> int:
    # ---- 第一步：先把日志落盘 ----
    # 顺序很重要：环境准备（数据目录/临时目录探测）可能失败，而**只有日志先就位，
    # 失败才留得下痕迹**。此前顺序相反，用户看到的只有一个 PyInstaller 弹窗，
    # 日志文件里连一行都没有。
    #
    # 这一步只用「数据目录」这一个信息（不依赖 bootstrap），因此失败概率极低；
    # 万一连它都失败，就退回控制台输出。
    data_dir = runtime_env.resolve_data_dir(args.data_dir)
    log_file = _resolve_log_file(args, data_dir)
    # 实际生效的日志路径（可能因主路径不可写而落到备选位置），
    # 错误提示里要告诉用户「去哪看日志」。
    effective_log = _configure_logging(
        args.log_level, log_file, to_console=not args.headless and sys.stderr is not None
    )
    if effective_log is not None and effective_log != log_file:
        log.warning("日志改写到：%s（主路径不可写）", effective_log)

    # ---- 第二步：环境准备（可能失败）----
    try:
        info = runtime_env.bootstrap(args.data_dir, use_hf_mirror=args.hf_mirror)
    except Exception as e:  # noqa: BLE001
        log.exception("环境初始化失败")
        _fatal(str(e), effective_log)
        return 1

    info["asr_backend"] = _apply_desktop_defaults()

    # 数据目录可能被降级到备选位置：此时日志也应跟着换位置，否则用户
    # 去默认位置找不到日志。
    #
    # 换位置本身有个副作用：日志会被**切成两个文件**。排查时只看第一个
    # 就会以为「日志到这就断了」（这一条是真实踩过的坑）。因此切换前后
    # 各写一行，保证任一文件都能指出另一半在哪。
    if info["data_dir"] != data_dir:
        data_dir = info["data_dir"]
        new_log = _resolve_log_file(args, data_dir)
        log.warning("数据目录已改为 %s，后续日志将写入：%s", data_dir, new_log)
        moved = _configure_logging(
            args.log_level, new_log, to_console=not args.headless and sys.stderr is not None
        )
        if moved is not None:
            effective_log = moved
            log.warning(
                "日志在此继续（前面的启动日志在另一处：原数据目录不可写导致切换）"
            )

    log.info("videoRAG 启动")
    log.info("运行环境：\n%s", runtime_env.format_summary(info))

    # 临时目录探测失败：不立刻终止，把原因记录下来。
    # 真正用不了向量库时，_preflight_vector_store 会给出更贴近现场的提示。
    if info.get("temp_error"):
        log.warning("未找到可写的临时目录：\n%s", info["temp_error"])
        _LAST_TEMP_ERROR["text"] = info["temp_error"]

    # 目标端口上已有实例 → 直接复用它的界面，不再起第二个服务。
    # （双开会让两个进程共写同一个数据目录，SQLite / LanceDB 有损坏风险）
    if not args.self_test:
        existing = _existing_instance(args.host, args.port)
        if existing:
            log.info("检测到已有实例正在运行（%s），直接打开它的界面", existing)
            return _open_existing(existing, args)

    # 构造应用：这一步会真正打开 LanceDB。失败时立刻弹窗/记日志，
    # 而不是让服务线程卡在 lifespan 里、90 秒后才报「未就绪」。
    try:
        app = _build_app(args)
    except Exception as e:  # noqa: BLE001
        log.exception("应用初始化失败")
        _fatal(str(e), effective_log)
        return 1

    port = runtime_env.find_free_port(args.port, args.host)
    if port != args.port:
        log.info("端口 %s 被占用，改用 %s", args.port, port)
    url = f"http://{_probe_host(args.host)}:{port}/"

    holder: dict = {}
    done = threading.Event()
    thread = threading.Thread(
        target=_serve_in_thread, args=(app, args, port, holder, done),
        name="videorag-server", daemon=True,
    )
    thread.start()

    if not _wait_ready_sync(args.host, port, args.open_timeout):
        _fatal(f"服务在 {args.open_timeout:.0f}s 内未就绪（端口 {port}）。", effective_log)
        _shutdown(holder, done, timeout=3)
        return 1

    log.info("服务就绪：%s", url)

    if args.self_test:
        checks = asyncio.run(_probe_endpoints(args.host, port))
        print("[self-test] " + ("-" * 52))
        failed = 0
        for name, ok, detail in checks:
            print(f"  {'PASS' if ok else 'FAIL'}  {name:22s} {detail}")
            failed += 0 if ok else 1
        print("[self-test] " + ("-" * 52))
        print(f"[self-test] {len(checks) - failed}/{len(checks)} passed")
        _shutdown(holder, done, timeout=10)
        return 1 if failed else 0

    if args.headless:
        try:
            done.wait()
        except KeyboardInterrupt:
            pass
        finally:
            _shutdown(holder, done)
        return 0

    return _run_window(url, args, holder, done)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return _run(args)
    except KeyboardInterrupt:
        print("\n[info] 已退出")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
