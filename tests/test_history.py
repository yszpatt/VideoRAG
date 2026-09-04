"""E5 / M3：历史提问与检索记录——写入/去重/淘汰/API。"""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.config import Settings
from app.core.history import record_history, serialize_history
from app.db import init_db
from app.main import create_app
from app.models import QueryHistory


async def _make_app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None, data_dir=str(tmp_path))
    app = create_app(settings=settings)
    await init_db(app.state.engine, app.state.settings)
    return app


async def _count(app, kind=None):
    async with app.state.session_factory() as s:
        stmt = select(QueryHistory)
        if kind:
            stmt = stmt.where(QueryHistory.kind == kind)
        return len((await s.execute(stmt)).scalars().all())


# ============ record_history：写入 ============


async def test_record_ask_writes_answer_and_citations(tmp_path, monkeypatch):
    app = await _make_app(tmp_path, monkeypatch)
    citations = [{"title": "视频A", "video_id": "v1", "url": "https://x?t=3"}]
    await record_history(
        app.state.session_factory, "ask", "什么是RAG？",
        top_k=8, answer="答案文本", citations=citations,
    )
    assert await _count(app, "ask") == 1
    async with app.state.session_factory() as s:
        h = (await s.execute(select(QueryHistory))).scalar_one()
        assert h.kind == "ask"
        assert h.answer == "答案文本"
        assert h.citations_json == citations
        assert h.hit_count == 1
        assert h.query_key.startswith("ask\x00")
    await app.state.engine.dispose()


async def test_record_search_writes_hits_count(tmp_path, monkeypatch):
    app = await _make_app(tmp_path, monkeypatch)
    await record_history(app.state.session_factory, "search", "向量检索", hits_count=5)
    async with app.state.session_factory() as s:
        h = (await s.execute(select(QueryHistory))).scalar_one()
        assert h.kind == "search"
        assert h.hits_count == 5
        assert h.answer is None
    await app.state.engine.dispose()


async def test_record_truncates_answer_to_2000(tmp_path, monkeypatch):
    app = await _make_app(tmp_path, monkeypatch)
    await record_history(app.state.session_factory, "ask", "Q", answer="长" * 5000)
    async with app.state.session_factory() as s:
        h = (await s.execute(select(QueryHistory))).scalar_one()
        assert len(h.answer) == 2000
    await app.state.engine.dispose()


# ============ record_history：去重 ============


async def test_record_dedup_same_query(tmp_path, monkeypatch):
    app = await _make_app(tmp_path, monkeypatch)
    await record_history(app.state.session_factory, "ask", "什么是RAG？", answer="答案1")
    await record_history(app.state.session_factory, "ask", "什么是RAG？", answer="答案2")
    assert await _count(app, "ask") == 1  # 不新增
    async with app.state.session_factory() as s:
        h = (await s.execute(select(QueryHistory))).scalar_one()
        assert h.hit_count == 2
        assert h.answer == "答案2"  # 结果字段更新
    # 不同 kind 同 query 不合并
    await record_history(app.state.session_factory, "search", "什么是RAG？", hits_count=3)
    assert await _count(app) == 2
    await app.state.engine.dispose()


# ============ record_history：环形淘汰 ============


async def test_record_evicts_oldest_over_limit(tmp_path, monkeypatch):
    app = await _make_app(tmp_path, monkeypatch)
    # 先写 201 条 ask（limit=200），最早一条应被淘汰
    for i in range(201):
        await record_history(
            app.state.session_factory, "ask", f"问题 {i}", top_k=8,
        )
    assert await _count(app, "ask") == 200
    async with app.state.session_factory() as s:
        rows = (await s.execute(select(QueryHistory))).scalars().all()
        queries = {h.query for h in rows}
        assert "问题 0" not in queries  # 最旧被删
        assert "问题 200" in queries  # 最新保留
    await app.state.engine.dispose()


# ============ API ============


async def _client(app):
    transport = ASGITransport(app=app)
    c = AsyncClient(transport=transport, base_url="http://test")
    await c.__aenter__()
    return c


async def test_api_list_get_delete_clear(tmp_path, monkeypatch):
    app = await _make_app(tmp_path, monkeypatch)
    await record_history(app.state.session_factory, "ask", "问题A", answer="答案A")
    await record_history(
        app.state.session_factory, "ask", "问题B",
        answer="答案B", citations=[{"video_id": "v1", "url": "u"}],
    )
    await record_history(app.state.session_factory, "search", "检索词", hits_count=2)
    c = await _client(app)

    # 列表（按 kind 过滤 + 倒序：B 最新在前）
    resp = await c.get("/api/history", params={"kind": "ask"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [it["query"] for it in items] == ["问题B", "问题A"]
    assert "citations" not in items[0]  # 列表不带完整引用

    # 单条完整（含 citations）
    bid = items[0]["id"]
    detail = (await c.get(f"/api/history/{bid}")).json()
    assert detail["query"] == "问题B"
    assert detail["citations"] == [{"video_id": "v1", "url": "u"}]

    # 404
    assert (await c.get("/api/history/nope")).status_code == 404

    # 删除单条
    resp = await c.delete(f"/api/history/{bid}")
    assert resp.status_code == 200
    assert await _count(app, "ask") == 1

    # 清空 ask 类（search 保留）
    resp = await c.delete("/api/history", params={"kind": "ask"})
    assert resp.status_code == 200
    assert await _count(app, "ask") == 0
    assert await _count(app, "search") == 1
    await app.state.engine.dispose()


async def test_api_serialize_history():
    class Fake:
        id = "h1"
        kind = "ask"
        query = "q"
        top_k = 8
        answer = "a"
        citations_json = None
        hits_count = None
        hit_count = 1
        last_used_at = None
        created_at = None

    d = serialize_history(Fake(), full=True)
    assert d["id"] == "h1"
    assert d["citations"] == []
