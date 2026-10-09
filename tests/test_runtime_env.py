"""桌面运行时引导（app/runtime_env.py）测试。

这些逻辑只在 Windows 桌面包 / 裸机形态生效，Docker 不经过——因此这里同时验证
「引导行为正确」与「不影响 Docker 语义」（DATA_DIR 注入时结果仍是 /data）。
"""
from __future__ import annotations

import os
import socket
from pathlib import Path, PureWindowsPath

import pytest

from app import runtime_env

FFMPEG_EXE = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"

# bootstrap 会**直接**写 os.environ（固化 DATA_DIR / COOKIE_DIR / HF_ENDPOINT），
# 这类写入不受 monkeypatch 管辖，必须显式快照与还原。
# 真实踩过：DATA_DIR 泄漏后，test_config 的「默认数据目录」断言读到临时路径而失败。
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


# ---------- resolve_data_dir ----------

def test_resolve_data_dir_cli_wins(tmp_path):
    """CLI 显式指定优先级最高。"""
    got = runtime_env.resolve_data_dir(str(tmp_path), env={"DATA_DIR": "/from-env"})
    assert got == str(tmp_path.resolve())


def test_resolve_data_dir_env_priority():
    """VIDEORAG_DATA_DIR 优先于 DATA_DIR（桌面专用名优先，兼容项目既有约定）。"""
    got = runtime_env.resolve_data_dir(
        None, env={"VIDEORAG_DATA_DIR": "/a", "DATA_DIR": "/b"}
    )
    assert got.replace("\\", "/") == "/a"


def test_resolve_data_dir_docker_semantics_unchanged(tmp_path):
    """Docker 注入 DATA_DIR=/data → 结果就是 /data（容器行为与历史一致）。"""
    got = runtime_env.resolve_data_dir(None, env={"DATA_DIR": "/data"})
    assert got.replace("\\", "/") == "/data"


def test_resolve_data_dir_portable(monkeypatch, tmp_path):
    """便携模式：用 bundle 同级 ./data（放 U 盘即可带走）。"""
    monkeypatch.setattr(runtime_env, "bundle_root", lambda: tmp_path)
    got = runtime_env.resolve_data_dir(None, env={"VIDEORAG_PORTABLE": "1"})
    assert Path(got) == (tmp_path / "data").resolve()


@pytest.mark.parametrize("flag", ["0", "false", "False", ""])
def test_resolve_data_dir_portable_disabled(monkeypatch, tmp_path, flag):
    """便携开关为假值时走平台默认，不进入便携分支。"""
    got = runtime_env.resolve_data_dir(
        None, env={"VIDEORAG_PORTABLE": flag, "LOCALAPPDATA": str(tmp_path)}
    )
    assert Path(got) != (runtime_env.bundle_root() / "data").resolve()


def test_windows_default_data_dir_uses_localappdata(monkeypatch, tmp_path):
    """Windows 默认目录 = %LOCALAPPDATA%\\videoRAG（在任何平台上都可断言）。

    这里刻意用 PureWindowsPath 构造期望值：测试不再依赖运行平台，
    也就不会出现在 Linux 上构造 WindowsPath 直接抛错的假失败。
    """
    got = runtime_env.windows_default_data_dir({"LOCALAPPDATA": r"C:\Users\me\AppData\Local"})
    assert got == r"C:\Users\me\AppData\Local\videoRAG"


def test_windows_default_data_dir_falls_back_to_home(monkeypatch, tmp_path):
    """LOCALAPPDATA 缺失时退回用户主目录下的 videoRAG。"""
    monkeypatch.setattr(runtime_env.Path, "home", lambda: str(tmp_path))
    got = runtime_env.windows_default_data_dir({})
    assert got == str(PureWindowsPath(str(tmp_path)) / "videoRAG")


def test_resolve_data_dir_windows_default(monkeypatch, tmp_path):
    """Windows 无显式配置 → 走 Windows 默认分支（而非 /data）。

    只断言分支选择与 env 传递；具体路径拼法由上面的
    windows_default_data_dir 用例覆盖（那部分与平台无关）。
    """
    monkeypatch.setattr(os, "name", "nt")
    got = runtime_env.resolve_data_dir(None, env={"LOCALAPPDATA": str(tmp_path)})
    assert got != "/data"
    assert got.endswith("videoRAG")


def test_resolve_data_dir_posix_default(monkeypatch):
    """POSIX 无显式配置 → /data（与 app/config.py 默认值一致）。

    在 Windows 上跑需显式把 os.name 伪装成 posix，否则会走 %LOCALAPPDATA% 分支。
    """
    monkeypatch.setattr(os, "name", "posix")
    got = runtime_env.resolve_data_dir(None, env={})
    assert got == "/data"


def test_resolve_data_dir_posix_ignores_localappdata(monkeypatch, tmp_path):
    """POSIX 下即使环境里有 LOCALAPPDATA 也不走 Windows 分支。"""
    monkeypatch.setattr(os, "name", "posix")
    got = runtime_env.resolve_data_dir(None, env={"LOCALAPPDATA": str(tmp_path)})
    assert got == "/data"


# ---------- ffmpeg 定位与 PATH 注入 ----------

def test_find_ffmpeg_dir_missing(tmp_path):
    assert runtime_env.find_ffmpeg_dir(tmp_path) is None


@pytest.mark.parametrize("sub", ["vendor/ffmpeg", "vendor/ffmpeg/bin", "ffmpeg", "bin"])
def test_find_ffmpeg_dir_candidate_layouts(tmp_path, sub):
    """多个候选布局都能命中（不同 ffmpeg 发行包解压结构不同）。"""
    d = tmp_path / sub
    d.mkdir(parents=True)
    (d / FFMPEG_EXE).write_bytes(b"")
    assert runtime_env.find_ffmpeg_dir(tmp_path) == d


def test_apply_vendor_path_injects_and_is_idempotent(monkeypatch, tmp_path):
    """PATH 前置注入，且重复调用不产生重复项。"""
    d = tmp_path / "vendor" / "ffmpeg"
    d.mkdir(parents=True)
    (d / FFMPEG_EXE).write_bytes(b"")
    monkeypatch.setenv("PATH", "/pre-existing")

    first = runtime_env.apply_vendor_path(tmp_path)
    assert first == str(d)
    # 已注入的目录在 PATH 最前，优先于系统同名程序
    assert os.environ["PATH"].split(os.pathsep)[0] == str(d)

    runtime_env.apply_vendor_path(tmp_path)
    assert os.environ["PATH"].split(os.pathsep).count(str(d)) == 1


def test_apply_vendor_path_no_bundle_keeps_path(monkeypatch, tmp_path):
    """未捆绑 ffmpeg 时不改动 PATH（交给系统 PATH 里的 ffmpeg）。"""
    monkeypatch.setenv("PATH", "/only-this")
    assert runtime_env.apply_vendor_path(tmp_path) is None
    assert os.environ["PATH"] == "/only-this"


# ---------- 端口探测 ----------

def test_is_port_free_and_find_free_port_skips_busy():
    """被占用的端口被跳过，返回可用端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        busy = srv.getsockname()[1]
        assert not runtime_env.is_port_free(busy)

        got = runtime_env.find_free_port(busy, scan=5)
        assert got != busy
        assert runtime_env.is_port_free(got)


def test_find_free_port_falls_back_to_ephemeral():
    """候选段内全被占用时交给系统分配临时端口（而不是抛错）。"""
    holders = []
    try:
        base = None
        for _ in range(3):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            holders.append(s)
        base = holders[0].getsockname()[1]
        got = runtime_env.find_free_port(base, scan=1)  # 只管 base 这一个
        assert got != base
        assert runtime_env.is_port_free(got)
    finally:
        for s in holders:
            s.close()


# ---------- 静态资源定位 ----------

def test_find_static_dir(tmp_path):
    """找到含 index.html 的 web/dist。"""
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    assert runtime_env.find_static_dir(str(dist)) == str(dist)


def test_find_static_dir_missing(tmp_path):
    """目录不存在返回 None（调用方降级为纯 API 模式并告警）。"""
    assert runtime_env.find_static_dir(str(tmp_path / "nope")) is None


# ---------- 应用图标 ----------

def test_find_app_icon_packaged_layout(monkeypatch, tmp_path):
    """打包布局：spec 把 ico 放到 _internal/assets。"""
    (tmp_path / "assets").mkdir()
    ico = tmp_path / "assets" / "videorag.ico"
    ico.write_bytes(b"ico")
    monkeypatch.setattr(runtime_env, "resource_root", lambda: tmp_path)
    monkeypatch.setattr(runtime_env, "bundle_root", lambda: tmp_path)

    got = runtime_env.find_app_icon()
    assert got is not None and got.name == "videorag.ico"


def test_find_app_icon_source_layout(monkeypatch, tmp_path):
    """源码布局：仓库内的 packaging/logo。"""
    (tmp_path / "packaging" / "logo").mkdir(parents=True)
    (tmp_path / "packaging" / "logo" / "videorag.ico").write_bytes(b"ico")
    monkeypatch.setattr(runtime_env, "resource_root", lambda: tmp_path)
    monkeypatch.setattr(runtime_env, "bundle_root", lambda: tmp_path)

    assert runtime_env.find_app_icon() is not None


def test_find_app_icon_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_env, "resource_root", lambda: tmp_path)
    monkeypatch.setattr(runtime_env, "bundle_root", lambda: tmp_path)
    assert runtime_env.find_app_icon() is None


# ---------- bootstrap ----------

def test_bootstrap_sets_data_dir_and_creates_it(monkeypatch, tmp_path):
    """引导固化 DATA_DIR 供 pydantic-settings 读取，并预创建目录。"""
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("COOKIE_DIR", raising=False)
    monkeypatch.delenv("HF_ENDPOINT", raising=False)

    target = tmp_path / "vrdata"
    info = runtime_env.bootstrap(str(target))

    assert info["data_dir"] == str(target.resolve())
    assert os.environ["DATA_DIR"] == str(target.resolve())
    assert target.is_dir()
    # 不设 COOKIE_DIR：交给 Settings 从 data_dir 派生（单一真相源）
    assert "COOKIE_DIR" not in os.environ
    # 刻意不设置 HF_HOME：保持与 Docker 形态一致的模型落点（<data>/models）
    assert "HF_HOME" not in os.environ


def test_bootstrap_does_not_touch_user_cookie_dir(monkeypatch, tmp_path):
    """用户显式设置的 COOKIE_DIR 不因引导而改变。"""
    monkeypatch.setenv("COOKIE_DIR", "/custom/cookies")
    runtime_env.bootstrap(str(tmp_path / "d"))
    assert os.environ["COOKIE_DIR"] == "/custom/cookies"


def test_bootstrap_hf_mirror_opt_in(monkeypatch, tmp_path):
    """HF 镜像仅在显式开启时设置，且不覆盖用户已有配置。"""
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    info = runtime_env.bootstrap(str(tmp_path / "d1"), use_hf_mirror=True)
    assert info["hf_endpoint"] == "https://hf-mirror.com"
    assert os.environ["HF_ENDPOINT"] == "https://hf-mirror.com"

    monkeypatch.setenv("HF_ENDPOINT", "https://my.mirror")
    info2 = runtime_env.bootstrap(str(tmp_path / "d2"), use_hf_mirror=True)
    assert os.environ["HF_ENDPOINT"] == "https://my.mirror"
    assert info2["hf_endpoint"] == "https://my.mirror"


def test_format_summary_mentions_missing_ffmpeg(monkeypatch, tmp_path):
    """摘要要显式暴露 ffmpeg 缺失，便于用户第一时间定位问题。"""
    text = runtime_env.format_summary(
        {"data_dir": str(tmp_path), "ffmpeg_dir": None, "ffmpeg_available": False}
    )
    assert "MISSING" in text
    assert str(tmp_path) in text
