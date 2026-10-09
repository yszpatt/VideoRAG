"""视频收藏夹 API（/api/collections + 视频归属）测试。

覆盖：默认收藏夹自动存在、CRUD 与重名/边界、导入即归默认夹、多夹归属的整表替换、
默认夹不可取消、按收藏夹过滤列表、删除收藏夹只解关系不动视频。
"""

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import init_db
from app.main import create_app
from app.models import Video, VideoCollection


@pytest.fixture
async def app(tmp_path):
    """覆盖 conftest 的同名 fixture：本文件需要**真正建库**。

    conftest 的 app fixture 不跑 lifespan（httpx 的 ASGITransport 不触发），因此
    init_db 不会执行、db/ 目录也不存在；凡是要读写数据库的测试都得自己先建库。
    """
    application = create_app(settings=Settings(_env_file=None, data_dir=str(tmp_path)))
    await init_db(application.state.engine, application.state.settings)
    yield application
    await application.state.engine.dispose()


async def _mk_video(app, vid="v1"):
    async with app.state.session_factory() as s:
        s.add(Video(id=vid, platform="youtube", url=f"https://youtu.be/{vid}", title=vid))
        await s.commit()


async def test_default_collection_exists_and_backfills(client, app):
    """默认收藏夹随建库出现，并收录没有任何归属的视频（老库升级路径）。"""
    await _mk_video(app, "v1")
    # fixture 已建过库（当时没有视频），这里再跑一次 init_db 模拟「再次启动」：
    # 孤儿视频应被补挂进默认收藏夹（老库升级路径）
    await init_db(app.state.engine, app.state.settings)

    d = (await client.get("/api/collections")).json()
    default = [c for c in d["collections"] if c["is_default"]]
    assert len(default) == 1
    assert default[0]["name"] == "默认收藏夹"
    assert default[0]["count"] == 1
    assert d["total_videos"] == 1


async def test_import_lands_in_default_collection(client, app):
    """新导入的视频自动进默认收藏夹（不需要等下次启动的孤儿修复）。"""
    r = await client.post("/api/videos", json={"url": "https://www.bilibili.com/video/BV1xx"})
    assert r.status_code == 200, r.text
    vid = r.json()["video_id"]

    cols = (await client.get("/api/collections")).json()["collections"]
    default = next(c for c in cols if c["is_default"])
    assert default["count"] == 1

    listed = (await client.get(f"/api/videos?collection_id={default['id']}")).json()
    assert [v["id"] for v in listed] == [vid]
    assert listed[0]["collection_ids"] == [default["id"]]


async def test_create_rename_delete_collection(client):
    created = (await client.post("/api/collections", json={"name": " 教程 "})).json()
    cid = created["collection"]["id"]
    assert created["collection"]["name"] == "教程"  # 去掉首尾空白
    assert created["collection"]["is_default"] is False

    renamed = (await client.patch(f"/api/collections/{cid}", json={"name": "AI 教程"})).json()
    assert renamed["collection"]["name"] == "AI 教程"

    assert (await client.delete(f"/api/collections/{cid}")).status_code == 200
    names = [c["name"] for c in (await client.get("/api/collections")).json()["collections"]]
    assert "AI 教程" not in names


async def test_duplicate_and_invalid_names_rejected(client):
    await client.post("/api/collections", json={"name": "重复"})
    assert (await client.post("/api/collections", json={"name": "重复"})).status_code == 409
    assert (await client.post("/api/collections", json={"name": "   "})).status_code == 422
    assert (
        await client.post("/api/collections", json={"name": "x" * 41})
    ).status_code == 422


async def test_default_collection_cannot_be_deleted(client):
    cols = (await client.get("/api/collections")).json()["collections"]
    default = next(c for c in cols if c["is_default"])
    r = await client.delete(f"/api/collections/{default['id']}")
    assert r.status_code == 400
    assert "默认收藏夹" in r.json()["detail"]


async def test_video_can_join_multiple_collections(client, app):
    await _mk_video(app, "v1")
    other = (await client.post("/api/collections", json={"name": "稍后再看"})).json()["collection"]["id"]
    default = next(
        c["id"] for c in (await client.get("/api/collections")).json()["collections"] if c["is_default"]
    )

    r = await client.put("/api/videos/v1/collections", json={"collection_ids": [other]})
    assert r.status_code == 200
    # 默认夹被自动保留（不可取消）
    assert r.json()["collection_ids"] == sorted([default, other])

    got = (await client.get("/api/videos/v1")).json()
    assert sorted(got["collection_ids"]) == sorted([default, other])

    # 两个夹里都能查到该视频
    for cid in (default, other):
        ids = [v["id"] for v in (await client.get(f"/api/videos?collection_id={cid}")).json()]
        assert ids == ["v1"]


async def test_default_collection_membership_cannot_be_dropped(client, app):
    """即使前端只传空数组，默认夹也会被加回来（保证视频总有归处）。"""
    await _mk_video(app, "v1")
    default = next(
        c["id"] for c in (await client.get("/api/collections")).json()["collections"] if c["is_default"]
    )
    r = await client.put("/api/videos/v1/collections", json={"collection_ids": []})
    assert r.json()["collection_ids"] == [default]

    async with app.state.session_factory() as s:
        rows = (await s.execute(select(VideoCollection.video_id))).scalars().all()
    assert rows == ["v1"]


async def test_unknown_collection_rejected(client, app):
    await _mk_video(app, "v1")
    r = await client.put("/api/videos/v1/collections", json={"collection_ids": ["nope"]})
    assert r.status_code == 422
    assert "收藏夹不存在" in r.json()["detail"]

    r2 = await client.put("/api/videos/missing/collections", json={"collection_ids": []})
    assert r2.status_code == 404


async def test_list_filter_by_collection(client, app):
    await _mk_video(app, "v1")
    await _mk_video(app, "v2")
    other = (await client.post("/api/collections", json={"name": "播客"})).json()["collection"]["id"]
    await client.put("/api/videos/v1/collections", json={"collection_ids": [other]})

    in_other = (await client.get(f"/api/videos?collection_id={other}")).json()
    assert [v["id"] for v in in_other] == ["v1"]
    assert len((await client.get("/api/videos")).json()) == 2  # 不带过滤=全部


async def test_delete_collection_keeps_videos(client, app):
    await _mk_video(app, "v1")
    cid = (await client.post("/api/collections", json={"name": "临时"})).json()["collection"]["id"]
    await client.put("/api/videos/v1/collections", json={"collection_ids": [cid]})

    assert (await client.delete(f"/api/collections/{cid}")).json()["removed_links"] == 1

    # 视频还在，且仍在默认收藏夹里
    assert (await client.get("/api/videos/v1")).status_code == 200
    listed = (await client.get("/api/videos")).json()
    assert len(listed) == 1
    default = next(
        c["id"] for c in (await client.get("/api/collections")).json()["collections"] if c["is_default"]
    )
    assert listed[0]["collection_ids"] == [default]


async def test_collection_counts_are_per_collection(client, app):
    await _mk_video(app, "v1")
    await _mk_video(app, "v2")
    cid = (await client.post("/api/collections", json={"name": "两个"})).json()["collection"]["id"]
    await client.put("/api/videos/v1/collections", json={"collection_ids": [cid]})
    await client.put("/api/videos/v2/collections", json={"collection_ids": [cid]})

    cols = {c["name"]: c["count"] for c in (await client.get("/api/collections")).json()["collections"]}
    assert cols["两个"] == 2
    assert cols["默认收藏夹"] == 2  # 两个视频都在默认夹里
