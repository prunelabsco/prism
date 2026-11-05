import json
from pathlib import Path

import pytest
import yt_dlp

from prism import download

URL = "https://www.instagram.com/p/X/"
VID = {"formats": [{}], "description": "cap", "uploader": "u"}
PIC = {"formats": None}


class FakeYDL:
    """Satu skenario per test: `calls` mencatat (download, process, playlist_items)."""

    calls: list
    carousel: list | None = None  # None = single reel
    error: str | None = None

    def __init__(self, opts):
        self.opts = opts
        type(self).last_opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False, process=True):
        items = self.opts.get("playlist_items")
        type(self).calls.append((download, process, items))
        if self.error:
            raise yt_dlp.utils.DownloadError(self.error)
        if self.carousel is None:
            info = VID
        elif not process:
            return {"entries": self.carousel}
        elif items is None:
            raise yt_dlp.utils.DownloadError("No video formats found!")
        else:
            info = {"entries": [self.carousel[int(items) - 1]]}
        if download:
            Path(self.opts["outtmpl"].replace("%(ext)s", "mp4")).write_bytes(b"v")
        return info


@pytest.fixture
def ydl(monkeypatch):
    FakeYDL.calls, FakeYDL.carousel, FakeYDL.error = [], None, None
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(
        download, "_ffprobe", lambda v: {"duration_s": 1.0, "width": 360, "height": 640}
    )
    return FakeYDL


def test_reel_one_request_and_meta(ydl, tmp_path):
    download.fetch_video(URL, tmp_path / "r")
    assert ydl.calls == [(True, True, None)]
    assert ydl.last_opts["format_sort"] == ["res:720"]
    assert download.is_complete(tmp_path / "r")
    meta = json.loads((tmp_path / "r" / "meta.json").read_text())
    assert meta == {
        "duration_s": 1.0, "width": 360, "height": 640,
        "caption": "cap", "uploader": "u",
    }  # fmt: skip


def test_mixed_carousel_picks_first_video(ydl, tmp_path):
    ydl.carousel = [PIC, PIC, VID, VID]
    download.fetch_video(URL, tmp_path / "r")
    assert ydl.calls == [(True, True, None), (False, False, None), (True, True, "3")]
    assert download.is_complete(tmp_path / "r")


def test_photo_carousel_slides_become_frames(ydl, tmp_path, monkeypatch):
    small = {"url": "s", "width": 150}
    ydl.carousel = [
        {"thumbnails": [small, {"url": "big1", "width": 1080}], "width": 1080},
        {"thumbnails": [{"url": "big2", "width": 1080}, small]},
    ]
    urls = []

    def fake_ffmpeg(cmd, **kw):
        urls.append(cmd[cmd.index("-i") + 1])
        Path(cmd[-1]).write_bytes(b"jpg")

    monkeypatch.setattr(download.subprocess, "run", fake_ffmpeg)
    out = tmp_path / "r"
    download.fetch_video(URL, out)
    assert urls == ["big1", "big2"]  # largest image per slide
    assert ydl.calls == [(True, True, None), (False, False, None)]  # no extra request
    assert download.is_complete(out)
    frames = json.loads((out / "frames.json").read_text())
    assert frames == [
        {"path": "frames/0001.jpg", "t": None, "slide": 1},
        {"path": "frames/0002.jpg", "t": None, "slide": 2},
    ]
    assert json.loads((out / "meta.json").read_text())["media"] == "photo"
    assert json.loads((out / "transcript.json").read_text())["has_speech"] is False


def test_post_without_video_or_image_fails(ydl, tmp_path):
    ydl.carousel = [PIC, PIC]
    with pytest.raises(download.IgError) as e:
        download.fetch_video(URL, tmp_path / "r")
    assert not e.value.transient
    assert not download.is_complete(tmp_path / "r")


@pytest.mark.parametrize(
    "msg, transient",
    [
        (
            "Requested content is not available, rate-limit reached or login required",
            True,
        ),
        ("HTTP Error 429: Too Many Requests", True),
        ("This content isn't available", False),
    ],
)
def test_error_classified(ydl, tmp_path, msg, transient):
    ydl.error = msg
    with pytest.raises(download.IgError) as e:
        download.fetch_video(URL, tmp_path / "r")
    assert e.value.transient is transient
    assert not download.is_complete(tmp_path / "r")


def test_cookies_file_wins_over_browser(monkeypatch):
    from prism import config

    both = config.Settings(
        download=config.Download(browser="brave", cookies_file="~/c.txt")
    )
    monkeypatch.setattr(download, "settings", both)
    opts = download._opts()
    assert opts["cookiefile"] == str(Path("~/c.txt").expanduser().resolve())
    assert "cookiesfrombrowser" not in opts


def test_browser_used_without_cookies_file(monkeypatch):
    from prism import config

    s = config.Settings(download=config.Download(browser="brave"))
    monkeypatch.setattr(download, "settings", s)
    opts = download._opts()
    assert opts["cookiesfrombrowser"] == ("brave", None)
    assert "cookiefile" not in opts
