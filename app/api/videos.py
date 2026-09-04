import time
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import delete, select

from app.core.fetchers.ytdlp import detect_platform
from app.core.notes import strip_quotes_section
from app.core.progress import progress_for_video
from app.core.video_service import SubmitError, submit_video_url
from app.models import Chunk, Comment, Note, Segment, Task, Video

router = APIRouter(prefix="/api/videos")

# thumbnail 代理（E1）：B 站图床校验 Referer，外链会 403；下载失败缓存 24h 不重试
THUMBNAIL_MISS_TTL = 24 * 3600
_THUMB_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
_PLATFORM_REFERER = {"bilibili": "https://www.bilibili.com"}


class SubmitVideoRequest(BaseModel):
    url: str


class SubmitVideoResponse(BaseModel):
    video_id: str
    status: str


def _serialize_video(v: Video, thumb_dir: Path | None) -> dict:
    return {
        "id": v.id,
        "platform": v.platform,
        "url": v.url,
        "title": v.title,
        "author": v.author,
        "duration_sec": v.duration_sec,
        "status": v.status,
        "error": v.error,
        "progress": progress_for_video(v),
        "created_at": v.created_at.isoformat() if v.created_at else None,
        # E1 元数据
        "description": v.description,
        "cover_url": v.cover_url,
        "upload_date": v.upload_date,
        "view_count": v.view_count,
        "like_count": v.like_count,
        "tags": v.tags,
        "comment_count": v.comment_count,
        "meta_source": v.meta_source,
        "has_thumbnail": bool(
            thumb_dir
            and (
                (thumb_dir / f"{v.id}.jpg").is_file() or (v.cover_url or "").strip()
            )
        ),
    }


def _thumb_dir(request: Request) -> Path:
    data_dir = request.app.state.components["settings"].data_dir
    return Path(data_dir) / "thumbnails"


@router.post("", response_model=SubmitVideoResponse)
async def submit_video(req: SubmitVideoRequest, request: Request):
    try:
        video_id, status = await submit_video_url(
            request.app.state.session_factory, request.app.state.queue, req.url
        )
    except SubmitError as e:
        raise HTTPException(400, str(e))
    return SubmitVideoResponse(video_id=video_id, status=status)


@router.get("")
async def list_videos(request: Request, limit: int = 50):
    sf = request.app.state.session_factory
    async with sf() as s:
        rows = (
            await s.execute(select(Video).order_by(Video.created_at.desc()).limit(limit))
        ).scalars().all()
    thumb_dir = _thumb_dir(request)
    return [_serialize_video(v, thumb_dir) for v in rows]


@router.get("/{video_id}")
async def get_video(video_id: str, request: Request):
    sf = request.app.state.session_factory
    thumb_dir = _thumb_dir(request)
    async with sf() as s:
        v = await s.get(Video, video_id)
        if v is None:
            raise HTTPException(404, "video not found")
        comments = (
            await s.execute(
                select(Comment)
                .where(Comment.video_id == video_id)
                .order_by(Comment.like_count.desc())
                .limit(5)
            )
        ).scalars().all()
        return {
            **_serialize_video(v, thumb_dir),
            "comments": [
                {
                    "author": c.author,
                    "text": c.text,
                    "like_count": c.like_count,
                    "published_at": c.published_at,
                }
                for c in comments
            ],
        }


@router.get("/{video_id}/note")
async def get_note(video_id: str, request: Request):
    sf = request.app.state.session_factory
    async with sf() as s:
        v = await s.get(Video, video_id)
        if v is None:
            raise HTTPException(404, "video not found")
        note = (
            await s.execute(select(Note).where(Note.video_id == video_id))
        ).scalar_one_or_none()
        if note is None:
            raise HTTPException(404, "note not ready")
        return {
            "summary": note.summary,
            "chapters": note.chapters or [],
            "key_points": note.key_points or [],
            "quotes": note.quotes or [],
            "glossary": note.glossary or [],
            "markdown": strip_quotes_section(note.markdown),
        }


@router.delete("/{video_id}")
async def delete_video(video_id: str, request: Request):
    """删除视频及其关联数据（笔记/字幕/转写/评论）、本地封面文件与向量分片。"""
    sf = request.app.state.session_factory
    thumb_dir = _thumb_dir(request)
    vs = request.app.state.components.get("vector_store")
    async with sf() as s:
        v = await s.get(Video, video_id)
        if v is None:
            raise HTTPException(404, "video not found")
        await s.execute(delete(Note).where(Note.video_id == video_id))
        await s.execute(delete(Segment).where(Segment.video_id == video_id))
        await s.execute(delete(Comment).where(Comment.video_id == video_id))
        await s.execute(delete(Chunk).where(Chunk.video_id == video_id))
        await s.execute(delete(Task).where(Task.video_id == video_id))
        await s.delete(v)
        await s.commit()
    # 清理本地封面与失败标记
    for suffix in ("", ".miss"):
        p = thumb_dir / f"{video_id}{suffix}"
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                pass
    # 清理向量库，避免检索残留已删除视频的分片
    if vs is not None:
        try:
            vs.delete_by_video_id(video_id)
        except Exception:
            pass
    return {"ok": True, "id": video_id}


@router.get("/{video_id}/thumbnail")
async def get_thumbnail(video_id: str, request: Request):
    """封面代理：本地缓存命中直接返回；未命中且 video 有 cover_url 时下载。

    前端 <img> 直连 B 站图床会因 Referer 校验 403，故走本端点代理。
    下载失败写 {id}.miss 标记，24h 内不再打外网；无封面/失败返回 404，
    前端显示平台色 SVG 占位。
    """
    thumb_dir = _thumb_dir(request)
    path = thumb_dir / f"{video_id}.jpg"
    if path.is_file():
        return FileResponse(path, media_type="image/jpeg")

    miss = thumb_dir / f"{video_id}.miss"
    if miss.is_file() and time.time() - miss.stat().st_mtime < THUMBNAIL_MISS_TTL:
        raise HTTPException(404, "thumbnail unavailable (cached miss)")

    sf = request.app.state.session_factory
    async with sf() as s:
        v = await s.get(Video, video_id)
    if v is None:
        raise HTTPException(404, "video not found")
    if not v.cover_url:
        raise HTTPException(404, "no cover")

    headers = {"User-Agent": _THUMB_UA}
    referer = _PLATFORM_REFERER.get(v.platform)
    if referer:
        headers["Referer"] = referer
    try:
        async with httpx.AsyncClient(timeout=10, headers=headers, follow_redirects=True) as c:
            resp = await c.get(v.cover_url)
            resp.raise_for_status()
            data = resp.content
    except Exception:
        thumb_dir.mkdir(parents=True, exist_ok=True)
        miss.write_text("", encoding="utf-8")  # 失败缓存 24h，避免列表反复打外网
        raise HTTPException(404, "cover download failed")

    thumb_dir.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return FileResponse(path, media_type="image/jpeg")


@router.get("/{video_id}/transcript")
async def get_transcript(video_id: str, request: Request):
    sf = request.app.state.session_factory
    async with sf() as s:
        v = await s.get(Video, video_id)
        if v is None:
            raise HTTPException(404, "video not found")
        segs = (
            await s.execute(
                select(Segment)
                .where(Segment.video_id == video_id)
                .order_by(Segment.start_sec)
            )
        ).scalars().all()
        return {
            "segments": [
                {"start_sec": g.start_sec, "end_sec": g.end_sec, "text": g.text}
                for g in segs
            ]
        }
