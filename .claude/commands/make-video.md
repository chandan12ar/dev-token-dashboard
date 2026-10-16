---
description: Make a narrated walkthrough video (voice-over, cursor, scrolling, highlighted captions) of a web app, dashboard or CLI tool
argument-hint: "<app URL or start command> [what the video should cover] [--voice/--sample/--privacy notes]"
---

Use the `narrated-screen-video` skill to make a narrated walkthrough video.

Request: $ARGUMENTS

Follow the skill's workflow exactly:

1. Read `.claude/skills/narrated-screen-video/SKILL.md` and copy `scripts/` next to the project (for example `video/`) if it is not there yet.
2. Settle the brief from the request and the conversation. Ask at most two questions, and only about what you cannot work out: real data or sample data, and which voice the user's machine can use. If the request is only a question ("how long would it take?", "can we remove that line?"), answer with options and time and change nothing.
3. Run `bash scripts/selftest.sh` once on a new machine so a broken setup is found in two minutes, not twenty.
4. Recon the environment, choose the best available voice (edge, else Kokoro via `scripts/fetch_kokoro.sh`; never ship the robotic fallback), draft the storyboard with `draft_storyboard.py`, look at its screenshots, then write every narration line yourself.
5. Dry-run with `--only 8`, fix every `! target not found`, then do the full build in the background and poll it.
6. Verify with `check_video.py` (loudness, sync, pacing, contact sheet) and look at the contact sheet before delivering.
7. Deliver the MP4 with the `.srt`, chapters and script text, plus any extras the user asked for (`make_extras.py`). State what is sample data, which voice was used and the one-line command for the real-data cut. Commit scripts and storyboard (not `out/`) to the branch the user named; do not open a PR unless asked.
