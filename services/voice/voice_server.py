"""The house voice: text to 48 kHz speech in 9 voices (spec 06).

HOUSE_VOICES narrows them (the Docker image ships two: af_heart, am_puck).

    uvicorn voice_server:app --host 127.0.0.1 --port 8202

Kokoro (hexgrad/Kokoro-82M) and Piper (rhasspy/piper-voices, models in
voices/, not in git) behind one endpoint. Speed and pitch are done with a
single polyphase resample: synthesis runs at speed/pitch, then the clip is
resampled by 48000/native/pitch, so the pitch moves and the pace stays.
Everything leaves as 48 kHz mono 16-bit PCM, so no player converts again.
"""
from __future__ import annotations

import io
import os
import struct
import threading
import time
import wave
from fractions import Fraction
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Response

HERE = Path(__file__).parent
VOICES_DIR = Path(os.environ.get("VOICE_MODELS", HERE / "voices"))
PEAK = 0.7
OUT_RATE = 48000

VOICES = {
    "af_heart": "Heart (Kokoro): warm, friendly woman",
    "af_bella": "Bella (Kokoro): bright, lively woman",
    "af_nicole": "Nicole (Kokoro): soft, breathy, whispery woman",
    "af_sky": "Sky (Kokoro): light, young woman",
    "am_michael": "Michael (Kokoro): warm, mature man",
    "am_puck": "Puck (Kokoro): playful, cheeky man",
    "en_US-amy-medium": "Amy (Piper): warm, clear woman",
    "en_US-hfc_female-medium": "HFC (Piper): bright, crisp woman",
    "en_US-ryan-medium": "Ryan (Piper): clear, friendly man",
}
# HOUSE_VOICES (comma list) narrows the voices, e.g. af_heart,am_puck for a
# house that ships only Kokoro (Apache 2.0) and no Piper models.
_ONLY = [v.strip() for v in os.environ.get("HOUSE_VOICES", "").split(",") if v.strip()]
if _ONLY:
    VOICES = {k: v for k, v in VOICES.items() if k in _ONLY} or VOICES
_FIRST = next(iter(VOICES))

app = FastAPI(title="ponkihouse voice")
_state = {"state": "loading", "error": None}
_lock = threading.Lock()
_kokoro = None
_piper: dict = {}


def _load() -> None:
    global _kokoro
    try:
        from kokoro import KPipeline

        _kokoro = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")
        kokoro_voice = next((v for v in VOICES if not v.startswith("en_US-")), None)
        if kokoro_voice:
            _speak_kokoro("Ready.", kokoro_voice, 1.0)  # warm-up
        for voice_id in VOICES:
            if voice_id.startswith("en_US-"):
                _piper_voice(voice_id)
                _speak_piper("Ready.", voice_id, 1.0)
        _state.update(state="ready", error=None)
    except Exception as exc:
        _state.update(state="error", error=f"{type(exc).__name__}: {exc}")


def _piper_voice(voice_id: str):
    if voice_id not in _piper:
        from piper import PiperVoice

        model = VOICES_DIR / f"{voice_id}.onnx"
        if not model.exists():
            raise HTTPException(400, f"unknown voice '{voice_id}'")
        _piper[voice_id] = PiperVoice.load(str(model),
                                           config_path=str(model) + ".json")
    return _piper[voice_id]


def _speak_kokoro(text: str, voice_id: str, pace: float) -> tuple[np.ndarray, int]:
    chunks = []
    for _gs, _ps, audio in _kokoro(text, voice=voice_id, speed=pace):
        chunks.append(np.asarray(audio, dtype=np.float32))
    if not chunks:
        raise ValueError("Kokoro made no sound")
    return np.concatenate(chunks), 24000


def _speak_piper(text: str, voice_id: str, pace: float) -> tuple[np.ndarray, int]:
    from piper.config import SynthesisConfig

    voice = _piper_voice(voice_id)
    config = SynthesisConfig(length_scale=pace)
    chunks = []
    rate = 22050
    for chunk in voice.synthesize(text, syn_config=config):
        chunks.append(np.asarray(chunk.audio_float_array, dtype=np.float32))
        rate = int(chunk.sample_rate)
    if not chunks:
        raise ValueError("Piper made no sound")
    return np.concatenate(chunks), rate


def _shape(audio: np.ndarray, native: int, pitch: float) -> bytes:
    """One resample does rate conversion + pitch; fades; peak; 16-bit WAV."""
    from scipy.signal import resample_poly

    frac = Fraction(OUT_RATE, native) / Fraction(pitch).limit_denominator(1000)
    frac = frac.limit_denominator(2000)
    out = resample_poly(audio, frac.numerator, frac.denominator).astype(np.float32)
    fade = min(240, len(out) // 2)
    if fade:
        out[:fade] *= np.linspace(0.0, 1.0, fade, dtype=np.float32)
        out[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
    peak = float(np.max(np.abs(out))) if len(out) else 0.0
    if peak > 0:
        out *= PEAK / peak
    pcm = (np.clip(out, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(OUT_RATE)
        wav.writeframes(pcm.tobytes())
    return buf.getvalue()


@app.get("/health")
def health():
    return {"state": _state["state"], "error": _state["error"]}


@app.get("/voices")
def voices():
    return dict(VOICES)


@app.post("/speak")
def speak(payload: dict):
    if _state["state"] != "ready":
        detail = "voice is loading" if _state["state"] == "loading" \
            else f"voice is error: {_state['error']}"
        raise HTTPException(503, detail)
    text = (payload.get("text") or "").strip()[:1000]
    if not text:
        raise HTTPException(400, "empty text")
    voice_id = payload.get("voice") or _FIRST
    if voice_id not in VOICES:
        raise HTTPException(400, f"unknown voice '{voice_id}'")
    speed = min(1.6, max(0.6, float(payload.get("speed", 1.0))))
    pitch = min(1.6, max(0.7, float(payload.get("pitch", 1.0))))
    started = time.time()
    with _lock:  # one synthesis at a time
        if voice_id.startswith("en_US-"):
            audio, native = _speak_piper(text, voice_id, pitch / speed)
        else:
            audio, native = _speak_kokoro(text, voice_id, speed / pitch)
        wav = _shape(audio, native, pitch)
    samples = len(wav) // 2 - 22  # rough; the header parse below is exact
    with wave.open(io.BytesIO(wav), "rb") as reader:
        duration_ms = int(reader.getnframes() / OUT_RATE * 1000)
    return Response(wav, media_type="audio/wav", headers={
        "X-Synth-Ms": str(int((time.time() - started) * 1000)),
        "X-Duration-Ms": str(duration_ms)})


threading.Thread(target=_load, daemon=True).start()
