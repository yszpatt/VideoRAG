"""画面文字识别（E3）：RapidOCR（ONNX，纯 CPU）+ 帧间文本去重。

- **懒加载单例**：RapidOCR 首次调用才加载模型（模型随包内置，无需联网），
  之后常驻复用（与 fastembed 同范式）；加载在 `asyncio.to_thread` 中执行，
  不阻塞事件循环。
- **帧间去重**：相邻关键帧文本相似度 ≥ 阈值（默认 0.9）视为同一画面内容
  （同一 PPT 页/同一字幕条），只保留首次出现的帧，避免重复片段污染切片。
"""

import logging
import threading
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

# 帧间文本去重阈值（SequenceMatcher 相似度）
DEFAULT_SIMILARITY = 0.9

_engine = None
_lock = threading.Lock()


def _get_engine():
    """RapidOCR 懒加载单例（线程安全）。"""
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                from rapidocr_onnxruntime import RapidOCR

                _engine = RapidOCR()
    return _engine


def ocr_frame(path: str) -> str:
    """识别单帧画面文字；失败返回空串（调用方跳过该帧）。"""
    try:
        engine = _get_engine()
        result, _elapse = engine(path)
    except Exception as e:
        logger.warning("OCR failed for %s: %s", path, e)
        return ""
    if not result:
        return ""
    parts = []
    for item in result:
        # RapidOCR 返回 [[box, text, score], ...]
        if isinstance(item, (list, tuple)) and len(item) > 1 and item[1]:
            parts.append(str(item[1]).strip())
    return " ".join(p for p in parts if p).strip()


def similarity(a: str, b: str) -> float:
    """两段文本的相似度（0~1）；任一为空返回 0。"""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def is_duplicate_text(a: str, b: str, threshold: float = DEFAULT_SIMILARITY) -> bool:
    """相邻帧文本是否重复（同一画面内容）。"""
    return similarity(a, b) >= threshold
