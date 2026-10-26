"""Manual check of the house voice (spec 06): every voice, three pitches.

    python tools/voice_check.py [base-url]

For every voice at pitch 0.7, 1.0 and 1.6: the clip must be 48 kHz mono
16-bit, peak about 0.7, and last within ±10% of the pitch-1.0 clip at the
same speed (pitch moves, pace stays).
"""
from __future__ import annotations

import io
import json
import struct
import sys
import urllib.request
import wave

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8202"
TEXT = "Hello, I'm a robot in this house, and this is my voice."


def speak(voice: str, pitch: float) -> tuple[float, float]:
    req = urllib.request.Request(
        f"{BASE}/speak",
        data=json.dumps({"text": TEXT, "voice": voice, "pitch": pitch,
                         "speed": 1.0}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
    with wave.open(io.BytesIO(raw), "rb") as reader:
        assert reader.getframerate() == 48000, reader.getframerate()
        assert reader.getnchannels() == 1 and reader.getsampwidth() == 2
        frames = reader.readframes(reader.getnframes())
        duration = reader.getnframes() / reader.getframerate()
    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
    peak = max(abs(s) for s in samples) / 32767
    return duration, peak


def main() -> None:
    voices = json.loads(urllib.request.urlopen(f"{BASE}/voices").read())
    failures = 0
    for voice in voices:
        durations = {}
        for pitch in (0.7, 1.0, 1.6):
            duration, peak = speak(voice, pitch)
            durations[pitch] = duration
            ok = 0.6 < peak < 0.75
            print(f"{voice:24} pitch {pitch}: {duration:5.2f}s peak {peak:.2f}"
                  f"{'' if ok else '  <-- PEAK OFF'}")
            failures += 0 if ok else 1
        base = durations[1.0]
        for pitch in (0.7, 1.6):
            drift = abs(durations[pitch] - base) / base
            if drift > 0.10:
                print(f"{voice:24} pitch {pitch}: pace drifted {drift:.0%}")
                failures += 1
    print("OK" if not failures else f"{failures} problems")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
