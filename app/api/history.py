"""历史提问/检索 API（设计文档 E5 / 5.2b）。

- GET    /api/history?kind=ask&limit=50  按 last_used_at 倒序列表
- GET    /api/history/{id}                单条完整（含 citations）
- DELETE /api/history/{id}                删除单条
- DELETE /api/history?kind=ask            清空某类（不带 kind 清全部）
"""

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import delete, select

from app.core.history import serialize_history
from app.models import QueryHistory

router = APIRouter(prefix="/api/history")

MAX_LIST_LIMIT = 200


@router.get("")
async def list_history(request: Request, kind: str | None = None, limit: int = 20):
    limit = max(1, min(limit, MAX_LIST_LIMIT))
    sf = request.app.state.session_factory
    stmt = select(QueryHistory)
    if kind:
        stmt = stmt.where(QueryHistory.kind == kind)
    stmt = stmt.order_by(
        QueryHistory.last_used_at.desc(), QueryHistory.created_at.desc()
    ).limit(limit)
    async with sf() as s:
        rows = (await s.execute(stmt)).scalars().all()
    return {"items": [serialize_history(h) for h in rows]}


@router.get("/{history_id}")
async def get_history(history_id: str, request: Request):
    sf = request.app.state.session_factory
    async with sf() as s:
        h = await s.get(QueryHistory, history_id)
        if h is None:
            raise HTTPException(404, "history not found")
        return serialize_history(h, full=True)


@router.delete("/{history_id}")
async def delete_history(history_id: str, request: Request):
    sf = request.app.state.session_factory
    async with sf() as s:
        h = await s.get(QueryHistory, history_id)
        if h is None:
            raise HTTPException(404, "history not found")
        await s.delete(h)
        await s.commit()
    return {"deleted": history_id}


@router.delete("")
async def clear_history(request: Request, kind: str | None = None):
    sf = request.app.state.session_factory
    stmt = delete(QueryHistory)
    if kind:
        stmt = stmt.where(QueryHistory.kind == kind)
    async with sf() as s:
        result = await s.execute(stmt)
        await s.commit()
    return {"cleared": kind or "all", "rows": result.rowcount}
