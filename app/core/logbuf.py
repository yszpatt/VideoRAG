"""进程内日志环形缓冲：供设置页「日志」页签查看。

为什么不用现成的日志文件：Docker 形态下应用日志只落在容器 stdout（容器里没有 log 文件
可读，`docker logs` 也只能从宿主机跑）；桌面形态虽然会写 ``<data>/logs/videorag.log``，
但「设置页能看日志」要在所有形态下都成立，最简做法就是在进程内留一份环形缓冲。

- 只保留最近 ``MAX_RECORDS`` 条，内存占用可控（每条几百字节量级）；
- 安装时同时挂到 root 与 uvicorn 的 logger 上：uvicorn 默认 ``propagate=False``，
  只挂 root 会漏掉它的记录；刻意不接 ``uvicorn.access``（轮询噪声）见下；
- 幂等：``create_app`` 在测试里会被反复调用，重复安装不产生重复记录；
- 缓冲自身绝不抛异常：日志链路把业务搞挂是最糟的失败模式。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

MAX_RECORDS = 1000
MAX_MESSAGE_CHARS = 4000
MAX_EXC_CHARS = 1500

_LEVEL_ORDER = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

# 挂缓冲的 logger。
# - root：应用自己的 logger（app.*）与第三方库都靠它；
# - "uvicorn"：它是 uvicorn.error 的父 logger 且 propagate=False，挂它既能收到
#   uvicorn.error（启动/异常），又不会经由 root 再记一遍。
#   ⚠️ 不要再单独挂 "uvicorn.error"：它本身 propagate 到 root，会记两遍（实测过）。
# - 刻意**不含 uvicorn.access**：Web UI 在前端按秒级轮询（列表 3s、下载进度 2.5s…），
#   接进来的话缓冲会被「GET /api/videos」这类访问日志淹掉，真正有用的应用日志被挤出去。
#   访问日志仍在容器 stdout（docker logs）里，不影响排障。
_ATTACH_LOGGERS = ("", "uvicorn")

# 出站 HTTP 客户端（调用 LLM / ASR / 下载模型都会走它）在 INFO 级逐条打印请求，
# 同样会把缓冲刷满；压到 WARNING：失败仍可见，逐条请求不再记录。
_NOISY_LOGGERS = ("httpx", "httpcore")


class LogBuffer:
    """线程安全的定长日志缓冲（最新 N 条）。"""

    def __init__(self, maxlen: int = MAX_RECORDS):
        self._records: deque[dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0

    def add(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # 格式化失败（参数不匹配等）也要留痕，不能丢
            msg = str(record.msg)
        if record.exc_info:
            try:
                exc = logging.Formatter().formatException(record.exc_info)
                msg = f"{msg}\n{exc[-MAX_EXC_CHARS:]}"
            except Exception:
                pass
        with self._lock:
            self._seq += 1
            self._records.append(
                {
                    "seq": self._seq,
                    "ts": time.strftime("%H:%M:%S", time.localtime(record.created))
                    + f".{int(record.msecs):03d}",
                    "date": time.strftime("%Y-%m-%d", time.localtime(record.created)),
                    "level": record.levelname,
                    "logger": record.name,
                    "msg": msg[:MAX_MESSAGE_CHARS],
                }
            )

    def snapshot(
        self, limit: int = 200, level: str | None = None, q: str | None = None
    ) -> list[dict]:
        """取最近的日志（可按最低级别与关键词过滤），返回最新在后的列表。"""
        with self._lock:
            rows = list(self._records)
        if level:
            min_level = _LEVEL_ORDER.get(str(level).upper(), 0)
            rows = [r for r in rows if _LEVEL_ORDER.get(r["level"], 0) >= min_level]
        if q:
            needle = str(q).lower()
            rows = [
                r
                for r in rows
                if needle in r["msg"].lower() or needle in r["logger"].lower()
            ]
        return rows[-max(1, limit) :]

    def clear(self) -> int:
        with self._lock:
            n = len(self._records)
            self._records.clear()
            return n

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


class _BufferHandler(logging.Handler):
    def __init__(self, buffer: LogBuffer, level: int):
        super().__init__(level=level)
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buffer.add(record)
        except Exception:  # noqa: BLE001  日志处理器绝不把业务搞挂
            pass


_buffer = LogBuffer()
_installed = False


def install_log_buffer(level: int = logging.INFO) -> LogBuffer:
    """把环形缓冲挂到 root 与 uvicorn 的 logger 上（幂等）。

    同时把 root logger 的级别压到 ``level``：Python 默认 root 是 WARNING，而应用的
    ``logger.info(...)``（启动清扫、任务恢复、抓取进度等）都在这条线上——不setLevel
    的话「日志」页只能看到 WARNING 以上，等于缺掉大半信息。只在 root 级别高于目标
    级别时才改，不动刻意调低的配置（例如 pytest 的 caplog）。
    """
    global _installed
    if _installed:
        return _buffer
    handler = _BufferHandler(_buffer, level)
    for name in _ATTACH_LOGGERS:
        logging.getLogger(name).addHandler(handler)
    # uvicorn 只在真正跑 uvicorn 时才会应用它的 dictConfig（那里给 uvicorn/uvicorn.access
    # 设了 propagate=False）。测试或嵌入式运行下这份配置不存在，就会出现「同一条日志在
    # 容器里记 1 次、在测试里记 2 次」。这里显式对齐，行为与运行环境无关：
    # - uvicorn：接收 uvicorn.error 的传播后不再上传（避免被 root 再记一遍）
    # - uvicorn.access：不接入缓冲（轮询噪声），也不再往 root 传
    for name in ("uvicorn", "uvicorn.access"):
        logging.getLogger(name).propagate = False
    for name in _NOISY_LOGGERS:
        noisy = logging.getLogger(name)
        if noisy.level == logging.NOTSET or noisy.level < logging.WARNING:
            noisy.setLevel(logging.WARNING)
    root = logging.getLogger()
    if root.level > level:
        root.setLevel(level)
    _installed = True
    return _buffer


def get_log_buffer() -> LogBuffer:
    return _buffer
