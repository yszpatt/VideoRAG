import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from sqlalchemy import select, update

from app.models import Task

logger = logging.getLogger(__name__)

HandlerFn = Callable[[dict], Awaitable[None]]

# 抢占落空（候选行已被其他 worker 抢走）后的重选次数。每次重选都重新读
# 当前最早的 pending 行，正常竞争下 1~2 次即成功；连续落空说明并发很高，
# 直接返回 None 让 worker 让出事件循环再试。
_CLAIM_RETRIES = 3


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
        """原子抢占最早一条 pending 任务并置为 running；无对应 handler 则不抢占。

        并发安全（重要）：候选行仍是「先 SELECT」选出，但落库改为**带
        ``status='pending'`` 条件的 UPDATE**，并以 ``rowcount == 1`` 作为
        「本 worker 抢到了」的判据。

        原实现（SELECT 出行对象后直接改 ``row.status = "running"``）不是原子的：
        ``MAX_CONCURRENT_TASKS > 1`` 时多个 worker 会同时读到同一条 pending，
        各自 commit 后都认为任务归自己，同一个视频被并发处理多次（重复下载、
        重复入库、Note.video_id 唯一约束冲突）。SQLite 下 UPDATE 自带写锁，
        条件更新能让只有一个 worker 拿到 rowcount=1。

        抢占落空（rowcount == 0）说明该行已被别人拿走，重选下一条；连续
        ``_CLAIM_RETRIES`` 次落空则返回 None，由 worker 循环稍后再试。
        """
        for _ in range(_CLAIM_RETRIES):
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
                claimed = await s.execute(
                    update(Task)
                    .where(Task.id == row.id, Task.status == "pending")
                    .values(status="running")
                )
                # 先脱离 session 再提交/返回：调用方在本 session 关闭后仍要读
                # id/type/payload，expunge 保证这些已加载的值不会被过期重取
                # （不依赖 session 是否配了 expire_on_commit=False）。
                s.expunge(row)
                await s.commit()
                if claimed.rowcount == 1:
                    return row
            # 落空：该候选已被其他 worker 抢走，重新选下一条
            logger.debug("task claim lost a race, retrying")
        return None

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
