"""sherpa-onnx + SenseVoice (int8) OpenAI 兼容转写服务。

本地降级 ASR 的轻量实现（替代 FunASR+torch 方案）：

- 运行时仅依赖 sherpa-onnx（自带 onnx 内核）+ fastapi/uvicorn/numpy，无 torch/funasr/modelscope；
- 模型：SenseVoiceSmall int8 ONNX（229MB，ModelScope: poloniumrock/SenseVoiceSmallOnnx /
  GitHub: sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17）；
- 支持 POST /v1/audio/transcriptions（multipart），response_format=verbose_json 返回
  segments[]（句子级 start/end/text，由词级 timestamps 按句末标点 / 静音 gap>0.6s 聚合）；
- 无状态非流式模型：长音频在服务端按 block 切片（默认 60s）逐块解码、偏移合并，
  与 videorag cloud_asr 客户端的 60s 分块策略天然兼容；
- 上传音频统一经 ffmpeg 转 wav 16k mono（调用方传任意格式均可）。

用法：
    uv run server.py --model models/model.int8.onnx --tokens models/tokens.txt \
        --port 9991 --num-threads 4
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile

log = logging.getLogger("sherpa-asr")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

BLOCK_SECONDS = 60.0
GAP_END_SENTENCE = 0.6        # 句末静音阈值（秒），与 deploy/sensevoice 补丁口径一致
_SENTENCE_END = set("。！？!?；;…")
# ITN 会把中文数字转阿拉伯数字（如 二零二四 → 2024），导致 tokens 与 text 字符不对齐；
# segments 文本采用 tokens 直接拼接（保持与时间轴一一对应），raw text 用服务端整句。
_MIN_GAP_TOKENS = 3           # gap 断句至少积累的 token 数，避免超短碎句

# —— 音频工具（ffmpeg → wav 16k mono f32）——
_WAV_F32 = "wav"


def _to_f32_mono(path: str) -> tuple[np.ndarray, int]:
    """任意音频 → (float32 单声道, 16000)。wav 16k mono 直接读；否则 ffmpeg 转换。"""
    if shutil.which("ffmpeg") is not None:
        try:
            proc = subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-i", path,
                 "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1"],
                capture_output=True, timeout=600,
            )
            if proc.returncode == 0 and proc.stdout:
                return np.frombuffer(proc.stdout, dtype=np.float32), 16000
            log.warning("ffmpeg convert failed: %s", proc.stderr.decode("utf-8", "replace")[-300:])
        except Exception as e:  # noqa: BLE001
            log.warning("ffmpeg exception: %s", e)
    # 回退：假定 wav
    import wave
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        if w.getnchannels() != 1 or sr != 16000:
            raise ValueError(f"wav must be 16k mono, got {sr}Hz/{w.getnchannels()}ch")
        raw = w.readframes(w.getnframes())
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0, sr


def _split_blocks(samples: np.ndarray, sr: int) -> list[tuple[int, np.ndarray]]:
    """长音频切成 ≤BLOCK 秒的块，返回 [(偏移秒, 块数据)]。短音频返回单块。"""
    total = len(samples) / sr
    if total <= BLOCK_SECONDS:
        return [(0.0, samples)]
    n = int(BLOCK_SECONDS * sr)
    return [(i * BLOCK_SECONDS, samples[i * n : i * n + n]) for i in range(int(np.ceil(total / BLOCK_SECONDS)))]


def _median_gap(starts: list[float]) -> float:
    """相邻词 start 差的中位数，作为词时长估计（夹在 0.05~1.0s）。"""
    diffs = [b - a for a, b in zip(starts, starts[1:]) if 0 < b - a < 2.0]
    if not diffs:
        return 0.3
    return float(np.median(diffs))


def _build_segments(tokens: list[str], starts: list[float]) -> list[dict]:
    """词级时间戳 → 句子级 segments。

    边界：句末标点 token，或 与下一词静音 gap > GAP_END_SENTENCE。
    句 end = 该句末词 start + 词时长估计（下句首词 start 或平均 gap）。
    时间戳缺失时返回 []（调用方整段兜底，兼容 cloud_asr 客户端）。
    """
    if not tokens or len(tokens) != len(starts):
        return []
    dur = _median_gap(starts)
    segs: list[dict] = []
    cur_tok: list[str] = []
    cur_start: float | None = None
    for i, (tok, st) in enumerate(zip(tokens, starts)):
        tok = tok.strip()
        if not tok:
            continue
        if cur_start is None:
            cur_start = st
        cur_tok.append(tok)
        nxt = starts[i + 1] if i + 1 < len(starts) else None
        is_end = tok[-1] in _SENTENCE_END or (nxt is not None and nxt - st > GAP_END_SENTENCE)
        is_last = nxt is None
        if (is_end or is_last) and len(cur_tok) >= _MIN_GAP_TOKENS:
            end = nxt if nxt is not None and (nxt - st > GAP_END_SENTENCE) else st + dur
            segs.append({"start": round(cur_start, 3), "end": round(min(end, st + dur), 3),
                         "text": "".join(cur_tok)})
            cur_tok, cur_start = [], None
    # 尾部不足 _MIN_GAP_TOKENS 的残句并入最后一段
    if cur_tok and segs:
        segs[-1]["text"] += "".join(cur_tok)
        segs[-1]["end"] = round((starts[-1] + dur), 3)
    elif cur_tok:
        segs.append({"start": round(cur_start or 0.0, 3),
                     "end": round(starts[-1] + dur, 3), "text": "".join(cur_tok)})
    return segs


class SenseVoiceEngine:
    def __init__(self, model: str, tokens: str, num_threads: int = 4, language: str = "zh"):
        import sherpa_onnx  # 延迟导入，避免 argparse/健康检查时加载重依赖

        t0 = time.time()
        self._rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=model, tokens=tokens, num_threads=num_threads,
            use_itn=True, language=language, debug=False,
        )
        self.model = Path(model).name
        log.info("model %s loaded in %.2fs (threads=%d, language=%s)", model, time.time() - t0, num_threads, language)

    def transcribe_samples(self, samples: np.ndarray, sr: int) -> dict:
        """解码整段音频（内部按 block 分块），返回 text + segments。"""
        t0 = time.time()
        blocks = _split_blocks(samples, sr)
        raw_parts: list[str] = []
        segs: list[dict] = []
        for offset, blk in blocks:
            st = self._rec.create_stream()
            st.accept_waveform(sr, blk)
            self._rec.decode_stream(st)
            res = st.result
            text = (res.text or "").strip()
            if text:
                raw_parts.append(text)
            ts = list(res.timestamps or [])
            for seg in _build_segments(list(res.tokens or []), ts):
                s = dict(seg)
                s["start"] = round(s["start"] + offset, 3)
                s["end"] = round(s["end"] + offset, 3)
                if s["end"] > offset + len(blk) / sr - 0.001:
                    s["end"] = round(offset + len(blk) / sr, 3)
                segs.append(s)
        duration = len(samples) / sr
        log.info("decoded %.1fs audio in %.2fs (rtf=%.3f)", duration, time.time() - t0, (time.time() - t0) / max(duration, 1e-6))
        # 无时间戳 → text-only（调用方整段兜底），有时间戳则 text 用逐句拼接保持与 segments 一致
        if segs:
            text = "\n".join(p["text"] for p in segs)
        return {"text": text, "segments": segs, "duration": round(duration, 3), "model": self.model}


def build_app(engine: SenseVoiceEngine):
    app = FastAPI(title="sherpa SenseVoice ASR (OpenAI compatible)")

    @app.get("/health")
    async def health():
        return {"status": "ok", "model": engine.model}

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [{"id": engine.model, "object": "model", "owned_by": "sherpa-onnx"}]}

    @app.post("/v1/audio/transcriptions")
    async def transcriptions(
        file: UploadFile = File(...),
        model: str = Form("sensevoice"),
        response_format: str = Form("json"),
    ):
        if not file.filename:
            raise HTTPException(400, "empty file")
        raw = await file.read()
        if not raw:
            raise HTTPException(400, "empty file")
        suffix = Path(file.filename or "audio.wav").suffix or ".wav"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fp:
            fp.write(raw)
            tmp = fp.name
        try:
            samples, sr = await asyncio.to_thread(_to_f32_mono, tmp)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"audio decode failed: {e}") from e
        finally:
            Path(tmp).unlink(missing_ok=True)
        out = await asyncio.to_thread(engine.transcribe_samples, samples, sr)
        if response_format == "verbose_json":
            return {
                "task": "transcribe", "language": "zh",
                "duration": out["duration"], "text": out["text"],
                "segments": out["segments"],
            }
        return {"text": out["text"]}

    @app.middleware("http")
    async def req_id(request, call_next):
        rid = uuid.uuid4().hex[:8]
        log.info("%s %s %s", rid, request.method, request.url.path)
        resp = await call_next(request)
        log.info("%s -> %s", rid, resp.status_code)
        return resp

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="sherpa-onnx SenseVoice OpenAI-compatible ASR")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9991)
    ap.add_argument("--model", required=True, help="path to model.int8.onnx")
    ap.add_argument("--tokens", required=True, help="path to tokens.txt")
    ap.add_argument("--num-threads", type=int, default=4)
    ap.add_argument("--language", default="zh", help="zh/en/ja/ko/yue")
    args = ap.parse_args()

    engine = SenseVoiceEngine(args.model, args.tokens, args.num_threads, args.language)
    import uvicorn
    uvicorn.run(build_app(engine), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
