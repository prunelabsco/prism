import json
import subprocess

import pytest

from prism import frames


def test_pick_times_no_scenes_fills_gaps():
    assert frames.pick_times([], 20.0, 12, 5) == [0.0, 5.0, 10.0, 15.0]


def test_pick_times_offset_and_merge():
    # +0.3: 0.1->0.4 merges into 0, 4.8->5.1 into the midpoint, 6.7 & 7.1 into one
    assert frames.pick_times([0.1, 2.7, 4.8, 6.7, 7.1], 10.0, 12, 100) == [
        0.0, 3.0, 5.0, 7.0,
    ]  # fmt: skip


def test_pick_times_offset_clamped_to_duration():
    assert frames.pick_times([9.95], 10.0, 12, 100) == [0.0, 5.0, 9.9]


def test_pick_times_capped_evenly():
    scenes = [i + 0.2 for i in range(30)]  # +0.3 -> 0.5 .. 29.5
    t = frames.pick_times(scenes, 30.0, 6, 5)
    assert len(t) == 6 and 0.0 in t and 15.0 in t
    assert t[1] == 0.5 and t[-1] == 29.5  # both ends kept


def test_pick_times_fill_respects_cap():
    assert frames.pick_times([], 100.0, 4, 5) == [0.0, 25.0, 50.0, 75.0]


def _color_video(out, colors, d):
    inputs = []
    for c in colors:
        inputs += ["-f", "lavfi", "-i", f"color={c}:s=360x640:d={d}:r=25"]
    n = len(colors)
    subprocess.run(
        ["ffmpeg", "-v", "error", *inputs, "-filter_complex",
         "".join(f"[{i}]" for i in range(n)) + f"concat=n={n}:v=1",
         "-y", str(out / "video.mp4")],
        check=True,
    )  # fmt: skip
    (out / "meta.json").write_text(
        json.dumps({"duration_s": n * d, "width": 360, "height": 640})
    )


@pytest.fixture
def video(tmp_path):
    """3 adegan hitam-putih-hitam @2 detik, 360x640, seperti reel 360p."""
    _color_video(tmp_path, ("black", "white", "black"), 2)
    return tmp_path


def test_extract_real_ffmpeg(video):
    scenes = frames.scene_times(video / "video.mp4", 0.3)
    assert [round(t) for t in scenes] == [2, 4]

    out = frames.extract(video)
    assert [f["t"] for f in out] == [0.0, 2.3, 3.0, 4.3]
    assert json.loads((video / "frames.json").read_text()) == out
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width",
         "-of", "csv=p=0", str(video / "frames" / "0001.jpg")],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    assert probe == "360"  # not upscaled to 768
    assert frames.is_complete(video)


def test_extract_no_scene_change_fills_gaps(tmp_path):
    _color_video(tmp_path, ("gray",), 20)
    assert [f["t"] for f in frames.extract(tmp_path)] == [0.0, 5.0, 10.0, 15.0]
