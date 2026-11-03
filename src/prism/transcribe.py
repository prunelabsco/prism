"""Video -> transcript.json via whisper."""

import functools
import importlib.util
import json
import subprocess
from pathlib import Path

from prism.config import settings

MODELS = {
    "mlx": "mlx-community/whisper-large-v3-turbo",
    "faster-whisper": "large-v3-turbo",
}

# segments with a "no speech" probability this high are dropped (whisper default)
NO_SPEECH_PROB = 0.6
# whisper pads audio to 30 s windows, so after speech ends it may invent
# segments past the end of the video (seen in 4 of 22 reels)
TAIL_SLACK_S = 0.5


class TranscribeError(Exception):
    pass


def has_audio(video: Path) -> bool:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, check=True,
        ).stdout  # fmt: skip
    except (OSError, subprocess.CalledProcessError) as e:
        raise TranscribeError(f"ffprobe failed: {e}") from e
    return bool(out.strip())


def duration(video: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, check=True,
        ).stdout  # fmt: skip
        return float(out.strip())
    except (OSError, subprocess.CalledProcessError, ValueError) as e:
        raise TranscribeError(f"ffprobe duration failed: {e}") from e


def backend() -> str:
    """mlx-whisper on Apple Silicon, faster-whisper elsewhere (pyproject picks one)."""
    return "mlx" if importlib.util.find_spec("mlx_whisper") else "faster-whisper"


def model_name() -> str:
    return settings.transcribe.model or MODELS[backend()]


@functools.cache
def _faster_model(name: str):
    from faster_whisper import WhisperModel

    # auto: CUDA if present, else CPU with the fastest compute type it supports
    return WhisperModel(name, device="auto", compute_type="auto")


def _whisper(video: Path) -> dict:
    # heavy imports, only loaded when something needs transcribing
    if backend() == "mlx":
        import mlx_whisper

        return mlx_whisper.transcribe(
            str(video), path_or_hf_repo=model_name(), language=None
        )
    segments, info = _faster_model(model_name()).transcribe(str(video))
    return {
        "language": info.language,
        "segments": [
            {"start": s.start, "end": s.end, "text": s.text,
             "no_speech_prob": s.no_speech_prob}
            for s in segments
        ],
    }  # fmt: skip


def is_complete(out_dir: Path) -> bool:
    return (out_dir / "transcript.json").exists()


def transcribe(out_dir: Path) -> dict:
    """Write transcript.json. An empty transcript (has_speech=false) is valid."""
    video = out_dir / "video.mp4"
    result: dict = {"language": None, "segments": []}
    limit = 0.0
    if has_audio(video):
        limit = duration(video)
        try:
            result = _whisper(video)
        except Exception as e:  # mlx/ffmpeg may raise anything
            raise TranscribeError(f"whisper failed: {e}") from e

    # ponytail: songs with lyrics count as speech, good enough for curation

    segments = [
        {
            "start": round(s["start"], 2),
            "end": round(s["end"], 2),
            "text": s["text"].strip(),
        }
        for s in result.get("segments", [])
        if s.get("no_speech_prob", 0) < NO_SPEECH_PROB
        and s["text"].strip()
        and s["start"] < limit - TAIL_SLACK_S
        and s["end"] <= limit + TAIL_SLACK_S
    ]
    t = {
        "language": result.get("language"),
        "text": " ".join(s["text"] for s in segments),
        "segments": segments,
        "has_speech": bool(segments),
    }
    (out_dir / "transcript.json").write_text(
        json.dumps(t, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return t
