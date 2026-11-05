import json
import subprocess
import sys
import types

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
            seg(86.28, " More aid facilities now forography"),  # starts at the very end
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
        raise AssertionError("whisper must not be called")

    monkeypatch.setattr(transcribe, "_whisper", never)
    t = transcribe.transcribe(tmp_path)
    assert t == {"language": None, "text": "", "segments": [], "has_speech": False}


class FakeModel:
    def __init__(self, error=None):
        self.error = error

    def transcribe(self, audio):
        def segments():
            if self.error:  # real errors come while reading the segments
                raise self.error
            yield from ()

        return segments(), type("Info", (), {"language": "id"})


@pytest.fixture
def fake_models(monkeypatch):
    """Records which devices load. `broken` holds the device -> error map."""
    loaded = []
    broken = {}

    def model(name, device):
        loaded.append(device)
        return FakeModel(broken.get(device))

    monkeypatch.setattr(transcribe, "_faster_model", model)
    monkeypatch.setattr(transcribe, "_cuda_failed", False)
    monkeypatch.setattr(transcribe, "model_name", lambda: "m")
    return loaded, broken


def set_device(monkeypatch, device):
    from prism import config

    s = config.Settings(transcribe=config.Transcribe(device=device))
    monkeypatch.setattr(transcribe, "settings", s)


CUBLAS = RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")


def test_missing_cuda_falls_back_to_cpu_and_stays_there(
    fake_models, monkeypatch, capsys
):
    loaded, broken = fake_models
    broken["auto"] = CUBLAS
    set_device(monkeypatch, "auto")
    assert transcribe._faster_with_fallback(None)["language"] == "id"
    assert loaded == ["auto", "cpu"]
    assert "CPU" in capsys.readouterr().err
    transcribe._faster_with_fallback(None)
    assert loaded == ["auto", "cpu", "cpu"]


def test_other_errors_are_not_swallowed(fake_models, monkeypatch):
    loaded, broken = fake_models
    broken["auto"] = RuntimeError("invalid audio")
    set_device(monkeypatch, "auto")
    with pytest.raises(RuntimeError, match="invalid audio"):
        transcribe._faster_with_fallback(None)
    assert loaded == ["auto"]


def test_explicit_cuda_never_falls_back(fake_models, monkeypatch):
    loaded, broken = fake_models
    broken["cuda"] = CUBLAS
    set_device(monkeypatch, "cuda")
    with pytest.raises(RuntimeError, match="cublas"):
        transcribe._faster_with_fallback(None)
    assert loaded == ["cuda"]


def test_device_cpu_from_config(fake_models, monkeypatch):
    loaded, _ = fake_models
    set_device(monkeypatch, "cpu")
    transcribe._faster_with_fallback(None)
    assert loaded == ["cpu"]


def test_cpu_uses_int8(monkeypatch):
    seen = {}

    def fake(name, device, compute_type):
        seen[device] = compute_type

    # not installed on Apple Silicon, which uses mlx
    module = types.SimpleNamespace(WhisperModel=fake)
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    transcribe._faster_model.cache_clear()
    transcribe._faster_model("m", "cpu")
    transcribe._faster_model("m", "auto")
    transcribe._faster_model.cache_clear()
    assert seen == {"cpu": "int8", "auto": "auto"}
