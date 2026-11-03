"""Adapter export resmi Instagram ("Download your information", Saved, JSON).

Format Terlihat di export asli 2026-11-02, file di `your_instagram_activity/saved/`:
- `saved_posts.json`: [{timestamp, label_values: [{label: "URL", href}, {title: "Owner", ...}]}]
- `saved_collections.json`: [{label_values: [{label: "Name"}, {title: "Media", dict: [item]}]}],
  item berbentuk {dict: [{label: "URL"}, {title: "Owner"}, ...]} tanpa timestamp.
Sebagian reel hanya ada di collections (154 dari 743), jadi dua file digabung per shortcode.
"""

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from prism.fetch import shortcode
from prism.sources.base import SourceItem

FILES = ("saved_posts.json", "saved_collections.json")


def _fix(s: str) -> str:
    """Export Meta menyimpan UTF-8 seolah latin-1 (emoji jadi `ð\\x9f...`)."""
    try:
        return s.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return s


def _load(path: Path) -> dict[str, list]:
    """ZIP atau folder export -> {nama file: isi}. File yang tidak ada dilewati."""
    out = {}
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for member in z.namelist():
                if member.endswith(tuple(f"saved/{f}" for f in FILES)):
                    out[member.rsplit("/", 1)[1]] = json.loads(z.read(member))
    else:
        for f in FILES:
            hit = next(path.rglob(f), None)
            if hit:
                out[f] = json.loads(hit.read_text(encoding="utf-8"))
    if not out:
        raise FileNotFoundError(f"{path}: tidak ada {' / '.join(FILES)}")
    return out


def _fields(entries: list[dict]) -> tuple[str | None, str | None]:
    """label_values satu post -> (url, username pemilik)."""
    url = user = None
    for e in entries:
        if e.get("label") == "URL":
            url = e.get("href") or e.get("value")
        elif e.get("title") == "Owner":
            for owner in e.get("dict", []):
                for f in owner.get("dict", []):
                    if f.get("label") == "Username" and f.get("value"):
                        user = _fix(f["value"])
    return url, user


class IgExportSource:
    def __init__(self, path: Path):
        self._path = path

    def list_new(self) -> list[SourceItem]:
        data = _load(self._path)
        items: dict[str, SourceItem] = {}

        def add(entries: list[dict], ts: int | None, collection: str | None) -> None:
            url, user = _fields(entries)
            sc = shortcode(url or "")
            if sc is None:
                return  # post dihapus/tanpa URL
            it = items.setdefault(
                sc[1],
                SourceItem(
                    source="ig_export",
                    source_id=sc[1],
                    url=url,
                    title=f"@{user}" if user else None,
                ),
            )
            if ts and not it.saved_at:
                it.saved_at = datetime.fromtimestamp(ts, UTC).isoformat()
            if collection and collection not in it.tags:
                it.tags.append(collection)

        for post in data.get("saved_posts.json", []):
            add(post.get("label_values", []), post.get("timestamp"), None)
        for col in data.get("saved_collections.json", []):
            lvs = col.get("label_values", [])
            name = next(
                (_fix(e["value"]) for e in lvs if e.get("label") == "Name"), None
            )
            for block in lvs:
                if block.get("title") == "Media":
                    for media in block.get("dict", []):
                        add(media.get("dict", []), None, name)
        return list(items.values())
