"""Skema curation.json (aturan di .docs/CURATION.md). Validasi dipakai `prism commit`."""

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    model_validator,
)

# kategori yang boleh dipakai ide (rejected tidak)
USABLE = ("core", "approved", "proposed")

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")]


class _Model(BaseModel):
    # field typo/asing dari Claude ditolak, bukan diam-diam diabaikan
    model_config = ConfigDict(extra="forbid")


class Idea(_Model):
    title: Text
    detail: Text
    why_relevant: Text
    category: Slug
    confidence: Literal["tinggi", "sedang", "rendah"]


class Evidence(_Model):
    label: Literal["terlihat", "dugaan", "tidak-diketahui"]
    text: Text
    at_s: float | None = Field(default=None, ge=0)


class ProposedCategory(_Model):
    slug: Slug
    name: Text
    description: Text
    reason: Text


class Curation(_Model):
    """Validasi wajib pakai context `{"categories": {slug: status}}` dari DB,
    semua kategori termasuk rejected:

        Curation.model_validate_json(text, context={"categories": cats})
    """

    reel_id: Text
    summary: Annotated[Text, StringConstraints(max_length=300)]
    card_line: Annotated[Text, StringConstraints(max_length=70)]
    duplicate_of: Text | None = None
    duplicate_reason: Text | None = None
    ideas: list[Idea] = Field(max_length=4)
    evidence: list[Evidence] = []
    proposed_category: ProposedCategory | None = None
    next_actions: list[Text] = []

    @model_validator(mode="after")
    def _rules(self, info: ValidationInfo) -> "Curation":
        cats = (info.context or {}).get("categories")
        if cats is None:
            # tanpa daftar kategori, cek kategori bisa kelewat diam-diam
            raise ValueError("validasi butuh context categories dari DB")

        errors = []
        if self.duplicate_of is not None:
            if self.ideas:
                errors.append("duplicate_of terisi, ideas harus kosong")
            if self.duplicate_reason is None:
                errors.append("duplicate_of terisi, duplicate_reason wajib")

        usable = {s for s, st in cats.items() if st in USABLE}
        if self.proposed_category is not None:
            slug = self.proposed_category.slug
            if slug in cats:
                errors.append(
                    f"proposed_category {slug} sudah ada (status {cats[slug]}),"
                    " pakai slug itu atau slug baru"
                )
            usable.add(slug)
        for i, idea in enumerate(self.ideas):
            if idea.category not in usable:
                errors.append(
                    f"ideas[{i}].category {idea.category} bukan kategori aktif/usulan"
                )

        if errors:
            raise ValueError("; ".join(errors))
        return self
