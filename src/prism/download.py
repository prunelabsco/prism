"""Pembungkus yt-dlp + throttle. Satu-satunya modul yang bicara ke Instagram."""

import json
import logging
import random
import subprocess
import time
from pathlib import Path

import yt_dlp

from prism.config import settings

log = logging.getLogger("prism.yt_dlp")

# pesan yt-dlp yang berarti "post ini memang tanpa video", bukan gangguan
_NO_VIDEO = ("no video formats found", "there is no video in this post")
# gangguan sementara: tidak dihitung sebagai "post tidak bisa diakses"
_TRANSIENT = (
    "rate-limit",
    "rate limit",
    "login required",
    "429",
    "timed out",
    "connection",
)


class IgError(Exception):
    """yt-dlp gagal untuk satu post.

    transient=True: rate limit/login/jaringan, coba lagi nanti.
    False: kemungkinan post dihapus/privat. no_video: post tanpa video.
    """

    def __init__(self, msg: str, transient: bool, no_video: bool = False):
        super().__init__(msg)
        self.transient = transient
        self.no_video = no_video


def throttle() -> None:
    d = settings.download
    time.sleep(random.uniform(d.throttle_min_s, d.throttle_max_s))


def _opts(**extra) -> dict:
    d = settings.download
    cookies = (
        {"cookiesfrombrowser": (d.browser, d.browser_profile)} if d.browser else {}
    )
    return {
        **cookies,
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "logger": log,
        **extra,
    }


def _extract(
    url: str, opts: dict, download: bool = False, process: bool = True
) -> dict:
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            return y.extract_info(url, download=download, process=process)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        low = msg.lower()
        if any(m in low for m in _NO_VIDEO):
            raise IgError(msg, transient=False, no_video=True) from e
        raise IgError(msg, any(m in low for m in _TRANSIENT)) from e


def _entries(info: dict) -> list[dict]:
    return info.get("entries") or [info]


def probe(url: str) -> bool:
    """True kalau post berisi video (carousel: minimal satu entry punya formats).

    `process=False` supaya entry foto di carousel tidak memicu error format.
    """
    try:
        info = _extract(url, _opts(), process=False)
    except IgError as e:
        if e.no_video:
            return False
        raise
    return any(e and e.get("formats") for e in _entries(info))


def _ffprobe(video: Path) -> dict:
    # durasi dari ffprobe: `duration` di info yt-dlp null untuk Instagram
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration",
             "-of", "json", str(video)],
            capture_output=True, text=True, check=True,
        ).stdout  # fmt: skip
    except (OSError, subprocess.CalledProcessError) as e:
        raise IgError(f"ffprobe gagal: {e}", transient=False) from e
    d = json.loads(out)
    s = d["streams"][0]
    return {
        "duration_s": round(float(d["format"]["duration"]), 2),
        "width": s["width"],
        "height": s["height"],
    }


def is_complete(out_dir: Path) -> bool:
    # post foto tidak punya video.mp4: slide langsung jadi frames.json
    media = (out_dir / "video.mp4").exists() or (out_dir / "frames.json").exists()
    return media and (out_dir / "meta.json").exists()


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _fetch_photos(info: dict, out_dir: Path) -> None:
    """Post foto: gambar terbesar tiap slide -> frames/*.jpg (lebar maks
    `frames.width`), plus frames.json, transkrip kosong, dan meta.json.
    Tahap frame & transkrip di `process` jadi otomatis terlewati."""
    width = settings.frames.width
    fdir = out_dir / "frames"
    fdir.mkdir(exist_ok=True)
    frames = []
    for i, entry in enumerate(_entries(info), 1):
        thumbs = [t for t in (entry or {}).get("thumbnails") or [] if t.get("url")]
        if not thumbs:
            continue
        best = max(thumbs, key=lambda t: t.get("width") or 0)
        rel = f"frames/{i:04d}.jpg"
        # ponytail: ffmpeg baca URL CDN langsung, tanpa klien HTTP tambahan
        try:
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
                 "-headers", "Referer: https://www.instagram.com/\r\n",
                 "-i", best["url"], "-frames:v", "1",
                 "-vf", f"scale='min({width},iw)':-2", "-q:v", "3", "-y",
                 str(out_dir / rel)],
                capture_output=True, text=True, check=True, timeout=60,
            )  # fmt: skip
        except (OSError, subprocess.SubprocessError) as e:
            raise IgError(f"unduh slide {i} gagal: {e}", transient=False) from e
        frames.append({"path": rel, "t": None, "slide": i})
    if not frames:
        raise IgError("post tanpa video dan tanpa gambar", transient=False)

    first = next(e for e in _entries(info) if e)
    _write_json(
        out_dir / "transcript.json",
        {"language": None, "text": "", "segments": [], "has_speech": False},
    )
    _write_json(
        out_dir / "meta.json",
        {
            "media": "foto",
            "duration_s": 0,
            "width": first.get("width"),
            "height": first.get("height"),
            "caption": info.get("description") or first.get("description"),
            "uploader": info.get("uploader") or info.get("channel"),
        },
    )
    # frames.json terakhir: is_complete() menganggap download selesai setelah ini
    _write_json(out_dir / "frames.json", frames)


def fetch_video(url: str, out_dir: Path) -> None:
    """Download ke `out_dir/video.mp4` + tulis `meta.json`. Raise IgError.

    Carousel campur: entry foto bikin download biasa error "no video", jadi
    diulang dengan `playlist_items` = entry video pertama. Post tanpa video
    sama sekali: slide diambil sebagai foto (`_fetch_photos`).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    # res = sisi terpendek: reel vertikal 720x1280 tetap dihitung 720p.
    # Tidak ada <=720 -> yt-dlp ambil resolusi terdekat, bukan error.
    opts = _opts(
        skip_download=False,
        format="bv*+ba/b",
        format_sort=[f"res:{settings.download.max_height}"],
        merge_output_format="mp4",
        outtmpl=str(out_dir / "video.%(ext)s"),
    )
    try:
        info = _extract(url, opts, download=True)
    except IgError as e:
        if not e.no_video:
            raise
        meta = _extract(url, _opts(), process=False)
        # ponytail: carousel banyak video cuma ambil yang pertama, tambah kalau perlu
        idx = next(
            (i for i, x in enumerate(_entries(meta), 1) if x and x.get("formats")),
            None,
        )
        if idx is None:
            _fetch_photos(meta, out_dir)
            return
        info = _extract(url, {**opts, "playlist_items": str(idx)}, download=True)

    video = out_dir / "video.mp4"
    if not video.exists():
        raise IgError("video.mp4 tidak terbentuk", transient=False)
    entry = _entries(info)[0]
    meta = {
        **_ffprobe(video),
        "caption": entry.get("description"),
        "uploader": entry.get("uploader") or entry.get("channel"),
    }
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
