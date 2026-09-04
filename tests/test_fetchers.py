from dataclasses import dataclass

import pytest

from app.core.fetchers.base import FetchedMedia
from app.core.fetchers.ytdlp import FetchError, YtdlpFetcher, detect_platform, parse_subtitle_file


@dataclass
class FakeResult:
    returncode: int
    stdout: str = ""


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/watch?v=abc", "youtube"),
        ("https://youtu.be/abc", "youtube"),
        ("https://www.bilibili.com/video/BV1xx411c7mD", "bilibili"),
        ("https://b23.tv/abc", "bilibili"),
        ("https://www.douyin.com/video/123", "douyin"),
        ("https://www.xiaohongshu.com/explore/abc", "xhs"),
        ("https://example.com/file.mp4", "generic"),
    ],
)
def test_detect_platform(url, expected):
    assert detect_platform(url) == expected


def test_parse_vtt(tmp_path):
    p = tmp_path / "a.vtt"
    p.write_text(
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:02.500\n"
        "align:start position:0%\n"
        "大家好\n\n"
        "00:00:02.500 --> 00:00:05.000\n"
        "欢迎观看\n",
        encoding="utf-8",
    )
    segs = parse_subtitle_file(str(p))
    assert segs[0] == (0.0, 2.5, "大家好")
    assert segs[1] == (2.5, 5.0, "欢迎观看")


def test_parse_srt(tmp_path):
    p = tmp_path / "a.srt"
    p.write_text(
        "1\n00:00:01,000 --> 00:00:03,500\n你好\n\n"
        "2\n00:00:03,500 --> 00:00:06,000\n世界\n",
        encoding="utf-8",
    )
    segs = parse_subtitle_file(str(p))
    assert segs[0] == (1.0, 3.5, "你好")
    assert segs[1] == (3.5, 6.0, "世界")


def test_fetch_subtitle_when_available(tmp_path):
    def fake_runner(args):
        assert "--write-subs" in args
        (tmp_path / "a.en.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n字幕测试\n")
        return FakeResult(0)

    fetcher = YtdlpFetcher(runner=fake_runner)
    media = _run(fetcher, tmp_path)
    assert media.kind == "subtitle"
    assert "字幕测试" in media.subtitle_text


def test_fetch_falls_back_to_audio(tmp_path):
    def fake_runner(args):
        if "--write-subs" in args:
            return FakeResult(0)  # 模拟无字幕，不写文件
        (tmp_path / "audio.mp3").touch()
        return FakeResult(0)

    fetcher = YtdlpFetcher(runner=fake_runner)
    media = _run(fetcher, tmp_path)
    assert media.kind == "audio"
    assert media.path.endswith("audio.mp3")


def test_fetch_raises_when_all_fail(tmp_path):
    def fake_runner(args):
        return FakeResult(0)  # 什么都不写

    fetcher = YtdlpFetcher(runner=fake_runner)
    with pytest.raises(FetchError):
        _run(fetcher, tmp_path)


def test_cookie_arg_passed_for_platform(tmp_path):
    cookie_dir = tmp_path / "cookies"
    cookie_dir.mkdir()
    (cookie_dir / "douyin.txt").write_text("# Netscape HTTP Cookie File\n")

    seen = []

    def fake_runner(args):
        seen.append(args)
        if "--write-subs" in args:
            (tmp_path / "a.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n字幕\n")
        return FakeResult(0)

    fetcher = YtdlpFetcher(runner=fake_runner, cookie_dir=str(cookie_dir))
    media = _run(fetcher, tmp_path, url="https://www.douyin.com/video/123")
    assert media.kind == "subtitle"
    assert f"--cookies={cookie_dir / 'douyin.txt'}" in seen[0]


def test_douyin_without_cookie_raises_hint(tmp_path):
    fetcher = YtdlpFetcher(runner=lambda args: FakeResult(0), cookie_dir=str(tmp_path / "empty"))
    with pytest.raises(FetchError, match="cookie"):
        _run(fetcher, tmp_path, url="https://www.douyin.com/video/123")


def test_xhs_without_cookie_raises_hint(tmp_path):
    fetcher = YtdlpFetcher(runner=lambda args: FakeResult(0), cookie_dir=str(tmp_path / "empty"))
    with pytest.raises(FetchError, match="cookie"):
        _run(fetcher, tmp_path, url="https://www.xiaohongshu.com/explore/abc")


def test_generic_url_no_cookie_required(tmp_path):
    def fake_runner(args):
        (tmp_path / "video.mp4").touch()
        return FakeResult(0)

    fetcher = YtdlpFetcher(runner=fake_runner, cookie_dir=str(tmp_path / "empty"))
    media = _run(fetcher, tmp_path, url="https://example.com/file.mp4")
    assert media.kind == "video"


def test_bilibili_uses_cookie_when_present(tmp_path):
    cookie_dir = tmp_path / "cookies"
    cookie_dir.mkdir()
    (cookie_dir / "bilibili.txt").write_text("# Netscape HTTP Cookie File\n")
    seen = []

    def fake_runner(args):
        seen.append(args)
        return FakeResult(0)

    fetcher = YtdlpFetcher(runner=fake_runner, cookie_dir=str(cookie_dir))
    with pytest.raises(FetchError):
        _run(fetcher, tmp_path, url="https://www.bilibili.com/video/BV1xx411c7mD")
    assert f"--cookies={cookie_dir / 'bilibili.txt'}" in seen[0]


def test_run_ytdlp_invokes_python_module(monkeypatch):
    import subprocess
    import sys

    from app.core.fetchers import ytdlp

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(ytdlp.subprocess, "run", fake_run)
    ytdlp.run_ytdlp(["--version"])
    assert captured["cmd"][:3] == [sys.executable, "-m", "yt_dlp"]


def _run(fetcher, tmp_path, url="https://www.youtube.com/watch?v=abc"):
    import asyncio

    return asyncio.run(fetcher.fetch(url, str(tmp_path)))
