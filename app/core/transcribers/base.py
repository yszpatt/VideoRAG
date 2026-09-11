from dataclasses import dataclass, field
from typing import Protocol

from app.core.fetchers.base import FetchedMedia


@dataclass
class Segment:
    start_sec: float
    end_sec: float
    text: str
    speaker: str | None = None
    # 来源：speech=语音转写（默认）| subtitle=字幕 | ocr=画面文字（E3 视觉旁路）| vlm=画面描述
    source: str = "speech"


@dataclass
class Transcript:
    segments: list[Segment]
    raw_text: str
    source: str
    meta: dict = field(default_factory=dict)


class Transcriber(Protocol):
    name: str

    async def transcribe(self, media: FetchedMedia) -> Transcript: ...
