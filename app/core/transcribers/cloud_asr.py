"""云端 ASR 转写（OpenAI 兼容 ``/v1/audio/transcriptions``）。

把语音识别卸载到局域网内的专用转写服务（faster-whisper-server /
SenseVoice / OpenAI whisper / ollama whisper），videoRAG 自身不再需要
本地加载 large-v3 这类大模型，省磁盘、可多设备共享。

服务需暴露 OpenAI 兼容接口，接收 multipart 上传的音频文件，
返回 ``verbose_json``。是否带 ``segments`` 取决于服务实现：

- faster-whisper-server（默认）：返回带时间戳的 segments，并接受
  ``vad_filter`` / ``hallucination_silence_threshold`` /
  ``condition_on_previous_text`` 等防幻觉参数（faster-whisper 专有）。
- SenseVoice 系列（falconia/sense-voice-openai-api、
  jackuh105/openai-sensevoice-stt 等）：**不接受** faster-whisper 专有参数；
  ``verbose_json`` 多数只返回 ``text``（极少数 VAD 模式带 segments），
  需做"无 segments → 整段兜底"的兼容。

provider 字符串里只要含 ``whisper``（如 ``faster-whisper-server`` /
``whisper``）即视为 faster-whisper 家族，按透传防幻觉参数处理；
其他视为通用 OpenAI 兼容 ASR，跳过 faster-whisper 专有参数。
"""
from __future__ import annotations

import io
import json
import logging
import shutil
import subprocess

import httpx

from app.core.fetchers.base import FetchedMedia
from app.core.transcribers.base import Segment, Transcript

log = logging.getLogger(__name__)

# faster-whisper-server 透传的防幻觉参数（实测 60s 纯静音 2 段幻觉 → 0 段）：
# - vad_filter：过滤非语音段
# - hallucination_silence_threshold：静音区幻觉阈值
# - condition_on_previous_text：切断"好好好好"式重复反馈环
# 其他 ASR（SenseVoice / OpenAI / ollama whisper）不认这几个键，
# 发送会被 400 拒或被忽略产生警告噪音——必须按 provider 区分。
_FASTER_WHISPER_PARAMS = {
    "vad_filter": "true",
    "hallucination_silence_threshold": "2",
    "condition_on_previous_text": "false",
}


def _audio_duration_sec(path: str) -> float | None:
    """读取本地音频时长（秒），用于无 segments 时构造兜底段。

    仅在 ``ffprobe`` 可用且解析成功时返回；失败返回 ``None``（调用方
    此时把兜底段的 ``end_sec`` 设为 0.0，下游仍可接受）。
    """
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            timeout=10,
        )
        return float(out.strip())
    except Exception as e:
        log.warning("ffprobe failed for %s: %s", path, e)
        return None


# 服务端 soundfile（libsndfile）只认 wav/flac/ogg 等 PCM 容器；
# mp3 / m4a(AAC) / opus / webm 这类压缩格式会解码失败（实测 m4a 上传
# 直接触发服务端三级降级全败返回 {"error": ...}）。而 yt-dlp 默认拿到的
# 是 m4a，转 mp3 又要依赖容器内 ffmpeg 存在。这里统一在客户端把音频
# 转成 wav 16k mono 再上传，任何服务端都能稳定解码。
# 容器内 ffmpeg 由 Dockerfile 安装（apt install ffmpeg）；宿主跑服务时
# 若没有 ffmpeg 则跳过转换、直接用原文件（仍会失败，但日志会说明原因）。
_WAV_EXTS = {".wav", ".flac", ".ogg"}

# SenseVoice（funasr）越界保护：服务端 model.py output_timestamp 分支
# 会因 CTC forced align 分组数超过文本 token 数而 IndexError
# （tokens[token_id] 越界）。实测主要在长音频出现，但个别短块内容也会
# 触发（内容相关，非纯长度问题）。策略：
#   1. 长音频切成 ≤60s 块逐块上传、按绝对偏移合并 segments；
#   2. 单块失败 → 二分成更小的子块重试（绕开触发内容）；
#   3. 到最小粒度仍失败 → 跳过该块（日志告警），保证整体链路完成。
_CHUNK_SECONDS = 60.0
_MIN_CHUNK_SECONDS = 8.0


def _slice_audio_wav(path: str, start: float, dur: float) -> bytes | None:
    """切出 [start, start+dur) 区间的音频，转成 wav 16k mono bytes。"""
    if shutil.which("ffmpeg") is None:
        return None
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-y",
                "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                "-i", path,
                "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1",
            ],
            capture_output=True, timeout=600,
        )
    except Exception as e:
        log.warning("slice ffmpeg failed at %.1fs: %s", start, e)
        return None
    if proc.returncode != 0 or not proc.stdout:
        log.warning(
            "slice ffmpeg failed at %.1fs: %s", start,
            proc.stderr.decode("utf-8", "replace")[-300:],
        )
        return None
    return proc.stdout


def _chunk_audio_16k_mono(path: str, chunk_sec: float = _CHUNK_SECONDS) -> list[bytes]:
    """把音频切成多个 ≤chunk_sec 的 wav 16k mono 块（bytes 列表）。

    - 时长探测失败或本身不超过 chunk_sec：直接返回单块（整段转 wav）
    - 否则用 ffmpeg 精确 seek 逐段切块，每块 16k mono wav
    """
    dur = _audio_duration_sec(path)
    if not dur or dur <= chunk_sec:
        data, _ = _to_wav_16k_mono(path)
        return [data]
    if shutil.which("ffmpeg") is None:
        log.warning("ffmpeg not found; cannot chunk %s, uploading as-is", path)
        data, _ = _to_wav_16k_mono(path)
        return [data]
    chunks: list[bytes] = []
    start = 0.0
    while start < dur - 0.05:  # 最后一段 <60s 也切出来
        data = _slice_audio_wav(path, start, chunk_sec)
        if data is None:
            break
        chunks.append(data)
        start += chunk_sec
    if not chunks:
        log.warning("chunking produced no output for %s, falling back to whole file", path)
        data, _ = _to_wav_16k_mono(path)
        return [data]
    log.info("chunked %s (%.1fs) into %d blocks of %.0fs", path, dur, len(chunks), chunk_sec)
    return chunks


def _to_wav_16k_mono(path: str) -> tuple[bytes, bool]:
    """把音频转成 wav 16k mono 字节流。

    返回 ``(bytes, converted)``：converted=True 表示经过了 ffmpeg 转换
    （调用方可用转换后的时长），False 表示原文件可直接用。
    """
    ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext in _WAV_EXTS:
        return open(path, "rb").read(), False
    if shutil.which("ffmpeg") is None:
        log.warning(
            "ffmpeg not found; cannot convert %s to wav (will upload as-is, "
            "service may fail to decode)", path,
        )
        return open(path, "rb").read(), False
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-y",
                "-i", path,
                "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1",
            ],
            capture_output=True, timeout=600,
        )
    except Exception as e:
        log.warning("ffmpeg convert failed for %s: %s", path, e)
        return open(path, "rb").read(), False
    if proc.returncode != 0:
        log.warning(
            "ffmpeg convert failed for %s: %s", path,
            proc.stderr.decode("utf-8", "replace")[-500:],
        )
        return open(path, "rb").read(), False
    if not proc.stdout:
        log.warning("ffmpeg produced empty output for %s", path)
        return open(path, "rb").read(), False
    return proc.stdout, True


class CloudASRTranscriber:
    """调用远程 OpenAI 兼容转写接口。"""

    name = "cloud_asr"

    def __init__(
        self,
        base_url: str,
        api_key: str = "local",
        model: str | None = None,
        language: str = "zh",
        timeout: float = 900.0,
        provider: str = "faster-whisper-server",
    ):
        # base_url 为服务根，如 http://host:8000（不含 /v1 也可）
        self._base = base_url.rstrip("/")
        # 多数服务要求非空 key，本地部署填任意非空串即可
        self._api_key = api_key or "local"
        self._model = model
        self._language = language
        self._timeout = timeout
        self._provider = (provider or "").lower()

    def _url(self) -> str:
        if self._base.endswith("/v1"):
            return self._base + "/audio/transcriptions"
        return self._base + "/v1/audio/transcriptions"

    def _is_faster_whisper(self) -> bool:
        # 兼容 faster-whisper-server / whisper / openai-whisper 等命名
        return "whisper" in self._provider

    async def transcribe(self, media: FetchedMedia) -> Transcript:
        if media.path is None or media.kind not in ("audio", "video"):
            raise ValueError("cloud_asr transcriber requires audio/video media")

        form: dict[str, str] = {
            "language": self._language,
            "response_format": "verbose_json",
        }
        # faster-whisper 专有参数仅发给 whisper 家族；SenseVoice/通用 ASR 跳过
        if self._is_faster_whisper():
            form.update(_FASTER_WHISPER_PARAMS)

        # model 字段：SenseVoice / funasr 系列必需（缺则 422）；faster-whisper 可省
        if self._model:
            form["model"] = self._model

        base_filename = media.path.split("/")[-1] or "media"
        base_stem = base_filename.rsplit(".", 1)[0] if "." in base_filename else base_filename
        has_ffmpeg = shutil.which("ffmpeg") is not None

        # 区间工作队列：(起始秒, 长度秒)。长区间切成 ≤60s 块；单块转写
        # 失败时二分成更小子块重试（SenseVoice 越界是内容触发的，更小的
        # 块通常能绕开）；到 _MIN_CHUNK_SECONDS 仍失败则跳过该区间。
        # 注：这是「集中式 ASR 内部」的块级降级；跨 provider 回退到本地
        # whisper 的链路未在 factory 装配（自动降级机制规划中，尚未实现）。
        total_dur = _audio_duration_sec(media.path) if has_ffmpeg else None
        work: list[tuple[float, float]] = [(0.0, total_dur or _CHUNK_SECONDS)]
        if total_dur and total_dur > _CHUNK_SECONDS:
            work = []
            cur = 0.0
            while cur < total_dur - 0.05:
                work.append((cur, min(_CHUNK_SECONDS, total_dur - cur)))
                cur += _CHUNK_SECONDS

        all_segments: list[Segment] = []
        raw_parts: list[str] = []  # (start, text) 先收集再按时间排序拼接
        skipped: list[str] = []
        seq = 0

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            while work:
                seg_start, seg_len = work.pop(0)
                if has_ffmpeg and total_dur and total_dur > _CHUNK_SECONDS:
                    chunk_bytes = _slice_audio_wav(media.path, seg_start, seg_len)
                else:
                    chunk_bytes, _ = _to_wav_16k_mono(media.path)
                if chunk_bytes is None:
                    skipped.append(f"[{seg_start:.0f}s+{seg_len:.0f}s](slice failed)")
                    continue

                filename = f"{base_stem}_{seq}.wav"
                seq += 1
                files = {
                    "file": (filename, io.BytesIO(chunk_bytes), "application/octet-stream"),
                }
                resp = await client.post(
                    self._url(),
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    data=form,
                    files=files,
                )
                resp.raise_for_status()
                try:
                    payload = resp.json()
                except json.JSONDecodeError as e:
                    raise RuntimeError(
                        f"cloud_asr chunk@{seg_start:.0f}s returned non-JSON "
                        f"(status={resp.status_code}): {resp.text[:200]}"
                    ) from e

                # 服务端错误路径也走 HTTP 200（{"error": {...}}），必须显式检查，
                # 否则静默得到空 transcript，下游 note 会基于空文本幻觉。
                if isinstance(payload, dict) and payload.get("error"):
                    err = payload["error"]
                    msg = (
                        err.get("message") if isinstance(err, dict) else str(err)
                    ) or str(payload.get("error"))
                    # 二分重试：更小的子块通常能绕开内容触发的越界
                    if has_ffmpeg and seg_len > _MIN_CHUNK_SECONDS:
                        half = seg_len / 2.0
                        work.insert(0, (seg_start + half, seg_len - half))
                        work.insert(0, (seg_start, half))
                        log.warning(
                            "cloud_asr chunk@%.0fs failed (%s); retrying as two %.0fs halves",
                            seg_start, msg[:120], half,
                        )
                        continue
                    skipped.append(f"[{seg_start:.0f}s+{seg_len:.0f}s]({msg[:120]})")
                    log.warning("cloud_asr chunk@%.0fs skipped: %s", seg_start, msg[:200])
                    continue

                # 解析本块 segments，时间戳叠加块绝对偏移
                for s in payload.get("segments") or []:
                    text = (s.get("text") or "").strip()
                    if not text:
                        continue
                    try:
                        start = float(s["start"]) + seg_start
                        end = float(s["end"]) + seg_start
                    except (KeyError, TypeError, ValueError):
                        continue
                    all_segments.append(Segment(start_sec=start, end_sec=end, text=text))

                raw_chunk = (payload.get("text") or "").strip()
                if raw_chunk:
                    raw_parts.append(raw_chunk)

        if skipped:
            log.warning("cloud_asr skipped %d segment(s): %s", len(skipped), "; ".join(skipped))

        raw = "\n".join(raw_parts) or "\n".join(s.text for s in all_segments)

        # 无 segments 兜底：SenseVoice 等仅返回 text 时构造一个整段 segment，
        # 让 pipeline 的 ``transcript.segments`` 非空判定通过。
        # end_sec 优先级：
        #   1) 服务端 verbose_json["duration"]（jackuh105/openai-sensevoice-stt 提供）
        #   2) 本地 ffprobe 探测
        #   3) 0.0（兜底；下游可继续处理）
        if not all_segments and raw.strip():
            duration = total_dur
            end_sec = float(duration) if duration else 0.0
            all_segments = [Segment(start_sec=0.0, end_sec=end_sec, text=raw)]
            log.info(
                "cloud_asr (%s) returned text-only result, synthesized one segment end=%.1fs",
                self._provider, end_sec,
            )

        return Transcript(segments=all_segments, raw_text=raw, source="cloud_asr")