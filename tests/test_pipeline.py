from dataclasses import dataclass

import pytest

from app.core.fetchers.base import FetchedMedia
from app.core.fetchers.ytdlp import FetchError
from app.core.transcribers.base import Segment, Transcript
from app.jobs.pipeline import fetch_with_fallback, process_video, transcribe_with_fallback

SAMPLE_NOTE_JSON = {
    "summary": "视频摘要",
    "chapters": [{"title": "开头", "start_sec": 0.0, "end_sec": 10.0, "points": ["背景"]}],
    "key_points": ["要点A"],
    "quotes": [{"text": "你好", "start_sec": 1.0}],
    "glossary": [{"term": "RAG", "explanation": "检索增强生成"}],
}


class FakeLLM:
    async def chat_json(self, messages, **kw):
        return SAMPLE_NOTE_JSON


class FakeEmbedder:
    async def embed_texts(self, texts, query=False):
        return [[float(i), 1.0] for i in range(len(texts))]


class FakeStore:
    def __init__(self):
        self.rows = []

    def add(self, rows):
        self.rows.extend(rows)

    def ensure_fts_index(self):
        pass

    def has_kind_rows(self, video_id, kind="meta"):
        return False

    def search(self, vec, top_k=5, where=None):
        return []


@dataclass
class FakeFetcher:
    name: str
    result: FetchedMedia | None = None
    raise_: Exception | None = None

    async def fetch(self, url, workdir):
        if self.raise_:
            raise self.raise_
        return self.result


@dataclass
class FakeTranscriber:
    name: str
    result: Transcript | None = None
    raise_: Exception | None = None

    async def transcribe(self, media):
        if self.raise_:
            raise self.raise_
        return self.result


async def test_fetch_falls_back_to_next_provider(tmp_path):
    chain = [
        FakeFetcher(name="sub", result=None),
        FakeFetcher(name="audio", result=FetchedMedia(kind="audio", path="a.mp3")),
    ]
    media, used = await fetch_with_fallback("https://x", str(tmp_path), chain)
    assert used == "audio"
    assert media.kind == "audio"


async def test_fetch_raises_when_all_fail(tmp_path):
    chain = [FakeFetcher(name="sub"), FakeFetcher(name="audio")]
    with pytest.raises(FetchError):
        await fetch_with_fallback("https://x", str(tmp_path), chain)


async def test_transcribe_starts_at_matching_provider():
    media = FetchedMedia(kind="subtitle", subtitle_text="x", meta={"segments": [(0, 1, "x")]})
    chain = [
        FakeTranscriber(
            name="subtitle",
            result=Transcript(segments=[], raw_text="", source="subtitle"),
        ),
        FakeTranscriber(
            name="whisper",
            result=Transcript(segments=[Segment(0, 1, "音频转写")], raw_text="音频转写", source="whisper"),
        ),
    ]
    transcript, used = await transcribe_with_fallback(media, chain)
    assert used == "whisper"  # 字幕为空时应降级 whisper
    assert transcript.segments[0].text == "音频转写"


async def test_process_video_end_to_end(tmp_path):
    from pathlib import Path

    from sqlalchemy import select

    from app.config import Settings
    from app.db import init_db, make_session_factory
    from app.models import Chunk, Note, Segment as ORMSegment, Video

    settings = Settings(_env_file=None, data_dir=str(tmp_path))
    engine, factory = make_session_factory(settings)
    await init_db(engine, settings)

    async with factory() as s:
        s.add(Video(id="vid1", platform="youtube", url="https://youtu.be/x", title="T"))
        await s.commit()

    fetchers = [
        FakeFetcher(
            name="sub",
            result=FetchedMedia(
                kind="subtitle", subtitle_text="hi", meta={"segments": [(0, 1, "hi")]}
            ),
        )
    ]
    transcribers = [
        FakeTranscriber(
            name="subtitle",
            result=Transcript(segments=[Segment(0, 1, "hi")], raw_text="hi", source="subtitle"),
        )
    ]
    store = FakeStore()

    await process_video(
        "vid1", factory, fetchers, transcribers, FakeLLM(), str(tmp_path),
        embedder=FakeEmbedder(), vector_store=store,
    )

    async with factory() as s:
        v = await s.get(Video, "vid1")
        assert v.status == "done"
        rows = (
            await s.execute(select(ORMSegment).where(ORMSegment.video_id == "vid1"))
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].text == "hi"
        note = (await s.execute(select(Note).where(Note.video_id == "vid1"))).scalar_one()
        assert note.summary == "视频摘要"
        assert note.markdown and "# T" in note.markdown
        assert v.note_path and Path(v.note_path).exists()
        chunks = (await s.execute(select(Chunk).where(Chunk.video_id == "vid1"))).scalars().all()
        assert len(chunks) == 1
        assert chunks[0].lancedb_id == chunks[0].id

    assert len(store.rows) == 1
    assert store.rows[0]["video_id"] == "vid1"
    assert store.rows[0]["content"] == "hi"
    assert store.rows[0]["title"] == "T"

    await engine.dispose()


async def test_process_video_marks_failed_on_fetch_error(tmp_path):
    from app.config import Settings
    from app.db import init_db, make_session_factory
    from app.models import Video

    settings = Settings(_env_file=None, data_dir=str(tmp_path))
    engine, factory = make_session_factory(settings)
    await init_db(engine, settings)
    async with factory() as s:
        s.add(Video(id="vid2", platform="youtube", url="https://youtu.be/x"))
        await s.commit()

    await process_video("vid2", factory, [FakeFetcher(name="sub")], [], FakeLLM(), str(tmp_path))
    async with factory() as s:
        v = await s.get(Video, "vid2")
        assert v.status == "failed"
        assert "all fetch providers failed" in v.error
    await engine.dispose()


async def test_process_video_marks_failed_on_note_error(tmp_path):
    from app.config import Settings
    from app.db import init_db, make_session_factory
    from app.models import Video

    settings = Settings(_env_file=None, data_dir=str(tmp_path))
    engine, factory = make_session_factory(settings)
    await init_db(engine, settings)
    async with factory() as s:
        s.add(Video(id="vid3", platform="youtube", url="https://youtu.be/x"))
        await s.commit()

    class BadLLM:
        async def chat_json(self, messages, **kw):
            raise RuntimeError("llm down")

    fetchers = [
        FakeFetcher(
            name="sub",
            result=FetchedMedia(kind="subtitle", subtitle_text="hi", meta={"segments": [(0, 1, "hi")]}),
        )
    ]
    transcribers = [
        FakeTranscriber(
            name="subtitle",
            result=Transcript(segments=[Segment(0, 1, "hi")], raw_text="hi", source="subtitle"),
        )
    ]
    await process_video("vid3", factory, fetchers, transcribers, BadLLM(), str(tmp_path))
    async with factory() as s:
        v = await s.get(Video, "vid3")
        assert v.status == "failed"
        assert "llm down" in v.error
    await engine.dispose()


async def test_embed_chunks_batches_to_bound_memory(tmp_path):
    """长视频切片必须分批向量化/入库（控内存峰值）。

    行为契约：
    - embedder.embed_texts 被多次调用，且每次入参不超过 EMBED_BATCH_SIZE；
    - vector_store.add 被多次调用（每批一次）；
    - ensure_fts_index 只在全部批次写完后调用一次（不随批次重复建索引）。
    """
    from pathlib import Path

    from sqlalchemy import select

    from app.config import Settings
    from app.db import init_db, make_session_factory
    from app.jobs.pipeline import EMBED_BATCH_SIZE
    from app.models import Chunk, Video

    settings = Settings(_env_file=None, data_dir=str(tmp_path))
    engine, factory = make_session_factory(settings)
    await init_db(engine, settings)
    async with factory() as s:
        s.add(Video(id="vidbig", platform="youtube", url="https://youtu.be/big", title="Big"))
        await s.commit()

    # 每个 segment 500 字符 = chunk_segments 的 max_chars，约每段独立成一块，
    # 共 260 块 > 64（一批），确保跨批。
    n_segments = 260
    segments = [
        Segment(start_sec=float(i), end_sec=float(i + 1), text="哈" * 500)
        for i in range(n_segments)
    ]

    class CountingEmbedder:
        def __init__(self):
            self.batch_sizes = []

        async def embed_texts(self, texts, query=False):
            self.batch_sizes.append(len(texts))
            return [[float(i), 1.0] for i in range(len(texts))]

    class CountingStore:
        def __init__(self):
            self.add_calls = 0
            self.rows = []
            self.fts_calls = 0

        def add(self, rows):
            self.add_calls += 1
            self.rows.extend(rows)

        def ensure_fts_index(self):
            self.fts_calls += 1

        def has_kind_rows(self, video_id, kind="meta"):
            return False

    fetchers = [
        FakeFetcher(
            name="sub",
            result=FetchedMedia(kind="subtitle", subtitle_text="x", meta={"segments": []}),
        )
    ]
    transcribers = [
        FakeTranscriber(
            name="subtitle",
            result=Transcript(segments=segments, raw_text="x", source="subtitle"),
        )
    ]
    emb, store = CountingEmbedder(), CountingStore()
    await process_video(
        "vidbig", factory, fetchers, transcribers, FakeLLM(), str(tmp_path),
        embedder=emb, vector_store=store,
    )

    # 分批：260 块 → 至少 2 批（64/批），每批不超限
    assert len(emb.batch_sizes) >= 2
    assert max(emb.batch_sizes) <= EMBED_BATCH_SIZE
    assert sum(emb.batch_sizes) == len(store.rows)
    # 每批一次 add；FTS 索引只建一次
    assert store.add_calls == len(emb.batch_sizes)
    assert store.fts_calls == 1

    async with factory() as s:
        chunks = (await s.execute(select(Chunk).where(Chunk.video_id == "vidbig"))).scalars().all()
        assert len(chunks) == len(store.rows)
        assert len(chunks) > EMBED_BATCH_SIZE

    await engine.dispose()


def test_sweep_temp_media_clears_leftovers_only(tmp_path):
    """启动清扫：删 downloads/audio/transcripts 下遗留文件，不动正式数据目录。"""
    from app.jobs.pipeline import sweep_temp_media

    # 三个临时目录各放遗留文件（含子目录）
    for sub in ("downloads", "audio", "transcripts"):
        d = tmp_path / sub
        d.mkdir()
        (d / "leftover.mp3").write_bytes(b"x")
    (tmp_path / "downloads" / "sub").mkdir()
    (tmp_path / "downloads" / "sub" / "part.webm").write_bytes(b"x")
    # 正式数据不应被清
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.md").write_text("keep")
    (tmp_path / "thumbnails").mkdir()
    (tmp_path / "thumbnails" / "a.jpg").write_bytes(b"x")

    removed = sweep_temp_media(str(tmp_path))

    assert removed == 4  # 3 + 1 子目录文件
    for sub in ("downloads", "audio", "transcripts"):
        leftovers = [p for p in (tmp_path / sub).rglob("*") if p.is_file()]
        assert leftovers == []
    assert (tmp_path / "notes" / "a.md").read_text() == "keep"
    assert (tmp_path / "thumbnails" / "a.jpg").is_file()
    # 目录本身保留
    assert (tmp_path / "downloads").is_dir()

    # 幂等：再跑一次无文件可删
    assert sweep_temp_media(str(tmp_path)) == 0


# ============ 已下载媒体归档（新增，默认关闭） ============


async def _prepare_archive_case(tmp_path, video_id, title="T"):
    from app.config import Settings
    from app.db import init_db, make_session_factory
    from app.models import Video

    settings = Settings(_env_file=None, data_dir=str(tmp_path))
    engine, factory = make_session_factory(settings)
    await init_db(engine, settings)
    async with factory() as s:
        s.add(Video(id=video_id, platform="youtube", url="https://youtu.be/x", title=title))
        await s.commit()
    return engine, factory


def _audio_fixtures(path):
    fetchers = [FakeFetcher(name="audio", result=FetchedMedia(kind="audio", path=str(path)))]
    transcribers = [
        FakeTranscriber(
            name="whisper",
            result=Transcript(
                segments=[Segment(0, 1, "hi")], raw_text="hi", source="whisper"
            ),
        )
    ]
    return fetchers, transcribers


async def test_process_video_archives_downloaded_audio(tmp_path, monkeypatch):
    """配置归档目录：音频留存副本（按元数据标题命名），临时文件仍被清理。"""
    from app.jobs import pipeline as pl
    from app.models import Video

    engine, factory = await _prepare_archive_case(tmp_path, "vid-arch")
    # 避免真实网络调用，同时验证归档文件名使用元数据标题（而非库内旧标题）
    async def fake_meta(url, cookie_dir=None, **kw):
        return {"title": "元数据标题"}

    monkeypatch.setattr(pl, "fetch_metadata", fake_meta)

    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio-bytes")
    archive_dir = tmp_path / "archive"
    fetchers, transcribers = _audio_fixtures(audio)

    await process_video(
        "vid-arch", factory, fetchers, transcribers, FakeLLM(), str(tmp_path),
        media_save_dir=str(archive_dir),
    )

    archived = list(archive_dir.glob("*"))
    assert len(archived) == 1
    assert archived[0].name == "元数据标题-vid-arch.mp3"
    assert archived[0].read_bytes() == b"audio-bytes"
    assert not audio.exists()  # 归档是复制：临时文件仍按原逻辑清理

    async with factory() as s:
        assert (await s.get(Video, "vid-arch")).status == "done"
    await engine.dispose()


async def test_process_video_archives_on_failure_path(tmp_path, monkeypatch):
    """转写失败路径同样归档（已完整下载的文件不因后续失败而丢掉）。"""
    from app.jobs import pipeline as pl
    from app.models import Video

    engine, factory = await _prepare_archive_case(tmp_path, "vid-fail")

    async def fake_meta(url, cookie_dir=None, **kw):
        return {"title": "失败标题"}

    monkeypatch.setattr(pl, "fetch_metadata", fake_meta)

    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"partial")
    archive_dir = tmp_path / "archive"
    fetchers = [FakeFetcher(name="audio", result=FetchedMedia(kind="audio", path=str(audio)))]
    transcribers = [FakeTranscriber(name="whisper", raise_=RuntimeError("asr down"))]

    await process_video(
        "vid-fail", factory, fetchers, transcribers, FakeLLM(), str(tmp_path),
        media_save_dir=str(archive_dir),
    )

    async with factory() as s:
        assert (await s.get(Video, "vid-fail")).status == "failed"
    assert (archive_dir / "失败标题-vid-fail.mp3").read_bytes() == b"partial"
    assert not audio.exists()
    await engine.dispose()


async def test_process_video_no_archive_when_dir_unset(tmp_path, monkeypatch):
    """默认（未配置归档目录）：不产生任何归档目录，行为与旧版一致。"""
    from app.jobs import pipeline as pl
    from app.models import Video

    engine, factory = await _prepare_archive_case(tmp_path, "vid-noarch")

    async def fake_meta(url, cookie_dir=None, **kw):
        return {"title": "T"}

    monkeypatch.setattr(pl, "fetch_metadata", fake_meta)

    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"x")
    fetchers, transcribers = _audio_fixtures(audio)

    await process_video("vid-noarch", factory, fetchers, transcribers, FakeLLM(), str(tmp_path))

    assert not audio.exists()  # 临时文件照旧清理
    assert not (tmp_path / "archive").exists()  # 未配置 → 不创建任何归档目录
    async with factory() as s:
        assert (await s.get(Video, "vid-noarch")).status == "done"
    await engine.dispose()
