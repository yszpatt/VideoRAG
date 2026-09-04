from app.core.notes import strip_quotes_section
from app.core.rag.qa import answer_question
from app.core.rag.retriever import RetrievalParams, retrieve
from app.core.video_service import submit_video_url
from app.models import Note, Segment, Video

from sqlalchemy import select


def _retrieval_params(components: dict) -> RetrievalParams:
    """从当前设置构造检索策略参数（与 /api/search、/api/ask 同源）。"""
    s = components.get("settings")
    if s is None:
        return RetrievalParams()
    return RetrievalParams(
        vector_k=s.retrieval_vector_k,
        fts_k=s.retrieval_fts_k,
        rrf_k=s.retrieval_rrf_k,
        vector_weight=s.retrieval_vector_weight,
        fts_weight=s.retrieval_fts_weight,
        per_video_cap=s.retrieval_per_video_cap,
        min_sim=s.retrieval_min_sim,
        neighbor_gap=s.retrieval_neighbor_gap,
    )


async def _search(components: dict, query: str, top_k: int = 5) -> list[dict]:
    hits = await retrieve(
        query, components["vector_store"], components["embedder"],
        top_k=top_k, params=_retrieval_params(components),
    )
    return [
        {
            "video_id": h.video_id,
            "title": h.title,
            "content": h.content,
            "start_sec": h.start_sec,
            "end_sec": h.end_sec,
            "score": round(h.score, 4),
        }
        for h in hits
    ]


async def _ask(components: dict, question: str, top_k: int = 5) -> dict:
    hits = await retrieve(
        question, components["vector_store"], components["embedder"],
        top_k=top_k, params=_retrieval_params(components),
    )
    answer = await answer_question(question, hits, components["llm"])
    return {"answer": answer.answer, "citations": answer.citations}


async def _list_videos(
    components: dict, platform: str | None = None, keyword: str | None = None, limit: int = 20
) -> list[dict]:
    sf = components["session_factory"]
    async with sf() as s:
        q = select(Video).order_by(Video.created_at.desc()).limit(min(limit, 100))
        if platform:
            q = q.where(Video.platform == platform)
        if keyword:
            q = q.where(Video.title.contains(keyword))
        rows = (await s.execute(q)).scalars().all()
    return [
        {"video_id": v.id, "platform": v.platform, "title": v.title or v.url, "status": v.status}
        for v in rows
    ]


async def _get_note(components: dict, video_id: str) -> dict | None:
    sf = components["session_factory"]
    async with sf() as s:
        note = (
            await s.execute(select(Note).where(Note.video_id == video_id))
        ).scalar_one_or_none()
        if note is None:
            return None
        return {
            "summary": note.summary,
            "chapters": note.chapters or [],
            "key_points": note.key_points or [],
            "quotes": note.quotes or [],
            "glossary": note.glossary or [],
            "markdown": strip_quotes_section(note.markdown),
        }


async def _get_transcript(
    components: dict, video_id: str, start: float | None = None, end: float | None = None
) -> list[dict]:
    sf = components["session_factory"]
    async with sf() as s:
        q = (
            select(Segment)
            .where(Segment.video_id == video_id)
            .order_by(Segment.start_sec)
        )
        if start is not None:
            q = q.where(Segment.end_sec >= start)
        if end is not None:
            q = q.where(Segment.start_sec <= end)
        rows = (await s.execute(q)).scalars().all()
    return [{"start_sec": g.start_sec, "end_sec": g.end_sec, "text": g.text} for g in rows]


async def _submit(components: dict, url: str) -> dict:
    video_id, status = await submit_video_url(
        components["session_factory"], components["queue"], url
    )
    return {"video_id": video_id, "status": status}


def build_mcp_server(components: dict) -> "FastMCP":
    """构造 FastMCP 实例（Streamable HTTP，挂载到 /mcp）。"""
    import json

    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    # stateless_http：无会话、无需 task group 常驻，适合嵌入现有 ASGI 应用
    # transport_security：关闭 DNS rebinding 防护（自托管内网场景，允许任意 Host 访问）
    # streamable_http_path="/mcp" 与外部 Route("/mcp") 对应
    mcp = FastMCP(
        "video-rag",
        stateless_http=True,
        streamable_http_path="/mcp",
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )

    @mcp.tool()
    async def search_knowledge_base(query: str, top_k: int = 5) -> str:
        """在视频知识库中检索相关片段（带时间戳与来源视频）。"""
        return json.dumps(await _search(components, query, top_k), ensure_ascii=False, indent=2)

    @mcp.tool()
    async def ask_video_rag(question: str) -> str:
        """基于视频知识库回答问题，返回带引用的答案。"""
        return json.dumps(await _ask(components, question), ensure_ascii=False, indent=2)

    @mcp.tool()
    async def list_videos(platform: str | None = None, keyword: str | None = None, limit: int = 20) -> str:
        """列出知识库中的视频。"""
        return json.dumps(await _list_videos(components, platform, keyword, limit), ensure_ascii=False, indent=2)

    @mcp.tool()
    async def get_video_note(video_id: str) -> str:
        """获取视频的结构化笔记（摘要/章节/要点/术语）。"""
        note = await _get_note(components, video_id)
        if note is None:
            return json.dumps({"error": f"note not found for {video_id}"}, ensure_ascii=False)
        return json.dumps(note, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def get_transcript(video_id: str, start: float | None = None, end: float | None = None) -> str:
        """获取视频带时间戳的转写文本。"""
        return json.dumps(await _get_transcript(components, video_id, start, end), ensure_ascii=False, indent=2)

    @mcp.tool()
    async def submit_video(url: str) -> str:
        """提交视频链接进入处理队列。"""
        return json.dumps(await _submit(components, url), ensure_ascii=False, indent=2)

    return mcp


def mcp_http_middleware(app_asgi, api_key: str, rate_per_minute: int = 60):
    """为 /mcp 加 Bearer 鉴权 + per-IP 限流 + 审计日志。"""
    import logging
    import time

    logger = logging.getLogger("mcp")
    hits: dict[str, list[float]] = {}

    async def _send_text(send, status: int, text: str):
        body = text.encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"text/plain"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def wrapped(scope, receive, send):
        if scope["type"] != "http":
            return await app_asgi(scope, receive, send)
        client = scope.get("client", ("?", 0))[0]
        now = time.time()
        # 鉴权
        if api_key:
            headers = dict(scope.get("headers") or [])
            if headers.get(b"authorization", b"") != f"Bearer {api_key}".encode():
                return await _send_text(send, 401, "unauthorized")
        # 限流（滑动窗口 60s）
        window = hits.setdefault(client, [])
        window[:] = [t for t in window if now - t < 60]
        if len(window) >= rate_per_minute:
            return await _send_text(send, 429, "rate limited")
        window.append(now)
        # 审计
        logger.info("mcp %s %s from %s", scope.get("method", ""), scope.get("path", ""), client)
        await app_asgi(scope, receive, send)

    return wrapped
