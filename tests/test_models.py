import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from app.models import Base, Chunk, Note, Segment, Task, Video


@pytest.fixture
async def session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/mem.sqlite")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def test_create_video_with_relations(session):
    video = Video(id="v1", platform="bilibili", url="https://b23.tv/x", title="t")
    session.add(video)
    await session.flush()
    session.add_all(
        [
            Task(id="t1", video_id="v1", type="fetch"),
            Segment(id="s1", video_id="v1", start_sec=0.0, end_sec=3.5, text="hello"),
            Chunk(id="c1", video_id="v1", content="hello world", start_sec=0.0, end_sec=3.5),
            Note(id="n1", video_id="v1", summary="sum"),
        ]
    )
    await session.commit()

    v = (
        await session.execute(
            select(Video).where(Video.id == "v1").options(selectinload(Video.tasks))
        )
    ).scalar_one()
    assert v.status == "pending"
    assert len(v.tasks) == 1
    assert v.created_at is not None
    assert v.tasks[0].status == "pending"
    assert v.tasks[0].progress == 0


async def test_video_status_transition(session):
    session.add(Video(id="v2", platform="youtube", url="https://youtu.be/x"))
    await session.commit()
    v = await session.get(Video, "v2")
    v.status = "done"
    await session.commit()
    v = await session.get(Video, "v2")
    assert v.status == "done"


async def test_make_session_factory_and_init_db(tmp_path):
    from app.config import Settings
    from app.db import init_db, make_session_factory

    settings = Settings(_env_file=None, data_dir=str(tmp_path))
    engine, session_factory = make_session_factory(settings)
    await init_db(engine, settings)

    assert (tmp_path / "db").is_dir()
    assert (tmp_path / "lancedb").is_dir()

    async with session_factory() as s:
        s.add(Video(id="v9", platform="generic", url="u"))
        await s.commit()
        v = await s.get(Video, "v9")
        assert v is not None
    await engine.dispose()
