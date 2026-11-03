# Prism

You save hundreds of Instagram reels "for later". Prism turns them into notes you can actually use: one Markdown note per reel, with the ideas worth taking, sorted into categories, and every claim labeled **seen**, **guessed** or **unknown**.

It runs entirely on your machine with your own AI agent (Claude Code, Codex, OpenCode, Gemini CLI, Cursor...). No server, no account, nothing uploaded.

```
Instagram export ──> prism fetch ──> prism process ──> your agent curates ──> prism export
  (your saved          queue          download, frames,     looks at frames,       Markdown notes
   reels + collections)                transcript (local)    writes ideas           in a folder
```

## Why the labels

Reels are full of confident claims. Prism's curation rules make the agent separate what it actually **saw** in a frame or heard in the transcript (with the timestamp) from what it **guessed**, and what the reel never shows (**unknown**, like "recipe only sent by DM"). You can trust a note at a glance.

## Requirements

| | |
|---|---|
| Python 3.12 + [uv](https://docs.astral.sh/uv/) | installs and runs the CLI |
| [ffmpeg](https://ffmpeg.org/download.html) | frames and audio (`brew install ffmpeg`, `winget install ffmpeg`, `apt install ffmpeg`) |
| An AI coding agent **that can read images** | does the curation. Text-only models can't see the frames. |
| A browser logged in to Instagram | yt-dlp reads its cookies locally to download your saved reels |

Transcription is local and installed automatically: [mlx-whisper](https://github.com/ml-explore/mlx-examples) on Apple Silicon, [faster-whisper](https://github.com/SYSTRAN/faster-whisper) everywhere else. The first run downloads the whisper model (~1.6 GB).

## Setup (about 10 minutes, plus waiting for Instagram)

**1. Request your Instagram export** (do this first, Meta can take a while):
Instagram → Accounts Center → Your information and permissions → Download your information → Download or transfer information → your account → Some of your information → **Saved** → Download to device → Format **JSON**, date range **All time** → Create files. Meta emails you a download link.

**2. Install the CLI and the agent skill:**

```bash
uv tool install git+https://github.com/prunelabsco/prism
```
```bash
npx skills add prunelabsco/prism
```

The second command copies the `prism` skill into your agent's skills folder. Prefer manual? Copy `skills/prism/` into `~/.claude/skills/` (Claude Code), `~/.agents/skills/` (Codex), or wherever your agent loads [Agent Skills](https://agentskills.io) from.

**3. Initialize and check:**

```bash
prism init
prism doctor
```

`init` creates `~/.prism/` with a config file and `profile.md`. Set your browser in `~/.prism/config.toml`:

```toml
[download]
browser = "chrome"           # chrome, brave, firefox, edge, safari, ...
# browser_profile = "Profile 1"  # only if you use several profiles
```

Run `prism doctor` again until everything says `ok`. Then write a few lines about yourself in `~/.prism/profile.md`. The agent uses it to judge what's useful **to you**. (Or just ask your agent to set Prism up; the skill walks it through all of this.)

## Using it

```bash
prism fetch ~/Downloads/instagram-yourname-2026-11-02.zip
prism process
```

Then in your agent: **"curate 10 reels with prism"**. When you're done for the day:

```bash
prism export      # notes go to ~/Documents/Prism
```

Already have a list of reel URLs instead of an export? `prism fetch urls.txt --source urls` (one URL per line).

### Go slow on purpose

`prism process` downloads at most 30 reels per run, a few seconds apart. Instagram rate-limits heavy downloading and can temporarily lock accounts. For a big backlog, run it a few times spread over the day, not in a loop. If it says it stopped on rate limit or login, wait.

## What a note looks like

```markdown
---
prism_id: ig_DExAmPlE01
source: https://www.instagram.com/p/DExAmPlE01/
via: ig_export
source_id: DExAmPlE01
user_note: ''
collections: [food🍴]
categories: [Recipes]
status: new
confidence: high
processed: 2026-11-03
tags: [prism]
---

## Summary
A 30-second one-pan lemon garlic pasta. Ingredients appear as on-screen text, steps are spoken.

## Ideas
### 1. One-pan lemon garlic pasta
Spaghetti cooked directly in stock with garlic and butter, lemon zest at the end. 4 ingredients, about 15 minutes.
**Why it matters:** You save quick weeknight meals; this one uses what's usually in the fridge.
**Category:** Recipes

## Evidence
- **Seen** (0:04): On-screen text: spaghetti, garlic, butter, lemon
- **Seen** (0:12): "Cook it right in the stock, no draining"
- **Guessed**: Roughly 2 servings, based on the pan size
- **Unknown**: Exact amounts, the caption says "full recipe in bio"

## Next actions
- [ ] Find the full recipe on the creator's profile

## My notes
<!-- prism: free area, never touched by prism -->
```

Change `status: new` to `picked` or `dropped` as you go through them, then `prism sync` and `prism status --picked` gives you the shortlist by category. Prism never overwrites a note you edited. Notes work great in an [Obsidian](https://obsidian.md) vault: point `output_dir` inside it and set `vault_dir` so moved notes are still tracked.

## Categories

You start with five generic ones: **Recipes, Places, Learn, Projects, Shopping**. When reels don't fit, the agent **proposes** a new category with a reason. Nothing is added until you decide:

```bash
prism status                      # proposals show up first
prism category approve home-decor
prism category reject home-decor  # its ideas go back to "needs a category"
```

Your Instagram collection names are passed to the agent as a hint ("you saved this in *food🍴*"), but they never decide the category on their own.

## Time and cost

Measured on 157 real reels (MacBook Air M3, mlx-whisper, Claude Code):

| Per 100 reels | |
|---|---|
| `prism process` (download + frames + transcript) | ~28 min of machine time, spread over 4 runs because of the 30-per-run cap |
| Curation by the agent | ~48 min |
| Tokens / money | not measured yet. Curation sends every frame (up to 12 images per reel) to your model, so it uses your agent's normal quota or API pricing. |

faster-whisper on CPU-only machines transcribes slower than mlx on Apple Silicon. Set `[transcribe] model = "small"` in the config for speed over accuracy.

## Privacy

- Everything runs locally. Your export, cookies, videos, frames and notes stay on your disk (`~/.prism`, `~/Documents/Prism`).
- Prism talks to Instagram only through yt-dlp, logged in as you, at a deliberately slow pace.
- Curation goes through whichever AI agent you use, under that provider's terms. Prism adds no service of its own.
- Videos are deleted after frames and transcript are extracted (`keep_video = false`).

## Commands

| | |
|---|---|
| `prism init` / `prism doctor` | set up / check setup |
| `prism fetch <export.zip>` | queue reels from an Instagram export (or `--source urls`) |
| `prism process` | download, frames, transcript |
| `prism next` / `prism commit` | used by the agent while curating |
| `prism export` / `prism sync` | write notes / read your `status` edits back |
| `prism status [--picked]` | queue and category overview, or your picks |
| `prism category list\|approve\|reject` | manage categories |
| `prism retry` | retry failed reels |

## License

[MIT](LICENSE)
