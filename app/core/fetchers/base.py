from dataclasses import dataclass, field
from typing import Literal, Protocol

MediaKind = Literal["subtitle", "audio", "video", "direct"]


@dataclass
class FetchedMedia:
    """获取层的产物：字幕 / 音频 / 视频 / 直读 URL。"""

    kind: MediaKind
    path: str | None = None
    subtitle_text: str | None = None
    url: str | None = None
    meta: dict = field(default_factory=dict)


class Fetcher(Protocol):
    name: str

    async def fetch(self, url: str, workdir: str) -> FetchedMedia: ...
