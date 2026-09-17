"""桌面入口：起本地服务并**以原生窗口打开界面**。

形态说明（与 ``app/main.py`` 的分工）
--------------------------------------
``app/main.py`` 是 ASGI 应用工厂；本模块是**启动器**，负责环境引导、端口选择、
日志落盘、就绪等待、窗口/浏览器呈现与优雅退出。真正的应用仍是同一个
``create_app()``，因此 Docker 形态（``uvicorn app.main:app``）与桌面形态共用全部业务代码。

默认呈现方式是 **pywebview 原生窗口**（Windows 上走 Edge WebView2）——双击即出现
应用窗口，不弹命令行、不打开浏览器。两条后备路径：

- ``--browser``：用系统浏览器打开（无 WebView2 / 需要浏览器调试时）
- ``--headless``：只起服务不开界面（CI 冒烟、当本地服务器用）

启动顺序有硬性要求：**先 bootstrap、后 import 应用**。
``app/main.py`` 在模块级执行 ``app = create_app()``，那一刻就会实例化 ``Settings``
并读取 ``DATA_DIR``——晚于引导就晚了，数据目录会落到默认位置。

线程模型：窗口（GUI）必须占用主线程，因此 uvicorn 跑在后台线程，主线程负责
等就绪 → 开窗口（阻塞至关闭）→ 通知服务退出。

用法::

    videorag                                # 原生窗口（默认）
    videorag --browser                       # 系统浏览器
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
import webbrowser
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
    ui.add_argument("--browser", action="store_true",
                    help="用系统浏览器打开界面（默认是原生窗口；无 WebView2 时可用）")
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
    return create_app(settings=settings, static_dir=static_dir)


# ---------- 日志 ----------

def _resolve_log_file(args: argparse.Namespace, data_dir: str) -> Path | None:
    """日志落点：默认 <data_dir>/logs/videorag.log；显式传空串则禁用。"""
    if args.log_file is not None:
        return Path(args.log_file).expanduser() if args.log_file else None
    return Path(data_dir) / "logs" / "videorag.log"


def _configure_logging(level: str, log_file: Path | None, to_console: bool) -> None:
    """统一配置 root logger。

    窗口模式没有控制台，日志必须落文件——否则「应用起不来」对用户就是个黑盒。
    uvicorn 侧用 ``log_config=None`` 接入同一套配置（见 _serve_in_thread）。
    """
    handlers: list[logging.Handler] = []
    if to_console and sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError as e:
            # 落盘失败不应阻断启动：退回控制台
            handlers.append(logging.StreamHandler(sys.stderr))
            log.warning("日志文件不可写（%s），仅输出到控制台：%s", log_file, e)
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers or [logging.NullHandler()],
        force=True,
    )


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
    """检查原生窗口后端是否可用（决定默认能否开窗口，失败则回落浏览器）。"""
    try:
        import webview  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return ("webview backend", False, f"pywebview 不可用: {type(e).__name__}")
    try:
        from webview.platforms import edgechromium  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return ("webview backend", False, f"WebView2/.NET 不可用: {type(e).__name__}")
    return ("webview backend", True, "EdgeChromium (WebView2)")


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


def _run_window(url: str, args: argparse.Namespace, holder: dict, done: threading.Event,
                log_file: Path | None) -> int:
    """用 pywebview 开原生窗口，阻塞到窗口关闭。

    后端失败（无 WebView2 / 无 .NET）时回落系统浏览器，而不是直接报错退出——
    用户至少还能用，日志里也有明确原因。
    """
    try:
        import webview
    except Exception as e:  # noqa: BLE001
        log.warning("pywebview 不可用（%s），回落系统浏览器", e)
        return _run_browser(url, holder, done)

    size = _parse_window_size(args.window_size)
    storage = Path(runtime_env.resolve_data_dir(args.data_dir)) / "webview"
    icon = runtime_env.find_app_icon()
    if icon is None:
        log.warning("未找到应用图标（assets/videorag.ico），窗口将沿用默认图标")
    try:
        storage.mkdir(parents=True, exist_ok=True)
        webview.create_window(
            "videoRAG",
            url,
            width=size[0],
            height=size[1],
            min_size=WINDOW_MIN_SIZE,
            background_color=WINDOW_BG,
        )
        # 显式指定 Edge 后端：避免在缺少 WebView2 的机器上静默回落到老旧内核。
        # icon 也必须在这里给：exe 的 PE 资源图标只影响资源管理器/快捷方式，
        # 窗口与任务栏图标要由程序自己设置（Windows 的 WM_SETICON）。
        webview.start(
            gui="edgechromium",
            private_mode=False,
            storage_path=str(storage),
            icon=str(icon) if icon else None,
        )
        return 0
    except Exception as e:  # noqa: BLE001
        log.exception("原生窗口启动失败")
        _fatal(f"窗口启动失败：{e}\n\n将改用系统浏览器。", log_file)
        return _run_browser(url, holder, done)
    finally:
        _shutdown(holder, done)


def _run_browser(url: str, holder: dict, done: threading.Event) -> int:
    """系统浏览器模式：打开后驻留，直到服务结束（Ctrl+C / 被终止）。"""
    log.info("界面地址：%s", url)
    try:
        webbrowser.open(url)
    except Exception as e:  # noqa: BLE001  打开失败不致命，用户可手动访问
        log.warning("自动打开浏览器失败：%s", e)
    try:
        done.wait()
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown(holder, done)
    return 0


# ---------- 主流程 ----------

def _run(args: argparse.Namespace) -> int:
    info = runtime_env.bootstrap(args.data_dir, use_hf_mirror=args.hf_mirror)
    info["asr_backend"] = _apply_desktop_defaults()

    log_file = _resolve_log_file(args, info["data_dir"])
    # 窗口模式没有控制台，日志只落文件；其他形态同时打屏，便于调试
    _configure_logging(args.log_level, log_file, to_console=not args.headless and sys.stderr is not None)

    log.info("videoRAG 启动")
    log.info("运行环境：\n%s", runtime_env.format_summary(info))

    app = _build_app(args)
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
        _fatal(f"服务在 {args.open_timeout:.0f}s 内未就绪（端口 {port}）。", log_file)
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

    if args.browser:
        return _run_browser(url, holder, done)

    return _run_window(url, args, holder, done, log_file)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return _run(args)
    except KeyboardInterrupt:
        print("\n[info] 已退出")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
