# Curation rules

## Judging one reel

- **Context is the user's profile** (`~/.prism/profile.md`, or `$PRISM_HOME/profile.md`). Ideas must be useful to *this* person, not generic.
- **`user_note` is the strongest signal** of why they saved the reel, when present.
- **`collections` (Instagram collection names) are a topic hint, not a category.** Names are often terse (`d1`, `food🍴`). Use them to understand why it was saved; the category still comes from the content. A collection name is never evidence.
- A reel yields 0 to 4 ideas. Zero is valid when nothing is usable: still curate it with `ideas: []` and a `summary`.
- One idea goes in exactly one category. One reel may have ideas in several categories.
- Be skeptical of marketing claims and tools whose mechanism isn't shown. Label those `guessed` or `unknown`.
- Cite seconds (`at_s`) when you can. Photo posts (`media: "photo"`) have no seconds: `at_s` null, slide number in `text` ("Slide 3: ...").

### `confidence`

How sure you are the idea is **correct and usable by the user**, not how exciting the reel is.

- **high:** the substance is directly visible in frames/transcript (steps, ingredients, settings, code, an address readable on screen), nothing essential is missing, and it fits the user's profile. Backed by `seen` evidence with `at_s`.
- **medium:** the core idea is clear and relevant but with one gap (a product named without details, a screen too brief/blurry, some details only spoken). Record the gap as `guessed` or `unknown` evidence.
- **low:** a claim without substance, a promo, or a guess from the caption. Fine to keep as something to look into (`next_actions`), never dressed up as fact.
- An idea that rests on a `guessed` item can't be `high`.

### Promo / call-to-action reels

Signs: "comment X for the link/recipe/file", "link in bio", "DM me", a short demo without steps, the real content promised outside the video.

- Take only what is **actually visible**. Content promised by DM is `unknown`; don't guess it.
- Confidence for ideas from promo reels is at most `medium`, and `low` if the substance isn't shown at all.
- Note in `evidence` (`unknown`) what was promised but not shown.
- `next_actions` may hold a lookup step (find the original source), never "comment X".
- A promo with nothing usable: `ideas: []` is valid, with a `summary` that says it's a promo.

### No `user_note`

An empty `user_note` is normal (most reels) and doesn't make a reel less important.

- Don't invent why they saved it. Build `why_relevant` from the profile, specific to this reel's content.
- Don't raise confidence because "they must have saved it for this". Judge the content only.
- If you can't explain the relevance specifically, the idea is `low` or not worth writing.

## `curation.json` schema

```json
{
  "reel_id": "ig_C9xyz",
  "summary": "1-2 sentences on what the reel shows.",
  "card_line": "Short one-liner, max 70 characters",
  "duplicate_of": null,
  "duplicate_reason": null,
  "ideas": [
    {
      "title": "Short idea title",
      "detail": "What to take from it, concretely.",
      "why_relevant": "Why this is useful to the user, specifically.",
      "category": "recipes",
      "confidence": "medium"
    }
  ],
  "evidence": [
    {"label": "seen", "text": "On-screen text lists 3 ingredients", "at_s": 4.2},
    {"label": "guessed", "text": "Oven temperature looks like ~180C from the dial", "at_s": 9.0},
    {"label": "unknown", "text": "Exact amounts only sent by DM", "at_s": null}
  ],
  "proposed_category": null,
  "next_actions": ["Find the creator's full recipe post"]
}
```

`prism commit` validates and stores it; on any error nothing changes and every error is printed at once:

- `card_line` max 70 characters, `summary` max 300.
- `category` must be a slug from the packet's `categories` (`core`/`approved`/`proposed`), or equal `proposed_category.slug`.
- `proposed_category.slug` must not match any existing category, rejected ones included.
- `label` is only `seen` / `guessed` / `unknown`. `confidence` is only `high` / `medium` / `low`.
- If `duplicate_of` is set: `ideas` must be empty and `duplicate_reason` is required.

## Proposing a category

Only when an idea clearly fits no `core`/`approved` category, and no existing `proposed` one fits either. Never re-propose something already rejected under another name. The user decides; a proposal is just a suggestion.

```json
"proposed_category": {
  "slug": "home-decor",
  "name": "Home decor",
  "description": "What belongs here, one sentence",
  "reason": "Why none of the existing categories fit"
}
```

## Content duplicates

URL duplicates are handled at fetch. You check **content** duplicates (a different reel, same content, e.g. a re-upload) against `recent_titles` (`[{reel_id, title}]`). Sure: set `duplicate_of` to that `reel_id` plus `duplicate_reason`. Unsure: don't mark it, mention it as `guessed` evidence.
