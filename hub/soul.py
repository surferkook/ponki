"""A soul: persona, journal, conversation, voice, colour (specs 02/03).

Step 1 keeps the file shape and the operations the House needs (birth from a
manifest, persona edits, photo forgetting, pending news). Spec 03 grows the
brain and the turn machinery on top of this same file.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from . import apilog

FALLBACK_COLORS = ["#4a6cf7", "#e0457b", "#16a085", "#f39c12", "#8e44ad", "#2d9cdb"]

DEFAULTS: dict = {
    "persona": "", "born": None, "journal": [], "conversation": [],
    "display_name": "", "color": "", "tagline": "", "portrait": 0,
    "painting": False, "promises": [], "news": [],
    "house_voice": {"id": "", "speed": 1.0, "pitch": 1.0},
    "usage": {"usd": None, "calls": 0, "in": 0, "out": 0, "cache_read": 0, "cache_write": 0},
    "turns": [],
}


def fallback_color(rid: str) -> str:
    digest = hashlib.sha256(rid.encode("utf-8")).hexdigest()
    return FALLBACK_COLORS[int(digest[:8], 16) % len(FALLBACK_COLORS)]


class Soul:
    def __init__(self, path: Path, rid: str) -> None:
        self.path = Path(path)
        self.id = rid
        self.data: dict = json.loads(json.dumps(DEFAULTS))
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    for key, value in loaded.items():
                        self.data[key] = value
            except (ValueError, OSError):
                pass
        self.busy_since: float | None = None

    # -- persistence ---------------------------------------------------------
    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    # -- simple views ----------------------------------------------------------
    @property
    def persona(self) -> str:
        return self.data.get("persona") or ""

    @property
    def display_name(self) -> str:
        return self.data.get("display_name") or ""

    @property
    def news(self) -> list:
        return self.data.setdefault("news", [])

    def info(self) -> dict:
        usage = dict(self.data.get("usage") or {})
        return {
            "persona": self.persona,
            "born": self.data.get("born"),
            "color": self.data.get("color") or fallback_color(self.id),
            "tagline": self.data.get("tagline") or "",
            "portrait": (f"/api/robots/{self.id}/portrait.webp?v={self.data.get('portrait', 0)}"
                         if self.data.get("portrait") else None),
            "painting": bool(self.data.get("painting")),
            "journal": list(self.data.get("journal") or []),
            "promises": list(self.data.get("promises") or []),
            "conversation_len": len(self.data.get("conversation") or []),
            "house_voice": dict(self.data.get("house_voice") or {}),
            "usage": usage,
            "turns": list(self.data.get("turns") or [])[-20:],
        }

    # -- life -------------------------------------------------------------------
    def ensure_born(self, manifest: dict) -> None:
        """Born from the manifest if there is no persona yet (03 grows this)."""
        if self.persona and self.data.get("born"):
            if manifest.get("display_name") and not self.display_name:
                self.data["display_name"] = manifest["display_name"]
                self.save()
            return
        display = (self.display_name or manifest.get("display_name")
                   or self.id[:1].upper() + self.id[1:])
        self.data["display_name"] = display
        if manifest.get("persona_hint"):
            persona = manifest["persona_hint"]
        else:
            description = manifest.get("description") or f"a robot called {display}"
            persona = f"You are {display}. {description}"
            if manifest.get("personality"):
                persona += f" Your personality: {manifest['personality']}."
        self.data["persona"] = persona
        self.data["born"] = self.data.get("born") or time.time()
        if not self.data.get("color"):
            self.data["color"] = fallback_color(self.id)
        self.save()

    def set_persona(self, persona: str) -> None:
        self.data["persona"] = persona
        self.data["conversation"] = []  # a fresh conversation; memories carry over
        self.save()

    def set_display_name(self, name: str) -> None:
        self.data["display_name"] = name
        self.save()

    def add_news(self, text: str) -> None:
        self.news.append(text)
        self.save()

    def forget_image(self, fp: str, note: str) -> int:
        count = apilog.forget_image(self.data.get("conversation"), fp, note)
        if count:
            self.save()
        return count
