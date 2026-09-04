import pytest

from app.core.fetchers.base import FetchedMedia
from app.core.transcribers.base import Segment, Transcript
from app.core.transcribers.subtitle import SubtitleTranscriber
from app.core.transcribers.whisper import WhisperTranscriber


def test_subtitle_transcriber_uses_timestamped_segments():
    media = FetchedMedia(
        kind="subtitle",
        subtitle_text="大家好\n欢迎观看",
        meta={"segments": [(0.0, 2.5, "大家好"), (2.5, 5.0, "欢迎观看")]},
    )
    tr = SubtitleTranscriber()
    transcript = _run(tr, media)
    assert transcript.source == "subtitle"
    assert transcript.raw_text == "大家好\n欢迎观看"
    assert [s.start_sec for s in transcript.segments] == [0.0, 2.5]
    assert transcript.segments[0].text == "大家好"


def test_subtitle_transcriber_requires_text():
    media = FetchedMedia(kind="subtitle", subtitle_text=None, meta={})
    tr = SubtitleTranscriber()
    with pytest.raises(ValueError):
        _run(tr, media)


class FakeSeg:
    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.text = text


class FakeModel:
    def transcribe(self, path, **kwargs):
        assert str(path).endswith(".mp3")
        self.calls = kwargs
        # faster-whisper 1.x 返回 (segments 生成器, info)
        return (
            iter([FakeSeg(0.0, 1.5, "hello"), FakeSeg(1.5, 3.0, "world")]),
            None,
        )


def test_whisper_transcriber_uses_model():
    model = FakeModel()
    media = FetchedMedia(kind="audio", path="/tmp/audio.mp3")
    tr = WhisperTranscriber(model_size="tiny", model=model)
    transcript = _run(tr, media)

    assert transcript.source == "whisper"
    assert transcript.raw_text == "hello\nworld"
    assert [s.text for s in transcript.segments] == ["hello", "world"]
    assert transcript.segments[0].start_sec == 0.0
    assert model.calls["language"] == "zh"


def _run(tr, media):
    import asyncio

    return asyncio.run(tr.transcribe(media))
