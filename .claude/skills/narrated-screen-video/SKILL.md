---
name: narrated-screen-video
description: Make a narrated product walkthrough / demo / explainer video of a web app or dashboard, fully from the terminal - natural-sounding voice-over, a moving cursor, smooth up/down/left/right scrolling, spotlight rings, burned-in highlighted captions, title and "the problem" cards, SRT subtitles and YouTube chapters. Works in a locked-down cloud sandbox (headless Chromium + ffmpeg + an offline neural voice, no Hugging Face or cloud TTS needed). Use this whenever the user wants a screen recording, demo video, product tour, walkthrough, tutorial or voice-over of an app, dashboard, site or tool, wants to "explain each tab", wants a robotic voice replaced with a natural one, wants to re-voice or re-record an existing video, or says anything like "record this and narrate it" - even if they never mention a skill, Playwright, TTS or ffmpeg.
---

# Narrated screen video

Turn a web app into a polished, narrated tour: a storyboard (JSON) goes in, an MP4 + captions + chapters come out. Every sentence of narration is a **beat** that owns the on-screen actions performed while it is spoken, so picture and voice cannot drift apart.

This was built and debugged end to end on a real project (a Claude Code analytics dashboard, 8-minute video, 52 beats, 14 chapters). The lessons that cost the most time are written into the steps below - read the "Why" lines, they are the expensive ones.

## What you get

- `scripts/build_video.py` - one command: synthesize voice -> record browser -> assemble MP4, SRT, chapters, script text
- `scripts/revoice.py` - change **only the voice** of an existing recording in minutes, no re-recording
- `scripts/record.py` + `scripts/overlay.js` - the recorder and the on-page overlay (cursor, spotlight, captions, cards)
- `scripts/tts.py` - voice engines behind one interface (edge, kokoro, sapi, say, flite)
- `scripts/fetch_kokoro.sh` - gets the offline neural voice through npm when Hugging Face is blocked
- `scripts/check_video.py` - duration, loudness, sync check and a contact sheet to look at
- `assets/script.example.json` - a minimal storyboard to copy
- `references/` - read on demand: `voice.md`, `storyboard.md`, `recording-and-sync.md`, `troubleshooting.md`

## Workflow

Copy `scripts/` next to the project (e.g. `video/`) so the user can re-run it locally; the skill folder itself may be read-only.

### 1. Settle the brief before touching anything (2 questions max)

Get these from the conversation first; ask only what is missing and genuinely changes the work:
- **Audience and goal** - the opening must state the problem the product solves, before any UI. A tour that starts on a dashboard with no "why" is the first thing viewers skip.
- **Data** - real data (user's machine) or sample data? In a cloud sandbox the user's real data usually is **not there**. Do not pretend: build against realistic generated sample data, label the video (a small corner badge), and give the user the one-line command to produce the real-data cut locally. Never present synthetic numbers as real usage.
- **Final voice** - the user's machine may reach cloud voices the sandbox cannot (see step 3).

### 2. Recon the environment (cheap, saves an hour)

Check what is actually reachable before designing around it:

```bash
python3 -c "import playwright; print(playwright.__version__)"; ls /opt/pw-browsers 2>/dev/null   # Chromium present?
ffmpeg -version | head -1; ffmpeg -filters | grep -c flite
pip download edge-tts --no-deps -d /tmp/x -q && echo pypi-ok
curl -sI https://huggingface.co | head -1; curl -sI https://registry.npmjs.org/kokoro-js | head -1
```

Typical sandbox: PyPI/npm/raw.githubusercontent reachable; Hugging Face, GitHub releases, Bing (edge-tts), Google, OpenAI blocked. Do not try to bypass a network policy - route around it with an allowed mirror (npm carries the Kokoro weights). Never `pkill -f <pattern>` where the pattern appears in your own command line: it kills your shell (exit 144). Use `kill $(cat pidfile)` or a pattern that cannot match itself.

### 3. Pick the voice - this decides whether the video is watchable

Quality ladder, best first: **edge** (Microsoft neural, needs internet to Bing) > **kokoro** (offline neural, ~92 MB, very natural) > **sapi/say** (OS voices) > **flite** (robotic; preview of timing only).

The first version of the real video used flite and the user could not understand it. Do not ship flite as a deliverable. Decision:
1. Try `edge` (`pip install edge-tts`). If the network blocks it, `tts.py` raises - it never silently swaps voices mid-video.
2. Otherwise `bash scripts/fetch_kokoro.sh ~/.cache/kokoro`, `pip install kokoro-onnx numpy`, export the two env vars it prints, use `--engine kokoro --voice af_heart`.
3. Tell the user which voice the sandbox cut used and that their own machine can use edge for the final cut. Details, voice names, pacing and text-normalisation tricks: `references/voice.md`.

Audition before the long job: synthesize one paragraph with 2-3 candidate voices (`tts.synth`) and send the WAVs. A 20-second sample costs seconds; re-recording an 8-minute video costs 20 minutes.

### 4. Write the storyboard

Copy `assets/script.example.json`. Full guide in `references/storyboard.md`; the essentials:
- Structure: **title card -> the problem -> what exists today and why it falls short -> the solution -> tab-by-tab tour -> wrap-up card with the run command / URL**.
- One sentence per beat, 10-30 words, written to be *heard* (spell out "A P I", "S Q L" if the voice mangles them and use `cap` for the correct on-screen text).
- Each beat's `do` list performs the visuals *while* the sentence plays: switch tab, `scroll` to a card, `spot` it with a label, `hover`/`sweep` a chart, `hscroll` a wide table, `click` to open a modal. Viewers asked for real movement - scroll up/down/left/right - not just tab switches.
- Prefer explaining **what the viewer should take away** from each tab (the question it answers), not reading numbers aloud.
- Add `chapter` on the first beat of each section (becomes YouTube chapters).

### 5. Dry-run on a few beats, then the full build

```bash
python scripts/build_video.py --script video/script.json --url http://127.0.0.1:8799/ \
   --serve-cmd "python app.py --port 8799" --config-js video/cfg.js \
   --engine kokoro --voice af_heart --size 1600x900 --out video/out/demo.mp4 --work /tmp/work --only 8
```

`--only N` records the first N beats (about a minute) to prove targets resolve (`! target not found` lines) and the look is right. Then drop `--only` for the real run. A full 8-minute video takes roughly its own length plus ~10 minutes of encoding and TTS. Run it in the background and poll; do not block on it.

Serving: the app must run without side effects while recorded (no desktop notifications, no writes to the user's state files). If it has any, wrap its server in a small recording launcher that neuters them (see `references/recording-and-sync.md`).

### 6. Verify like an editor - without listening

You cannot hear the audio, so prove everything else:
```bash
python scripts/check_video.py out/demo.mp4 --work /tmp/work --sheet out/contact.jpg --frames 24 --at 65 130
```
Open the contact sheet (Read the image) and check every scene, scroll position, caption and the final card. Confirm: loudness mean about -16 dB with peak below -1 dB, first frame 0.2-0.3 s before the first beat, no "not found" targets, captions absent over title cards, SRT text matching what is spoken. Fix what you see, then rebuild.

### 7. Deliver

- Send the MP4 (messaging often caps files at ~30 MB: re-encode with `-crf 27 -c:a copy`, which keeps the voice untouched), plus `.srt`, `.chapters.txt`, `.script.md`.
- Say plainly what is synthetic, which voice was used, and the exact command for the user's real-data cut.
- Commit the scripts and storyboard, not `out/` (add it to `.gitignore`). Push to the branch the user named; do not open a PR unless asked.

## Changing things later

| User says | Do |
|---|---|
| "The voice is bad / make it sound real" | Only the voice: `revoice.py --work <dir> --engine ... --voice ...` (minutes). Do not touch the picture. |
| "Remove / reword that sentence" | If it changes the on-screen caption or timing, edit `script.json` and re-run `build_video.py` with the same `--work` (TTS clips are cached by content, only changed lines re-synthesize). Tell the user the voice-only shortcut would leave the caption out of sync. |
| "Different pace" | `--rate -8%` (slower) / `+6%`; re-run. |
| "Hide private details" | Use the app's own privacy mode via `--init-js`, or blur selectors with an `eval` action. |
| "How long will it take?" (no go-ahead yet) | Answer with options and time, change nothing, wait for the decision. |

## Principles that held up

- **One sentence = one beat = its own actions.** Narration is placed at the beat's start + 0.30 s, the beat lasts audio + 0.30 + 0.55 s. Sync is structural, not tuned afterwards.
- **Frames carry real timestamps** (Chrome screencast), then a constant-frame-rate encode. Never screen-record at wall-clock and hope.
- **Fail loudly.** No silent fallback voices, no silent missing targets; a wrong video you notice is cheaper than a plausible wrong one you ship.
- **Cache by content** (engine|voice|rate|text hash). Iteration speed is what keeps a 20-minute job from becoming a 90-minute one.
- **Be honest about data.** Synthetic data gets a visible label; real-data runs are the user's to make.
