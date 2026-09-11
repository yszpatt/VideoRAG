from dataclasses import dataclass

from app.core.transcribers.base import Segment


@dataclass
class Chunk:
    content: str
    start_sec: float
    end_sec: float
    # E3：切片来源（由构成片段推断）——speech|subtitle|ocr|vlm|mixed
    src: str = "speech"


def _chunk_src(seg_list: list[Segment]) -> str:
    """切片来源推断：含语音/字幕片段时以语音为主，纯画面片段标其视觉来源。

    混合切片（语音 + 画面）标 speech：语音是主内容，视觉段只是补充。
    """
    if not seg_list:
        return "speech"
    sources = {s.source for s in seg_list}
    if sources & {"speech", "subtitle"}:
        return "speech" if "speech" in sources else "subtitle"
    if len(sources) == 1:
        return next(iter(sources))
    return "mixed"


def chunk_segments(
    segments: list[Segment],
    max_chars: int = 500,
    min_chars: int = 200,
    overlap_chars: int = 50,
) -> list[Chunk]:
    """把带时间戳的转写分段聚合成语义切片。

    - 累积到 max_chars 且已超过 min_chars 时切分
    - 下一块保留尾部 overlap_chars 作为重叠
    - 单段超长按 max_chars 硬切
    """
    chunks: list[Chunk] = []
    buf: list[Segment] = []
    buf_len = 0

    def make_chunk(seg_list: list[Segment]) -> Chunk:
        return Chunk(
            content="".join(s.text for s in seg_list),
            start_sec=seg_list[0].start_sec,
            end_sec=seg_list[-1].end_sec,
            src=_chunk_src(seg_list),
        )

    def flush() -> None:
        nonlocal buf, buf_len
        if buf:
            chunks.append(make_chunk(buf))
        buf = []
        buf_len = 0

    for seg in segments:
        if len(seg.text) > max_chars:  # 超长段单独硬切
            flush()
            chunks.extend(_split_long_segment(seg, max_chars))
            continue
        if buf and buf_len + len(seg.text) > max_chars and buf_len >= min_chars:
            tail = _tail(buf, overlap_chars)
            flush()
            buf = tail
            buf_len = sum(len(s.text) for s in tail)
        buf.append(seg)
        buf_len += len(seg.text)
    flush()
    return chunks


def _split_long_segment(seg: Segment, max_chars: int) -> list[Chunk]:
    text = seg.text
    return [
        Chunk(
            content=text[i : i + max_chars],
            start_sec=seg.start_sec,
            end_sec=seg.end_sec,
            src=seg.source,
        )
        for i in range(0, len(text), max_chars)
    ]


def _tail(buf: list[Segment], overlap_chars: int) -> list[Segment]:
    out: list[Segment] = []
    total = 0
    for seg in reversed(buf):
        out.append(seg)
        total += len(seg.text)
        if total >= overlap_chars:
            break
    return list(reversed(out))
