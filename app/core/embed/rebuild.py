"""向量库重建（re-embed）：切换 embedding 模型后以当前模型重嵌入全部切片。

场景：更换 embedding 模型（如 bge-small-zh → bge-m3）后，向量库里的旧向量
与新模型不在同一语义空间——维度不同会直接报错（VectorStoreIncompatible），
同维度不同模型则会静默失真（模型指纹护栏会拦截并提示重建）。

原理：切片原文都保存在 SQLite chunks 表，无需重跑转写/笔记，只需：
  1. load_rows 读出全部 chunks（含 kind=meta 的简介分片，title/platform 从 meta JSON 恢复）
  2. reembed 用新模型批量重嵌入
  3. drop LanceDB 表 → 按原 id 重建（lancedb_id 与 SQLite 保持一致，无需改库）
  4. 重建 FTS 索引 + 落盘新模型指纹（model.meta.json）

fail-safe 顺序：先分批把全部向量算完（此阶段任何失败都不动旧库），
成功后才 drop → add → FTS → 指纹落盘；避免「表已删、embedding 中途失败」
导致旧向量丢失。

入口：
- CLI：scripts/reembed_vector_store.py（交互确认 / --yes）
- Web：「设置 → 本地模型 → 重建知识库」（POST /api/vectorstore/rebuild）
- 库调用：from app.core.embed.rebuild import load_rows, reembed
"""
from __future__ import annotations

import json
import sqlite3

from app.core.embed.embedder import Embedder
from app.core.vector_store import VectorStore


def load_rows(db_path: str) -> list[dict]:
    """从 SQLite chunks 表读出全部待重嵌入切片。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, video_id, content, start_sec, end_sec, kind, meta"
            " FROM chunks ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        meta = {}
        if r["meta"]:
            try:
                meta = json.loads(r["meta"]) if isinstance(r["meta"], str) else r["meta"]
            except (ValueError, TypeError):
                meta = {}
        out.append(
            {
                "id": r["id"],
                "video_id": r["video_id"],
                "content": r["content"],
                "start_sec": r["start_sec"] or 0.0,
                "end_sec": r["end_sec"] or 0.0,
                "kind": r["kind"] or "content",
                "title": meta.get("title") or "",
                "platform": meta.get("platform") or "",
            }
        )
    return out


async def reembed(
    rows: list[dict],
    embedder: Embedder,
    vector_store: VectorStore,
    batch_size: int = 32,
    progress=None,
) -> int:
    """重嵌入并重建 LanceDB 表（fail-safe 顺序：先全部嵌入，成功后才 drop 重建）。

    先分批把全部向量算完（此阶段任何失败都不动旧库），再 drop → add（原 id）
    → FTS → 指纹落盘。避免「表已删、embedding 中途失败」导致旧向量丢失。
    progress 为可选回调 progress(done, total)，供 CLI / API 显示进度。
    返回重建的切片数。空库（0 条切片）也会更新指纹。
    """
    if not rows:
        fp = getattr(embedder, "fingerprint", None)
        if fp:
            vector_store.set_model_meta(dict(fp))
        return 0

    # 阶段 1：全部嵌入（不动旧库；失败即中止，数据无损）
    embedded: list[tuple[dict, list[float]]] = []
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        vectors = await embedder.embed_texts([r["content"] for r in batch])
        embedded.extend(zip(batch, vectors))
        if progress:
            progress(len(embedded), len(rows))

    dim = len(embedded[0][1])

    # 阶段 2：嵌入全部成功后才重建（drop → 写入原 id → FTS → 指纹）
    vector_store.drop_table()
    for i in range(0, len(embedded), batch_size):
        vector_store.add([{**r, "embedding": v} for r, v in embedded[i : i + batch_size]])
    vector_store.ensure_fts_index()
    fp = getattr(embedder, "fingerprint", None)
    if fp:
        vector_store.set_model_meta({**fp, "dim": dim})
    return len(rows)
