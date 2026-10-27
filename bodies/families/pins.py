"""The pins family (spec 04): Arduino Uno/Nano boards running house-pins.

Finds boards, flashes the one generic sketch, plans parts, assigns pins
and checks the wiring one part at a time. The contract is spec 09's.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "pins"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import parts as parts_lib  # noqa: E402

NAME = "pins"
TITLE = "Arduino board"
SCAN = "USB boards"
FIXTURE = None
FIRMWARE = "house-pins"
SUPPORTED_FQBN = {"arduino:avr:uno": "uno", "arduino:avr:nano": "nano"}
CONNECT = {"label": "Install the pins firmware",
           "hint": "It needs the pins firmware (once per board, about a minute).",
           "fields": []}
CHECK_HINT = "Unplug the USB before you move a wire."
PLAN_PARAMS = {
    "type": "object",
    "properties": {
        "parts": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "short lowercase id"},
                "kind": {"type": "string", "enum": sorted(parts_lib.KINDS)},
                "about": {"type": "string",
                          "description": "what this part is on THIS robot, as the robot "
                                         "would say it: 'your flag arm'"},
                "pins": {"type": "object",
                         "description": "only to override, e.g. swapped RGB legs"},
            }, "required": ["id", "kind", "about"]}},
    },
    "required": ["parts"],
}
EXAMPLE = {
    "device": {"id": "sim", "name": "Simulated Uno", "fqbn": "arduino:avr:uno",
               "board": "uno", "firmware": "house-pins 2", "ready": True},
    "plan": {"name": "blinky",
             "parts": [{"id": "lamp", "kind": "led", "about": "your little lamp"}]},
}

GUIDE = """An Uno/Nano from a starter kit; the person may never have built anything.
Connect = flash the pins firmware (say it takes a minute), once per board.
Body = plan with 2–4 parts for a first robot (they can grow later); pins are chosen
automatically — never pick them yourself except to fix swapped legs.
Check = one part at a time: give that part's wiring steps, wait until the person
says it's wired, run the check, say exactly what should happen; say pins as the
board prints them (D9, A0, 5V, GND).
Gotchas from real builds:
- Unplug the USB before moving any wire: wiring while powered can hang the board
  (it stops answering); unplug/replug fixes it.
- Breadboard rails on each side are separate, and on many boards each rail is
  split in the middle (a gap in the printed line): bridge + to + and − to −.
  A part that does nothing often sits on an unpowered rail.
- Servo wires go by colour, never position: red 5V, brown/black GND,
  orange/yellow/white signal. Header pins can sit loose in a servo plug;
  male jumpers pushed into the plug grip better.
- RGB LED (common cathode): the longest leg goes to GND. Swapped colours are
  fixed in the plan (override pins), not by rewiring. Electrolytic capacitor:
  the striped leg is −.
- Isolate: when a part fails, re-test one that worked. If that fails too, it's
  power/ground, not the part. A multimeter (DC volts, black probe on −): a
  powered rail reads about 5 V; ~0–1 V means not connected.
""" + "\nParts catalog:\n" + json.dumps(parts_lib.catalog(), ensure_ascii=False)


def arduino_cli() -> str:
    found = os.environ.get("ARDUINO_CLI")
    if found:
        return found
    home_cli = Path.home() / "bin" / "arduino-cli"
    if home_cli.exists():
        return str(home_cli)
    return "arduino-cli"


def _probe(port: str, wait: float = 3.0) -> str:
    """Open exclusively, listen for HELLO, else ask INFO. Never resets a held board."""
    import serial

    ser = serial.Serial(port, 115200, timeout=0.1, exclusive=True)
    try:
        deadline = time.time() + wait
        buf = b""
        while time.time() < deadline:
            chunk = ser.read(64)
            if chunk:
                buf += chunk
                if b"HELLO" in buf:
                    line = buf.decode("utf-8", "replace")
                    return "house-pins 2" if "house-pins" in line else "unknown"
        ser.write(b"INFO\n")
        deadline = time.time() + 2
        buf = b""
        while time.time() < deadline:
            chunk = ser.read(64)
            if chunk:
                buf += chunk
                if b"INFO" in buf:
                    return "house-pins 2" if b"house-pins" in buf else "unknown"
        return "unknown"
    finally:
        ser.close()


def discover(taken=None) -> list[dict]:
    taken = taken or set()
    taken_real = {os.path.realpath(p) for p in taken if isinstance(p, str)}
    boards: list[dict] = []
    listed: list[tuple[str, str, str]] = []  # port, name, fqbn
    try:
        out = subprocess.run([arduino_cli(), "board", "list", "--format", "json"],
                             capture_output=True, text=True, timeout=30)
        data = json.loads(out.stdout or "{}")
        for entry in data.get("detected_ports", []):
            port = (entry.get("port") or {}).get("address", "")
            matches = entry.get("matching_boards") or []
            if matches:
                listed.append((port, matches[0].get("name", "Arduino"),
                               matches[0].get("fqbn", "")))
            elif port:
                listed.append((port, "Serial board", ""))
    except Exception:
        listed = [(p, "Serial board", "") for p in
                  sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))]
    for port, name, fqbn in listed:
        if not port.startswith("/dev/tty"):
            continue
        by_id = sorted(glob.glob("/dev/serial/by-id/*"))
        stable = next((p for p in by_id if os.path.realpath(p) == os.path.realpath(port)),
                      port)
        device = {"id": stable, "name": name, "fqbn": fqbn,
                  "board": SUPPORTED_FQBN.get(fqbn, "uno"),
                  "where": Path(port).name, "usb": port}
        if os.path.realpath(port) in taken_real:
            device.update(firmware="in use", ready=False, robot="a robot")
            boards.append(device)
            continue
        try:
            device["firmware"] = _probe(port)
        except Exception:
            device.update(firmware="in use", ready=False,
                          robot="a robot that isn't run by this workshop")
            boards.append(device)
            continue
        if fqbn and fqbn not in SUPPORTED_FQBN:
            device.update(ready=False, unsupported=True,
                          status=f"a {name} isn't supported yet "
                                 f"(supported: Arduino Uno, Nano)")
        else:
            device["ready"] = device["firmware"] == "house-pins 2"
        boards.append(device)
    sims = int(os.environ.get("HOUSE_SIMULATED_BOARDS", "0") or 0)
    for i in range(sims):
        suffix = "" if i == 0 else str(i + 1)
        device = {"id": f"sim{suffix}", "name": "Simulated Uno",
                  "fqbn": "arduino:avr:uno", "board": "uno",
                  "firmware": "house-pins 2", "ready": True, "where": "sim",
                  "status": "ready"}
        # taken maps device id -> robot (a set/list of ids also works).
        owner = None
        if isinstance(taken, dict):
            owner = taken.get(device["id"])
        elif device["id"] in taken or os.path.realpath(device["id"]) in taken_real:
            owner = "a robot"
        if owner:
            device.update(ready=False, robot=owner, status="in use")
        boards.append(device)
    return boards


def connect(device: dict, fields: dict):
    if str(device.get("id", "")).startswith("sim"):
        time.sleep(2)
        return ("the simulated board is ready", {**device, "ready": True})
    fqbn = device.get("fqbn") or ""
    if fqbn not in SUPPORTED_FQBN:
        raise ValueError(f"a {device.get('name', 'board')} isn't supported yet "
                         f"(supported: Arduino Uno, Nano)")
    port = device.get("usb") or device.get("id")
    sketch = Path(__file__).parent.parent.parent / "firmware" / "pins"
    started = time.time()
    try:
        proc = subprocess.run([arduino_cli(), "compile", "-b", fqbn, "-u", "-p", port,
                               str(sketch)], capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        raise ValueError("flashing took too long (over 4 minutes)")
    if time.time() - started > 240:
        raise ValueError("flashing took too long (over 4 minutes)")
    if proc.returncode != 0:
        tail = (proc.stdout + proc.stderr)[-600:]
        raise ValueError(f"flashing failed: {tail}")
    firmware = _probe(port)
    if firmware != "house-pins 2":
        raise ValueError(f"flashed, but the board doesn't answer as house-pins "
                         f"(it says: {firmware})")
    return ("flashed house-pins; the board answers as house-pins 2",
            {**device, "ready": True, "firmware": firmware})


def setup(config: dict, device: dict) -> dict:
    config["board"] = device.get("board", "uno")
    config["port"] = device.get("id")
    return config


def plan(config: dict, args: dict) -> str:
    listed = (args or {}).get("parts") or []
    seen: set[str] = set()
    new_parts: dict = {}
    for entry in listed:
        pid = (entry.get("id") or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", pid) or pid in seen:
            raise ValueError("part ids must be unique, non-empty and lowercase")
        seen.add(pid)
        previous = (config.get("parts") or {}).get(pid) or {}
        merged = dict(entry)
        if previous.get("kind") == entry.get("kind") and previous.get("pins"):
            merged["pins"] = {**previous["pins"], **(entry.get("pins") or {})}
        new_parts[pid] = merged
    if not new_parts:
        raise ValueError("plan at least one part")
    config["parts"] = new_parts
    piezos = [pid for pid, p in new_parts.items() if p.get("kind") == "piezo"]
    config["voice"] = piezos[0] if piezos else None
    parts_lib.assign_pins(config)
    problems, warnings = parts_lib.validate(config)
    lines = [f"PROBLEM: {p}" for p in problems]
    lines += [f"warning: {w}" for w in warnings]
    wires = parts_lib.wiring(config)
    board = parts_lib.BOARDS[config.get("board") or "uno"]
    for pid, part in new_parts.items():
        pins = {role: board.label(board.pin(raw))
                for role, raw in (part.get("pins") or {}).items()}
        lines.append(f"{pid} ({part.get('kind')}, pins {pins}): "
                     + " · ".join(wires.get(pid) or []))
    return "\n".join(lines)


def checks(config: dict, device: dict) -> dict:
    wires = parts_lib.wiring(config)
    out: dict = {}
    for pid, part in (config.get("parts") or {}).items():
        sig = json.dumps([part.get("kind"), part.get("pins") or {}], sort_keys=True)
        out[pid] = {"title": pid, "steps": wires.get(pid) or [], "sig": sig}
    return out


def problems(config: dict):
    if not config.get("parts"):
        return ([], [])
    return parts_lib.validate(config)


def body(config: dict, device: dict) -> dict:
    board = parts_lib.BOARDS[config.get("board") or "uno"]
    rows = []
    for pid, part in (config.get("parts") or {}).items():
        pins = part.get("pins") or {}
        if len(pins) > 1:
            shown = " · ".join(f"{role} {board.label(board.pin(raw))}"
                                for role, raw in pins.items())
        elif pins:
            shown = board.label(board.pin(next(iter(pins.values()))))
        else:
            shown = ""
        rows.append([pid, f"{part.get('kind')} · {part.get('about', '')}", shown])
    return {"head": ["Part", "What it is", "Pins"], "rows": rows}


async def check(device: dict, config: dict, check_id: str) -> dict:
    part_config = (config.get("parts") or {})[check_id]
    mini = {"name": config.get("name") or "check", "board": config.get("board", "uno"),
            "parts": {check_id: part_config}}
    found, _warnings = parts_lib.validate(mini)
    if found:
        raise ValueError("; ".join(found))
    from pins_body import SerialLink, SimBoard, build_part

    port = device.get("id") or config.get("port") or ""
    pins = SimBoard() if str(port).startswith("sim") else SerialLink(port)
    try:
        if isinstance(pins, SerialLink) and not pins.wait_hello(4.0):
            pins.info()
        part = build_part(check_id, part_config,
                          parts_lib.BOARDS[mini["board"]], pins,
                          lambda text: asyncio.sleep(0))
        part.setup()
        await asyncio.sleep(1)  # a moment to look
        observed = await asyncio.to_thread(part.self_test)
        result = {"expect": part.test_expect}
        if observed is not None:
            result["reading"] = observed
        return result
    except Exception as exc:
        raise ValueError(f"can't open the board ({exc}) — is a robot running "
                         f"on it, or was it unplugged?")
    finally:
        if isinstance(pins, SerialLink):
            pins.close()


def command(config_path: str, config: dict):
    driver = Path(__file__).parent.parent / "pins" / "pins_body.py"
    return ([sys.executable, "-u", str(driver), str(config_path)],
            {"HOUSE_PINS_PORT": config.get("port") or ""})
