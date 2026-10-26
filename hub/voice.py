"""The house voice: the catalogue and the client of the voice service (spec 06).

The service (services/voice, :8202) turns text into 48 kHz speech in one of
9 voices. The hub keeps an identical copy of the table because it must know
the voices while the service is still loading. A soul's voice is
{"id", "speed", "pitch"}; clean() is the one place voices are validated.
"""
from __future__ import annotations

import os
import zlib

import httpx

VOICE_URL = os.environ.get("HOUSE_VOICE_URL", "http://127.0.0.1:8202")

VOICES: list[dict] = [
    {"id": "af_heart", "label": "Heart (Kokoro): warm, friendly woman",
     "description": "warm, friendly woman"},
    {"id": "af_bella", "label": "Bella (Kokoro): bright, lively woman",
     "description": "bright, lively woman"},
    {"id": "af_nicole", "label": "Nicole (Kokoro): soft, breathy, whispery woman",
     "description": "soft, breathy, whispery woman"},
    {"id": "af_sky", "label": "Sky (Kokoro): light, young woman",
     "description": "light, young woman"},
    {"id": "am_michael", "label": "Michael (Kokoro): warm, mature man",
     "description": "warm, mature man"},
    {"id": "am_puck", "label": "Puck (Kokoro): playful, cheeky man",
     "description": "playful, cheeky man"},
    {"id": "en_US-amy-medium", "label": "Amy (Piper): warm, clear woman",
     "description": "warm, clear woman"},
    {"id": "en_US-hfc_female-medium", "label": "HFC (Piper): bright, crisp woman",
     "description": "bright, crisp woman"},
    {"id": "en_US-ryan-medium", "label": "Ryan (Piper): clear, friendly man",
     "description": "clear, friendly man"},
]
_IDS = {v["id"] for v in VOICES}
# voices a soul may still carry from before the ids took their service names
_LEGACY = {"heart": "af_heart", "bella": "af_bella", "nicole": "af_nicole",
           "sky": "af_sky", "michael": "am_michael", "puck": "am_puck",
           "amy": "en_US-amy-medium", "hfc": "en_US-hfc_female-medium",
           "ryan": "en_US-ryan-medium"}


class VoiceError(Exception):
    """The house voice failed, in plain words."""


def descriptions() -> dict[str, str]:
    return {v["id"]: v["description"] for v in VOICES}


def labels() -> dict[str, str]:
    return {v["id"]: v["label"] for v in VOICES}


def clean(v: dict | None, min_pitch: float = 0.7) -> dict | None:
    """A valid voice dict, clamped and rounded, or None for an unknown id."""
    if not isinstance(v, dict):
        return None
    vid = _LEGACY.get(v.get("id"), v.get("id"))
    if vid not in _IDS:
        return None
    try:
        pitch = float(v.get("pitch", 1.0))
        speed = float(v.get("speed", 1.0))
    except (TypeError, ValueError):
        return None
    return {"id": vid,
            "pitch": round(min(1.6, max(min_pitch, pitch)), 2),
            "speed": round(min(1.6, max(0.6, speed)), 2)}


def default_for(name: str) -> dict:
    """A stable voice pick: sorted ids indexed by crc32(name) mod 9."""
    ids = sorted(_IDS)
    return {"id": ids[zlib.crc32(name.encode("utf-8")) % len(ids)],
            "speed": 1.0, "pitch": 1.0}


async def synth(text: str, v: dict) -> tuple[bytes, int]:
    """(WAV bytes, duration ms) from the voice service."""
    cleaned = clean(v) or default_for("house")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{VOICE_URL}/speak", json={
                "text": text, "voice": cleaned["id"],
                "speed": cleaned["speed"], "pitch": cleaned["pitch"]})
    except httpx.HTTPError as exc:
        raise VoiceError(f"the house voice isn't reachable ({type(exc).__name__})")
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", "")
        except Exception:
            detail = resp.text[:200]
        raise VoiceError(str(detail)[:200] or f"the house voice failed ({resp.status_code})")
    try:
        ms = int(resp.headers.get("X-Duration-Ms", "0"))
    except ValueError:
        ms = 0
    return resp.content, ms


async def health() -> dict:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{VOICE_URL}/health")
        data = resp.json()
        return {"state": data.get("state", "error"), "error": data.get("error")}
    except Exception:
        return {"state": "down", "error": "not running"}
