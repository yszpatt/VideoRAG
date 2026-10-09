"""运行日志 API（/api/logs）测试。

缓冲由 create_app 挂到 root/uvicorn logger 上，因此这里直接用标准 logging 写日志，
再断言接口能读到、能按级别与关键词过滤、能带出异常堆栈、能清空。
"""

import logging


async def test_logs_records_and_filters(client):
    logging.getLogger("app.test.alpha").warning("alpha 警告 %s", 1)
    logging.getLogger("app.test.beta").error("beta 错误")

    d = (await client.get("/api/logs?limit=100")).json()
    msgs = [e["msg"] for e in d["entries"]]
    assert "alpha 警告 1" in msgs  # 占位符已被格式化
    assert "beta 错误" in msgs
    assert d["capacity"] > 0 and d["buffered"] >= 2
    assert d["matched"] >= 2

    entry = d["entries"][-1]
    assert {"seq", "ts", "date", "level", "logger", "msg"} <= set(entry)

    # 级别过滤：WARNING 及以上
    warn = (await client.get("/api/logs?level=WARNING")).json()["entries"]
    assert all(e["level"] in ("WARNING", "ERROR", "CRITICAL") for e in warn)
    assert "alpha 警告 1" in [e["msg"] for e in warn]
    info_only = (await client.get("/api/logs?level=ERROR")).json()["entries"]
    assert "alpha 警告 1" not in [e["msg"] for e in info_only]

    # 关键词过滤命中内容
    beta = (await client.get("/api/logs?q=beta")).json()["entries"]
    assert [e["msg"] for e in beta] == ["beta 错误"]
    # 关键词也命中 logger 名
    by_logger = (await client.get("/api/logs?q=app.test.alpha")).json()["entries"]
    assert any("alpha 警告 1" == e["msg"] for e in by_logger)


async def test_logs_records_exception_traceback(client):
    """logger.exception 的堆栈要留得住：排查「视频 failed」时全靠它。"""
    try:
        raise ValueError("boom-detail-123")
    except ValueError:
        logging.getLogger("app.test.exc").exception("处理失败")

    entries = (await client.get("/api/logs?q=boom-detail-123")).json()["entries"]
    assert entries, "异常日志应可检索"
    assert "Traceback" in entries[-1]["msg"]
    assert "ValueError" in entries[-1]["msg"]


async def test_logs_limit_and_clear(client):
    for i in range(5):
        logging.getLogger("app.test.bulk").info("bulk-%d", i)

    limited = (await client.get("/api/logs?limit=2")).json()["entries"]
    assert len(limited) == 2

    cleared = (await client.delete("/api/logs")).json()
    assert cleared["cleared"] >= 5

    after = (await client.get("/api/logs")).json()
    # 清空后不该再有刚才那些条目（可能残留测试框架/httpx 自己的一两条，不影响判断）
    assert all("bulk-" not in e["msg"] for e in after["entries"])


async def test_logs_limit_is_validated(client):
    assert (await client.get("/api/logs?limit=0")).status_code == 422
    assert (await client.get("/api/logs?limit=99999")).status_code == 422


async def test_logs_not_recorded_twice_for_propagating_loggers(client):
    """uvicorn.error 会向 root 传播：handler 只能挂一处，否则每条日志记两遍。

    回归：曾同时挂 root / uvicorn / uvicorn.error，容器实测「Application startup
    complete.」出现两次。
    """
    logging.getLogger("uvicorn.error").info("dedupe-probe-xyz")

    entries = (await client.get("/api/logs?q=dedupe-probe-xyz&limit=50")).json()["entries"]
    assert len(entries) == 1, f"应只记录一次，实际 {len(entries)} 次"


async def test_access_logs_are_not_buffered(client):
    """uvicorn.access（前端轮询噪声）不进缓冲，避免把有效日志挤掉。"""
    logging.getLogger("uvicorn.access").info('127.0.0.1 - "GET /api/videos HTTP/1.1" 200')
    entries = (await client.get("/api/logs?q=GET /api/videos")).json()["entries"]
    assert entries == []
