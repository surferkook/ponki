"""The house's ears: speech to text (spec 06).

    uvicorn ears_server:app --host 127.0.0.1 --port 8203

Muse Voice Transcribe 1.0 (Meta's ASR API, not OpenAI's shape), and only
Muse: every model Ponki calls at runtime is a Meta model, so there is no
local fallback. The mic cuts the clip; this service only transcribes it.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import time
import wave

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException

LANGUAGE = os.environ.get("EARS_LANGUAGE", "en")
MUSE_URL = "https://api.meta.ai/v1/asr/transcribe"
LANGUAGES = {"en": "English", "nl": "Dutch", "de": "German", "fr": "French",
             "es": "Spanish"}

app = FastAPI(title="ponkihouse ears")


# -- WAV helpers (pure) ---------------------------------------------------------

def decode_wav(raw: bytes):
    """(mono int16 samples, rate). Only 16-bit WAV is accepted."""
    try:
        with wave.open(io.BytesIO(raw), "rb") as reader:
            if reader.getsampwidth() != 2:
                raise ValueError("only 16-bit audio is accepted")
            rate = reader.getframerate()
            channels = reader.getnchannels()
            frames = reader.readframes(reader.getnframes())
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"can't read that audio: {exc}")
    samples = np.frombuffer(frames, dtype="<i2").astype(np.int32)
    if channels > 1:
        samples = samples[: len(samples) // channels * channels] \
            .reshape(-1, channels).mean(axis=1)
    return samples.astype(np.int16), rate


def wrap_wav(samples: np.ndarray, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(samples.astype("<i2").tobytes())
    return buf.getvalue()


def resample(samples: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or len(samples) == 0:
        return samples
    count = int(len(samples) * dst / src)
    positions = np.linspace(0, len(samples) - 1, count)
    return np.interp(positions, np.arange(len(samples)),
                     samples.astype(np.float64)).astype(np.int16)


def prepare_for_muse(raw: bytes) -> bytes:
    """Mono 16-bit at 16/24 kHz passes unchanged; else 24 kHz mono."""
    samples, rate = decode_wav(raw)
    # decode_wav already mixed to mono; re-wrap check needs the channel count
    with wave.open(io.BytesIO(raw), "rb") as reader:
        channels = reader.getnchannels()
    if channels == 1 and rate in (16000, 24000):
        return raw
    return wrap_wav(resample(samples, rate, 24000), 24000)


def muse_request(keywords: list[str]) -> dict:
    return {"model": "muse-voice-transcribe-1.0", "audioEncoding": "WAV",
            "mode": "PUSH_TO_TALK",
            "languageBias": [LANGUAGES.get(LANGUAGE, "English")],
            "keywords": list(keywords or [])}


# -- Muse path --------------------------------------------------------------------

async def _post_muse(wav_bytes: bytes, keywords: list[str], session_id: str):
    key = os.environ.get("META_API_KEY", "")
    files = {
        "request": (None, json.dumps(muse_request(keywords)), "application/json"),
        "audio": ("audio.wav", wav_bytes, "audio/wav"),
    }
    async with httpx.AsyncClient(timeout=15) as client:
        return await client.post(
            f"{MUSE_URL}?sessionId={session_id}", files=files,
            headers={"Authorization": f"Bearer {key}",
                     "Accept": "application/json"})


async def muse_transcribe(wav_bytes: bytes, keywords: list[str],
                          post=None) -> dict:
    """Raises MuseError(reason) on any failure."""
    post = post or _post_muse
    session_id = f"house-{int(time.time() * 1000)}"
    prepared = prepare_for_muse(wav_bytes)
    reason = ""
    for attempt in range(2):
        try:
            resp = await post(prepared, keywords, session_id)
        except httpx.HTTPError as exc:
            raise MuseError(f"Muse Voice Transcribe: {type(exc).__name__}")
        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                raise MuseError("Muse Voice Transcribe: a bad answer")
            return {"text": (data.get("transcript") or "").strip(),
                    "language": LANGUAGE,
                    "billed_seconds": int(data.get("audioDurationMs", 0)) // 1000,
                    "session_id": data.get("sessionId") or session_id}
        if resp.status_code in (429, 500) and attempt == 0:
            reason = (f"Muse Voice Transcribe: {resp.status_code} "
                      f"{'rate limited' if resp.status_code == 429 else 'failed'}")
            await asyncio.sleep(1)
            continue
        raise MuseError(f"Muse Voice Transcribe: {resp.status_code} "
                          f"{resp.text[:120]}")
    raise MuseError(reason or "Muse Voice Transcribe: failed")


class MuseError(Exception):
    pass


# -- the service ----------------------------------------------------------------------

@app.get("/health")
def health():
    if os.environ.get("META_API_KEY"):
        return {"state": "ready", "error": None, "engine": "muse", "muse": "configured"}
    return {"state": "error", "error": "no META_API_KEY: hearing uses Muse Voice Transcribe",
            "engine": "muse", "muse": "no key"}


@app.post("/transcribe")
async def transcribe(payload: dict):
    import base64

    started = time.time()
    try:
        raw = base64.b64decode(payload.get("audio") or "")
        samples, rate = decode_wav(raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)
                            if str(exc).startswith("can't") else
                            f"can't read that audio: {exc}")
    seconds = round(len(samples) / rate, 2) if rate else 0.0
    if not os.environ.get("META_API_KEY"):
        raise HTTPException(503, "no META_API_KEY: hearing uses Muse Voice Transcribe")
    try:
        result = await muse_transcribe(raw, payload.get("keywords") or [])
    except MuseError as exc:
        raise HTTPException(503, str(exc))
    return {**result, "seconds": seconds, "engine": "muse",
            "ms": int((time.time() - started) * 1000)}
