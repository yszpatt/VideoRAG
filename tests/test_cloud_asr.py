"""CloudASRTranscriber 单元测试：验证对 OpenAI 兼容 verbose_json 的解析。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.transcribers.cloud_asr import CloudASRTranscriber


def _media(path, kind="audio"):
    return SimpleNamespace(path=path, kind=kind)


def _mock_client(resp):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    client.post.return_value = resp
    return client


def _patch_conversion():
    """测试环境不依赖真实 ffmpeg：时长探测失败（走整段路径）、跳过 wav 转换。"""
    import contextlib

    dur = patch(
        "app.core.transcribers.cloud_asr._audio_duration_sec",
        return_value=None,
    )
    conv = patch(
        "app.core.transcribers.cloud_asr._to_wav_16k_mono",
        side_effect=lambda p: (open(p, "rb").read(), False),
    )

    @contextlib.contextmanager
    def _both():
        with dur, conv:
            yield

    return _both()


def test_parses_verbose_json_segments_and_timestamps(tmp_path):
    f = tmp_path / "a.mp3"
    f.write_bytes(b"dummy")
    payload = {
        "text": "你好世界",
        "segments": [
            {"start": 0.0, "end": 1.5, "text": "你好"},
            {"start": 1.5, "end": 3.0, "text": "世界"},
        ],
    }
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    client = AsyncMock()
    client.post.return_value = resp

    client = _mock_client(resp)
    with _patch_conversion(), patch(
        "app.core.transcribers.cloud_asr.httpx.AsyncClient",
        return_value=client,
    ):
        t = CloudASRTranscriber("http://asr:8000", api_key="local")
        tr = asyncio.run(t.transcribe(_media(str(f))))

    assert tr.source == "cloud_asr"
    assert tr.raw_text == "你好世界"
    assert len(tr.segments) == 2
    assert tr.segments[0].text == "你好"
    assert tr.segments[0].start_sec == 0.0
    assert tr.segments[1].end_sec == 3.0

    args, kwargs = client.post.call_args
    assert kwargs["data"]["language"] == "zh"
    assert kwargs["data"]["response_format"] == "verbose_json"
    # 防幻觉/重复循环参数必须随请求发送（VAD + 静音暂停 + 不以上文为条件）
    assert kwargs["data"]["vad_filter"] == "true"
    assert kwargs["data"]["hallucination_silence_threshold"] == "2"
    assert kwargs["data"]["condition_on_previous_text"] == "false"
    assert "Authorization" in kwargs["headers"]


def test_empty_segment_text_is_skipped(tmp_path):
    f = tmp_path / "a.mp3"
    f.write_bytes(b"dummy")
    payload = {
        "text": "有效文本",
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "   "},
            {"start": 1.0, "end": 2.0, "text": "有效"},
        ],
    }
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    client = AsyncMock()
    client.post.return_value = resp

    client = _mock_client(resp)
    with _patch_conversion(), patch(
        "app.core.transcribers.cloud_asr.httpx.AsyncClient",
        return_value=client,
    ):
        t = CloudASRTranscriber("http://asr:8000")
        tr = asyncio.run(t.transcribe(_media(str(f))))

    assert len(tr.segments) == 1
    assert tr.segments[0].text == "有效"


def test_service_error_payload_skips_chunk_instead_of_raise(tmp_path):
    """服务端错误也走 HTTP 200（{"error": {...}}）：不再像旧逻辑那样静默返回空
    transcript 让下游幻觉，而是走块级降级——该块被跳过并记 warning；整段都跳过时
    transcribe 返回空 transcript（pipeline 的 note 阶段会因空文本抛错，避免空笔记入库）。
    """
    f = tmp_path / "a.mp3"
    f.write_bytes(b"dummy")
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "error": {
            "message": "torchaudio[LibsndfileError: Format not recognised.]",
            "type": "invalid_request_error",
        }
    }
    client = _mock_client(resp)
    with _patch_conversion(), patch(
        "app.core.transcribers.cloud_asr.httpx.AsyncClient",
        return_value=client,
    ):
        t = CloudASRTranscriber("http://asr:8000", provider="sensevoice")
        # 不抛异常：错误以块级跳过处理，保住链路
        tr = asyncio.run(t.transcribe(_media(str(f))))
    # 全部跳过 → 空 segments（非空 transcript 不会误导下游）
    assert tr.segments == []


def test_rejects_non_audio_media():
    t = CloudASRTranscriber("http://asr:8000")
    with pytest.raises(ValueError):
        asyncio.run(t.transcribe(_media("/nonexistent", kind="image")))


def test_to_wav_16k_mono_converts_compressed_audio(tmp_path):
    """m4a/mp3 必须被转成 wav 16k mono（服务端 soundfile 解不了压缩格式）。"""
    import shutil
    import subprocess

    from app.core.transcribers.cloud_asr import _to_wav_16k_mono

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available")

    src = tmp_path / "tone.wav"
    m4a = tmp_path / "tone.m4a"
    # 生成一段 1 秒 440Hz wav，再压成 m4a
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-ar", "16000", "-ac", "1", str(src)],
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src), "-c:a", "aac", str(m4a)],
        check=True,
    )
    data, converted = _to_wav_16k_mono(str(m4a))
    assert converted is True
    # wav 文件头 "RIFF"
    assert data[:4] == b"RIFF"
    # 16k mono：wav fmt chunk 采样率字段
    assert data[24:28] == (16000).to_bytes(4, "little")


def test_chunks_long_audio_and_offsets(tmp_path):
    """长音频切块：>60s 切成多块，且每块是 16k mono wav。"""
    import shutil
    import subprocess

    from app.core.transcribers.cloud_asr import _chunk_audio_16k_mono

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available")

    src = tmp_path / "long.wav"
    # 生成 130 秒音频（sine 2s 循环拼接），确保 >60s 触发切块
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=130",
         "-ar", "16000", "-ac", "1", str(src)],
        check=True,
    )
    chunks = _chunk_audio_16k_mono(str(src))
    assert len(chunks) >= 3  # 130s / 60s -> 至少 3 块（60+60+10）
    for c in chunks:
        assert c[:4] == b"RIFF"
        # 采样率 16000
        assert c[24:28] == (16000).to_bytes(4, "little")
