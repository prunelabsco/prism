"""Video -> frame berdasarkan pergantian adegan + frames.json. Aturan di PIPELINE.md."""

import json
import re
import subprocess
from pathlib import Path

from prism.config import settings

_PTS = re.compile(r"pts_time:([\d.]+)")
MIN_GAP_S = 0.5  # kandidat lebih rapat dari ini dianggap frame yang sama
# scene change diambil sedikit setelah titik transisi, supaya tidak buram setengah
SCENE_OFFSET_S = 0.3


class FrameError(Exception):
    pass


def _ffmpeg(*args: str) -> str:
    try:
        return subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", *args],
            capture_output=True, text=True, check=True,
        ).stderr  # fmt: skip
    except (OSError, subprocess.CalledProcessError) as e:
        tail = getattr(e, "stderr", "") or ""
        raise FrameError(f"ffmpeg gagal: {e} {tail[-300:]}") from e


def scene_times(video: Path, threshold: float) -> list[float]:
    """Detik tiap pergantian adegan. Tidak menulis file."""
    err = _ffmpeg(
        "-i", str(video), "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-f", "null", "-",
    )  # fmt: skip
    return [float(t) for t in _PTS.findall(err)]


def pick_times(
    scenes: list[float], duration: float, max_frames: int, max_gap: float
) -> list[float]:
    """Detik 0 + tengah selalu ada, lalu scene change, lalu isi celah > max_gap.

    Scene change dipangkas merata kalau melebihi kuota. Sisa kuota dipakai
    mengisi celah: reel slide dengan transisi halus jarang memicu scene change.
    """
    fixed = [0.0, round(duration / 2, 2)]
    rest: list[float] = []
    for t in sorted(scenes):
        t = round(min(t + SCENE_OFFSET_S, max(duration - 0.1, 0)), 2)
        if all(abs(t - x) >= MIN_GAP_S for x in fixed + rest):
            rest.append(t)
    room = max_frames - len(fixed)
    if len(rest) > room:
        # ambil merata dari daftar urut, ujung awal & akhir ikut
        step = (len(rest) - 1) / max(room - 1, 1)
        rest = [rest[round(i * step)] for i in range(room)] if room > 0 else []

    times = sorted(fixed + rest)
    while len(times) < max_frames:
        # celah terbesar (termasuk ujung sampai durasi) dibelah dua
        bounds = times + [duration]
        gap, i = max((bounds[j + 1] - bounds[j], j) for j in range(len(times)))
        if gap <= max_gap:
            break
        times.insert(i + 1, round(times[i] + gap / 2, 2))
    return times


def is_complete(out_dir: Path) -> bool:
    return (out_dir / "frames.json").exists()


def extract(out_dir: Path) -> list[dict]:
    """Baca video.mp4 + meta.json di out_dir, tulis frames/*.jpg + frames.json."""
    cfg = settings.frames
    video = out_dir / "video.mp4"
    meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
    times = pick_times(
        scene_times(video, cfg.scene_threshold),
        meta["duration_s"],
        cfg.max_frames,
        cfg.max_gap_s,
    )
    width = min(meta["width"], cfg.width)  # reel 360p tidak di-upscale

    fdir = out_dir / "frames"
    fdir.mkdir(exist_ok=True)
    for old in fdir.glob("*.jpg"):
        old.unlink()
    frames = []
    for i, t in enumerate(times, 1):
        rel = f"frames/{i:04d}.jpg"
        _ffmpeg(
            "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
            "-vf", f"scale={width}:-2", "-q:v", "3", "-y", str(out_dir / rel),
        )  # fmt: skip
        if not (out_dir / rel).exists():
            raise FrameError(f"frame {rel} di {t}s tidak terbentuk")
        frames.append({"path": rel, "t": round(t, 2)})
    (out_dir / "frames.json").write_text(json.dumps(frames, indent=2), encoding="utf-8")
    return frames
