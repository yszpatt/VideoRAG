from app.core.fetchers.ytdlp import detect_platform
from app.models import Video


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
        await s.commit()
        vid = video.id
    await queue.enqueue(vid, "process", {"video_id": vid})
    return vid, "pending"
