"""The mic fixture: the house's ears on one machine (spec 06).

Run on the Pi: python mic_fixture.py
Settings: MIC_NAME, MIC_SOURCE (`alsa:<card id>` or `onvif:<ip>`),
MIC_LATENCY (how far the source lags the room: 0.1 local, 0.5 camera).

Everything is resampled to 16 kHz mono s16 on arrival. Push-to-talk has
0.4 s of pre-roll; always-on cuts sentences with the SentenceCutter below
(a pure function of per-frame loudness, so it can be tested with numbers).
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import os
import socket
import sys
import threading
import time
import wave
from collections import deque
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent / "sdk"))

from body import Body  # noqa: E402

RATE = 16000
FRAME_SAMPLES = 480  # 30 ms at 16 kHz
KEEP_SECONDS = 40.0


class SentenceCutter:
    """Sentence boundaries from per-frame RMS values only.

    feed(rms) -> {"start": frame, "end": frame} (inclusive) when a sentence
    ends, else None. Frames are 30 ms. The noise floor follows the room on
    non-speech frames, even while listening is off.
    """

    FRAME_S = 0.03

    def __init__(self) -> None:
        self.floor = 20.0
        self.listening = False
        self.idx = 0
        self._history: deque[int] = deque(maxlen=10)  # 0.3 s
        self._speech_run = 0
        self._in_sentence = False
        self._start = 0
        self._speech_start = 0
        self._quiet = 0

    def set_listening(self, on: bool) -> None:
        self.listening = bool(on)
        self._history.clear()
        self._speech_run = 0
        self._in_sentence = False
        self._quiet = 0

    def _frames(self, seconds: float) -> int:
        return max(1, int(round(seconds / self.FRAME_S)))

    def feed(self, rms: float) -> dict | None:
        speech = rms > max(4 * self.floor, 60)
        if not speech:
            self.floor = 0.98 * self.floor + 0.02 * rms
        event = None
        if self.listening:
            if not self._in_sentence:
                if speech:
                    if self._speech_run == 0:
                        self._speech_start = self.idx
                    self._speech_run += 1
                else:
                    self._history.append(self.idx)  # pre-speech history only
                    self._speech_run = 0
                if self._speech_run >= self._frames(0.25):
                    self._in_sentence = True
                    self._start = self._history[0] if self._history \
                        else self._speech_start
                    self._quiet = 0
            else:
                self._quiet = 0 if speech else self._quiet + 1
                length = self.idx - self._start + 1
                if self._quiet >= self._frames(0.8) \
                        or length >= self._frames(30.0):
                    end = self.idx - self._quiet
                    if end - self._speech_start + 1 >= self._frames(0.6):
                        event = {"start": self._start, "end": end}
                    self._in_sentence = False
                    self._history.clear()
                    self._speech_run = 0
                    self._quiet = 0
        else:
            self._history.clear()
            self._speech_run = 0
            self._in_sentence = False
        self.idx += 1
        return event


def wrap_wav(samples: np.ndarray) -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(RATE)
        writer.writeframes(samples.astype("<i2").tobytes())
    return base64.b64encode(buf.getvalue()).decode("ascii")


class MicSource(threading.Thread):
    """Reads the source forever, handing 30 ms frames to on_frame."""

    def __init__(self, source: str, on_frame, on_status) -> None:
        super().__init__(daemon=True)
        self.source = source
        self.on_frame = on_frame
        self.on_status = on_status

    def _open(self):
        import av

        if self.source.startswith("alsa:"):
            card = self.source[len("alsa:"):]
            container = av.open(f"hw:CARD={card}", format="alsa")
        elif self.source.startswith("onvif:"):
            ip = self.source[len("onvif:"):]
            secret = _camera_secret(ip)
            user = urllib_quote(secret.get("username", ""))
            password = urllib_quote(secret.get("password", ""))
            url = f"rtsp://{user}:{password}@{ip}:554/stream1"
            container = av.open(url, format="rtsp",
                                options={"rtsp_transport": "tcp",
                                         "timeout": "10000000"})
        else:
            container = av.open(self.source)
        return container

    def run(self) -> None:
        import av

        while True:
            try:
                container = self._open()
                stream = container.streams.audio[0]
                self.on_status(
                    f"hearing ({stream.codec_context.format.name}, "
                    f"{stream.codec_context.sample_rate} Hz)")
                resampler = av.AudioResampler(format="s16", layout="mono",
                                              rate=RATE)
                pending = np.zeros(0, dtype=np.int16)
                for frame in container.decode(stream):
                    for out in resampler.resample(frame):
                        samples = out.to_ndarray().reshape(-1).astype(np.int16)
                        pending = np.concatenate([pending, samples])
                        while len(pending) >= FRAME_SAMPLES:
                            chunk, pending = pending[:FRAME_SAMPLES], \
                                pending[FRAME_SAMPLES:]
                            rms = float(np.sqrt(np.mean(
                                chunk.astype(np.float64) ** 2))) if len(chunk) \
                                else 0.0
                            self.on_frame(chunk.copy(),
                                          time.time(), rms)
                container.close()
            except Exception as exc:
                self.on_status(f"can't hear: {type(exc).__name__}: {exc}"[:120])
                time.sleep(3)


def urllib_quote(text: str) -> str:
    import urllib.parse

    return urllib.parse.quote(text, safe="")


def _camera_secret(ip: str) -> dict:
    folder = Path(os.environ.get("HOUSE_SECRETS_DIR", "data/secrets"))
    path = folder / f"onvif:{ip}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def build_body() -> tuple[Body, MicSource, dict]:
    name = os.environ.get("MIC_NAME") or f"{socket.gethostname()}-mic"
    body = Body(name, "A microphone the house listens through.", kind="mic")
    source = os.environ.get("MIC_SOURCE", "alsa:BRIO")
    latency = float(os.environ.get("MIC_LATENCY", "0.1") or 0.1)
    if source.startswith("onvif:") and "MIC_LATENCY" not in os.environ:
        latency = 0.5
    cutter = SentenceCutter()
    shared: dict = {"loop": None, "status": "starting", "level": 0.0,
                    "t0": None}
    chunks: deque[tuple[float, np.ndarray]] = deque()  # (arrival, samples)
    frames: deque[tuple[int, np.ndarray]] = deque()  # (frame idx, samples)
    lock = threading.Lock()

    def on_frame(samples: np.ndarray, when: float, rms: float) -> None:
        shared["level"] = rms
        event = cutter.feed(rms)
        with lock:
            chunks.append((when, samples))
            frames.append((cutter.idx - 1, samples))
            while chunks and chunks[0][0] < when - KEEP_SECONDS - 5:
                chunks.popleft()
            while frames and frames[0][0] < cutter.idx - int(
                    (KEEP_SECONDS + 5) / SentenceCutter.FRAME_S):
                frames.popleft()
        if event is not None:
            with lock:
                picked = [s for idx, s in frames
                          if event["start"] <= idx <= event["end"]]
            if picked:
                audio = np.concatenate(picked)
                loop = shared["loop"]
                if loop is not None:
                    asyncio.run_coroutine_threadsafe(
                        body._send({"type": "input",
                                    "audio": wrap_wav(audio),
                                    "seconds": round(len(audio) / RATE, 2)}),
                        loop)

    def on_status(text: str) -> None:
        shared["status"] = text

    thread = MicSource(source, on_frame, on_status)

    @body.action("start", "Start push-to-talk listening.", {})
    async def start():
        shared["t0"] = time.time() + latency - 0.4  # pre-roll
        return "listening"

    @body.action("stop", "Stop push-to-talk and return what was said.", {})
    async def stop():
        t0 = shared["t0"]
        if t0 is None:
            raise ValueError("push-to-talk wasn't started")
        shared["t0"] = None
        await asyncio.sleep(latency + 0.25)  # the tail is still arriving
        end = min(time.time(), t0 + 30)
        with lock:
            picked = [s for when, s in chunks if t0 <= when <= end]
        audio = np.concatenate(picked) if picked else np.zeros(0, np.int16)
        return {"audio": wrap_wav(audio),
                "seconds": round(len(audio) / RATE, 2)}

    @body.action("listen", "Switch always-on listening.",
                 {"on": {"type": "boolean"}})
    async def listen(on: bool = False):
        cutter.set_listening(bool(on))
        return "always listening" if on else "push-to-talk only"

    async def state_loop() -> None:
        shared["loop"] = asyncio.get_running_loop()
        thread.start()
        while True:
            await body.set_state(
                hearing=shared["status"], listening=cutter.listening,
                level=int(shared["level"]), floor=round(cutter.floor, 1))
            await asyncio.sleep(5)

    return body, thread, {"state_loop": state_loop}


def main() -> None:
    import logging

    logging.basicConfig(format="%(asctime)s %(name)s %(message)s",
                        level=logging.INFO)
    body, _thread, extra = build_body()
    body.run(extra["state_loop"]())


if __name__ == "__main__":
    main()
