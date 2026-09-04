"""本地演示服务：预置演示数据 + fake AI 组件，供浏览器走查 UI。

用法：python scripts/dev_demo.py   （访问 http://localhost:8080）
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.config import Settings
from app.core.embed.embedder import Embedder
from app.core.fetchers.base import FetchedMedia
from app.core.transcribers.base import Segment as TSegment
from app.core.transcribers.base import Transcript
from app.core.vector_store import VectorStore
from app.db import init_db, make_session_factory
from app.main import create_app
from app.models import Note, Segment, Video, new_id

DEMO_VIDEOS = [
    {
        "id": "demo-rag",
        "platform": "bilibili",
        "url": "https://www.bilibili.com/video/BV1RAGx1y7demo",
        "title": "什么是 RAG：检索增强生成入门",
        "summary": "介绍检索增强生成（RAG）的核心原理：把大模型与外部知识库结合，先检索后生成，显著减少幻觉并让回答有出处。",
        "segments": [
            (0.0, 8.0, "大家好，今天聊聊 RAG，也就是检索增强生成。它的核心思想是把大模型和外部知识库结合起来，先检索再生成。"),
            (8.0, 16.0, "第一步，把文档切分成小块，用向量模型编码后存入向量数据库。"),
            (16.0, 24.0, "第二步，用户提问时把问题也转成向量，在数据库里检索出最相关的几个片段。"),
            (24.0, 32.0, "第三步，把检索结果拼进提示词上下文，让大模型基于这些资料来回答。"),
            (32.0, 40.0, "这样做的好处是显著减少幻觉，回答有出处可查，也方便随时更新知识。"),
        ],
    },
    {
        "id": "demo-nas",
        "platform": "youtube",
        "url": "https://www.youtube.com/watch?v=demoNAS",
        "title": "Docker 部署应用到 NAS 实战",
        "summary": "讲解如何用 Docker 在 NAS 上部署自托管服务：镜像打包、端口映射、数据卷挂载、反向代理与备份。",
        "segments": [
            (0.0, 7.0, "NAS 是自托管的好伙伴，配合 Docker 一条命令就能把服务跑起来。"),
            (7.0, 14.0, "先写 Dockerfile，把应用和运行环境一起打包成镜像。"),
            (14.0, 21.0, "用 docker run 或 compose 启动，把容器端口映射到宿主机。"),
            (21.0, 28.0, "数据目录用卷挂载，容器重建也不丢数据。"),
            (28.0, 35.0, "再配一个反向代理，绑定域名和 HTTPS 证书。"),
            (35.0, 42.0, "最后设置开机自启，并定期备份数据卷。"),
        ],
    },
    {
        "id": "demo-whisper",
        "platform": "bilibili",
        "url": "https://www.bilibili.com/video/BV1WHisperdemo",
        "title": "Whisper 语音转写教程",
        "summary": "介绍 OpenAI Whisper 与 faster-whisper 加速实现：CPU int8 量化、中文 large-v3 效果最佳、转写结果带时间戳。",
        "segments": [
            (0.0, 6.0, "Whisper 是 OpenAI 开源的语音识别模型，支持近百种语言。"),
            (6.0, 12.0, "faster-whisper 是它的加速实现，用 CTranslate2 推理，CPU 也能跑。"),
            (12.0, 18.0, "用 int8 量化可以把模型体积和内存占用降下来。"),
            (18.0, 24.0, "转写结果带时间戳，方便对齐字幕和生成笔记。"),
            (24.0, 30.0, "中文用 large-v3 效果最好，内存建议 8G 以上。"),
        ],
    },
]

# 覆盖各种处理状态，便于走查进度 UI（不入库向量，仅状态演示）
EXTRA_VIDEOS = [
    {
        "id": "demo-pending",
        "platform": "douyin",
        "url": "https://www.douyin.com/video/7123456789",
        "title": "短视频运营的五个误区",
        "author": "运营小课堂",
        "duration_sec": 186,
        "status": "pending",
        "with_segments": False,
    },
    {
        "id": "demo-transcribing",
        "platform": "bilibili",
        "url": "https://www.bilibili.com/video/BV1TranscribeDemo",
        "title": "从零搭建本地大模型推理环境",
        "author": "硬核实验室",
        "duration_sec": 1542,
        "status": "transcribing",
        "with_segments": True,
    },
    {
        "id": "demo-failed",
        "platform": "xiaohongshu",
        "url": "https://www.xiaohongshu.com/explore/demo-failed",
        "title": "周末露营装备清单",
        "author": None,
        "duration_sec": None,
        "status": "failed",
        "error": "cookie 缺失：该平台需要提供 cookies.txt 才能抓取（详见部署文档）",
        "with_segments": True,
    },
]

DEMO_NOTE_JSON = {
    "summary": "视频围绕主题展开讲解，包含核心概念、操作步骤与实用建议，条理清晰适合收藏回顾。",
    "chapters": [
        {"title": "背景与核心概念", "start_sec": 0.0, "end_sec": 10.0, "points": ["引出主题"]},
        {"title": "关键步骤拆解", "start_sec": 10.0, "end_sec": 32.0, "points": ["步骤一", "步骤二", "步骤三"]},
        {"title": "总结与建议", "start_sec": 32.0, "end_sec": 42.0, "points": ["要点回顾"]},
    ],
    "key_points": ["先检索后生成", "数据本地化", "结果带出处"],
    "quotes": [{"text": "把大模型和外部知识库结合起来，先检索再生成。", "start_sec": 2.0}],
    "glossary": [{"term": "RAG", "explanation": "检索增强生成，先检索相关资料再让模型生成答案"}],
}


def _note_markdown(video: dict) -> str:
    return (
        f"# {video['title']}\n\n"
        "## 摘要\n"
        f"{video['summary']}\n\n"
        "## 章节\n"
        "### 1. 背景与核心概念（00:00–00:10）\n"
        "- 引出主题\n"
        "### 2. 关键步骤拆解（00:10–00:32）\n"
        "- 步骤一\n- 步骤二\n- 步骤三\n"
        "### 3. 总结与建议（00:32–00:42）\n"
        "- 要点回顾\n\n"
        "## 要点\n"
        "- 先检索后生成\n- 数据本地化\n- 结果带出处\n\n"
        "## 金句\n"
        "> 把大模型和外部知识库结合起来，先检索再生成。（00:02）\n\n"
        "## 术语\n"
        "- **RAG**：检索增强生成，先检索相关资料再让模型生成答案\n"
    )


async def seed(data_dir: str, models_dir: str):
    settings = Settings(_env_file=None, data_dir=data_dir, llm_api_key="demo")
    engine, sf = make_session_factory(settings)
    await init_db(engine, settings)
    embedder = Embedder(models_dir=models_dir)
    store = VectorStore(settings.lancedb_path)

    async with sf() as s:
        existing = set((await s.execute(select(Video.id))).scalars().all())
        todo = [v for v in DEMO_VIDEOS if v["id"] not in existing]
        for v in todo:
            s.add(Video(id=v["id"], platform=v["platform"], url=v["url"], title=v["title"], status="done"))
            for start, end, text in v["segments"]:
                s.add(Segment(video_id=v["id"], start_sec=start, end_sec=end, text=text))
            s.add(
                Note(
                    video_id=v["id"],
                    summary=v["summary"],
                    chapters=DEMO_NOTE_JSON["chapters"],
                    key_points=DEMO_NOTE_JSON["key_points"],
                    quotes=DEMO_NOTE_JSON["quotes"],
                    glossary=DEMO_NOTE_JSON["glossary"],
                    markdown=_note_markdown(v),
                )
            )
        await s.commit()

    for v in todo:
        vecs = await embedder.embed_texts([seg[2] for seg in v["segments"]])
        rows = [
            {
                "id": new_id(),
                "video_id": v["id"],
                "content": text,
                "embedding": vec,
                "start_sec": start,
                "end_sec": end,
                "title": v["title"],
                "platform": v["platform"],
            }
            for (start, end, text), vec in zip(v["segments"], vecs)
        ]
        store.add(rows)
    async with sf() as s:
        for v in [x for x in EXTRA_VIDEOS if x["id"] not in existing]:
            s.add(
                Video(
                    id=v["id"],
                    platform=v["platform"],
                    url=v["url"],
                    title=v["title"],
                    author=v.get("author"),
                    duration_sec=v.get("duration_sec"),
                    status=v["status"],
                    error=v.get("error"),
                )
            )
            if v.get("with_segments"):
                s.add(
                    Segment(video_id=v["id"], start_sec=0.0, end_sec=12.0, text="（演示数据）已转写出的片段。")
                )
        await s.commit()

    store.ensure_fts_index()
    await engine.dispose()
    return settings, sf


async def main():
    data_dir = os.environ.get("DATA_DIR", "/tmp/vr-demo")
    models_dir = os.environ.get("MODELS_DIR", "/tmp/fe-cache")
    settings, sf = await seed(data_dir, models_dir)
    print(f"[demo] seeded {len(DEMO_VIDEOS)} videos -> {data_dir}")

    class DemoFetcher:
        name = "demo"

        async def fetch(self, url, workdir):
            segs = [
                (0.0, 4.0, "这是一个演示视频，由本地 fake 组件即时生成内容。"),
                (4.0, 8.0, "提交后会自动完成转写、笔记与入库，方便体验完整流程。"),
            ]
            return FetchedMedia(
                kind="subtitle",
                subtitle_text="\n".join(t for _, _, t in segs),
                meta={"segments": segs},
            )

    class DemoTranscriber:
        name = "demo"

        async def transcribe(self, media):
            segs = [TSegment(s, e, t) for s, e, t in media.meta["segments"]]
            return Transcript(segments=segs, raw_text="\n".join(x.text for x in segs), source="demo")

    class DemoLLM:
        async def chat_json(self, messages, **kw):
            return DEMO_NOTE_JSON

        async def chat(self, messages, **kw):
            return (
                "根据视频内容，RAG 通过「先检索、再生成」的方式，把大模型和外部知识库结合，"
                "显著减少幻觉并让回答有出处[1]。"
            )

    app = create_app(
        settings=settings,
        fetchers=[DemoFetcher()],
        transcribers=[DemoTranscriber()],
        llm=DemoLLM(),
        embedder=Embedder(models_dir=models_dir),
        vector_store=VectorStore(settings.lancedb_path),
    )

    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info")
    await uvicorn.Server(config).serve()


if __name__ == "__main__":
    asyncio.run(main())
