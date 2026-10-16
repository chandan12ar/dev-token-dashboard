# Voice: getting natural-sounding narration

## Contents
1. Engine comparison
2. Kokoro offline setup (works when Hugging Face is blocked)
3. Writing text that a TTS reads well
4. Pacing and fitting
5. Auditioning and re-voicing
6. Pronunciation lexicon
7. Word-by-word caption highlighting

## 1. Engines (`scripts/tts.py`)

| Engine | Quality | Needs | Notes |
|---|---|---|---|
| `edge` | excellent | `pip install edge-tts`, internet to Microsoft | default on the user's machine. Voices: `en-US-AriaNeural` (default), `AvaNeural`, `AndrewNeural`, `GuyNeural`; `edge-tts --list-voices` |
| `kokoro` | very good, natural | `pip install kokoro-onnx numpy`, ~92 MB model, runs on CPU | Apache-2.0. Best offline option. Default voice `af_heart` (warm female); also `af_bella`, `af_nicole`, `am_adam`, `am_michael` |
| `sapi` | fair | Windows PowerShell | offline |
| `say` | fair | macOS | offline |
| `flite` | robotic | ffmpeg built with flite | timing previews only; a user could not understand it |

`auto` picks the first available in that order. `tts.synth()` **raises** on failure instead of falling back: a voice that changes halfway through a video is worse than a stopped build.

Output of every engine is normalised to mono 24 kHz s16 WAV so clips can be mixed sample-exactly.

## 2. Kokoro offline setup

Hugging Face and GitHub releases are blocked in many sandboxes, but npm usually is not, and npm republishes the pieces:

- model: npm `kokoro-q8-shards` -> `package/kokoro-q8.part0..5.bin`; `cat` them in order into `model_q8.onnx` (92 MB int8). Verify the join with sha256 if you have a reference; a truncated model fails to load rather than sounding wrong.
- voices: npm `kokoro-js` -> `package/voices/<name>.bin` (float32, 510x256 style vectors per voice).
- runtime: `pip install kokoro-onnx` (needs `numpy`, `onnxruntime`).

`bash scripts/fetch_kokoro.sh ~/.cache/kokoro` does all of that and prints:
```
export KOKORO_MODEL=~/.cache/kokoro/model_q8.onnx
export KOKORO_VOICES=~/.cache/kokoro/voices
```
`tts.py` packs the `.bin` files into `_voices.npz` on first use (the layout `kokoro-onnx` expects: `(510,1,256)` per voice), loads the model once per process, and maps `--rate +N%` to Kokoro `speed = 1 + N/100`.

Speed: roughly 1.5-3 s of compute per sentence on CPU; `revoice.py --jobs 2` runs clips in parallel. Memory is the limit, not CPU: do not set jobs above 2-3 in a small container.

## 3. Writing text that reads well aloud

- Short sentences, plain words. A comma where a person would breathe.
- Spell out what the engine mangles: "A P I", "S Q L", "C L I", "U R L". Put the properly written form in the beat's `cap` so the burned-in caption and SRT stay correct.
- Numbers: write them as you want them said ("about two hundred thousand tokens"), not as digits, unless the voice handles them (audition).
- Avoid parentheses, slashes and symbols; avoid reading file paths and code aloud, show them on screen instead.
- Product names that are pronounced oddly: respell phonetically in `say`, keep the real spelling in `cap`.
- Leave sentences without a trailing "..." - engines add odd pauses.

## 4. Pacing and fitting

- Defaults are per engine: edge `-4%`, kokoro `-20%` (`tts.DEFAULT_RATE`). Measured: Kokoro at native speed reads ~200 words/min, which is brisk and tiring over minutes; -20% lands near 160, the comfortable narration range. `check_video.py --script` reports words/min per beat (flags above 185).
- In `build_video.py` the recording is timed from the real clip lengths, so nothing is stretched.
- In `revoice.py` the *picture is fixed*, so each new clip must fit the old slot (old duration - 0.42 s). If it is longer it is re-spoken faster, at most +18% (beyond that the voice sounds rushed) and the script warns `still X s long` if it still overlaps. If many lines overflow, re-record instead of speeding up.
- Loudness: the final mix is `loudnorm I=-16 TP=-1.5`; a healthy result measures mean about -16 dB, peak below -1 dB (`check_video.py`).

## 5. Auditioning and re-voicing

Before a long build, render one representative paragraph with two or three voices and send the WAVs to the user:

```python
import sys; sys.path.insert(0, "scripts"); import tts
for v in ("af_heart", "af_bella", "am_adam"):
    tts.synth("This is the dashboard. Let's take a tour, tab by tab.", f"audition_{v}.wav", "kokoro", v)
```

To change the voice on an existing video (user said "don't change anything, just the voice"):
```bash
python scripts/revoice.py --script video/script.json --work <the build --work dir> \
    --engine kokoro --voice af_heart --jobs 2 --out video/out/new-voice.mp4
```
It needs the `--work` folder from the original build (`rec.json` has every beat's timing, `frames.txt` the picture). If the user also changed the *text*, the caption in the picture is baked in and no longer matches - re-record instead.

## 6. Pronunciation lexicon

Do not respell words inside the narration. Keep the storyboard text as written and add spoken forms to a lexicon: `scripts/lexicon.default.json` ships with common ones (`API` -> `A P I`, `SQL` -> `sequel`, `CLI`, `JSON`, `OAuth`...). Add yours in a file and pass `--lexicon mine.json` (merged over the default):

```json
{"Kokoro": "ko-ko-ro", "Nginx": "engine-x", "kubectl": "cube control"}
```
Whole words only, case-sensitive, applied to what is *spoken*; the burned-in caption and SRT keep the real spelling. TTS clips are cached by the spoken text, so a lexicon change re-synthesizes only affected lines. Audition the term before relying on a respelling.

## 7. Word-by-word caption highlighting

Captions highlight each word as it is spoken (karaoke style). No engine reports word times offline (Kokoro does not), so `words.py` estimates them: it finds where speech really starts and ends inside each clip, then spreads the words over that window in proportion to how long each takes to say (letters + syllables, with extra pause after commas and sentence ends). Typical error about 100 ms, which reads as in sync. Turn off with `--no-highlight`. Because timing is estimated from the caption text, a `cap` that differs a lot from the speech still highlights smoothly.
