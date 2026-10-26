"""The house's hearing: the ears client and who a sentence is for (spec 06).

The ears (services/ears, :8203) turn speech into text. This module calls
them, and works out which robot a sentence names — loosely, because
speech-to-text bends names it doesn't know ("Kaiki", "room-room").
"""
from __future__ import annotations

import difflib
import os
import re

import httpx

EARS_URL = os.environ.get("HOUSE_EARS_URL", "http://127.0.0.1:8203")
_WORD = re.compile(r"[a-z0-9]+")


class HearingError(Exception):
    """The house's ears failed, in plain words."""


async def transcribe(audio_b64: str, keywords: list[str] | None = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{EARS_URL}/transcribe", json={
                "audio": audio_b64, "keywords": keywords or []})
    except httpx.HTTPError as exc:
        raise HearingError(f"the house's ears aren't reachable ({type(exc).__name__})")
    if resp.status_code >= 400:
        raise HearingError(
            f"the house's ears failed ({resp.status_code}): {resp.text[:200]}")
    return resp.json()


async def health() -> dict:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{EARS_URL}/health")
        return resp.json()
    except Exception:
        return {"state": "down", "error": "not running"}


def _forms(rid: str, display: str) -> set[str]:
    forms = {rid.lower(), (display or "").lower()}
    squashed = re.sub(r"[^a-z0-9]", "", (display or "").lower())
    if squashed:
        forms.add(squashed)
    forms.discard("")
    return forms


def _matches(candidate: str, form: str) -> bool:
    if candidate == form:
        return True
    if len(form) < 4:
        return False
    allowed = 2 if len(form) >= 8 else 1
    if abs(len(candidate) - len(form)) > allowed:
        return False
    return difflib.SequenceMatcher(None, candidate, form).ratio() >= 0.75


def addressed(text: str, robots: dict[str, str]) -> str | None:
    """The id of the robot named in the text, earliest mention first."""
    words = _WORD.findall((text or "").lower())
    forms = {rid: _forms(rid, display) for rid, display in robots.items()}
    for i, word in enumerate(words):
        candidates = [word]
        if i + 1 < len(words):
            candidates.append(word + words[i + 1])
        for rid, robot_forms in forms.items():
            if any(_matches(c, f) for c in candidates for f in robot_forms):
                return rid
    return None
