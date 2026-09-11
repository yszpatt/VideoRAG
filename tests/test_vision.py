"""E3 视觉旁路测试：无语音判定 / 抽帧去重 / OCR 帧间去重 / 转写合并 / 流水线接线。

覆盖设计文档「3.4 验证方法」的用例，并额外覆盖：
- 按需下载视频（fetch_video）成功与失败；
- 视觉层失败隔离（视频仍 done）；
- VISUAL_PIPELINE=off 不触发；
- 来源 source 贯通到 ORM Segment / Chunk.meta / 转写 API。
"""

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.fetchers.base import FetchedMedia
from app.core.fetchers.ytdlp import fetch_video
from app.core.transcribers.base import Segment, Transcript
from app.core.vision import (
    is_speechless,
    merge_transcripts,
    run_visual_pipeline,
    speech_density,
)
from app.core.vision.keyframes import KeyFrame, extract_keyframes
from app.core.vision.ocr import is_duplicate_text, ocr_frame, similarity
from app.core.vision.phash import dhash, hamming, is_similar

# ============ 无语音判定 ============


def test_speechless_empty_transcript_with_duration():
    """空转写 + 有时长 → 无语音（纯画面视频的典型形态）。"""
    assert is_speechless([], 300) is True


def test_speechless_without_duration_is_false():
    """时长不可用 → 保守返回 False（避免误触发昂贵的视觉处理）。"""
    assert is_speechless([]) is False
    assert is_speechless([Segment(0, 0, "")]) is False


def test_speechless_low_density_true_and_normal_false():
    # 5 字 / 60s = 5 字/分钟 < 10 → 无语音
    assert is_speechless([Segment(0, 60, "五个字的文本")], 60) is True
    # 200 字 / 10s = 1200 字/分钟 → 正常口播
    assert is_speechless([Segment(0, 10, "字" * 200)], 10) is False


def test_speechless_threshold_boundary():
    """密度正好等于阈值不算无语音（严格小于才触发）。"""
    assert speech_density([Segment(0, 60, "字" * 10)], 60) == pytest.approx(10.0)
    assert is_speechless([Segment(0, 60, "字" * 10)], 60, min_wpm=10) is False
    assert is_speechless([Segment(0, 60, "字" * 10)], 60, min_wpm=11) is True


def test_speechless_uses_segment_end_as_duration_fallback():
    """未传时长时用转写末段 end_sec 兜底。"""
    segs = [Segment(0, 120, "少"), Segment(120, 240, "字")]
    assert is_speechless(segs, None, min_wpm=10) is True  # 2 字 / 240s


# ============ 图像指纹（dHash）============


def _gradient(path: Path, reverse: bool = False) -> str:
    """生成水平渐变图（正/反向），用于得到不同的 dHash。"""
    from PIL import Image

    img = Image.new("L", (9, 8))
    px = img.load()
    for y in range(8):
        for x in range(9):
            px[x, y] = x * 28 if reverse else 255 - x * 28
    img.convert("RGB").save(path, "JPEG")
    return str(path)


def test_dhash_identical_and_distinct(tmp_path):
    a = _gradient(tmp_path / "a.jpg")
    a2 = _gradient(tmp_path / "a2.jpg")
    b = _gradient(tmp_path / "b.jpg", reverse=True)
    ha, ha2, hb = dhash(a), dhash(a2), dhash(b)
    assert ha is not None and hb is not None
    assert hamming(ha, ha2) == 0
    assert hamming(ha, hb) > 8
    assert is_similar(ha, ha2) is True
    assert is_similar(ha, hb) is False


def test_dhash_missing_file_returns_none(tmp_path):
    assert dhash(str(tmp_path / "nope.jpg")) is None
    assert is_similar(None, 123) is False  # 无法判定 → 不视为重复


# ============ 抽帧 ============


class _FakeFFmpeg:
    """伪 ffmpeg：按输出模式写出图片并伪造 showinfo 日志（pts_time）。"""

    def __init__(self, scene_pts: list[float], fixed_count: int, reverse: bool = False):
        self.scene_pts = scene_pts
        self.fixed_count = fixed_count
        self.reverse = reverse

    def __call__(self, args):
        pattern = args[-1]
        # 用文件名前缀判断（tmp_path 目录名可能含 "scene_" 造成子串误判）
        is_scene = Path(pattern).name.startswith("scene_")
        n = len(self.scene_pts) if is_scene else self.fixed_count
        for i in range(1, n + 1):
            # 交替正反向渐变 → 场景帧彼此不同；兜底帧同类
            rev = self.reverse if not is_scene else (i % 2 == 0)
            _gradient(Path(pattern.replace("%05d", f"{i:05d}")), reverse=rev)
        stderr = "\n".join(
            f"[Parsed_showinfo_1] n:{i} pts_time:{t}"
            for i, t in enumerate(self.scene_pts)
        )
        return SimpleNamespace(stderr=stderr, returncode=0)


def test_extract_keyframes_scene_and_fixed_with_timestamps(tmp_path):
    # 只产出场景帧（fixed_count=0），两张场景帧内容不同 → 都保留
    runner = _FakeFFmpeg(scene_pts=[1.5, 4.0], fixed_count=0)
    frames = extract_keyframes("v.mp4", str(tmp_path), max_frames=10, runner=runner)
    ts = [f.ts for f in frames]
    assert ts == [1.5, 4.0]  # 场景帧时间戳来自 showinfo，且按时间有序


def test_extract_keyframes_dedups_similar_frames(tmp_path):
    """两路抽帧都产出同向渐变 → 近似重复，只保留 1 帧。"""

    class _SameRunner:
        def __call__(self, args):
            pattern = args[-1]
            for i in range(1, 3):
                _gradient(Path(pattern.replace("%05d", f"{i:05d}")), reverse=False)
            stderr = "[Parsed_showinfo_1] pts_time:1.0\n[Parsed_showinfo_1] pts_time:2.0"
            return SimpleNamespace(stderr=stderr, returncode=0)

    frames = extract_keyframes("v.mp4", str(tmp_path), max_frames=10, runner=_SameRunner())
    assert len(frames) == 1


def test_extract_keyframes_uniform_sample_caps_count(tmp_path):
    runner = _FakeFFmpeg(scene_pts=[1.0, 2.0, 3.0], fixed_count=3)
    frames = extract_keyframes("v.mp4", str(tmp_path), max_frames=2, runner=runner)
    assert len(frames) == 2


def test_extract_keyframes_ffmpeg_failure_returns_empty(tmp_path):
    def boom(args):
        raise RuntimeError("ffmpeg missing")

    assert extract_keyframes("v.mp4", str(tmp_path), runner=boom) == []


# ============ OCR ============


def test_ocr_frame_joins_recognized_text(monkeypatch):
    class FakeEngine:
        def __call__(self, path):
            return (
                [[[[0, 0], [1, 1]], "第一行", 0.9], [[[0, 0], [1, 1]], "第二行", 0.8]],
                0.01,
            )

    monkeypatch.setattr("app.core.vision.ocr._get_engine", lambda: FakeEngine())
    assert ocr_frame("x.jpg") == "第一行 第二行"


def test_ocr_frame_returns_empty_on_error(monkeypatch):
    def boom():
        raise RuntimeError("engine unavailable")

    monkeypatch.setattr("app.core.vision.ocr._get_engine", boom)
    assert ocr_frame("x.jpg") == ""


def test_ocr_text_dedup():
    assert similarity("同一页PPT", "同一页PPT") == 1.0
    assert is_duplicate_text("同一页PPT内容", "同一页PPT内容") is True
    assert is_duplicate_text("第一页标题", "完全不同的画面") is False


# ============ 编排（run_visual_pipeline）============


async def test_run_visual_pipeline_produces_ocr_segments(monkeypatch, tmp_path):
    frames = [
        KeyFrame(path="f1.jpg", ts=0.0),
        KeyFrame(path="f2.jpg", ts=30.0),
        KeyFrame(path="f3.jpg", ts=60.0),
    ]
    monkeypatch.setattr("app.core.vision.extract_keyframes", lambda *a, **k: frames)
    texts = iter(["第一页标题", "第一页标题", "第二页标题"])
    monkeypatch.setattr("app.core.vision.ocr_frame", lambda p: next(texts))

    segs = await run_visual_pipeline("v.mp4", str(tmp_path))
    # 第 2 帧与第 1 帧文本相同 → 去重；最终 2 段（第一页 / 第二页）
    assert [s.text for s in segs] == ["第一页标题", "第二页标题"]
    assert all(s.source == "ocr" for s in segs)
    assert segs[0].end_sec == 60.0  # 零长度段补到下一保留段起点


async def test_run_visual_pipeline_vlm_none_skips(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.core.vision.extract_keyframes",
        lambda *a, **k: [KeyFrame(path="f.jpg", ts=1.0)],
    )
    monkeypatch.setattr("app.core.vision.ocr_frame", lambda p: "画面文字")
    segs = await run_visual_pipeline("v.mp4", str(tmp_path), vlm=None)
    assert [s.source for s in segs] == ["ocr"]


async def test_run_visual_pipeline_with_vlm_and_failure_isolation(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.core.vision.extract_keyframes",
        lambda *a, **k: [KeyFrame(path="f.jpg", ts=1.0)],
    )
    monkeypatch.setattr("app.core.vision.ocr_frame", lambda p: "画面文字")

    class GoodVLM:
        async def describe(self, path):
            return "画面描述"

    segs = await run_visual_pipeline("v.mp4", str(tmp_path), vlm=GoodVLM())
    assert {s.source for s in segs} == {"ocr", "vlm"}

    class BadVLM:
        async def describe(self, path):
            raise RuntimeError("vlm down")

    segs2 = await run_visual_pipeline("v.mp4", str(tmp_path), vlm=BadVLM())
    assert [s.source for s in segs2] == ["ocr"]  # 单帧失败跳过，OCR 层不受影响


async def test_run_visual_pipeline_empty_video_path():
    assert await run_visual_pipeline("", "/tmp/x") == []


# ============ 合并转写 ============


def test_merge_transcripts_orders_and_fixes_ranges():
    tr = Transcript(segments=[Segment(10, 12, "语音")], raw_text="语音", source="cloud_asr")
    vis = [
        Segment(0, 0, "画面一", source="ocr"),
        Segment(20, 0, "画面二", source="ocr"),
    ]
    merged = merge_transcripts(tr, vis, duration_sec=30.0)
    assert [s.text for s in merged.segments] == ["画面一", "语音", "画面二"]
    assert merged.segments[0].end_sec == 10  # 补到下一段起点
    assert merged.segments[1].end_sec == 12  # 语音段区间不被改写
    assert merged.segments[2].end_sec == 30  # 末段用视频时长兜底
    assert merged.source == "cloud_asr"


def test_merge_transcripts_no_visual_returns_same_object():
    tr = Transcript(segments=[Segment(0, 1, "a")], raw_text="a", source="subtitle")
    assert merge_transcripts(tr, []) is tr


# ============ 来源贯通（chunker / ORM / API）============


def test_chunker_propagates_source():
    from app.core.embed.chunker import chunk_segments

    pure_ocr = chunk_segments([Segment(0, 1, "画面文字", source="ocr")])
    assert pure_ocr[0].src == "ocr"

    # 混合切片（语音 + 画面）以语音为主
    mixed = chunk_segments(
        [Segment(0, 1, "口播", source="speech"), Segment(1, 2, "画面", source="ocr")]
    )
    assert mixed[0].src == "speech"


async def test_orm_segment_default_source_is_speech(tmp_path):
    from app.config import Settings
    from app.db import init_db, make_session_factory
    from app.models import Segment as ORMSegment

    settings = Settings(_env_file=None, data_dir=str(tmp_path))
    engine, factory = make_session_factory(settings)
    await init_db(engine, settings)
    async with factory() as s:
        s.add(ORMSegment(video_id="v", start_sec=0, end_sec=1, text="t"))
        await s.commit()
        row = (await s.execute(select(ORMSegment))).scalar_one()
        assert row.source == "speech"
    await engine.dispose()


async def test_transcript_endpoint_exposes_source(app):
    from httpx import ASGITransport, AsyncClient

    from app.db import init_db
    from app.models import Segment as ORMSegment
    from app.models import Video

    # client fixture 不跑 lifespan，这里显式初始化 DB（建目录 + 建表）
    await init_db(app.state.engine, app.state.settings)
    sf = app.state.session_factory
    async with sf() as s:
        s.add(Video(id="vv", platform="bilibili", url="u", status="done"))
        s.add(ORMSegment(video_id="vv", start_sec=0, end_sec=1, text="画面", source="ocr"))
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/videos/vv/transcript")
    assert r.status_code == 200
    seg = r.json()["segments"][0]
    assert seg["source"] == "ocr"
    assert seg["text"] == "画面"


# ============ fetch_video（按需下载）============


async def test_fetch_video_returns_downloaded_path(tmp_path):
    def runner(args):
        (tmp_path / "video.mp4").write_bytes(b"x")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    path = await fetch_video("https://example.com/v", str(tmp_path), runner=runner)
    assert path is not None and path.endswith("video.mp4")


async def test_fetch_video_returns_none_on_error(tmp_path):
    def boom(args):
        raise RuntimeError("download failed")

    assert await fetch_video("https://example.com/v", str(tmp_path), runner=boom) is None


async def test_fetch_video_returns_none_when_no_file(tmp_path):
    def runner(args):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    assert await fetch_video("https://example.com/v", str(tmp_path), runner=runner) is None


# ============ 流水线接线 ============

SAMPLE_NOTE_JSON = {
    "summary": "摘要",
    "chapters": [],
    "key_points": ["要点"],
    "glossary": [],
}


class _FakeLLM:
    async def chat_json(self, messages, **kw):
        return SAMPLE_NOTE_JSON


class _FakeEmbedder:
    async def embed_texts(self, texts, query=False):
        return [[float(i), 1.0] for i in range(len(texts))]


class _FakeStore:
    def __init__(self):
        self.rows = []

    def add(self, rows):
        self.rows.extend(rows)

    def ensure_fts_index(self):
        pass

    def has_kind_rows(self, video_id, kind="meta"):
        return False


@dataclass
class _FakeFetcher:
    name: str
    result: FetchedMedia | None = None

    async def fetch(self, url, workdir):
        return self.result


@dataclass
class _FakeTranscriber:
    name: str
    result: Transcript | None = None

    async def transcribe(self, media):
        return self.result


async def _prepare_video(tmp_path, video_id="v1"):
    from app.config import Settings
    from app.db import init_db, make_session_factory
    from app.models import Video

    settings = Settings(_env_file=None, data_dir=str(tmp_path))
    engine, factory = make_session_factory(settings)
    await init_db(engine, settings)
    async with factory() as s:
        s.add(Video(id=video_id, platform="bilibili", url="https://x/v", title="T"))
        await s.commit()
    return engine, factory


def _speech_fixtures():
    fetchers = [
        _FakeFetcher(
            name="sub",
            result=FetchedMedia(kind="subtitle", subtitle_text="hi", meta={"segments": [(0, 1, "hi")]}),
        )
    ]
    transcribers = [
        _FakeTranscriber(
            name="subtitle",
            result=Transcript(segments=[Segment(0, 1, "hi", source="subtitle")], raw_text="hi", source="subtitle"),
        )
    ]
    return fetchers, transcribers


async def test_process_video_visual_always_persists_source(tmp_path, monkeypatch):
    from app.jobs import pipeline as pl
    from app.jobs.pipeline import process_video
    from app.models import Chunk, Segment as ORMSegment, Video

    engine, factory = await _prepare_video(tmp_path)
    fetchers, transcribers = _speech_fixtures()

    async def fake_visual(video_path, workdir, **kw):
        return [Segment(2.0, 2.0, "PPT 文字", source="ocr")]

    async def fake_fetch_video(url, workdir, cookie_dir=None, **kw):
        return "/tmp/fake.mp4"

    monkeypatch.setattr(pl, "run_visual_pipeline", fake_visual)
    monkeypatch.setattr(pl, "fetch_video", fake_fetch_video)

    await process_video(
        "v1", factory, fetchers, transcribers, _FakeLLM(), str(tmp_path),
        embedder=_FakeEmbedder(), vector_store=_FakeStore(),
        visual_pipeline="always",
    )

    async with factory() as s:
        v = await s.get(Video, "v1")
        assert v.status == "done"
        segs = (await s.execute(select(ORMSegment).where(ORMSegment.video_id == "v1"))).scalars().all()
        by_text = {x.text: x.source for x in segs}
        assert by_text["PPT 文字"] == "ocr"
        assert by_text["hi"] == "subtitle"
        chunks = (await s.execute(select(Chunk).where(Chunk.video_id == "v1"))).scalars().all()
        assert chunks and all("src" in (c.meta or {}) for c in chunks)
    await engine.dispose()


async def test_process_video_visual_off_does_not_trigger(tmp_path, monkeypatch):
    from app.jobs import pipeline as pl
    from app.jobs.pipeline import process_video
    from app.models import Segment as ORMSegment, Video

    engine, factory = await _prepare_video(tmp_path, "v2")
    fetchers, transcribers = _speech_fixtures()

    async def should_not_call(*a, **k):
        raise AssertionError("视觉层不应在 off 档被调用")

    monkeypatch.setattr(pl, "run_visual_pipeline", should_not_call)
    monkeypatch.setattr(pl, "fetch_video", should_not_call)

    await process_video(
        "v2", factory, fetchers, transcribers, _FakeLLM(), str(tmp_path),
        embedder=_FakeEmbedder(), vector_store=_FakeStore(),
        visual_pipeline="off",
    )
    async with factory() as s:
        assert (await s.get(Video, "v2")).status == "done"
        segs = (await s.execute(select(ORMSegment).where(ORMSegment.video_id == "v2"))).scalars().all()
        assert [x.source for x in segs] == ["subtitle"]
    await engine.dispose()


async def test_process_video_visual_failure_is_isolated(tmp_path, monkeypatch):
    """视觉层抛错 → 只记日志，视频仍 done，原有转写照常落库。"""
    from app.jobs import pipeline as pl
    from app.jobs.pipeline import process_video
    from app.models import Segment as ORMSegment, Video

    engine, factory = await _prepare_video(tmp_path, "v3")
    fetchers, transcribers = _speech_fixtures()

    async def boom(*a, **k):
        raise RuntimeError("vision down")

    async def fake_fetch_video(url, workdir, cookie_dir=None, **kw):
        return "/tmp/fake.mp4"

    monkeypatch.setattr(pl, "run_visual_pipeline", boom)
    monkeypatch.setattr(pl, "fetch_video", fake_fetch_video)

    await process_video(
        "v3", factory, fetchers, transcribers, _FakeLLM(), str(tmp_path),
        embedder=_FakeEmbedder(), vector_store=_FakeStore(),
        visual_pipeline="always",
    )
    async with factory() as s:
        assert (await s.get(Video, "v3")).status == "done"
        segs = (await s.execute(select(ORMSegment).where(ORMSegment.video_id == "v3"))).scalars().all()
        assert [x.text for x in segs] == ["hi"]
    await engine.dispose()


async def test_process_video_auto_skips_normal_speech(tmp_path, monkeypatch):
    """auto 档：正常口播（密度高）不触发视觉层。"""
    from app.jobs import pipeline as pl
    from app.jobs.pipeline import process_video
    from app.models import Video

    engine, factory = await _prepare_video(tmp_path, "v4")
    fetchers, transcribers = _speech_fixtures()

    async def should_not_call(*a, **k):
        raise AssertionError("正常语音不应触发视觉层")

    monkeypatch.setattr(pl, "run_visual_pipeline", should_not_call)

    await process_video(
        "v4", factory, fetchers, transcribers, _FakeLLM(), str(tmp_path),
        embedder=_FakeEmbedder(), vector_store=_FakeStore(),
        visual_pipeline="auto",
    )
    async with factory() as s:
        assert (await s.get(Video, "v4")).status == "done"
    await engine.dispose()


async def test_sweep_temp_media_includes_frames(tmp_path):
    from app.jobs.pipeline import sweep_temp_media

    frames = tmp_path / "frames" / "vid"
    frames.mkdir(parents=True)
    (frames / "scene_00001.jpg").write_bytes(b"x")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.md").write_text("keep")

    assert sweep_temp_media(str(tmp_path)) == 1
    assert not list((tmp_path / "frames").rglob("*.*"))
    assert (tmp_path / "notes" / "a.md").exists()
