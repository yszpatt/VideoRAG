"""一次性脚本：为已有视频补建「视频级 meta 分片」（标题 + 简介，kind=meta）。

meta 通道上线前入库的视频只嵌了带时间戳的口播切片；本脚本对 status=done 且
简介非空的视频调用 pipeline._embed_meta 补一条 meta 分片，让简介/标题可被检索。
幂等：已存在 meta 分片的视频自动跳过（_embed_meta 内部有 has_kind_rows 去重）。

用法：.venv/bin/python scripts/backfill_meta.py
"""
import asyncio

from sqlalchemy import select

from app.config import Settings
from app.core.factory import build_embedder
from app.core.vector_store import VectorStore
from app.db import make_session_factory
from app.jobs.pipeline import _embed_meta
from app.models import Video


async def main() -> None:
    settings = Settings()
    _, sf = make_session_factory(settings)
    embedder = build_embedder(settings)
    vs = VectorStore(settings.lancedb_path)

    async with sf() as s:
        rows = (
            await s.execute(select(Video).where(Video.status == "done"))
        ).scalars().all()
        targets = [
            (v.id, v.title or v.url, v.description, v.platform)
            for v in rows
            if (v.description or "").strip()
        ]

    print(f"待补 meta 分片的视频：{len(targets)} 个")
    ok = skipped = failed = 0
    for vid, title, description, platform in targets:
        try:
            if vs.has_kind_rows(vid, "meta"):
                skipped += 1
                print(f"  [{vid}] 已有 meta 分片，跳过")
                continue
            await _embed_meta(sf, vid, title, description, platform, embedder, vs)
            ok += 1
            print(f"  [{vid}] OK title={title[:24]!r}")
        except Exception as e:  # 隔离：单条失败不影响其它
            failed += 1
            print(f"  [{vid}] 失败（{e}）")
    print(f"完成：新增 {ok}，跳过 {skipped}，失败 {failed}。")


if __name__ == "__main__":
    asyncio.run(main())
