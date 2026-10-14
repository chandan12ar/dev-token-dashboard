# Walkthrough video

Records a narrated tour of **AI Fluency Insights**: a spoken intro to the problem, then every tab, with a
moving cursor, smooth scrolling, a spotlight ring on whatever is being explained, and captions.

## Make the real video (on your machine, with your own logs)

```
pip install playwright edge-tts
playwright install chromium          # plus ffmpeg on your PATH
python video/build_video.py          # reads ~/.claude/projects
```

Output lands in `video/out/`: the `.mp4`, an `.srt` caption file, `.chapters.txt` (paste into a YouTube
description) and `.script.md` (the narration as text).

Useful flags:

| Flag | What it does |
|---|---|
| `--privacy` | Turns on the dashboard's Privacy mode first: hides session titles, prompt previews and file names. Use this if the video will be shared. |
| `--voice en-US-AvaNeural` | Another edge-tts voice. `edge-tts --list-voices` shows them all. |
| `--engine sapi` / `say` | Offline voices (Windows / macOS) if you do not want edge-tts, which needs internet. |
| `--engine kokoro` | Kokoro-82M, an open neural voice that runs offline on CPU and sounds natural. `pip install kokoro-onnx`, then set `KOKORO_MODEL` (the `model_quantized.onnx` file) and `KOKORO_VOICES` (the `voices/` folder of the `kokoro-js` npm package). Default voice `af_heart`. |
| `--rate -8%` | Slow the narration down (edge-tts). |
| `--dir PATH` | Read logs from somewhere else. |
| `--no-captions` | Do not burn captions into the picture. |
| `--sample` | Use generated **sample** data (`make_sample_logs.py`) and label the video "SAMPLE DATA - PREVIEW". For trying the pipeline on a machine with no logs. Never presented as real usage. |

A run takes roughly as long as the video plus encoding (about 10 minutes for a 7 minute video).

## Change only the voice

`build_video.py` keeps its working folder. To try another voice without recording the screen again
(minutes instead of a full run):

```
python video/revoice.py --work <that folder> --engine kokoro --voice af_heart --out video/out/new-voice.mp4
```

Each sentence is fitted into the slot the old one had, sped up slightly (at most 18%) if it runs long.

## Change the story

Everything spoken and every on-screen action is in `walkthrough.json`. Each beat is one sentence (`say`)
plus the actions to perform while it is spoken (`do`): `tab`, `scroll`, `spot`, `hover`, `sweep`, `click`,
`hscroll`, `stage`, `reveal`, `theme`, `wait`. Targets are CSS selectors, `card:<title>`, `kpi:<label>`,
or `nav:<tab>`. `cap` overrides the caption text when the spoken form differs (for example "A P I" vs "API").

## How it works

* `serve_for_recording.py` serves the dashboard on your logs with toasts and notification state switched
  off, so recording never pops a notification or touches `.notify_state.json`.
* `tts.py` turns each sentence into audio (edge-tts, Windows SAPI, macOS `say`, or ffmpeg flite).
* `record.py` + `overlay.js` drive headless Chromium and capture frames with timestamps from Chrome's
  screencast, so the voice is placed on the exact moment each sentence began.
* `build_video.py` joins frames and narration with ffmpeg (H.264 + AAC, loudness-normalised).
