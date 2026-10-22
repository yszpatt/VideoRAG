"""桌面入口（app/desktop.py）测试。

重点覆盖**打包/窗口形态特有**的逻辑——这些是线上真出过问题、或换形态就失效的地方：

1. 本地 ASR 默认档位（桌面不能用 Docker 的 sidecar HTTP 形态）；
2. 呈现方式：只有「原生窗口」与「无界面」两种，**没有浏览器降级**；
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


# ---------- 参数解析：只有窗口 / 无界面两种呈现方式 ----------

def test_ui_defaults_to_window():
    """默认 = 原生窗口（不是浏览器，也不是无界面）。"""
    args = desktop.parse_args([])
    assert args.headless is False


def test_ui_headless_flag():
    assert desktop.parse_args(["--headless"]).headless is True


def test_no_browser_is_headless_alias():
    """`--no-browser` 是早期文档里的写法，保留为别名以免破坏既有用法。"""
    assert desktop.parse_args(["--no-browser"]).headless is True


def test_browser_flag_is_gone():
    """回归锁：`--browser` 已被移除——桌面版只保留一个应用程序窗口。

    浏览器形态曾作为「原生窗口起不来」的兜底存在，但它同时带来两个问题：
    用户会以为装错了东西；且在没有 Edge 的机器上会静默退化成普通标签页。
    """
    with pytest.raises(SystemExit):
        desktop.parse_args(["--browser"])


def test_browser_helpers_removed():
    """回归锁：与浏览器相关的实现（应用模式窗口、找浏览器）必须一并删除。"""
    for name in ("_run_browser", "_open_app_window", "_find_browser_exe",
                 "_try_native_window"):
        assert not hasattr(desktop, name), f"{name} 应已移除（不再有浏览器降级）"


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
    """本机具备 WebView2 时应报 PASS；CI 上若无 Runtime 则跳过。"""
    name, ok, detail = desktop._probe_webview()
    if not ok:
        pytest.skip(f"当前环境无 WebView2：{detail}")
    assert name == "webview2 backend"
    assert "WebView2 Runtime" in detail


def test_probe_webview_reports_missing_loader(monkeypatch):
    """缺少 WebView2Loader.dll 时要给出可读原因，而不是抛异常。"""
    from app import webview2_host

    monkeypatch.setattr(webview2_host, "find_loader_dll", lambda: None)
    name, ok, detail = desktop._probe_webview()
    assert ok is False
    assert "WebView2Loader.dll" in detail


def test_probe_webview_reports_missing_runtime(monkeypatch, tmp_path):
    """有 loader 但没装 Runtime → 必须判 FAIL 并给出下载地址。

    这是「双击没反应」最常见的原因，探针必须能说清楚，而不是报「缺 .NET」。
    """
    from app import webview2_host

    fake = tmp_path / "WebView2Loader.dll"
    fake.write_bytes(b"MZ")
    monkeypatch.setattr(webview2_host, "find_loader_dll", lambda: fake)
    monkeypatch.setattr(webview2_host, "webview2_runtime_version", lambda: None)

    name, ok, detail = desktop._probe_webview()
    assert ok is False
    assert "WebView2 Runtime" in detail
    assert "developer.microsoft.com" in detail


def test_probe_webview_does_not_mention_dotnet(monkeypatch, tmp_path):
    """回归锁：探针不得再把失败归因于 .NET Desktop Runtime。

    历史误导：WinForms 版 WebView2 在 CoreCLR 下根本不可用（ContextMenu 被移除），
    失败的真实原因要么是 CLR 加载策略，要么是 WebView2 Runtime 缺失。现在窗口
    已脱离 .NET，这个提示必须消失。
    """
    from app import webview2_host

    monkeypatch.setattr(webview2_host, "find_loader_dll", lambda: None)
    _, ok, detail = desktop._probe_webview()
    assert ok is False
    assert ".NET" not in detail


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


def test_probe_webview_verifies_runtime_not_just_loader(monkeypatch, tmp_path):
    """回归锁：探针必须验证 **WebView2 Runtime 真的能创建环境**。

    踩过的坑：早期只验证模块能 import（或只看版本号读得到）就报 PASS，而真机上
    窗口起不来——真机上出现过「版本号读得到、目录也在，但建环境返回
    0x80070002」，所以判据是真实创建一次环境。
    """
    from app import webview2_host

    fake = tmp_path / "WebView2Loader.dll"
    fake.write_bytes(b"MZ")
    monkeypatch.setattr(webview2_host, "find_loader_dll", lambda: fake)
    monkeypatch.setattr(
        webview2_host, "probe_environment", lambda timeout=0: (True, "可创建环境")
    )
    # loader 在、Runtime 不在 → 必须 FAIL
    monkeypatch.setattr(webview2_host, "webview2_runtime_version", lambda: None)
    _, ok, _ = desktop._probe_webview()
    assert ok is False

    # 两者都在 → PASS
    monkeypatch.setattr(webview2_host, "webview2_runtime_version", lambda: "152.0.0.0")
    name, ok, detail = desktop._probe_webview()
    assert ok is True
    assert "152.0.0.0" in detail

    # 版本号读得到、但环境建不出来（安装不完整的真实故障）→ 必须 FAIL
    monkeypatch.setattr(
        webview2_host,
        "probe_environment",
        lambda timeout=0: (False, "运行时可定位但无法创建环境"),
    )
    _, ok, detail = desktop._probe_webview()
    assert ok is False
    assert "无法创建环境" in detail


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


# ---------- 窗口呈现：只有原生窗口，不再有浏览器路径 ----------

def test_run_window_passes_expected_options(monkeypatch, tmp_path):
    """窗口参数要正确转交：尺寸、底色（#RRGGBB → RGB 三元组）、图标、数据目录。"""
    from app import runtime_env, webview2_host

    captured = {}

    def fake_run_window(url, **kw):
        captured["url"] = url
        captured.update(kw)
        return 0

    monkeypatch.setattr(webview2_host, "run_window", fake_run_window)
    monkeypatch.setattr(runtime_env, "resolve_data_dir", lambda *_: str(tmp_path))
    monkeypatch.setattr(runtime_env, "find_app_icon", lambda: None)

    args = desktop.parse_args(["--data-dir", str(tmp_path), "--window-size", "1280x800"])
    rc = desktop._run_window("http://127.0.0.1:8566/", args, None, None)

    assert rc == 0
    assert captured["url"] == "http://127.0.0.1:8566/"
    assert (captured["width"], captured["height"]) == (1280, 800)
    assert captured["title"] == "videoRAG"
    assert captured["background"] == (0x14, 0x17, 0x1C)
    assert "webview" in str(captured["storage_dir"])


def test_run_window_reports_failure_without_browser_fallback(monkeypatch, tmp_path):
    """窗口起不来时：返回非 0、弹错误提示，但**绝不**改成打开浏览器。"""
    from app import runtime_env, webview2_host

    def boom(*a, **kw):
        raise webview2_host.Wv2Error("WebView2 Runtime 未安装")

    fatal_messages = []
    monkeypatch.setattr(webview2_host, "run_window", boom)
    monkeypatch.setattr(runtime_env, "resolve_data_dir", lambda *_: str(tmp_path))
    monkeypatch.setattr(runtime_env, "find_app_icon", lambda: None)
    monkeypatch.setattr(desktop, "_fatal", lambda msg, lf: fatal_messages.append(msg))

    # 若还有浏览器降级，这里会去调 webbrowser.open——显式让它炸
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

    args = desktop.parse_args(["--data-dir", str(tmp_path)])
    rc = desktop._run_window("http://127.0.0.1:8566/", args, None, None)

    assert rc == 1
    assert fatal_messages, "必须给用户一个可读的错误提示"
    assert "WebView2 Runtime" in fatal_messages[0]
    assert not opened, "不得回落到系统浏览器"


def test_run_window_shuts_down_local_server(tmp_path):
    """窗口关闭后，本进程内的本地服务要一并退出（否则会留下后台进程）。"""
    from app import runtime_env, webview2_host

    stopped = []
    monkeypatch_targets = []
    monkeypatch_targets.append(
        (webview2_host, "run_window", lambda *a, **kw: 0)
    )
    monkeypatch_targets.append(
        (runtime_env, "resolve_data_dir", lambda *_: str(tmp_path))
    )
    monkeypatch_targets.append(
        (runtime_env, "find_app_icon", lambda: None)
    )
    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    try:
        for obj, name, value in monkeypatch_targets:
            mp.setattr(obj, name, value)
        mp.setattr(desktop, "_shutdown", lambda holder, done, timeout=10.0: stopped.append(True))

        args = desktop.parse_args(["--data-dir", str(tmp_path)])
        desktop._run_window("http://x/", args, {"server": object()}, object())
    finally:
        mp.undo()

    assert stopped, "窗口关闭后必须通知服务退出"


def test_bg_rgb_parses_hex():
    assert desktop._bg_rgb() == (0x14, 0x17, 0x1C)


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
        effective = desktop._configure_logging("info", log_file, to_console=False)
        assert effective == log_file

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


def test_configure_logging_falls_back_when_primary_unwritable(tmp_path, monkeypatch):
    """主路径不可写时要退到备选位置，并返回**实际生效**的路径。

    窗口形态没有控制台，日志是唯一的可观测性来源；数据目录被锁时若直接放弃，
    用户就只剩一个弹窗，什么线索都没有。
    """
    from pathlib import Path

    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        primary = tmp_path / "locked" / "logs" / "videorag.log"
        fallback_dir = tmp_path / "fallback"
        fallback_dir.mkdir()

        real_write_bytes = Path.write_bytes

        def deny_primary(self, *a, **kw):
            # 只让主路径下的写入失败
            if str(self).startswith(str(tmp_path / "locked")):
                raise PermissionError(13, "Access is denied", str(self))
            return real_write_bytes(self, *a, **kw)

        monkeypatch.setattr(Path, "write_bytes", deny_primary)
        # 让备选目录可写：把候选固定成 [主路径, 备选]
        monkeypatch.setattr(
            desktop,
            "_log_file_candidates",
            lambda p: [primary, fallback_dir / "videorag.log"],
        )

        effective = desktop._configure_logging("info", primary, to_console=False)
        assert effective is not None
        assert effective != primary
        assert effective.parent == fallback_dir

        logging.getLogger("videorag.test2").info("fallback-log")
        for handler in root.handlers:
            handler.flush()
        assert "fallback-log" in effective.read_text(encoding="utf-8")
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
