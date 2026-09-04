"""E1 / M2：元数据归一化 + fetch_metadata + 落库幂等 + 失败隔离 + thumbnail 代理。"""

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.config import Settings
from app.core.fetchers.ytdlp import (
    FetchMetadataError,
    _normalize_metadata,
    fetch_metadata,
)
from app.db import init_db
from app.jobs.pipeline import _save_metadata, process_video
from app.main import create_app
from app.models import Comment, Video
from app.core.fetchers.base import FetchedMedia
from app.core.transcribers.base import Segment, Transcript

BILI_INFO = {
    "id": "BV1xx",
    "title": "十分钟理解 RAG",
    "uploader": "UP主甲",
    "duration": 603,
    "description": "长简介" * 3000,  # > 8000 字符，验证截断
    "thumbnail": "https://i0.hdslb.com/bfs/archive/abc.jpg",
    "upload_date": "20260115",
    "view_count": 123456,
    "like_count": 789,
    "tags": ["AI", "RAG", "LLM", "教程", "字幕"],
    "comment_count": 42,
    "comments": [
        {"author": "乙", "text": "普通", "like_count": 5, "timestamp": 1700000000},
        {"author": "甲", "text": "讲得真好" + "长" * 3000, "like_count": 999},
        {"author": "", "text": "空作者但有内容", "like_count": 7},
        {"text": "", "like_count": 999},  # 空文本丢弃
    ],
}

SPARSE_INFO = {"id": "yt1", "title": "YT Video", "uploader": "Some Channel"}


def _proc(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr=stderr)


# ============ 归一化 ============


def test_normalize_bilibili_maps_fields_and_sorts_comments():
    meta = _normalize_metadata(BILI_INFO)
    assert meta["title"] == "十分钟理解 RAG"
    assert meta["author"] == "UP主甲"
    assert meta["duration"] == 603
    assert len(meta["description"]) == 8000  # 截断
    assert meta["thumbnail"].startswith("https://i0.hdslb.com")
    assert meta["upload_date"] == "20260115"
    assert meta["view_count"] == 123456
    assert meta["like_count"] == 789
    assert meta["tags"] == ["AI", "RAG", "LLM", "教程", "字幕"]
    assert meta["comment_count"] == 42
    # 评论：按点赞降序；空文本丢弃；文本截断 2000
    assert [c["like_count"] for c in meta["comments"]] == [999, 7, 5]
    assert len(meta["comments"][0]["text"]) == 2000
    assert meta["comments"][0]["author"] == "甲"
    # timestamp → ISO
    assert meta["comments"][2]["published_at"] == datetime.fromtimestamp(
        1700000000, tz=timezone.utc
    ).isoformat()
    assert meta["comments"][1]["published_at"] is None


def test_normalize_missing_fields_are_none():
    meta = _normalize_metadata(SPARSE_INFO)
    assert meta["author"] == "Some Channel"
    for key in (
        "duration", "description", "thumbnail", "upload_date",
        "view_count", "like_count", "tags", "comment_count",
    ):
        assert meta[key] is None, key
    assert meta["comments"] == []
    assert meta["title"] == "YT Video"


def test_normalize_clean_types():
    """字符串/异常数值不被信任（平台差异），一律置 None。"""
    meta = _normalize_metadata({"id": "x", "title": "T", "view_count": "1.2万"})
    assert meta["view_count"] is None
    meta = _normalize_metadata({"id": "x", "title": "T", "view_count": 10.9})
    assert meta["view_count"] == 10  # 数值转 int


# ============ fetch_metadata（fake runner）============


class _FakeRunner:
    def __init__(self, proc=None, raise_=None):
        self.proc = proc
        self.raise_ = raise_
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        if self.raise_:
            raise self.raise_
        return self.proc


async def test_fetch_metadata_parses_first_json_line(tmp_path):
    runner = _FakeRunner(_proc(stdout='{"id":"x","title":"正确标题","view_count":1}\n{"id":"y"}'))
    meta = await fetch_metadata("https://www.bilibili.com/video/BV1xx", runner=runner)
    assert meta["title"] == "正确标题"
    # 命令参数：dump-json / skip-download / no-playlist / write-comments / url
    # 注：yt-dlp 无全局 --max-comments 选项（曾误用导致 exit 2、元数据被隔离）
    args = runner.calls[0]
    for flag in ("--dump-json", "--skip-download", "--no-playlist", "--write-comments", "--extractor-args"):
        assert flag in args, flag
    assert "youtube:max_comments=100,40,0,0" in args
    assert args[-1] == "https://www.bilibili.com/video/BV1xx"


async def test_fetch_metadata_uses_cookie_for_douyin(tmp_path):
    cookie = tmp_path / "douyin.txt"
    cookie.write_text("# Netscape\n", encoding="utf-8")
    runner = _FakeRunner(_proc(stdout='{"title":"抖音视频"}'))
    await fetch_metadata("https://www.douyin.com/video/1", cookie_dir=str(tmp_path), runner=runner)
    assert any(str(a).startswith("--cookies=") for a in runner.calls[0])


async def test_fetch_metadata_nonzero_exit_raises():
    runner = _FakeRunner(_proc(stderr="some error", code=1))
    with pytest.raises(FetchMetadataError):
        await fetch_metadata("https://www.bilibili.com/video/x", runner=runner)


async def test_fetch_metadata_timeout_raises():
    runner = _FakeRunner(raise_=subprocess.TimeoutExpired("yt-dlp", 120))
    with pytest.raises(FetchMetadataError):
        await fetch_metadata("https://www.bilibili.com/video/x", runner=runner)


async def test_fetch_metadata_no_json_raises():
    runner = _FakeRunner(_proc(stdout="no json here"))
    with pytest.raises(FetchMetadataError):
        await fetch_metadata("https://www.bilibili.com/video/x", runner=runner)


# ============ 落库幂等 + 失败隔离（真实 DB + pipeline）============


SAMPLE_META = _normalize_metadata(BILI_INFO)


async def _db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # init_db 内部 ensure_dirs 会 os.chdir
    settings = Settings(_env_file=None, data_dir=str(tmp_path))
    app = create_app(settings=settings)
    await init_db(app.state.engine, app.state.settings)
    return app


async def _insert_video(app, url="https://www.bilibili.com/video/BV1xx") -> str:
    async with app.state.session_factory() as s:
        v = Video(platform="bilibili", url=url)
        s.add(v)
        await s.commit()
        return v.id


async def test_save_metadata_idempotent(tmp_path, monkeypatch):
    """重复调用不产生重复评论、元数据列更新。"""
    app = await _db(tmp_path, monkeypatch)
    vid = await _insert_video(app)

    await _save_metadata(app.state.session_factory, vid, SAMPLE_META)
    await _save_metadata(app.state.session_factory, vid, SAMPLE_META)  # 幂等重跑

    async with app.state.session_factory() as s:
        v = await s.get(Video, vid)
        assert v.title == "十分钟理解 RAG"
        assert v.author == "UP主甲"
        assert v.view_count == 123456
        assert v.meta_source == "ytdlp"
        n = (
            await s.execute(select(Comment).where(Comment.video_id == vid))
        ).scalars().all()
        assert len(n) == 3  # 两次调用后仍只有 3 条（无重复）
    await app.state.engine.dispose()


async def test_metadata_failure_isolated(tmp_path, monkeypatch):
    """fetch_metadata 抛错 → 视频继续走 fetch 链成功，meta_source=none 不 failed。"""
    import app.jobs.pipeline as pipeline_mod

    app = await _db(tmp_path, monkeypatch)
    vid = await _insert_video(app)

    async def boom(*a, **kw):
        raise FetchMetadataError("bilibili risk control")

    monkeypatch.setattr(pipeline_mod, "fetch_metadata", boom)

    media = FetchedMedia(kind="audio", path="a.mp3")
    transcript = Transcript(
        segments=[Segment(start_sec=0, end_sec=1, text="你好")],
        raw_text="你好",
        source="cloud",
    )

    class _LLM:
        async def chat_json(self, messages, **kw):
            return {
                "summary": "摘要",
                "chapters": [], "key_points": ["k"], "quotes": [], "glossary": [],
            }

    class _Emb:
        async def embed_texts(self, texts, query=False):
            return [[0.1, 0.2] for _ in texts]

    class _Store:
        def add(self, rows):
            pass

        def ensure_fts_index(self):
            pass

        def has_kind_rows(self, video_id, kind="meta"):
            return False

    async def _fetch(self, url, workdir):
        return media

    async def _transcribe(self, media_):
        return transcript

    await process_video(
        vid, app.state.session_factory,
        [type("F", (), {"name": "ytdlp", "fetch": _fetch})()],
        [type("T", (), {"name": "cloud", "transcribe": _transcribe})()],
        _LLM(), str(tmp_path), embedder=_Emb(), vector_store=_Store(),
        cookie_dir=str(tmp_path),
    )

    async with app.state.session_factory() as s:
        v = await s.get(Video, vid)
        assert v.status == "done"
        assert v.meta_source == "none"
        assert v.title is None  # 元数据失败不回填标题（URL 兜底在笔记层）
    await app.state.engine.dispose()


async def test_metadata_success_feeds_note_title(tmp_path, monkeypatch):
    """元数据成功 → 视频标题落库，笔记用真实标题（非 URL）。"""
    import app.jobs.pipeline as pipeline_mod

    app = await _db(tmp_path, monkeypatch)
    vid = await _insert_video(app)

    async def fake_fetch_metadata(url, cookie_dir=None, runner=None, timeout=120.0):
        return SAMPLE_META

    monkeypatch.setattr(pipeline_mod, "fetch_metadata", fake_fetch_metadata)
    media = FetchedMedia(kind="audio", path="a.mp3")
    transcript = Transcript(
        segments=[Segment(start_sec=0, end_sec=1, text="你好")],
        raw_text="你好", source="cloud",
    )

    class _LLM:
        async def chat_json(self, messages, **kw):
            # 真实标题（替代 URL 兜底）已进入 user 段
            assert "十分钟理解 RAG" in messages[1]["content"]
            return {
                "summary": "摘要",
                "chapters": [], "key_points": ["k"], "quotes": [], "glossary": [],
            }

    class _Emb:
        async def embed_texts(self, texts, query=False):
            return [[0.1, 0.2] for _ in texts]

    class _Store:
        def add(self, rows):
            pass

        def ensure_fts_index(self):
            pass

        def has_kind_rows(self, video_id, kind="meta"):
            return False

    async def _fetch(self, url, workdir):
        return media

    async def _transcribe(self, media_):
        return transcript

    await process_video(
        vid, app.state.session_factory,
        [type("F", (), {"name": "ytdlp", "fetch": _fetch})()],
        [type("T", (), {"name": "cloud", "transcribe": _transcribe})()],
        _LLM(), str(tmp_path), embedder=_Emb(), vector_store=_Store(),
    )

    async with app.state.session_factory() as s:
        v = await s.get(Video, vid)
        assert v.title == "十分钟理解 RAG"
        assert v.status == "done"
        assert v.meta_source == "ytdlp"
    await app.state.engine.dispose()


# ============ thumbnail 代理端点 ============


class _FakeResp:
    def __init__(self, content=b"fake-jpg", status=200):
        self.content = content
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"http {self.status}")


class _FakeHTTPX:
    """记录 get 调用；可配置抛出（模拟下载失败）。"""

    def __init__(self, resp=None, fail=False):
        self.resp = resp
        self.fail = fail
        self.gets = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        self.gets += 1
        if self.fail:
            raise RuntimeError("download failed")
        return self.resp


async def _video_app(tmp_path, monkeypatch, cover_url=None, platform="bilibili"):
    app = await _db(tmp_path, monkeypatch)
    async with app.state.session_factory() as s:
        v = Video(platform=platform, url="https://x", cover_url=cover_url)
        s.add(v)
        await s.commit()
        vid = v.id
    return app, vid


async def _get_thumb(app, vid):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.get(f"/api/videos/{vid}/thumbnail")


async def test_thumbnail_no_cover_404(tmp_path, monkeypatch):
    app, vid = await _video_app(tmp_path, monkeypatch)
    resp = await _get_thumb(app, vid)
    assert resp.status_code == 404
    await app.state.engine.dispose()


async def test_thumbnail_downloads_then_caches(tmp_path, monkeypatch):
    app, vid = await _video_app(tmp_path, monkeypatch, cover_url="https://i0.hdslb.com/x.jpg")
    fake = _FakeHTTPX(resp=_FakeResp(b"real-image-bytes"))
    monkeypatch.setattr("app.api.videos.httpx.AsyncClient", lambda **kw: fake)

    resp = await _get_thumb(app, vid)
    assert resp.status_code == 200
    assert resp.content == b"real-image-bytes"
    assert fake.gets == 1
    # 磁盘已落盘
    assert (Path(tmp_path) / "thumbnails" / f"{vid}.jpg").read_bytes() == b"real-image-bytes"

    # 二次请求命中缓存，不再请求外网
    resp2 = await _get_thumb(app, vid)
    assert resp2.status_code == 200
    assert fake.gets == 1
    await app.state.engine.dispose()


async def test_thumbnail_failure_cached_miss(tmp_path, monkeypatch):
    app, vid = await _video_app(tmp_path, monkeypatch, cover_url="https://i0.hdslb.com/x.jpg")
    fake = _FakeHTTPX(fail=True)
    monkeypatch.setattr("app.api.videos.httpx.AsyncClient", lambda **kw: fake)

    assert (await _get_thumb(app, vid)).status_code == 404
    miss = Path(tmp_path) / "thumbnails" / f"{vid}.miss"
    assert miss.is_file()  # miss 标记已写

    # 24h 内不再请求外网
    assert (await _get_thumb(app, vid)).status_code == 404
    assert fake.gets == 1
    await app.state.engine.dispose()
