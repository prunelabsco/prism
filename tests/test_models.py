import copy

import pytest
from pydantic import ValidationError

from prism.models import Curation

CATS = {
    "konten-yt-ai": "core",
    "config-claude-code": "core",
    "ide-project": "core",
    "desain-ui": "proposed",
    "crypto": "rejected",
}

# contoh dari .docs/CURATION.md
VALID = {
    "reel_id": "ig_C9xyz",
    "summary": "1-2 kalimat isi reel, bahasa Indonesia.",
    "card_line": "Kalimat pendek untuk kartu UI, maks 70 karakter",
    "duplicate_of": None,
    "duplicate_reason": None,
    "ideas": [
        {
            "title": "Judul ide singkat",
            "detail": "Apa yang bisa diambil, konkret.",
            "why_relevant": "Kenapa berguna untuk Rakha secara spesifik.",
            "category": "config-claude-code",
            "confidence": "sedang",
        }
    ],
    "evidence": [
        {"label": "terlihat", "text": "Layar menampilkan SKILL.md", "at_s": 12.0},
        {"label": "dugaan", "text": "Kemungkinan memakai hook", "at_s": None},
        {"label": "tidak-diketahui", "text": "Nama repo tidak disebut", "at_s": None},
    ],
    "proposed_category": None,
    "next_actions": ["Cari repo skill yang disebut di detik 0:12"],
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
    assert c.ideas[0].category == "config-claude-code"
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
        (variant(evidence=[{"label": "dugaan", "text": "t", "at_s": -1}]), "at_s"),
        (variant(ideas=[{**idea("ide-project"), "confidence": "pasti"}]), "confidence"),
        (variant(ideas=[idea("ide-project")] * 5), "ideas"),
        (variant(tags=["x"]), "tags"),
        (variant(ideas=[idea("crypto")]), "crypto bukan kategori"),
        (variant(ideas=[idea("ngawur")]), "ngawur bukan kategori"),
        (variant(proposed_category=proposal("Desain UI")), "slug"),
        (variant(proposed_category=proposal("crypto")), "status rejected"),
        (variant(duplicate_of="ig_A", duplicate_reason="sama"), "ideas harus kosong"),
        (variant(ideas=[], duplicate_of="ig_A"), "duplicate_reason wajib"),
    ],
)
def test_rejected(data, msg):
    with pytest.raises(ValidationError, match=msg):
        check(data)


def test_duplicate_valid():
    c = check(variant(ideas=[], duplicate_of="ig_A", duplicate_reason="re-upload"))
    assert c.duplicate_of == "ig_A"


def test_context_required():
    with pytest.raises(ValidationError, match="context categories"):
        Curation.model_validate(VALID)


def test_errors_reported_together():
    data = variant(card_line="x" * 71, evidence=[{"label": "yakin", "text": "t"}])
    with pytest.raises(ValidationError) as e:
        check(data)
    assert e.value.error_count() == 2
    cross = variant(ideas=[idea("ngawur")], proposed_category=proposal("crypto"))
    with pytest.raises(ValidationError, match="crypto.*ngawur"):
        check(cross)
