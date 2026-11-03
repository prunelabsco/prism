"""curation.json schema (rules in docs/CURATION.md). Validated by `prism commit`."""

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    model_validator,
)

# categories an idea may use (not rejected)
USABLE = ("core", "approved", "proposed")

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")]


class _Model(BaseModel):
    # typo'd or unknown fields from the agent are rejected, not silently dropped
    model_config = ConfigDict(extra="forbid")


class Idea(_Model):
    title: Text
    detail: Text
    why_relevant: Text
    category: Slug
    confidence: Literal["high", "medium", "low"]


class Evidence(_Model):
    label: Literal["seen", "guessed", "unknown"]
    text: Text
    at_s: float | None = Field(default=None, ge=0)


class ProposedCategory(_Model):
    slug: Slug
    name: Text
    description: Text
    reason: Text


class Curation(_Model):
    """Validation requires context `{"categories": {slug: status}}` from the DB,
    every category including rejected ones:

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
            # without the category list the category check would silently pass
            raise ValueError("validation needs the categories context from the DB")

        errors = []
        if self.duplicate_of is not None:
            if self.ideas:
                errors.append("duplicate_of is set, ideas must be empty")
            if self.duplicate_reason is None:
                errors.append("duplicate_of is set, duplicate_reason is required")

        usable = {s for s, st in cats.items() if st in USABLE}
        if self.proposed_category is not None:
            slug = self.proposed_category.slug
            if slug in cats:
                errors.append(
                    f"proposed_category {slug} already exists (status {cats[slug]}),"
                    " use that slug or a new one"
                )
            usable.add(slug)
        for i, idea in enumerate(self.ideas):
            if idea.category not in usable:
                errors.append(
                    f"ideas[{i}].category {idea.category} is not an active/proposed category"
                )

        if errors:
            raise ValueError("; ".join(errors))
        return self
