"""模型下载器：httpx 流式下载（.part + Range 续传 + sha256 校验）。

- 文件下载到 ``<dest>.part``，完成后改名；已存在 ``.part`` 时从已有字节发起 Range 请求续传；
- 下载完成、改名落定前比对 sha256（不符 → 删除 .part 抛错）；
- 服务端不支持 Range（无 206）时降级整文件重下（续传无用则从头拉）；
- 取消（cancel_event set）抛 asyncio.CancelledError，保留 .part 供 retry 续传。

设计来源：docs/plans/2026-09-03-local-fallback-design.md §3.4 / D4。
"""
from __future__ import annotations

import asyncio
import hashlib
import shutil
import tempfile
from pathlib import Path

import httpx

_CHUNK = 256 * 1024


class DownloadError(RuntimeError):
    """下载失败（网络 / 磁盘 / 校验）。message 面向用户可读。"""


async def download_file(
    url: str,
    dest: Path,
    *,
    expected_sha256: str = "",
    cancel_event: asyncio.Event | None = None,
    progress: object | None = None,
    timeout: float = 900.0,
) -> int:
    """下载 url 到 dest（带 .part 断点续传与可选 sha256 校验）。返回最终字节数。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    # 已完整下载且（可选）校验通过 → 幂等直接返回；校验不符删除重下
    if dest.is_file():
        if not expected_sha256 or _sha256(dest) == expected_sha256:
            return dest.stat().st_size
        dest.unlink()

    offset = part.stat().st_size if part.is_file() else 0
    timeout_cfg = httpx.Timeout(timeout, connect=30.0)

    async with httpx.AsyncClient(timeout=timeout_cfg, follow_redirects=True) as client:
        # 至多两轮：第一轮带 Range 续传；服务端不支持时（返回 200）第二轮整下。
        for attempt in (1, 2):
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code == 416:
                    break  # 服务端认为 .part 已完整：直接走校验
                if resp.status_code == 206 and offset:
                    mode = "ab"  # 续传
                elif resp.status_code == 200:
                    if attempt == 2 or offset == 0:
                        mode = "wb"
                        offset = 0  # 无 Range 支持 → 从头写
                    else:
                        offset = 0  # 第一轮返回 200（忽略 Range）→ 清 offset 重试整下
                        with open(part, "wb"):
                            pass
                        continue
                else:
                    raise DownloadError(
                        f"download failed: HTTP {resp.status_code} for {url}"
                    )
                with open(part, mode) as fp:
                    async for chunk in resp.aiter_bytes(_CHUNK):
                        if cancel_event is not None and cancel_event.is_set():
                            raise asyncio.CancelledError()
                        fp.write(chunk)
                        offset += len(chunk)
                        if progress is not None:
                            progress(offset)
            break  # 流式读完即本轮成功（httpx 会在断连时抛异常而非静默截断）

    # sha256 校验（.part 完整后、改名落定前）
    if expected_sha256:
        if _sha256(part) != expected_sha256:
            part.unlink(missing_ok=True)
            raise DownloadError(
                f"sha256 mismatch for {dest.name}: 下载文件损坏或源已变更，已清理 .part 请重试"
            )
    _ensure_free(dest.parent, part.stat().st_size)
    part.rename(dest)
    return dest.stat().st_size


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _ensure_free(dirpath: Path, need: int) -> None:
    """目标分区剩余空间不足 → 抛可读错误。"""
    try:
        usage = shutil.disk_usage(dirpath)
        if usage.free < need:
            raise DownloadError(
                f"磁盘空间不足：需要 {need / 1e6:.0f} MB，剩余 {usage.free / 1e6:.0f} MB"
            )
    except DownloadError:
        raise
    except OSError:
        pass  # 分区不可探测时跳过预检（不阻塞下载）


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """小文件（如 tokens.txt）原子写入。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(path.parent), delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    _ensure_free(path.parent, len(data))
    tmp_path.rename(path)
