#!/usr/bin/env python3
"""Record a narrated walkthrough video of AI Fluency Insights.

    python video/build_video.py                    # real logs from ~/.claude/projects
    python video/build_video.py --privacy          # hide session titles, prompts and file names
    python video/build_video.py --sample           # synthetic data, for previewing on a machine with no logs

What it does: starts the dashboard on your logs, synthesizes the narration for every sentence in
walkthrough.json, drives a headless browser through the tabs (cursor, scrolling, spotlight, captions),
and joins picture and voice into an MP4 with matching SRT captions and chapter timestamps.

One-time setup:  pip install playwright edge-tts && playwright install chromium
                 and ffmpeg on your PATH.
"""
import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tts  # noqa: E402
from record import LEAD, record  # noqa: E402

FPS = 30
DATA_REAL = "Everything you are about to see is running on my own real Claude Code logs."
DATA_SAMPLE = "This preview runs on sample data, not real usage."


def default_logs():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "projects")


def start_server(logs, port):
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "serve_for_recording.py"), "--dir", logs, "--port", str(port)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = "http://127.0.0.1:%d/" % port
    for _ in range(100):
        if p.poll() is not None:
            raise SystemExit("dashboard server exited early:\n" + (p.stdout.read() or ""))
        try:
            urllib.request.urlopen(url + "api/stats", timeout=2).read(64)
            return p, url
        except Exception:
            time.sleep(0.3)
    p.kill()
    raise SystemExit("dashboard server did not start")


def srt_time(s):
    ms = int(round(s * 1000))
    return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)


def mix_narration(path, total_s, clips):
    """clips: [(offset_s, wav_path)] -> one 24 kHz mono wav, `total_s` long."""
    rate = 24000
    buf = bytearray(int(total_s * rate) * 2)
    for off, wp in clips:
        with wave.open(wp, "rb") as w:
            assert w.getframerate() == rate and w.getnchannels() == 1 and w.getsampwidth() == 2, wp
            data = w.readframes(w.getnframes())
        i = int(off * rate) * 2
        buf[i:i + len(data)] = data[:max(0, len(buf) - i)]
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(bytes(buf))


def frames_to_list(rec, list_path):
    frames = rec["frames"]
    t0 = frames[0][0]
    with open(list_path, "w") as f:
        f.write("ffconcat version 1.0\n")
        for i, (ts, path) in enumerate(frames):
            nxt = frames[i + 1][0] if i + 1 < len(frames) else rec["t_end"]
            d = nxt - ts
            if d <= 0.0005:
                continue
            f.write("file '%s'\nduration %.4f\n" % (path.replace("\\", "/"), d))
        f.write("file '%s'\n" % frames[-1][1].replace("\\", "/"))
    return t0, rec["t_end"] - t0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=default_logs(), help="Claude projects log dir (default: ~/.claude/projects)")
    ap.add_argument("--sample", action="store_true", help="use generated sample data instead of real logs (preview only)")
    ap.add_argument("--privacy", action="store_true", help="turn on the dashboard's Privacy mode (hides titles, prompts, file names)")
    ap.add_argument("--engine", default="auto", choices=("auto",) + tts.ENGINES, help="text-to-speech engine")
    ap.add_argument("--voice", default=None, help="voice name for the engine (edge default: en-US-AriaNeural)")
    ap.add_argument("--rate", default="-4%", help="edge-tts speaking rate, e.g. -4%% or +8%%")
    ap.add_argument("--out", default=os.path.join(HERE, "out", "ai-fluency-insights-walkthrough.mp4"))
    ap.add_argument("--size", default="1600x900")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--chromium", default=os.environ.get("CHROMIUM_PATH"), help="path to a Chromium/Chrome binary (default: Playwright's)")
    ap.add_argument("--no-captions", action="store_true", help="do not burn captions into the picture")
    ap.add_argument("--only", type=int, default=0, help="debug: only the first N beats")
    ap.add_argument("--data-line", default=None, help="override the spoken line about the data source")
    ap.add_argument("--work", default=None, help="working directory (kept; default: a temp dir)")
    a = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit("%s not found on PATH" % tool)
    W, H = (int(x) for x in a.size.lower().split("x"))
    work = a.work or tempfile.mkdtemp(prefix="afi-video-")
    os.makedirs(work, exist_ok=True)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)

    logs = a.dir
    if a.sample:
        logs = os.path.join(work, "sample_logs")
        if not os.path.isdir(logs):
            subprocess.run([sys.executable, os.path.join(HERE, "make_sample_logs.py"), logs], check=True)
    elif not os.path.isdir(logs):
        sys.exit("log directory not found: %s  (use --dir, or --sample for a preview)" % logs)
    data_line = a.data_line or (DATA_SAMPLE if a.sample else DATA_REAL)
    badge = "SAMPLE DATA - PREVIEW" if a.sample else ""

    with open(os.path.join(HERE, "walkthrough.json"), encoding="utf-8") as f:
        script = json.load(f)
    if a.only:
        script["beats"] = script["beats"][:a.only]
    beats = script["beats"]

    engine = tts.pick_engine(a.engine)
    voice = a.voice or tts.DEFAULT_VOICE[engine]
    print("voice: %s / %s%s" % (engine, voice, "   (robotic preview voice - install edge-tts for the final cut)" if engine == "flite" else ""))
    print("data : %s" % ("SAMPLE (synthetic)" if a.sample else logs))

    # 1. narration, one WAV per sentence (cached by content so re-runs are quick)
    wav_dir = os.path.join(work, "wav"); os.makedirs(wav_dir, exist_ok=True)
    durations, wavs = [], []
    print("synthesizing narration...")
    for i, b in enumerate(beats):
        text = b["say"].replace("{DATA}", data_line).strip()
        key = hashlib.sha1(("%s|%s|%s|%s" % (engine, voice, a.rate, text)).encode()).hexdigest()[:12]
        wp = os.path.join(wav_dir, "%03d-%s.wav" % (i, key))
        if not os.path.exists(wp):
            tts.synth(text, wp, engine, voice, a.rate)
        durations.append(tts.duration(wp)); wavs.append(wp)
    print("  narration: %.0f s of speech in %d lines" % (sum(durations), len(beats)))

    # 2. record the screen
    srv, url = start_server(logs, a.port)
    try:
        frames_dir = os.path.join(work, "frames")
        shutil.rmtree(frames_dir, ignore_errors=True)
        print("recording...")
        rec = asyncio.run(record(script, durations, url, frames_dir, chromium=a.chromium, size=(W, H),
                                 privacy=a.privacy, captions=not a.no_captions, badge=badge, data_line=data_line))
    finally:
        srv.terminate()

    # 3. assemble
    list_path = os.path.join(work, "frames.txt")
    t0, total = frames_to_list(rec, list_path)
    print("recorded %d frames, %.1f s  (first frame is %.2f s before the first beat; expect ~0.3)"
          % (len(rec["frames"]), total, rec["beats"][0]["start"] - t0))
    with open(os.path.join(work, "rec.json"), "w") as f:
        json.dump(rec, f)
    narr = os.path.join(work, "narration.wav")
    mix_narration(narr, total + 0.5, [(max(0.0, bt["start"] - t0 + LEAD), wavs[bt["i"]]) for bt in rec["beats"]])
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
                    "-i", narr, "-vf", "fps=%d,format=yuv420p" % FPS, "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                    "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                    "-shortest", a.out], check=True)

    # 4. captions + chapters + script
    base = os.path.splitext(a.out)[0]
    with open(base + ".srt", "w", encoding="utf-8") as f, open(base + ".chapters.txt", "w", encoding="utf-8") as c, \
            open(base + ".script.md", "w", encoding="utf-8") as s:
        s.write("# Narration script\n\n")
        for n, bt in enumerate(rec["beats"], 1):
            b = beats[bt["i"]]
            text = (b.get("cap") or b["say"]).replace("{DATA}", data_line).strip()
            start = max(0.0, bt["start"] - t0 + LEAD)
            f.write("%d\n%s --> %s\n%s\n\n" % (n, srt_time(start), srt_time(start + durations[bt["i"]]), text))
            if b.get("chapter"):
                m = int(start // 60)
                c.write("%d:%02d %s\n" % (m, int(start % 60), b["chapter"]))
                s.write("\n## %s\n\n" % b["chapter"])
            s.write(text + "\n\n")
    print("\nDone: %s  (%.1f MB, %d:%02d)" % (a.out, os.path.getsize(a.out) / 1e6, total // 60, total % 60))
    print("      %s.srt / .chapters.txt / .script.md" % base)
    print("      working files kept in %s" % work)


if __name__ == "__main__":
    main()
