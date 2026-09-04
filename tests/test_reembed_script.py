"""reembed_vector_store.py 重建脚本测试：load_rows / reembed 核心逻辑。"""

import asyncio
import importlib.util
import json
import sqlite3
from pathlib import Path

from app.core.vector_store import VectorStore

_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "reembed_vector_store.py"
)
_spec = importlib.util.spec_from_file_location("reembed_script", _SCRIPT)
reembed_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reembed_mod)


class FakeEmbedder:
    """固定 4 维向量的测试替身，模拟切换后的新模型。"""

    fingerprint = {"provider": "openai", "model": "fake-new-model"}

    async def embed_texts(self, texts, query=False):
        return [[0.1, 0.2, 0.3, 0.4]] * len(texts)


def _seed_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE chunks (id TEXT PRIMARY KEY, video_id TEXT, content TEXT,"
        " start_sec REAL, end_sec REAL, kind TEXT, meta TEXT)"
    )
    conn.execute(
        "INSERT INTO chunks VALUES ('c1', 'v1', '口播内容一', 0.0, 10.0, 'content', ?)",
        (json.dumps({"title": "标题A", "platform": "bilibili", "kind": "content"}),),
    )
    conn.execute(
        "INSERT INTO chunks VALUES ('m1', 'v1', '视频标题：标题A', 0.0, 0.0, 'meta', ?)",
        (json.dumps({"title": "标题A", "platform": "bilibili", "kind": "meta"}),),
    )
    conn.commit()
    conn.close()


def test_load_rows_parses_meta_json(tmp_path):
    db = tmp_path / "db.sqlite"
    _seed_db(str(db))
    rows = reembed_mod.load_rows(str(db))
    assert [r["id"] for r in rows] == ["c1", "m1"]
    assert rows[0]["title"] == "标题A"
    assert rows[0]["platform"] == "bilibili"
    assert rows[1]["kind"] == "meta"


def test_reembed_rebuilds_table_keeps_ids_and_writes_fingerprint(tmp_path):
    db = tmp_path / "db.sqlite"
    _seed_db(str(db))
    vs = VectorStore(str(tmp_path / "lancedb"))
    # 模拟旧模型的 2 维向量数据（与新的 4 维不同）
    vs.add(
        [
            {
                "id": "old",
                "video_id": "v0",
                "content": "旧",
                "embedding": [0.5, 0.5],
                "start_sec": 0.0,
                "end_sec": 1.0,
                "title": "旧",
                "platform": "x",
            }
        ]
    )

    rows = reembed_mod.load_rows(str(db))
    n = asyncio.run(reembed_mod.reembed(rows, FakeEmbedder(), vs))

    assert n == 2
    assert vs.count_rows() == 2  # 旧数据被 drop，只余重建数据
    hits = vs.search([0.1, 0.2, 0.3, 0.4], top_k=5)
    assert {h["id"] for h in hits} == {"c1", "m1"}  # 原 id 保留
    assert vs.get_model_meta() == {
        "provider": "openai",
        "model": "fake-new-model",
        "dim": 4,
    }


def test_reembed_fts_index_searchable_after_rebuild(tmp_path):
    db = tmp_path / "db.sqlite"
    _seed_db(str(db))
    vs = VectorStore(str(tmp_path / "lancedb"))
    rows = reembed_mod.load_rows(str(db))
    asyncio.run(reembed_mod.reembed(rows, FakeEmbedder(), vs))
    hits = vs.search_text("口播内容", top_k=5)
    assert hits and hits[0]["id"] == "c1"


def test_reembed_embed_failure_keeps_old_table_intact(tmp_path):
    """fail-safe：嵌入阶段失败时旧向量库不被 drop，数据无损。"""
    db = tmp_path / "db.sqlite"
    _seed_db(str(db))
    vs = VectorStore(str(tmp_path / "lancedb"))
    vs.add(
        [
            {
                "id": "old",
                "video_id": "v0",
                "content": "旧",
                "embedding": [0.5, 0.5],
                "start_sec": 0.0,
                "end_sec": 1.0,
                "title": "旧",
                "platform": "x",
            }
        ]
    )

    class BrokenEmbedder:
        fingerprint = {"provider": "openai", "model": "broken"}

        async def embed_texts(self, texts, query=False):
            raise RuntimeError("embedding 服务不可用")

    rows = reembed_mod.load_rows(str(db))
    try:
        asyncio.run(reembed_mod.reembed(rows, BrokenEmbedder(), vs))
        raised = False
    except RuntimeError:
        raised = True
    assert raised
    # 旧库原样保留
    assert vs.count_rows() == 1
    assert vs.search([0.5, 0.5], top_k=1)[0]["id"] == "old"
    assert vs.get_model_meta() is None  # 失败时未落盘新指纹
