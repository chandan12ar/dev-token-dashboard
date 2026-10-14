"""Text-to-speech adapters for the walkthrough video.

Every engine turns one sentence of text into a mono 24 kHz WAV file.

  edge    Microsoft neural voices via the `edge-tts` package (pip install edge-tts).
          Best quality, free, needs internet. Default when installed.
  sapi    Windows built-in voices (System.Speech via PowerShell). Offline.
  say     macOS `say`. Offline.
  kokoro  Kokoro-82M open neural voice, offline on CPU (pip install kokoro-onnx; set
          KOKORO_MODEL and KOKORO_VOICES). Natural sounding, no network needed.
  flite   ffmpeg's bundled flite filter. Offline but robotic: preview quality only.

`auto` picks the first engine that works, in the order above.
"""
import asyncio
import os
import shutil
import subprocess
import sys

ENGINES = ("edge", "kokoro", "sapi", "say", "flite")
DEFAULT_VOICE = {"edge": "en-US-AriaNeural", "kokoro": "af_heart", "sapi": "", "say": "Samantha", "flite": "slt"}


def _ffmpeg_to_wav(src, dst):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
                    "-ar", "24000", "-ac", "1", "-sample_fmt", "s16", dst], check=True)


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def _edge(text, out, voice, rate):
    import edge_tts

    tmp = out + ".mp3"

    async def go():
        await edge_tts.Communicate(text, voice, rate=rate).save(tmp)

    asyncio.run(go())
    _ffmpeg_to_wav(tmp, out)
    os.remove(tmp)


def _sapi(text, out, voice, rate):
    tmp = out + ".src.wav"
    ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          + ("$s.SelectVoice('%s'); " % voice if voice else "")
          + "$s.Rate = 0; $s.SetOutputToWaveFile($env:TTS_OUT); $s.Speak($env:TTS_TEXT); $s.Dispose()")
    env = dict(os.environ, TTS_OUT=tmp, TTS_TEXT=text)
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, env=env)
    _ffmpeg_to_wav(tmp, out)
    os.remove(tmp)


def _say(text, out, voice, rate):
    tmp = out + ".aiff"
    subprocess.run(["say", "-v", voice, "-o", tmp, text], check=True)
    _ffmpeg_to_wav(tmp, out)
    os.remove(tmp)


_KOKORO = None


def _kokoro(text, out, voice, rate):
    """Kokoro-82M, a small open (Apache-2.0) neural voice that runs offline on CPU.
    KOKORO_MODEL = path to model_quantized.onnx, KOKORO_VOICES = folder of <voice>.bin style files
    (the kokoro-js npm package ships them). speed comes from `rate` like "+5%"."""
    global _KOKORO
    import wave

    import numpy as np
    from kokoro_onnx import Kokoro

    if _KOKORO is None:
        vdir = os.environ["KOKORO_VOICES"]
        npz = os.path.join(vdir, "_voices.npz")
        if not os.path.exists(npz):
            np.savez(npz, **{f[:-4]: np.fromfile(os.path.join(vdir, f), dtype=np.float32).reshape(-1, 1, 256)
                             for f in os.listdir(vdir) if f.endswith(".bin")})
        _KOKORO = Kokoro(os.environ["KOKORO_MODEL"], npz)
    speed = 1.0 + float(str(rate).rstrip("%") or 0) / 100.0
    audio, sr = _KOKORO.create(text, voice=voice, speed=speed, lang="en-us")
    tmp = out + ".src.wav"
    with wave.open(tmp, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    _ffmpeg_to_wav(tmp, out)
    os.remove(tmp)


def _flite(text, out, voice, rate):
    txt = out + ".txt"
    with open(txt, "w", encoding="utf-8") as f:
        f.write(text)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "flite=textfile=%s:voice=%s" % (txt.replace("\\", "/").replace(":", "\\:"), voice),
                    "-ar", "24000", "-ac", "1", "-sample_fmt", "s16", out], check=True)
    os.remove(txt)


_FN = {"edge": _edge, "kokoro": _kokoro, "sapi": _sapi, "say": _say, "flite": _flite}


def _available(engine):
    if engine == "edge":
        try:
            import edge_tts  # noqa: F401
            return True
        except ImportError:
            return False
    if engine == "kokoro":
        try:
            import kokoro_onnx  # noqa: F401
        except ImportError:
            return False
        return bool(os.environ.get("KOKORO_MODEL") and os.environ.get("KOKORO_VOICES"))
    if engine == "sapi":
        return sys.platform.startswith("win") and shutil.which("powershell") is not None
    if engine == "say":
        return shutil.which("say") is not None
    if engine == "flite":
        r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
        return "flite" in r.stdout
    return False


def pick_engine(requested="auto"):
    if requested != "auto":
        if not _available(requested):
            raise SystemExit("TTS engine %r is not available on this machine" % requested)
        return requested
    for e in ENGINES:
        if _available(e):
            return e
    raise SystemExit("no TTS engine available (install edge-tts: pip install edge-tts)")


def synth(text, out, engine, voice=None, rate="+0%"):
    """Synthesize `text` to `out` (WAV) and return its duration in seconds.

    A failure is raised, never papered over: silently switching voice mid-video would be worse
    than stopping."""
    _FN[engine](text, out, voice or DEFAULT_VOICE[engine], rate)
    return duration(out)
