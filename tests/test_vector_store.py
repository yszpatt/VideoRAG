"""VectorStore kind 列迁移与 meta 分片基础能力测试（真实 LanceDB，临时目录）。"""

import pytest

from app.core.vector_store import KIND_CONTENT, KIND_META, VectorStore


def _row(cid, video_id="v1", text="口播内容测试", kind=None):
    r = {
        "id": cid,
        "video_id": video_id,
        "content": text,
        "embedding": [0.1, 0.2, 0.3, 0.4],
        "start_sec": 0.0,
        "end_sec": 1.0,
        "title": "标题",
        "platform": "bilibili",
    }
    if kind is not None:
        r["kind"] = kind
    return r


def test_ensure_kind_idempotent(tmp_path):
    vs = VectorStore(str(tmp_path / "lancedb"))
    vs.add([_row("c1")])  # 老数据：无 kind
    assert "kind" not in vs._get_db().open_table("chunks").schema.names
    assert vs.ensure_kind() is True  # 首次迁移
    assert vs.ensure_kind() is False  # 幂等
    rows = vs.search([0.1, 0.2, 0.3, 0.4], top_k=1)
    assert rows[0]["kind"] == KIND_CONTENT  # 老行回填 content


def test_add_auto_migrates_when_kind_rows_arrive(tmp_path):
    vs = VectorStore(str(tmp_path / "lancedb"))
    vs.add([_row("c1")])
    meta = _row("m1", text="视频简介：量子位专访独家报道", kind=KIND_META)
    meta["start_sec"] = 0.0
    meta["end_sec"] = 0.0
    vs.add([meta])  # 老表 + 带 kind 新行 → 自动迁移后追加
    assert vs.count_rows() == 2
    assert vs.count_rows("kind = 'meta'") == 1
    assert vs.has_kind_rows("v1", KIND_META) is True
    assert vs.has_kind_rows("v1", KIND_CONTENT) is True


def test_fts_searchable_after_migration(tmp_path):
    vs = VectorStore(str(tmp_path / "lancedb"))
    vs.add([_row("c1")])
    vs.ensure_kind()
    vs.ensure_fts_index()  # 迁移后索引需重建
    meta = _row("m1", text="视频简介：量子位专访独家报道", kind=KIND_META)
    meta["start_sec"] = 0.0
    meta["end_sec"] = 0.0
    vs.add([meta])
    hits = vs.search_text("量子位专访", top_k=5)
    assert hits and hits[0]["id"] == "m1"


def test_count_rows_missing_table_returns_zero(tmp_path):
    vs = VectorStore(str(tmp_path / "empty"))
    assert vs.count_rows() == 0
    assert vs.has_kind_rows("v1") is False


# ============ 向量维度校验：切换 embedding 模型时给出明确报错 ============


def test_search_dim_mismatch_raises_clear_error(tmp_path):
    """查询向量维度与库不一致 → 明确中文报错（而非 LanceDB 误导性错误）。"""
    vs = VectorStore(str(tmp_path / "lancedb"))
    vs.add([_row("c1")])  # 4 维
    with pytest.raises(ValueError, match="维度不匹配"):
        vs.search([0.1] * 8, top_k=1)  # 8 维查询


def test_add_dim_mismatch_raises_clear_error(tmp_path):
    """追加行维度与库不一致 → 写入前拦截，明确报错且不污染库。"""
    vs = VectorStore(str(tmp_path / "lancedb"))
    vs.add([_row("c1")])  # 4 维
    bad = _row("c2")
    bad["embedding"] = [0.5] * 3  # 3 维
    with pytest.raises(ValueError, match="维度不匹配"):
        vs.add([bad])
    assert vs.count_rows() == 1  # 坏行未写入


def test_search_missing_table_no_dim_error(tmp_path):
    """表不存在时检索静默返回空（不触发维度校验）。"""
    vs = VectorStore(str(tmp_path / "empty"))
    assert vs.search([0.1] * 4, top_k=1) == []


# ============ 模型指纹：防「同维度不同模型」静默失真 ============


def test_model_meta_first_check_adopts_fingerprint(tmp_path):
    """首次校验（老库无旁车文件）自动落盘当前指纹，平滑 adopting。"""
    vs = VectorStore(str(tmp_path / "lancedb"))
    vs.add([_row("c1")])
    assert vs.get_model_meta() is None
    fp = {"provider": "openai", "model": "bge-m3:latest", "dim": 4}
    vs.check_model_compat(fp)  # 不抛错
    assert vs.get_model_meta() == fp


def test_model_compat_same_model_passes(tmp_path):
    vs = VectorStore(str(tmp_path / "lancedb"))
    fp = {"provider": "openai", "model": "bge-m3:latest", "dim": 4}
    vs.check_model_compat(fp)
    vs.check_model_compat(fp)  # 同模型同维度 → 放行


def test_model_compat_same_dim_different_model_raises(tmp_path):
    """同维度不同模型：维度护栏拦不住的静默失真，指纹护栏必须拦截。"""
    vs = VectorStore(str(tmp_path / "lancedb"))
    vs.check_model_compat({"provider": "openai", "model": "bge-m3:latest", "dim": 4})
    with pytest.raises(ValueError, match="embedding 模型已切换"):
        vs.check_model_compat(
            {"provider": "fastembed", "model": "BAAI/bge-small-zh-v1.5", "dim": 4}
        )


def test_model_compat_dim_change_raises(tmp_path):
    vs = VectorStore(str(tmp_path / "lancedb"))
    vs.check_model_compat({"provider": "openai", "model": "bge-m3:latest", "dim": 4})
    with pytest.raises(ValueError, match="embedding 模型已切换"):
        vs.check_model_compat({"provider": "openai", "model": "bge-m3:latest", "dim": 8})


def test_drop_table_for_rebuild(tmp_path):
    """重建脚本依赖的 drop_table：删表后 count 归零、可重新建表。"""
    vs = VectorStore(str(tmp_path / "lancedb"))
    vs.add([_row("c1")])
    vs.drop_table()
    assert vs.count_rows() == 0
    vs.add([_row("c2")])  # 重建后可正常写入
    assert vs.count_rows() == 1
