---
name: prism
description: Turn the user's saved Instagram reels into categorized, evidence-labeled idea notes, entirely on their machine. Use when the user wants to sort, curate, or "do something with" their saved Instagram posts/reels or collections, mentions an Instagram data export, or says "prism", "curate my reels", "process my saved reels". Drives the `prism` CLI (fetch, process, curate, export). Curation needs a model that can read images.
license: MIT
---

# Prism

Prism has two halves:

- **The `prism` CLI** does the mechanical work: reads the Instagram export, downloads each reel with the user's own browser login, cuts frames, transcribes, stores state, writes Markdown notes.
- **You** do the judgment: look at each reel's frames + transcript and write a curation result the CLI validates and stores.

Never edit Prism's database or packet files by hand. Everything goes through `prism`.

## 1. Setup (first time only)

1. `prism --help`. If not found, install it (needs [uv](https://docs.astral.sh/uv/)):
   `uv tool install git+https://github.com/prunelabsco/prism`
2. `prism init`, then `prism doctor`. Walk the user through every `FIX` line until all are `ok`. The usual one is the Instagram login: ask which browser they are logged in to Instagram with, then set `browser` (and `browser_profile` if they use several) under `[download]` in the config file `prism init` printed. Cookies stay on their machine; yt-dlp reads them locally.
3. If `~/.prism/profile.md` is missing or still the template, ask the user 2-3 short questions (what they do, what they save reels for, what they want out of them) and write their answers there. This is how ideas become relevant to *them*, not generic.
4. They need their Instagram export. If they don't have it, tell them:
   Instagram app or web → Accounts Center → Your information and permissions → Download your information → Download or transfer information → their Instagram account → Some of your information → **Saved** → Download to device → Format **JSON**, date range **All time** → Create files. Meta emails a link (minutes to a couple of days). The ZIP is what `prism fetch` takes.

## 2. Fetch and process

```bash
prism fetch path/to/instagram-export.zip     # or: prism fetch urls.txt --source urls
prism process                                # 30 downloads per run by default
prism status
```

- `process` downloads at most `max_per_run` reels per run, throttled on purpose. Instagram rate-limits aggressive downloading and may lock the account. Don't loop `process` back to back or in parallel; suggest a few runs spread over the day for big backlogs.
- If `process` reports it stopped on rate limit / login, stop and tell the user. Don't retry immediately.
- `prism fetch` again picks up `/p/` posts it deferred.

## 3. Curate (repeat N times, default 5)

Read `references/curation.md` once before the first reel. It has the judgment rules, the `curation.json` schema and the confidence criteria.

1. `prism next` prints a `packet.json` path, or `No ready reels.` (then stop and summarize). The reel folder is that file's folder.
2. Read `packet.json`:
   - `user_note`, `collections`: the user's own hints about why they saved it. Hints, not proof.
   - `categories`: `core`/`approved` are active; `proposed` already exist as proposals (reuse, never propose a twin).
   - `recent_titles`: for spotting re-uploads (`duplicate_of`).
3. **Look at every frame** (`frames[].path`, relative to the reel folder). Most reels carry their content as on-screen text; never judge from caption or transcript alone.
4. Read `transcript` (`has_speech: false` with an empty transcript is normal).
5. Write `curation.json` in the reel folder. Every evidence item is labeled:
   - `seen`: visible in a frame or said in the transcript. Cite `at_s` (a frame's `t` or a segment's `start`).
   - `guessed`: your inference.
   - `unknown`: something the reel doesn't show or say (e.g. "recipe amounts only in the DM").
   Photo posts (`media: "photo"`): `at_s` is null, put the slide number in `text` ("Slide 3: ...").
6. `prism commit <reel_id> <path/to/curation.json>`
   - Validation error: read all messages, fix, commit again. At most 2 fixes.
   - Still failing: `prism commit <reel_id> --fail "<short last error>"` and move on.

Count before committing: `card_line` max 70 characters, `summary` max 300.

After the loop, if `prism status` shows ideas needing a category, `prism recategorize` lists them; move only the clear fits with `prism recategorize <idea_id> <slug>`. Never approve or reject categories: that's the user's call.

## 4. Export

`prism export` writes one Markdown note per curated reel to the output folder. It never overwrites a note the user edited. The user marks notes `status: picked` or `dropped` in the frontmatter; `prism sync` reads that back, `prism status --picked` lists the picks.

## Report to the user after each session

- Reels curated, ideas per category.
- New category proposals with reasons; they decide with `prism category approve|reject <slug>`.
- Duplicates found, reels marked `--fail` and why.
- Anything you were unsure about (blurry frames, cut-off text) so they can judge quality.
