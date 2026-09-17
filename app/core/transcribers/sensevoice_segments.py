"""SenseVoice 解码结果的纯函数工具：长音频分块 + 词级时间戳 → 句子级段落。

**为什么单独成模块**：同一套逻辑有两种运行形态需要复用——

1. Docker 侧车（``deploy/asr/server.py``，sherpa-onnx 容器内以 HTTP 服务形态跑）；
2. 进程内转写（``app/core/transcribers/sensevoice.py``，Windows 桌面包用，不再起第二个进程）。

侧车容器只 COPY ``server.py`` + ``requirements.txt``，**不依赖 app 包**，所以
``deploy/asr/server.py`` 里保留了一份同逻辑实现；两份实现由
``tests/test_sensevoice_parity.py`` 用同一组输入锁定输出完全一致，防止逻辑漂移。
这样处理而不是强行 import 共用，是为了守住「Docker 构建上下文与镜像内容零改动」。

本模块只做纯计算（仅依赖 numpy），不碰 ffmpeg / IO / 模型，便于单测。
"""
from __future__ import annotations

from typing import Sequence

import numpy as np

# 单块解码上限（秒）：SenseVoice 是无状态非流式模型，长音频分块逐块解码再按偏移合并，
# 与 cloud_asr 客户端的 60s 分块策略天然兼容。
BLOCK_SECONDS = 60.0

# 句末静音阈值（秒）：相邻词起始间隔超过该值即视为断句（与 deploy/sensevoice 补丁口径一致）
GAP_END_SENTENCE = 0.6

# 句末标点：命中即断句
SENTENCE_END = set("。！？!?；;…")

# gap 断句至少积累的 token 数，避免超短碎句
MIN_GAP_TOKENS = 3


def split_blocks(samples: np.ndarray, sr: int) -> list[tuple[float, np.ndarray]]:
    """长音频切成 ≤BLOCK_SECONDS 秒的块，返回 [(偏移秒, 块数据)]。短音频返回单块。"""
    total = len(samples) / sr
    if total <= BLOCK_SECONDS:
        return [(0.0, samples)]
    n = int(BLOCK_SECONDS * sr)
    return [
        (i * BLOCK_SECONDS, samples[i * n : i * n + n])
        for i in range(int(np.ceil(total / BLOCK_SECONDS)))
    ]


def median_gap(starts: Sequence[float]) -> float:
    """相邻词 start 差的中位数，作为词时长估计（夹在 0.05~1.0s）。"""
    diffs = [b - a for a, b in zip(starts, starts[1:]) if 0 < b - a < 2.0]
    if not diffs:
        return 0.3
    return float(np.median(diffs))


def build_segments(tokens: Sequence[str], starts: Sequence[float]) -> list[dict]:
    """词级时间戳 → 句子级 segments。

    边界：句末标点 token，或 与下一词静音 gap > GAP_END_SENTENCE。
    句 end = 该句末词 start + 词时长估计（下句首词 start 或平均 gap）。
    时间戳缺失时返回 []（调用方整段兜底，兼容 cloud_asr 客户端）。
    """
    if not tokens or len(tokens) != len(starts):
        return []
    dur = median_gap(starts)
    segs: list[dict] = []
    cur_tok: list[str] = []
    cur_start: float | None = None
    st = 0.0
    for i, (tok, st) in enumerate(zip(tokens, starts)):
        tok = tok.strip()
        if not tok:
            continue
        if cur_start is None:
            cur_start = st
        cur_tok.append(tok)
        nxt = starts[i + 1] if i + 1 < len(starts) else None
        is_end = tok[-1] in SENTENCE_END or (nxt is not None and nxt - st > GAP_END_SENTENCE)
        is_last = nxt is None
        if (is_end or is_last) and len(cur_tok) >= MIN_GAP_TOKENS:
            end = nxt if nxt is not None and (nxt - st > GAP_END_SENTENCE) else st + dur
            segs.append(
                {
                    "start": round(cur_start, 3),
                    "end": round(min(end, st + dur), 3),
                    "text": "".join(cur_tok),
                }
            )
            cur_tok, cur_start = [], None
    # 尾部不足 MIN_GAP_TOKENS 的残句并入最后一段
    if cur_tok and segs:
        segs[-1]["text"] += "".join(cur_tok)
        segs[-1]["end"] = round((starts[-1] + dur), 3)
    elif cur_tok:
        segs.append(
            {
                "start": round(cur_start or 0.0, 3),
                "end": round(starts[-1] + dur, 3),
                "text": "".join(cur_tok),
            }
        )
    return segs
