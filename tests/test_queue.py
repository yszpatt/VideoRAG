import asyncio

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core.queue import Queue, TaskHandler, start_workers
from app.db import init_db, make_session_factory
from app.models import Task


@pytest.fixture
async def session_factory(tmp_path):
    settings = Settings(_env_file=None, data_dir=str(tmp_path))
    engine, factory = make_session_factory(settings)
    await init_db(engine, settings)
    yield factory
    await engine.dispose()


async def test_enqueue_persists_task(session_factory):
    q = Queue(session_factory)
    task_id = await q.enqueue(video_id="v1", type="fetch", payload={"url": "https://x"})
    assert task_id

    async with session_factory() as s:
        row = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
        assert row.video_id == "v1"
        assert row.type == "fetch"
        assert row.status == "pending"


async def test_worker_runs_handler_and_marks_done(session_factory):
    calls = []

    async def handler(payload: dict) -> None:
        calls.append(payload)

    q = Queue(session_factory, handlers={"fetch": TaskHandler(handler)})
    task_id = await q.enqueue(video_id="v1", type="fetch", payload={"n": 1})
    await q.process_one()

    assert calls == [{"n": 1}]
    async with session_factory() as s:
        row = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
        assert row.status == "done"


async def test_handler_failure_marks_failed(session_factory):
    async def handler(payload: dict) -> None:
        raise RuntimeError("boom")

    q = Queue(session_factory, handlers={"fetch": TaskHandler(handler)})
    task_id = await q.enqueue(video_id="v1", type="fetch", payload={})
    await q.process_one()

    async with session_factory() as s:
        row = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
        assert row.status == "failed"
        assert "boom" in row.error


async def test_unknown_type_is_not_consumed(session_factory):
    q = Queue(session_factory)
    task_id = await q.enqueue(video_id="v1", type="nope", payload={})
    await q.process_one()

    async with session_factory() as s:
        row = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
        assert row.status == "pending"  # 无 handler，不应消费


async def test_recover_interrupted_tasks(session_factory):
    async with session_factory() as s:
        s.add(Task(id="stale", video_id="v1", type="fetch", status="running"))
        await s.commit()

    q = Queue(session_factory)
    await q.recover()

    async with session_factory() as s:
        row = (await s.execute(select(Task).where(Task.id == "stale"))).scalar_one()
        assert row.status == "pending"


async def test_start_workers_consumes_queued(session_factory):
    ran = asyncio.Event()

    async def handler(payload: dict) -> None:
        ran.set()

    q = Queue(session_factory, handlers={"fetch": TaskHandler(handler)})
    await q.enqueue(video_id="v1", type="fetch", payload={})
    worker = asyncio.create_task(start_workers(q, concurrency=1))
    await asyncio.wait_for(ran.wait(), timeout=2)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)


async def test_claim_one_is_atomic_under_concurrency(session_factory):
    """同一时刻只有一个 worker 能抢到同一条 pending 任务。

    回归：原实现「SELECT 后直接改 row.status」在多 worker 下会同时读到同一行，
    本用例在修复前稳定复现「4 个 worker 全部认领成功」。
    """
    q = Queue(session_factory, handlers={"fetch": TaskHandler(lambda p: asyncio.sleep(0))})
    await q.enqueue(video_id="v1", type="fetch", payload={"n": 1})

    claimed = [t for t in await asyncio.gather(*(q._claim_one() for _ in range(4))) if t]

    assert len(claimed) == 1
    assert claimed[0].payload == {"n": 1}  # 脱离 session 后仍可读（expunge）
    async with session_factory() as s:
        row = (await s.execute(select(Task).where(Task.id == claimed[0].id))).scalar_one()
        assert row.status == "running"


async def test_concurrent_process_one_runs_handler_once(session_factory):
    """并发消费同一条任务时，handler 只执行一次，其余 worker 空手而归。"""
    calls = []

    async def handler(payload: dict) -> None:
        calls.append(payload)
        await asyncio.sleep(0.05)  # 拉长执行窗口，放大并发踩踏概率

    q = Queue(session_factory, handlers={"fetch": TaskHandler(handler)})
    task_id = await q.enqueue(video_id="v1", type="fetch", payload={"n": 1})

    results = await asyncio.gather(*(q.process_one() for _ in range(3)))

    assert calls == [{"n": 1}]
    assert sorted(results) == [False, False, True]
    async with session_factory() as s:
        row = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
        assert row.status == "done"
