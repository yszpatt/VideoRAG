"""进程内 SenseVoice 与 Docker 侧车的实现等价性（parity）测试。

**为什么要这个测试**：分块/分句逻辑存在两份实现——

- ``deploy/asr/server.py``（侧车容器内，只 COPY server.py + requirements.txt，
  刻意不依赖 app 包，以保证 Docker 构建上下文与镜像内容零改动）；
- ``app/core/transcribers/sensevoice_segments.py``（进程内转写复用）。

两条路径必须产出**完全一致**的段落切分，否则同一份音频在 Docker 与 Windows 桌面
形态下会得到不同的时间轴/分段。本测试用同一组输入逐字段比对，把「逻辑漂移」
挡在 CI 上。

实现说明：不需要真的起容器——侧车模块的这几个函数是纯函数，直接按路径加载即可。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

from app.core.transcribers import sensevoice_segments as inproc

ROOT = Path(__file__).resolve().parent.parent
SIDECAR_SERVER = ROOT / "deploy" / "asr" / "server.py"

SR = 16000


@pytest.fixture(scope="module")
def sidecar():
    """按路径加载 deploy/asr/server.py（它不在 app 包内，不构成可导入模块）。"""
    if not SIDECAR_SERVER.is_file():
        pytest.skip(f"侧车实现不存在：{SIDECAR_SERVER}")
    spec = importlib.util.spec_from_file_location("videorag_asr_sidecar", SIDECAR_SERVER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_constants_match(sidecar):
    """关键常量必须一致（改一处忘另一处是最典型的漂移）。"""
    assert sidecar.BLOCK_SECONDS == inproc.BLOCK_SECONDS
    assert sidecar.GAP_END_SENTENCE == inproc.GAP_END_SENTENCE
    assert sidecar._SENTENCE_END == inproc.SENTENCE_END
    assert sidecar._MIN_GAP_TOKENS == inproc.MIN_GAP_TOKENS


@pytest.mark.parametrize("total_sec", [0.5, 10, 59.9, 60, 60.1, 125, 181.5])
def test_split_blocks_parity(sidecar, total_sec):
    """分块结果逐块一致（偏移 + 样本内容）。"""
    samples = np.arange(int(SR * total_sec), dtype=np.float32)
    theirs = sidecar._split_blocks(samples, SR)
    ours = inproc.split_blocks(samples, SR)

    assert len(theirs) == len(ours)
    for (t_off, t_blk), (o_off, o_blk) in zip(theirs, ours):
        assert t_off == o_off
        assert np.array_equal(t_blk, o_blk)


@pytest.mark.parametrize(
    "starts",
    [
        [],
        [1.0],
        [0.0, 0.3, 0.6],
        [0.0, 0.3, 0.6, 0.9, 1.2, 2.0, 2.3, 2.6],
        [0.0, 0.05, 0.1, 3.0, 3.2],
        [0.0, 2.5, 5.0],  # 全部超阈值：无有效间隔 → 双双回落 0.3
    ],
)
def test_median_gap_parity(sidecar, starts):
    assert sidecar._median_gap(starts) == inproc.median_gap(starts)


CASES = [
    # 纯标点断句 + 尾句
    (["今天", "我们", "聊聊", "RAG", "。", "它", "是", "什么"],
     [0.0, 0.3, 0.6, 0.9, 1.2, 2.0, 2.3, 2.6]),
    # 静音 gap 断句（gap 前已积累够 MIN_GAP_TOKENS）
    (["第一句", "的", "内容", "第二句", "的", "内容"], [0.0, 0.3, 0.6, 2.0, 2.3, 2.6]),
    # 不足 MIN_GAP_TOKENS：标点不断句
    (["你好", "。", "再", "见"], [0.0, 0.3, 1.0, 1.3]),
    # 尾部积累够 MIN_GAP_TOKENS → 自成一段
    (["开头", "句子", "。", "空", "格", "残", "句"], [0.0, 0.3, 0.6, 0.9, 1.2, 1.5, 1.8]),
    # 尾部残句（不足 MIN_GAP_TOKENS）并入上一段
    (["开头", "句子", "。", "残", "句"], [0.0, 0.3, 0.6, 0.9, 1.2]),
    # 含空白 token
    (["甲", "  ", "乙", "丙", "丁"], [0.0, 0.3, 0.6, 0.9, 1.2]),
    # 长度不匹配 → 双双返回空
    (["a", "b"], [0.0]),
    ([], []),
    # 单 token
    (["独"], [0.0]),
]


@pytest.mark.parametrize("tokens,starts", CASES)
def test_build_segments_parity(sidecar, tokens, starts):
    assert sidecar._build_segments(tokens, starts) == inproc.build_segments(tokens, starts)


def test_build_segments_parity_random(sidecar):
    """固定种子随机输入批量比对，覆盖人工用例想不到的组合。"""
    rng = np.random.default_rng(20260917)
    puncts = list("。！？!?；;…，,")

    for _ in range(30):
        n = int(rng.integers(1, 24))
        tokens: list[str] = []
        for _ in range(n):
            if rng.random() < 0.2:
                tokens.append(str(rng.choice(puncts)))
            else:
                tokens.append("".join(str(rng.choice(list("语音转写测试内容"))) for _ in range(int(rng.integers(1, 4)))))
        # 单调递增的时间戳，间隔 0.05~1.5s
        steps = rng.uniform(0.05, 1.5, size=n)
        starts = np.cumsum(steps).round(3).tolist()

        theirs = sidecar._build_segments(list(tokens), list(starts))
        ours = inproc.build_segments(list(tokens), list(starts))
        assert theirs == ours, f"mismatch tokens={tokens} starts={starts}"
