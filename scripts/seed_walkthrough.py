"""走查演示数据：注入带 M2 富元数据 + M3 历史记录的视频，无需真实转写/LLM。

用法：
    .venv/bin/python scripts/seed_walkthrough.py
默认读项目根 .env（DATA_DIR 指向真实实例目录），写入 videos/comments/notes/
query_history 与 thumbnails/ 缓存封面。再次运行会先清理 wt- 前缀数据再重灌。
"""
import asyncio
import sys
import zlib
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select

from app.config import Settings
from app.db import init_db, make_session_factory
from app.models import Chunk, Comment, Note, QueryHistory, Segment, Video


# ---------- 纯 Python 生成渐变 PNG（避免依赖 PIL） ----------
def _hex_to_rgb(hexstr: str):
    h = hexstr.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _make_gradient_png(path: Path, w: int, h: int, top, bottom):
    def lerp(a, b, t):
        return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

    raw = bytearray()
    for y in range(h):
        raw.append(0)  # filter type 0
        col = lerp(top, bottom, y / max(1, h - 1))
        for _ in range(w):
            raw += bytes(col)
    data = bytes(raw)

    def chunk(typ, payload):
        body = typ + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    idat = zlib.compress(data, 9)
    png = sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    path.write_bytes(png)


VIDEOS = [
    {
        "id": "wt-rag",
        "platform": "bilibili",
        "url": "https://www.bilibili.com/video/BV1wtRAGdemo",
        "title": "【硬核科普】十分钟讲透 RAG 检索增强生成",
        "author": "林粒粒呀",
        "duration_sec": 615,
        "upload_date": "20260115",
        "view_count": 123456,
        "like_count": 7890,
        "tags": ["AI", "RAG", "大模型", "科普"],
        "description": "从向量数据库到语义检索，一条视频讲清楚 RAG 的核心原理。\n"
        "包括：Embedding 是什么、为什么纯向量检索会漏召回、重排（Rerank）怎么救场。\n"
        "适合所有想入门 AI 应用开发的观众。",
        "comment_count": 3,
        "thumb": ("#3b6fe0", "#1b2f6b"),
        "comments": [
            ("程序员老王", "Rerank 那段醍醐灌顶，原来漏召回是这么补的", 1284, "2026-01-16T08:20:00"),
            ("AI 小白", "终于有人把 embedding 讲明白了，收藏", 866, "2026-01-17T12:05:00"),
            ("路人甲", "排版清晰，二刷了", 42, "2026-01-20T19:40:00"),
        ],
        "note": {
            "summary": "检索增强生成（RAG）把大模型与外部知识库结合，先检索后生成，显著减少幻觉并让回答有出处。",
            "chapters": [
                {"title": "背景与核心概念", "start_sec": 0.0, "end_sec": 120.0, "points": ["RAG 定义", "为什么需要检索"]},
                {"title": "关键步骤拆解", "start_sec": 120.0, "end_sec": 420.0, "points": ["切块向量化", "相似度检索", "重排"]},
                {"title": "总结与建议", "start_sec": 420.0, "end_sec": 615.0, "points": ["落地建议"]},
            ],
            "key_points": ["先检索后生成", "重排提升召回", "回答可溯源"],
            "quotes": [{"text": "把大模型和外部知识库结合起来，先检索再生成。", "start_sec": 18.0}],
            "glossary": [{"term": "RAG", "explanation": "检索增强生成：先检索相关资料再让模型生成答案"}],
        },
    },
    {
        "id": "wt-nas",
        "platform": "youtube",
        "url": "https://www.youtube.com/watch?v=wtNASdemo",
        "title": "Docker 部署应用到 NAS 实战",
        "author": "运维老司机",
        "duration_sec": 1523,
        "upload_date": "20260202",
        "view_count": 45200,
        "like_count": 2300,
        "tags": ["NAS", "Docker", "自托管"],
        "description": "手把手教你把自托管服务跑在 NAS 上：镜像打包、端口映射、数据卷挂载、反向代理与备份。\n零基础也能跟着做。",
        "comment_count": 2,
        "thumb": ("#1faa8a", "#0c3d33"),
        "comments": [
            ("家庭实验室", "反向代理那节救我狗命", 530, "2026-02-03T10:00:00"),
            ("小白", "求后续：HTTPS 证书自动续期", 188, "2026-02-05T21:30:00"),
        ],
        "note": {
            "summary": "用 Docker 在 NAS 上部署自托管服务：镜像打包、端口映射、卷挂载、反代与备份。",
            "chapters": [
                {"title": "镜像与端口", "start_sec": 0.0, "end_sec": 300.0, "points": ["写 Dockerfile", "端口映射"]},
                {"title": "数据与反代", "start_sec": 300.0, "end_sec": 900.0, "points": ["卷挂载", "反向代理", "备份"]},
            ],
            "key_points": ["数据卷挂载防丢失", "反代绑域名+HTTPS", "定期备份"],
            "quotes": [{"text": "数据目录用卷挂载，容器重建也不丢数据。", "start_sec": 360.0}],
            "glossary": [{"term": "反向代理", "explanation": "在公网入口统一转发到内网服务，并管理域名与证书"}],
        },
    },
    {
        "id": "wt-whisper",
        "platform": "bilibili",
        "url": "https://www.bilibili.com/video/BV1wtWhisperdemo",
        "title": "Whisper 语音转写从入门到实战",
        "author": "硬核实验室",
        "duration_sec": 980,
        "upload_date": "20251220",
        "view_count": 88300,
        "like_count": 4100,
        "tags": ["Whisper", "ASR", "语音识别"],
        "description": "OpenAI Whisper 与 faster-whisper 加速实现：CPU int8 量化、中文 large-v3 效果最佳、转写带时间戳。",
        "comment_count": 1,
        "thumb": ("#8b5cf6", "#3b1f6b"),
        "comments": [
            ("语音爱好者", "int8 量化省了一半内存，实测可用", 720, "2025-12-21T09:10:00"),
        ],
        "note": None,
    },
    {
        "id": "wt-transcribing",
        "platform": "bilibili",
        "url": "https://www.bilibili.com/video/BV1wtTransdemo",
        "title": "从零搭建本地大模型推理环境",
        "author": "硬核实验室",
        "duration_sec": 1542,
        "status": "transcribing",
        "thumb": None,
        "comments": [],
        "note": None,
    },
    {
        "id": "wt-failed",
        "platform": "xiaohongshu",
        "url": "https://www.xiaohongshu.com/explore/wt-failed",
        "title": "周末露营装备清单",
        "author": None,
        "status": "failed",
        "error": "cookie 缺失：该平台需要提供 cookies.txt 才能抓取（详见部署文档）",
        "thumb": None,
        "comments": [],
        "note": None,
    },
]

HISTORY = [
    {
        "kind": "ask",
        "query": "RAG 的检索流程是怎么做的？",
        "top_k": 8,
        "answer": "RAG 检索流程主要分三步：1. 将文档切块并向量化入库；2. 提问时把问题向量化后做相似度检索；"
        "3. 命中片段重排后交给 LLM 生成答案。",
        "citations": [{"title": "十分钟讲透 RAG", "video_id": "wt-rag",
                       "url": "https://www.bilibili.com/video/BV1wtRAGdemo",
                       "start_sec": 120, "end_sec": 180, "content": "检索流程：切块→向量化→相似度检索→重排→生成"}],
        "hit_count": 3,
        "ago": timedelta(minutes=5),
    },
    {
        "kind": "ask",
        "query": "有哪些可以直接落地的做法？",
        "top_k": 8,
        "answer": "可以从三类开始落地：先用开源 embedding 模型自建小库验证效果，再接入 Rerank 提升召回精度，最后用流式输出优化体验。",
        "citations": [],
        "hit_count": 1,
        "ago": timedelta(hours=2),
    },
    {
        "kind": "search",
        "query": "向量数据库 选型",
        "top_k": 8,
        "hits_count": 12,
        "hit_count": 2,
        "ago": timedelta(hours=1),
    },
]


async def main():
    settings = Settings()  # 读项目根 .env → data_dir 等
    engine, sf = make_session_factory(settings)
    await init_db(engine, settings)
    now = datetime.now(timezone.utc)
    thumb_dir = Path(settings.data_dir) / "thumbnails"
    thumb_dir.mkdir(parents=True, exist_ok=True)

    async with sf() as s:
        # idempotent：清理旧的 wt- 数据
        old = (await s.execute(select(Video.id).where(Video.id.like("wt-%")))).scalars().all()
        for vid in old:
            await s.execute(delete(Comment).where(Comment.video_id == vid))
            await s.execute(delete(Note).where(Note.video_id == vid))
            await s.execute(delete(Segment).where(Segment.video_id == vid))
            await s.execute(delete(Chunk).where(Chunk.video_id == vid))
            v = await s.get(Video, vid)
            if v:
                await s.delete(v)
        await s.execute(delete(QueryHistory).where(QueryHistory.query_key.like("wt-%")))
        await s.commit()

        for v in VIDEOS:
            status = v.get("status", "done")
            s.add(Video(
                id=v["id"], platform=v["platform"], url=v["url"], title=v["title"],
                author=v.get("author"), duration_sec=v.get("duration_sec"), status=status,
                error=v.get("error"),
                description=v.get("description"), upload_date=v.get("upload_date"),
                view_count=v.get("view_count"), like_count=v.get("like_count"),
                tags=v.get("tags"), comment_count=v.get("comment_count"),
                meta_source="ytdlp" if status == "done" else None,
                created_at=now, updated_at=now,
            ))
            for author, text, like, pub in v.get("comments", []):
                s.add(Comment(video_id=v["id"], author=author, text=text,
                              like_count=like, published_at=pub))
            note = v.get("note")
            if note:
                s.add(Note(
                    video_id=v["id"], summary=note["summary"], chapters=note["chapters"],
                    key_points=note["key_points"], quotes=note["quotes"],
                    glossary=note["glossary"],
                    markdown=f"# {v['title']}\n\n## 摘要\n{note['summary']}\n",
                ))
            if v.get("thumb"):
                top = _hex_to_rgb(v["thumb"][0])
                bottom = _hex_to_rgb(v["thumb"][1])
                _make_gradient_png(thumb_dir / f"{v['id']}.jpg", 96, 54, top, bottom)
        await s.commit()

        for i, h in enumerate(HISTORY):
            s.add(QueryHistory(
                id=f"wt-{h['kind']}-{i}",
                kind=h["kind"],
                query=h["query"],
                query_key=f"wt-{h['kind']}\x00{h['query']}",
                top_k=h.get("top_k"),
                answer=(h.get("answer") or "") or None,
                citations_json=h.get("citations", []),
                hits_count=h.get("hits_count"),
                hit_count=h["hit_count"],
                last_used_at=now - h["ago"],
            ))
        await s.commit()

    print(f"seeded {len(VIDEOS)} videos + {len(HISTORY)} history rows -> {settings.data_dir}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
