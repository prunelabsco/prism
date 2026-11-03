"""Reel source contract. The pipeline only knows SourceItem, not where it came from."""

from typing import Protocol

from pydantic import BaseModel


class SourceItem(BaseModel):
    source: str
    source_id: str
    url: str
    title: str | None = None
    user_note: str | None = None
    tags: list[str] = []
    saved_at: str | None = None


class SourceAdapter(Protocol):
    def list_new(self) -> list[SourceItem]:
        """Every item in the source. Video filtering & dedup happen in fetch.py."""

        ...
