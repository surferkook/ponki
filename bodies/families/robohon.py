"""The RoBoHoN family (spec 08): a 20 cm Sharp humanoid with his own
control server on this machine (http://localhost:8091). The house only
calls that server's HTTP API; nothing is flashed or wired.

Family HTTP is the standard library only (GET 5 s, POST 30 s).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

NAME = "robohon"
TITLE = "RoBoHoN"
CONNECT = {"label": "", "hint": "", "fields": []}
CHECK_HINT = "Take him out of the cradle if you want to see the full-body gestures."
PLAN_PARAMS: dict = {}

BODY = ("A 20 cm Sharp RoBoHoN humanoid: walks, dances, waves, sings, LED eyes, "
        "a camera in his forehead, a speaker and a mic. When docked in his "
        "charging cradle his legs are locked — only upper-body gestures work there.")

GUIDE = """A real 20 cm Sharp RoBoHoN humanoid with his own control server, which
the house uses as it is. He speaks through his own speaker in his own
voices, plays gestures (head, arms, legs, LED eyes), has a forehead
camera, and knows whether he is standing, sitting or docked — docked in his
charging cradle means only upper-body gestures work, and only a person can lift
him out. Connect: nothing to do (offline means asleep or off Wi-Fi; his server
shows why). Body: a name, a description and the person's wish for his
personality; no parts. Check: voice, gesture, camera — in the cradle the wave
is an upper-body wave."""

EXAMPLE = {
    "device": {"id": "robohon:http://localhost:8091", "name": "RoBoHoN",
               "web": "http://localhost:8091", "gestures": 56,
               "where": "localhost:8091", "ready": True,
               "status": "online (bridge)"},
    "plan": {"name": "robohon", "description": "A 20 cm Sharp RoBoHoN humanoid.",
             "personality": "warm and cheeky"},
}


def _web(device: dict | None = None) -> str:
    if device and device.get("web"):
        return str(device["web"]).rstrip("/")
    return os.environ.get("HOUSE_ROBOHON_WEB", "http://localhost:8091").rstrip("/")


def _get(web: str, path: str, timeout: float = 5.0):
    with urllib.request.urlopen(web + path, timeout=timeout) as resp:
        return resp.status, resp.read()


def _post(web: str, path: str, payload: dict | None = None, timeout: float = 30.0):
    data = json.dumps(payload).encode() if payload is not None else b""
    req = urllib.request.Request(web + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def _get_json(web: str, path: str, timeout: float = 5.0):
    _, data = _get(web, path, timeout)
    return json.loads(data or b"null")


def discover(taken=None) -> list[dict]:
    web = _web()
    try:
        status = _get_json(web, "/api/status", timeout=2.0)
        gestures = _get_json(web, "/api/gestures", timeout=5.0)
    except Exception:
        return []  # no control server on this machine
    online = bool(status.get("online"))
    via = status.get("via") or ""
    device = {
        "id": f"robohon:{web}", "name": "RoBoHoN", "web": web,
        "gestures": len(gestures) if isinstance(gestures, list) else 0,
        "where": web.split("://", 1)[-1], "body": BODY,
        "ready": online,
        "status": (f"online ({via})" if online and via else
                   "online" if online else
                   "his control server is up, but he's offline"),
    }
    if taken and device["id"] in taken:
        device["robot"] = taken[device["id"]]
    return [device]


def connect(device: dict, fields: dict, config_path=None) -> dict:
    found = discover()
    if not found:
        raise ValueError("his control server doesn't answer anymore")
    device = found[0]
    if not device["ready"]:
        raise ValueError(device["status"])
    return {"ready": True, "device": device, "text": "he's online"}


def setup(config: dict, device: dict) -> dict:
    config["web"] = device.get("web") or _web(device)
    config["device"] = {k: v for k, v in device.items()
                        if k not in ("status", "blocked", "manual", "robot")}
    config.setdefault("display_name", "RoBoHoN")
    return config


def plan(config: dict, args: dict) -> str:
    return ""


def problems(config: dict) -> list[str]:
    return []


def body(config: dict, device: dict) -> str:
    n = device.get("gestures", 0)
    return "\n".join([
        "| Ability | Details |",
        "|---|---|",
        "| voice | his own speaker and his own voices |",
        f"| gestures | {n} gestures: head, arms, legs, LED eyes |",
        "| camera | in his forehead |",
        "| pose | standing / sitting / docked in the cradle |",
    ])


def checks(config: dict, device: dict) -> list[dict]:
    return [
        {"title": "voice", "kind": "voice", "sig": "1", "id": "voice",
         "steps": [], "expect": 'he says "Hello! Can you hear me?" through his own speaker'},
        {"title": "gesture", "kind": "gesture", "sig": "1", "id": "gesture",
         "steps": [], "expect": "he waves (in the cradle: an upper-body wave)"},
        {"title": "camera", "kind": "camera", "sig": "1", "id": "camera",
         "steps": [], "expect": "the photo shows what's in front of him"},
    ]


async def check(device: dict, config: dict, check_id: str, config_path=None) -> dict:
    import asyncio

    web = _web(device)
    if check_id == "voice":
        await asyncio.to_thread(
            _post, web, "/api/speak",
            {"text": "Hello! Can you hear me?", "voice": "kokoro:af_heart",
             "gesture": ""})
        return {"expect": 'he says "Hello! Can you hear me?" through his own speaker'}
    if check_id == "gesture":
        _, data = await asyncio.to_thread(_post, web, "/api/gesture/wave_hello")
        result = json.loads(data or b"{}")
        if not result.get("ok"):
            raise ValueError(result.get("reason") or "the wave didn't play")
        return {"expect": "he waves (in the cradle: an upper-body wave)"}
    if check_id == "camera":
        _, data = await asyncio.to_thread(_post, web, "/api/look")
        if not json.loads(data or b"{}").get("ok"):
            raise ValueError("his camera didn't return a photo")
        _, photo = await asyncio.to_thread(_get, web, "/vision/latest.jpg", 10.0)
        import base64
        return {"expect": "the photo shows what's in front of him",
                "image": base64.b64encode(photo).decode()}
    raise ValueError(f"unknown check '{check_id}'")


def command(config_path: str, config: dict):
    driver = Path(__file__).parent.parent / "robohon" / "robohon_body.py"
    return ([sys.executable, "-u", str(driver), str(config_path)],
            {"HOUSE_ROBOHON_WEB": config.get("web") or _web()})
