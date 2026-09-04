import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from sqlalchemy import select, update

from app.models import Task

logger = logging.getLogger(__name__)

HandlerFn = Callable[[dict], Awaitable[None]]


@dataclass
class TaskHandler:
    """包装一个异步任务处理函数。"""

    fn: HandlerFn

    async def __call__(self, payload: dict) -> None:
        await self.fn(payload)


class Queue:
    """基于 SQLite 持久化 + 进程内轮询的任务队列（单容器场景，不引入 Redis）。"""

    def __init__(self, session_factory, handlers: dict[str, TaskHandler] | None = None):
        self._session_factory = session_factory
        self._handlers = handlers or {}

    async def enqueue(self, video_id: str, type: str, payload: dict | None = None) -> str:
        task = Task(video_id=video_id, type=type, payload=payload)
        async with self._session_factory() as s:
            s.add(task)
            await s.commit()
            return task.id

    async def _claim_one(self) -> Task | None:
        """原子抢占最早一条 pending 任务并置为 running；无对应 handler 则不抢占。"""
        async with self._session_factory() as s:
            row = (
                await s.execute(
                    select(Task)
                    .where(Task.status == "pending")
                    .order_by(Task.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None or row.type not in self._handlers:
                return None
            row.status = "running"
            await s.commit()
            return row

    async def process_one(self) -> bool:
        task = await self._claim_one()
        if task is None:
            return False
        handler = self._handlers[task.type]
        async with self._session_factory() as s:
            current = await s.get(Task, task.id)
            try:
                await handler(current.payload or {})
                current.status = "done"
                current.progress = 1.0
            except Exception as e:
                current.status = "failed"
                current.error = str(e)
                logger.exception("task %s (%s) failed", task.id, task.type)
            await s.commit()
        return True

    async def recover(self) -> int:
        """崩溃恢复：把遗留的 running 任务重置为 pending 重新入队。"""
        async with self._session_factory() as s:
            result = await s.execute(
                update(Task)
                .where(Task.status == "running")
                .values(status="pending", progress=0)
            )
            await s.commit()
            return result.rowcount


async def start_workers(queue: Queue, concurrency: int = 1) -> None:
    """后台 worker：持续消费 pending 任务。"""

    async def _loop() -> None:
        while True:
            if not await queue.process_one():
                await asyncio.sleep(0.2)

    await asyncio.gather(*[_loop() for _ in range(concurrency)])
