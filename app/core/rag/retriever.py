from dataclasses import dataclass

# 兼容旧引用的默认 RRF 平滑常数；实际以 RetrievalParams.rrf_k 为准
RRF_K = 60

# 常见中英文标点：FTS 查询前替换为空格，避免 ngram 分词产生噪声 token
_PUNCT_CHARS = "，。！？；：、()（）[]【】{}<>《》“”‘’…—·!?;:,.()\n\t\r\"'"
_PUNCT = str.maketrans({c: " " for c in _PUNCT_CHARS})


@dataclass
class RetrievalParams:
    """检索策略参数（设置页「检索」组在线可调，保存后下一次请求即生效）。

    流水线：宽召回（向量 vector_k + 全文 fts_k）
          → 加权 RRF 融合（vector_weight / fts_weight）
          → 相关性过滤（min_sim：仅丢弃「纯向量命中且相似度低于门槛」的结果，
             有全文命中的结果视为有词法佐证，不因向量相似度低而丢弃）
          → 相邻切片合并（neighbor_gap：同视频时间重叠/相邻的切片只留排名最高一条，
             并把其时间范围扩到并集，引用时覆盖完整段落）
          → 单视频配额（per_video_cap：防止单视频霸榜）
          → meta 护栏（cap_meta）→ top_k 截断。
    """

    vector_k: int = 24          # 向量召回条数
    fts_k: int = 24             # 全文召回条数
    rrf_k: int = 60             # RRF 平滑常数，越大名次差异越平缓
    vector_weight: float = 0.7  # 向量路权重（语义相关性为主）
    fts_weight: float = 0.3     # 全文路权重（关键词/术语佐证）
    per_video_cap: int = 3      # 单视频最多占的结果位数（0 = 不限）
    min_sim: float = 0.0        # 向量路最低余弦相似度门槛（0 = 关闭；建议 0.3）
    neighbor_gap: float = 2.0   # 相邻切片合并窗口（秒）；0 = 关闭


@dataclass
class Hit:
    chunk_id: str
    video_id: str
    content: str
    start_sec: float
    end_sec: float
    title: str
    score: float
    platform: str = ""
    kind: str = "content"  # content=口播切片 | meta=视频级简介分片


def _to_hit(row: dict) -> Hit:
    return Hit(
        chunk_id=row.get("id", ""),
        video_id=row.get("video_id", ""),
        content=row.get("content", ""),
        start_sec=float(row.get("start_sec", 0) or 0),
        end_sec=float(row.get("end_sec", 0) or 0),
        title=row.get("title", "") or "",
        score=0.0,
        platform=row.get("platform", "") or "",
        kind=row.get("kind", "content") or "content",
    )


def _vec_sim(row: dict) -> float | None:
    """LanceDB L2 距离还原余弦相似度（归一化向量下 cos = 1 - d²/2）。

    bge 系列向量均归一化，该换算可靠；行里没有 _distance（如 FTS 行）返回 None。
    """
    d = row.get("_distance")
    if d is None:
        return None
    try:
        d = float(d)
    except (TypeError, ValueError):
        return None
    return max(0.0, 1.0 - d * d / 2.0)


def clean_fts_query(text: str) -> str:
    """FTS 查询预处理：剥离标点/多余空白，保留纯文本给 ngram 分词。"""
    return " ".join(text.translate(_PUNCT).split())


def cap_meta(hits: list[Hit], max_total: int = 2) -> list[Hit]:
    """meta（视频简介）命中护栏：每条视频最多 1 条、总量不超过 max_total。

    简介是发布者自述的营销文案，相关性可能虚高；限制其数量避免挤占真正
    讲内容的带时间戳口播切片。保持原始排序，被裁掉的 meta 直接丢弃。
    """
    if max_total <= 0:
        return [h for h in hits if h.kind != "meta"]
    seen: set[str] = set()
    kept: list[Hit] = []
    n = 0
    for h in hits:
        if h.kind == "meta":
            if h.video_id in seen or n >= max_total:
                continue
            seen.add(h.video_id)
            n += 1
        kept.append(h)
    return kept


def cap_per_video(hits: list[Hit], cap: int = 3) -> list[Hit]:
    """单视频配额：content 切片每视频最多 cap 条（meta 不计），保证结果多样性。"""
    if cap <= 0:
        return hits
    counts: dict[str, int] = {}
    kept: list[Hit] = []
    for h in hits:
        if h.kind == "content":
            n = counts.get(h.video_id, 0)
            if n >= cap:
                continue
            counts[h.video_id] = n + 1
        kept.append(h)
    return kept


def dedup_neighbors(hits: list[Hit], gap_sec: float = 2.0) -> list[Hit]:
    """相邻切片去重：同视频时间重叠或间隔 ≤ gap_sec 的切片只留排名最高一条。

    切片间自带 50 字符重叠，同一句话常命中多个相邻切片挤占结果位。
    被合并切片的时间范围并入保留切片（引用时可覆盖完整段落）。
    """
    if gap_sec <= 0:
        return hits
    kept: list[Hit] = []
    for h in hits:
        if h.kind != "content":
            kept.append(h)
            continue
        dup = next(
            (
                k
                for k in kept
                if k.kind == "content"
                and k.video_id == h.video_id
                and h.start_sec <= k.end_sec + gap_sec
                and h.end_sec >= k.start_sec - gap_sec
            ),
            None,
        )
        if dup is not None:
            dup.start_sec = min(dup.start_sec, h.start_sec)
            dup.end_sec = max(dup.end_sec, h.end_sec)
        else:
            kept.append(h)
    return kept


def rrf_merge(
    ranked_lists: list, top_k: int = 8, rrf_k: int = RRF_K
) -> list[Hit]:
    """Reciprocal Rank Fusion：多路检索结果合并。

    ranked_lists 的元素可以是行列表（权重 1.0），也可以是 (行列表, 权重) 元组；
    加权 RRF 得分 = Σ weight / (rrf_k + rank + 1)。
    """
    scores: dict[str, float] = {}
    rows: dict[str, dict] = {}
    for item in ranked_lists:
        lst, weight = item if isinstance(item, tuple) else (item, 1.0)
        for rank, row in enumerate(lst):
            key = row.get("id", "")
            if not key:
                continue
            scores[key] = scores.get(key, 0.0) + weight / (rrf_k + rank + 1)
            rows[key] = row
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
    hits = []
    for key, score in ranked:
        h = _to_hit(rows[key])
        h.score = score
        hits.append(h)
    return hits


async def retrieve(
    query: str,
    vector_store,
    embedder,
    top_k: int = 8,
    vector_k: int = 16,
    fts_k: int = 16,
    params: RetrievalParams | None = None,
) -> list[Hit]:
    """混合检索：宽召回 → 加权 RRF → 相关性过滤 → 去重/配额 → meta 护栏。

    兼容旧签名：不传 params 时用 vector_k/fts_k 旧参数构造（其余用默认策略）。
    """
    if params is None:
        params = RetrievalParams(vector_k=vector_k, fts_k=fts_k)

    qv = (await embedder.embed_texts([query], query=True))[0]

    # 模型指纹校验：embedding 模型已切换（含同维度不同模型）时直接报错，
    # 避免「检索静默变差」。FakeStore 等测试替身无该方法时跳过。
    checker = getattr(vector_store, "check_model_compat", None)
    fp = getattr(embedder, "fingerprint", None)
    if checker is not None and fp:
        checker({**fp, "dim": len(qv)})

    vec_hits = vector_store.search(qv, top_k=params.vector_k)

    fts_hits: list[dict] = []
    try:
        fts_hits = vector_store.search_text(
            clean_fts_query(query), top_k=params.fts_k
        )
    except Exception:
        fts_hits = []  # 无 FTS 索引或全文检索不可用时降级为纯向量

    # 溯源信息：每个 key 的向量相似度、是否被全文路命中（词法佐证）
    vec_keys = {r.get("id", "") for r in vec_hits if r.get("id")}
    fts_keys = {r.get("id", "") for r in fts_hits if r.get("id")}
    sims = {r.get("id", ""): _vec_sim(r) for r in vec_hits if r.get("id")}

    # 融合候选池取 top_k 的 3 倍，给后续过滤/去重留余量
    pool = max(top_k * 3, top_k)
    merged = rrf_merge(
        [(vec_hits, params.vector_weight), (fts_hits, params.fts_weight)],
        top_k=pool,
        rrf_k=params.rrf_k,
    )

    # 相关性过滤：纯向量命中（无词法佐证）且相似度低于门槛 → 丢弃
    if params.min_sim > 0:
        merged = [
            h
            for h in merged
            if not (
                h.chunk_id in sims
                and h.chunk_id not in fts_keys
                and sims[h.chunk_id] is not None
                and sims[h.chunk_id] < params.min_sim
            )
        ]

    merged = dedup_neighbors(merged, gap_sec=params.neighbor_gap)
    merged = cap_per_video(merged, cap=params.per_video_cap)
    # meta（简介）护栏：每视频 ≤1 条、总量 ≤ top_k//4（至少 1），保持排序稳定。
    merged = cap_meta(merged, max_total=max(1, top_k // 4))
    return merged[:top_k]
