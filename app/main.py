import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.history import router as history_router
from app.api.prompts import router as prompts_router
from app.api.search import router as search_router
from app.api.settings import router as settings_router
from app.api.vectorstore import router as vectorstore_router
from app.api.videos import router as videos_router
from app.config import Settings
from app.core.fetchers.ytdlp import YtdlpFetcher
from app.core.factory import build_embedder, build_llm, build_transcribers
from app.core.local_models.api import router as models_router
from app.core.local_models.manager import ModelManager
from app.core.prompts import PromptRegistry
from app.core.queue import Queue, TaskHandler, start_workers
from app.core.runtime_config import load_runtime_env
from app.core.vector_store import VectorStore, VectorStoreIncompatible
from app.db import init_db, make_session_factory
from app.jobs.pipeline import process_video, sweep_temp_media
from app.mcp_server import build_mcp_server, mcp_http_middleware


def create_app(
    settings: Settings | None = None,
    session_factory=None,
    queue: Queue | None = None,
    fetchers=None,
    transcribers=None,
    llm=None,
    embedder=None,
    vector_store=None,
    static_dir: str | None = None,
) -> FastAPI:
    settings = settings or Settings()
    # 在线修改的运行时配置（runtime.env）优先级最高
    runtime = load_runtime_env(settings.data_dir)
    if runtime:
        settings = settings.apply_runtime(runtime)

    engine, sf = session_factory or make_session_factory(settings)
    fetchers = fetchers or [YtdlpFetcher(cookie_dir=settings.cookie_dir)]
    transcribers = transcribers or build_transcribers(settings)
    llm = llm or build_llm(settings)
    embedder = embedder or build_embedder(settings)
    vector_store = vector_store or VectorStore(settings.lancedb_path)

    # 组件容器：settings 在线热更新时整组重建并替换
    model_manager = ModelManager(settings)
    components = {
        "settings": settings,
        "fetchers": fetchers,
        "transcribers": transcribers,
        "llm": llm,
        "embedder": embedder,
        "vector_store": vector_store,
        "model_manager": model_manager,
        # 提示词注册表（E2）：prompts.json 热读取，不参与 settings 热替换
        "prompts": PromptRegistry(settings.data_dir),
    }

    async def process_handler(payload: dict) -> None:
        c = components
        await process_video(
            payload["video_id"], sf, c["fetchers"], c["transcribers"], c["llm"],
            c["settings"].data_dir, embedder=c["embedder"], vector_store=c["vector_store"],
            prompts=c["prompts"], cookie_dir=c["settings"].cookie_dir,
        )

    q = queue or Queue(sf, handlers={"process": TaskHandler(process_handler)})

    # MCP（Streamable HTTP，挂载 /mcp）
    mcp_components = {
        "session_factory": sf,
        "vector_store": vector_store,
        "embedder": embedder,
        "llm": llm,
        "queue": q,
    }
    mcp = build_mcp_server(mcp_components)
    mcp_app = mcp.streamable_http_app()
    if settings.mcp_api_key:
        mcp_app = mcp_http_middleware(mcp_app, settings.mcp_api_key, rate_per_minute=60)
    mcp_session_manager = mcp._session_manager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await init_db(engine, settings)
        # LanceDB chunks 表 kind 列幂等迁移：老库无 kind 时整表重建补列（保留向量），
        # 重建会丢 FTS 索引，迁移成功即重建索引。假 store（测试注入）无此方法则跳过。
        try:
            if await asyncio.to_thread(vector_store.ensure_kind):
                await asyncio.to_thread(vector_store.ensure_fts_index)
        except AttributeError:
            pass
        recovered = await q.recover()
        if recovered:
            print(f"[queue] recovered {recovered} interrupted task(s)")
        # 启动清扫临时媒体（downloads/audio/transcripts）：清掉被杀进程/失败路径
        # 遗留的文件。此刻 worker 未启动且 recover 已把 running 重置为 pending，
        # 清扫安全（被重置的任务会重新下载）。
        try:
            swept = await asyncio.to_thread(sweep_temp_media, settings.data_dir)
            if swept:
                print(f"[startup] swept {swept} stale temp media file(s)")
        except Exception as e:  # 清扫失败不应阻断启动
            print(f"[startup] temp media sweep skipped: {e}")
        worker = asyncio.create_task(start_workers(q, settings.max_concurrent_tasks))
        try:
            async with mcp_session_manager.run():  # MCP session 管理器随应用生命周期启停
                yield
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            await engine.dispose()

    app = FastAPI(title="videoRAG", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = sf
    app.state.queue = q
    app.state.components = components
    app.state.fetchers = fetchers
    app.state.transcribers = transcribers
    app.state.llm = llm
    app.state.embedder = embedder
    app.state.vector_store = vector_store
    app.include_router(videos_router)
    app.include_router(search_router)
    app.include_router(settings_router)
    app.include_router(vectorstore_router)
    app.include_router(models_router)
    app.include_router(prompts_router)
    app.include_router(history_router)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    # 向量库与当前 embedding 模型不兼容（维度/指纹不匹配）→ 409 + 可读指引。
    # 检索/问答端点若不拦截，ValueError 会被 FastAPI 默认 500 吞成
    # "Internal Server Error"，前端只看到无意义的失败，无法指引用户重建知识库。
    from fastapi.responses import JSONResponse

    @app.exception_handler(VectorStoreIncompatible)
    async def _vs_incompatible_handler(request, exc: VectorStoreIncompatible):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    # MCP 通过纯 ASGI 中间件拦截 /mcp（scope path 原样传给 FastMCP，避免路由层解析）
    app.add_middleware(MCPRouteMiddleware, mcp_app=mcp_app)
    _mount_static(app, static_dir)

    return app


class MCPRouteMiddleware:
    """把 /mcp 请求直接转发给 MCP ASGI 应用，其余请求走正常 FastAPI 路由。"""

    def __init__(self, app, mcp_app):
        self.app = app
        self.mcp_app = mcp_app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/mcp":
            await self.mcp_app(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def _mount_static(app: FastAPI, static_dir: str | None) -> None:
    """挂载前端构建产物；目录不存在时静默跳过（纯 API 模式）。"""
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    if static_dir is None:
        static_dir = str(Path(__file__).resolve().parent.parent / "web" / "dist")
    if Path(static_dir).is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="spa")


app = create_app()
