import asyncio

from app.core.fetchers.base import FetchedMedia
from app.core.transcribers.base import Segment, Transcript


class WhisperTranscriber:
    """本地 faster-whisper 转写（int8 量化，纯 CPU）。

    模型懒加载且可能触发下载（数百 MB~GB），必须在 to_thread 中执行，
    避免阻塞事件循环（真实部署首次运行会下载模型）。
    """

    name = "whisper"

    def __init__(
        self,
        model_size: str = "large-v3",
        model: object | None = None,
        device: str = "cpu",
        compute_type: str = "int8",
        model_dir: str | None = None,
    ):
        self._model_size = model_size
        self._model = model
        self._device = device
        self._compute_type = compute_type
        self._model_dir = model_dir

    def _get_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self._model_size,
                device=self._device,
                compute_type=self._compute_type,
                download_root=self._model_dir,  # 持久化到 /data/models
            )
        return self._model

    async def transcribe(self, media: FetchedMedia) -> Transcript:
        if media.path is None or media.kind not in ("audio", "video"):
            raise ValueError("whisper transcriber requires audio/video media")

        def _run():
            model = self._get_model()  # 懒加载（含模型下载），线程内执行
            result = model.transcribe(media.path, language="zh", vad_filter=True)
            # faster-whisper 1.x 返回 (segments 生成器, info)，兼容旧版直接返回可迭代
            if isinstance(result, tuple):
                result = result[0]
            return list(result)

        result = await asyncio.to_thread(_run)
        segments = [
            Segment(start_sec=float(s.start), end_sec=float(s.end), text=s.text.strip())
            for s in result
            if s.text.strip()
        ]
        raw = "\n".join(s.text for s in segments)
        return Transcript(segments=segments, raw_text=raw, source="whisper")
