# Troubleshooting - problems actually hit, and the fix

| Symptom | Cause | Fix |
|---|---|---|
| Voice unintelligible / robotic | Only the `flite` engine was available offline | Use `edge` where internet reaches it, else Kokoro via `fetch_kokoro.sh`. Never deliver flite. |
| edge-tts fails with connection errors | Sandbox blocks Bing | Expected. Use Kokoro here; edge on the user's machine. |
| Kokoro: model/voices can't be downloaded | Hugging Face / GitHub releases blocked | They are on npm: `fetch_kokoro.sh` (model shards + voice packs). |
| `KeyError`/voice not found in Kokoro | `_voices.npz` built from an empty/wrong folder | Delete `$KOKORO_VOICES/_voices.npz`, confirm `*.bin` files exist, rerun. |
| Everything after the first tab prints `! target not found`, or a click lands on the neighbouring button | The cursor keep-away clamp nudged click targets off their buttons (hit twice: dashboard, then the self-test) | Fixed: `tab`/`click` use the exact centre. If it recurs, check the target is visible in the current tab only. |
| Caption shows over a title card | Caption was not suppressed when a card was on | Already handled: captions hide on beats that show/continue a card. Don't re-add per beat. |
| Cursor visible over a title card | Cursor overlay stays on | `stage` action calls `V.hideCursor()`. |
| Scroll looks jumpy | Native smooth scrolling mixed with JS | Use `V.scrollTo` (eased rAF) via the `scroll` action; don't send `scroll-behavior` CSS. |
| `hscroll` does nothing | Table already fits at this width | Fine. To force it, use a narrower `--size`. |
| A toast pops up / app state file modified during recording | The app's own notifier ran | Launch it through a recording wrapper that neuters notifications and uses a temp state path. |
| Video is out of sync late in the film | Beat start times were taken from an estimate instead of actual clock, or frames weren't acked | Use `record.py` as is: real timestamps per beat and frame; check "first frame is 0.2-0.3 s before the first beat". |
| Highlighted caption word looks like a tiny cursor block / wrong size | Host page has its own `.cur`/`.w` CSS that matched the overlay's classes | Overlay classes are prefixed `vw-`; keep any new overlay class prefixed too. |
| Whole page dimmed with a stray label in the corner | Spotlight target became hidden (tab switched) | Fixed: spotlight fades out when its target has no box; add `{"spot": null}` before `tab` anyway. |
| `the storyboard still has TODO text` | `draft_storyboard.py` leaves narration blank on purpose | Write every `say` and the stage cards, then rebuild. |
| Build ends with `!! N target(s) were not found` and exit 3 | A selector no longer matches (or is in a hidden tab) | Fix the storyboard; `--allow-missing` only if the beat is fine without the effect. |
| Narration feels rushed / tiring | Kokoro native speed is ~200 wpm | Default is now `-20%`; `check_video.py --script` shows words/min per beat; shorten sentences over ~30 words. |
| A secret/email was visible in the video | Blur not enabled or selector missed it | `--blur-auto` and `--blur`; look at the contact sheet at several scroll positions before sharing. |
| `ModuleNotFoundError: soundfile` | Not needed | The pipeline writes WAVs with the stdlib `wave` module. |
| Send fails: file too large (~30 MB cap) | Output at crf 18 is ~40 MB | `ffmpeg -i in.mp4 -c:v libx264 -crf 27 -c:a copy -movflags +faststart small.mp4` |
| Shell dies with exit 144 | `pkill -f`/`pgrep -f` pattern matched its own command line | Kill by PID/pidfile, or use a pattern in brackets like `[h]ttp.server`. |
| Backgrounded job "finished" immediately | `nohup` inside a wrapper detached the real process | Poll `pgrep`/the log for `Done:`; start the command by itself, variables set separately. |
| `revoice.py` warns `still X s long` | New voice is slower than the old slot | Raise `--rate`, shorten the sentence and re-record, or accept a small overlap into the next beat's lead-in. |
| Caption text no longer matches the voice after a text change | Voice-only shortcut keeps the old picture | Edit the script and re-run `build_video.py` (cached clips make it faster). |
| User says "an hour is too long" | Re-ran everything for each tweak | Keep `--work`; use content-hash TTS cache, `--only N` for dry runs, `revoice.py` for voice-only changes. Quote time options before starting. |

## Time budget seen in practice (8 min video, 52 beats, 1600x900, CPU only)

- TTS (Kokoro): ~3 min for all clips first time; instant when cached.
- Recording: roughly real time (8 min).
- Encode: 2-3 min.
- Total about 15-20 minutes per full build; voice-only revoice 4-6 minutes.
