"""The API log and photo fingerprints (spec 02).

One JSON line per model/SAM call in data/api_log.jsonl (30 days). Photos are
named here, never stored: fingerprints (sha256 of the model copy's base64
string, first 16 hex) live in data/seen/fingerprints.json.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from pathlib import Path

_data_dir: Path | None = None
_fingerprints: dict[str, str] = {}
_last_prune: float = 0.0


def _fp_path() -> Path:
    assert _data_dir is not None
    return _data_dir / "seen" / "fingerprints.json"


def _log_path() -> Path:
    assert _data_dir is not None
    return _data_dir / "api_log.jsonl"


def _save_fingerprints() -> None:
    path = _fp_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_fingerprints, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def configure(data_dir: Path) -> None:
    global _data_dir, _fingerprints
    _data_dir = Path(data_dir)
    _fingerprints = {}
    path = _fp_path()
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                _fingerprints = {str(k): str(v) for k, v in loaded.items()}
        except (ValueError, OSError):
            pass


def fingerprint(image_b64: str) -> str:
    return hashlib.sha256(image_b64.encode("utf-8")).hexdigest()[:16]


def register(model_b64: str | None, label: str) -> str | None:
    if not model_b64:
        return None
    fp = fingerprint(model_b64)
    _fingerprints[fp] = label
    _save_fingerprints()
    return fp


def unregister(fp: str) -> None:
    if _fingerprints.pop(fp, None) is not None:
        _save_fingerprints()


def photo_name(fp: str | None) -> str:
    if not fp:
        return "photo"
    return _fingerprints.get(fp, f"photo {fp[:8]}")


def label_index() -> dict[str, str]:
    return {label: fp for fp, label in _fingerprints.items()}


# -- images inside conversations (both provider shapes) ---------------------

def _image_b64(node: object) -> str | None:
    if not isinstance(node, dict):
        return None
    if node.get("type") == "image":
        source = node.get("source")
        if isinstance(source, dict) and isinstance(source.get("data"), str):
            return source["data"]
    if node.get("type") == "image_url":
        inner = node.get("image_url")
        if isinstance(inner, dict):
            url = inner.get("url")
            if isinstance(url, str) and url.startswith("data:") and "," in url:
                return url.split(",", 1)[1]
    return None


def images_in(conversation: object) -> list[str]:
    found: list[str] = []

    def walk(node: object) -> None:
        b64 = _image_b64(node)
        if b64 is not None:
            found.append(fingerprint(b64))
            return
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(conversation)
    return found


def forget_image(conversation: object, fp: str, note: str) -> int:
    count = 0

    def walk(node: object) -> None:
        nonlocal count
        if isinstance(node, list):
            for i, item in enumerate(node):
                b64 = _image_b64(item)
                if b64 is not None and fingerprint(b64) == fp:
                    node[i] = {"type": "text", "text": note}
                    count += 1
                else:
                    walk(item)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)

    walk(conversation)
    return count


# -- log entries --------------------------------------------------------------

def _texts(new: object) -> tuple[str, list[str]]:
    """(summary text, new photo labels) for the messages new to a request."""
    parts: list[str] = []
    photos: list[str] = []

    def walk(node: object) -> None:
        b64 = _image_b64(node)
        if b64 is not None:
            label = photo_name(fingerprint(b64))
            photos.append(label)
            parts.append(f"[{label}]")
            return
        if isinstance(node, str):
            if node.strip():
                parts.append(node)
        elif isinstance(node, dict):
            if node.get("type") in ("tool_use", "function_call") and node.get("name"):
                args = node.get("input", node.get("arguments", ""))
                parts.append(f"{node['name']}({json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args})")
                return
            for key in ("tool_calls",):
                for call in node.get(key) or []:
                    fn = (call or {}).get("function") or {}
                    if fn.get("name"):
                        parts.append(f"{fn['name']}({fn.get('arguments', '')})")
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(new)
    return " · ".join(p for p in parts if p), photos


def _cut(text: str, limit: int) -> str:
    text = text or ""
    return text[:limit] + "…" if len(text) > limit else text


def model_call(who, model, new, all_messages, reply="", tools=None, usage=None, error=None) -> None:
    sent, photos_new = _texts(new)
    entry: dict = {
        "t": time.time(), "kind": "model", "who": who, "model": model,
        "sent": _cut(sent, 600), "photos_new": photos_new,
        "photos_in_request": len(images_in(all_messages)),
        "reply": _cut(reply or "", 600),
        "tools": [f"{t.get('name', '?')}({_cut(json.dumps(t.get('args', ''), ensure_ascii=False) if not isinstance(t.get('args'), str) else t.get('args', ''), 120)})"
                  if isinstance(t, dict) else str(t) for t in (tools or [])],
        "usage": usage,
    }
    if error:
        entry["error"] = _cut(str(error), 300)
    write(entry)


def sam_call(who, kind, what, photo=None, found="", error=None) -> None:
    entry: dict = {"t": time.time(), "kind": "sam", "who": who, "call": kind,
                   "what": what, "photo": photo, "found": _cut(found or "", 600)}
    if error:
        entry["error"] = _cut(str(error), 300)
    write(entry)


def write(entry: dict) -> None:
    if _data_dir is None:
        return
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    prune()


def prune() -> None:
    global _last_prune
    now = time.time()
    if now - _last_prune < 3600 or _data_dir is None:
        return
    _last_prune = now
    path = _log_path()
    if not path.exists():
        return
    cutoff = now - 30 * 86400
    kept: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if float(entry.get("t", 0)) >= cutoff:
                kept.append(line)
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _usage_nums(usage: object) -> tuple[int, int, int]:
    if not isinstance(usage, dict):
        return 0, 0, 0
    def pick(*keys: str) -> int:
        for key in keys:
            if isinstance(usage.get(key), (int, float)):
                return int(usage[key])
        return 0
    return (pick("input_tokens", "prompt_tokens", "in"),
            pick("cache_read", "cached_tokens", "cache_read_input_tokens"),
            pick("output_tokens", "completion_tokens", "out"))


def tail(lines: int = 200) -> list[str]:
    if _data_dir is None or not _log_path().exists():
        return []
    raw = _log_path().read_text(encoding="utf-8").splitlines()[-lines:]
    out: list[str] = []
    for line in raw:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        stamp = datetime.fromtimestamp(float(e.get("t", 0))).isoformat(timespec="seconds")
        if e.get("kind") == "model":
            inn, cached, out_n = _usage_nums(e.get("usage"))
            labels = ", ".join(e.get("photos_new") or [])
            out.append(f"{stamp} model {str(e.get('who', '')):14} {e.get('model', '')} · "
                       f"in {inn} (cached {cached}) out {out_n} · photos: "
                       f"{len(e.get('photos_new') or [])} new ({labels}), "
                       f"{e.get('photos_in_request', 0)} in the request")
            if e.get("sent"):
                out.append(f"  sent: {e['sent']}")
            if e.get("reply"):
                out.append(f"  reply: {e['reply']}")
            if e.get("tools"):
                out.append(f"  tools: {', '.join(e['tools'])}")
            if e.get("error"):
                out.append(f"  ERROR: {e['error']}")
        elif e.get("kind") == "sam":
            out.append(f"{stamp} sam {e.get('who', '')} {e.get('call', '')} "
                       f"{e.get('what', '')} photo={e.get('photo')} found: {e.get('found', '')}")
            if e.get("error"):
                out.append(f"  ERROR: {e['error']}")
        else:
            out.append(f"{stamp} {json.dumps(e, ensure_ascii=False)[:400]}")
    return out
