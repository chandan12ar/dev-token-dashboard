#!/usr/bin/env python3
"""Record a narrated screen video of a web app from a storyboard.

    python build_video.py --script script.json --url http://localhost:8000/ --out out/demo.mp4
    python build_video.py --script script.json --serve-cmd "python app.py --port 8799" --url http://127.0.0.1:8799/

What it does: synthesizes the narration for every sentence in the storyboard, drives a headless
browser through it (cursor, scrolling, spotlight ring, captions, title cards) while capturing frames
with exact timestamps, and joins picture and voice into an MP4 with matching SRT captions and chapters.

One-time setup:  pip install playwright && playwright install chromium   (+ a TTS engine, see tts.py)
                 and ffmpeg/ffprobe on your PATH.
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

def start_server(cmd, url, ready_path):
    """Start the app (if --serve-cmd was given) and wait until `url + ready_path` answers."""
    p = None
    if cmd:
        p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(150):
        if p is not None and p.poll() is not None:
            raise SystemExit("app server exited early:\n" + (p.stdout.read() or ""))
        try:
            urllib.request.urlopen(url.rstrip("/") + "/" + ready_path.lstrip("/"), timeout=2).read(64)
            return p
        except Exception:
            time.sleep(0.3)
    if p is not None:
        p.kill()
    raise SystemExit("app did not answer at %s" % url)


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
    ap.add_argument("--script", required=True, help="storyboard JSON (see assets/script.example.json)")
    ap.add_argument("--url", required=True, help="page to record, e.g. http://127.0.0.1:8799/")
    ap.add_argument("--serve-cmd", default=None, help="shell command that starts the app (omit if it is already running)")
    ap.add_argument("--ready-path", default="", help="URL path that answers once the app is up (default: the page itself)")
    ap.add_argument("--init-js", default=None, help="JS file run before the page's own scripts (seed localStorage, flags)")
    ap.add_argument("--config-js", default=None, help="JS file that sets window.V_CONFIG (target resolvers, accent colour)")
    ap.add_argument("--sidebar-px", type=int, default=0, help="width of a fixed left sidebar; the cursor lands exactly there")
    ap.add_argument("--scheme", default="light", choices=("light", "dark"), help="browser colour scheme")
    ap.add_argument("--badge", default="", help="text for a small corner badge when a beat says {BADGE} (e.g. SAMPLE DATA)")
    ap.add_argument("--engine", default="auto", choices=("auto",) + tts.ENGINES, help="text-to-speech engine")
    ap.add_argument("--voice", default=None, help="voice name for the engine")
    ap.add_argument("--rate", default="+0%", help="speaking rate, e.g. -4%% or +8%%")
    ap.add_argument("--out", default="out/video.mp4")
    ap.add_argument("--size", default="1600x900")
    ap.add_argument("--chromium", default=os.environ.get("CHROMIUM_PATH"), help="path to a Chromium/Chrome binary (default: Playwright's)")
    ap.add_argument("--no-captions", action="store_true", help="do not burn captions into the picture")
    ap.add_argument("--only", type=int, default=0, help="debug: only the first N beats")
    ap.add_argument("--work", default=None, help="working directory (kept; default: a temp dir)")
    a = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit("%s not found on PATH" % tool)
    W, H = (int(x) for x in a.size.lower().split("x"))
    work = a.work or tempfile.mkdtemp(prefix="narrated-video-")
    os.makedirs(work, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)

    data_line = ""     # kept so storyboards written for older versions (with {DATA}) still work: it renders as nothing
    with open(a.script, encoding="utf-8") as f:
        script = json.load(f)
    if a.only:
        script["beats"] = script["beats"][:a.only]
    beats = script["beats"]
    rd = lambda p: open(p, encoding="utf-8").read() if p else ""

    engine = tts.pick_engine(a.engine)
    voice = a.voice or tts.DEFAULT_VOICE[engine]
    print("voice: %s / %s%s" % (engine, voice, "   (robotic preview voice - install edge-tts for the final cut)" if engine == "flite" else ""))
    print("app  : %s" % a.url)

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
    srv = start_server(a.serve_cmd, a.url, a.ready_path)
    url = a.url
    try:
        frames_dir = os.path.join(work, "frames")
        shutil.rmtree(frames_dir, ignore_errors=True)
        print("recording...")
        rec = asyncio.run(record(script, durations, url, frames_dir, chromium=a.chromium, size=(W, H),
                                 init_js=rd(a.init_js), config_js=rd(a.config_js), sidebar_px=a.sidebar_px,
                                 color_scheme=a.scheme, captions=not a.no_captions, badge=a.badge, data_line=data_line))
    finally:
        if srv is not None:
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
