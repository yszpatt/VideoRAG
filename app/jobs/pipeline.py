"""摄入流水线：获取 → 转写 → 笔记 → 切片入库 → 更新视频状态。"""

import asyncio
import logging
import os
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.embed.chunker import Chunk, chunk_segments
from app.core.fetchers.base import FetchedMedia, Fetcher
from app.core.fetchers.ytdlp import FetchError, fetch_metadata
from app.core.notes import NoteData, generate_note, render_markdown
from app.core.transcribers.base import Transcript, Transcriber
from app.models import Chunk as ORMChunk
from app.models import Comment, Note, Segment, Video, new_id

logger = logging.getLogger(__name__)

# 笔记上下文里简介注入的截断长度（E2 变量表口径，库内保存更长）
NOTE_DESC_CHARS = 2000
NOTE_TOP_COMMENTS = 5
# meta 分片（视频级简介）正文截断：标题 + 简介拼入一条向量文档
META_DESC_CHARS = 4000

# 默认降级链（遗留常量，仅供引用；实际转写链由 factory.build_transcribers 按配置构造，
# 当前部署为 [字幕, 集中ASR]，本地 whisper 不在链内）。
DEFAULT_FETCH_CHAIN = ["subtitle", "audio", "video"]
DEFAULT_TRANSCRIBE_CHAIN = ["subtitle", "whisper"]


async def fetch_with_fallback(
    url: str, workdir: str, fetchers: list[Fetcher]
) -> tuple[FetchedMedia, str]:
    """按序尝试 fetcher，记录实际使用的 provider；全部失败抛 FetchError。"""
    last: Exception | None = None
    for f in fetchers:
        try:
            media = await f.fetch(url, workdir)
            if media is not None:
                return media, f.name
        except Exception as e:  # 该 provider 失败，降级
            last = e
    raise FetchError(f"all fetch providers failed for {url}: {last}")


async def transcribe_with_fallback(
    media: FetchedMedia, transcribers: list[Transcriber]
) -> tuple[Transcript, str]:
    """按序尝试 transcriber。

    - 转写结果非空（有 segments）即命中
    - 空结果（如抓到空字幕）继续降级下一档
    - 最后一个 transcriber 无论结果是否为空都接受（兜底）
    """
    last: Exception | None = None
    for i, t in enumerate(transcribers):
        try:
            transcript = await t.transcribe(media)
            if transcript is not None and (transcript.segments or i == len(transcribers) - 1):
                return transcript, t.name
        except Exception as e:
            last = e
    raise RuntimeError(f"all transcribe providers failed for {media.kind}: {last}")


async def process_video(
    video_id: str,
    session_factory: async_sessionmaker,
    fetchers: list[Fetcher],
    transcribers: list[Transcriber],
    llm,
    data_dir: str,
    embedder=None,
    vector_store=None,
    prompts=None,
    cookie_dir: str | None = None,
) -> None:
    """单个视频的完整摄入流程：metadata → fetch → transcribe → note → embed → done。

    - metadata（E1）：抓取标题/简介/封面/热评，失败只记日志不 fail 视频；
      成功后标题/简介/热评注入笔记上下文（note_vars）；
    - prompts（E2）：PromptRegistry，笔记生成走用户自定义提示词；None 用内置默认。
    """
    async with session_factory() as s:
        video = await s.get(Video, video_id)
        if video is None:
            raise ValueError(f"video {video_id} not found")
        url = video.url
        title = video.title or video.url
        platform = video.platform

    media: FetchedMedia | None = None
    try:
        await _save(session_factory, video_id, status="fetching")
        meta = await _fetch_metadata_step(session_factory, video_id, url, cookie_dir)
        if meta:
            title = meta.get("title") or title  # 笔记/入库用真实标题（替代 URL 兜底）

        media, _used_fetcher = await fetch_with_fallback(url, "", fetchers)

        await _save(session_factory, video_id, status="transcribing")
        transcript, _used_transcriber = await transcribe_with_fallback(media, transcribers)
        await _store_segments(session_factory, video_id, transcript)

        await _save(session_factory, video_id, status="noting")
        note = await generate_note(
            title, transcript, llm, prompts=prompts,
            note_vars=_note_vars_from_meta(meta),
        )
        await _save_note(session_factory, video_id, note, title, data_dir)

        if embedder is not None and vector_store is not None:
            await _save(session_factory, video_id, status="embedding")
            await _embed_chunks(
                session_factory, video_id, transcript, title, platform,
                embedder, vector_store,
            )
            await _embed_meta(
                session_factory, video_id, title,
                (meta or {}).get("description"), platform,
                embedder, vector_store,
            )

        await _save(session_factory, video_id, status="done")
    except Exception as e:
        await _save(session_factory, video_id, status="failed", error=str(e))
    finally:
        # 清理临时音频/视频（字幕文件保留）：成功与失败路径都清，避免转写/笔记
        # 失败时把下载的 m4a/webm 留在 data/audio、data/downloads 里积盘。
        # 仅在媒体已下载（media 非 None）且非字幕时删除。
        if media and media.kind in ("audio", "video") and media.path:
            try:
                os.remove(media.path)
            except OSError:
                pass


async def _save(session_factory, video_id, status=None, error=None):
    async with session_factory() as s:
        v = await s.get(Video, video_id)
        if status:
            v.status = status
        if error:
            v.error = error
        await s.commit()


async def _fetch_metadata_step(session_factory, video_id: str, url: str, cookie_dir):
    """E1：抓取元数据并落库。失败只记日志并把 meta_source 标记为 none（隔离）。"""
    try:
        meta = await fetch_metadata(url, cookie_dir=cookie_dir)
    except Exception as e:
        logger.warning("metadata fetch failed for %s: %s", video_id, e)
        async with session_factory() as s:
            v = await s.get(Video, video_id)
            if v is not None and v.meta_source is None:
                v.meta_source = "none"
                await s.commit()
        return None
    await _save_metadata(session_factory, video_id, meta)
    return meta


async def _save_metadata(session_factory, video_id: str, meta: dict) -> None:
    """更新 Video 元数据列 + 幂等替换评论（先删旧再插，支持重试）。"""
    async with session_factory() as s:
        v = await s.get(Video, video_id)
        if v is None:
            return
        v.title = v.title or meta.get("title")
        v.author = meta.get("author")
        v.duration_sec = v.duration_sec or meta.get("duration")
        v.description = meta.get("description")
        v.cover_url = meta.get("thumbnail")
        v.upload_date = meta.get("upload_date")
        v.view_count = meta.get("view_count")
        v.like_count = meta.get("like_count")
        v.tags = meta.get("tags")
        v.comment_count = meta.get("comment_count")
        v.meta_source = "ytdlp"
        await s.execute(delete(Comment).where(Comment.video_id == video_id))
        for c in meta.get("comments") or []:
            s.add(
                Comment(
                    video_id=video_id,
                    author=c.get("author"),
                    text=c.get("text") or "",
                    like_count=c.get("like_count") or 0,
                    published_at=c.get("published_at"),
                )
            )
        await s.commit()


def _note_vars_from_meta(meta: dict | None) -> dict | None:
    """E1→E2：从元数据构造笔记上下文变量（title 由 generate_note 参数提供）。

    简介截断 2000 字符；热评取 top5（已按点赞降序归一化）。只放入非空键：
    元数据缺失的维度留空让 notes.py 回落「（未采集）」占位。
    """
    if not meta:
        return None
    comments = meta.get("comments") or []
    top = "\n".join(
        f"{i}. {c['text']}（赞 {c['like_count']}）"
        for i, c in enumerate(comments[:NOTE_TOP_COMMENTS], 1)
    )
    vars_: dict[str, str] = {}
    if (meta.get("author") or "").strip():
        vars_["author"] = meta["author"].strip()
    if (meta.get("description") or "").strip():
        vars_["description"] = meta["description"].strip()[:NOTE_DESC_CHARS]
    if top:
        vars_["top_comments"] = top
    return vars_ or None


async def _store_segments(session_factory, video_id, transcript: Transcript) -> None:
    async with session_factory() as s:
        for seg in transcript.segments:
            s.add(
                Segment(
                    video_id=video_id,
                    start_sec=seg.start_sec,
                    end_sec=seg.end_sec,
                    text=seg.text,
                    speaker=seg.speaker,
                )
            )
        await s.commit()


async def _save_note(
    session_factory, video_id: str, note: NoteData, title: str, data_dir: str
) -> None:
    md = render_markdown(note, title)
    notes_dir = Path(data_dir) / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    path = notes_dir / f"{video_id}.md"
    path.write_text(md, encoding="utf-8")
    async with session_factory() as s:
        s.add(
            Note(
                video_id=video_id,
                summary=note.summary,
                chapters=note.chapters,
                key_points=note.key_points,
                quotes=note.quotes,
                glossary=note.glossary,
                markdown=md,
            )
        )
        v = await s.get(Video, video_id)
        v.note_path = str(path)
        await s.commit()


def assert_model_compat(vector_store, embedder, vec: list[float]) -> None:
    """入库前模型指纹校验：embedding 模型与建库模型不一致时抛错（含同维度不同模型）。

    防御式接线：测试替身（FakeStore/FakeEmbedder）无相应方法/属性时跳过。
    """
    checker = getattr(vector_store, "check_model_compat", None)
    fp = getattr(embedder, "fingerprint", None)
    if checker is None or not fp:
        return
    checker({**fp, "dim": len(vec)})


# embedding 分批大小：一次向量化/入库的切片上限。
# 长视频（1h 视频 ≈ 400~900 块）若整批处理，本地 fastembed 的 ONNX 中间张量与
# 远程 /v1/embeddings 的超长请求体都会把峰值内存抬高数倍；分批后峰值降到
# 「单批 chunk + 单批向量」，长视频内存峰值可降 5~10 倍。短内容（≤ 一批）行为不变。
EMBED_BATCH_SIZE = 64


async def _embed_chunks(
    session_factory,
    video_id: str,
    transcript: Transcript,
    title: str,
    platform: str,
    embedder,
    vector_store,
) -> None:
    """切片 → 向量化 → LanceDB 入库 → SQLite chunks 表落库（分批流式）。

    分批的意义是控制内存峰值而不是减少写入：每批 embed 完立即入库并释放该批
    向量，避免「全部 chunk 的向量同时在内存」。批间通过 await 让事件循环有机会
    回收。``ensure_fts_index`` 仍在全部批次写完后调用一次（每批建一次 FTS 索引
    会造成重复索引构建，浪费 CPU 与磁盘）。
    """
    chunks: list[Chunk] = chunk_segments(transcript.segments)
    if not chunks:
        return
    first_vec: list[float] | None = None
    for start in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[start : start + EMBED_BATCH_SIZE]
        vectors = await embedder.embed_texts([c.content for c in batch])
        if not vectors:
            continue
        if first_vec is None:
            # 首向量用于模型指纹/维度校验（只需一次）
            first_vec = vectors[0]
            assert_model_compat(vector_store, embedder, first_vec)
        rows: list[dict] = []
        for c, v in zip(batch, vectors):
            rows.append(
                {
                    "id": new_id(),
                    "video_id": video_id,
                    "content": c.content,
                    "embedding": v,
                    "start_sec": c.start_sec,
                    "end_sec": c.end_sec,
                    "title": title,
                    "platform": platform,
                    "kind": "content",
                }
            )
        await asyncio.to_thread(vector_store.add, rows)
        await _store_chunks(session_factory, video_id, rows)
    await asyncio.to_thread(vector_store.ensure_fts_index)


async def _embed_meta(
    session_factory,
    video_id: str,
    title: str,
    description: str | None,
    platform: str,
    embedder,
    vector_store,
) -> None:
    """视频简介（标题 + 简介）→ 单条视频级 meta 分片（kind=meta，无时间戳）入库。

    - 简介缺失（元数据未采到）直接跳过；
    - 幂等：该视频已有 meta 分片则跳过（重试 / backfill 安全）；
    - 与口播切片同表同 schema，靠 kind 区分，删除视频时一并清理。
    """
    desc = (description or "").strip()
    if not desc:
        return
    if await asyncio.to_thread(vector_store.has_kind_rows, video_id, "meta"):
        return
    content = f"视频标题：{(title or '').strip()}\n视频简介：{desc[:META_DESC_CHARS]}"
    vectors = await embedder.embed_texts([content])
    assert_model_compat(vector_store, embedder, vectors[0])
    row = {
        "id": new_id(),
        "video_id": video_id,
        "content": content,
        "embedding": vectors[0],
        "start_sec": 0.0,
        "end_sec": 0.0,
        "title": title,
        "platform": platform,
        "kind": "meta",
    }
    await asyncio.to_thread(vector_store.add, [row])
    # 存量表若在 add 时发生 kind 迁移（表重建），FTS 索引会丢失，这里幂等重建；
    # 已有索引时该调用静默跳过。
    await asyncio.to_thread(vector_store.ensure_fts_index)
    await _store_chunks(session_factory, video_id, [row])


async def _store_chunks(session_factory, video_id: str, rows: list[dict]) -> None:
    async with session_factory() as s:
        for r in rows:
            kind = r.get("kind", "content")
            s.add(
                ORMChunk(
                    id=r["id"],
                    video_id=video_id,
                    content=r["content"],
                    start_sec=r["start_sec"],
                    end_sec=r["end_sec"],
                    kind=kind,
                    meta={
                        "title": r.get("title"),
                        "platform": r.get("platform"),
                        "kind": kind,
                    },
                    lancedb_id=r["id"],
                )
            )
        await s.commit()
