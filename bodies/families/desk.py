"""The virtual desk family: two devices that live only in software.

A desk camera on a pan head and a desk lamp, sharing one drawn desk
(``bodies/desk/world.py``): the lamp's colour shows up in the camera's
photos, so robots built on them can see each other's work, and SAM 3.1
can find what's on the desk. It is there so anyone can meet the house
and try Add a robot without hardware.

Only a workshop with ``HOUSE_DESK=on`` finds the desk (the Docker
compose file sets it), so real workshops never offer it.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

NAME = "desk"
TITLE = "Virtual desk"
SCAN = "the virtual desk (a camera and a lamp that live in software)"
FIXTURE = None
CONNECT = {"label": "", "hint": "", "fields": []}
CHECK_HINT = "Everything happens on the virtual desk: each check shows you a picture."
PLAN_PARAMS: dict = {}

_DRIVERS = Path(__file__).resolve().parent.parent / "desk"

PARTS = {
    "camera": {
        "name": "Desk camera (virtual)",
        "body": ("A small camera on a pan head, looking at a desk from the front: a window "
                 "and a plant on the left, books, a blue mug and an orange in the middle, "
                 "a desk lamp and a rubber duck on the right. It can look and turn 40° "
                 "each way. It has no speaker of its own: it speaks through the house."),
        "abilities": [["look", "a photo of the desk where it's facing"],
                      ["turn", "pan 40° left or right of the middle"],
                      ["voice", "the house speaker"]],
    },
    "lamp": {
        "name": "Desk lamp (virtual)",
        "body": ("An angle-poise desk lamp on the right of a desk. Its light can be off, "
                 "warm, white, red, orange, yellow, green, blue, purple or pink, it can nod "
                 "its shade and blink. It can't see; the desk camera can see it. It has no "
                 "speaker of its own: it speaks through the house."),
        "abilities": [["light", "off, warm, white or one of seven colours"],
                      ["nod", "tips its shade"],
                      ["blink", "flashes its light"],
                      ["voice", "the house speaker"]],
    },
}

GUIDE = """A virtual desk that exists only in software, for trying the house without
hardware: a desk camera on a pan head (look, turn 40° each way) and a desk
lamp (light colour, nod, blink). Both share one drawn desk, so the lamp's
light shows up in the camera's photos. Connect: nothing to do. Body: a name,
a description and the person's wish for its personality; no parts. Check:
each check returns a picture to look at. Neither has a speaker; both speak
through the house."""

EXAMPLE = {
    "device": {"id": "desk:lamp", "name": "Desk lamp (virtual)", "part": "lamp",
               "where": "virtual desk", "ready": True, "status": "on the desk"},
    "plan": {"name": "lamp", "description": "A desk lamp on a virtual desk.",
             "personality": "bright and eager"},
}


def _world():
    if str(_DRIVERS) not in sys.path:
        sys.path.insert(0, str(_DRIVERS))
    import world
    return world


def enabled() -> bool:
    return os.environ.get("HOUSE_DESK", "").lower() in ("1", "on", "yes", "true")


def discover(taken=None) -> list[dict]:
    if not enabled():
        return []
    found = []
    for part, info in PARTS.items():
        device = {"id": f"desk:{part}", "name": info["name"], "part": part,
                  "where": "virtual desk", "body": info["body"], "ready": True,
                  "status": "on the desk"}
        if taken and device["id"] in taken:
            device.update(robot=taken[device["id"]], ready=False, status="in use")
        found.append(device)
    return found


def connect(device: dict, fields: dict):
    time.sleep(1)
    return ("it's on the desk and switched on", {**device, "ready": True})


def setup(config: dict, device: dict) -> dict:
    part = device.get("part") if device.get("part") in PARTS else "lamp"
    config["part"] = part
    config["device"] = {k: v for k, v in device.items()
                        if k not in ("status", "blocked", "manual", "robot")}
    config.setdefault("description", PARTS[part]["body"])
    return config


def plan(config: dict, args: dict) -> str:
    return ""


def problems(config: dict):
    if config.get("part") not in PARTS:
        return (["the desk has only a camera and a lamp"], [])
    return ([], [])


def body(config: dict, device: dict) -> dict:
    part = config.get("part") or device.get("part") or "lamp"
    rows = [list(r) for r in PARTS.get(part, PARTS["lamp"])["abilities"]]
    return {"head": ["Ability", "Details"], "rows": rows}


def checks(config: dict, device: dict) -> dict:
    if (config.get("part") or device.get("part")) == "camera":
        return {"photo": {"title": "photo", "steps": [], "sig": "1"},
                "turn": {"title": "turn", "steps": [], "sig": "1"}}
    return {"light": {"title": "light", "steps": [], "sig": "1"},
            "nod": {"title": "nod", "steps": [], "sig": "1"}}


async def check(device: dict, config: dict, check_id: str, config_path=None) -> dict:
    world = _world()
    if check_id == "photo":
        image = await asyncio.to_thread(world.view, 0)
        return {"expect": "a photo of the desk: a blue mug and an orange in the middle",
                "image": image}
    if check_id == "turn":
        image = await asyncio.to_thread(world.view, 40)
        return {"expect": "turned right: the lamp and the rubber duck", "image": image}
    if check_id == "light":
        before = world.load()["lamp"]
        lit = {**world.load(), "lamp": {**before, "color": "green"}}
        image = await asyncio.to_thread(world.closeup, lit)
        return {"expect": "the lamp glows green", "image": image}
    if check_id == "nod":
        before = world.load()["lamp"]
        nodded = {**world.load(), "lamp": {**before, "tilt": 25}}
        image = await asyncio.to_thread(world.closeup, nodded)
        return {"expect": "the shade is tipped down", "image": image}
    raise ValueError(f"unknown check '{check_id}'")


def command(config_path: str, config: dict):
    driver = _DRIVERS / "desk_body.py"
    return ([sys.executable, "-u", str(driver), str(config_path)], {})
