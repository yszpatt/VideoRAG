# SenseVoice ASR 服务（Windows / GPU，端口 9991）

基于 [jackuh105/openai-sensevoice-stt](https://github.com/jackuh105/openai-sensevoice-stt) 的
`funasr_http_server.py`（GitHub 元版本），本目录存放 **打了补丁** 的版本，使其返回带真实时间戳的 `segments`。

## 为什么需要打补丁

上游版本在 `verbose_json` 下只返回 `{task, language, duration, text}`，
把 FunASR 原生的词级时间戳丢弃了。videoRAG 的切片/检索/跳转依赖 `segments[].start/end`，
没有时间戳会导致检索命中的时间点不准。

## 时间戳实现机制（关键，已对照 FunASR 源码确认）

- SenseVoice 模型原生支持 **`output_timestamp=True`**（`funasr/models/sense_voice/model.py`）：
  通过 CTC forced align 返回词级时间戳 `rec_result["timestamp"]`（毫秒）+ `rec_result["words"]`。
- `inference_with_vad` 会自动给每段时间戳**加 VAD 偏移**（`funasr/auto/auto_model.py` 1018-1027 行），
  所以 `result["timestamp"]` 是**全局毫秒**时间戳，可直接使用。
- **不需要 punc 标点模型、不需要 `sentence_timestamp` 参数**——SenseVoice 没有
  `sentence_info`，之前试图用 `sentence_timestamp` 拿句级时间戳的路线是死路（会让
  AutoModel 去找 punc 模型，Windows 上表现为 `FileNotFoundError [WinError 2]`）。
- 本补丁拿到词级时间戳后，按**词间静音 gap > 0.6s** 的位置切句，聚合出句子级 segments。

## 补丁内容（`server.patch` 为相对 GitHub 元版本的完整 diff）

| # | 改动 | 说明 |
|---|------|------|
| 1 | `inference_params` 加 `"output_timestamp": True` | SenseVoice 原生词级时间戳 |
| 2 | 新增 `_build_segments_from_words()` | 词级时间戳按 gap>0.6s 聚合成句子级 segments（毫秒→秒） |
| 3 | `format_transcription_response` 加 `segments` 形参 | verbose_json 返回 `"segments": segments or []` |
| 4 | 诊断日志 `REC KEYS=... words_len=... segments_len=...` | 便于确认时间戳是否产出 |
| 5 | 异常透传 | 错误信息带 `type(e).__name__`，便于定位 |

## 部署步骤

```powershell
# 0) 装 uv（若未装）
irm https://astral.sh/uv/install.ps1 | iex

# 1) 拿代码
git clone https://github.com/jackuh105/openai-sensevoice-stt.git
cd openai-sensevoice-stt

# 2) 用本目录的 funasr_http_server.py 覆盖仓库里的同名文件
#    （或手动应用 server.patch）

# 3) 复用已下载的模型
$env:MODELSCOPE_CACHE = "D:\sensevoice\models"

# 4) 启动（GPU）
uv sync
uv run funasr_http_server.py --port 9991 --device cuda `
  --model_dir "iic/SenseVoiceSmall" --use_itn True --merge_vad True --merge_length_s 15
```

> 无需额外下载 punc 模型。若某音频没有词级时间戳（理论上不会），segments 为空，
> 下游 videoRAG 会自动兜底成整段。

## 验证

```bash
curl -s http://192.168.x.x:9991/v1/audio/transcriptions \
  -F "file=@/tmp/clip.mp3" -F "model=sensevoice" -F "response_format=verbose_json"
```

期望：`text` 有内容，且 `segments` 数组的每一项都带 `start` / `end` / `text`。
日志里会出现 `REC KEYS=[...] words_len=N segments_len=M`，M > 0 即成功。

## videoRAG 侧对接

`.env` / Web 设置页：

```env
CLOUD_ASR_PROVIDER=sensevoice
CLOUD_ASR_BASE_URL=http://192.168.x.x:9991
CLOUD_ASR_MODEL=sensevoice
CLOUD_ASR_KEY=local
```

`CLOUD_ASR_PROVIDER` 不含 `whisper` 时，videoRAG 不会发送 faster-whisper 专有的
`vad_filter` / `hallucination_silence_threshold` / `condition_on_previous_text` 参数。
