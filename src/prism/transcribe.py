"""Video -> transcript.json lewat mlx-whisper. Aturan di PIPELINE.md."""

import json
import subprocess
from pathlib import Path

from prism.config import settings

# segmen dengan peluang "bukan ucapan" setinggi ini dibuang (default whisper)
NO_SPEECH_PROB = 0.6
# whisper memadding audio ke jendela 30 detik, jadi setelah ucapan habis ia
# mengarang segmen yang melewati durasi video (Terlihat di 4 dari 22 reel)
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
        raise TranscribeError(f"ffprobe gagal: {e}") from e
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
        raise TranscribeError(f"ffprobe durasi gagal: {e}") from e


def _whisper(video: Path) -> dict:
    import mlx_whisper  # berat, cuma dimuat kalau ada yang ditranskrip

    return mlx_whisper.transcribe(
        str(video), path_or_hf_repo=settings.transcribe.model, language=None
    )


def is_complete(out_dir: Path) -> bool:
    return (out_dir / "transcript.json").exists()


def transcribe(out_dir: Path) -> dict:
    """Tulis transcript.json. Transkrip kosong (has_speech=false) itu valid."""
    video = out_dir / "video.mp4"
    result: dict = {"language": None, "segments": []}
    limit = 0.0
    if has_audio(video):
        limit = duration(video)
        try:
            result = _whisper(video)
        except Exception as e:  # mlx/ffmpeg bisa melempar apa saja
            raise TranscribeError(f"whisper gagal: {e}") from e

    # ponytail: musik berlirik tetap terhitung speech, cukup untuk kurasi
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
