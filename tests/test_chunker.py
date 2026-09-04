from app.core.embed.chunker import Chunk, chunk_segments
from app.core.transcribers.base import Segment


def _segs(texts):
    return [
        Segment(start_sec=i * 2.0, end_sec=i * 2.0 + 1.5, text=t)
        for i, t in enumerate(texts)
    ]


def test_empty_segments():
    assert chunk_segments([]) == []


def test_single_short_segment():
    chunks = chunk_segments(_segs(["你好"]))
    assert len(chunks) == 1
    assert chunks[0].content == "你好"
    assert chunks[0].start_sec == 0.0
    assert chunks[0].end_sec == 1.5


def test_long_text_split_with_time_bounds():
    # 每段 120 字，10 段 → 累计 1200 字，max_chars=500 → 至少 2 块
    texts = ["字" * 120] * 10
    chunks = chunk_segments(_segs(texts))
    assert len(chunks) >= 2
    # 每块内容不超过上限
    assert all(len(c.content) <= 500 for c in chunks)
    # 时间区间单调递增
    for a, b in zip(chunks, chunks[1:]):
        assert a.end_sec <= b.end_sec


def test_chunk_has_overlap():
    texts = ["字" * 120] * 10
    chunks = chunk_segments(_segs(texts), max_chars=500, overlap_chars=50)
    if len(chunks) >= 2:
        prev = chunks[0].content
        cur = chunks[1].content
        # 后一块开头应包含前一块尾部内容（重叠）
        tail = prev[-50:]
        assert tail in cur or cur.startswith(tail)


def test_respects_min_chars():
    # 每段 10 字，10 段 = 100 字 < min_chars=200 → 全部合成一块
    chunks = chunk_segments(_segs(["字" * 10] * 10), max_chars=500, min_chars=200)
    assert len(chunks) == 1
    assert len(chunks[0].content) == 100


def test_max_chars_hard_cap():
    # 单段超长（800 字）也应被硬切
    chunks = chunk_segments(_segs(["字" * 800]), max_chars=500)
    assert all(len(c.content) <= 500 for c in chunks)
    assert len(chunks) >= 2
