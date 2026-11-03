"""Kontrak sumber reel. Mesin hanya kenal SourceItem, tidak peduli sumbernya."""

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
    card_type: str | None = None


class SourceAdapter(Protocol):
    def list_new(self) -> list[SourceItem]:
        """Semua item dari sumber. Filter video & dedup dilakukan fetch.py."""
        ...
