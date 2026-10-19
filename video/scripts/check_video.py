#!/usr/bin/env python3
"""Verify a finished video the way a careful editor would, without needing to listen to it.

    python check_video.py out/video.mp4 [--work WORKDIR] [--script script.json] [--sheet out/contact.jpg] [--frames 24]

Prints duration, resolution, fps, loudness (mean/peak dB) and, with --work, the picture/voice sync
(the first frame must come ~0.2-0.3 s before the first beat). --sheet writes a contact sheet of
evenly spaced frames: LOOK at it (open the image) to confirm every scene, scroll position and
caption is what the storyboard says. Frame grabs are cheap; re-recording is not.

With --work and --script it also checks pacing: words per minute per beat (130-170 is comfortable), very long
sentences, and stretches where the picture does not move for 3 s or more while the voice talks (add a
spotlight, scroll or hover there). Exit code 0 always: these are findings to act on, not failures.
"""
import argparse
import json
import os
import re
import subprocess


def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def pacing(a, dur):
    import json as _j
    rec = _j.load(open(os.path.join(a.work, "rec.json")))
    beats = _j.load(open(a.script, encoding="utf-8"))["beats"]
    t0 = rec["frames"][0][0]
    starts = [(max(0.0, b["start"] - t0), b["dur"], b["i"]) for b in rec["beats"]]
    print("\npacing")
    issues = 0
    tot_w = tot_s = 0.0
    for st, d, i in starts:
        words = len(beats[i]["say"].split())
        speech = max(d - 0.85, 0.5)                        # beat = clip + 0.30 lead + 0.55 tail
        wpm = words / speech * 60
        tot_w += words; tot_s += speech
        flag = []
        if wpm > 185: flag.append("fast (%d wpm; comfortable is 140-170: lower --rate or shorten)" % wpm)
        if wpm < 95 and words > 6: flag.append("slow (%d wpm)" % wpm)
        if words > 40: flag.append("long sentence (%d words)" % words)
        if flag:
            issues += 1
            print("  beat %2d @ %s  %s  | %s" % (i + 1, "%d:%02d" % (st // 60, st % 60), ", ".join(flag), beats[i]["say"][:60]))
    print("  overall %d wpm over %d beats" % (tot_w / max(tot_s, 1) * 60, len(starts)))
    out = run("ffmpeg", "-hide_banner", "-i", a.video, "-vf", "freezedetect=n=-40dB:d=%s" % a.freeze, "-an", "-f", "null", "-").stderr
    import re as _re
    starts_f = [float(x) for x in _re.findall(r"freeze_start: ([\d.]+)", out)]
    durs = [float(x) for x in _re.findall(r"freeze_duration: ([\d.]+)", out)]
    for fs, fd in zip(starts_f, durs):
        b = next((i for st, d, i in starts if st <= fs < st + d), None)
        if fs > dur - 3:                                    # the held outro card is meant to be still
            continue
        stage_beat = b is not None and any("stage" in x and x["stage"] for x in beats[b].get("do", []))
        if stage_beat or fs < starts[0][0] + 1.0:           # title/problem cards and the opening card are meant to hold
            continue
        issues += 1
        print("  almost no change on screen for %.1f s at %d:%02d (beat %s; cursor movement alone does not count): add a spotlight, scroll or hover" % (fd, fs // 60, fs % 60, (b + 1) if b is not None else "?"))
    print("  pacing: %s" % ("nothing to fix" if not issues else "%d thing(s) to look at" % issues))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--work", help="build_video --work folder (for the sync check)")
    ap.add_argument("--script", help="storyboard JSON (enables the pacing check together with --work)")
    ap.add_argument("--freeze", type=float, default=3.0, help="seconds of unchanged picture to report")
    ap.add_argument("--sheet", help="write a contact sheet JPEG here")
    ap.add_argument("--frames", type=int, default=24, help="frames on the contact sheet")
    ap.add_argument("--at", type=float, nargs="*", help="also save single frames at these seconds next to --sheet")
    a = ap.parse_args()

    if not os.path.exists(a.video):
        raise SystemExit("no such video: %s (did the build finish?)" % a.video)
    p = json.loads(run("ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height,r_frame_rate",
                       "-of", "json", a.video).stdout)
    dur = float(p["format"]["duration"])
    v = next(s for s in p["streams"] if s["codec_type"] == "video")
    print("duration %d:%02d  %sx%s  %s fps  audio=%s" % (dur // 60, dur % 60, v["width"], v["height"], v["r_frame_rate"],
                                                         any(s["codec_type"] == "audio" for s in p["streams"])))
    vol = run("ffmpeg", "-hide_banner", "-i", a.video, "-af", "volumedetect", "-vn", "-f", "null", "-").stderr
    m = re.search(r"mean_volume: (-?[\d.]+) dB", vol); x = re.search(r"max_volume: (-?[\d.]+) dB", vol)
    if m and x:
        mean, peak = float(m.group(1)), float(x.group(1))
        print("loudness mean %.1f dB, peak %.1f dB  %s" % (mean, peak, "OK" if -22 < mean < -12 and peak <= -0.3 else "<- check levels (want mean ~-16, peak <= -1)"))
    if a.work:
        rec = json.load(open(os.path.join(a.work, "rec.json")))
        skew = rec["beats"][0]["start"] - rec["frames"][0][0]
        print("first frame is %.2f s before the first beat  %s" % (skew, "OK" if 0.1 < skew < 0.6 else "<- sync suspect"))
        print("beats: %d" % len(rec["beats"]))
    if a.work and a.script:
        pacing(a, dur)
    if a.sheet:
        n = a.frames
        cols = 4
        rows = (n + cols - 1) // cols
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", a.video, "-vf",
                        "fps=%f,scale=640:-1,tile=%dx%d" % (n / dur, cols, rows), "-frames:v", "1", "-q:v", "3", a.sheet], check=True)
        print("contact sheet:", a.sheet)
        for t in a.at or []:
            out = "%s.%ds.jpg" % (os.path.splitext(a.sheet)[0], int(t))
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(t), "-i", a.video, "-frames:v", "1", "-q:v", "2", out], check=True)
            print("frame:", out)


if __name__ == "__main__":
    main()
