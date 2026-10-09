"""视频收藏夹 API（「视频收藏」页侧边栏）。

- GET    /api/collections             列表（含每个夹的视频数）
- POST   /api/collections             新建 {name}
- PATCH  /api/collections/{id}        重命名 {name}
- DELETE /api/collections/{id}        删除（默认收藏夹不可删）

归属关系在 ``PUT /api/videos/{video_id}/collections``（见 app/api/videos.py）。

语义（与启动时的孤儿修复一致，见 app/db.py）：
**默认收藏夹 = 自动归属、不可取消** —— 新导入的视频自动进它，用户只能再往其它
收藏夹里加，不能把它移出去。这样每个视频总有归处，也不会出现「全部里有、却不在
任何收藏夹里」的幽灵条目。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models import Collection, Video, VideoCollection

router = APIRouter(prefix="/api/collections")

MAX_NAME_CHARS = 40


class NameBody(BaseModel):
    name: str


def _clean_name(raw: str) -> str:
    name = (raw or "").strip()
    if not name:
        raise HTTPException(422, "收藏夹名称不能为空")
    if len(name) > MAX_NAME_CHARS:
        raise HTTPException(422, f"收藏夹名称最长 {MAX_NAME_CHARS} 个字符")
    return name


def _serialize(coll: Collection, count: int) -> dict:
    return {
        "id": coll.id,
        "name": coll.name,
        "is_default": bool(coll.is_default),
        "count": count,
        "created_at": coll.created_at.isoformat() if coll.created_at else None,
    }


async def _counts(session) -> dict[str, int]:
    rows = await session.execute(
        select(VideoCollection.collection_id, func.count()).group_by(
            VideoCollection.collection_id
        )
    )
    return {cid: n for cid, n in rows.all()}


@router.get("")
async def list_collections(request: Request):
    """收藏夹列表 + 视频总数（前端「全部」用）。默认夹排最前。"""
    sf = request.app.state.session_factory
    async with sf() as s:
        rows = (
            await s.execute(
                select(Collection).order_by(
                    Collection.is_default.desc(), Collection.created_at
                )
            )
        ).scalars().all()
        counts = await _counts(s)
        total = (await s.execute(select(func.count()).select_from(Video))).scalar_one()
    return {
        "collections": [_serialize(c, counts.get(c.id, 0)) for c in rows],
        "total_videos": total,
    }


@router.post("")
async def create_collection(req: NameBody, request: Request):
    name = _clean_name(req.name)
    sf = request.app.state.session_factory
    async with sf() as s:
        exists = (
            await s.execute(select(Collection).where(Collection.name == name))
        ).scalars().first()
        if exists is not None:
            raise HTTPException(409, f"已存在同名收藏夹：{name}")
        coll = Collection(name=name)
        s.add(coll)
        try:
            await s.commit()
        except IntegrityError as e:  # 并发下唯一约束兜底
            await s.rollback()
            raise HTTPException(409, f"已存在同名收藏夹：{name}") from e
        return {"collection": _serialize(coll, 0)}


@router.patch("/{collection_id}")
async def rename_collection(collection_id: str, req: NameBody, request: Request):
    name = _clean_name(req.name)
    sf = request.app.state.session_factory
    async with sf() as s:
        coll = await s.get(Collection, collection_id)
        if coll is None:
            raise HTTPException(404, "收藏夹不存在")
        clash = (
            await s.execute(
                select(Collection).where(Collection.name == name, Collection.id != coll.id)
            )
        ).scalars().first()
        if clash is not None:
            raise HTTPException(409, f"已存在同名收藏夹：{name}")
        coll.name = name
        await s.commit()
        counts = await _counts(s)
        return {"collection": _serialize(coll, counts.get(coll.id, 0))}


@router.delete("/{collection_id}")
async def delete_collection(collection_id: str, request: Request):
    """删除收藏夹（只删归属关系，视频本身与其它收藏夹不受影响）。"""
    sf = request.app.state.session_factory
    async with sf() as s:
        coll = await s.get(Collection, collection_id)
        if coll is None:
            raise HTTPException(404, "收藏夹不存在")
        if coll.is_default:
            raise HTTPException(400, "默认收藏夹不可删除（新导入的视频自动进这里）")
        result = await s.execute(
            VideoCollection.__table__.delete().where(
                VideoCollection.collection_id == collection_id
            )
        )
        removed = result.rowcount or 0
        await s.delete(coll)
        await s.commit()
    return {"ok": True, "id": collection_id, "removed_links": removed}
