"""The record: identity and placement of every robot and fixture (spec 02).

One file, data/registry.json: {"version": 1, "robots": {<id>: <record>}}.
Written atomically. The id never changes; renames change only display_name.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

PROTOCOL = 1

KNOWN_FIELDS = ("id", "display_name", "kind", "family", "device", "workshop",
                "runner", "build", "protocol", "created", "updated")


def _atomic_write(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _unknown(value: object) -> bool:
    return value is None or value == "" or value == {}


class Registry:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict = {"version": 1, "robots": {}}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("robots"), dict):
                    self._data = loaded
            except (ValueError, OSError):
                pass

    def _save(self) -> None:
        _atomic_write(self.path, self._data)

    def get(self, rid: str) -> dict | None:
        rec = self._data["robots"].get(rid)
        return dict(rec) if rec else None

    def all(self, kind: str | None = None) -> list[dict]:
        recs = [dict(r) for r in self._data["robots"].values()]
        if kind is not None:
            recs = [r for r in recs if r.get("kind") == kind]
        return sorted(recs, key=lambda r: r.get("id", ""))

    def upsert(self, rid: str, **fields: object) -> dict:
        now = time.time()
        rec = self._data["robots"].get(rid)
        if rec is None:
            rec = {"id": rid, "kind": "robot", "created": now}
            self._data["robots"][rid] = rec
        for key, value in fields.items():
            if key == "id" or key not in KNOWN_FIELDS:
                continue
            if value is None:
                continue  # a None never overwrites a known value
            rec[key] = value
        rec["updated"] = now
        self._save()
        return dict(rec)

    def adopt(self, rid: str, **fields: object) -> dict:
        now = time.time()
        rec = self._data["robots"].get(rid)
        if rec is None:
            # a fresh record takes the caller's kind (a fixture is not a robot)
            kind = fields.get("kind")
            rec = {"id": rid, "kind": kind if isinstance(kind, str) and kind
                   else "robot", "created": now, "updated": now}
            self._data["robots"][rid] = rec
        changed = False
        for key, value in fields.items():
            if key == "id" or key not in KNOWN_FIELDS:
                continue
            if _unknown(value):
                continue
            if _unknown(rec.get(key)):
                rec[key] = value
                changed = True
        rec["updated"] = now
        self._save()
        return dict(rec)

    def pop(self, rid: str) -> dict | None:
        rec = self._data["robots"].pop(rid, None)
        if rec is None:
            return None
        self._save()
        return dict(rec)

    def restore(self, rec: dict) -> None:
        rec = dict(rec)
        rec["updated"] = time.time()
        self._data["robots"][rec["id"]] = rec
        self._save()

    def by_display_name(self, name: str) -> str | None:
        wanted = (name or "").strip().lower()
        for rid, rec in self._data["robots"].items():
            if (rec.get("display_name") or "").strip().lower() == wanted:
                return rid
        return None
