"""The house voice catalogue (specs 03/06).

Spec 06 grows synthesis and playback on top of this list. A soul's voice is
{"id", "pitch", "speed"}; clean() is the one place voices are validated.
"""
from __future__ import annotations

import hashlib

VOICES: list[dict] = [
    {"id": "heart", "label": "Heart (Kokoro): warm, friendly woman", "description": "warm, friendly woman"},
    {"id": "bella", "label": "Bella (Kokoro): bright, lively woman", "description": "bright, lively woman"},
    {"id": "nicole", "label": "Nicole (Kokoro): soft, breathy, whispery woman", "description": "soft, breathy, whispery woman"},
    {"id": "sky", "label": "Sky (Kokoro): light, young woman", "description": "light, young woman"},
    {"id": "michael", "label": "Michael (Kokoro): warm, mature man", "description": "warm, mature man"},
    {"id": "puck", "label": "Puck (Kokoro): playful, cheeky man", "description": "playful, cheeky man"},
    {"id": "amy", "label": "Amy (Piper): warm, clear woman", "description": "warm, clear woman"},
    {"id": "hfc", "label": "HFC (Piper): bright, crisp woman", "description": "bright, crisp woman"},
    {"id": "ryan", "label": "Ryan (Piper): clear, friendly man", "description": "clear, friendly man"},
]
_IDS = {v["id"] for v in VOICES}


def descriptions() -> dict[str, str]:
    return {v["id"]: v["description"] for v in VOICES}


def clean(v: dict | None, min_pitch: float = 0.70) -> dict | None:
    """A valid voice dict, clamped, or None if the id isn't a real voice."""
    if not isinstance(v, dict) or v.get("id") not in _IDS:
        return None
    try:
        pitch = float(v.get("pitch", 1.0))
        speed = float(v.get("speed", 1.0))
    except (TypeError, ValueError):
        return None
    return {"id": v["id"],
            "pitch": min(1.60, max(min_pitch, pitch)),
            "speed": min(1.60, max(0.60, speed))}


def default_for(rid: str) -> dict:
    """A stable voice pick from the robot's id."""
    digest = hashlib.sha256(rid.encode("utf-8")).hexdigest()
    voice = VOICES[int(digest[:8], 16) % len(VOICES)]
    return {"id": voice["id"], "pitch": 1.0, "speed": 1.0}
