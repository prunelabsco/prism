"""Official Instagram export ("Download your information", Saved, JSON format).

Format seen in a real export (2026-11-02), files under `your_instagram_activity/saved/`:
- `saved_posts.json`: [{timestamp, label_values: [{label: "URL", href}, {title: "Owner", ...}]}]
- `saved_collections.json`: [{label_values: [{label: "Name"}, {title: "Media", dict: [item]}]}],
  each item {dict: [{label: "URL"}, {title: "Owner"}, ...]} without a timestamp.
Some reels only appear in collections (154 of 743), so both files merge per shortcode.
"""

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from prism.fetch import shortcode
from prism.sources.base import SourceItem

FILES = ("saved_posts.json", "saved_collections.json")


def _fix(s: str) -> str:
    """Meta's export stores UTF-8 as if it were latin-1 (emoji become `ð\\x9f...`)."""
    try:
        return s.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return s


def _load(path: Path) -> dict[str, list]:
    """Export ZIP or folder -> {file name: content}. Missing files are skipped."""
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
        raise FileNotFoundError(f"{path}: no {' / '.join(FILES)} found")
    return out


def _fields(entries: list[dict]) -> tuple[str | None, str | None]:
    """One post's label_values -> (url, owner username)."""
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
                return  # deleted post / no URL

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
