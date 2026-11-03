"""Settings: built-in defaults, overridden by `$PRISM_HOME/config.toml` (default ~/.prism).

The only source of paths for other modules. The config file is optional and may
override any subset of keys; unknown keys are an error, not silently ignored.
"""

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict

HOME = Path(os.environ.get("PRISM_HOME", "~/.prism")).expanduser()
CONFIG_FILE = HOME / "config.toml"

TEMPLATE = """\
# Prism config. Every key is optional; uncomment to override the default.

[paths]
# data_dir = "~/.prism"            # database + per-reel frames/transcripts
# output_dir = "~/Documents/Prism" # where Markdown notes are written
# vault_dir = "~/Documents/Vault"  # optional: notes moved anywhere under here are
#                                  # still found by `prism sync` (e.g. an Obsidian vault)

[download]
# Browser logged in to Instagram. yt-dlp reads its cookies locally.
# chrome, brave, firefox, edge, safari, ... Run `prism doctor` to check.
# browser = "chrome"
# browser_profile = "Default"
# max_per_run = 30
"""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Paths(_Section):
    data_dir: Path = HOME
    output_dir: Path = Path("~/Documents/Prism")
    vault_dir: Path | None = None


class Download(_Section):
    max_per_run: int = 30
    throttle_min_s: float = 3
    throttle_max_s: float = 8
    max_height: int = 720  # shortest video side (format_sort res:720)
    keep_video: bool = False
    browser: str | None = None  # yt-dlp browser name
    # explicit profile: otherwise yt-dlp picks the profile whose cookies changed
    # last, not necessarily the one logged in to Instagram
    browser_profile: str | None = None


class Frames(_Section):
    scene_threshold: float = 0.30
    max_frames: int = 12
    dense_max_frames: int = 20
    width: int = 768
    max_gap_s: float = 5  # gaps longer than this get filled (slow slide transitions)


class Transcribe(_Section):
    model: str = "mlx-community/whisper-large-v3-turbo"


class Curate(_Section):
    default_batch: int = 5


class Settings(_Section):
    paths: Paths = Paths()
    download: Download = Download()
    frames: Frames = Frames()
    transcribe: Transcribe = Transcribe()
    curate: Curate = Curate()

    @property
    def data_dir(self) -> Path:
        return self.paths.data_dir.expanduser().resolve()

    @property
    def output_dir(self) -> Path:
        return self.paths.output_dir.expanduser().resolve()

    @property
    def vault_dir(self) -> Path:
        v = self.paths.vault_dir
        return v.expanduser().resolve() if v else self.output_dir

    @property
    def db_path(self) -> Path:
        return self.data_dir / "prism.db"

    @property
    def reels_dir(self) -> Path:
        return self.data_dir / "reels"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"


def load(path: Path = CONFIG_FILE) -> Settings:
    if not path.exists():
        return Settings()
    with path.open("rb") as f:
        return Settings.model_validate(tomllib.load(f))


settings = load()
