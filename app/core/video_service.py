from sqlalchemy import select

from app.core.fetchers.ytdlp import detect_platform
from app.models import Collection, Video, VideoCollection


class SubmitError(ValueError):
    pass


async def _register(
    session_factory,
    queue,
    *,
    video_id: str | None = None,
    platform: str,
    url: str = "",
    title: str | None = None,
    local_path: str | None = None,
    duration_sec: float | None = None,
) -> tuple[str, str]:
    """建 Video 记录并入队处理（URL 导入与本地导入共用）。

    - 同事务里把视频挂进「默认收藏夹」（语义见 app/api/collections.py：
      默认夹自动归属、不可取消），避免出现没有任何收藏夹归属的孤儿视频；
    - 返回 (video_id, status)。
    """
    async with session_factory() as s:
        video = Video(id=video_id) if video_id else Video()
        video.platform = platform
        video.url = url
        if title:
            video.title = title
        if local_path:
            video.local_path = local_path
        if duration_sec:
            video.duration_sec = duration_sec
        s.add(video)
        await s.flush()  # 先拿到 id，再挂默认收藏夹
        default = (
            await s.execute(select(Collection).where(Collection.is_default.is_(True)))
        ).scalars().first()
        if default is not None:
            s.add(VideoCollection(video_id=video.id, collection_id=default.id))
        await s.commit()
        vid = video.id

    await queue.enqueue(vid, "process", {"video_id": vid})
    return vid, "pending"


async def submit_video_url(session_factory, queue, url: str) -> tuple[str, str]:
    """创建视频记录并入队处理（在线链接）。返回 (video_id, status)。"""
    url = (url or "").strip()
    if not url:
        raise SubmitError("url is required")
    return await _register(
        session_factory, queue, platform=detect_platform(url), url=url
    )


async def submit_local_video(
    session_factory,
    queue,
    *,
    video_id: str,
    filename: str,
    local_path: str,
    title: str | None = None,
    duration_sec: float | None = None,
) -> tuple[str, str]:
    """登记一个本地导入的视频并入队处理。

    ``local_path`` 是**相对数据目录**的路径（见 models.Video.local_path），
    ``filename`` 仅用于展示/兜底标题。
    """
    return await _register(
        session_factory,
        queue,
        video_id=video_id,
        platform="local",
        url="",
        title=(title or filename or "本地视频").strip() or "本地视频",
        local_path=local_path,
        duration_sec=duration_sec,
    )
