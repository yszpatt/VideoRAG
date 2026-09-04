import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import select

from app.core.history import record_history
from app.core.rag.qa import answer_question
from app.core.rag.retriever import Hit, RetrievalParams, retrieve
from app.models import Video

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _retrieval_params(request: Request) -> RetrievalParams:
    """从当前设置构造检索策略参数（设置页保存即生效，无需重启）。"""
    s = request.app.state.components.get("settings")
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


def _history_limit(request: Request) -> int:
    return int(
        getattr(request.app.state.components.get("settings"), "history_limit", 200)
        or 200
    )


class AskRequest(BaseModel):
    question: str
    top_k: int = 8


class SearchRequest(BaseModel):
    query: str
    top_k: int = 8


async def _video_urls(request: Request, video_ids: set[str]) -> dict[str, str]:
    """批量取视频原始 URL，供前端构造时间戳跳转链接。"""
    if not video_ids:
        return {}
    sf = request.app.state.session_factory
    async with sf() as s:
        rows = (
            await s.execute(select(Video).where(Video.id.in_(video_ids)))
        ).scalars().all()
    return {v.id: v.url for v in rows}


@router.post("/ask")
async def ask(req: AskRequest, request: Request):
    hits = await retrieve(
        req.question, request.app.state.vector_store, request.app.state.embedder,
        top_k=req.top_k, params=_retrieval_params(request),
    )
    # E2：提示词注册表现取（每次请求读最新值，保存即生效）
    prompts = request.app.state.components.get("prompts")
    answer = await answer_question(req.question, hits, request.app.state.llm, prompts)
    urls = await _video_urls(request, {h.video_id for h in hits})
    citations = [{**c, "url": urls.get(c.get("video_id"), "")} for c in answer.citations]
    # E5：成功后才写历史（失败隔离，不影响主请求）
    try:
        await record_history(
            request.app.state.session_factory, "ask", req.question,
            top_k=req.top_k, answer=answer.answer, citations=citations,
            limit=_history_limit(request),
        )
    except Exception as e:
        logger.warning("history record failed (ask): %s", e)
    return {"answer": answer.answer, "citations": citations}


@router.post("/search")
async def search(req: SearchRequest, request: Request):
    hits: list[Hit] = await retrieve(
        req.query, request.app.state.vector_store, request.app.state.embedder,
        top_k=req.top_k, params=_retrieval_params(request),
    )
    urls = await _video_urls(request, {h.video_id for h in hits})
    # E5：写历史（失败隔离）
    try:
        await record_history(
            request.app.state.session_factory, "search", req.query,
            top_k=req.top_k, hits_count=len(hits),
            limit=_history_limit(request),
        )
    except Exception as e:
        logger.warning("history record failed (search): %s", e)
    return {
        "hits": [
            {
                "chunk_id": h.chunk_id,
                "video_id": h.video_id,
                "content": h.content,
                "start_sec": h.start_sec,
                "end_sec": h.end_sec,
                "title": h.title,
                "score": round(h.score, 4),
                "url": urls.get(h.video_id, ""),
                "kind": h.kind,
            }
            for h in hits
        ]
    }
