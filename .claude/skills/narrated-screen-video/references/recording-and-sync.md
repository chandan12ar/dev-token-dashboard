# Recording and sync: how the picture and voice stay together

## Contents
1. Pipeline
2. Why frames, not a screen recorder
3. Beat timing
4. Overlay: cursor, spotlight, captions
5. Serving the app safely
6. Encoding settings
7. Sandbox notes

## 1. Pipeline

```
script.json --tts--> one WAV per beat (cached) --> durations
            --record.py (Playwright + CDP screencast)--> frames + epoch timestamps + beat start times
            --build_video.py--> ffconcat list --> 30 fps CFR H.264 + narration mix --> MP4, SRT, chapters, script
```

## 2. Why CDP screencast frames

Headless Chromium has no window to capture, and a wall-clock screen recorder drifts and stutters under load. `Page.startScreencast` (JPEG q92) delivers a frame whenever the page changes, each with a wall-clock timestamp. `build_video.py` writes an ffmpeg concat list where each frame lasts until the next frame's timestamp, then `fps=30` resamples to constant frame rate. Result: frame-accurate timing independent of how fast the machine recorded. Always `Page.screencastFrameAck` every frame or Chrome stops sending.

## 3. Beat timing

- Synthesize all audio **first**; each beat's duration = clip length + `LEAD` 0.30 s + `TAIL` 0.55 s.
- The recorder logs `time.time()` at the start of each beat; narration is mixed in at `beat_start - first_frame_time + LEAD`. Visuals begin 0.30 s before the voice so the eye leads the ear.
- If a beat's actions run longer than its audio, the beat is extended and the recorded start times still hold, so later beats stay in sync. Check the build log line "first frame is 0.2-0.3 s before the first beat" - a different value means timestamps are off.
- Held title cards: start the page on the intro card *before* starting the screencast so frame 0 is already the intro, never the bare app.

## 4. Overlay (`overlay.js`, injected into the page)

All overlay elements are `position:fixed`, `pointer-events:none`, z-index above the app, so they never intercept clicks.
- **Cursor**: Chromium headless draws none. The overlay follows `mousemove` with an SVG arrow and adds a ripple on `mousedown`. The recorder moves the real mouse with an eased path (so hovers and clicks really fire) rather than teleporting.
- **Spotlight**: a box whose huge `box-shadow` dims everything else; tracked every animation frame so it follows scroll. The label chip flips below the target when there is no room above (sticky headers).
- **Captions**: a bottom pill, burned into the picture so the video works muted and on any player. Hidden over title cards (the card is the visual) and on beats following a card.
- **Badge**: small corner label, used for "SAMPLE DATA - PREVIEW".
- **Scrolling**: `V.scrollTo` runs an eased `requestAnimationFrame` loop; CSS `scroll-behavior` is bypassed so the motion is the same in every browser. `V.scrollInfo` leaves 112 px for a sticky header.

Cursor clamp: the cursor is kept at least 150 px from the top and 130 px from the bottom so it does not hide under the header or caption. A fixed left **sidebar is exempt** (`--sidebar-px`) because clamping there made every nav click miss its button.

## 5. Serving the app safely

The recorded app must not have real side effects. For an app that raises desktop notifications or persists state when it polls, write a tiny launcher that imports the app, replaces the notifier with a no-op, points state files at a temp dir, runs one baseline poll so cards have data, then serves. (The dashboard in this repo uses `video/serve_for_recording.py` as the pattern.) Pass it with `--serve-cmd`.

For sample data, generate deterministic data with a fixed seed (`make_sample_logs.py` is the pattern: realistic variety, habits that improve over time so charts have a story) and label the video.

## 6. Encoding settings

`libx264 -preset medium -crf 18`, `yuv420p`, `+faststart`, AAC 192k, `loudnorm=I=-16:TP=-1.5:LRA=11`. For a smaller share copy (~25 MB for 8 min at 1600x900): re-encode video at `-crf 27`, `-c:a copy` so the voice is bit-identical.

Narration is mixed in Python with the `wave` module into one 24 kHz mono track (no `soundfile` dependency), then loudness-normalised in the final ffmpeg pass.

## 7. Sandbox notes

- Use the pre-installed Chromium (`--chromium /opt/pw-browsers/chromium-*/chrome-linux/chrome` or `CHROMIUM_PATH`); do not run `playwright install`.
- Headless flags used: `--hide-scrollbars --force-device-scale-factor=1`.
- Run long builds in the background and poll for the output file / log; a `nohup` inside a wrapper can report completion early, so check the process or the final `Done:` line.
- Define shell variables in their own command, not in a `A=b && cmd &` chain (the assignment gets backgrounded too).
- Send large files in pieces under the channel cap or re-encode smaller.
