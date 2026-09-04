"""本地模型管理 API（/api/models）。

对应 docs/plans/2026-09-03-local-fallback-design.md §3.4 API 草案。
下载/进度状态由后台任务驱动，前端 3s 轮询 GET /api/models 即可。
"""
from __future__ import annotations

import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.local_models import registry
from app.core.local_models.manager import (
    DownloadBusy,
    DownloadConflict,
    DownloadNotFound,
)

router = APIRouter(prefix="/api/models")


def _manager(request: Request):
    return request.app.state.components["model_manager"]


def _settings(request: Request):
    return request.app.state.components["settings"]


@router.get("")
async def list_models(request: Request):
    """全部 kind 状态：installed / manual_path / job（若有）/ 生效档位 / 端点。

    embedding 额外附向量库指纹信息（store_* / compatible / needs_rebuild），
    供前端判断「向量库由哪个模型写入、当前模型是否需要重建」。
    """
    models = _manager(request).status_all()
    _enrich_embedding_compat(request, models)
    return {"models": models}


def _enrich_embedding_compat(request: Request, models: list[dict]) -> None:
    """给 embedding 条目合并向量库兼容信息（数据驱动，替代前端按表单猜测）。

    - store_*：向量库 model.meta.json 里记录的是哪个模型（建库者）
    - active_*：当前生效的 embedding 模型指纹
    - compatible / needs_rebuild：两者是否一致；不一致 → 需重建知识库
    无向量库指纹（首次建档/空库）时 compatible=True（不打扰）。
    """
    emb = next((m for m in models if m["kind"] == "embedding"), None)
    c = request.app.state.components
    if emb is None:
        return
    vs = c.get("vector_store")
    embedder = c.get("embedder")
    try:
        store_meta = vs.get_model_meta() if vs is not None else None
        store_rows = vs.count_rows() if vs is not None else 0
    except Exception:  # noqa: BLE001（测试替身缺方法时静默降级）
        store_meta, store_rows = None, 0
    try:
        fp = dict(embedder.fingerprint) if embedder is not None else {}
    except Exception:  # noqa: BLE001
        fp = {}
    store_provider = (store_meta or {}).get("provider", "")
    store_model = (store_meta or {}).get("model", "")
    store_dim = (store_meta or {}).get("dim")
    active_provider = fp.get("provider", "")
    active_model = fp.get("model", "")
    compatible = store_meta is None or (
        store_provider == active_provider and store_model == active_model
    )
    emb.update(
        {
            "store_provider": store_provider,
            "store_model": store_model,
            "store_dim": store_dim,
            "store_rows": store_rows,
            "active_provider": active_provider,
            "active_model": active_model,
            "compatible": bool(compatible),
            "needs_rebuild": store_meta is not None and not compatible,
        }
    )


class PathBody(BaseModel):
    path: str = ""


@router.get("/{kind}/health")
async def probe_health(kind: str, request: Request):
    """连通性探测（浏览器不可直连容器内 asr:9991 → 服务端代探）。

    asr：GET 本地端点 /health（2.5s 超时）；
    embedding：进程内加载无外部服务 → 回显已装状态。
    """
    if kind not in registry.KINDS:
        raise HTTPException(404, f"unknown kind: {kind}")
    s = _settings(request)
    if kind == "asr":
        endpoint = s.asr_local_endpoint
        url = endpoint.rstrip("/") + "/health"
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=2.5, follow_redirects=True) as c:
                r = await c.get(url)
            return {
                "kind": kind, "ok": r.status_code < 500,
                "endpoint": endpoint, "status": r.status_code,
                "latency_ms": round((time.monotonic() - t0) * 1000),
            }
        except Exception as e:  # noqa: BLE001（连接拒绝/超时 → 服务未起）
            return {
                "kind": kind, "ok": False, "endpoint": endpoint,
                "error": f"{type(e).__name__}: {e}",
            }
    info = next(
        (m for m in _manager(request).status_all() if m["kind"] == kind), {}
    )
    return {
        "kind": kind, "ok": bool(info.get("installed")),
        "installed": info.get("installed"), "endpoint": "",
    }


@router.post("/{kind}/download")
async def download_model(kind: str, request: Request):
    if kind not in registry.KINDS:
        raise HTTPException(404, f"unknown kind: {kind}")
    try:
        job = await _manager(request).start(kind)
    except DownloadBusy as e:
        raise HTTPException(409, str(e)) from e
    except DownloadConflict as e:
        raise HTTPException(400, str(e)) from e
    except KeyError:
        raise HTTPException(404, f"unknown kind: {kind}") from None
    return {"kind": kind, "job": job.snapshot()}


@router.post("/{kind}/cancel")
async def cancel_model(kind: str, request: Request):
    try:
        job = await _manager(request).cancel(kind)
    except DownloadNotFound as e:
        raise HTTPException(404, str(e)) from e
    return {"kind": kind, "job": job.snapshot()}


@router.post("/{kind}/retry")
async def retry_model(kind: str, request: Request):
    try:
        job = await _manager(request).retry(kind)
    except DownloadConflict as e:
        raise HTTPException(400, str(e)) from e
    return {"kind": kind, "job": job.snapshot()}


@router.delete("/{kind}")
async def delete_model(kind: str, request: Request):
    try:
        await _manager(request).delete(kind)
    except DownloadConflict as e:
        raise HTTPException(400, str(e)) from e
    except KeyError:
        raise HTTPException(404, f"unknown kind: {kind}") from None
    return {"kind": kind, "deleted": True}


@router.put("/{kind}/path")
async def set_model_path(kind: str, body: PathBody, request: Request):
    """手动指定本地模型目录（写 runtime.env 持久化 + 组件重建；空串清除）。"""
    if kind not in registry.KINDS:
        raise HTTPException(404, f"unknown kind: {kind}")
    try:
        new_settings = await _manager(request).set_manual_path(kind, body.path)
    except DownloadConflict as e:
        raise HTTPException(400, str(e)) from e
    except KeyError:
        raise HTTPException(404, f"unknown kind: {kind}") from None
    # 同步组件（与 /api/settings PUT 一致）：settings 替换 + embedder/transcribers 重建
    from app.core.factory import build_embedder, build_transcribers

    c = request.app.state.components
    c["settings"] = new_settings
    c["embedder"] = build_embedder(new_settings)
    c["transcribers"] = build_transcribers(new_settings)
    request.app.state.settings = new_settings
    request.app.state.embedder = c["embedder"]
    request.app.state.transcribers = c["transcribers"]
    return {"kind": kind, "manual_path": body.path.strip().rstrip("/") or None}
