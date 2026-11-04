"""`prism doctor`: check every local prerequisite, say how to fix what's missing.

Reads local state (PATH, installed packages, browser cookie store, folders), plus
one anonymous request to see if instagram.com answers on this network.
"""

import http.client
import importlib.util
import os
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass

from prism import config, transcribe


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _ffmpeg() -> list[Check]:
    out = []
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        out.append(
            Check(tool, bool(path), path or "not on PATH, install ffmpeg (see README)")
        )
    return out


def _transcriber() -> Check:
    b = transcribe.backend()
    pkg = "mlx_whisper" if b == "mlx" else "faster_whisper"
    if importlib.util.find_spec(pkg) is None:
        return Check("transcription", False, f"{pkg} not installed, reinstall prism")
    return Check("transcription", True, f"{b}, model {transcribe.model_name()}")


def _cookies() -> Check:
    d = config.settings.download
    if not d.browser:
        return Check(
            "instagram login",
            False,
            f'no browser set. Add `browser = "chrome"` (or brave, firefox, edge,'
            f" safari...) under [download] in {config.CONFIG_FILE}",
        )
    import yt_dlp.cookies

    label = f"{d.browser}" + (
        f" profile {d.browser_profile}" if d.browser_profile else ""
    )
    try:
        jar = yt_dlp.cookies.extract_cookies_from_browser(d.browser, d.browser_profile)
    # unknown browser/profile, locked or unreadable cookie store: all mean "fix setup"
    except Exception as e:  # noqa: BLE001
        return Check("instagram login", False, f"can't read {label} cookies: {e}")
    if not any(c.name == "sessionid" and "instagram.com" in c.domain for c in jar):
        return Check(
            "instagram login",
            False,
            f"{label} has no Instagram session. Log in to instagram.com in that"
            " browser (or set browser_profile to the profile that is logged in)",
        )
    return Check("instagram login", True, label)


def _reachable() -> Check:
    name = "network"
    try:
        # no cookies: this only tests the network, never the account
        urllib.request.urlopen("https://www.instagram.com/", timeout=15).close()
    except urllib.error.HTTPError:
        pass  # any HTTP answer means it is reachable
    except (OSError, http.client.HTTPException) as e:
        return Check(
            name,
            False,
            f"no answer from instagram.com ({e}). `prism process` will stop on"
            " timeouts. Wait, or try another network (e.g. a phone hotspot)",
        )
    return Check(name, True, "instagram.com answers")


def _output_dir() -> Check:
    out = config.settings.output_dir
    parent = next((p for p in (out, *out.parents) if p.exists()), None)
    if parent is None or not parent.is_dir() or not os.access(parent, os.W_OK):
        return Check(
            "output_dir", False, f"{out} is not writable, set paths.output_dir"
        )
    return Check("output_dir", True, str(out))


def _db() -> Check:
    p = config.settings.db_path
    return Check("database", p.exists(), str(p) if p.exists() else "run `prism init`")


def run() -> list[Check]:
    return [*_ffmpeg(), _transcriber(), _cookies(), _reachable(), _output_dir(), _db()]
