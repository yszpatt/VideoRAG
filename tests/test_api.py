from sqlalchemy import select

from app.config import Settings
from app.core.fetchers.base import FetchedMedia
from app.core.transcribers.base import Segment, Transcript
from app.main import create_app
# 注意：ORM 的 Segment 必须别名导入，否则会遮蔽上面的转写器数据类 Segment，
# 导致 make_fake_components 的 Segment(0, 1, "hi") 以位置参数构造 ORM 模型而报错
from app.models import Chunk, Comment, Note, Segment as OrmSegment, Task, Video


def make_fake_components():
    class FakeFetcher:
        name = "fake-fetcher"

        async def fetch(self, url, workdir):
            return FetchedMedia(
                kind="subtitle", subtitle_text="hi", meta={"segments": [(0, 1, "hi")]}
            )

    class FakeTranscriber:
        name = "fake-transcriber"

        async def transcribe(self, media):
            return Transcript(segments=[Segment(0, 1, "hi")], raw_text="hi", source="subtitle")

    class FakeLLM:
        async def chat_json(self, messages, **kw):
            return {
                "summary": "测试摘要",
                "chapters": [{"title": "章节一", "start_sec": 0.0, "end_sec": 1.0, "points": ["点"]}],
                "key_points": ["要点1"],
                "quotes": [{"text": "hi", "start_sec": 0.0}],
                "glossary": [{"term": "RAG", "explanation": "检索增强生成"}],
            }

        async def chat(self, messages, **kw):
            return "RAG 是把检索和生成结合的方法[1]。"

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

        def delete_by_video_id(self, video_id):
            self.rows = [r for r in self.rows if r.get("video_id") != video_id]

    return [FakeFetcher()], [FakeTranscriber()], FakeLLM(), FakeEmbedder(), FakeStore()


def _make_app(tmp_path, fetchers=None, transcribers=None, llm=None, embedder=None, vector_store=None):
    settings = Settings(_env_file=None, data_dir=str(tmp_path))
    return create_app(
        settings=settings,
        fetchers=fetchers,
        transcribers=transcribers,
        llm=llm,
        embedder=embedder,
        vector_store=vector_store,
    )


async def _client(app):
    from httpx import ASGITransport, AsyncClient

    from app.db import init_db

    await init_db(app.state.engine, app.state.settings)
    transport = ASGITransport(app=app)
    c = AsyncClient(transport=transport, base_url="http://test")
    await c.__aenter__()
    return c


async def test_submit_video_creates_record(tmp_path):
    app = _make_app(tmp_path)
    c = await _client(app)
    resp = await c.post("/api/videos", json={"url": "https://www.youtube.com/watch?v=abc"})
    await c.__aexit__(None, None, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"

    async with app.state.session_factory() as s:
        v = await s.get(Video, body["video_id"])
        assert v.platform == "youtube"
        tasks = (
            await s.execute(select(Task).where(Task.video_id == body["video_id"]))
        ).scalars().all()
        assert len(tasks) == 1
        assert tasks[0].type == "process"
        assert tasks[0].status == "pending"


async def test_submit_video_rejects_empty_url(tmp_path):
    app = _make_app(tmp_path)
    c = await _client(app)
    resp = await c.post("/api/videos", json={"url": "  "})
    await c.__aexit__(None, None, None)
    assert resp.status_code == 400


async def test_list_videos_returns_submitted(tmp_path):
    app = _make_app(tmp_path)
    c = await _client(app)
    await c.post("/api/videos", json={"url": "https://www.bilibili.com/video/BV1aa"})
    await c.post("/api/videos", json={"url": "https://youtu.be/xyz"})
    r = await c.get("/api/videos")
    await c.__aexit__(None, None, None)

    assert r.status_code == 200
    items = r.json()
    assert len(items) == 2
    assert {i["platform"] for i in items} == {"bilibili", "youtube"}
    assert all(i["status"] == "pending" for i in items)


async def test_get_video_detail(tmp_path):
    app = _make_app(tmp_path)
    c = await _client(app)
    r1 = await c.post("/api/videos", json={"url": "https://youtu.be/abc"})
    vid = r1.json()["video_id"]
    r2 = await c.get(f"/api/videos/{vid}")
    await c.__aexit__(None, None, None)

    assert r2.status_code == 200
    body = r2.json()
    assert body["id"] == vid
    assert body["platform"] == "youtube"
    assert body["status"] == "pending"


async def test_list_videos_failed_progress_from_light_query(tmp_path):
    """列表接口：failed 视频的进度阶段仍由 segments/note/chunks 的存在性推断，
    但改由轻量聚合查询取得（不再加载正文关联）。回归保护：
    - failed + 有 segments 无 chunks → 卡在 noting 阶段（index 3）
    - failed + 无任何数据 → 卡在 fetching（index 1）
    """
    app = _make_app(tmp_path)
    c = await _client(app)

    async with app.state.session_factory() as s:
        s.add(Video(id="vA", platform="bilibili", url="u-a", status="failed", error="llm down"))
        s.add(Video(id="vB", platform="bilibili", url="u-b", status="failed", error="fetch failed"))
        # vA 有转写句子但无切片/笔记
        s.add(OrmSegment(video_id="vA", start_sec=0.0, end_sec=1.0, text="hi"))
        await s.commit()

    r = await c.get("/api/videos")
    await c.__aexit__(None, None, None)
    assert r.status_code == 200
    items = {i["id"]: i for i in r.json()}
    assert items["vA"]["progress"]["stage"] == "noting"
    assert items["vB"]["progress"]["stage"] == "fetching"


async def test_get_video_404(tmp_path):
    app = _make_app(tmp_path)
    c = await _client(app)
    resp = await c.get("/api/videos/nonexistent")
    await c.__aexit__(None, None, None)
    assert resp.status_code == 404


async def test_delete_video_cascade_removes_children_and_vectors(tmp_path):
    """回归：删除视频须联动清理子表（Chunk/Task/Note/Segment/Comment）与向量分片。

    早期 bug 漏删 Chunk/Task，commit 时 ORM 把关联外键置 NULL 触发 NOT NULL
    约束 → 整段 500 回滚，导致什么都没删（笔记/向量全残留）。本测试用真实
    session 预置各子表行，再走 DELETE 端点，断言全部被清理、向量库 rows 清空。
    """
    fetchers, transcribers, llm, embedder, store = make_fake_components()
    app = _make_app(tmp_path, fetchers, transcribers, llm, embedder, store)
    c = await _client(app)
    vid = "testvid_cascade"
    async with app.state.session_factory() as s:
        s.add(Video(id=vid, platform="youtube", status="done", url="https://www.youtube.com/watch?v=abc"))
        s.add(OrmSegment(video_id=vid, start_sec=0.0, end_sec=1.0, text="hi"))
        s.add(Chunk(video_id=vid, content="hi", start_sec=0.0, end_sec=1.0))
        s.add(Task(video_id=vid, type="embed", status="done"))
        s.add(Note(video_id=vid, summary="s", markdown="# x"))
        s.add(Comment(video_id=vid, text="c"))
        await s.commit()
    store.rows = [{"video_id": vid, "content": "hi"}]  # 模拟向量库已有该视频分片

    resp = await c.delete(f"/api/videos/{vid}")
    assert resp.status_code == 200, resp.text

    async with app.state.session_factory() as s:
        assert await s.get(Video, vid) is None
        for model in (Chunk, Task, Note, OrmSegment, Comment):
            n = (await s.execute(select(model).where(model.video_id == vid))).scalars().all()
            assert len(n) == 0, model.__name__

    assert store.rows == [], "向量分片应随视频删除被清理"
    await c.__aexit__(None, None, None)


async def test_transcript_after_pipeline(tmp_path):
    fetchers, transcribers, llm, embedder, store = make_fake_components()
    app = _make_app(tmp_path, fetchers, transcribers, llm, embedder, store)
    c = await _client(app)
    r1 = await c.post("/api/videos", json={"url": "https://www.youtube.com/watch?v=abc"})
    vid = r1.json()["video_id"]
    await app.state.queue.process_one()  # 模拟 worker 消费
    r2 = await c.get(f"/api/videos/{vid}/transcript")
    await c.__aexit__(None, None, None)

    assert r2.status_code == 200
    segs = r2.json()["segments"]
    assert len(segs) == 1
    assert segs[0]["text"] == "hi"
    assert segs[0]["start_sec"] == 0


async def test_note_after_pipeline(tmp_path):
    fetchers, transcribers, llm, embedder, store = make_fake_components()
    app = _make_app(tmp_path, fetchers, transcribers, llm, embedder, store)
    c = await _client(app)
    r1 = await c.post("/api/videos", json={"url": "https://www.youtube.com/watch?v=abc"})
    vid = r1.json()["video_id"]
    await app.state.queue.process_one()
    r2 = await c.get(f"/api/videos/{vid}/note")
    await c.__aexit__(None, None, None)

    assert r2.status_code == 200
    body = r2.json()
    assert body["summary"] == "测试摘要"
    assert body["key_points"] == ["要点1"]
    assert "# " in body["markdown"]


async def test_ask_returns_answer_and_citations(tmp_path):
    fetchers, transcribers, llm, embedder, store = make_fake_components()
    app = _make_app(tmp_path, fetchers, transcribers, llm, embedder, store)
    c = await _client(app)
    resp = await c.post("/api/ask", json={"question": "什么是RAG", "top_k": 3})
    await c.__aexit__(None, None, None)

    assert resp.status_code == 200
    body = resp.json()
    assert "RAG" in body["answer"]
    assert len(body["citations"]) == 1
    assert body["citations"][0]["title"] == "RAG教程"
    assert body["citations"][0]["start_sec"] == 0.0


async def test_search_returns_hits(tmp_path):
    fetchers, transcribers, llm, embedder, store = make_fake_components()
    app = _make_app(tmp_path, fetchers, transcribers, llm, embedder, store)
    c = await _client(app)
    resp = await c.post("/api/search", json={"query": "RAG", "top_k": 3})
    await c.__aexit__(None, None, None)

    assert resp.status_code == 200
    hits = resp.json()["hits"]
    assert len(hits) == 1
    assert hits[0]["content"] == "RAG 结合检索与生成"
    assert hits[0]["start_sec"] == 0.0


async def test_ask_citations_include_video_info(tmp_path):
    fetchers, transcribers, llm, embedder, store = make_fake_components()
    app = _make_app(tmp_path, fetchers, transcribers, llm, embedder, store)
    c = await _client(app)
    # 先建一条视频记录，验证 url 能带进引用
    async with app.state.session_factory() as s:
        s.add(Video(id="v1", platform="bilibili", url="https://www.bilibili.com/video/BV1xx", title="RAG教程"))
        await s.commit()
    resp = await c.post("/api/ask", json={"question": "什么是RAG"})
    await c.__aexit__(None, None, None)
    cit = resp.json()["citations"][0]
    assert cit["video_id"] == "v1"
    assert cit["platform"] == "bilibili"
    assert cit["url"] == "https://www.bilibili.com/video/BV1xx"


async def test_static_index_served(tmp_path):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<h1>videorag ui</h1>", encoding="utf-8")
    app = create_app(
        settings=Settings(_env_file=None, data_dir=str(tmp_path)),
        static_dir=str(static_dir),
    )
    c = await _client(app)
    r = await c.get("/")
    await c.__aexit__(None, None, None)
    assert r.status_code == 200
    assert "videorag ui" in r.text


async def test_missing_static_dir_does_not_break_api(tmp_path):
    app = create_app(
        settings=Settings(_env_file=None, data_dir=str(tmp_path)),
        static_dir=str(tmp_path / "missing"),
    )
    c = await _client(app)
    r = await c.get("/health")
    await c.__aexit__(None, None, None)
    assert r.status_code == 200
