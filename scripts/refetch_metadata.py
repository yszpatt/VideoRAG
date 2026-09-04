"""一次性脚本：对元数据采集失败（meta_source=none 或 cover_url 为空）的已有视频，
用修复后的 fetch_metadata 重新抓取并落库。仅补元数据，不重跑转写/笔记。

用法：.venv/bin/python scripts/refetch_metadata.py
"""
import asyncio

from sqlalchemy import select

from app.config import Settings
from app.db import make_session_factory
from app.core.fetchers.ytdlp import fetch_metadata
from app.jobs.pipeline import _save_metadata
from app.models import Video


async def main() -> None:
    settings = Settings()
    _, sf = make_session_factory(settings)

    async with sf() as s:
        rows = (
            await s.execute(
                select(Video).where(
                    (Video.meta_source.is_(None)) | (Video.cover_url.is_(None))
                )
            )
        ).scalars().all()
        targets = [
            (v.id, v.url, v.platform)
            for v in rows
            if v.status in ("done", "failed", "transcribing", "noting", "embedding")
        ]

    print(f"待补元数据视频：{len(targets)} 个")
    for vid, url, platform in targets:
        try:
            meta = await fetch_metadata(url, cookie_dir=settings.cookie_dir, timeout=120)
        except Exception as e:  # 隔离：单条失败不影响其它
            print(f"  [{vid}] 跳过（抓取失败：{e}）")
            continue
        await _save_metadata(sf, vid, meta)
        print(
            f"  [{vid}] OK title={meta.get('title')!r} "
            f"author={meta.get('author')!r} cover={bool(meta.get('thumbnail'))} "
            f"comments={len(meta.get('comments') or [])}"
        )
    print("完成。")


if __name__ == "__main__":
    asyncio.run(main())
