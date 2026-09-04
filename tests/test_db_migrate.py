"""v2 轻量列迁移测试（设计文档 0.4a / M0 任务①）。

模拟真实升级路径：v1 老库（videos 表只有 v1 列）挂载新版数据卷后，
init_db 应幂等补齐 E1 新列且不破坏老数据。
"""

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.db import _REQUIRED_COLUMNS, _ensure_columns, init_db

_NEW_COLS = set(_REQUIRED_COLUMNS["videos"])

# v1 时代的 videos 表结构（无 E1 新列）
_V1_VIDEO_DDL = (
    "CREATE TABLE videos ("
    "id VARCHAR PRIMARY KEY, "
    "platform VARCHAR NOT NULL, "
    "url VARCHAR NOT NULL, "
    "title VARCHAR, "
    "author VARCHAR, "
    "duration_sec FLOAT, "
    "status VARCHAR, "
    "note_path VARCHAR, "
    "error VARCHAR, "
    "created_at DATETIME, "
    "updated_at DATETIME)"
)


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, data_dir=str(tmp_path))


def _engine(settings: Settings):
    # 测试不经 ensure_dirs，手动保证 db 父目录存在
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    return create_async_engine(f"sqlite+aiosqlite:///{settings.db_path}")


async def _table_columns(engine, table: str = "videos") -> list[str]:
    async with engine.connect() as conn:
        result = await conn.execute(text(f"PRAGMA table_info({table})"))
        return [row[1] for row in result.fetchall()]


async def test_old_db_gets_new_columns_with_null_defaults(settings):
    """老库补齐新列，老数据保留且新列默认 NULL。"""
    engine = _engine(settings)
    async with engine.begin() as conn:
        await conn.execute(text(_V1_VIDEO_DDL))
        await conn.execute(
            text("INSERT INTO videos (id, platform, url) VALUES ('v1', 'bilibili', 'https://x')")
        )

    await _ensure_columns(engine)

    assert _NEW_COLS <= set(await _table_columns(engine))
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT description, cover_url, upload_date, view_count, like_count, "
                    "tags, comment_count, meta_source FROM videos WHERE id = 'v1'"
                )
            )
        ).fetchone()
    assert row is not None
    assert all(v is None for v in row)
    await engine.dispose()


async def test_ensure_columns_idempotent(settings):
    """重复执行不报错、不重复加列。"""
    engine = _engine(settings)
    async with engine.begin() as conn:
        await conn.execute(text(_V1_VIDEO_DDL))

    await _ensure_columns(engine)
    cols_first = await _table_columns(engine)
    await _ensure_columns(engine)
    assert await _table_columns(engine) == cols_first
    assert _NEW_COLS <= set(cols_first)
    await engine.dispose()


async def test_init_db_new_database_and_rerun(settings, tmp_path, monkeypatch):
    """新库走完整 init_db（create_all + 迁移）后新列齐备；重复跑不报错。"""
    monkeypatch.chdir(tmp_path)  # ensure_dirs 会 os.chdir(data_dir)，teardown 自动恢复
    engine = _engine(settings)

    await init_db(engine, settings)
    assert _NEW_COLS <= set(await _table_columns(engine))

    await init_db(engine, settings)  # 幂等重跑
    assert _NEW_COLS <= set(await _table_columns(engine))
    await engine.dispose()
