"""运行日志 API（设置页「日志」页签）。

- GET    /api/logs?limit=200&level=WARNING&q=关键词   进程内环形缓冲的最近日志
- DELETE /api/logs                                     清空缓冲

缓冲由 ``app.core.logbuf`` 在 ``create_app`` 时挂到 root 与 uvicorn 的 logger 上，
所以这里读到的就是容器/桌面进程自己那份日志（不依赖 docker logs，也不读文件）。

注意：日志可能包含视频标题、URL 等内容，接口与项目其它 /api 一样目前**没有鉴权**
（既有已知项）——若端口对外暴露，请自行加访问控制。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.core.logbuf import MAX_RECORDS, get_log_buffer

router = APIRouter(prefix="/api/logs")


@router.get("")
async def list_logs(
    limit: int = Query(200, ge=1, le=MAX_RECORDS),
    level: str = Query("", description="最低级别：DEBUG/INFO/WARNING/ERROR/CRITICAL"),
    q: str = Query("", description="关键词过滤（匹配内容与 logger 名）"),
):
    buf = get_log_buffer()
    entries = buf.snapshot(limit=limit, level=level or None, q=q or None)
    return {
        "entries": entries,
        "capacity": MAX_RECORDS,
        "buffered": len(buf),
        # 过滤后一共多少条（前端显示"显示 x / 共 y"）
        "matched": len(buf.snapshot(limit=MAX_RECORDS, level=level or None, q=q or None)),
    }


@router.delete("")
async def clear_logs():
    cleared = get_log_buffer().clear()
    return {"cleared": cleared}
