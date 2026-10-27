"""USB devices as families see them (read-only, opens nothing).

Walks /sys/bus/usb/devices and reports each real device with its
interfaces and dev nodes. No device file is ever opened; only sysfs
text files and symlink targets are read.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

SYS_USB = Path("/sys/bus/usb/devices")
SYS_CLASS = Path("/sys/class")
SERIAL_BY_ID = Path("/dev/serial/by-id")

_KIND_WORDS = {"serial": "serial port", "video": "camera", "sound": "sound"}


def _read(entry: Path, name: str) -> str:
    try:
        return (entry / name).read_text(encoding="utf-8",
                                        errors="replace").strip()
    except OSError:
        return ""


def _driver_of(interface: Path) -> str:
    link = interface / "driver"
    try:
        if link.is_symlink() or link.exists():
            return link.resolve().name
    except OSError:
        pass
    return ""


def _stable_for(node: str) -> str:
    try:
        if SERIAL_BY_ID.is_dir():
            for link in sorted(SERIAL_BY_ID.iterdir()):
                try:
                    if os.path.realpath(link) == os.path.realpath(node) \
                            or os.path.realpath(link) == node:
                        return str(link)
                except OSError:
                    continue
    except OSError:
        pass
    return node


def _nodes_for_interface(interface: Path) -> list[dict]:
    nodes: list[dict] = []
    # Serial: tty* directly below the interface, or below its tty/ dir.
    serial_dirs: dict[str, Path] = {}
    for base in (interface, interface / "tty"):
        try:
            if not base.is_dir():
                continue
            for entry in sorted(base.glob("tty*")):
                if entry.is_dir():
                    serial_dirs[entry.name] = entry
        except OSError:
            continue
    for tty_name in sorted(serial_dirs):
        node = f"/dev/{tty_name}"
        nodes.append({"kind": "serial", "node": node,
                      "stable": _stable_for(node)})
    # Video: video4linux/video*.
    try:
        video_dir = interface / "video4linux"
        if video_dir.is_dir():
            for entry in sorted(video_dir.glob("video*")):
                nodes.append({"kind": "video",
                              "node": f"/dev/{entry.name}"})
    except OSError:
        pass
    # Sound: sound/cardN -> /dev/snd/pcmCND0c (capture).
    try:
        sound_dir = interface / "sound"
        if sound_dir.is_dir():
            for entry in sorted(sound_dir.glob("card*")):
                card = entry.name[len("card"):]
                if card.isdigit():
                    nodes.append({"kind": "sound", "card": card,
                                  "node": f"/dev/snd/pcmC{card}D0c"})
    except OSError:
        pass
    return nodes


def _interfaces_and_nodes(device_dir: Path) -> tuple[list[dict], list[dict]]:
    by_name: dict[str, Path] = {}
    try:
        for entry in device_dir.iterdir():
            if ":" in entry.name and entry.is_dir():
                by_name[entry.name] = entry
    except OSError:
        pass
    try:
        if SYS_USB.is_dir():
            for entry in SYS_USB.iterdir():
                if entry.name.startswith(device_dir.name + ":"):
                    by_name.setdefault(entry.name, entry)
    except OSError:
        pass
    interfaces: list[dict] = []
    nodes: list[dict] = []
    for name in sorted(by_name):
        interface = by_name[name]
        interfaces.append({
            "class": _read(interface, "bInterfaceClass"),
            "subclass": _read(interface, "bInterfaceSubClass"),
            "driver": _driver_of(interface),
        })
        nodes.extend(_nodes_for_interface(interface))
    return interfaces, nodes


def devices() -> list[dict]:
    """Every real USB device: {path, vid, pid, ..., interfaces, nodes}."""
    out: list[dict] = []
    try:
        if not SYS_USB.is_dir():
            return out
        entries = sorted(SYS_USB.iterdir(), key=lambda p: p.name)
    except OSError:
        return out
    for entry in entries:
        name = entry.name
        if ":" in name:  # an interface entry, not a device
            continue
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        vid = _read(entry, "idVendor")
        if not vid:
            continue
        dev_class = _read(entry, "bDeviceClass")
        if dev_class == "09":  # a hub
            continue
        interfaces, nodes = _interfaces_and_nodes(entry)
        out.append({
            "path": name,
            "vid": vid,
            "pid": _read(entry, "idProduct"),
            "manufacturer": _read(entry, "manufacturer"),
            "product": _read(entry, "product"),
            "serial": _read(entry, "serial"),
            "class": dev_class,
            "interfaces": interfaces,
            "nodes": nodes,
        })
    return out


def path_of(node_or_card) -> str | None:
    """The USB path (e.g. "1-1.1") owning a dev node or sound card."""
    if node_or_card is None:
        return None
    text = str(node_or_card).strip()
    if not text:
        return None
    start: Path | None = None
    pcm = re.search(r"pcmC(\d+)D", text)
    base_name = Path(text).name
    if pcm:
        start = SYS_CLASS / "sound" / f"card{pcm.group(1)}" / "device"
    elif re.fullmatch(r"\d+", text):
        start = SYS_CLASS / "sound" / f"card{text}" / "device"
    elif re.fullmatch(r"card\d+", base_name):
        start = SYS_CLASS / "sound" / base_name / "device"
    else:
        hint = base_name
        if text.startswith("/dev/"):
            hint = Path(os.path.realpath(text)).name
        if hint.startswith("tty"):
            start = SYS_CLASS / "tty" / hint / "device"
        elif hint.startswith("video"):
            start = SYS_CLASS / "video4linux" / hint / "device"
        elif hint.startswith("card"):
            start = SYS_CLASS / "sound" / hint / "device"
        else:
            return None
    if start is None:
        return None
    try:
        current = Path(os.path.realpath(start))
    except OSError:
        return None
    for _ in range(12):
        try:
            if (current / "idVendor").exists():
                return current.name
        except OSError:
            return None
        if current.parent == current:
            return None
        current = current.parent
    return None


def label(d: dict) -> str:
    """'BRIO 4K Stream Edition (camera, sound)' style one-liner."""
    words: list[str] = []
    for node in d.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        word = _KIND_WORDS.get(node.get("kind"))
        if word and word not in words:
            words.append(word)
    suffix = f" ({', '.join(words)})" if words else ""
    product = str(d.get("product") or "").strip()
    if product:
        return f"{product}{suffix}"
    vid = str(d.get("vid") or "").strip()
    pid = str(d.get("pid") or "").strip()
    if vid and pid:
        return f"USB device {vid}:{pid}{suffix}"
    if vid:
        return f"USB device {vid}{suffix}"
    return f"USB device{suffix}"
