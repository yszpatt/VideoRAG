from dataclasses import dataclass

import pytest

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
    """源码 / 裸机运行：用当前解释器 ``-m yt_dlp``，不依赖 PATH 里的 yt-dlp 命令。"""
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


def test_run_ytdlp_frozen_uses_inproc(monkeypatch):
    """打包运行：改为**同进程**调用，不再起子进程。

    历史故障：打包后 ``sys.executable`` 是应用 exe，``python -m yt_dlp`` 不成立；
    而 windowed 形态（console=False）连 stdout 句柄都没有，子进程方式即使改了命令
    也拿不到输出。故打包形态直接同进程调用并重定向捕获。
    """
    import subprocess
    import sys

    from app.core.fetchers import ytdlp

    monkeypatch.setattr(ytdlp.sys, "frozen", True, raising=False)
    called = {}

    def fake_inproc(args):
        called["args"] = list(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(ytdlp, "_run_ytdlp_inproc", fake_inproc)
    monkeypatch.setattr(
        ytdlp.subprocess, "run",
        lambda *a, **kw: pytest.fail("打包形态不应再走子进程"),
    )

    proc = ytdlp.run_ytdlp(["--version"])
    assert called["args"] == ["--version"]
    assert proc.returncode == 0
    assert sys  # 明确本测试依赖 sys 语义（sys.frozen）


def test_run_ytdlp_not_frozen_uses_subprocess(monkeypatch):
    """未打包时保持既有行为：当前解释器 ``-m yt_dlp``（隔离性好）。"""
    import subprocess

    from app.core.fetchers import ytdlp

    monkeypatch.delattr(ytdlp.sys, "frozen", raising=False)
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(ytdlp.subprocess, "run", fake_run)
    monkeypatch.setattr(
        ytdlp, "_run_ytdlp_inproc",
        lambda args: pytest.fail("非打包形态不应同进程调用"),
    )

    ytdlp.run_ytdlp(["--version"])
    assert captured["cmd"][1:3] == ["-m", "yt_dlp"]


def test_inproc_captures_output_and_restores_globals(monkeypatch):
    """同进程调用：捕获 yt-dlp 的 stdout，且必须还原 sys.argv/stdout/stderr。"""
    import sys

    import yt_dlp

    from app.core.fetchers import ytdlp

    def fake_main(argv):
        print("2026.08.19")
        return 0

    monkeypatch.setattr(yt_dlp, "main", fake_main)
    before_argv, before_out, before_err = list(sys.argv), sys.stdout, sys.stderr

    proc = ytdlp._run_ytdlp_inproc(["--version"])

    assert proc.returncode == 0
    assert "2026.08.19" in proc.stdout
    # 不还原会污染主进程的 print / logging（尤其在窗口形态下 stdout 是 None 时）
    assert list(sys.argv) == before_argv
    assert sys.stdout is before_out
    assert sys.stderr is before_err


def test_inproc_passes_args_without_program_name(monkeypatch):
    """回归锁：传给 ``yt_dlp.main`` 的参数**不能**含程序名。

    踩过的坑：``yt_dlp.main(argv)`` 内部直接走 ``argparse.parse_args(argv)``，
    期望的是不含程序名的参数列表。当时传了 ``["yt-dlp", *args]``，那个多出来的
    ``"yt-dlp"`` 被当成第二个 URL，报
    ``You've asked yt-dlp to download the URL "yt-dlp"`` ——
    表现为**元数据采集静默失败**（meta_source=none）。

    更阴的是：``--version`` 这类提前退出的路径不受影响，所以自检里的
    ``yt-dlp --version`` 探针完全发现不了，必须靠这条测试钉住。
    """
    import sys

    import yt_dlp

    from app.core.fetchers import ytdlp

    seen = {}

    def fake_main(argv):
        seen["argv"] = list(argv)
        seen["sys_argv"] = list(sys.argv)
        return 0

    monkeypatch.setattr(yt_dlp, "main", fake_main)
    args = ["--dump-json", "--skip-download", "https://example.com/v"]
    ytdlp._run_ytdlp_inproc(args)

    # main 收到的不含程序名
    assert seen["argv"] == args
    assert "yt-dlp" not in seen["argv"]
    # 但 sys.argv 仍是含程序名的完整形式（yt-dlp 内部会读它）
    assert seen["sys_argv"] == ["yt-dlp", *args]


def test_inproc_converts_systemexit_to_code(monkeypatch):
    import yt_dlp

    from app.core.fetchers import ytdlp

    def boom(argv):
        raise SystemExit(2)

    monkeypatch.setattr(yt_dlp, "main", boom)
    assert ytdlp._run_ytdlp_inproc(["--version"]).returncode == 2


def test_inproc_turns_exception_into_failure(monkeypatch):
    """异常要转成非 0 退出码 + stderr 线索，让上层能记录并降级。"""
    import yt_dlp

    from app.core.fetchers import ytdlp

    def boom(argv):
        raise RuntimeError("network down")

    monkeypatch.setattr(yt_dlp, "main", boom)
    proc = ytdlp._run_ytdlp_inproc(["--version"])
    assert proc.returncode == 1
    assert "network down" in proc.stderr


def _run(fetcher, tmp_path, url="https://www.youtube.com/watch?v=abc"):
    import asyncio

    return asyncio.run(fetcher.fetch(url, str(tmp_path)))
