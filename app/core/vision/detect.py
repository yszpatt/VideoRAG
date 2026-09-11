"""无语音判定（E3 视觉旁路触发条件）。

判定口径：语音密度 = 转写文本总字数 / 时长 × 60（字/分钟）。
低于阈值（默认 10 字/分钟）即视为「无对白视频」，需要走画面信息采集。

设计要点（防误触发）：
- 时长优先取传入的 `duration_sec`（生产为 E1 采集的 `Video.duration_sec`），
  兜底取转写末段 `end_sec`；
- **时长无法确定时一律返回 False**（保守）：宁可不触发视觉旁路，也不要
  在信息不足时对正常视频做昂贵的抽帧 + OCR。
"""

from app.core.transcribers.base import Segment

DEFAULT_MIN_WPM = 10


def _resolve_duration(
    segments: list[Segment], duration_sec: float | None
) -> float | None:
    """确定判定用时长的秒数：显式时长优先，其次转写末段 end_sec。"""
    if duration_sec is not None:
        try:
            if float(duration_sec) > 0:
                return float(duration_sec)
        except (TypeError, ValueError):
            pass
    end = max((s.end_sec for s in segments), default=0.0) or 0.0
    return end if end > 0 else None


def speech_density(
    segments: list[Segment], duration_sec: float | None
) -> float | None:
    """语音密度（字/分钟）；时长不可用时返回 None。"""
    duration = _resolve_duration(segments, duration_sec)
    if duration is None:
        return None
    chars = sum(len(s.text or "") for s in segments)
    return chars / duration * 60.0


def is_speechless(
    segments: list[Segment],
    duration_sec: float | None = None,
    min_wpm: float = DEFAULT_MIN_WPM,
) -> bool:
    """是否判定为无语音视频（语音密度低于 min_wpm）。

    - 空转写 + 有时长 → 密度 0 → True（纯画面视频的典型形态）；
    - 时长不可用（无 duration 且无 segments） → False（保守不触发）。
    """
    density = speech_density(segments, duration_sec)
    if density is None:
        return False
    return density < float(min_wpm)
