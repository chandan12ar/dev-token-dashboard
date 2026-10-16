---
name: narrated-screen-video
description: Make a narrated product walkthrough / demo / explainer video of a web app, dashboard or command-line tool, fully from the terminal - natural-sounding voice-over, a moving cursor, smooth up/down/left/right scrolling, spotlight rings, word-by-word highlighted captions, brand-matched title and "the problem" cards, SRT subtitles and YouTube chapters, plus poster, 30-second teaser, vertical 9:16 cut and GIF. Works in a locked-down cloud sandbox (headless Chromium + ffmpeg + an offline neural voice, no Hugging Face or cloud TTS needed). Use this whenever the user wants a screen recording, demo video, product tour, walkthrough, tutorial, release-notes video or voice-over of an app, dashboard, site, API docs or CLI, wants to "explain each tab", wants a robotic voice replaced with a natural one, wants to re-voice or re-record an existing video, or says anything like "record this and narrate it" - even if they never mention a skill, Playwright, TTS or ffmpeg.
---

# Narrated screen video

Turn a web app (or a terminal session) into a polished, narrated tour: a storyboard (JSON) goes in, an MP4 + captions + chapters (+ optional teaser, vertical cut, GIF) come out. Every sentence of narration is a **beat** that owns the on-screen actions performed while it is spoken, so picture and voice cannot drift apart.

Built and debugged end to end on a real project (a Claude Code analytics dashboard: 8 minutes, 52 beats, 14 chapters). The lessons that cost the most time are written into the steps below - read the "why" lines, they are the expensive ones. Slash command: `/make-video <url or command> <what to cover>`.

## What is in the box

| File | Purpose |
|---|---|
| `scripts/selftest.sh` | 1-2 minute proof that this machine can make a video (demo app, draft, record, check, extras). Run it first. |
| `scripts/draft_storyboard.py` | Explores the app (tabs, cards, charts, wide tables), writes `script.json` with the actions filled in and screenshots of every view. Narration left as `TODO`. |
| `scripts/build_video.py` | One command: voice -> record -> MP4, SRT, chapters, script text. Refuses to run while any `TODO` remains. |
| `scripts/revoice.py` | Change only the voice of an existing recording, in minutes. |
| `scripts/extract_brand.py` | Reads the app's accent colour and font so title cards match it (`--brand`). |
| `scripts/check_video.py` | Duration, loudness, A/V sync, pacing (words/min, still stretches), contact sheet. |
| `scripts/make_extras.py` | Poster, ~30 s teaser, vertical 9:16, GIF from a finished video. |
| `scripts/save_login.py` | Saves a signed-in session for apps behind a login (`--storage-state`). |
| `scripts/fetch_kokoro.sh` | Gets the offline neural voice through npm when Hugging Face is blocked. |
| `scripts/record.py`, `overlay.js`, `tts.py`, `words.py`, `lexicon.default.json` | The recorder, the on-page overlay, voice engines, word timing for highlighting, pronunciation fixes. |
| `assets/templates/` | Storyboard skeletons: dashboard, SaaS onboarding, API docs, CLI tool, release notes. |
| `assets/terminal-replay.html` | A scripted terminal, so CLI tools are filmed with the same pipeline. |
| `assets/demo-site/`, `assets/examples/` | A tiny app + storyboard used by the self-test; a working CLI example. |
| `references/` | Read on demand: `voice.md`, `storyboard.md`, `recording-and-sync.md`, `deliverables.md`, `troubleshooting.md`. |

## Workflow

Copy `scripts/` (and `assets/` if you want the templates) next to the project, e.g. `video/`, so the user can re-run it locally; the skill folder may be read-only.

### 1. Settle the brief (2 questions at most)

Take answers from the conversation first; ask only what is missing and changes the work:
- **Audience and goal.** The opening must state the problem the product solves before any UI. A tour that starts on the app with no "why" is the first thing viewers skip.
- **Data.** In a cloud sandbox the user's real data usually is not there. Build against realistic generated sample data, label the video (`--badge`), and give the one-line command for the real-data cut. Never present synthetic numbers as real.
- **Privacy.** If the screen can show emails, keys, customer names: `--blur-auto` plus `--blur ".selector"` for the rest, and check the contact sheet.
- **Login.** Behind a sign-in? The user runs `save_login.py` on their machine; recording uses `--storage-state`. Treat that file as a password.

If the user is only asking *what/how long/can we* - answer with options and time, change nothing, wait for the go-ahead.

### 2. Recon, then self-test

```bash
ls /opt/pw-browsers 2>/dev/null; ffmpeg -version | head -1                 # Chromium + ffmpeg?
curl -sI https://huggingface.co | head -1; curl -sI https://registry.npmjs.org/kokoro-js | head -1   # what is reachable
bash scripts/selftest.sh --engine auto                                      # whole pipeline on a demo app
```
Typical sandbox: PyPI/npm reachable; Hugging Face, GitHub releases, Bing (edge-tts), Google, OpenAI blocked. Do not bypass a network policy - route around it with an allowed mirror (npm carries the Kokoro weights). Never `pkill -f <pattern>` where the pattern is in your own command line: it kills your shell (exit 144). Kill by PID.

### 3. Pick the voice - this decides whether the video is watchable

Best first: **edge** (Microsoft neural, needs internet) > **kokoro** (offline neural, ~92 MB, very natural) > sapi/say > **flite** (robotic; timing previews only - a user could not understand it). `tts.py` raises rather than silently swapping voices.
1. Try edge. 2. Otherwise `bash scripts/fetch_kokoro.sh ~/.cache/kokoro`, `pip install kokoro-onnx numpy`, export the two variables it prints, use `--engine kokoro --voice af_heart`. 3. Tell the user which voice the sandbox cut used; their machine can use edge for the final.

Pace matters as much as timbre: Kokoro at native speed reads ~200 words/min (tiring). Defaults are set per engine (kokoro -20%, edge -4%) to land near 160. Audition 2-3 voices on one paragraph before the long job. Mispronounced terms go in a lexicon (`--lexicon`), not in the narration text. Details: `references/voice.md`.

### 4. Storyboard

```bash
python scripts/draft_storyboard.py --url http://127.0.0.1:8799/ --out video/script.json --shots video/shots
```
**Look at the screenshots**, then write the narration for every `TODO`, keeping the drafted actions (fix selectors it got wrong). Start from `assets/templates/*.json` for the shape of the story. Essentials (full guide: `references/storyboard.md`):
- Arc: title card -> the problem -> what exists today and why it falls short -> the solution -> tab-by-tab tour -> wrap-up card with the command/URL.
- One sentence per beat, 10-30 words, written to be heard. Explain what question each view answers, not the numbers.
- Real movement - scroll, hover, sweep a chart, scroll a wide table sideways - while the sentence plays.
- `chapter` on the first beat of each section. `cap` overrides caption text when it differs from speech.
- CLI tool? Serve `assets/terminal-replay.html` and drive it with `eval` actions; paste REAL output, never invent it.
- Match the app's look: `python scripts/extract_brand.py --url ... --out video/brand.json`, then `--brand`.

### 5. Dry run, then the full build

```bash
python scripts/build_video.py --script video/script.json --url http://127.0.0.1:8799/ --serve-cmd "python app.py --port 8799" \
   --config-js video/cfg.js --brand video/brand.json --engine kokoro --out video/out/demo.mp4 --work /tmp/work --only 8
```
`--only 8` records 8 beats (~1 min) to prove targets resolve. A missing spotlight/scroll/click target now **fails the build (exit 3)** and is listed at the end; fix the storyboard, don't pass `--allow-missing` to hide it. Then drop `--only`. A full 8-minute video takes about its own length plus ~10 minutes; run it in the background and poll.

The recorded app must have no side effects (no desktop notifications, no writes to the user's state): wrap its server in a recording launcher if it does (`references/recording-and-sync.md`).

### 6. Verify like an editor - you cannot hear the audio

```bash
python scripts/check_video.py video/out/demo.mp4 --work /tmp/work --script video/script.json --sheet video/out/contact.jpg --frames 24
```
Open the contact sheet and look at every scene, caption, blur and the final card. Check: loudness mean about -16 dB / peak under -1; first frame 0.2-0.3 s before the first beat; pacing findings (anything over ~185 wpm, long sentences, stretches with almost no change on screen) fixed or consciously accepted. Rebuild after fixes.

### 7. Deliver

- Send the MP4 (messaging often caps at ~30 MB: `ffmpeg -i in.mp4 -c:v libx264 -crf 27 -c:a copy small.mp4` keeps the voice bit-identical) plus `.srt`, `.chapters.txt`, `.script.md`.
- Extras on request: `python scripts/make_extras.py out/demo.mp4 --work /tmp/work --script video/script.json` -> poster, teaser, vertical, GIF (`references/deliverables.md`).
- Say plainly what is synthetic, which voice was used, and the exact command for the user's real-data cut.
- Commit scripts and storyboard, not `out/` (gitignore it, and any `state.json` login file). Push to the branch the user named; no PR unless asked.

## Changing things later

| User says | Do |
|---|---|
| "Voice is bad / make it sound real" | Only the voice: `revoice.py --work <dir> --engine ... --voice ...` (minutes); don't touch the picture. |
| "Too fast / too slow" | Faster/slower narration changes beat lengths, so re-run `build_video.py --rate -25%` with the same `--work` (cached clips keyed by rate; picture is re-recorded). |
| "Remove / reword that sentence" | Edit `script.json`, re-run `build_video.py` with the same `--work` (only changed lines re-synthesize). The voice-only shortcut would leave the burned-in caption out of sync - say so. |
| "That word is pronounced wrong" | Add it to a lexicon JSON, `--lexicon`; re-run (only affected lines change). |
| "Hide X" | `--blur ".selector"` / `--blur-auto`, or privacy mode of the app via `--init-js`. |
| "Make a short / vertical / GIF / thumbnail" | `make_extras.py`. |

## Principles that held up

- **One sentence = one beat = its own actions.** Narration is placed at the beat start + 0.30 s; the beat lasts clip + 0.30 + 0.55 s. Sync is structural, not tuned afterwards.
- **Frames carry real timestamps** (Chrome screencast), then a constant-frame-rate encode.
- **Fail loudly.** No silent fallback voices, no silent missing targets, no TODO narration; a wrong video you notice beats a plausible wrong one you ship.
- **Clicks never get nudged.** Only decorative cursor moves are kept off the header and caption bar; a nudged click hits the neighbouring button (found by the self-test).
- **Overlay classes never collide with the host app** (`v-`/`vw-` prefixes): a page's own `.cur` class once broke the caption highlight.
- **Cache by content** (engine|voice|rate|text hash). Iteration speed is what keeps a 20-minute job from becoming 90.
- **Honest data.** Synthetic data gets a visible label; real-data runs are the user's to make.
