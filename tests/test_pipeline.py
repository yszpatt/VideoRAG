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
