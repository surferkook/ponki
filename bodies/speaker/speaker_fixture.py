"""The speaker fixture: plays house-voice clips on this machine (spec 06).

Run on the Pi: python speaker_fixture.py
Settings: SPEAKER_NAME, SPEAKER_DEVICE (an aplay -D device), SPEAKER_LEADIN
(seconds of silence before each clip, for the Bluetooth stream starting),
SPEAKER_KEEPAWAKE (idle seconds before a faint keep-awake noise).
"""
from __future__ import annotations

import asyncio
import io
import os
import random
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "sdk"))

from body import Body  # noqa: E402


def with_lead_in(wav_bytes: bytes, seconds: float) -> bytes:
    """The same WAV with `seconds` of digital silence in front."""
    if seconds <= 0:
        return wav_bytes
    with wave.open(io.BytesIO(wav_bytes), "rb") as reader:
        params = reader.getparams()
        frames = reader.readframes(reader.getnframes())
    silence = b"\x00" * int(params.framerate * seconds) \
        * params.nchannels * params.sampwidth
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setparams(params)
        writer.writeframes(silence + frames)
    return buf.getvalue()


def hub_http(body: Body) -> str:
    url = body.hub_url
    for ws, http in (("wss://", "https://"), ("ws://", "http://")):
        if url.startswith(ws):
            url = http + url[len(ws):]
            break
    return url[: -len("/ws/body")] if url.endswith("/ws/body") else url


def build_body() -> tuple[Body, dict]:
    name = os.environ.get("SPEAKER_NAME") or f"{socket.gethostname()}-speaker"
    body = Body(name, "A speaker the house talks through.", kind="speaker")
    lead_in = float(os.environ.get("SPEAKER_LEADIN", "0") or 0)
    device = os.environ.get("SPEAKER_DEVICE") or ""
    keepawake = float(os.environ.get("SPEAKER_KEEPAWAKE", "0") or 0)
    body.state["lead_in"] = lead_in  # goes out with the manifest and welcome
    state = {"last_played": 0.0}
    play_lock = asyncio.Lock()

    def note_output(text: str) -> None:
        loop = _loop()
        if loop is not None:
            asyncio.run_coroutine_threadsafe(body.set_state(output=text), loop)

    def fetch_and_play(url: str) -> None:
        req = urllib.request.Request(hub_http(body) + url)
        if body.token:
            req.add_header("Authorization", f"Bearer {body.token}")
        with urllib.request.urlopen(req, timeout=20) as resp:
            wav = resp.read()
        wav = with_lead_in(wav, lead_in)
        _aplay(wav, device, state, on_output=note_output)

    @body.action("play", "Play a house-voice clip.", {"url": {"type": "string"}})
    async def play(url: str):
        if not (url or "").startswith("/api/voice/"):
            raise ValueError("a speaker only plays house-voice clips (/api/voice/…)")
        async with play_lock:  # one at a time, in arrival order
            await asyncio.to_thread(fetch_and_play, url)
        return "played"

    async def keep_awake() -> None:
        while True:
            await asyncio.sleep(15)
            if keepawake <= 0:
                continue
            if time.time() - state["last_played"] < keepawake:
                continue
            # half a second of very faint noise: inaudible, but real audio,
            # so the speaker doesn't power itself off when idle
            frames = b"".join(
                int(random.uniform(-10, 10)).to_bytes(2, "little", signed=True)
                for _ in range(24000))
            buf = io.BytesIO()
            with wave.open(buf, "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(48000)
                writer.writeframes(frames)
            try:
                await asyncio.to_thread(_aplay, buf.getvalue(), device, state,
                                        quiet=True)
            except Exception:
                pass

    return body, {"keep_awake": keep_awake}


def _aplay(wav: bytes, device: str, state: dict, quiet: bool = False,
           on_output=None) -> None:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(wav)
        path = tmp.name
    try:
        cmd = ["aplay", "-q"] + (["-D", device] if device else []) + [path]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=70)
        state["last_played"] = time.time()
        if proc.returncode != 0:
            lines = [ln for ln in (proc.stderr or "").splitlines()
                     if "error" in ln.lower()] or (proc.stderr or "").splitlines()
            why = (lines[-1] if lines else "aplay failed")[:120]
            if on_output is not None:
                on_output(f"can't play ({why})")
            if not quiet:
                raise RuntimeError(f"can't play ({why})")
            return
        if not quiet and on_output is not None:
            on_output("ok")
    finally:
        Path(path).unlink(missing_ok=True)


_LOOP = None


def _loop():
    return _LOOP


def main() -> None:
    global _LOOP
    body, extra = build_body()

    async def start() -> None:
        global _LOOP
        _LOOP = asyncio.get_running_loop()
        await body.run_async(extra["keep_awake"]())

    import logging
    logging.basicConfig(format="%(asctime)s %(name)s %(message)s",
                        level=logging.INFO)
    asyncio.run(start())


if __name__ == "__main__":
    main()
