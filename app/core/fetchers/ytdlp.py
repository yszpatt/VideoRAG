import asyncio
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from app.core.fetchers.base import FetchedMedia

Runner = Callable[[Sequence[str]], object]

_TIMECODE_RE = re.compile(
    r"(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})[.,](?P<ms>\d{3})"
)

SUBTITLE_EXTS = (".vtt", ".srt")
AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".webm", ".opus")
VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov")

# 元数据抓取（E1）：简介截断上限（入库）与评论单条截断上限
MAX_DESCRIPTION_CHARS = 8000
MAX_COMMENT_CHARS = 2000

# 需要 cookie 的平台；其余（bilibili/youtube/generic）不强制
COOKIE_REQUIRED = ("douyin", "xhs")
COOKIE_FILES = ("douyin", "xhs", "bilibili", "youtube")


class FetchError(Exception):
    """所有获取策略都失败。"""


class FetchMetadataError(Exception):
    """元数据抓取失败（E1）。隔离语义：不 fail 视频主流程。"""


def detect_platform(url: str) -> str:
    host = (url.split("/")[2] if len(url.split("/")) > 2 else "").lower()
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "bilibili.com" in host or "b23.tv" in host:
        return "bilibili"
    if "douyin.com" in host or "iesdouyin.com" in host:
        return "douyin"
    if "xiaohongshu.com" in host or "xhslink.com" in host:
        return "xhs"
    return "generic"


def _ts_to_sec(ts: str) -> float:
    m = _TIMECODE_RE.match(ts.strip())
    if not m:
        return 0.0
    h, mm, s, ms = (int(m.group(g)) for g in ("h", "m", "s", "ms"))
    return h * 3600 + mm * 60 + s + ms / 1000


def parse_subtitle_file(path: str) -> list[tuple[float, float, str]]:
    """解析 .vtt / .srt 为 [(start_sec, end_sec, text)]。"""
    content = Path(path).read_text(encoding="utf-8", errors="ignore")
    if path.endswith(".srt"):
        return _parse_srt(content)
    return _parse_vtt(content)


def _clean_line(line: str) -> str:
    line = re.sub(r"<[^>]+>", "", line)  # 去标签
    line = re.sub(r"align:.*?position:\d+%", "", line)
    return line.strip()


def _parse_vtt(content: str) -> list[tuple[float, float, str]]:
    out: list[tuple[float, float, str]] = []
    cur: tuple[float, float] | None = None
    buf: list[str] = []
    for line in content.splitlines():
        if "-->" in line:
            parts = line.split("-->")
            if len(parts) == 2:
                cur = (_ts_to_sec(parts[0]), _ts_to_sec(parts[1]))
                buf = []
        elif cur and line.strip():
            text = _clean_line(line)
            if text:
                buf.append(text)
        elif cur:
            if buf:
                out.append((cur[0], cur[1], "".join(buf)))
                cur = None
                buf = []
    if cur and buf:
        out.append((cur[0], cur[1], "".join(buf)))
    return out


def _parse_srt(content: str) -> list[tuple[float, float, str]]:
    out: list[tuple[float, float, str]] = []
    cur: tuple[float, float] | None = None
    buf: list[str] = []
    for line in content.splitlines():
        if "-->" in line:
            parts = line.split("-->")
            if len(parts) == 2:
                cur = (_ts_to_sec(parts[0]), _ts_to_sec(parts[1]))
                buf = []
        elif cur and line.strip() and not line.strip().isdigit():
            buf.append(_clean_line(line))
        elif cur:
            if buf:
                out.append((cur[0], cur[1], "".join(buf)))
                cur = None
                buf = []
    if cur and buf:
        out.append((cur[0], cur[1], "".join(buf)))
    return out


def run_ytdlp(args: Sequence[str], timeout: float = 600) -> subprocess.CompletedProcess:
    # 用当前解释器调 -m yt_dlp，避免依赖 PATH 中的 yt-dlp 命令
    return subprocess.run(
        [sys.executable, "-m", "yt_dlp", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def cookie_args_for(cookie_dir: str | None, platform: str) -> list[str]:
    """为指定平台挑选 cookie 文件（目录下的 cookies/<platform>.txt）。"""
    if not cookie_dir:
        return []
    for name in COOKIE_FILES:
        if platform == name and Path(cookie_dir, f"{name}.txt").is_file():
            return [f"--cookies={Path(cookie_dir, name)}"]
    return []


def _clean_int(value) -> int | None:
    """归一化数值字段：非 int/float 或缺失 → None（各平台字段差异大）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _clean_tags(value) -> list[str] | None:
    if not isinstance(value, list):
        return None
    tags = [str(t) for t in value if t]
    return tags[:50] or None


def _normalize_metadata(info: dict) -> dict:
    """把 yt-dlp dump-json 的 info dict 归一化为本库元数据。

    各平台字段缺失时置 None，不做假设；评论按点赞降序保留全部
    （命令侧 --max-comments 100 已限总量）。
    """
    comments_raw = info.get("comments") or []
    comments: list[dict] = []
    for c in comments_raw:
        if not isinstance(c, dict):
            continue
        text = (c.get("text") or "").strip()
        if not text:
            continue
        ts = _clean_int(c.get("timestamp"))
        comments.append(
            {
                "author": c.get("author") or None,
                "text": text[:MAX_COMMENT_CHARS],
                "like_count": _clean_int(c.get("like_count")) or 0,
                "published_at": (
                    datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                    if ts is not None
                    else None
                ),
            }
        )
    comments.sort(key=lambda c: c["like_count"], reverse=True)

    description = (info.get("description") or "").strip()
    return {
        "title": info.get("title") or None,
        "author": info.get("uploader") or info.get("channel") or None,
        "duration": _clean_int(info.get("duration")),
        "description": description[:MAX_DESCRIPTION_CHARS] or None,
        "thumbnail": info.get("thumbnail") or None,
        "upload_date": info.get("upload_date") or None,
        "view_count": _clean_int(info.get("view_count")),
        "like_count": _clean_int(info.get("like_count")),
        "tags": _clean_tags(info.get("tags")),
        "comment_count": _clean_int(info.get("comment_count")),
        "comments": comments,
    }


async def fetch_metadata(
    url: str,
    cookie_dir: str | None = None,
    runner: Runner | None = None,
    timeout: float = 120.0,
) -> dict:
    """一次 yt-dlp 调用抓取视频元数据 + 限量评论（E1 / 设计文档 1.2a）。

    - ``--dump-json --skip-download --no-playlist``：只取当前分P、不下载；
    - ``--write-comments --max-comments 100``：限制评论翻页量
      （B站评论翻页是抓取阶段最慢环节）；
    - 解析 stdout 逐行 JSON，取第一个对象；超时/失败抛 FetchMetadataError
      （调用方隔离，不 fail 视频）。
    """
    platform = detect_platform(url)
    cookie_args = cookie_args_for(cookie_dir, platform)
    args = [
        "--dump-json",
        "--skip-download",
        "--no-playlist",
        "--write-comments",
        # 注意：yt-dlp 没有全局 --max-comments 选项；之前误用该参数会导致
        # 进程直接 exit 2（no such option），元数据抓取永远失败、被隔离为
        # meta_source=none（卡片只能回退显示链接）。评论上限改为按平台用
        # extractor-args 控制（这里仅对 YouTube 限 100 条，其余平台由
        # --write-comments 默认抓取即可）。B 站实测 1~2s 即可返回。
        "--extractor-args", "youtube:max_comments=100,40,0,0",
        *cookie_args,
        url,
    ]
    # 默认路径走 run_ytdlp(args, timeout=120)；注入的测试 runner 保持单参数签名
    exec_ = runner if runner is not None else (lambda a: run_ytdlp(a, timeout))
    try:
        proc = await asyncio.to_thread(exec_, args)
    except subprocess.TimeoutExpired as e:
        raise FetchMetadataError(f"metadata fetch timeout after {timeout}s") from e
    if proc.returncode != 0:
        stderr = (getattr(proc, "stderr", None) or "")[:300]
        raise FetchMetadataError(
            f"yt-dlp exit {proc.returncode}: {stderr or 'no stderr'}"
        )
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            info = json.loads(line)
        except json.JSONDecodeError:
            continue
        return _normalize_metadata(info)
    raise FetchMetadataError("no dump-json object in yt-dlp stdout")


class YtdlpFetcher:
    """基于 yt-dlp 的获取器：字幕 → 音频 → 视频，逐级降级。

    抖音/小红书需要登录 cookie（Netscape 格式），文件放 cookie_dir/<platform>.txt。
    """

    name = "ytdlp"

    def __init__(self, runner: Runner | None = None, cookie_dir: str | None = None):
        self._runner = runner or run_ytdlp
        self._cookie_dir = cookie_dir

    def _cookie_path_for(self, platform: str) -> str | None:
        if not self._cookie_dir:
            return None
        for name in COOKIE_FILES:
            path = Path(self._cookie_dir) / f"{name}.txt"
            if platform == name and path.is_file():
                return str(path)
        return None

    async def fetch(self, url: str, workdir: str) -> FetchedMedia:
        platform = detect_platform(url)
        cookie_path = self._cookie_path_for(platform)
        if platform in COOKIE_REQUIRED and cookie_path is None:
            raise FetchError(
                f"{platform} 视频需要登录 cookie：请用浏览器插件导出 Netscape 格式 cookie，"
                f"保存为 {Path(self._cookie_dir or '/data/cookies')}/{platform}.txt"
            )
        cookie_args = [f"--cookies={cookie_path}"] if cookie_path else []

        media = await self._try_subtitle(url, workdir, cookie_args)
        if media:
            return media
        media = await self._try_audio(url, workdir, cookie_args)
        if media:
            return media
        media = await self._try_video(url, workdir, cookie_args)
        if media:
            return media
        raise FetchError(f"all fetch strategies failed for {url}")

    async def _try_subtitle(
        self, url: str, workdir: str, cookie_args: list[str]
    ) -> FetchedMedia | None:
        await asyncio.to_thread(
            self._runner,
            [
                "--skip-download",
                "--write-subs",
                "--write-auto-subs",
                "--sub-langs", "zh.*|en.*|zh-Hans|zh-CN",
                "--sub-format", "vtt/srt/best",
                *cookie_args,
                "-o", str(Path(workdir) / "sub.%(ext)s"),
                url,
            ],
        )
        sub_file = next(
            (p for p in Path(workdir).iterdir() if p.suffix in SUBTITLE_EXTS), None
        )
        if sub_file is None:
            return None
        segs = parse_subtitle_file(str(sub_file))
        text = "\n".join(t for _, _, t in segs)
        return FetchedMedia(
            kind="subtitle",
            path=str(sub_file),
            subtitle_text=text,
            meta={"segments": segs},
        )

    async def _try_audio(
        self, url: str, workdir: str, cookie_args: list[str]
    ) -> FetchedMedia | None:
        await asyncio.to_thread(
            self._runner,
            [
                "-f", "bestaudio/best",
                "-x",
                "--audio-format", "mp3",
                "--audio-quality", "0",
                *cookie_args,
                "-o", str(Path(workdir) / "audio.%(ext)s"),
                url,
            ],
        )
        audio = next(
            (p for p in Path(workdir).iterdir() if p.suffix in AUDIO_EXTS), None
        )
        if audio is None:
            return None
        return FetchedMedia(kind="audio", path=str(audio))

    async def _try_video(
        self, url: str, workdir: str, cookie_args: list[str]
    ) -> FetchedMedia | None:
        await asyncio.to_thread(
            self._runner,
            [
                "-f", "bestvideo*+bestaudio/best",
                "--merge-output-format", "mp4",
                *cookie_args,
                "-o", str(Path(workdir) / "video.%(ext)s"),
                url,
            ],
        )
        video = next(
            (p for p in Path(workdir).iterdir() if p.suffix in VIDEO_EXTS), None
        )
        if video is None:
            return None
        return FetchedMedia(kind="video", path=str(video))
