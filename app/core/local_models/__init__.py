"""本地降级模型管理（local_models 包）。

- registry：本地模型静态注册表（asr: sherpa SenseVoice int8；embedding: fastembed bge-small-zh）
- downloader：httpx 流式下载（.part + Range 续传 + sha256）
- manager：任务状态机 + 互斥 + 进度（进程内单例）
- api：/api/models 路由

设计来源：docs/plans/2026-09-03-local-fallback-design.md §3.4。
"""
