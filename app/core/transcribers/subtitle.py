from app.core.fetchers.base import FetchedMedia
from app.core.transcribers.base import Segment, Transcript


class SubtitleTranscriber:
    """字幕直用：抓到的字幕已带时间戳，跳过 ASR。"""

    name = "subtitle"

    async def transcribe(self, media: FetchedMedia) -> Transcript:
        if media.kind != "subtitle" or not (media.subtitle_text or media.meta.get("segments")):
            raise ValueError("subtitle transcriber requires subtitle media")
        raw = media.subtitle_text or ""
        segments = [
            Segment(start_sec=s, end_sec=e, text=t)
            for s, e, t in media.meta.get("segments", [])
        ]
        return Transcript(segments=segments, raw_text=raw, source="subtitle")
