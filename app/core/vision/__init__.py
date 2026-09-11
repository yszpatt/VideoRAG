"""视觉旁路（E3）：无语音视频的画面信息采集。

图层结构（每层独立容错，任一层失败不影响其它层）：
- 本地 OCR 层（默认可用）：抽帧 → 指纹去重 → 画面文字识别 → 帧间文本去重；
- 云 VLM 层（预留接口，默认未接入）：对关键帧生成画面描述。

产出为带时间戳、`source` 为 `ocr`/`vlm` 的 `Segment` 列表，由 `merge_transcripts`
与语音转写合并后，复用既有「切片 → 笔记 → 向量入库」链路。

入口：
- `is_speechless`：无语音判定（触发条件）；
- `run_visual_pipeline`：执行视觉采集；
- `merge_transcripts`：把视觉段并入转写（时间排序 + 区间修正）。
"""

import asyncio
import logging

from app.core.transcribers.base import Segment, Transcript
from app.core.vision.detect import DEFAULT_MIN_WPM, is_speechless, speech_density
from app.core.vision.keyframes import KeyFrame, extract_keyframes
from app.core.vision.ocr import DEFAULT_SIMILARITY, is_duplicate_text, ocr_frame

logger = logging.getLogger(__name__)

# 单帧识别文本的截断上限（防个别帧整屏文字过长）
TEXT_LIMIT = 2000

__all__ = [
    "DEFAULT_MIN_WPM",
    "is_speechless",
    "speech_density",
    "extract_keyframes",
    "KeyFrame",
    "run_visual_pipeline",
    "merge_transcripts",
]


async def run_visual_pipeline(
    video_path: str,
    workdir: str,
    *,
    max_frames: int = 60,
    vlm=None,
    text_similarity: float = DEFAULT_SIMILARITY,
) -> list[Segment]:
    """执行视觉采集，返回 `source` 为 ocr/vlm 的带时间戳片段。

    Args:
        video_path: 已下载的视频文件路径；空则直接返回空列表。
        workdir: 抽帧临时目录（由调用方负责清理）。
        max_frames: 关键帧数上限。
        vlm: 画面描述层实现（需有 `describe(path) -> str`）；None = 不启用。
        text_similarity: 帧间文本去重阈值。

    任何一层失败只记日志并跳过该层，绝不抛错（调用方仍有语音转写可用）。
    """
    if not video_path:
        return []
    try:
        frames = await asyncio.to_thread(
            extract_keyframes, video_path, workdir, max_frames
        )
    except Exception as e:  # 抽帧整体失败：视觉层整体跳过
        logger.warning("keyframe extraction failed: %s", e)
        return []
    if not frames:
        return []

    segments = await _ocr_segments(frames, text_similarity)
    segments += await _vlm_segments(frames, vlm)
    return _fix_zero_ranges(segments)


async def _ocr_segments(
    frames: list[KeyFrame], similarity_threshold: float
) -> list[Segment]:
    """OCR 层：逐帧识别 + 帧间文本去重（同画面只留首次出现的帧）。"""
    out: list[Segment] = []
    last_text = ""
    for f in frames:
        text = (await asyncio.to_thread(ocr_frame, f.path) or "").strip()[:TEXT_LIMIT]
        if not text:
            continue  # 无文字画面（纯图像/空屏）不入库
        if is_duplicate_text(text, last_text, similarity_threshold):
            continue  # 与上一保留帧同一画面内容 → 跳过
        last_text = text
        out.append(Segment(start_sec=f.ts, end_sec=f.ts, text=text, source="ocr"))
    return out


async def _vlm_segments(frames: list[KeyFrame], vlm) -> list[Segment]:
    """VLM 层（预留）：未注入实现时直接跳过；已注入时逐帧描述。"""
    if vlm is None:
        return []
    out: list[Segment] = []
    for f in frames:
        try:
            desc = (await vlm.describe(f.path) or "").strip()[:TEXT_LIMIT]
        except Exception as e:  # 单帧失败跳过，不影响其它帧
            logger.warning("vlm describe failed for %s: %s", f.path, e)
            continue
        if not desc:
            continue
        out.append(Segment(start_sec=f.ts, end_sec=f.ts, text=desc, source="vlm"))
    return out


def _fix_zero_ranges(segments: list[Segment]) -> list[Segment]:
    """把零长度视觉段的 end_sec 补到下一段起点（末段留待 merge 用时长兜底）。"""
    segs = sorted(segments, key=lambda s: s.start_sec)
    for i, s in enumerate(segs):
        if s.end_sec > s.start_sec:
            continue
        if i + 1 < len(segs) and segs[i + 1].start_sec > s.start_sec:
            s.end_sec = segs[i + 1].start_sec
    return segs


def merge_transcripts(
    transcript: Transcript,
    visual_segments: list[Segment],
    duration_sec: float | None = None,
) -> Transcript:
    """把视觉段并入转写：按时间排序，零长度段补 end_sec。

    - 不改动语音段已有的 end_sec（ASR 时间戳本身可靠，避免误缩区间）；
    - 视觉段（end_sec == start_sec）用「下一个片段的起点」补全，末段用视频时长；
    - 无视觉段时原样返回（零开销）。
    """
    if not visual_segments:
        return transcript
    segs = sorted([*transcript.segments, *visual_segments], key=lambda s: s.start_sec)
    for i, s in enumerate(segs):
        if s.end_sec > s.start_sec:
            continue
        nxt = segs[i + 1].start_sec if i + 1 < len(segs) else None
        if nxt is None:
            nxt = duration_sec
        if nxt is not None and nxt > s.start_sec:
            s.end_sec = nxt
    return Transcript(
        segments=segs,
        raw_text=transcript.raw_text,
        source=transcript.source,
        meta=transcript.meta,
    )
