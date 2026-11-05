"""`prism login`: open a browser window, the user logs in, cookies go to a file.

For browsers whose cookie store can't be read from outside (Chromium on Windows).
Each browser gets a profile of its own, so it never touches the everyday one.
Cookie values are written to the file and never printed.
"""

import json
import os
import platform
import re
import shutil
import subprocess
import time
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from prism import config

URL = "https://www.instagram.com/accounts/login/"
TIMEOUT_S = 300
POLL_S = 2

Cookies = list[dict]

# Windows: relative to Program Files, Program Files (x86) and Local AppData
_PATHS = {
    "chrome": {
        "darwin": ["Google Chrome.app/Contents/MacOS/Google Chrome"],
        "windows": [r"Google\Chrome\Application\chrome.exe"],
        "linux": ["google-chrome", "google-chrome-stable"],
    },
    "brave": {
        "darwin": ["Brave Browser.app/Contents/MacOS/Brave Browser"],
        "windows": [r"BraveSoftware\Brave-Browser\Application\brave.exe"],
        "linux": ["brave-browser", "brave"],
    },
    "edge": {
        "darwin": ["Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
        "windows": [r"Microsoft\Edge\Application\msedge.exe"],
        "linux": ["microsoft-edge", "microsoft-edge-stable"],
    },
    "chromium": {
        "darwin": ["Chromium.app/Contents/MacOS/Chromium"],
        "windows": [r"Chromium\Application\chrome.exe"],
        "linux": ["chromium", "chromium-browser"],
    },
    "vivaldi": {
        "darwin": ["Vivaldi.app/Contents/MacOS/Vivaldi"],
        "windows": [r"Vivaldi\Application\vivaldi.exe"],
        "linux": ["vivaldi", "vivaldi-stable"],
    },
    "opera": {
        "darwin": ["Opera.app/Contents/MacOS/Opera"],
        "windows": [r"Programs\Opera\opera.exe", r"Opera\opera.exe"],
        "linux": ["opera"],
    },
    "firefox": {
        "darwin": ["Firefox.app/Contents/MacOS/firefox"],
        "windows": [r"Mozilla Firefox\firefox.exe"],
        "linux": ["firefox"],
    },
    "safari": {"darwin": ["Safari.app/Contents/MacOS/Safari"]},
}


class LoginError(Exception):
    pass


class _WindowClosed(Exception):
    pass


def find_browser(name: str, system: str | None = None) -> Path | None:
    system = system or platform.system().lower()
    for rel in _PATHS.get(name, {}).get(system, []):
        if system == "darwin":
            roots = [Path("/Applications"), Path.home() / "Applications"]
        elif system == "windows":
            keys = ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")
            roots = [Path(os.environ[k]) for k in keys if os.environ.get(k)]
        else:
            roots = []
        for root in roots:
            if (root / rel).is_file():
                return root / rel
        if found := shutil.which(rel):
            return Path(found)
    return None


def pick_browser(name: str | None, path: Path | None) -> tuple[str, Path]:
    """Asked for > config > first one installed. A custom path is any Chromium
    browser, unless it's named firefox."""
    if path:
        if not path.is_file():
            raise LoginError(f"{path} not found")
        return (
            name or ("firefox" if "firefox" in path.name.lower() else "chromium"),
            path,
        )
    name = name or config.settings.download.browser
    if name:
        if name not in _PATHS:
            raise LoginError(
                f"don't know {name}. Use one of {', '.join(_PATHS)}, or point to"
                " any Chromium browser with --browser-path"
            )
        if found := find_browser(name):
            return name, found
        raise LoginError(f"{name} not found. Point to it with --browser-path")
    for candidate in _PATHS:
        if found := find_browser(candidate):
            return candidate, found
    raise LoginError("no browser found. Point to one with --browser-path")


def instagram_only(cookies: Cookies) -> Cookies:
    return [c for c in cookies if c["domain"].endswith("instagram.com")]


def logged_in(cookies: Cookies) -> bool:
    return any(c["name"] == "sessionid" and c["value"] for c in instagram_only(cookies))


def write_cookies(path: Path, cookies: Cookies) -> None:
    """Netscape cookies.txt with Instagram's cookies only, readable by the owner only."""
    lines = ["# Netscape HTTP Cookie File\n"]
    for c in instagram_only(cookies):
        domain = c["domain"]
        lines.append(
            "\t".join([
                domain,
                "TRUE" if domain.startswith(".") else "FALSE",
                c["path"],
                "TRUE" if c["secure"] else "FALSE",
                str(max(int(c["expires"] or 0), 0)),  # session cookies: -1 or None
                c["name"],
                c["value"],
            ]) + "\n"
        )  # fmt: skip
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.writelines(lines)


def with_cookies_file(text: str, path: Path) -> str:
    """config.toml text with `cookies_file` set, everything else left alone."""
    line = f"cookies_file = {json.dumps(path.as_posix())}"
    for pattern in (r"^cookies_file\s*=.*$", r"^#\s*cookies_file\s*=.*$"):
        if re.search(pattern, text, re.MULTILINE):
            out = re.sub(pattern, lambda _: line, text, count=1, flags=re.MULTILINE)
            break
    else:
        if re.search(r"^\[download\][^\n]*$", text, re.MULTILINE):
            out = re.sub(
                r"^\[download\][^\n]*$",
                lambda m: f"{m.group(0)}\n{line}",
                text,
                count=1,
                flags=re.MULTILINE,
            )
        else:
            out = f"{text.rstrip()}\n\n[download]\n{line}\n".lstrip()
    tomllib.loads(out)  # never leave a broken config behind
    return out


def wait_for_session(
    read: Callable[[], Cookies], timeout: float = TIMEOUT_S, poll: float = POLL_S
) -> Cookies:
    deadline = time.monotonic() + timeout
    while True:
        try:
            cookies = read()
        except _WindowClosed:
            raise LoginError("the window was closed before you logged in") from None
        if logged_in(cookies):
            return cookies
        if time.monotonic() >= deadline:
            raise LoginError(f"no login after {int(timeout)} seconds. Run it again")
        time.sleep(poll)


@contextmanager
def _chromium(binary: Path, profile: Path) -> Iterator[Callable[[], Cookies]]:
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect

    port_file = profile / "DevToolsActivePort"
    port_file.unlink(missing_ok=True)
    proc = subprocess.Popen(
        [
            str(binary),
            f"--user-data-dir={profile}",
            "--remote-debugging-port=0",  # the browser picks a free local port
            "--no-first-run",
            "--no-default-browser-check",
            URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        parts: list[str] = []
        while len(parts) < 2:
            if time.monotonic() >= deadline:
                raise LoginError(
                    f"{binary.name} did not start. Close any window left from an"
                    " earlier `prism login`, then run it again"
                )
            time.sleep(0.2)
            parts = port_file.read_text().split() if port_file.exists() else []
        with connect(
            f"ws://127.0.0.1:{parts[0]}{parts[1]}", max_size=None, proxy=None
        ) as ws:

            def call(method: str) -> dict:
                ws.send(json.dumps({"id": 1, "method": method}))
                while True:
                    reply = json.loads(ws.recv(timeout=15))  # OSError if it hangs
                    if reply.get("id") == 1:
                        return reply

            def read() -> Cookies:
                try:
                    return call("Storage.getCookies")["result"]["cookies"]
                except (ConnectionClosed, OSError):
                    raise _WindowClosed from None

            yield read
            try:
                call("Browser.close")
            except (ConnectionClosed, OSError):
                pass
    finally:
        _stop(proc)


def _stop(proc: subprocess.Popen) -> None:
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()


def _jar_cookies(browser: str, profile: Path | None) -> Cookies:
    """Firefox and Safari keep cookies readable, so yt-dlp reads them directly."""
    import yt_dlp.cookies

    jar = yt_dlp.cookies.extract_cookies_from_browser(
        browser, str(profile) if profile else None
    )
    return [
        {
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path,
            "secure": c.secure,
            "expires": c.expires,
        }
        for c in jar
    ]


@contextmanager
def _firefox(binary: Path, profile: Path) -> Iterator[Callable[[], Cookies]]:
    proc = subprocess.Popen(
        [str(binary), "-no-remote", "-profile", str(profile), URL],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def read() -> Cookies:
        if proc.poll() is not None:
            raise _WindowClosed
        try:
            return _jar_cookies("firefox", profile)
        except Exception:  # noqa: BLE001  # no cookie database until first use
            return []

    try:
        yield read
    finally:
        proc.terminate()


@contextmanager
def _safari() -> Iterator[Callable[[], Cookies]]:
    # ponytail: Safari has no separate profile or debug port, so this reads its real
    # cookie store and leaves the window open. Needs Full Disk Access for the terminal
    subprocess.run(["open", "-a", "Safari", URL], check=True)
    errors: list[str] = []

    def read() -> Cookies:
        try:
            return _jar_cookies("safari", None)
        except Exception as e:  # noqa: BLE001
            errors[:] = [str(e)]
            return []

    try:
        yield read
    except LoginError as e:
        hint = f" Last error reading Safari's cookies: {errors[0]}" if errors else ""
        raise LoginError(f"{e}.{hint}") from None


def _session(name: str, binary: Path):
    if name == "safari":
        return _safari()
    profile = config.HOME / "browser-profiles" / name
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    return (
        _firefox(binary, profile) if name == "firefox" else _chromium(binary, profile)
    )


def run(name: str | None = None, path: Path | None = None) -> tuple[str, Path]:
    """-> (browser used, cookies file). Raises LoginError."""
    browser, binary = pick_browser(name, path)
    with _session(browser, binary) as read:
        cookies = wait_for_session(read)
    out = config.HOME / "instagram-cookies.txt"
    write_cookies(out, cookies)
    cfg = config.CONFIG_FILE
    text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(with_cookies_file(text, out), encoding="utf-8")
    return browser, out
