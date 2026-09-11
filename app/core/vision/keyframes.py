"""关键帧提取（E3）：ffmpeg 场景检测帧 + 固定间隔兜底帧 → 指纹去重 → 均匀采样。

两路抽帧的理由：
- **场景检测帧**（`select='gt(scene,0.35)'`）：画面切换点，覆盖 PPT 翻页、镜头切换；
- **固定间隔兜底帧**（每 30s 一帧）：教程/录屏类画面变化慢，纯场景检测会漏帧。

时间戳来源：
- 场景帧从 ffmpeg `showinfo` 日志解析 `pts_time:`（输出顺序与文件序号一致）；
- 兜底帧按 `序号 × 间隔` 推算。

抽帧完成后用 dHash 去掉近似重复帧（同一画面只留一帧），再均匀采样到 max_frames
上限，从而给 OCR 调用次数一个上界（长视频新增耗时可控）。

ffmpeg 子进程可注入（`runner`），便于单元测试不依赖真实 ffmpeg。
"""

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from app.core.vision.phash import DEFAULT_HAMMING_THRESHOLD, dhash, is_similar

logger = logging.getLogger(__name__)

# 场景切换阈值（0~1，越大越严格）；经验值 0.35 能覆盖 PPT 翻页
SCENE_THRESHOLD = 0.35
# 兜底帧间隔（秒）
FALLBACK_INTERVAL_SEC = 30.0
# ffmpeg 抽帧超时（秒）：整段解码是耗时大头，长视频给足余量
DEFAULT_TIMEOUT = 3600.0
# 单帧 JPEG 质量（2=高质量，越小越好）
JPEG_QUALITY = "3"

_PTS_RE = re.compile(r"pts_time:([0-9]*\.?[0-9]+)")

Runner = Callable[[Sequence[str]], object]


@dataclass
class KeyFrame:
    """一个关键帧：文件路径 + 时间戳（秒）+ 指纹（懒计算，可能为 None）。"""

    path: str
    ts: float
    phash: int | None = None


def run_ffmpeg(args: Sequence[str], timeout: float = DEFAULT_TIMEOUT):
    """默认 ffmpeg 执行器（子进程）；capture stderr 供 showinfo 解析。"""
    return subprocess.run(
        ["ffmpeg", *args], capture_output=True, text=True, timeout=timeout
    )


def _parse_showinfo_pts(stderr: str) -> list[float]:
    """从 ffmpeg showinfo 日志按出现顺序提取 pts_time（秒）。"""
    return [float(m) for m in _PTS_RE.findall(stderr or "")]


def _scene_frames(
    video_path: str, workdir: Path, run: Runner, threshold: float
) -> list[KeyFrame]:
    args = [
        "-y",
        "-i", str(video_path),
        "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-vsync", "vfr",
        "-q:v", JPEG_QUALITY,
        str(workdir / "scene_%05d.jpg"),
    ]
    try:
        proc = run(args)
    except Exception as e:  # 无视频流 / ffmpeg 缺失 / 超时：隔离，退回兜底帧
        logger.warning("scene keyframe extraction failed: %s", e)
        return []
    pts = _parse_showinfo_pts(getattr(proc, "stderr", "") or "")
    files = sorted(workdir.glob("scene_*.jpg"))
    frames: list[KeyFrame] = []
    for i, f in enumerate(files):
        # pts 与文件顺序一一对应；解析失败时按 0 兜底（后续排序仍正确）
        ts = pts[i] if i < len(pts) else 0.0
        frames.append(KeyFrame(path=str(f), ts=max(0.0, ts)))
    return frames


def _fixed_frames(
    video_path: str, workdir: Path, run: Runner, interval: float
) -> list[KeyFrame]:
    if interval <= 0:
        return []
    args = [
        "-y",
        "-i", str(video_path),
        "-vf", f"fps=1/{interval:g}",
        "-q:v", JPEG_QUALITY,
        str(workdir / "fixed_%05d.jpg"),
    ]
    try:
        run(args)
    except Exception as e:
        logger.warning("fallback keyframe extraction failed: %s", e)
        return []
    files = sorted(workdir.glob("fixed_*.jpg"))
    return [
        KeyFrame(path=str(f), ts=max(0.0, i * interval))
        for i, f in enumerate(files)
    ]


def _uniform_sample(frames: list[KeyFrame], n: int) -> list[KeyFrame]:
    """按等间隔取 n 帧（保持时间顺序，首尾覆盖）。"""
    if n <= 0 or len(frames) <= n:
        return frames
    step = len(frames) / n
    return [frames[min(int(i * step), len(frames) - 1)] for i in range(n)]


def dedup_frames(
    frames: list[KeyFrame], hamming_threshold: int = DEFAULT_HAMMING_THRESHOLD
) -> list[KeyFrame]:
    """按时间排序后做指纹去重：与上一保留帧近似则丢弃（同一画面只留一帧）。"""
    kept: list[KeyFrame] = []
    last: int | None = None
    for f in sorted(frames, key=lambda x: x.ts):
        h = dhash(f.path)
        f.phash = h
        if is_similar(h, last, hamming_threshold):
            continue
        kept.append(f)
        if h is not None:
            last = h
    return kept


def extract_keyframes(
    video_path: str,
    workdir: str,
    max_frames: int = 60,
    *,
    runner: Runner | None = None,
    scene_threshold: float = SCENE_THRESHOLD,
    interval: float = FALLBACK_INTERVAL_SEC,
) -> list[KeyFrame]:
    """抽帧主入口：场景帧 + 兜底帧 → 去重 → 均匀采样到 max_frames。

    - 任一抽帧路失败只记日志（另一路仍可用）；
    - 视频无画面（纯音频）时两路都失败 → 返回空列表，由调用方跳过视觉层。
    """
    run = runner or run_ffmpeg
    wd = Path(workdir)
    wd.mkdir(parents=True, exist_ok=True)
    scene = _scene_frames(video_path, wd, run, scene_threshold)
    fixed = _fixed_frames(video_path, wd, run, interval)
    kept = dedup_frames(scene + fixed)
    return _uniform_sample(kept, max_frames)
