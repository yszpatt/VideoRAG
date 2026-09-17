"""SenseVoice 分块/分句纯函数测试。

这些函数同时服务于 Docker 侧车（``deploy/asr/server.py``）与进程内转写
（``app/core/transcribers/sensevoice.py``），是两条路径输出一致性的基础。
"""

import numpy as np

from app.core.transcribers.sensevoice_segments import (
    BLOCK_SECONDS,
    build_segments,
    median_gap,
    split_blocks,
)

SR = 16000


# ---------- split_blocks ----------

def test_split_blocks_short_audio_single_block():
    """≤60s 音频单块返回，偏移为 0。"""
    samples = np.zeros(SR * 10, dtype=np.float32)
    blocks = split_blocks(samples, SR)
    assert len(blocks) == 1
    assert blocks[0][0] == 0.0
    assert len(blocks[0][1]) == SR * 10


def test_split_blocks_exactly_block_seconds_is_single_block():
    """正好 60s 不切分（边界包含）。"""
    samples = np.zeros(int(SR * BLOCK_SECONDS), dtype=np.float32)
    assert len(split_blocks(samples, SR)) == 1


def test_split_blocks_long_audio_offsets_and_sizes():
    """125s → 3 块，偏移 0/60/120，末块为余量。"""
    total_sec = 125
    samples = np.zeros(SR * total_sec, dtype=np.float32)
    blocks = split_blocks(samples, SR)
    assert [round(b[0], 3) for b in blocks] == [0.0, 60.0, 120.0]
    n = int(BLOCK_SECONDS * SR)
    assert [len(b[1]) for b in blocks] == [n, n, SR * total_sec - 2 * n]


def test_split_blocks_preserves_all_samples():
    """切分不丢样本、不重叠（合并后与原数组等长）。"""
    samples = np.arange(SR * 137, dtype=np.float32)
    blocks = split_blocks(samples, SR)
    merged = np.concatenate([b[1] for b in blocks])
    assert merged.shape == samples.shape
    assert np.array_equal(merged, samples)


# ---------- median_gap ----------

def test_median_gap_empty_and_single():
    """无有效间隔时回落 0.3s（词时长估计的兜底）。"""
    assert median_gap([]) == 0.3
    assert median_gap([1.0]) == 0.3


def test_median_gap_ignores_outliers():
    """间隔 ≥2s 视为异常丢弃，取剩余中位数。"""
    assert median_gap([0.0, 0.3, 0.6, 5.0, 5.3]) == 0.3


# ---------- build_segments ----------

def test_build_segments_splits_on_punctuation_then_tail():
    """句末标点断句；尾句靠 is_last 收尾。"""
    tokens = ["今天", "我们", "聊聊", "RAG", "。", "它", "是", "什么"]
    starts = [0.0, 0.3, 0.6, 0.9, 1.2, 2.0, 2.3, 2.6]
    segs = build_segments(tokens, starts)
    assert segs == [
        {"start": 0.0, "end": 1.5, "text": "今天我们聊聊RAG。"},
        {"start": 2.0, "end": 2.9, "text": "它是什么"},
    ]


def test_build_segments_splits_on_silence_gap():
    """无标点但静音 gap > 0.6s 也断句。

    注意前提：gap 前须积累够 MIN_GAP_TOKENS 个词，否则不断句（见下一个用例）。
    """
    tokens = ["第一句", "的", "内容", "第二句", "的", "内容"]
    starts = [0.0, 0.3, 0.6, 2.0, 2.3, 2.6]
    segs = build_segments(tokens, starts)
    assert [s["text"] for s in segs] == ["第一句的内容", "第二句的内容"]
    # 前句 end = min(下句首词起点 2.0, 末词起点 0.6 + 词时长 0.3) = 0.9
    assert segs[0] == {"start": 0.0, "end": 0.9, "text": "第一句的内容"}
    assert segs[1] == {"start": 2.0, "end": 2.9, "text": "第二句的内容"}


def test_build_segments_min_tokens_merges_short_fragments():
    """标点命中但不足 3 个 token 时不断句，避免超短碎句。"""
    tokens = ["你好", "。", "再", "见"]
    starts = [0.0, 0.3, 1.0, 1.3]
    segs = build_segments(tokens, starts)
    assert len(segs) == 1
    assert segs[0]["text"] == "你好。再见"


def test_build_segments_tail_fragment_appended_to_last():
    """尾部残句（不足 MIN_GAP_TOKENS）并入上一段并延后其 end。"""
    tokens = ["开头", "句子", "。", "残", "句"]
    starts = [0.0, 0.3, 0.6, 0.9, 1.2]
    segs = build_segments(tokens, starts)
    assert len(segs) == 1
    assert segs[0]["text"] == "开头句子。残句"
    assert segs[0]["end"] == 1.5  # starts[-1] + 词时长 0.3


def test_build_segments_tail_long_enough_becomes_own_segment():
    """尾部积累够 MIN_GAP_TOKENS 时自成一段（不是并入上一段）。"""
    tokens = ["开头", "句子", "。", "空", "格", "残", "句"]
    starts = [0.0, 0.3, 0.6, 0.9, 1.2, 1.5, 1.8]
    segs = build_segments(tokens, starts)
    assert [s["text"] for s in segs] == ["开头句子。", "空格残句"]
    assert segs[1] == {"start": 0.9, "end": 2.1, "text": "空格残句"}


def test_build_segments_mismatched_lengths_returns_empty():
    """tokens 与时间戳数量不一致 → 返回空（调用方走整段兜底）。"""
    assert build_segments(["a", "b"], [0.0]) == []
    assert build_segments([], []) == []


def test_build_segments_skips_blank_tokens():
    """空白 token 被跳过但时间戳按原索引对齐（不破坏相邻关系）。"""
    tokens = ["甲", "  ", "乙", "丙", "丁"]
    starts = [0.0, 0.3, 0.6, 0.9, 1.2]
    segs = build_segments(tokens, starts)
    assert segs and "  " not in segs[0]["text"]
    assert segs[0]["text"] == "甲乙丙丁"
