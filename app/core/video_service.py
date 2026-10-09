from sqlalchemy import select

from app.core.fetchers.ytdlp import detect_platform
from app.models import Collection, Video, VideoCollection


class SubmitError(ValueError):
    pass


async def submit_video_url(session_factory, queue, url: str) -> tuple[str, str]:
    """创建视频记录并入队处理。返回 (video_id, status)。"""
    url = (url or "").strip()
    if not url:
        raise SubmitError("url is required")
    async with session_factory() as s:
        video = Video(platform=detect_platform(url), url=url)
        s.add(video)
        await s.flush()  # 先拿到 id，再挂默认收藏夹（同事务，避免出现孤儿视频）
        default = (
            await s.execute(select(Collection).where(Collection.is_default.is_(True)))
        ).scalars().first()
        if default is not None:
            s.add(VideoCollection(video_id=video.id, collection_id=default.id))
        await s.commit()
        vid = video.id
    await queue.enqueue(vid, "process", {"video_id": vid})
    return vid, "pending"
