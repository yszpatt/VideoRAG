"""媒体时长探测（``app/api/videos.py::_probe_duration``）的测试。

为什么单独测它
--------------
桌面发行版**刻意不捆绑 ffprobe**（它单独约 98MB），改为在 ffprobe 缺失时用
``ffmpeg -i`` 的信息输出解析时长。这条回退路径若不测，很容易在某次改动后
静默失效——表现只是「导入后视频时长是空的」，不影响主流程，很难被发现。

另外保证：任何失败都必须返回 ``None`` 而不是抛异常，因为时长只是展示用，
绝不能阻断导入。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.api import videos as V


class _FakeProc:
    def __init__(self, out: bytes = b"", err: bytes = b"") -> None:
        self._out = out
        self._err = err

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._out, self._err


def _run(coro):
    return asyncio.run(coro)


def test_uses_ffprobe_when_available(monkeypatch, tmp_path):
    """ffprobe 存在时优先用它（最准）。"""
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _FakeProc(out=b"12.5\n")

    monkeypatch.setattr(V.shutil, "which", lambda n: "/usr/bin/ffprobe" if n == "ffprobe" else None)
    monkeypatch.setattr(V.asyncio, "create_subprocess_exec", fake_exec)

    assert _run(V._probe_duration(tmp_path / "a.mp4")) == 12.5
    assert "ffprobe" in captured["args"][0]


def test_falls_back_to_ffmpeg_when_ffprobe_missing(monkeypatch, tmp_path):
    """ffprobe 缺失时用 ``ffmpeg -i`` 的 `Duration:` 行解析时长。

    这是发行版的实际路径（发布包不带 ffprobe），必须始终可用。
    """
    ffmpeg_err = (
        b"Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'a.mp4':\n"
        b"  Duration: 00:01:23.45, start: 0.000000, bitrate: 800 kb/s\n"
        b"    Stream #0:0: Video: h264\n"
    )
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _FakeProc(err=ffmpeg_err)

    monkeypatch.setattr(V.shutil, "which", lambda n: "/usr/bin/ffmpeg" if n == "ffmpeg" else None)
    monkeypatch.setattr(V.asyncio, "create_subprocess_exec", fake_exec)

    got = _run(V._probe_duration(tmp_path / "a.mp4"))
    # 1:23.45 == 83.45 秒
    assert got == pytest.approx(83.45, abs=0.01)
    assert "ffmpeg" in captured["args"][0]


def test_ffmpeg_fallback_parses_hours(monkeypatch, tmp_path):
    """超过 1 小时的时长必须正确换算。"""
    err = b"  Duration: 02:03:04.50, start: 0.0\n"

    async def fake_exec(*args, **kwargs):
        return _FakeProc(err=err)

    monkeypatch.setattr(V.shutil, "which", lambda n: "/usr/bin/ffmpeg" if n == "ffmpeg" else None)
    monkeypatch.setattr(V.asyncio, "create_subprocess_exec", fake_exec)

    assert _run(V._probe_duration(tmp_path / "a.mp4")) == pytest.approx(2 * 3600 + 3 * 60 + 4.5)


def test_returns_none_when_both_missing(monkeypatch, tmp_path):
    """两个都不可用 → None（不抛异常）。"""
    monkeypatch.setattr(V.shutil, "which", lambda n: None)
    assert _run(V._probe_duration(tmp_path / "a.mp4")) is None


def test_returns_none_when_output_unparsable(monkeypatch, tmp_path):
    """ffmpeg 输出里没有 Duration 行 → None。"""
    async def fake_exec(*args, **kwargs):
        return _FakeProc(err=b"a.mp4: Invalid data found when processing input\n")

    monkeypatch.setattr(V.shutil, "which", lambda n: "/usr/bin/ffmpeg" if n == "ffmpeg" else None)
    monkeypatch.setattr(V.asyncio, "create_subprocess_exec", fake_exec)

    assert _run(V._probe_duration(tmp_path / "a.mp4")) is None


def test_returns_none_when_ffprobe_errors(monkeypatch, tmp_path):
    """ffprobe 存在但解析失败 → 继续尝试 ffmpeg，最终仍失败则 None。"""
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args[0])
        if "ffprobe" in args[0]:
            raise OSError("boom")
        return _FakeProc(err=b"no duration here\n")

    monkeypatch.setattr(
        V.shutil, "which", lambda n: f"/usr/bin/{n}" if n in ("ffmpeg", "ffprobe") else None
    )
    monkeypatch.setattr(V.asyncio, "create_subprocess_exec", fake_exec)

    assert _run(V._probe_duration(tmp_path / "a.mp4")) is None
    # 走过 ffprobe 又走过 ffmpeg
    assert any("ffprobe" in c for c in calls)
    assert any("ffmpeg" in c for c in calls)


def test_never_raises_on_unexpected_error(monkeypatch, tmp_path):
    """任何意外异常都不能冒出来：时长只是展示用，不能阻断导入。"""
    def boom(*a, **kw):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(V.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(V.asyncio, "create_subprocess_exec", boom)

    assert _run(V._probe_duration(tmp_path / "a.mp4")) is None
