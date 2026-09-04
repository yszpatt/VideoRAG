"""进度派生：video.status → 前端可直接渲染的阶段结构。"""

from httpx import ASGITransport, AsyncClient

from app.core.progress import compute_progress
from app.main import create_app
from app.models import Chunk, Note, Segment, Video
from app.config import Settings

STAGE_KEYS = ["queued", "fetching", "transcribing", "noting", "embedding"]


def test_queued_marks_only_first_stage_running():
    p = compute_progress("pending")
    assert [s["key"] for s in p["stages"]] == STAGE_KEYS
    assert p["stages"][0]["state"] == "running"
    assert all(s["state"] == "pending" for s in p["stages"][1:])
    assert p["percent"] < 10
    assert p["failed"] is False


def test_transcribing_marks_previous_stages_done():
    p = compute_progress("transcribing")
    assert [s["state"] for s in p["stages"]] == [
        "done", "done", "running", "pending", "pending",
    ]
    assert p["stage"] == "transcribing"
    assert p["index"] == 2


def test_done_completes_every_stage():
    p = compute_progress("done")
    assert all(s["state"] == "done" for s in p["stages"])
    assert p["percent"] == 100
    assert p["stage"] == "done"


def test_failed_without_segments_fails_at_fetching():
    p = compute_progress("failed", has_segments=False)
    assert p["failed"] is True
    assert [s["state"] for s in p["stages"]] == [
        "done", "failed", "pending", "pending", "pending",
    ]


def test_failed_with_note_but_no_chunks_fails_at_embedding():
    p = compute_progress("failed", has_segments=True, has_note=True, has_chunks=False)
    assert [s["state"] for s in p["stages"]] == [
        "done", "done", "done", "done", "failed",
    ]


def test_failed_with_chunks_fails_at_embedding_too():
    p = compute_progress("failed", has_segments=True, has_note=True, has_chunks=True)
    assert p["stages"][-1]["state"] == "failed"


def test_percent_never_exceeds_100_and_is_monotonic():
    order = ["pending", "fetching", "transcribing", "noting", "embedding", "done"]
    percents = [compute_progress(s)["percent"] for s in order]
    assert percents == sorted(percents)
    assert max(percents) == 100
    assert min(percents) >= 0


def _make_app(tmp_path):
    return create_app(settings=Settings(_env_file=None, data_dir=str(tmp_path)))


async def _client(app):
    from app.db import init_db

    await init_db(app.state.engine, app.state.settings)
    transport = ASGITransport(app=app)
    c = AsyncClient(transport=transport, base_url="http://test")
    await c.__aenter__()
    return c


async def _seed(app, status, *, with_segments=False, with_note=False, with_chunks=False):
    async with app.state.session_factory() as s:
        v = Video(platform="bilibili", url="https://www.bilibili.com/video/BV1aa",
                  title="标题", author="UP主", duration_sec=120.5, status=status)
        s.add(v)
        await s.flush()
        if with_segments:
            s.add(Segment(video_id=v.id, start_sec=0, end_sec=1, text="你好"))
        if with_note:
            s.add(Note(video_id=v.id, summary="摘要", markdown="# 标题"))
        if with_chunks:
            s.add(Chunk(video_id=v.id, content="片段", start_sec=0, end_sec=1,
                        lancedb_id="c1"))
        await s.commit()
        return v.id


async def test_api_list_includes_progress_and_metadata(tmp_path):
    app = _make_app(tmp_path)
    c = await _client(app)
    await _seed(app, "noting", with_segments=True)
    r = await c.get("/api/videos")
    await c.__aexit__(None, None, None)

    item = r.json()[0]
    assert item["author"] == "UP主"
    assert item["duration_sec"] == 120.5
    assert item["progress"]["stage"] == "noting"
    assert item["progress"]["stages"][3]["state"] == "running"
    assert item["progress"]["stages"][4]["state"] == "pending"


async def test_api_detail_progress_reflects_failed_stage(tmp_path):
    app = _make_app(tmp_path)
    c = await _client(app)
    vid = await _seed(app, "failed", with_segments=True)
    r = await c.get(f"/api/videos/{vid}")
    await c.__aexit__(None, None, None)

    p = r.json()["progress"]
    assert p["failed"] is True
    assert [s["state"] for s in p["stages"]] == [
        "done", "done", "done", "failed", "pending",
    ]
