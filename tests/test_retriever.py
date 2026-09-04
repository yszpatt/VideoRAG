import pytest
from app.core.rag.retriever import Hit, RetrievalParams, cap_meta, retrieve, rrf_merge


def _row(cid, text, start=0.0):
    return {
        "id": cid,
        "video_id": "v1",
        "content": text,
        "start_sec": start,
        "end_sec": start + 1.0,
        "title": "t",
        "platform": "youtube",
    }


def test_rrf_merges_ranked_lists():
    list_a = [_row("c1", "a"), _row("c2", "b"), _row("c3", "c")]
    list_b = [_row("c2", "b"), _row("c4", "d")]
    merged = rrf_merge([list_a, list_b], top_k=4)
    # c2 同时出现在两路，RRF 得分最高
    assert merged[0].chunk_id == "c2"
    assert {h.chunk_id for h in merged} == {"c2", "c1", "c3", "c4"}


def test_rrf_single_list_order_preserved():
    lst = [_row("c1", "a"), _row("c2", "b")]
    merged = rrf_merge([lst], top_k=10)
    assert [h.chunk_id for h in merged] == ["c1", "c2"]


async def test_retrieve_combines_vector_and_fts():
    class FakeEmbedder:
        async def embed_texts(self, texts, query=False):
            return [[1.0, 0.0]] * len(texts)

    class FakeStore:
        def search(self, qv, top_k=5, where=None):
            return [_row("c1", "向量命中", 0.0), _row("c2", "向量命中2", 10.0)]

        def search_text(self, text, top_k=5, where=None):
            return [_row("c2", "全文命中", 10.0), _row("c3", "全文命中2", 20.0)]

    hits = await retrieve("问题", FakeStore(), FakeEmbedder(), top_k=3)
    assert hits[0].chunk_id == "c2"  # 双路命中排最前
    assert len(hits) == 3


async def test_retrieve_handles_fts_empty():
    class FakeEmbedder:
        async def embed_texts(self, texts, query=False):
            return [[1.0, 0.0]] * len(texts)

    class FakeStore:
        def search(self, qv, top_k=5, where=None):
            return [_row("c1", "向量", 0.0)]

        def search_text(self, text, top_k=5, where=None):
            return []  # 表无 FTS 索引

    hits = await retrieve("问题", FakeStore(), FakeEmbedder(), top_k=3)
    assert [h.chunk_id for h in hits] == ["c1"]


def test_hit_has_time_and_title_fields():
    h = Hit(
        chunk_id="c1", video_id="v1", content="x", start_sec=1.5, end_sec=2.5,
        title="视频标题", score=0.9,
    )
    assert h.title == "视频标题"
    assert h.start_sec == 1.5
    assert h.kind == "content"  # 默认口播切片


def test_rrf_rows_without_kind_default_content():
    merged = rrf_merge([[_row("c1", "a")]], top_k=4)
    assert merged[0].kind == "content"


def _hit(cid, video_id, kind="content", score=0.5):
    return Hit(
        chunk_id=cid, video_id=video_id, content="x", start_sec=1.0, end_sec=2.0,
        title="t", score=score, kind=kind,
    )


def test_cap_meta_keeps_order_and_drops_excess():
    hits = [
        _hit("c1", "v1"),
        _hit("m1", "v1", kind="meta"),
        _hit("c2", "v2"),
        _hit("m2", "v2", kind="meta"),
        _hit("m3", "v3", kind="meta"),
    ]
    kept = cap_meta(hits, max_total=2)
    # 顺序保持；meta 每条视频最多 1 条、总量 ≤2 → 第 3 条 meta（m3）被裁
    assert [h.chunk_id for h in kept] == ["c1", "m1", "c2", "m2"]
    assert sum(1 for h in kept if h.kind == "meta") == 2


def test_cap_meta_dedupes_same_video():
    hits = [
        _hit("c1", "v1"),
        _hit("m1", "v1", kind="meta"),
        _hit("m2", "v1", kind="meta"),  # 同一视频第二条 meta → 丢弃
    ]
    kept = cap_meta(hits, max_total=4)
    assert [h.chunk_id for h in kept] == ["c1", "m1"]


def test_cap_meta_zero_drops_all_meta():
    hits = [_hit("c1", "v1"), _hit("m1", "v1", kind="meta")]
    assert [h.chunk_id for h in cap_meta(hits, max_total=0)] == ["c1"]


# ============ 检索策略优化：加权 RRF / 相似度门槛 / 邻居合并 / 单视频配额 ============


def test_clean_fts_query_strips_punctuation():
    from app.core.rag.retriever import clean_fts_query

    assert clean_fts_query("什么是RAG？原理，架构！") == "什么是RAG 原理 架构"
    assert clean_fts_query("  多  空格 \n 制表 \t") == "多 空格 制表"


def test_rrf_weighted_vector_dominates():
    """向量权重高时，向量路第 2 名应胜过全文路第 1 名（等权时会输）。"""
    vec = [_row("c1", "a"), _row("c2", "b")]
    fts = [_row("c3", "c")]
    # 等权：c3 与 c2 同 rank，但 c3 只有一路 → c2 双路仍更高？验证加权后 c2 > c3
    merged = rrf_merge([(vec, 0.7), (fts, 0.3)], top_k=3)
    ids = [h.chunk_id for h in merged]
    assert ids.index("c2") < ids.index("c3")  # 向量第2名(0.7权) 胜 全文第1名(0.3权)


def test_rrf_backward_compat_plain_lists():
    """不传权重的旧调用方式仍工作（等权合并）。"""
    merged = rrf_merge([[_row("c1", "a")], [_row("c2", "b")]], top_k=2)
    assert {h.chunk_id for h in merged} == {"c1", "c2"}


def _vrow(cid, text, dist, start=0.0, vid="v1"):
    """带 _distance 的向量命中行（LanceDB 真实返回形态）。"""
    return {
        "id": cid,
        "video_id": vid,
        "content": text,
        "start_sec": start,
        "end_sec": start + 1.0,
        "title": "t",
        "platform": "youtube",
        "_distance": dist,
    }


async def test_retrieve_min_sim_drops_weak_vector_only_hits():
    """纯向量命中且相似度低于门槛 → 丢弃；有全文佐证的保留。"""

    class FakeEmbedder:
        async def embed_texts(self, texts, query=False):
            return [[1.0, 0.0]] * len(texts)

    class FakeStore:
        def search(self, qv, top_k=5, where=None):
            # d=1.2 → sim = 1 - 1.44/2 = 0.28 < 0.3；d=0.6 → sim = 0.82
            return [_vrow("weak", "弱相关", 1.2), _vrow("strong", "强相关", 0.6)]

        def search_text(self, text, top_k=5, where=None):
            return [_row("lex", "词法命中", start=100.0)]  # 时间远离，不被邻居合并

    store = FakeStore()
    hits = await retrieve("问题", store, FakeEmbedder(), top_k=5,
                          params=RetrievalParams(min_sim=0.3))
    ids = {h.chunk_id for h in hits}
    assert "weak" not in ids  # 纯向量 + sim 0.28 < 0.3 → 丢弃
    assert {"strong", "lex"} <= ids


def test_dedup_neighbors_merges_adjacent():
    from app.core.rag.retriever import dedup_neighbors

    hits = [
        _hit("c1", "v1"),  # 1.0-2.0
        Hit(chunk_id="c2", video_id="v1", content="x", start_sec=2.5,
            end_sec=3.5, title="t", score=0.4),  # 间隔 0.5s ≤ gap → 合并
    ]
    kept = dedup_neighbors(hits, gap_sec=2.0)
    assert len(kept) == 1
    assert kept[0].chunk_id == "c1"
    assert kept[0].start_sec == 1.0 and kept[0].end_sec == 3.5  # 时间范围扩到并集


def test_dedup_neighbors_far_apart_kept():
    from app.core.rag.retriever import dedup_neighbors

    hits = [
        _hit("c1", "v1"),  # 1.0-2.0
        Hit(chunk_id="c2", video_id="v1", content="x", start_sec=10.0,
            end_sec=11.0, title="t", score=0.4),
    ]
    kept = dedup_neighbors(hits, gap_sec=2.0)
    assert len(kept) == 2


def test_dedup_neighbors_cross_video_not_merged():
    from app.core.rag.retriever import dedup_neighbors

    hits = [_hit("c1", "v1"), _hit("c2", "v2")]
    assert len(dedup_neighbors(hits, gap_sec=100.0)) == 2


def test_cap_per_video_limits_content_keeps_meta():
    from app.core.rag.retriever import cap_per_video

    hits = [
        _hit("c1", "v1"), _hit("c2", "v1"), _hit("c3", "v1"), _hit("c4", "v1"),
        _hit("m1", "v1", kind="meta"), _hit("c5", "v2"),
    ]
    kept = cap_per_video(hits, cap=3)
    ids = [h.chunk_id for h in kept]
    assert ids == ["c1", "c2", "c3", "m1", "c5"]  # v1 第4条 content 被裁，meta 不占额


async def test_retrieve_params_wired_end_to_end():
    """per_video_cap 经 params 生效：同视频超配额的命中被裁掉。"""

    class FakeEmbedder:
        async def embed_texts(self, texts, query=False):
            return [[1.0, 0.0]] * len(texts)

    class FakeStore:
        def search(self, qv, top_k=5, where=None):
            return [
                _vrow("c1", "a", 0.5, 0.0),
                _vrow("c2", "b", 0.6, 10.0),
                _vrow("c3", "c", 0.7, 20.0),
                _vrow("c4", "d", 0.8, 30.0),
            ]

        def search_text(self, text, top_k=5, where=None):
            return []

    hits = await retrieve("问题", FakeStore(), FakeEmbedder(), top_k=5,
                          params=RetrievalParams(per_video_cap=2))
    assert [h.chunk_id for h in hits] == ["c1", "c2"]  # c3/c4 超出单视频配额


# ============ 模型指纹校验接线：retrieve 前置拦截模型切换 ============


async def test_retrieve_checks_model_compat():
    """embedder 带 fingerprint、store 带 check_model_compat 时，检索前完成校验。"""

    class FakeEmbedder:
        fingerprint = {"provider": "openai", "model": "bge-m3:latest"}

        async def embed_texts(self, texts, query=False):
            return [[1.0, 0.0]] * len(texts)

    class FakeStore:
        checked = None

        def check_model_compat(self, fp):
            FakeStore.checked = fp

        def search(self, qv, top_k=5, where=None):
            return [_row("c1", "命中")]

        def search_text(self, text, top_k=5, where=None):
            return []

    hits = await retrieve("问题", FakeStore(), FakeEmbedder(), top_k=3)
    assert [h.chunk_id for h in hits] == ["c1"]
    # 校验收到了 embedder 指纹 + 查询向量维度
    assert FakeStore.checked == {
        "provider": "openai",
        "model": "bge-m3:latest",
        "dim": 2,
    }


async def test_retrieve_model_switch_raises_before_search():
    """模型已切换时，检索在向量搜索前就被拦截并抛明确错误。"""

    class FakeEmbedder:
        fingerprint = {"provider": "fastembed", "model": "new-model"}

        async def embed_texts(self, texts, query=False):
            return [[1.0, 0.0]]

    class FakeStore:
        searched = False

        def check_model_compat(self, fp):
            raise ValueError("embedding 模型已切换")

        def search(self, qv, top_k=5, where=None):
            FakeStore.searched = True
            return []

        def search_text(self, text, top_k=5, where=None):
            return []

    with pytest.raises(ValueError, match="模型已切换"):
        await retrieve("问题", FakeStore(), FakeEmbedder(), top_k=3)
    assert FakeStore.searched is False  # 未触达向量搜索
