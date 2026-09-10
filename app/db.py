import asyncio
import os
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings
from app.models import Base

# 与设计文档第 6 节目录结构一致
SUBDIRS = ("db", "lancedb", "models", "cookies", "downloads", "audio", "transcripts", "notes", "thumbnails")

# v2 轻量列迁移（设计文档 0.4a）：老库已有表缺 ORM 新列时补齐。
# 表驱动而非 SQL 迁移文件——字段与 ORM 定义一一对应，缺失即补，幂等可重跑。
# 新表（Comment、QueryHistory 等）由 create_all 自动创建，不走这里。
_REQUIRED_COLUMNS: dict[str, dict[str, str]] = {
    "videos": {
        "description": "TEXT",
        "cover_url": "TEXT",
        "upload_date": "TEXT",
        "view_count": "INTEGER",
        "like_count": "INTEGER",
        "tags": "JSON",
        "comment_count": "INTEGER",
        "meta_source": "TEXT",
    },
    "chunks": {
        # 老库 chunk 镜像表补 kind（新库由 create_all 建全）。NOT NULL + 常量默认
        # 使 SQLite 允许直接 ADD COLUMN，存量行自动回填 content。
        "kind": "TEXT NOT NULL DEFAULT 'content'",
    },
}


def ensure_dirs(settings: Settings) -> None:
    for sub in SUBDIRS:
        Path(settings.data_dir, sub).mkdir(parents=True, exist_ok=True)
    os.chdir(settings.data_dir)


def make_session_factory(settings: Settings) -> tuple[AsyncEngine, async_sessionmaker]:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{settings.db_path}",
        connect_args={"timeout": 15},  # 写锁等待，避免瞬时锁冲突直接报错
    )
    _apply_sqlite_pragmas(engine)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, session_factory


def _apply_sqlite_pragmas(engine: AsyncEngine) -> None:
    """连接级 PRAGMA 调优（每次新建连接都执行）。

    - journal_mode=WAL：写事务不再 rewrite 整个 db 文件（默认 DELETE 模式会产生
      `db-journal` 并在提交时重写），显著降低笔记/评论/切片频繁增删时的磁盘写放大；
      WAL 下读写并发也更好（读不阻塞写）。
    - synchronous=NORMAL：WAL 模式下的推荐值，安全（崩溃最多丢最后一个事务）且更快。
    - cache_size=-16000：约 16MB 页缓存（负值 = KiB），减少热数据的磁盘往返。
    - busy_timeout=15000：与 connect_args timeout 一致，写锁竞争时等待而非立即失败。
    """
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA cache_size=-16000")
            cur.execute("PRAGMA busy_timeout=15000")
        finally:
            cur.close()


async def _ensure_columns(engine: AsyncEngine) -> None:
    """对已存在的表补齐缺失列（幂等）。

    必须在 create_all 之后调用：新库建表即含全部列（此处空转）；
    老库的已有表 create_all 不会动，由这里 ALTER TABLE 补列。
    表不存在时跳过（防御：直接调用本函数而未经 create_all 的场景）。
    """
    async with engine.begin() as conn:
        for table, cols in _REQUIRED_COLUMNS.items():
            result = await conn.execute(text(f"PRAGMA table_info({table})"))
            existing = {row[1] for row in result.fetchall()}
            if not existing:
                continue
            for col, ddl in cols.items():
                if col not in existing:
                    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))


async def init_db(engine: AsyncEngine, settings: Settings) -> None:
    await asyncio.to_thread(ensure_dirs, settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _ensure_columns(engine)
