#!/usr/bin/env python3
"""Re-voice an already recorded walkthrough without recording the screen again.

build_video.py keeps its working folder (frames + rec.json with every beat's timing). This script
synthesizes the narration again with another engine or voice, fits each sentence into the slot the
original sentence had (speeding it up slightly if needed), and rebuilds the MP4. It takes minutes,
not a full re-recording, and the picture is identical.

    python revoice.py --script script.json --work <build_video --work dir> --engine kokoro --voice af_heart --out out/new.mp4
"""
import argparse
import json
import os
import subprocess
import sys
from multiprocessing import Pool

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tts  # noqa: E402
from build_video import FPS, frames_to_list, mix_narration, srt_time  # noqa: E402
from record import LEAD  # noqa: E402

MAX_SPEEDUP = 18     # percent: beyond this a voice starts to sound rushed


def _job(args):
    text, out, engine, voice, rate = args
    return tts.synth(text, out, engine, voice, rate)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", required=True, help="working folder of a previous build_video.py run")
    ap.add_argument("--engine", default="auto", choices=("auto",) + tts.ENGINES)
    ap.add_argument("--voice", default=None)
    ap.add_argument("--script", required=True, help="the same storyboard JSON the recording used")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    with open(a.script, encoding="utf-8") as f:
        beats = json.load(f)["beats"]
    with open(os.path.join(a.work, "rec.json")) as f:
        rec = json.load(f)
    engine = tts.pick_engine(a.engine)
    voice = a.voice or tts.DEFAULT_VOICE[engine]
    data_line = ""
    wdir = os.path.join(a.work, "wav-%s-%s" % (engine, voice))
    os.makedirs(wdir, exist_ok=True)
    texts = {bt["i"]: beats[bt["i"]]["say"].replace("{DATA}", data_line).strip() for bt in rec["beats"]}
    slot = {bt["i"]: bt["dur"] - LEAD - 0.12 for bt in rec["beats"]}

    def path(i, rate):
        return os.path.join(wdir, "%03d%s.wav" % (i, "" if rate == "+0%" else rate.replace("%", "pc")))

    print("voice: %s / %s, %d lines" % (engine, voice, len(texts)))
    todo = [(texts[i], path(i, "+0%"), engine, voice, "+0%") for i in texts if not os.path.exists(path(i, "+0%"))]
    with Pool(a.jobs) as pool:
        pool.map(_job, todo)
    final, redo = {}, []
    for i in texts:
        d = tts.duration(path(i, "+0%"))
        if d <= slot[i]:
            final[i] = path(i, "+0%")
        else:
            pct = min(MAX_SPEEDUP, int((d / slot[i] - 1) * 100) + 2)
            redo.append((i, "+%d%%" % pct))
    if redo:
        print("  %d lines run long; re-speaking them slightly faster" % len(redo))
        with Pool(a.jobs) as pool:
            pool.map(_job, [(texts[i], path(i, r), engine, voice, r) for i, r in redo if not os.path.exists(path(i, r))])
        for i, r in redo:
            final[i] = path(i, r)
            over = tts.duration(final[i]) - slot[i]
            if over > 0:
                print("  ! line %d still %.1f s long; it overlaps the next sentence's lead-in" % (i + 1, over))

    list_path = os.path.join(a.work, "frames.txt")
    t0, total = frames_to_list(rec, list_path)
    starts = {bt["i"]: max(0.0, bt["start"] - t0 + LEAD) for bt in rec["beats"]}
    narr = os.path.join(a.work, "narration-%s.wav" % voice)
    mix_narration(narr, total + 0.5, [(starts[i], final[i]) for i in texts])
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
                    "-i", narr, "-vf", "fps=%d,format=yuv420p" % FPS, "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                    "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                    "-shortest", a.out], check=True)
    with open(os.path.splitext(a.out)[0] + ".srt", "w", encoding="utf-8") as f:
        for n, i in enumerate(sorted(texts), 1):
            b = beats[i]
            text = (b.get("cap") or b["say"]).replace("{DATA}", data_line).strip()
            f.write("%d\n%s --> %s\n%s\n\n" % (n, srt_time(starts[i]), srt_time(starts[i] + tts.duration(final[i])), text))
    print("Done: %s (%.1f MB)" % (a.out, os.path.getsize(a.out) / 1e6))


if __name__ == "__main__":
    main()
