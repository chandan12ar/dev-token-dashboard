# Product video

A ~85-second narrated tour of AI Fluency Insights: the problem, the answer, then
Overview, My Adoption, Quality and Advisor, ending on the one command to start.
Made with the `narrated-screen-video` skill; the scripts in `scripts/` are a copy of it.

| File | What it is |
|---|---|
| `script.json` | The storyboard: every sentence of narration and what happens on screen while it is spoken |
| `cfg.js` | Target helpers (`card:<heading>`, `nav:<tab>`) and two hidden elements (plan-usage card, footer path) |
| `brand.json` | Title-card colours and font, read from the dashboard |
| `make_sample_logs.py` | Deterministic **sample** Claude Code logs (no real usage) |
| `serve_for_recording.py` | Runs the dashboard with notifications off and no browser pop-up |

Output goes to `video/out/` (gitignored): MP4, `.srt` captions, `.chapters.txt`, `.script.md`.

## Rebuild the sample-data cut

```
pip install playwright kokoro-onnx numpy
bash video/scripts/fetch_kokoro.sh ~/.cache/kokoro        # offline neural voice; prints two exports
python video/make_sample_logs.py --out /tmp/sample-claude
python video/scripts/build_video.py --script video/script.json \
  --serve-cmd "python video/serve_for_recording.py --dir /tmp/sample-claude/projects --port 8799" \
  --url http://127.0.0.1:8799/ --config-js video/cfg.js --brand video/brand.json --sidebar-px 236 \
  --badge "SAMPLE DATA" --engine kokoro --voice af_heart --out video/out/ai-fluency-insights.mp4
```

## Teaser and vertical (LinkedIn) cuts

The ~37 s teaser is cut from the finished video, whole sentences only: the hook, what it is,
Overview, Advisor and the closing card. The vertical cut is recorded again at phone width
(`script-vertical.json`: same narration, shots re-framed for the one-column layout), so text
stays large on a phone. Both land in `video/out/extras/`.

```
python video/scripts/make_extras.py video/out/ai-fluency-insights.mp4 --work <build --work dir> \
  --script video/script.json --only teaser poster --teaser-beats 1,3,4,9,11
python video/scripts/build_video.py --script video/script-vertical.json \
  --serve-cmd "python video/serve_for_recording.py --dir /tmp/sample-claude/projects --port 8799" \
  --url http://127.0.0.1:8799/ --config-js video/cfg.js --brand video/brand.json \
  --size 540x960 --scale 1.5 --video-size 1080x1920 \
  --badge "SAMPLE DATA" --engine kokoro --voice af_heart --out video/out/extras/ai-fluency-insights-vertical.mp4
```

`--scale 1.5` is deliberate: at 2 the browser captures only ~18 frames a second while scrolling,
so motion stutters; 1.5 captures ~30 and is upscaled to 1080x1920.

## Real-data cut (on your own machine)

Drop `--dir` so it reads your own `~/.claude/projects`, drop `--badge`, and use the
Microsoft neural voice if you have internet (`pip install edge-tts`):

```
python video/scripts/build_video.py --script video/script.json \
  --serve-cmd "python video/serve_for_recording.py --port 8799" \
  --url http://127.0.0.1:8799/ --config-js video/cfg.js --brand video/brand.json --sidebar-px 236 \
  --engine edge --out video/out/ai-fluency-insights-real.mp4
```

Check before you share it: one sentence says "hooks or skills, you've never tried",
and that beat spotlights the Skills and Hooks tiles. If you already use them, change that
line in `script.json`. Your project names and file paths will be on screen too.
