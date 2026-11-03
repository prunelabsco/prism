"""Adapter file daftar URL: satu URL per baris, baris kosong & `#` diabaikan."""

from pathlib import Path

from prism.fetch import shortcode
from prism.sources.base import SourceItem


class UrlsSource:
    def __init__(self, path: Path):
        self._path = path

    def list_new(self) -> list[SourceItem]:
        items = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            url = line.strip()
            if not url or url.startswith("#"):
                continue
            sc = shortcode(url)
            # shortcode = source_id: URL sama beda query string tidak dihitung duplikat
            items.append(
                SourceItem(source="urls", source_id=sc[1] if sc else url, url=url)
            )
        return items
