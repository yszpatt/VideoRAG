"""向量库重建 API（/api/vectorstore）：换 embedding 模型后以当前模型一键重嵌入。

- POST /rebuild        启动后台重建任务（单飞：已有任务进行中 → 409）
- GET  /rebuild/status 查询任务进度 / 结果

任务状态放 request.app.state（进程内；重建幂等，重启/中断后重按按钮即可）。
与 scripts/reembed_vector_store.py 共用 app.core.embed.rebuild 同一实现。
"""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, HTTPException, Request

from app.core.embed.rebuild import load_rows, reembed

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vectorstore")

# 任务状态结构（进程内单例）
_IDLE = {
    "state": "idle",     # idle | running | done | failed
    "done": 0,
    "total": 0,
    "error": "",
    "started_at": 0.0,
    "finished_at": 0.0,
}


def _state(request: Request) -> dict:
    st = getattr(request.app.state, "vector_rebuild", None)
    if st is None:
        st = dict(_IDLE)
        request.app.state.vector_rebuild = st
    return st


def _snapshot(st: dict) -> dict:
    pct = round(st["done"] / st["total"] * 100, 1) if st["total"] else 0.0
    return {
        "state": st["state"],
        "done": st["done"],
        "total": st["total"],
        "pct": pct,
        "error": st["error"],
        "started_at": st["started_at"],
        "finished_at": st["finished_at"],
    }


@router.post("/rebuild")
async def start_rebuild(request: Request):
    """以当前生效的 embedding 配置重嵌入全部切片并重建向量库。

    fail-safe：先全量嵌入（失败不动旧库），成功后才 drop 重建。
    返回任务快照；前端轮询 GET /rebuild/status 显示进度。
    """
    st = _state(request)
    if st["state"] == "running":
        raise HTTPException(409, "重建任务正在进行中，请等待完成后再试")

    c = request.app.state.components
    settings = c["settings"]
    embedder = c["embedder"]
    vector_store = c["vector_store"]

    rows = await asyncio.to_thread(load_rows, settings.db_path)
    total = len(rows)
    st.update(
        state="running", done=0, total=total, error="",
        started_at=time.time(), finished_at=0.0,
    )

    async def _run() -> None:
        def _progress(done: int, t: int) -> None:
            st["done"], st["total"] = done, t

        try:
            n = await reembed(rows, embedder, vector_store, progress=_progress)
            st["state"], st["done"] = "done", n
        except asyncio.CancelledError:
            st["state"], st["error"] = "failed", "任务已取消"
        except Exception as e:  # noqa: BLE001（重建失败需完整回显给前端）
            st["state"], st["error"] = "failed", f"{type(e).__name__}: {e}"
            log.exception("vectorstore rebuild failed")
        finally:
            st["finished_at"] = time.time()

    st["_task"] = asyncio.create_task(_run())
    return {"started": True, **_snapshot(st)}


@router.get("/rebuild/status")
async def rebuild_status(request: Request):
    return _snapshot(_state(request))
