import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.config import Settings
from app.db import init_db, make_session_factory
from app.mcp_server import _ask, _get_note, _get_transcript, _list_videos, _search, _submit
from app.mcp_server import mcp_http_middleware
from app.models import Note, Segment, Video


async def _ok_app(scope, receive, send):
    body = b"ok"
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def test_middleware_requires_token():
    wrapped = mcp_http_middleware(_ok_app, api_key="secret", rate_per_minute=1000)
    transport = ASGITransport(app=wrapped)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.post("/mcp")).status_code == 401
        r = await c.post("/mcp", headers={"Authorization": "Bearer secret"})
        assert r.status_code == 200
        assert r.text == "ok"


async def test_middleware_no_key_skips_auth():
    wrapped = mcp_http_middleware(_ok_app, api_key="", rate_per_minute=1000)
    transport = ASGITransport(app=wrapped)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.post("/mcp")).status_code == 200


async def test_middleware_rate_limits_per_ip():
    wrapped = mcp_http_middleware(_ok_app, api_key="", rate_per_minute=2)
    transport = ASGITransport(app=wrapped)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.post("/mcp")).status_code == 200
        assert (await c.post("/mcp")).status_code == 200
        assert (await c.post("/mcp")).status_code == 429


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1.0"},
    },
}


async def _mcp_http_client(app):
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://t", follow_redirects=True)


async def test_mcp_end_to_end_jsonrpc(tmp_path):
    from app.main import create_app

    settings = Settings(_env_file=None, data_dir=str(tmp_path), mcp_api_key="")
    app = create_app(settings=settings)
    from app.db import init_db

    await init_db(app.state.engine, settings)
    async with app.state.session_factory() as s:
        s.add(Video(id="v1", platform="bilibili", url="https://www.bilibili.com/video/BV1", title="RAG教程", status="done"))
        await s.commit()

    headers = {"Accept": "application/json, text/event-stream"}
    async with app.router.lifespan_context(app):
        c = await _mcp_http_client(app)
        r = await c.post("/mcp", headers=headers, json=INIT)
        assert r.status_code == 200, r.text

        r2 = await c.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        tools = [t["name"] for t in r2.json()["result"]["tools"]]
        assert "search_knowledge_base" in tools
        assert "ask_video_rag" in tools
        assert "list_videos" in tools
        assert "submit_video" in tools

        r3 = await c.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_videos", "arguments": {}}},
        )
        text = r3.json()["result"]["content"][0]["text"]
        assert "RAG教程" in text
        await c.aclose()
    await app.state.engine.dispose()


async def test_mcp_end_to_end_auth_required(tmp_path):
    from app.main import create_app

    settings = Settings(_env_file=None, data_dir=str(tmp_path), mcp_api_key="secret")
    app = create_app(settings=settings)
    from app.db import init_db

    await init_db(app.state.engine, settings)
    async with app.router.lifespan_context(app):
        c = await _mcp_http_client(app)
        r = await c.post("/mcp", headers={"Accept": "application/json, text/event-stream"}, json=INIT)
        assert r.status_code == 401
        await c.aclose()
    await app.state.engine.dispose()


class FakeEmbedder:
    async def embed_texts(self, texts, query=False):
        return [[1.0, 0.0]] * len(texts)


class FakeStore:
    def search(self, qv, top_k=5, where=None):
        return [
            {
                "id": "c1",
                "video_id": "v1",
                "content": "RAG 结合检索与生成",
                "start_sec": 0.0,
                "end_sec": 3.0,
                "title": "RAG教程",
                "platform": "bilibili",
            }
        ]

    def search_text(self, text, top_k=5, where=None):
        return []


class FakeLLM:
    async def chat(self, messages, **kw):
        return "RAG 是先检索再生成的方法[1]。"

    async def chat_json(self, messages, **kw):
        return {"summary": "s", "chapters": [], "key_points": [], "quotes": [], "glossary": []}


class FakeQueue:
    def __init__(self):
        self.enqueued = []

    async def enqueue(self, video_id, type, payload=None):
        self.enqueued.append((video_id, type, payload))
        return "task1"


@pytest.fixture
async def components(tmp_path):
    settings = Settings(_env_file=None, data_dir=str(tmp_path))
    engine, sf = make_session_factory(settings)
    await init_db(engine, settings)
    async with sf() as s:
        s.add(Video(id="v1", platform="bilibili", url="https://www.bilibili.com/video/BV1", title="RAG教程", status="done"))
        s.add(Segment(id="seg1", video_id="v1", start_sec=0.0, end_sec=3.0, text="RAG 结合检索与生成"))
        s.add(Note(id="n1", video_id="v1", summary="视频摘要", markdown="# RAG教程"))
        await s.commit()
    comps = {
        "session_factory": sf,
        "vector_store": FakeStore(),
        "embedder": FakeEmbedder(),
        "llm": FakeLLM(),
        "queue": FakeQueue(),
    }
    yield comps
    await engine.dispose()


async def test_mcp_search(components):
    hits = await _search(components, "什么是RAG")
    assert hits[0]["video_id"] == "v1"
    assert hits[0]["content"] == "RAG 结合检索与生成"
    assert hits[0]["start_sec"] == 0.0


async def test_mcp_ask(components):
    result = await _ask(components, "什么是RAG")
    assert "RAG" in result["answer"]
    assert result["citations"][0]["title"] == "RAG教程"


async def test_mcp_list_videos(components):
    videos = await _list_videos(components)
    assert len(videos) == 1
    assert videos[0]["video_id"] == "v1"
    assert videos[0]["status"] == "done"


async def test_mcp_list_videos_filter(components):
    videos = await _list_videos(components, platform="youtube")
    assert videos == []


async def test_mcp_get_note(components):
    note = await _get_note(components, "v1")
    assert note["summary"] == "视频摘要"
    assert "# RAG教程" in note["markdown"]


async def test_mcp_get_note_missing(components):
    assert await _get_note(components, "nope") is None


async def test_mcp_get_transcript_range(components):
    segs = await _get_transcript(components, "v1", start=1.0)
    assert len(segs) == 1
    assert segs[0]["text"] == "RAG 结合检索与生成"


async def test_mcp_submit(components):
    result = await _submit(components, "https://www.bilibili.com/video/BV1submit")
    assert result["status"] == "pending"
    assert components["queue"].enqueued == [(result["video_id"], "process", {"video_id": result["video_id"]})]
    async with components["session_factory"]() as s:
        v = await s.get(Video, result["video_id"])
        assert v.platform == "bilibili"
