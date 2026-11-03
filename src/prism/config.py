"""Baca config.toml, expose path & opsi. Satu-satunya sumber path untuk modul lain."""

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict

PIPELINE_DIR = Path(__file__).resolve().parents[2]
CONFIG_FILE = PIPELINE_DIR / "config.toml"


class _Section(BaseModel):
    # typo/kunci asing di config.toml langsung error, bukan diam-diam diabaikan
    model_config = ConfigDict(extra="forbid", frozen=True)


class Paths(_Section):
    data_dir: Path
    nexus_inbox: Path


class Download(_Section):
    max_per_run: int
    throttle_min_s: float
    throttle_max_s: float
    max_height: int
    keep_video: bool
    browser: str
    browser_profile: str


class Frames(_Section):
    scene_threshold: float
    max_frames: int
    dense_max_frames: int
    width: int
    max_gap_s: float


class Transcribe(_Section):
    model: str


class Curate(_Section):
    default_batch: int


class Settings(_Section):
    paths: Paths
    download: Download
    frames: Frames
    transcribe: Transcribe
    curate: Curate

    @property
    def data_dir(self) -> Path:
        # path relatif dihitung dari folder pipeline/, bukan dari cwd
        return (PIPELINE_DIR / self.paths.data_dir.expanduser()).resolve()

    @property
    def nexus_inbox(self) -> Path:
        return (PIPELINE_DIR / self.paths.nexus_inbox.expanduser()).resolve()

    @property
    def nexus_vault(self) -> Path:
        # inbox = <vault>/00-Inbox/Prism, sync mencari catatan yang dipindah ke seluruh vault
        return self.nexus_inbox.parents[1]

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
    with path.open("rb") as f:
        return Settings.model_validate(tomllib.load(f))


settings = load()
