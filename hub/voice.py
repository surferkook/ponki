"""The house voice: the catalogue and the client of the voice service (spec 06).

The service (services/voice, :8202) speaks with Meta's MMS-TTS. It has one
English speaker, so the house's voices are presets of it, each with its own
pitch and pace; a soul's own pitch and speed go on top. The hub keeps an
identical copy of the table because it must know the voices while the
service is still loading. A soul's voice is {"id", "speed", "pitch"};
clean() is the one place voices are validated.
"""
from __future__ import annotations

import os
import zlib

import httpx

VOICE_URL = os.environ.get("HOUSE_VOICE_URL", "http://127.0.0.1:8202")

VOICES: list[dict] = [
    {"id": "warm", "label": "Warm (MMS): steady, friendly", "description": "steady, friendly voice"},
    {"id": "bright", "label": "Bright (MMS): lively, clear", "description": "bright, lively voice"},
    {"id": "small", "label": "Small (MMS): little, quick", "description": "small, quick voice"},
    {"id": "low", "label": "Low (MMS): deep, calm", "description": "low, calm voice"},
    {"id": "playful", "label": "Playful (MMS): cheeky, bouncy", "description": "playful, cheeky voice"},
    {"id": "soft", "label": "Soft (MMS): gentle, unhurried", "description": "soft, gentle voice"},
]
_IDS = {v["id"] for v in VOICES}
# voices a soul may still carry from before the house spoke with MMS: each
# goes to the nearest preset, and the soul's own pitch and speed still apply
_LEGACY = {"af_heart": "bright", "af_bella": "bright", "af_nicole": "soft",
           "af_sky": "warm", "am_michael": "low", "am_puck": "playful",
           "en_US-amy-medium": "bright", "en_US-hfc_female-medium": "bright",
           "en_US-ryan-medium": "warm",
           "heart": "bright", "bella": "bright", "nicole": "soft", "sky": "warm",
           "michael": "low", "puck": "playful", "amy": "bright", "hfc": "bright",
           "ryan": "warm"}


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
    """A stable voice pick: sorted ids indexed by crc32(name)."""
    ids = sorted(_IDS)
    return {"id": ids[zlib.crc32(name.encode("utf-8")) % len(ids)],
            "speed": 1.0, "pitch": 1.0}


async def synth(text: str, v: dict) -> tuple[bytes, int]:
    """(WAV bytes, duration ms) from the voice service."""
    cleaned = clean(v) or default_for("house")
    if VOICE_URL.strip().lower() in ("", "off"):
        raise VoiceError("this house has no voice service")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{VOICE_URL}/speak", json={
                "text": text, "voice": cleaned["id"],
                "speed": cleaned["speed"], "pitch": cleaned["pitch"]})
    except httpx.HTTPError as exc:
        if _optional():
            raise VoiceError("the house voice isn't running: start Ponki with "
                             "docker compose --profile voice up to hear the robots")
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


def _optional() -> bool:
    """A house where the voice is an extra (Docker: --profile voice)."""
    return os.environ.get("HOUSE_VOICE_OPTIONAL", "").lower() in ("1", "on", "yes", "true")


async def health() -> dict:
    if VOICE_URL.strip().lower() in ("", "off"):
        return {"state": "off", "error": None}  # this house has none
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{VOICE_URL}/health")
        data = resp.json()
        return {"state": data.get("state", "error"), "error": data.get("error")}
    except Exception:
        if _optional():
            return {"state": "off", "error": None}  # an optional service, not started
        return {"state": "down", "error": "not running"}
