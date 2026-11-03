import copy

import pytest
from pydantic import ValidationError

from prism.models import Curation

CATS = {
    "recipes": "core",
    "learn": "core",
    "projects": "core",
    "desain-ui": "proposed",
    "crypto": "rejected",
}

# example from docs/CURATION.md
VALID = {
    "reel_id": "ig_C9xyz",
    "summary": "1-2 sentences on what the reel shows.",
    "card_line": "Short one-liner, max 70 characters",
    "duplicate_of": None,
    "duplicate_reason": None,
    "ideas": [
        {
            "title": "Short idea title",
            "detail": "What to take from it, concretely.",
            "why_relevant": "Why this is useful to the user, specifically.",
            "category": "learn",
            "confidence": "medium",
        }
    ],
    "evidence": [
        {"label": "seen", "text": "Screen shows SKILL.md", "at_s": 12.0},
        {"label": "guessed", "text": "Probably uses a hook", "at_s": None},
        {"label": "unknown", "text": "Repo name not mentioned", "at_s": None},
    ],
    "proposed_category": None,
    "next_actions": ["Find the repo mentioned at 0:12"],
}


def check(data, cats=CATS):
    return Curation.model_validate(data, context={"categories": cats})


def variant(**changes):
    d = copy.deepcopy(VALID)
    d.update(changes)
    return d


def idea(category):
    return {**VALID["ideas"][0], "category": category}


def proposal(slug):
    return {"slug": slug, "name": "N", "description": "d", "reason": "r"}


def test_valid_example():
    c = check(VALID)
    assert c.ideas[0].category == "learn"
    assert check(variant(ideas=[], evidence=[], next_actions=[])).ideas == []


def test_existing_proposed_and_own_proposal_usable():
    check(variant(ideas=[idea("desain-ui")]))
    check(variant(ideas=[idea("riset")], proposed_category=proposal("riset")))


@pytest.mark.parametrize(
    "data, msg",
    [
        (variant(card_line="x" * 71), "card_line"),
        (variant(summary="x" * 301), "summary"),
        (variant(summary="   "), "summary"),
        (variant(evidence=[{"label": "yakin", "text": "t"}]), "label"),
        (variant(evidence=[{"label": "guessed", "text": "t", "at_s": -1}]), "at_s"),
        (variant(ideas=[{**idea("projects"), "confidence": "pasti"}]), "confidence"),
        (variant(ideas=[idea("projects")] * 5), "ideas"),
        (variant(tags=["x"]), "tags"),
        (variant(ideas=[idea("crypto")]), "crypto is not an active"),
        (variant(ideas=[idea("bogus")]), "bogus is not an active"),
        (variant(proposed_category=proposal("Desain UI")), "slug"),
        (variant(proposed_category=proposal("crypto")), "status rejected"),
        (variant(duplicate_of="ig_A", duplicate_reason="same"), "ideas must be empty"),
        (variant(ideas=[], duplicate_of="ig_A"), "duplicate_reason is required"),
    ],
)
def test_rejected(data, msg):
    with pytest.raises(ValidationError, match=msg):
        check(data)


def test_duplicate_valid():
    c = check(variant(ideas=[], duplicate_of="ig_A", duplicate_reason="re-upload"))
    assert c.duplicate_of == "ig_A"


def test_context_required():
    with pytest.raises(ValidationError, match="categories context"):
        Curation.model_validate(VALID)


def test_errors_reported_together():
    data = variant(card_line="x" * 71, evidence=[{"label": "yakin", "text": "t"}])
    with pytest.raises(ValidationError) as e:
        check(data)
    assert e.value.error_count() == 2
    cross = variant(ideas=[idea("bogus")], proposed_category=proposal("crypto"))
    with pytest.raises(ValidationError, match="crypto.*bogus"):
        check(cross)
