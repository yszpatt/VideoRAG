"""/api/vectorstore 重建 API + 检索 409 处理器测试。

覆盖：
- 换模型后检索/提问 → 409 + 可读 detail（不再裸 500 Internal Server Error）
- POST /rebuild 启动后台任务 → 轮询 status → done，指纹落盘、维度刷新
- 重建后检索恢复 200
- 已有任务 running 时再触发 → 409
"""
import asyncio
import time


class FakeEmbed:
    """固定 2 维向量测试替身：模拟切换后的本地模型（与旧库 4 维/异模型不符）。"""

    fingerprint = {"provider": "fastembed", "model": "BAAI/bge-small-zh-v1.5"}

    async def embed_texts(self, texts, query=False):
        return [[0.5, 0.25]] * len(texts)


async def _seed_chunks(app) -> None:
    """向 init_db 建好的 chunks 表插入 2 条切片（c1 content / m1 meta）。

    依赖 init_db 先建 ORM 全 schema：
    - 用 ORM 插入而非裸 CREATE TABLE —— 裸建表会与 create_all 冲突
      （table already exists）；
    - 用 ORM 插入而非裸 sqlite3 INSERT —— created_at/updated_at 等
      NOT NULL 列由 Python 默认值自动填充；
    - videos 保持空表：select(Video) 返回空 → 不触发 relationship
      selectin 连查（tasks/segments/notes/comments），也无需 FK 父行。
    """
    from app.models import Chunk

    async with app.state.session_factory() as s:
        meta = {"title": "标题A", "platform": "bilibili"}
        s.add_all(
            [
                Chunk(
                    id="c1", video_id="v1", content="口播内容一",
                    start_sec=0.0, end_sec=10.0, kind="content", meta=meta,
                ),
                Chunk(
                    id="m1", video_id="v1", content="视频标题：标题A",
                    start_sec=0.0, end_sec=0.0, kind="meta", meta=meta,
                ),
            ]
        )
        await s.commit()


def _settings_of(app):
    return app.state.components["settings"]


def _use_fake_embedder(app):
    """把运行期 embedder 换成 2 维测试替身（components 与 app.state 同步，
    与 settings PUT 热更新一致；避免触发真实 fastembed 模型下载）。"""
    fake = FakeEmbed()
    app.state.components["embedder"] = fake
    app.state.embedder = fake
    return fake


async def _wait_done(client, timeout=6.0):
    """轮询重建状态直到非 running；返回最终快照。"""
    t0 = time.monotonic()
    while True:
        r = await client.get("/api/vectorstore/rebuild/status")
        st = r.json()
        if st["state"] != "running" or time.monotonic() - t0 > timeout:
            return st
        await asyncio.sleep(0.02)


async def test_rebuild_empty_db_writes_fingerprint(app, client):
    """空库重建：不报错，仅落盘当前模型指纹（首次建档）。"""
    from app.db import init_db
    await init_db(app.state.engine, app.state.settings)  # 建全表（含空 chunks）

    r = await client.post("/api/vectorstore/rebuild")
    assert r.status_code == 200, r.text
    st = await _wait_done(client)
    assert st["state"] == "done"
    assert st["total"] == 0

    from app.core.vector_store import VectorStore
    vs = VectorStore(_settings_of(app).lancedb_path)
    meta = vs.get_model_meta()
    assert meta and meta["model"] == "BAAI/bge-small-zh-v1.5"


async def test_search_incompatible_returns_readable_409(app, client):
    """换模型后检索：409 + 中文指引（模型已切换），不再是裸 500。"""
    s = _settings_of(app)
    vs = app.state.components["vector_store"]
    # 旧库：4 维向量 + 远程 bge-m3 指纹
    vs.add(
        [
            {
                "id": "c1", "video_id": "v1", "content": "口播内容一",
                "embedding": [0.1, 0.2, 0.3, 0.4], "start_sec": 0.0,
                "end_sec": 10.0, "kind": "content", "title": "标题A",
                "platform": "bilibili",
            }
        ]
    )
    vs.set_model_meta({"provider": "openai", "model": "bge-m3:latest", "dim": 4})
    # 当前 embedder 换成 2 维本地模型（模拟用户切到本地后未重建）
    _use_fake_embedder(app)

    r = await client.post("/api/search", json={"query": "标题", "top_k": 3})
    assert r.status_code == 409, r.text
    assert "模型已切换" in r.json()["detail"]
    assert "重建知识库" in r.json()["detail"]


async def test_rebuild_full_flow_restores_search(app, client):
    """重建全流程：旧库不兼容 → rebuild → 指纹/维度更新 → 检索恢复 200。"""
    from app.db import init_db

    s = _settings_of(app)
    # 先建 ORM 全 schema（videos 空表带全列，避免检索后 _video_urls 的
    # select(Video) 报 no such column）；再 ORM 插入 2 条切片。
    await init_db(app.state.engine, s)
    await _seed_chunks(app)

    vs = app.state.components["vector_store"]
    vs.add(
        [
            {
                "id": "c1", "video_id": "v1", "content": "口播内容一",
                "embedding": [0.1, 0.2, 0.3, 0.4], "start_sec": 0.0,
                "end_sec": 10.0, "kind": "content", "title": "标题A",
                "platform": "bilibili",
            },
            {
                "id": "m1", "video_id": "v1", "content": "视频标题：标题A",
                "embedding": [0.1, 0.2, 0.3, 0.4], "start_sec": 0.0,
                "end_sec": 0.0, "kind": "meta", "title": "标题A",
                "platform": "bilibili",
            },
        ]
    )
    vs.set_model_meta({"provider": "openai", "model": "bge-m3:latest", "dim": 4})
    _use_fake_embedder(app)

    # 重建前检索 → 409
    pre = await client.post("/api/search", json={"query": "标题", "top_k": 3})
    assert pre.status_code == 409

    r = await client.post("/api/vectorstore/rebuild")
    assert r.status_code == 200, r.text
    st = await _wait_done(client)
    assert st["state"] == "done"
    assert st["total"] == 2

    meta = vs.get_model_meta()
    assert meta["provider"] == "fastembed"
    assert meta["model"] == "BAAI/bge-small-zh-v1.5"
    assert meta["dim"] == 2

    # 重建后检索恢复 200（维度一致 + 指纹一致）
    ok = await client.post("/api/search", json={"query": "标题", "top_k": 3})
    assert ok.status_code == 200, ok.text
    assert isinstance(ok.json()["hits"], list)


async def test_rebuild_conflict_while_running(app, client):
    """已有任务 running 时再次触发 → 409（单飞）。"""
    app.state.vector_rebuild = {
        "state": "running", "done": 1, "total": 10, "error": "",
        "started_at": 0.0, "finished_at": 0.0,
    }
    r = await client.post("/api/vectorstore/rebuild")
    assert r.status_code == 409
    assert "进行中" in r.json()["detail"]


async def test_rebuild_status_idle_initial(app, client):
    r = await client.get("/api/vectorstore/rebuild/status")
    assert r.status_code == 200
    assert r.json()["state"] == "idle"
