"""Video -> transcript.json via whisper."""

import functools
import importlib.util
import json
import logging
import subprocess
import sys
from pathlib import Path

from prism.config import settings

log = logging.getLogger("prism.transcribe")

MODELS = {
    "mlx": "mlx-community/whisper-large-v3-turbo",
    "faster-whisper": "large-v3-turbo",
}

# segments with a "no speech" probability this high are dropped (whisper default)
NO_SPEECH_PROB = 0.6
# whisper pads audio to 30 s windows, so after speech ends it may invent
# segments past the end of the video (seen in 4 of 22 reels)
TAIL_SLACK_S = 0.5
# e.g. "Library cublas64_12.dll is not found or cannot be loaded"
_CUDA_ERRORS = ("cublas", "cudnn", "cuda")


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
def _faster_model(name: str, device: str):
    from faster_whisper import WhisperModel

    # int8 on CPU: Windows crashed on auto with the GPU hidden, this one held
    compute = "int8" if device == "cpu" else "auto"
    return WhisperModel(name, device=device, compute_type=compute)


def _faster_transcribe(audio, device: str) -> dict:
    segments, info = _faster_model(model_name(), device).transcribe(audio)
    # segments is lazy: CUDA errors only show up while reading it
    return {
        "language": info.language,
        "segments": [
            {"start": s.start, "end": s.end, "text": s.text,
             "no_speech_prob": s.no_speech_prob}
            for s in segments
        ],
    }  # fmt: skip


_cuda_failed = False  # stays on CPU for the rest of the run after one failure


def _faster_with_fallback(audio) -> dict:
    """GPU machines without the CUDA libraries see the GPU, then fail on first use."""
    global _cuda_failed
    device = settings.transcribe.device
    if device == "auto" and _cuda_failed:
        device = "cpu"
    try:
        return _faster_transcribe(audio, device)
    except Exception as e:
        if device != "auto" or not any(m in str(e).lower() for m in _CUDA_ERRORS):
            raise
        log.warning("CUDA unusable, falling back to CPU: %s", e)
        print("CUDA isn't usable here, transcribing on CPU (slower).", file=sys.stderr)
        _cuda_failed = True
        return _faster_transcribe(audio, "cpu")


def _whisper(video: Path) -> dict:
    # heavy imports, only loaded when something needs transcribing
    if backend() == "mlx":
        import mlx_whisper

        return mlx_whisper.transcribe(
            str(video), path_or_hf_repo=model_name(), language=None
        )
    import numpy as np

    # decode with ffmpeg ourselves: faster-whisper's PyAV path breaks on av>=19
    pcm = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(video),
         "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
    return _faster_with_fallback(audio)


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
