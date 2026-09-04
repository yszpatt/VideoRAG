"""历史提问/检索记录（E5 / 设计文档 5.2c）。

- ``record_history``：去重（同 kind + 同 query 命中 → 更新 hit_count/last_used_at/
  answer/citations，不新增）+ 环形淘汰（每类保留 ``limit`` 条，超限删最旧）。
- 由 API handler 调用；写入失败由调用方 try/except 隔离（不影响主请求）。
"""

import logging
from datetime import timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import QueryHistory, new_id, utcnow

logger = logging.getLogger(__name__)

# query_key 唯一键的 query 截断长度（超长问题去重只取前缀）
_QUERY_KEY_CHARS = 500
# ask 答案只存前 2000 字（够历史回看，避免库膨胀）
ANSWER_STORE_CHARS = 2000


def _query_key(kind: str, query: str) -> str:
    return kind + "\x00" + query[:_QUERY_KEY_CHARS]


async def record_history(
    session_factory,
    kind: str,
    query: str,
    top_k: int | None = None,
    answer: str | None = None,
    citations: list[dict] | None = None,
    hits_count: int | None = None,
    limit: int = 200,
) -> None:
    """记录一次提问/检索。

    - 同 kind + 同 query 已存在：hit_count+1、更新 last_used_at 与结果字段；
    - 不存在：插入新行；
    - 写入后该类超限：同事务删除最旧（环形淘汰）。
    """
    q_key = _query_key(kind, query.strip())
    now = utcnow()
    async with session_factory() as s:
        existing = (
            await s.execute(
                select(QueryHistory).where(
                    QueryHistory.kind == kind,
                    QueryHistory.query_key == q_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.hit_count = (existing.hit_count or 1) + 1
            existing.last_used_at = now
            if answer is not None:
                existing.answer = answer[:ANSWER_STORE_CHARS]
                existing.citations_json = citations
            if hits_count is not None:
                existing.hits_count = hits_count
            if top_k is not None:
                existing.top_k = top_k
        else:
            s.add(
                QueryHistory(
                    id=new_id(),
                    kind=kind,
                    query=query.strip(),
                    query_key=q_key,
                    top_k=top_k,
                    answer=(answer or "")[:ANSWER_STORE_CHARS] or None,
                    citations_json=citations,
                    hits_count=hits_count,
                    hit_count=1,
                    last_used_at=now,
                )
            )
        await s.commit()
        await _trim(s, kind, limit)


async def _trim(s: AsyncSession, kind: str, limit: int) -> None:
    """同类超过 limit 条时删除最旧的（last_used_at 最早）。"""
    if limit <= 0:
        return
    total = (
        await s.execute(
            select(func.count()).select_from(QueryHistory).where(QueryHistory.kind == kind)
        )
    ).scalar_one()
    if total <= limit:
        return
    stale_ids = (
        await s.execute(
            select(QueryHistory.id)
            .where(QueryHistory.kind == kind)
            .order_by(QueryHistory.last_used_at.asc(), QueryHistory.created_at.asc())
            .limit(total - limit)
        )
    ).scalars().all()
    if stale_ids:
        await s.execute(delete(QueryHistory).where(QueryHistory.id.in_(stale_ids)))
        await s.commit()


# ---- 序列化（API 复用）----


def _iso(dt) -> str | None:
    """序列化为带显式 UTC 时区（Z）的 ISO 字符串。

    所有时间戳在写入时均为 UTC（``utcnow``），但 SQLite 的 DATETIME 列会
    以裸 ISO（无时区偏移）存储。前端 ``new Date(...)`` 会把无偏移的 ISO
    当作*本地*时间解析，造成 UTC+8 环境下约 8 小时的偏差（表现为
    “来自历史记录 8 小时前”）。统一补 ``Z`` 让前端按绝对 UTC 解析。
    """
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def serialize_history(h: QueryHistory, full: bool = False) -> dict:
    base = {
        "id": h.id,
        "kind": h.kind,
        "query": h.query,
        "top_k": h.top_k,
        "answer": h.answer,  # 列表用（已截断）；单条详情也可复用
        "hits_count": h.hits_count,
        "hit_count": h.hit_count,
        "last_used_at": _iso(h.last_used_at),
        "created_at": _iso(h.created_at),
    }
    if full:
        base["citations"] = h.citations_json or []
    return base
