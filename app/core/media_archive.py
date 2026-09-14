"""已下载媒体归档（新增能力）：把流水线已下载的音频/视频留存一份副本。

设计要点（与 docs/plans/2026-09-14-media-archive-design.md 一致）：

- **默认关闭**：``save_dir`` 为空直接返回 None，零 I/O、零行为变化；
- **按标题命名 + video_id 保底**：``<清洗后标题>-<video_id><源扩展名>``，
  标题为空时回落为 ``<video_id><扩展名>``，保证唯一且可反查；
- **已存在即跳过**：重复导入 / 任务重试不会重复占盘；
- **异常隔离**：目录不可写、复制失败等只记日志并返回 None，
  绝不影响转写 / 笔记 / 入库主流程（沿用 E1/E3 的隔离原则）；
- **临时目录冲突防护**：若归档目录落在 ``data_dir`` 下的启动清扫目录内，
  拒绝归档并告警——否则 ``sweep_temp_media()`` 会在下次启动时把归档删光。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# 文件系统非法字符（Windows 保留字符 + ASCII 控制符）；换行/制表由 \x00-\x1f 覆盖
_ILLEGAL_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RE = re.compile(r"\s+")

# 标题片段最长字符数（字节长度受 UTF-8 影响，255 字节上限下中文约 60 字，80 字保守留余量）
MAX_TITLE_CHARS = 80

# 启动清扫会清空的临时媒体子目录（与 app/jobs/pipeline.py 的 _TEMP_MEDIA_DIRS 对应）。
# 此处独立定义而非互相 import：pipeline 依赖本模块，反向 import 会形成循环依赖。
TEMP_MEDIA_SUBDIRS = ("downloads", "audio", "transcripts", "frames")


def sanitize_filename(title: str, max_len: int = MAX_TITLE_CHARS) -> str:
    """把视频标题清洗为可安全用作文件名的主体片段。

    - 先把换行/制表等空白字符折叠为单个空格（必须早于非法字符替换，
      否则换行会先变成 ``_``，标题里的换行就会留下 ``a_b`` 而不是 ``a b``）；
    - 非法字符（``<>:"/\\|?*`` 与残余控制符）统一替换为 ``_``；
    - 去除首尾空白与点号（尾随 ``.`` 在 Windows 上不合法）；
    - 截断到 ``max_len`` 字符，截断后再清理一次尾部。
    """
    cleaned = _WHITESPACE_RE.sub(" ", title or "").strip()
    cleaned = _ILLEGAL_CHARS_RE.sub("_", cleaned).strip().strip(". ")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].strip().strip(". ")
    return cleaned


def build_archive_path(
    save_dir: str | Path, video_id: str, title: str | None, src_path: str | Path
) -> Path:
    """计算归档目标路径：``<save_dir>/<标题>-<video_id><源扩展名>``。

    扩展名取源文件真实后缀（音频 mp3 / 视频 mp4 / webm 等），使不同媒体
    类型不会互相覆盖；源文件无后缀时回落 ``.bin``。
    """
    ext = Path(src_path).suffix or ".bin"
    stem = sanitize_filename(title or "")
    name = f"{stem}-{video_id}{ext}" if stem else f"{video_id}{ext}"
    return Path(save_dir) / name


def is_in_temp_media_dir(target_dir: Path, data_dir: str | Path | None) -> bool:
    """判断归档目录是否落在启动清扫的临时媒体目录内（会随启动被清空）。"""
    if not data_dir:
        return False
    try:
        root = Path(data_dir).resolve()
        target = target_dir.resolve()
    except OSError:  # 路径解析异常视为不可判定，放行由后续 IO 报错兜底
        return False
    for sub in TEMP_MEDIA_SUBDIRS:
        try:
            target.relative_to(root / sub)
            return True
        except ValueError:
            continue
    return False


def resolve_archive_dir(
    save_dir: str | None, data_dir: str | Path | None = None
) -> Path | None:
    """校验并解析归档目录：未配置/落在临时目录内/路径非法 → None（附告警）。"""
    if not save_dir:
        return None
    try:
        target = Path(save_dir).resolve()
    except OSError as e:  # 极端路径（超长/非法字符）解析失败
        logger.warning("media archive disabled: invalid dir %r: %s", save_dir, e)
        return None
    if is_in_temp_media_dir(target, data_dir):
        logger.warning(
            "media archive disabled: %s 位于启动清扫的临时媒体目录内，"
            "归档文件会在下次启动时被删除；请改配置到独立目录（如 /media）",
            target,
        )
        return None
    return target


def ensure_archive_dir(save_dir: str | None, data_dir: str | Path | None = None) -> bool:
    """启动校验：已配置时创建目录并探测可写，提前暴露「卷没挂/属主不对」。

    失败只打印告警并返回 False，不阻断启动（归档是可选增强能力，
    且``archive_media`` 每次调用仍会自行兜底）。
    """
    target = resolve_archive_dir(save_dir, data_dir)
    if target is None:
        return False
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / f".write_probe_{os.getpid()}"
        probe.touch()
        probe.unlink()
    except OSError as e:
        logger.warning("media archive dir %s 不可写：%s", target, e)
        return False
    return True


def archive_media(
    src_path: str | None,
    save_dir: str | None,
    video_id: str,
    title: str | None,
    data_dir: str | Path | None = None,
) -> str | None:
    """把已下载的媒体复制一份到归档目录，返回归档后的绝对路径。

    返回 ``None`` 的所有情形（均为「跳过」而非「失败」，不影响主流程）：
    ``save_dir`` 未配置、源文件不存在、目录不可用、目标已存在、复制出错。
    """
    if not save_dir or not src_path:
        return None
    src = Path(src_path)
    if not src.is_file():
        logger.warning("media archive skipped for %s: source missing %s", video_id, src_path)
        return None

    target_dir = resolve_archive_dir(save_dir, data_dir)
    if target_dir is None:
        return None

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("media archive skipped for %s: mkdir %s failed: %s", video_id, target_dir, e)
        return None

    target = build_archive_path(target_dir, video_id, title, src_path)
    if target.exists():
        logger.info("media archive skipped for %s: already exists %s", video_id, target)
        return None
    try:
        # copy2 保留 mtime（与源文件一致，便于按抓取时间排序），复制而非移动：
        # 转写/视觉旁路仍可能读取原临时文件，且失败路径下临时文件仍由 finally 清理。
        shutil.copy2(src, target)
    except OSError as e:
        logger.warning("media archive failed for %s -> %s: %s", video_id, target, e)
        return None
    logger.info("media archived for %s: %s", video_id, target)
    return str(target)
