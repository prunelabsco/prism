import json
import subprocess

import pytest

from prism import transcribe


def seg(start, text, nsp=0.1):
    return {"start": start, "end": start + 1.234, "text": text, "no_speech_prob": nsp}


@pytest.fixture
def reel(tmp_path, monkeypatch):
    (tmp_path / "video.mp4").write_bytes(b"v")
    monkeypatch.setattr(transcribe, "has_audio", lambda v: True)
    monkeypatch.setattr(transcribe, "duration", lambda v: 100.0)
    return tmp_path


def test_filters_no_speech_and_empty(reel, monkeypatch):
    res = {
        "language": "id",
        "segments": [
            seg(0, " Halo semua"),
            seg(2, " ♪", 0.9),
            seg(4, "  "),
            seg(6, " oke"),
        ],
    }
    monkeypatch.setattr(transcribe, "_whisper", lambda v: res)
    t = transcribe.transcribe(reel)
    assert t == {
        "language": "id",
        "text": "Halo semua oke",
        "segments": [
            {"start": 0, "end": 1.23, "text": "Halo semua"},
            {"start": 6, "end": 7.23, "text": "oke"},
        ],
        "has_speech": True,
    }
    assert json.loads((reel / "transcript.json").read_text()) == t
    assert transcribe.is_complete(reel)


def test_drops_segments_past_video_end(reel, monkeypatch):
    monkeypatch.setattr(transcribe, "duration", lambda v: 86.31)
    res = {
        "language": "en",
        "segments": [
            seg(83.3, " Now follow for more."),
            seg(86.28, " More aid facilities now forography"),  # mulai di ujung
            {
                "start": 97.4,
                "end": 108.56,
                "text": " Don't forget",
                "no_speech_prob": 0.1,
            },
        ],
    }
    monkeypatch.setattr(transcribe, "_whisper", lambda v: res)
    t = transcribe.transcribe(reel)
    assert [s["text"] for s in t["segments"]] == ["Now follow for more."]


def test_music_only_has_no_speech(reel, monkeypatch):
    res = {"language": "en", "segments": [seg(0, " Thank you.", 0.95)]}
    monkeypatch.setattr(transcribe, "_whisper", lambda v: res)
    t = transcribe.transcribe(reel)
    assert t["has_speech"] is False and t["segments"] == [] and t["text"] == ""


def test_whisper_error_wrapped(reel, monkeypatch):
    def boom(v):
        raise RuntimeError("model rusak")

    monkeypatch.setattr(transcribe, "_whisper", boom)
    with pytest.raises(transcribe.TranscribeError):
        transcribe.transcribe(reel)
    assert not transcribe.is_complete(reel)


def test_silent_video_skips_whisper(tmp_path, monkeypatch):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=black:s=64x64:d=1",
         "-y", str(tmp_path / "video.mp4")],
        check=True,
    )  # fmt: skip
    assert transcribe.has_audio(tmp_path / "video.mp4") is False

    def never(v):
        raise AssertionError("whisper tidak boleh dipanggil")

    monkeypatch.setattr(transcribe, "_whisper", never)
    t = transcribe.transcribe(tmp_path)
    assert t == {"language": None, "text": "", "segments": [], "has_speech": False}
