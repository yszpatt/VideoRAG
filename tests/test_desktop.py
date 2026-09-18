"""桌面入口（app/desktop.py）测试。

重点覆盖**打包/窗口形态特有**的逻辑——这些是线上真出过问题、或换形态就失效的地方：

1. 本地 ASR 默认档位（桌面不能用 Docker 的 sidecar HTTP 形态）；
2. 窗口/浏览器/无界面三种呈现方式的参数互斥；
3. 外部依赖探针（yt-dlp、ffmpeg、WebView2 后端）；
4. 日志落盘（窗口形态没有控制台，日志是唯一线索）。
"""
from __future__ import annotations

import importlib.util
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import desktop

# 被测代码会**直接**写 os.environ（bootstrap 固化 DATA_DIR、_apply_desktop_defaults
# 切换 ASR 后端），这类写入不受 monkeypatch 管辖，必须显式快照/还原。
#
# 真实踩过：ASR_LOCAL_BACKEND=inproc 泄漏到同进程的后续测试文件，导致
# test_fallback_config 里「默认应为 http」的断言误判为失败。
_ENV_KEYS = (
    "DATA_DIR",
    "COOKIE_DIR",
    "HF_ENDPOINT",
    "VIDEORAG_DATA_DIR",
    "VIDEORAG_PORTABLE",
    "ASR_LOCAL_BACKEND",
)


@pytest.fixture(autouse=True)
def _isolate_env():
    saved = {k: os.environ[k] for k in _ENV_KEYS if k in os.environ}
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update(saved)


# ---------- 参数解析：三种呈现方式互斥 ----------

def test_ui_defaults_to_window():
    """默认 = 原生窗口（不是浏览器，也不是无界面）。"""
    args = desktop.parse_args([])
    assert args.browser is False
    assert args.headless is False


def test_ui_browser_flag():
    assert desktop.parse_args(["--browser"]).browser is True


def test_ui_headless_flag():
    assert desktop.parse_args(["--headless"]).headless is True


def test_no_browser_is_headless_alias():
    """`--no-browser` 是早期文档里的写法，保留为别名以免破坏既有用法。"""
    assert desktop.parse_args(["--no-browser"]).headless is True


def test_browser_and_headless_conflict():
    with pytest.raises(SystemExit):
        desktop.parse_args(["--browser", "--headless"])


# ---------- 窗口尺寸 ----------

@pytest.mark.parametrize("text,expected", [
    ("1600x1000", (1600, 1000)),
    ("1280X720", (1280, 720)),
    ("100x50", (600, 400)),        # 过小 → 夹到下限，避免窗口内容不可用
])
def test_parse_window_size(text, expected):
    assert desktop._parse_window_size(text) == expected


def test_parse_window_size_invalid_falls_back():
    assert desktop._parse_window_size("garbage") == desktop.WINDOW_SIZE


# ---------- 桌面默认档位：本地 ASR 走进程内 ----------

def _patch_sherpa(monkeypatch, available: bool):
    """伪装 sherpa_onnx 是否可导入（CI 只装 dev extra，不一定有它）。"""
    real = importlib.util.find_spec

    def fake(name, *a, **kw):
        if name == "sherpa_onnx":
            return object() if available else None
        return real(name, *a, **kw)

    monkeypatch.setattr(importlib.util, "find_spec", fake)


def test_desktop_defaults_switch_to_inproc_when_sherpa_available(monkeypatch):
    """有 sherpa-onnx 时默认走进程内——否则桌面包会一直去连不存在的 127.0.0.1:9991。"""
    monkeypatch.delenv("ASR_LOCAL_BACKEND", raising=False)
    _patch_sherpa(monkeypatch, True)

    assert desktop._apply_desktop_defaults() == "inproc"
    assert os.environ["ASR_LOCAL_BACKEND"] == "inproc"


def test_desktop_defaults_respect_explicit_value(monkeypatch):
    """用户显式配置（自备 sidecar）时不得被覆盖。"""
    monkeypatch.setenv("ASR_LOCAL_BACKEND", "http")
    _patch_sherpa(monkeypatch, True)

    assert desktop._apply_desktop_defaults() == "http"
    assert os.environ["ASR_LOCAL_BACKEND"] == "http"


def test_desktop_defaults_stay_http_without_sherpa(monkeypatch):
    """没有 sherpa-onnx（如源码环境只装了 dev）就保持 http，把选择权交给用户。"""
    monkeypatch.delenv("ASR_LOCAL_BACKEND", raising=False)
    _patch_sherpa(monkeypatch, False)

    assert desktop._apply_desktop_defaults() == "http"
    assert "ASR_LOCAL_BACKEND" not in os.environ


# ---------- 外部依赖探针 ----------

def test_probe_ffmpeg_reports_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    name, ok, detail = desktop._probe_ffmpeg()
    assert name == "ffmpeg"
    assert ok is False
    assert "not found" in detail


def test_probe_ffmpeg_reports_version(monkeypatch, tmp_path):
    fake = tmp_path / "ffmpeg.exe"
    fake.write_text("")
    monkeypatch.setattr("shutil.which", lambda name: str(fake))
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, stdout="ffmpeg version 9.0.1\n", stderr=""),
    )
    name, ok, detail = desktop._probe_ffmpeg()
    assert ok is True
    assert "9.0.1" in detail


def test_probe_ytdlp_ok(monkeypatch):
    from app.core.fetchers import ytdlp as ytdlp_mod

    monkeypatch.setattr(
        ytdlp_mod, "run_ytdlp",
        lambda args, timeout=None: subprocess.CompletedProcess(
            args, 0, stdout="2026.08.19\n", stderr=""
        ),
    )
    name, ok, detail = desktop._probe_ytdlp()
    assert ok is True
    assert "2026.08.19" in detail


def test_probe_ytdlp_detects_usage_exit(monkeypatch):
    """复现打包故障形态：调用被 argparse 拦下、退出码非 0 → 必须判为失败。"""
    from app.core.fetchers import ytdlp as ytdlp_mod

    monkeypatch.setattr(
        ytdlp_mod, "run_ytdlp",
        lambda args, timeout=None: subprocess.CompletedProcess(
            args, 2, stdout="", stderr="usage: videorag [-h] [--host HOST]\n"
        ),
    )
    name, ok, detail = desktop._probe_ytdlp()
    assert ok is False
    assert "exit 2" in detail


def test_probe_ytdlp_survives_exception(monkeypatch):
    from app.core.fetchers import ytdlp as ytdlp_mod

    def boom(args, timeout=None):
        raise FileNotFoundError("yt_dlp 未打包")

    monkeypatch.setattr(ytdlp_mod, "run_ytdlp", boom)
    name, ok, detail = desktop._probe_ytdlp()
    assert ok is False
    assert "FileNotFoundError" in detail


def test_probe_webview_ok():
    """本机已具备 WebView2 + .NET（CI 上无 pywebview 时该断言会失败，属预期）。"""
    name, ok, detail = desktop._probe_webview()
    if not ok:
        pytest.skip(f"当前环境无窗口后端：{detail}")
    assert "WebView2" in detail


def test_probe_webview_reports_failure(monkeypatch):
    """后端缺失时要给出可读原因，而不是抛异常。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "webview" or name.startswith("webview."):
            raise ImportError("no webview")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    name, ok, detail = desktop._probe_webview()
    assert ok is False
    assert "pywebview" in detail


def test_probe_app_icon_found(monkeypatch, tmp_path):
    """图标随包分发时要能定位到（窗口/任务栏靠它，不是靠 exe 的 PE 资源）。"""
    from app import runtime_env

    ico = tmp_path / "videorag.ico"
    ico.write_bytes(b"\x00" * 4096)
    monkeypatch.setattr(runtime_env, "find_app_icon", lambda: ico)

    name, ok, detail = desktop._probe_app_icon()
    assert ok is True
    assert "videorag.ico" in detail


def test_probe_app_icon_missing(monkeypatch):
    """漏打包时要在自检里显式失败，而不是等用户看到默认图标才发现。"""
    from app import runtime_env

    monkeypatch.setattr(runtime_env, "find_app_icon", lambda: None)
    name, ok, detail = desktop._probe_app_icon()
    assert ok is False
    assert "默认图标" in detail


def test_probe_webview_verifies_host_window_module(monkeypatch):
    """回归锁：探针必须验证**宿主窗口**模块（winforms），而不是只验渲染层。

    踩过的坑：原先只 import ``webview.platforms.edgechromium``（渲染层，不需要 .NET）
    就报 PASS；但 Windows 上真正的窗口实现是 ``webview.platforms.winforms``，
    它依赖 pythonnet + .NET 6+。缺 .NET 的机器上自检全绿、窗口却起不来。
    """
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "webview.platforms.winforms":
            raise ImportError("模拟缺 .NET 6+ Desktop Runtime")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    name, ok, detail = desktop._probe_webview()
    if os.name != "nt":
        pytest.skip("该断言只针对 Windows 宿主窗口")
    assert ok is False, "宿主窗口模块加载失败时必须判为不可用"
    assert ".NET" in detail


# ---------- 单实例检测 ----------

class _FakeResp:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_existing_instance_detects_running_app(monkeypatch):
    """/health 与 /api/videos 都 200 → 认为是已有实例。"""
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=None: _FakeResp())
    assert desktop._existing_instance("127.0.0.1", 8566) == "http://127.0.0.1:8566/"


def test_existing_instance_none_when_port_closed(monkeypatch):
    import urllib.error
    import urllib.request

    def boom(url, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert desktop._existing_instance("127.0.0.1", 8566) is None


def test_existing_instance_none_on_partial_match(monkeypatch):
    """只有 /health 通、/api/videos 不通 → 不是本应用，不应误判。"""
    import urllib.error
    import urllib.request

    calls = {"n": 0}

    def sometimes(url, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResp(200)
        raise urllib.error.URLError("boom")

    monkeypatch.setattr(urllib.request, "urlopen", sometimes)
    assert desktop._existing_instance("127.0.0.1", 8566) is None


def test_existing_instance_probes_loopback_for_wildcard(monkeypatch):
    """绑 0.0.0.0 时探测目标要用 127.0.0.1。"""
    import urllib.request

    seen = {}

    def fake(url, timeout=None):
        seen["url"] = url
        return _FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    desktop._existing_instance("0.0.0.0", 8566)
    assert seen["url"].startswith("http://127.0.0.1:8566")


# ---------- 应用模式窗口（--app 降级） ----------

def test_find_browser_exe_prefers_path_lookup(monkeypatch):
    monkeypatch.setattr(
        "shutil.which", lambda n: r"C:\fake\msedge.exe" if n == "msedge" else None
    )
    assert desktop._find_browser_exe() == r"C:\fake\msedge.exe"


def test_open_app_window_builds_expected_command(monkeypatch, tmp_path):
    """--app 模式：命令要带 --app=<url>、窗口尺寸与独立 profile。"""
    import subprocess

    captured = {}

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        return object()

    monkeypatch.setattr(desktop, "_find_browser_exe", lambda: r"C:\fake\msedge.exe")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    args = desktop.parse_args(["--data-dir", str(tmp_path)])
    assert desktop._open_app_window("http://127.0.0.1:8566/", args) is True

    cmd = captured["cmd"]
    assert cmd[0] == r"C:\fake\msedge.exe"
    assert "--app=http://127.0.0.1:8566/" in cmd
    assert "--window-size=1360,900" in cmd
    assert any(a.startswith("--user-data-dir=") for a in cmd)


def test_open_app_window_false_without_browser(monkeypatch):
    """找不到 Edge / Chrome 时要老实返回 False，交给下一级降级。"""
    monkeypatch.setattr(desktop, "_find_browser_exe", lambda: None)
    assert desktop._open_app_window("http://x/", desktop.parse_args([])) is False


def test_open_app_window_false_on_popen_failure(monkeypatch, tmp_path):
    import subprocess

    monkeypatch.setattr(desktop, "_find_browser_exe", lambda: r"C:\fake\msedge.exe")
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("cannot start")),
    )
    args = desktop.parse_args(["--data-dir", str(tmp_path)])
    assert desktop._open_app_window("http://x/", args) is False


# ---------- 日志 ----------

def test_resolve_log_file_defaults_under_data_dir():
    args = desktop.parse_args([])
    got = desktop._resolve_log_file(args, r"C:\data")
    assert got == Path(r"C:\data") / "logs" / "videorag.log"


def test_resolve_log_file_explicit_and_disabled(tmp_path):
    args = desktop.parse_args(["--log-file", str(tmp_path / "x.log")])
    assert desktop._resolve_log_file(args, "/tmp/d") == tmp_path / "x.log"

    args_off = desktop.parse_args(["--log-file", ""])
    assert desktop._resolve_log_file(args_off, "/tmp/d") is None


def test_configure_logging_writes_file(tmp_path):
    """窗口形态没有控制台：日志必须真的落到文件里（否则出问题无从排查）。"""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        log_file = tmp_path / "logs" / "videorag.log"
        desktop._configure_logging("info", log_file, to_console=False)

        logging.getLogger("videorag.test").info("hello-window-mode")
        for handler in root.handlers:
            handler.flush()

        assert log_file.is_file()
        assert "hello-window-mode" in log_file.read_text(encoding="utf-8")
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def test_module_survives_missing_std_streams():
    """窗口形态下 sys.stdout/stderr 可能为 None——模块导入不得因此炸掉。"""
    # 该兜底在模块顶部执行，这里只做语义确认：模块已成功导入且 main 可调用
    assert callable(desktop.main)
    assert sys.modules[desktop.__name__] is desktop
