# Deliverables and quality checks

## Contents
1. Files a build produces
2. `check_video.py`
3. `make_extras.py`
4. Sharing notes

## 1. Files a build produces

`<name>.mp4` (H.264 + AAC, 30 fps), `<name>.srt` (captions, one cue per sentence, real spelling), `<name>.chapters.txt` (paste into a YouTube description), `<name>.script.md` (narration as text). The `--work` folder keeps `rec.json` (frame and beat times), `frames.txt`, cached `wav/` clips: keep it until the user approves the video; revoice and rebuilds depend on it.

## 2. `check_video.py`

```bash
python scripts/check_video.py out/demo.mp4 --work /tmp/work --script video/script.json --sheet out/contact.jpg --frames 24 --at 65 130
```
- duration, resolution, fps, audio present;
- loudness: mean about -16 dB, peak at or below -1 dB (flags otherwise);
- sync: first frame 0.2-0.3 s before the first beat;
- pacing per beat: words/min (flag over 185 or, for longer lines, under 95), sentences over 40 words;
- "almost no change on screen for N s" stretches (title cards and the held outro excluded; cursor movement alone does not count) - add a spotlight, scroll or hover there;
- contact sheet (`--frames` tiles) and single frames (`--at`). Look at them.
It cannot judge how a voice sounds; say so to the user and let them audition.

## 3. `make_extras.py`

```bash
python scripts/make_extras.py out/demo.mp4 --work /tmp/work --script video/script.json --out-dir out/extras [--only poster teaser vertical gif]
```
| Output | How it is made |
|---|---|
| `*-poster.jpg` | 1280x720 frame of the clean intro card (no caption). `--poster-at 42` for an app frame (pick a moment between sentences; captions are burned in), `--poster-title` for a title band (needs Pillow) |
| `*-teaser.mp4` | opening line + one strong beat (spotlight/scroll/chart/click) from several chapters + closing card, whole beats only, 0.35 s fades; `--teaser-len 30` target. Needs `--work` and `--script` |
| `*-vertical.mp4` | 1080x1920: the 16:9 picture centred over a blurred, darkened copy; audio copied. Captions stay but are small - for big portrait captions record again at `--size 1080x1920` on a responsive app |
| `*.gif` | 960 px, 12 fps, palette-optimised, silent; `--gif-at 20 --gif-len 6`, defaults to the first spotlight beat |

## 4. Sharing notes

- Chat/email caps are often ~30 MB: re-encode `-crf 27 -c:a copy`.
- Upload the `.srt` as captions; paste `.chapters.txt` into the description.
- Do not publish a video containing sample data without the label, or one that shows credentials: re-check the contact sheet after any storyboard edit.
