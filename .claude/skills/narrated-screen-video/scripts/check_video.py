#!/usr/bin/env python3
"""Verify a finished video the way a careful editor would, without needing to listen to it.

    python check_video.py out/video.mp4 [--work WORKDIR] [--sheet out/contact.jpg] [--frames 5]

Prints duration, resolution, fps, loudness (mean/peak dB) and, with --work, the picture/voice sync
(the first frame must come ~0.2-0.3 s before the first beat). --sheet writes a contact sheet of
evenly spaced frames: LOOK at it (open the image) to confirm every scene, scroll position and
caption is what the storyboard says. Frame grabs are cheap; re-recording is not.
"""
import argparse
import json
import os
import re
import subprocess


def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--work", help="build_video --work folder (for the sync check)")
    ap.add_argument("--sheet", help="write a contact sheet JPEG here")
    ap.add_argument("--frames", type=int, default=24, help="frames on the contact sheet")
    ap.add_argument("--at", type=float, nargs="*", help="also save single frames at these seconds next to --sheet")
    a = ap.parse_args()

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
