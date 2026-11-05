"""yt-dlp wrapper + throttle. The only module that talks to Instagram."""

import json
import logging
import random
import subprocess
import time
from pathlib import Path

import yt_dlp

from prism.config import settings

log = logging.getLogger("prism.yt_dlp")

# yt-dlp messages meaning "this post simply has no video", not a hiccup
_NO_VIDEO = ("no video formats found", "there is no video in this post")
# temporary trouble: not counted as "post unreachable"
_TRANSIENT = (
    "rate-limit",
    "rate limit",
    "login required",
    "429",
    "timed out",
    "connection",
)


# browser cookie store unreadable: (yt-dlp message, what the user can do)
_COOKIE_FIXES = (
    ("could not copy chrome cookie", "Close the browser completely, then try again."),
    (
        "dpapi",
        (
            "The browser is blocking access to its cookies. Run `prism login`,"
            " or set `browser` to another browser, such as Firefox."
        ),
    ),
)


def cookie_fix(msg: str) -> str | None:
    low = msg.lower()
    return next((fix for marker, fix in _COOKIE_FIXES if marker in low), None)


class IgError(Exception):
    """yt-dlp failed for one post.

    transient=True: rate limit/login/network/cookies, try again later.
    False: probably deleted/private. no_video: post has no video.
    """

    def __init__(self, msg: str, transient: bool, no_video: bool = False):
        super().__init__(msg)
        self.transient = transient
        self.no_video = no_video


def error_kind(e: IgError) -> str:
    """A timeout is the network, not a limit on the account."""
    low = str(e).lower()
    if cookie_fix(low):
        return "cookies"
    if "timed out" in low or "connection" in low:
        return "network"
    return "rate_limited"


def stop_reason(e: IgError) -> str:
    """Why a run stopped."""
    kind = error_kind(e)
    if kind == "cookies":
        return f"can't read the browser's cookies. {cookie_fix(str(e))} ({e})"
    if kind == "network":
        return f"Instagram is not responding on this network, wait or try another: {e}"
    return f"rate limit or login, wait before trying again: {e}"


def throttle() -> None:
    d = settings.download
    time.sleep(random.uniform(d.throttle_min_s, d.throttle_max_s))


def _opts(**extra) -> dict:
    d = settings.download
    if settings.cookies_file:
        cookies = {"cookiefile": str(settings.cookies_file)}
    elif d.browser:
        cookies = {"cookiesfrombrowser": (d.browser, d.browser_profile)}
    else:
        cookies = {}
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
        # cookies: our setup, not the post. Must not count against the reel
        transient = any(m in low for m in _TRANSIENT) or cookie_fix(msg) is not None
        raise IgError(msg, transient) from e


def _entries(info: dict) -> list[dict]:
    return info.get("entries") or [info]


def probe(url: str) -> bool:
    """True if the post has a video (carousel: at least one entry has formats).

    `process=False` so photo entries in a carousel don't raise format errors.
    """
    try:
        info = _extract(url, _opts(), process=False)
    except IgError as e:
        if e.no_video:
            return False
        raise
    return any(e and e.get("formats") for e in _entries(info))


def _ffprobe(video: Path) -> dict:
    # duration from ffprobe: yt-dlp's `duration` is null for Instagram
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration",
             "-of", "json", str(video)],
            capture_output=True, text=True, check=True,
        ).stdout  # fmt: skip
    except (OSError, subprocess.CalledProcessError) as e:
        raise IgError(f"ffprobe failed: {e}", transient=False) from e
    d = json.loads(out)
    s = d["streams"][0]
    return {
        "duration_s": round(float(d["format"]["duration"]), 2),
        "width": s["width"],
        "height": s["height"],
    }


def is_complete(out_dir: Path) -> bool:
    # photo posts have no video.mp4: slides go straight to frames.json
    media = (out_dir / "video.mp4").exists() or (out_dir / "frames.json").exists()
    return media and (out_dir / "meta.json").exists()


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _fetch_photos(info: dict, out_dir: Path) -> None:
    """Photo post: largest image per slide -> frames/*.jpg (max width
    `frames.width`), plus frames.json, an empty transcript, and meta.json.
    The frame & transcript stages in `process` are then skipped."""
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
        # ponytail: ffmpeg reads the CDN URL directly, no extra HTTP client
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
            raise IgError(f"slide {i} download failed: {e}", transient=False) from e
        frames.append({"path": rel, "t": None, "slide": i})
    if not frames:
        raise IgError("post has neither video nor images", transient=False)

    first = next(e for e in _entries(info) if e)
    _write_json(
        out_dir / "transcript.json",
        {"language": None, "text": "", "segments": [], "has_speech": False},
    )
    _write_json(
        out_dir / "meta.json",
        {
            "media": "photo",
            "duration_s": 0,
            "width": first.get("width"),
            "height": first.get("height"),
            "caption": info.get("description") or first.get("description"),
            "uploader": info.get("uploader") or info.get("channel"),
        },
    )
    # frames.json last: is_complete() treats the download as done after this
    _write_json(out_dir / "frames.json", frames)


def fetch_video(url: str, out_dir: Path) -> None:
    """Download to `out_dir/video.mp4` + write `meta.json`. Raises IgError.

    Mixed carousel: photo entries make a plain download fail with "no video",
    so it is retried with `playlist_items` = the first video entry. Posts with
    no video at all: slides are taken as photos (`_fetch_photos`).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    # res = shortest side: a vertical 720x1280 reel still counts as 720p.
    # Nothing <=720 -> yt-dlp takes the closest resolution, not an error.
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
        # ponytail: multi-video carousels only take the first video, extend if needed
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
        raise IgError("video.mp4 was not created", transient=False)

    entry = _entries(info)[0]
    meta = {
        **_ffprobe(video),
        "caption": entry.get("description"),
        "uploader": entry.get("uploader") or entry.get("channel"),
    }
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
