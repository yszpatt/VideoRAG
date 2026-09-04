import asyncio

from app.core.embed.embedder import Embedder
from app.core.vector_store import VectorStore


class FakeEmbedModel:
    def embed(self, texts, query=False):
        for i in range(len(texts)):
            yield [float(i), 1.0]


def _row(cid, vec, start=0.0):
    return {
        "id": cid,
        "video_id": "v1",
        "content": f"content-{cid}",
        "embedding": vec,
        "start_sec": start,
        "end_sec": start + 1.0,
        "title": "t",
        "platform": "youtube",
    }


async def test_embedder_returns_vectors():
    e = Embedder(model=FakeEmbedModel())
    vecs = await e.embed_texts(["a", "b"])
    assert vecs == [[0.0, 1.0], [1.0, 1.0]]


class FakeRemoteEmbedResponse:
    def __init__(self, dims):
        self.data = [
            type("E", (), {"index": i, "embedding": [float(i), float(dims)]})()
            for i in range(dims)
        ]


class FakeRemoteEmbedClient:
    """OpenAI 兼容客户端 mock：embeddings 是 cached_property，create 是 async。"""

    def __init__(self):
        self.calls = []

    @property
    def embeddings(self):
        return self

    async def create(self, model, input):
        self.calls.append((model, input))
        n = len(input)
        return FakeRemoteEmbedResponse(n)


async def test_embedder_remote_openai():
    """provider=openai 走 /v1/embeddings（Ollama bge-m3 场景）。"""
    client = FakeRemoteEmbedClient()
    e = Embedder(
        model_name="bge-m3:latest",
        provider="openai",
        base_url="http://192.168.x.x:11434/v1",
        api_key="",
        client=client,
    )
    vecs = await e.embed_texts(["烧茄子", "向量检索"])
    assert len(vecs) == 2
    assert vecs[0] == [0.0, 2.0]  # index=0, dims=2
    assert vecs[1] == [1.0, 2.0]
    assert client.calls == [("bge-m3:latest", ["烧茄子", "向量检索"])]


def test_vector_store_add_and_search(tmp_path):
    vs = VectorStore(str(tmp_path))
    vs.add([_row("c1", [1, 0, 0, 0]), _row("c2", [0, 1, 0, 0])])
    hits = vs.search([1, 0, 0, 0], top_k=1)
    assert hits[0]["id"] == "c1"
    assert hits[0]["content"] == "content-c1"


def test_vector_store_append_to_existing(tmp_path):
    vs = VectorStore(str(tmp_path))
    vs.add([_row("c1", [1, 0, 0, 0])])
    vs.add([_row("c2", [0, 1, 0, 0])])
    assert len(vs.search([0, 1, 0, 0], top_k=10)) == 2


def test_vector_store_search_missing_table(tmp_path):
    vs = VectorStore(str(tmp_path))
    assert vs.search([1, 0, 0, 0]) == []


def test_vector_store_filter_by_video(tmp_path):
    vs = VectorStore(str(tmp_path))
    vs.add(
        [
            _row("c1", [1, 0, 0, 0]),
            _row("c2", [1, 0, 0, 0]),
        ]
    )
    hits = vs.search([1, 0, 0, 0], top_k=10, where="video_id = 'v1'")
    assert len(hits) == 2


def test_fts_chinese_ngram(tmp_path):
    vs = VectorStore(str(tmp_path))
    vs.add(
        [
            {
                "id": "c1",
                "video_id": "v1",
                "content": "向量数据库支持相似度检索",
                "embedding": [1, 0, 0, 0],
                "start_sec": 0.0,
                "end_sec": 1.0,
                "title": "t",
                "platform": "bilibili",
            },
            {
                "id": "c2",
                "video_id": "v1",
                "content": "Whisper 转写语音为文本",
                "embedding": [0, 1, 0, 0],
                "start_sec": 1.0,
                "end_sec": 2.0,
                "title": "t",
                "platform": "bilibili",
            },
        ]
    )
    vs.ensure_fts_index()
    hits = vs.search_text("向量数据库", top_k=2)
    assert len(hits) == 1
    assert hits[0]["id"] == "c1"
