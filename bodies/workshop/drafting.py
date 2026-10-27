"""Drafting helpers for the workshop: draft folders, local families,
and the SandboxFamily that runs builder-written code in bubblewrap.

A draft lives in ``<repo>/data/drafts/<build id>/`` while the builder
writes it. Once approved it can be promoted to a local family in
``<repo>/data/families/<NAME>/``. Both are reached through
``resolve("draft:<id>")`` / ``resolve("local:<NAME>")``, which gate on
the approval hash: the code that runs is the code that was reviewed.

The real ``connect`` / ``check`` hooks only ever run inside the
sandbox (see sandbox.py); the pure hooks are answered from the
reviewed meta by families.drafts.Declared.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BODIES = _HERE.parent
_REPO = _HERE.parent.parent
if str(_BODIES) not in sys.path:
    sys.path.insert(0, str(_BODIES))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

try:
    from workshop import sandbox  # bodies/ on sys.path
except ImportError:  # run as a plain script/module in this folder
    import sandbox  # type: ignore

from families import drafts as drafts_lib  # noqa: E402
from families import usb as usb_lib  # noqa: E402

NOT_APPROVED = "this code isn't approved (or it changed since)"


def repo_root() -> Path:
    return _REPO


def drafts_root() -> Path:
    return _REPO / "data" / "drafts"


def draft_folder(build) -> Path:
    """The draft folder for build id *build*."""
    return drafts_root() / str(build)


def local_root() -> Path:
    return _REPO / "data" / "families"


def local_folder(name) -> Path:
    return local_root() / str(name)


def read_folder_files(folder) -> dict:
    """{file name: text} for every regular file in *folder*."""
    folder = Path(folder)
    out: dict = {}
    if not folder.is_dir():
        return out
    for entry in sorted(folder.iterdir(), key=lambda p: p.name):
        if entry.is_file():
            try:
                out[entry.name] = entry.read_text(
                    encoding="utf-8", errors="replace")
            except OSError:
                continue
    return out


def review_files(files: dict) -> dict:
    known = set()
    try:
        from families import NAMES
        known.update(NAMES)
    except Exception:
        pass
    known.update(local_ids())
    return drafts_lib.review(files, known_families=known)


def review_folder(folder) -> dict:
    return review_files(read_folder_files(folder))


def local_ids() -> list:
    """Names of promoted local families (folders with provenance)."""
    root = local_root()
    out: list = []
    if root.is_dir():
        for entry in sorted(root.iterdir(), key=lambda p: p.name):
            if entry.is_dir() and (entry / "provenance.json").is_file():
                out.append(entry.name)
    return out


def device_of(usb_device: dict, taken=None) -> dict:
    """The device dict a draft/local family offers for a USB device."""
    vid = str(usb_device.get("vid") or "")
    pid = str(usb_device.get("pid") or "")
    serial = str(usb_device.get("serial") or "")
    path = str(usb_device.get("path") or "")
    if serial:
        device_id = f"usb:{vid}:{pid}:{serial}"
    else:
        device_id = f"usb:{path}"
    device = {
        "id": device_id,
        "name": usb_lib.label(usb_device),
        "usb": path,
        "ids": {"vid": vid, "pid": pid, "serial": serial},
        "nodes": usb_device.get("nodes") or [],
        "where": f"USB {path}",
        "ready": False,
        "status": "ready to connect",
    }
    if taken and device_id in taken:
        device["robot"] = taken[device_id]
        device["status"] = "in use"
    return device


def current(device: dict) -> dict:
    """*device* with its nodes refreshed from the USB device now here."""
    if not isinstance(device, dict):
        return device
    path = device.get("usb")
    ids = device.get("ids") if isinstance(device.get("ids"), dict) else {}
    for entry in usb_lib.devices():
        if path and entry.get("path") == path:
            refreshed = dict(device)
            refreshed["nodes"] = entry.get("nodes") or []
            return refreshed
        if ids and str(entry.get("vid") or "") == str(ids.get("vid") or "") \
                and str(entry.get("pid") or "") == str(ids.get("pid") or "") \
                and str(entry.get("serial") or "") == str(ids.get("serial") or ""):
            refreshed = dict(device)
            refreshed["nodes"] = entry.get("nodes") or []
            refreshed["usb"] = entry.get("path")
            refreshed["where"] = f"USB {entry.get('path')}"
            return refreshed
    return device


class SandboxFamily:
    """A draft or local family whose live hooks run in the sandbox."""

    def __init__(self, folder, meta: dict, ref: str = "") -> None:
        self.folder = Path(folder)
        self.meta = dict(meta or {})
        self.ref = ref
        self._declared = drafts_lib.Declared(self.meta)
        self.NAME = self.meta.get("NAME")
        self.TITLE = self.meta.get("TITLE")
        self.FIXTURE = None
        self.CLAIMS_PORTS = True

    # -- pure hooks, answered from the reviewed meta --------------------
    def setup(self, config, device):
        return self._declared.setup(config, device)

    def plan(self, config, args) -> str:
        return self._declared.plan(config, args)

    def checks(self, config, device) -> dict:
        return self._declared.checks(config, device)

    def problems(self, config):
        return self._declared.problems(config)

    def body(self, config, device) -> dict:
        return self._declared.body(config, device)

    # -- workshop-side hooks ---------------------------------------------
    def discover(self, taken=None) -> list:
        taken = taken or {}
        wanted = {str(m) for m in (self.meta.get("MATCH") or [])}
        out: list = []
        for entry in usb_lib.devices():
            pair = f"{entry.get('vid')}:{entry.get('pid')}"
            if wanted and pair not in wanted:
                continue
            device = device_of(entry, taken)
            device["family"] = self.ref or self.NAME
            out.append(device)
        return out

    def connect(self, device: dict, fields: dict):
        result = sandbox.run_hook(
            self.folder, "connect", [device, fields or {}],
            device=device, timeout=180)
        if isinstance(result, (list, tuple)) and len(result) == 2:
            text, updated = result
        elif isinstance(result, dict):
            text, updated = result.get("text", ""), result.get("device", device)
        else:
            text, updated = str(result), device
        merged = dict(device)
        if isinstance(updated, dict):
            merged.update(updated)
        merged["id"] = device.get("id")  # the merged device keeps its id
        merged = current(merged)  # nodes refreshed from the device now here
        merged["id"] = device.get("id")
        return (text, merged)

    def check(self, device: dict, config: dict, check_id: str) -> dict:
        result = sandbox.run_hook(
            self.folder, "check", [device, config, check_id],
            device=device, timeout=90)
        # run_hook has already decoded any __bytes__ back into bytes;
        # the workshop turns image bytes into base64 for the hub.
        return result if isinstance(result, dict) else {"expect": str(result)}

    def command(self, config_path: str, config: dict):
        device = (config or {}).get("device")
        argv = sandbox.driver_argv(self.folder, config_path, device=device)
        return (argv, {})  # sandboxed driver argv, no extra env


def _approval_hash(folder: Path, kind: str):
    """The recorded approval hash for *folder*, or None."""
    name = "approved.json" if kind == "draft" else "provenance.json"
    try:
        data = json.loads((folder / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):
        return data.get("hash")
    return None


def resolve(ref: str) -> SandboxFamily:
    """Resolve "draft:<id>" / "local:<NAME>" to a SandboxFamily.

    The gate: the recorded approval must match the current content
    hash, so only the exact reviewed code can ever run.
    """
    text = str(ref or "")
    if text.startswith("draft:"):
        folder = draft_folder(text[len("draft:"):])
        kind = "draft"
    elif text.startswith("local:"):
        folder = local_folder(text[len("local:"):])
        kind = "local"
    else:
        raise ValueError(f"not a draft or local family: {ref!r}")
    reviewed = review_folder(folder)
    approved = _approval_hash(folder, kind)
    if not approved or approved != reviewed.get("hash"):
        raise ValueError(NOT_APPROVED)
    return SandboxFamily(folder, reviewed.get("meta") or {}, ref=text)


def promote(draft_id: str, build: str) -> str:
    """Copy an approved draft into data/families/<NAME>/; see workshop."""
    folder = draft_folder(draft_id)
    reviewed = review_folder(folder)
    approved = _approval_hash(folder, "draft")
    if not approved or approved != reviewed.get("hash"):
        raise ValueError(NOT_APPROVED)
    if reviewed.get("problems"):
        raise ValueError("fix these first: "
                         + "; ".join(reviewed["problems"]))
    meta = reviewed.get("meta") or {}
    name = meta.get("NAME")
    if not name:
        raise ValueError("the draft has no NAME yet")
    if name in local_ids() or local_folder(name).exists():
        raise ValueError(f"{name} is already a local family")
    approved_at = None
    try:
        data = json.loads((folder / "approved.json").read_text(
            encoding="utf-8"))
        if isinstance(data, dict):
            approved_at = data.get("at")
    except (OSError, ValueError):
        pass
    target = local_folder(name)
    target.mkdir(parents=True, exist_ok=True)
    files = reviewed.get("files") or {}
    for fname, content in files.items():
        (target / fname).write_text(
            content if isinstance(content, str) else str(content),
            encoding="utf-8")
    provenance = {
        "written_by": "the builder",
        "build": build,
        "draft": str(draft_id),
        "hash": reviewed.get("hash"),
        "approved_at": approved_at,
        "promoted_at": time.time(),
        "files": sorted(files),
        "imports": reviewed.get("imports") or {},
        "reaches": reviewed.get("reaches") or [],
    }
    (target / "provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return f"local:{name}"


__all__ = ["SandboxFamily", "current", "device_of", "draft_folder",
           "drafts_root", "local_folder", "local_ids", "local_root",
           "promote", "read_folder_files", "repo_root", "resolve",
           "review_files", "review_folder", "NOT_APPROVED"]
