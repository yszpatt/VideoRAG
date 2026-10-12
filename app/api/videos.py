import asyncio
import shutil
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import noload
from starlette.concurrency import run_in_threadpool

from app.core.notes import strip_quotes_section
from app.core.progress import progress_for_video
from app.core.video_service import (
    SubmitError,
    submit_local_video,
    submit_video_url,
)
from app.models import (
    Chunk,
    Collection,
    Comment,
    Note,
    Segment,
    Task,
    Video,
    VideoCollection,
    new_id,
)

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


def _serialize_video(
    v: Video,
    thumb_dir: Path | None,
    *,
    has_segments: bool | None = None,
    has_note: bool | None = None,
    has_chunks: bool | None = None,
    collection_ids: list[str] | None = None,
) -> dict:
    return {
        # 所属收藏夹（多对多；默认收藏夹始终在内 —— 见 /api/collections 的语义说明）
        "collection_ids": collection_ids or [],
        "id": v.id,
        "platform": v.platform,
        "url": v.url,
        "title": v.title,
        "author": v.author,
        "duration_sec": v.duration_sec,
        "status": v.status,
        "error": v.error,
        "progress": progress_for_video(
            v, has_segments=has_segments, has_note=has_note, has_chunks=has_chunks
        ),
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


# 列表/详情查询显式关闭的重关联：这些关联默认 lazy="selectin"，加载 Video 时会
# 顺带把每个视频的逐句转写（segments）、切片正文（chunks）、笔记 markdown（notes）、
# 任务（tasks）全部拉进内存——列表页完全用不到，且前端每 3s 轮询一次，长视频多时
# 是持续的内存与 I/O 浪费。进度推断所需的「是否存在」改由 _failed_flags 轻量查询。
_HEAVY_RELATIONS = ("tasks", "segments", "chunks", "note")


async def _failed_flags(session, videos: list[Video]) -> dict[str, dict]:
    """为 failed 视频批量取 has_segments/has_note/has_chunks（只 count 不取全文）。

    非 failed 视频不需要这些信息（进度由 status 决定），跳过。返回
    {video_id: {"has_segments": bool, "has_note": bool, "has_chunks": bool}}，
    未命中的视频由调用方以全 False 兜底。
    """
    failed_ids = [v.id for v in videos if v.status == "failed"]
    if not failed_ids:
        return {}
    flags: dict[str, dict] = {
        vid: {"has_segments": False, "has_note": False, "has_chunks": False}
        for vid in failed_ids
    }
    for model, key in ((Segment, "has_segments"), (Chunk, "has_chunks"), (Note, "has_note")):
        rows = await session.execute(
            select(model.video_id)
            .where(model.video_id.in_(failed_ids))
            .group_by(model.video_id)
        )
        for vid in rows.scalars():
            flags[vid][key] = True
    return flags


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


# ---- 本地导入：上传视频/音频文件 ----

# 允许的扩展名：视频为主，顺带支持纯音频（本地播客/录音同样能进知识库）
VIDEO_EXTS = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".m4v", ".ts", ".mpg", ".mpeg", ".wmv",
}
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".opus", ".wma"}
UPLOAD_SUBDIR = "uploads"
# 单文件上限：本地视频常见几百 MB~数 GB；超过时明确报错而不是写爆磁盘
MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024
_CHUNK = 1024 * 1024


class UploadVideoResponse(BaseModel):
    video_id: str
    status: str
    title: str
    local_path: str
    duration_sec: float | None = None


async def _probe_duration(path: Path) -> float | None:
    """ffprobe 取时长（失败/未装返回 None）：只影响展示与笔记，绝不阻断导入。"""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        return float(out.decode().strip())
    except Exception:  # noqa: BLE001  解析失败只记 None
        return None


@router.post("/upload", response_model=UploadVideoResponse)
async def upload_local_video(request: Request, filename: str = Query(..., max_length=300)):
    """上传本地视频/音频并入库（请求体是**原始字节流**，不是 multipart）。

    为什么不用 multipart：本地视频动辄几百 MB~GB，multipart 既需要额外依赖（python-multipart）
    又会多包一层；这里直接 `request.stream()` 分块落盘，内存占用与文件大小无关。
    文件名走 query（只用于取扩展名与标题），落盘名用 video_id，避免重名/路径穿越。
    """
    settings = request.app.state.components["settings"]
    name = Path(filename or "").name  # 去掉任何目录部分，防路径穿越
    ext = Path(name).suffix.lower()
    if ext not in VIDEO_EXTS | AUDIO_EXTS:
        raise HTTPException(
            422,
            f"不支持的文件类型 {ext or '(无扩展名)'}；"
            f"支持视频 {', '.join(sorted(VIDEO_EXTS))} 与音频 {', '.join(sorted(AUDIO_EXTS))}",
        )

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"文件超过 {MAX_UPLOAD_BYTES // (1024**3)}GB 上限")

    upload_dir = Path(settings.data_dir) / UPLOAD_SUBDIR
    upload_dir.mkdir(parents=True, exist_ok=True)
    video_id = new_id()
    dest = upload_dir / f"{video_id}{ext}"
    rel_path = f"{UPLOAD_SUBDIR}/{dest.name}"

    written = 0
    try:
        with open(dest, "wb") as f:
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:  # 分块过程中兜底（Content-Length 可能缺失）
                    raise HTTPException(413, f"文件超过 {MAX_UPLOAD_BYTES // (1024**3)}GB 上限")
                # 写盘放到线程池：避免阻塞事件循环（每块 1MB，开销可忽略）
                await run_in_threadpool(f.write, chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except Exception as e:  # noqa: BLE001  磁盘满/客户端中断：清掉半截文件再报错
        dest.unlink(missing_ok=True)
        raise HTTPException(500, f"写入上传文件失败：{e}") from e

    if written == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(422, "上传内容为空")

    duration = await _probe_duration(dest)
    try:
        vid, status = await submit_local_video(
            request.app.state.session_factory,
            request.app.state.queue,
            video_id=video_id,
            filename=name,
            local_path=rel_path,
            title=Path(name).stem,
            duration_sec=duration,
        )
    except SubmitError as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, str(e)) from e

    return UploadVideoResponse(
        video_id=vid, status=status, title=Path(name).stem,
        local_path=rel_path, duration_sec=duration,
    )

async def _collection_map(session, video_ids: list[str]) -> dict[str, list[str]]:
    """批量取「视频 → 收藏夹 id 列表」，避免逐条查询。"""
    if not video_ids:
        return {}
    rows = await session.execute(
        select(VideoCollection.video_id, VideoCollection.collection_id).where(
            VideoCollection.video_id.in_(video_ids)
        )
    )
    out: dict[str, list[str]] = {}
    for vid, cid in rows.all():
        out.setdefault(vid, []).append(cid)
    return out


@router.get("")
async def list_videos(request: Request, limit: int = 50, collection_id: str | None = None):
    """视频列表；``collection_id`` 给出时只返回该收藏夹内的视频。

    过滤放在服务端：前端只按 3s 轮询拉一页（limit 默认 50），如果改成前端过滤，
    收藏夹里超过一页的视频会「查不全」。
    """
    sf = request.app.state.session_factory
    async with sf() as s:
        stmt = (
            select(Video)
            .options(*[noload(getattr(Video, r)) for r in _HEAVY_RELATIONS])
            .order_by(Video.created_at.desc())
            .limit(limit)
        )
        if collection_id:
            stmt = stmt.where(
                Video.id.in_(
                    select(VideoCollection.video_id).where(
                        VideoCollection.collection_id == collection_id
                    )
                )
            )
        rows = (await s.execute(stmt)).scalars().all()
        flags = await _failed_flags(s, rows)
        cmap = await _collection_map(s, [v.id for v in rows])
    thumb_dir = _thumb_dir(request)
    return [
        _serialize_video(
            v, thumb_dir, collection_ids=cmap.get(v.id, []), **flags.get(v.id, {})
        )
        for v in rows
    ]


@router.get("/{video_id}")
async def get_video(video_id: str, request: Request):
    sf = request.app.state.session_factory
    thumb_dir = _thumb_dir(request)
    async with sf() as s:
        v = (
            await s.execute(
                select(Video)
                .options(*[noload(getattr(Video, r)) for r in _HEAVY_RELATIONS])
                .where(Video.id == video_id)
            )
        ).scalar_one_or_none()
        if v is None:
            raise HTTPException(404, "video not found")
        flags = await _failed_flags(s, [v])
        cmap = await _collection_map(s, [v.id])
        comments = (
            await s.execute(
                select(Comment)
                .where(Comment.video_id == video_id)
                .order_by(Comment.like_count.desc())
                .limit(5)
            )
        ).scalars().all()
        return {
            **_serialize_video(
                v, thumb_dir, collection_ids=cmap.get(v.id, []), **flags.get(v.id, {})
            ),
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


class VideoCollectionsBody(BaseModel):
    collection_ids: list[str]


@router.put("/{video_id}/collections")
async def set_video_collections(
    video_id: str, req: VideoCollectionsBody, request: Request
):
    """设置视频所属的收藏夹（整表替换）。

    **默认收藏夹会被自动保留**：即使前端没传，也会加回来 —— 导入的视频自动归它，
    用户只能再往别的夹里加，不能移出去。这样保证每个视频总有归处。
    """
    sf = request.app.state.session_factory
    async with sf() as s:
        video = await s.get(Video, video_id)
        if video is None:
            raise HTTPException(404, "video not found")
        default = (
            await s.execute(select(Collection).where(Collection.is_default.is_(True)))
        ).scalars().first()
        known = {
            cid
            for (cid,) in (
                await s.execute(select(Collection.id))
            ).all()
        }
        unknown = [cid for cid in req.collection_ids if cid not in known]
        if unknown:
            raise HTTPException(422, f"收藏夹不存在：{', '.join(unknown)}")

        desired = set(req.collection_ids)
        if default is not None:
            desired.add(default.id)

        current = {
            cid
            for (cid,) in (
                await s.execute(
                    select(VideoCollection.collection_id).where(
                        VideoCollection.video_id == video_id
                    )
                )
            ).all()
        }
        for cid in current - desired:
            await s.execute(
                VideoCollection.__table__.delete().where(
                    VideoCollection.video_id == video_id,
                    VideoCollection.collection_id == cid,
                )
            )
        for cid in desired - current:
            s.add(VideoCollection(video_id=video_id, collection_id=cid))
        await s.commit()
    return {"video_id": video_id, "collection_ids": sorted(desired)}


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
        local_rel = v.local_path  # 本地导入的源文件：随视频一起删除
        await s.delete(v)
        await s.commit()
    if local_rel:
        # 上传的原始文件不再需要（笔记/转写/向量都已落库），删掉避免 uploads/ 无限增长
        try:
            (Path(request.app.state.components["settings"].data_dir) / local_rel).unlink(
                missing_ok=True
            )
        except OSError:
            pass
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
                {
                    "start_sec": g.start_sec,
                    "end_sec": g.end_sec,
                    "text": g.text,
                    "source": g.source or "speech",
                }
                for g in segs
            ]
        }
