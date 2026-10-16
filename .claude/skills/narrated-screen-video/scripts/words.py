"""Estimate when each word of a narrated sentence is spoken, for karaoke-style caption highlighting.

No TTS engine here reports word times (edge-tts can, kokoro cannot), so this works for all of them:
1. find where speech really starts and ends inside the WAV (energy gate, skipping leading/trailing silence);
2. spread the words over that window in proportion to how long each takes to say (letters + a pause
   weight after commas and sentence ends).
It is an estimate, typically within ~100 ms per word, which is plenty for a highlight that reads as "in sync".

    python words.py "Some caption text." clip.wav        # prints [[start, end], ...] in seconds
"""
import array
import json
import re
import sys
import wave


def speech_window(wav_path, hop_ms=10):
    """(start_s, end_s) of the audible part of a mono s16 WAV."""
    with wave.open(wav_path, "rb") as w:
        sr, n = w.getframerate(), w.getnframes()
        pcm = array.array("h"); pcm.frombytes(w.readframes(n))
    hop = max(1, int(sr * hop_ms / 1000))
    env = []
    for i in range(0, len(pcm) - hop, hop):
        seg = pcm[i:i + hop]
        env.append((sum(x * x for x in seg) / len(seg)) ** 0.5)
    if not env:
        return 0.0, n / float(sr)
    gate = max(max(env) * 0.04, 60.0)
    on = next((i for i, e in enumerate(env) if e > gate), 0)
    off = next((i for i in range(len(env) - 1, -1, -1) if env[i] > gate), len(env) - 1)
    return on * hop_ms / 1000.0, (off + 1) * hop_ms / 1000.0


def _weight(word):
    core = re.sub(r"[^A-Za-z0-9]", "", word)
    w = 1.5 + len(core) * 1.0                        # every word has a floor, longer words take longer
    w += len(re.findall(r"[aeiouy]+", core.lower())) * 0.8   # syllable-ish
    return w


def _pause_after(word):
    if re.search(r"[.!?][\"')\]]*$", word):
        return 4.0
    if re.search(r"[,;:—–-][\"')\]]*$", word) and len(word) > 1:
        return 2.2
    return 0.0


def word_timings(text, wav_path):
    """[[start, end], ...] seconds from the start of the clip, one entry per whitespace-separated word of `text`."""
    words = text.split()
    if not words:
        return []
    t0, t1 = speech_window(wav_path)
    ws = [_weight(w) for w in words]
    ps = [_pause_after(w) for w in words]
    ps[-1] = 0.0
    total = sum(ws) + sum(ps)
    unit = max(t1 - t0, 0.1) / total
    out, t = [], t0
    for w, p in zip(ws, ps):
        a, b = t, t + w * unit
        out.append([round(a, 3), round(b, 3)])
        t = b + p * unit
    return out


if __name__ == "__main__":
    print(json.dumps(word_timings(sys.argv[1], sys.argv[2])))
