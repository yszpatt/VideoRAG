"""本地视频导入：上传端点 + 流水线本地分支测试。

覆盖：上传落盘与入库（platform=local / local_path / 默认收藏夹 / 入队）、扩展名与空内容校验、
大小上限、文件名路径穿越、删除视频时清理源文件，以及**流水线处理本地文件时不走抓取、
且绝不删除用户的原始文件**。
"""

import json

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import init_db
from app.main import create_app
from app.models import Task, Video, VideoCollection


@pytest.fixture
async def app(tmp_path):
    """本文件需要真正建库（conftest 的 app 不跑 lifespan，init_db 不会执行）。"""
    application = create_app(settings=Settings(_env_file=None, data_dir=str(tmp_path)))
    await init_db(application.state.engine, application.state.settings)
    yield application
    await application.state.engine.dispose()


async def _upload(client, name="我的录屏.mp4", data=b"fake-video-bytes"):
    return await client.post(
        f"/api/videos/upload?filename={name}",
        content=data,
        headers={"Content-Type": "application/octet-stream"},
    )


async def test_upload_registers_local_video(client, app):
    r = await _upload(client)
    assert r.status_code == 200, r.text
    body = r.json()
    vid = body["video_id"]
    assert body["title"] == "我的录屏"          # 标题取文件名（去扩展名）
    assert body["local_path"] == f"uploads/{vid}.mp4"
    assert body["status"] == "pending"

    # 文件确实落在数据目录的 uploads/ 下
    dest = app.state.settings.data_dir + f"/uploads/{vid}.mp4"
    from pathlib import Path

    p = Path(dest)
    assert p.is_file() and p.read_bytes() == b"fake-video-bytes"

    # 视频行：platform=local + local_path，且已入队、已归默认收藏夹
    async with app.state.session_factory() as s:
        v = await s.get(Video, vid)
        assert v.platform == "local" and v.local_path == f"uploads/{vid}.mp4"
        assert v.url == ""
        tasks = (await s.execute(select(Task).where(Task.video_id == vid))).scalars().all()
        assert len(tasks) == 1 and tasks[0].type == "process"
        links = (
            await s.execute(
                select(VideoCollection.collection_id).where(
                    VideoCollection.video_id == vid
                )
            )
        ).scalars().all()
        assert len(links) == 1  # 默认收藏夹

    # 列表接口能查到（前端导入后立即刷新就能看到）
    listed = (await client.get("/api/videos")).json()
    assert [v["id"] for v in listed] == [vid]


async def test_upload_validates_extension_and_body(client):
    r = await client.post("/api/videos/upload?filename=note.txt", content=b"x")
    assert r.status_code == 422
    assert "不支持的文件类型" in r.json()["detail"]

    r2 = await client.post("/api/videos/upload?filename=noext", content=b"x")
    assert r2.status_code == 422

    r3 = await _upload(client, data=b"")
    assert r3.status_code == 422
    assert "为空" in r3.json()["detail"]


async def test_upload_enforces_size_cap(client, monkeypatch):
    from app.api import videos as videos_api

    monkeypatch.setattr(videos_api, "MAX_UPLOAD_BYTES", 16)
    r = await client.post(
        "/api/videos/upload?filename=big.mp4",
        content=b"x" * 64,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert r.status_code == 413
    # 超限时不留半截文件
    uploads = videos_api.Path(client._transport.app.state.settings.data_dir) / "uploads"
    assert not uploads.exists() or not any(uploads.iterdir())


async def test_upload_neutralizes_path_in_filename(client, app):
    r = await client.post(
        "/api/videos/upload?filename=../../etc/passwd.mp4", content=b"data"
    )
    assert r.status_code == 200
    vid = r.json()["video_id"]
    assert r.json()["local_path"] == f"uploads/{vid}.mp4"  # 落盘名只用 id，无目录成分
    from pathlib import Path

    assert (Path(app.state.settings.data_dir) / "uploads" / f"{vid}.mp4").is_file()


async def test_delete_video_removes_local_file(client, app):
    from pathlib import Path

    vid = (await _upload(client)).json()["video_id"]
    src = Path(app.state.settings.data_dir) / "uploads" / f"{vid}.mp4"
    assert src.is_file()

    assert (await client.delete(f"/api/videos/{vid}")).status_code == 200
    assert not src.exists()  # 源文件随视频删除，避免 uploads/ 无限增长
    assert (await client.get(f"/api/videos/{vid}")).status_code == 404


async def test_pipeline_handles_local_file_without_fetching_or_deleting(tmp_path):
    """本地导入：不走抓取链、能正常转写/入库，且**不删除用户原始文件**。"""
    from app.core.fetchers.base import FetchedMedia
    from app.core.transcribers.base import Segment, Transcript
    from app.db import make_session_factory
    from app.jobs.pipeline import process_video
    from tests.test_pipeline import FakeEmbedder, FakeFetcher, FakeLLM, PurgeAwareStore

    settings = Settings(_env_file=None, data_dir=str(tmp_path))
    engine, factory = make_session_factory(settings)
    await init_db(engine, settings)

    # 造一个「上传好的」本地文件
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    src = uploads / "vid-local.mp4"
    src.write_bytes(b"local-video-bytes")

    async with factory() as s:
        s.add(
            Video(
                id="vid-local",
                platform="local",
                url="",
                title="本地录屏",
                local_path="uploads/vid-local.mp4",
            )
        )
        await s.commit()

    class FakeTranscriber:
        name = "fake"

        async def transcribe(self, media):
            assert media.kind == "video"          # 本地视频按视频媒体处理
            assert media.path == str(src)         # 直接复用源文件
            assert media.meta.get("local") is True
            return Transcript(
                segments=[Segment(0.0, 2.0, "本地视频的转写")],
                raw_text="本地视频的转写",
                source="whisper",
            )

    store = PurgeAwareStore()
    await process_video(
        "vid-local",
        factory,
        # 抓取链一旦被调用就报错：本地导入必须完全跳过 yt-dlp
        [FakeFetcher(name="ytdlp", raise_=AssertionError("本地导入不应调用抓取链"))],
        [FakeTranscriber()],
        FakeLLM(),
        str(tmp_path),
        embedder=FakeEmbedder(),
        vector_store=store,
    )

    async with factory() as s:
        v = await s.get(Video, "vid-local")
        assert v.status == "done", v.error
    assert src.is_file(), "本地导入的原始文件绝不能被清理流程删除"
    assert src.read_bytes() == b"local-video-bytes"
    assert len(store.rows) == 1  # 正常入库
    await engine.dispose()


async def test_pipeline_fails_clearly_when_local_file_missing(tmp_path):
    """源文件被移走/删掉时给出可读报错，而不是静默失败。"""
    from app.db import make_session_factory
    from app.jobs.pipeline import process_video
    from tests.test_pipeline import FakeEmbedder, FakeLLM, PurgeAwareStore

    settings = Settings(_env_file=None, data_dir=str(tmp_path))
    engine, factory = make_session_factory(settings)
    await init_db(engine, settings)
    async with factory() as s:
        s.add(
            Video(
                id="v-gone",
                platform="local",
                url="",
                title="丢了",
                local_path="uploads/not-there.mp4",
            )
        )
        await s.commit()

    await process_video(
        "v-gone", factory, [], [], FakeLLM(), str(tmp_path),
        embedder=FakeEmbedder(), vector_store=PurgeAwareStore(),
    )
    async with factory() as s:
        v = await s.get(Video, "v-gone")
        assert v.status == "failed"
        assert "本地文件不存在" in (v.error or "")
    await engine.dispose()
