import argparse
import io
import logging
import os
import shutil
import traceback
import uuid
from pathlib import Path
from opencc import OpenCC
from typing import Optional

import aiofiles
import ffmpeg
import numpy as np
import soundfile as sf
import torch
import torchaudio
import uvicorn
from fastapi import FastAPI, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from modelscope.utils.logger import get_logger

from funasr import AutoModel
from funasr.utils.postprocess_utils import rich_transcription_postprocess

logger = get_logger(log_level=logging.INFO)
logger.setLevel(logging.INFO)


def process_audio_bytes_torchaudio(audio_bytes: bytes) -> np.ndarray:
    """
    使用 soundfile 在記憶體中處理音頻，無需臨時文件
    
    Args:
        audio_bytes: 原始音頻文件的 bytes
    
    Returns:
        numpy array of audio samples (16kHz, mono, float32)
    """
    # 從 bytes 加載音頻，使用 soundfile
    audio_buffer = io.BytesIO(audio_bytes)
    
    # 使用 soundfile 讀取音頻
    waveform, sample_rate = sf.read(audio_buffer, dtype='float32')
    
    # soundfile 返回的是 (samples, channels) 格式，需要轉置為 (channels, samples)
    if waveform.ndim == 1:
        # 單聲道
        waveform = waveform[np.newaxis, :]  # 添加 channel 維度
    else:
        # 多聲道，轉置
        waveform = waveform.T
    
    # 轉換為 torch tensor 以便處理
    waveform_tensor = torch.from_numpy(waveform)
    
    # 轉換為單聲道（如果是多聲道）
    if waveform_tensor.shape[0] > 1:
        waveform_tensor = torch.mean(waveform_tensor, dim=0, keepdim=True)
    
    # 重採樣到 16kHz（如果需要）
    if sample_rate != 16000:
        resampler = torchaudio.transforms.Resample(sample_rate, 16000)
        waveform_tensor = resampler(waveform_tensor)
    
    # 轉換為 numpy array
    audio_array = waveform_tensor.squeeze().numpy()
    
    return audio_array

def process_audio_bytes_ffmpeg(audio_path: str) -> bytes:
    """
    使用 ffmpeg 處理音頻（備用方案）
    
    Args:
        audio_path: 臨時音頻文件路徑
    
    Returns:
        PCM audio bytes (16kHz, mono, s16le)
    """
    audio_bytes, _ = (
        ffmpeg.input(audio_path, threads=0)
        .output("-", format="s16le", acodec="pcm_s16le", ac=1, ar=16000)
        .run(cmd=["ffmpeg", "-nostdin"], capture_stdout=True, capture_stderr=True)
    )
    return audio_bytes

def llm_correction(text: str) -> str:
    """
    使用 LLM 進行文本校正
    
    Args:
        text: 需要校正的文本
    
    Returns:
        校正後的文本
    """
    try:
        response = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL"),
            messages=[
                {"role": "system", "content": LLM_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Error in llm_correction: {e}")
        return text

parser = argparse.ArgumentParser()
parser.add_argument(
    "--host", type=str, default="0.0.0.0", required=False, help="host ip, localhost, 0.0.0.0"
)
parser.add_argument("--port", type=int, default=8000, required=False, help="server port")
parser.add_argument(
    "--model_dir",
    type=str,
    default="iic/SenseVoiceSmall",
    help="SenseVoice model directory path",
)
parser.add_argument(
    "--remote_code",
    type=str,
    default="./model.py",
    help="Path to remote code file for SenseVoice model",
)
parser.add_argument(
    "--vad_model",
    type=str,
    default="fsmn-vad",
    help="VAD model name",
)
parser.add_argument(
    "--vad_kwargs",
    type=int,
    default=30000,
    help="max_single_segment_time for VAD in milliseconds",
)
parser.add_argument("--device", type=str, default="cpu", help="cuda or cpu")
parser.add_argument("--ncpu", type=int, default=4, help="cpu cores")
parser.add_argument(
    "--language",
    type=str,
    default="auto",
    help="Language for ASR (auto, zh, en, yue, ja, ko)",
)
parser.add_argument(
    "--use_itn",
    type=bool,
    default=True,
    help="Use inverse text normalization",
)
parser.add_argument(
    "--merge_vad",
    type=bool,
    default=True,
    help="Merge VAD segments",
)
parser.add_argument(
    "--merge_length_s",
    type=int,
    default=15,
    help="Maximum length in seconds for merging VAD segments",
)
parser.add_argument("--certfile", type=str, default=None, required=False, help="certfile for ssl")
parser.add_argument("--keyfile", type=str, default=None, required=False, help="keyfile for ssl")
parser.add_argument("--temp_dir", type=str, default="temp_dir/", required=False, help="temp dir")
parser.add_argument("--llm_correct", action="store_true", help="enable llm correction")
args = parser.parse_args()
logger.info("-----------  Configuration Arguments -----------")
for arg, value in vars(args).items():
    logger.info("%s: %s" % (arg, value))
logger.info("------------------------------------------------")

os.makedirs(args.temp_dir, exist_ok=True)

logger.info("model loading")
logger.info("ffmpeg binary: %s", shutil.which("ffmpeg") or "NOT_FOUND on PATH")
# load SenseVoice model
model_dir = args.model_dir
model = AutoModel(
    model=model_dir,
    trust_remote_code=True,
    remote_code=args.remote_code,
    vad_model=args.vad_model,
    vad_kwargs={"max_single_segment_time": args.vad_kwargs},
    device=args.device,
    ncpu=args.ncpu,
    disable_pbar=True,
    disable_log=True,
)
logger.info("loaded models!")
import funasr
import inspect as _inspect
logger.info("funasr version: %s", getattr(funasr, "__version__", "unknown"))

# ---------------------------------------------------------------------------
# 老版本 FunASR 兼容补丁（双保险 + 诊断）：
# 老版 SenseVoice 的 timestamp 元素可能是 [word, start_ms, end_ms]（词文本前缀），
# 而老版 auto_model.inference_with_vad 做 VAD 偏移合并时直接 `t[0] += vadsegments[j][0]`，
# 把 word(str) += int 抛 TypeError。
#   补丁 1：post 方法（若存在）——统一 timestamp 元素为纯 [start_ms, end_ms] + 提取 words
#   补丁 2：inference 方法（若存在）——调用返回前把结果里 timestamp 结构归一化
#   诊断：funasr 版本 / 模型类型 / 方法是否存在 / 方法源码（塞进错误响应的 diagnostics）
# ---------------------------------------------------------------------------
_sv_model = getattr(model, "model", None)
_sv_type = type(_sv_model).__name__ if _sv_model is not None else None
_sv_has_post = hasattr(_sv_model, "post") if _sv_model is not None else False
_sv_has_inference = hasattr(_sv_model, "inference") if _sv_model is not None else False
_sv_post_src = ""
_sv_inf_src = ""
_patch_applied = False

if _sv_model is not None:
    for _name, _bucket in (("post", "_sv_post_src"), ("inference", "_sv_inf_src")):
        _m = getattr(_sv_model, _name, None)
        if _m is not None:
            try:
                _src = _inspect.getsource(_m)
                globals()[_bucket] = _src[-1500:] if _name == "post" else _src[:800]
            except Exception:
                pass

    # --- 补丁 1：post 方法（若存在） ---
    if _sv_has_post:
        _orig_post = _sv_model.post

        def _patched_post(timestamp):
            out = _orig_post(timestamp)
            if isinstance(out, tuple) and len(out) == 2:
                ts, words = out
            else:
                ts, words = out, []
            clean, clean_words = [], []
            for t in ts:
                try:
                    if len(t) >= 3:  # [word, start, end] -> [start, end] + 提取 word
                        clean.append([int(t[1]), int(t[2])])
                        clean_words.append(str(t[0]).replace("▁", ""))
                    else:            # [start, end]
                        clean.append([int(t[0]), int(t[1])])
                except (TypeError, ValueError) as ex:
                    logger.warning("post patch skip t=%s: %s", t, ex)
                    continue
            if not clean_words:
                clean_words = [str(w).replace("▁", "") for w in (words or [])]
            return clean, clean_words

        _sv_model.post = _patched_post
        logger.info("patched SenseVoice.post")
        _patch_applied = True

    # --- 补丁 2：inference 方法（若存在）——返回前归一化 timestamp 结构 ---
    if _sv_has_inference:
        _orig_inference = _sv_model.inference

        def _patched_inference(*args, **kwargs):
            try:
                out = _orig_inference(*args, **kwargs)
            except IndexError as _ie:
                # 长音频越界保护：SenseVoice model.py 的 output_timestamp 分支里
                #   tokens = tokenizer.text2tokens(text)[4:]
                #   for ...: if pred_token != 0: timestamp.append([tokens[token_id], ...])
                # CTC forced align 对长音频会把同一 token 的帧拆成多组，
                # 非 blank 分组数 > 文本 token 数 → tokens[token_id] 越界。
                # 兜底：去掉 output_timestamp 重试一次（文本完整，无时间戳）。
                logger.warning(
                    "inference IndexError(%s); retrying without output_timestamp",
                    _ie,
                )
                _kw = dict(kwargs)
                _kw.pop("output_timestamp", None)
                try:
                    out = _orig_inference(*args, **_kw)
                except Exception as _e2:
                    raise RuntimeError(
                        f"inference failed with output_timestamp "
                        f"({type(_ie).__name__}: {_ie}) and without "
                        f"({type(_e2).__name__}: {_e2})"
                    ) from _e2
                # 无时间戳重试成功后，给结果补空 timestamp 保持结构一致
                try:
                    _res = out[0] if isinstance(out, (tuple, list)) else out
                    if isinstance(_res, list):
                        for r in _res:
                            if isinstance(r, dict) and "timestamp" not in r:
                                r["timestamp"] = []
                except Exception:
                    pass
            try:
                results = out[0] if isinstance(out, (tuple, list)) else out
                if isinstance(results, list):
                    for r in results:
                        if not isinstance(r, dict):
                            continue
                        ts = r.get("timestamp")
                        if ts:
                            clean, cw = [], []
                            for t in ts:
                                try:
                                    if len(t) >= 3:
                                        clean.append([int(t[1]), int(t[2])])
                                        cw.append(str(t[0]).replace("▁", ""))
                                    elif len(t) >= 2:
                                        clean.append([int(t[0]), int(t[1])])
                                except (TypeError, ValueError):
                                    continue
                            r["timestamp"] = clean
                            if cw and "words" not in r:
                                r["words"] = cw
            except Exception as ex:
                logger.warning("inference patch post-process failed: %s", ex)
            return out

        _sv_model.inference = _patched_inference
        logger.info("patched SenseVoice.inference (timestamp 结构归一化 + 长音频 IndexError 兜底)")
        _patch_applied = True

logger.info(
    "ASR patch applied=%s sv_type=%s has_post=%s has_inference=%s",
    _patch_applied, _sv_type, _sv_has_post, _sv_has_inference,
)
# s2t converter
s2t_cc = OpenCC("s2t")

client = None
LLM_SYSTEM_PROMPT = None
if args.llm_correct:
    from openai import OpenAI
    from dotenv import load_dotenv
    env_path = Path.cwd() / ".env"
    load_dotenv(dotenv_path=env_path)
    client = OpenAI(base_url=os.getenv("OPENAI_BASE_URL"), api_key=os.getenv("OPENAI_API_KEY"))
    
    # 讀取 LLM 系統提示
    prompt_file = Path.cwd() / "prompts" / "llm_correction_system.txt"
    try:
        with open(prompt_file, "r", encoding="utf-8") as f:
            LLM_SYSTEM_PROMPT = f.read()
        logger.info(f"已從 {prompt_file} 載入 LLM 系統提示")
    except Exception as e:
        logger.error(f"無法讀取 LLM 系統提示文件 {prompt_file}: {e}")
        logger.error("將使用預設的系統提示")
        LLM_SYSTEM_PROMPT = """
You are an expert editor designed to post-process ASR (Automatic Speech Recognition) transcripts. 

Your task is to correct the provided text while strictly adhering to the following rules:

1. **Fix Errors:** Correct spelling, grammar, and punctuation mistakes.
2. **Contextual Correction:** Fix obvious phonetic mistranscriptions (homophones) based on the context.
3. **Formatting:** Restore proper capitalization for sentences and proper nouns.
4. **Preserve Meaning:** Do NOT change the original meaning, tone, or style of the speaker. Do NOT summarize or hallucinate information.
5. **Output Constraint:** Output ONLY the corrected text. Do not include any conversational fillers, introductions, or explanations (e.g., do not say "Here is the corrected text").
6. **Language:** Follow the original language of the transcript.
        """
    
    logger.info("2 Pass mode enable!")

app = FastAPI(title="FunASR")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

param_dict = {
    "language": args.language,
    "use_itn": args.use_itn,
    "merge_vad": args.merge_vad,
    "merge_length_s": args.merge_length_s,
    "output_dir": None,
}


# Helper functions for OpenAI API response formatting
def format_srt_time(seconds: float) -> str:
    """將秒數轉換為 SRT 時間格式 (HH:MM:SS,mmm)"""
    if seconds is None:
        return "00:00:10,000"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def format_vtt_time(seconds: float) -> str:
    """將秒數轉換為 VTT 時間格式 (HH:MM:SS.mmm)"""
    if seconds is None:
        return "00:00:10.000"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def format_srt(text: str, duration: float = None) -> str:
    """格式化為 SRT 字幕格式"""
    start_time = "00:00:00,000"
    end_time = format_srt_time(duration) if duration else "00:00:10,000"
    srt = f"1\n{start_time} --> {end_time}\n{text}\n"
    return srt


def format_vtt(text: str, duration: float = None) -> str:
    """格式化為 WebVTT 字幕格式"""
    start_time = "00:00:00.000"
    end_time = format_vtt_time(duration) if duration else "00:00:10.000"
    vtt = f"WEBVTT\n\n1\n{start_time} --> {end_time}\n{text}\n"
    return vtt


def _build_segments_from_words(rec_result: dict, gap_s: float = 0.6) -> list:
    """从 FunASR `output_timestamp=True` 的词级时间戳聚合出句子级 segments。

    SenseVoice 推理（funasr/models/sense_voice/model.py）在
    `output_timestamp=True` 时通过 CTC forced align 返回词级时间戳：
      rec_result["timestamp"] = [[start_ms, end_ms], ...]  （毫秒，与 words 对齐）
      rec_result["words"]     = [词, ...]
    这里把相邻词之间的静音 gap 超过 gap_s 秒的位置当作句子边界切段。
    不依赖任何标点（punc）模型——SenseVoice 本身没有 sentence_info。
    """
    words = rec_result.get("words") or []
    ts = rec_result.get("timestamp") or []
    if not words or len(ts) != len(words):
        return []
    segments = []
    cur_words, cur_start, prev_end = [], None, None
    for w, (s_ms, e_ms) in zip(words, ts):
        s, e = s_ms / 1000.0, e_ms / 1000.0
        if cur_start is None:
            cur_start = s
        if prev_end is not None and s - prev_end > gap_s and cur_words:
            seg_text = "".join(cur_words).strip()
            if seg_text:
                segments.append({
                    "start": round(cur_start, 3),
                    "end": round(prev_end, 3),
                    "text": seg_text,
                })
            cur_words, cur_start = [], s
        cur_words.append(w)
        prev_end = e
    if cur_words:
        seg_text = "".join(cur_words).strip()
        if seg_text:
            segments.append({
                "start": round(cur_start, 3),
                "end": round(prev_end or cur_start, 3),
                "text": seg_text,
            })
    return segments


def format_transcription_response(
    text: str, response_format: str = "json", duration: float = None,
    language: str = None, segments: list = None,
):
    """
    將辨識結果格式化為指定的回應格式
    
    Args:
        text: 辨識文本
        response_format: 回應格式 (json, text, verbose_json, srt, vtt)
        duration: 音頻時長（秒）
        language: 檢測到的語言
        segments: 帶時間戳的分段（verbose_json 使用）
    
    Returns:
        格式化後的回應
    """
    if response_format == "text":
        return text
    elif response_format == "json":
        return {"text": text}
    elif response_format == "verbose_json":
        return {
            "task": "transcribe",
            "language": language or "zh",
            "duration": duration,
            "text": text,
            "segments": segments or [],
        }
    elif response_format == "srt":
        return format_srt(text, duration)
    elif response_format == "vtt":
        return format_vtt(text, duration)
    else:
        # 預設返回 json
        return {"text": text}



@app.post("/recognition")
async def api_recognition(audio: UploadFile = File(..., description="audio file")):
    # 讀取上傳的文件到記憶體
    content = await audio.read()

    try:
        suffix = audio.filename.split(".")[-1] if "." in audio.filename else "wav"
        audio_path = f"{args.temp_dir}/{str(uuid.uuid1())}.{suffix}"
        async with aiofiles.open(audio_path, "wb") as out_file:
            await out_file.write(content)

        if "Fun-ASR-Nano" in args.model_dir:
            rec_results = model.generate(input=[audio_path], cache={}, batch_size=1, is_final=True, **param_dict)
        else:
            try:
                logger.info("使用 torchaudio 處理音頻...")
                audio_array = process_audio_bytes_torchaudio(content)
                rec_results = model.generate(input=audio_array, is_final=True, **param_dict)
            except Exception as torchaudio_error:
                # 如果 torchaudio 失敗，回退到 ffmpeg
                logger.warning(f"torchaudio 處理失敗：{torchaudio_error}，回退到 ffmpeg")
                audio_bytes = process_audio_bytes_ffmpeg(audio_path)
                rec_results = model.generate(input=audio_bytes, is_final=True, **param_dict)

        # 清理臨時文件
        if os.path.exists(audio_path):
            os.remove(audio_path)

    except Exception as e:
        logger.error(f"读取音频文件发生错误，错误信息：{e}")
        return {"msg": f"读取音频文件发生错误: {e}", "code": 1}
    
    # 檢查結果
    if not rec_results or len(rec_results) == 0:
        return {"text": "", "code": 0}
    
    rec_result = rec_results[0]
    if "text" not in rec_result or len(rec_result["text"]) == 0:
        return {"text": "", "code": 0}
    
    # 使用 rich_transcription_postprocess 處理結果
    text = rich_transcription_postprocess(rec_result["text"])
    text = s2t_cc.convert(text)
    
    # 簡化的回應格式
    ret = {"text": text, "code": 0}
    # logger.info(f"識別結果：{ret}")
    return ret



@app.post("/v1/audio/transcriptions")
async def openai_transcriptions(
    file: UploadFile = File(..., description="audio file"),
    model_name: Optional[str] = Form("sensevoice", alias="model", description="model to use"),
    language: Optional[str] = Form(None, description="language code (ISO-639-1)"),
    prompt: Optional[str] = Form(None, description="optional prompt"),
    response_format: Optional[str] = Form("json", description="response format"),
    temperature: Optional[float] = Form(0, description="sampling temperature"),
):
    """
    OpenAI-compatible audio transcription endpoint
    
    Compatible with OpenAI's /v1/audio/transcriptions API
    Supports response formats: json, text, verbose_json, srt, vtt
    """
    # 讀取上傳的文件到記憶體
    content = await file.read()
    
    # 準備推理參數
    inference_params = {
        "language": language or args.language,
        "use_itn": args.use_itn,
        "merge_vad": args.merge_vad,
        "merge_length_s": args.merge_length_s,
        "output_dir": None,
        # SenseVoice 原生词级时间戳：CTC forced align 返回
        # rec_result["timestamp"]（[[start_ms,end_ms],...]）+ rec_result["words"]
        "output_timestamp": True,
    }
    
    try:
        suffix = file.filename.split(".")[-1] if "." in file.filename else "wav"
        audio_path = f"{args.temp_dir}/{str(uuid.uuid1())}.{suffix}"
        async with aiofiles.open(audio_path, "wb") as out_file:
            await out_file.write(content)
        # 探测音频时长（用于末段 end 修正与 verbose_json 的 duration 字段）
        audio_duration = None
        try:
            audio_duration = sf.info(audio_path).duration
        except Exception as ex:
            logger.warning("sf.info 失败: %s", ex)
        if "Fun-ASR-Nano" in args.model_dir:
            rec_results = model.generate(input=[audio_path], cache={}, batch_size=1, is_final=True, **inference_params)
        else:
            try:
                logger.info("使用 torchaudio 處理音頻...")
                audio_array = process_audio_bytes_torchaudio(content)
                rec_results = model.generate(input=audio_array, is_final=True, **inference_params)
            except Exception as torchaudio_error:
                # 三级降级：torchaudio -> 文件路径（FunASR 内部解码） -> ffmpeg
                # 全部失败时返回每级异常 + torchaudio 完整 traceback，不再掩盖真因
                logger.warning(f"torchaudio 處理失敗：{torchaudio_error}，回退到文件路徑推理", exc_info=True)
                tb_torch = traceback.format_exc()[-4000:]
                try:
                    logger.info("使用文件路徑推理...")
                    rec_results = model.generate(input=[audio_path], cache={}, batch_size=1, is_final=True, **inference_params)
                except Exception as path_error:
                    logger.warning(f"文件路徑推理失敗：{path_error}，回退到 ffmpeg", exc_info=True)
                    try:
                        audio_bytes = process_audio_bytes_ffmpeg(audio_path)
                        rec_results = model.generate(input=audio_bytes, is_final=True, **inference_params)
                    except Exception as ffmpeg_error:
                        logger.error("ffmpeg 回退也失敗", exc_info=True)
                        return {
                            "error": {
                                "message": (
                                    f"torchaudio[{type(torchaudio_error).__name__}: {torchaudio_error}] "
                                    f"filepath[{type(path_error).__name__}: {path_error}] "
                                    f"ffmpeg[{type(ffmpeg_error).__name__}: {ffmpeg_error}] "
                                    f"(ffmpeg_binary={shutil.which('ffmpeg') or 'NOT_FOUND'})"
                                ),
                                "traceback": tb_torch,
                                "diagnostics": {
                                    "funasr_version": getattr(funasr, "__version__", "unknown"),
                                    "sv_model_type": _sv_type,
                                    "sv_has_post": _sv_has_post,
                                    "sv_has_inference": _sv_has_inference,
                                    "patch_applied": _patch_applied,
                                    "post_src_tail": _sv_post_src,
                                    "inference_src_head": _sv_inf_src,
                                },
                                "type": "invalid_request_error",
                            }
                        }
        
        # 清理臨時文件
        if os.path.exists(audio_path):
            os.remove(audio_path)
    except Exception as e:
        logger.error(f"音頻文件處理錯誤：{e}", exc_info=True)
        return {"error": {"message": f"Processing failed: {type(e).__name__}: {e}", "type": "invalid_request_error"}}

    # 執行辨識
    try:
        if not rec_results or len(rec_results) == 0:
            formatted_response = format_transcription_response("", response_format, language=language)
            if response_format in ["text", "srt", "vtt"]:
                return Response(content=formatted_response, media_type="text/plain")
            return formatted_response
        
        rec_result = rec_results[0]
        if "text" not in rec_result or len(rec_result["text"]) == 0:
            formatted_response = format_transcription_response("", response_format, language=language)
            if response_format in ["text", "srt", "vtt"]:
                return Response(content=formatted_response, media_type="text/plain")
            return formatted_response
        
        # 後處理文本
        text = rich_transcription_postprocess(rec_result["text"])
        text = s2t_cc.convert(text)
        if args.llm_correct:
            text = llm_correction(text)

        # 词级时间戳 -> 句子级 segments（无 punc 依赖）
        segments = _build_segments_from_words(rec_result)
        # 修正 end：老版 SenseVoice 段内词时间戳挤在段首，导致 end≈start。
        # 用后一段的 start 作为前一段的 end；末段 end 用音频总时长。
        for i in range(len(segments) - 1):
            if segments[i]["end"] < segments[i + 1]["start"]:
                segments[i]["end"] = segments[i + 1]["start"]
        if segments and audio_duration and segments[-1]["end"] < audio_duration:
            segments[-1]["end"] = round(audio_duration, 3)
        logger.info(
            "REC KEYS=%s words_len=%s segments_len=%s duration=%s",
            list(rec_result.keys()),
            len(rec_result.get("words") or []),
            len(segments),
            audio_duration,
        )

        # 格式化回應
        formatted_response = format_transcription_response(
            text=text,
            response_format=response_format,
            language=language,
            duration=audio_duration,
            segments=segments,
        )
        
        # logger.info(f"OpenAI API 辨識結果：{text[:100]}...")
        
        # 根據回應格式設定 Content-Type
        if response_format == "text":
            return Response(content=formatted_response, media_type="text/plain")
        elif response_format in ["srt", "vtt"]:
            return Response(content=formatted_response, media_type="text/plain")
        else:
            return formatted_response
            
    except Exception as e:
        logger.error(f"辨識過程發生錯誤：{e}")
        return {"error": {"message": str(e), "type": "server_error"}}

if __name__ == "__main__":
    uvicorn.run(
        app, host=args.host, port=args.port, ssl_keyfile=args.keyfile, ssl_certfile=args.certfile
    )