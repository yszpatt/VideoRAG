"""进程内 SenseVoice 转写（sherpa-onnx，纯 CPU int8）。

**用途**：Windows 桌面包的默认本地转写路径。

Docker 形态下本地 SenseVoice 是一个独立侧车容器（``deploy/asr/``），主服务经
``http://asr:9991`` 用 OpenAI 兼容接口调用——这在家里/NAS 上很合适（模型与主服务
解耦、可跨设备共享）。但桌面应用里再拉起第二个进程 + 第二个 HTTP 服务属于纯粹的
运维负担：sherpa-onnx 本身提供 Windows wheel，直接进程内加载即可。

两条路径**共用同一套分块/分句逻辑**（``sensevoice_segments``，与侧车实现有
parity 测试锁定），因此输出段落一致，只是传输层从 HTTP 换成了函数调用。

由 ``ASR_LOCAL_BACKEND`` 决定走哪条（默认 ``http``，桌面端构建时置 ``inproc``），
见 ``app/core/factory.py``。
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterator

import numpy as np

from app.core.fetchers.base import FetchedMedia
from app.core.transcribers.base import Segment, Transcript
from app.core.transcribers.sensevoice_segments import BLOCK_SECONDS, build_segments

log = logging.getLogger(__name__)

_TARGET_SR = 16000
# ffmpeg 解码整体超时（秒）：长视频整段解码是耗时大头，给足余量，仅防永久挂死
_DECODE_TIMEOUT = 3600.0
# 单次 ffmpeg 读取的样本数（= 一个解码块），与 sidecar 的 60s 分块边界一致
_BLOCK_SAMPLES = int(BLOCK_SECONDS * _TARGET_SR)

# 模型清单中的必需文件（与 app/core/local_models/registry.py 的 ASR_SPEC.required 一致）
_MODEL_FILE = "model.int8.onnx"
_TOKENS_FILE = "tokens.txt"


class SenseVoiceModelMissing(RuntimeError):
    """模型未下载：给出可操作的指引，而不是让用户面对一句 "file not found"。"""


def _iter_audio_blocks(path: str, timeout: float = _DECODE_TIMEOUT) -> Iterator[np.ndarray]:
    """用 ffmpeg 把任意音频/视频解码成 16k 单声道 float32，按块惰性产出。

    **流式**读取而非整段读入内存：1 小时音频的 f32le 是 ~230MB，流式按 60s 块
    产出只需常数级内存，且块边界与 ``sensevoice_segments.split_blocks`` 完全一致
    （同样按 ``BLOCK_SECONDS * 16000`` 个样本切），因此两条路径的段落切分结果相同。

    stderr 落临时文件而非 PIPE：PIPE 在 ffmpeg 大量报错时可能写满缓冲导致
    双向阻塞（我们读 stdout、它堵在写 stderr），落盘可彻底规避死锁。
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg 未找到，无法解码音频。Docker 镜像内已内置；"
            "桌面包应随包分发 ffmpeg（见 app/runtime_env.py 的 PATH 注入）"
        )

    err = tempfile.TemporaryFile()
    proc = subprocess.Popen(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-i", path,
            "-vn",                      # 只取音频流（视频文件时忽略画面）
            "-ac", "1", "-ar", str(_TARGET_SR),
            "-f", "f32le", "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=err,
    )
    t0 = time.monotonic()
    try:
        assert proc.stdout is not None
        while True:
            if time.monotonic() - t0 > timeout:
                raise RuntimeError(f"ffmpeg 解码超时（>{timeout:.0f}s）：{path}")
            # BufferedReader.read(n) 会读满 n 字节或到 EOF，故块大小稳定
            data = proc.stdout.read(_BLOCK_SAMPLES * 4)
            if not data:
                break
            # copy()：frombuffer 返回只读视图，sherpa-onnx 的 pybind 转换要求可写数组
            yield np.frombuffer(data, dtype=np.float32).copy()
        code = proc.wait(timeout=30)
        if code != 0:
            err.seek(0)
            detail = err.read().decode("utf-8", "replace")[-500:]
            raise RuntimeError(f"ffmpeg 解码失败（exit {code}）：{detail.strip() or path}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        err.close()


class SenseVoiceTranscriber:
    """sherpa-onnx SenseVoice（int8）进程内转写。

    模型懒加载：构造时不触碰磁盘，首次 ``transcribe`` 才加载（~240MB，耗时秒级），
    且在 ``to_thread`` 中执行，避免阻塞事件循环。
    """

    name = "sensevoice"

    def __init__(
        self,
        model_dir: str,
        num_threads: int = 4,
        language: str = "zh",
        use_itn: bool = True,
        recognizer: object | None = None,
    ):
        self._model_dir = model_dir
        self._num_threads = num_threads
        self._language = language
        self._use_itn = use_itn
        self._recognizer = recognizer  # 测试可注入假引擎，无需真实模型
        self._lock = threading.Lock()

    # ---- 模型加载 ----

    def _model_paths(self) -> tuple[Path, Path]:
        d = Path(self._model_dir)
        return d / _MODEL_FILE, d / _TOKENS_FILE

    def _get_recognizer(self):
        if self._recognizer is not None:
            return self._recognizer
        with self._lock:
            if self._recognizer is None:
                model, tokens = self._model_paths()
                if not model.is_file() or not tokens.is_file():
                    raise SenseVoiceModelMissing(
                        f"本地 SenseVoice 模型未就绪：缺少 {model.name} 或 {tokens.name}"
                        f"（目录 {self._model_dir}）。请在 Web 界面「设置 → 本地模型」"
                        f"下载 SenseVoice 模型后重试。"
                    )
                import sherpa_onnx  # 延迟导入：避免拖慢启动与无 ASR 场景

                t0 = time.time()
                self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=str(model),
                    tokens=str(tokens),
                    num_threads=self._num_threads,
                    use_itn=self._use_itn,
                    language=self._language,
                    debug=False,
                )
                log.info(
                    "SenseVoice loaded from %s in %.2fs (threads=%d, language=%s)",
                    self._model_dir, time.time() - t0, self._num_threads, self._language,
                )
        return self._recognizer

    # ---- 解码 ----

    def _decode_blocks(self, blocks: list[np.ndarray]) -> dict:
        """逐块解码并合并为绝对时间轴上的 segments（同步，跑在线程里）。"""
        rec = self._get_recognizer()
        raw_parts: list[str] = []
        segs: list[dict] = []
        t0 = time.time()
        total_samples = 0

        for offset, blk in zip(
            (i * BLOCK_SECONDS for i in range(len(blocks))), blocks
        ):
            total_samples += len(blk)
            stream = rec.create_stream()
            stream.accept_waveform(_TARGET_SR, blk)
            rec.decode_stream(stream)
            res = stream.result

            text = (res.text or "").strip()
            if text:
                raw_parts.append(text)

            block_end = offset + len(blk) / _TARGET_SR
            for seg in build_segments(list(res.tokens or []), list(res.timestamps or [])):
                s = dict(seg)
                s["start"] = round(s["start"] + offset, 3)
                s["end"] = round(s["end"] + offset, 3)
                # 块内收尾不得越过块边界（与 sidecar 同口径）
                if s["end"] > block_end - 0.001:
                    s["end"] = round(block_end, 3)
                segs.append(s)

        duration = total_samples / _TARGET_SR
        log.info(
            "sensevoice decoded %.1fs audio in %.2fs (rtf=%.3f)",
            duration, time.time() - t0, (time.time() - t0) / max(duration, 1e-6),
        )
        # 有时间戳则 text 用逐句拼接（与 segments 严格一致），否则用整段识别文本
        text = "\n".join(p["text"] for p in segs) if segs else "\n".join(raw_parts)
        return {"text": text, "segments": segs, "duration": round(duration, 3)}

    async def transcribe(self, media: FetchedMedia) -> Transcript:
        if media.path is None or media.kind not in ("audio", "video"):
            raise ValueError("sensevoice transcriber requires audio/video media")

        def _run() -> dict:
            blocks = list(_iter_audio_blocks(media.path))
            if not blocks:
                raise RuntimeError(f"解码得到空音频：{media.path}")
            # 短音频（<60s）走与 sidecar 相同的单块路径
            return self._decode_blocks(blocks)

        out = await asyncio.to_thread(_run)

        segments = [
            Segment(start_sec=s["start"], end_sec=s["end"], text=s["text"])
            for s in out["segments"]
        ]
        raw = out["text"] or ""

        # 无时间戳兜底：与 cloud_asr 一致，构造一个整段 segment 让下游判定通过
        if not segments and raw.strip():
            segments = [
                Segment(start_sec=0.0, end_sec=float(out["duration"] or 0.0), text=raw)
            ]
            log.info(
                "sensevoice returned text-only result, synthesized one segment end=%.1fs",
                out["duration"] or 0.0,
            )

        return Transcript(segments=segments, raw_text=raw, source="sensevoice")


__all__ = ["SenseVoiceTranscriber", "SenseVoiceModelMissing"]
