"""The ONVIF family (spec 05): IP cameras with pan/tilt heads.

Finds cameras (WS-Discovery plus a LAN sweep), logs in with the Camera
Account (the password stays on the workshop, in its secrets), checks the
camera, the pan, the tilt and the home view, and runs the camera driver.
The contract is spec 09's; hooks mirror the pins family's shape.
"""
from __future__ import annotations

import asyncio
import socket
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "onvif"))
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import net as net_lib  # noqa: E402
from onvif_body import Stream, load_secret, save_secret  # noqa: E402
from onvif_client import (  # noqa: E402
    OnvifError, device_xaddr, discover_ws, login_at, probe_datetime)

NAME = "onvif"
TITLE = "IP camera"
SCAN = "cameras on the network"
FIXTURE = None
# on the Wi-Fi, not on a cable: any workshop that reaches it and holds its account can run it
REACH = "network"
CONNECT = {
    "label": "Connect the camera",
    "hint": "It needs a Camera Account: Tapo app → the camera → ⚙️ → Advanced "
            "Settings → Camera Account (on some firmware also Me → Tapo Lab → "
            "Third-Party Compatibility on). The account stays on this workshop, "
            "never in the hub. It is not your TP-Link cloud login.",
    "fields": [
        {"id": "address", "label": "Address (IP)", "secret": False},
        {"id": "username", "label": "Camera account username", "secret": False},
        {"id": "password", "label": "Camera account password", "secret": True},
    ],
}
CHECK_HINT = "Make sure the camera's privacy mode is off."
PLAN_PARAMS = {"type": "object", "properties": {}, "required": []}
EXAMPLE = {
    "device": {"id": "onvif:192.0.2.39", "name": "Tapo C225",
               "host": "192.0.2.39", "where": "192.0.2.39", "ptz": True,
               "pan_degrees": 328, "tilt_degrees": 96, "pan_sign": -1,
               "backlash": 0.016, "home": [0.0, 0.0], "home_after": 90,
               "ready": True},
    "plan": {},
}

# Measured heads, keyed on the lower-cased model name (spec 05).
HEADS = {
    "c225": {"pan_degrees": 328, "tilt_degrees": 96, "pan_sign": -1,
             "backlash": 0.016},
    "tapo c225": {"pan_degrees": 328, "tilt_degrees": 96, "pan_sign": -1,
                  "backlash": 0.016},
}
DEFAULT_HEAD = {"pan_degrees": 360, "tilt_degrees": 90, "pan_sign": 1,
                "backlash": 0}

GUIDE = """An IP camera (Tapo C225 and friends) that becomes a robot with eyes.
Connect = the Camera Account form; the password stays on the workshop and must
never be typed into this conversation: if the person starts typing one here,
stop them and point at the form. Checks, one at a time: a fresh photo, pan
45° right and back, tilt 20° up and back, then the home view — for that one
the person first aims the camera at what it should watch (in the Tapo app or
by hand), and the check remembers exactly where that is.
Gotchas:
- Without a Camera Account the camera shows as blocked: Tapo app → the camera
  → ⚙️ → Advanced Settings → Camera Account; on some firmware also Me → Tapo
  Lab → Third-Party Compatibility.
- Privacy mode must be off, or every picture is black.
- The camera guides others from fresh photos only; the driver reopens a
  stalled stream by itself.
"""

ONVIF_PORTS = {2020, 80, 8000, 8080}
TAPO_APP_PORTS = {443, 8800}
BLOCKED_STATUS = (
    "a Tapo camera, but it doesn't speak ONVIF yet: give it a Camera Account "
    "(Tapo app → the camera → ⚙️ → Advanced Settings → Camera Account; on some "
    "firmware also Me → Tapo Lab → Third-Party Compatibility), then Look again")


def head_for(model: str) -> dict:
    return dict(HEADS.get((model or "").strip().lower(), DEFAULT_HEAD))


def discover(taken=None) -> list[dict]:
    taken = taken or {}
    devices: dict[str, dict] = {}
    for dev in discover_ws():
        host = dev["host"]
        devices[host] = {
            "id": f"onvif:{host}", "name": dev.get("name") or "ONVIF camera",
            "host": host, "xaddr": dev.get("xaddr") or device_xaddr(host, 2020),
            "where": host, "family": NAME}
    open_map = net_lib.sweep(ONVIF_PORTS | TAPO_APP_PORTS)
    for host, ports in sorted(open_map.items(),
                              key=lambda kv: tuple(int(p) for p in kv[0].split("."))):
        if host in devices:
            continue
        onvif_ports = sorted(ports & ONVIF_PORTS)
        answered = False
        for port in onvif_ports:
            if probe_datetime(device_xaddr(host, port)):
                answered = True
                break
        if answered:
            model = net_lib.tapo_model_from_name(net_lib.name(host))
            devices[host] = {
                "id": f"onvif:{host}", "name": model or "ONVIF camera",
                "host": host, "xaddr": device_xaddr(host, onvif_ports[0]),
                "where": host, "family": NAME}
            continue
        if TAPO_APP_PORTS <= ports:
            cert = net_lib.tls_certificate(host, 443)
            if b"TPRI" in cert:
                model = net_lib.tapo_model_from_name(net_lib.name(host))
                devices[host] = {
                    "id": f"onvif:{host}",
                    "name": model or "Tapo camera",
                    "host": host, "where": host, "family": NAME,
                    "ready": False, "blocked": True, "status": BLOCKED_STATUS}
    out = []
    for host in sorted(devices, key=lambda h: tuple(int(p) for p in h.split("."))):
        dev = devices[host]
        owner = taken.get(dev["id"]) if isinstance(taken, dict) else None
        if owner:
            dev.update(ready=False, robot=owner, status="in use")
        elif not dev.get("blocked"):
            dev.setdefault("ready", False)
            dev.setdefault("status", "needs its account")
        out.append(dev)
    out.append({"id": "onvif:manual", "name": "A camera by its address",
                "host": "", "where": "type its IP", "family": NAME,
                "manual": True, "ready": False, "status": "needs its account"})
    return out


def connect(device: dict, fields: dict, config_path=None):
    host = (fields.get("address") or device.get("host") or "").strip()
    username = (fields.get("username") or "").strip()
    password = fields.get("password") or ""
    if not host or not username or not password:
        raise ValueError("the camera needs its address and its Camera Account "
                         "(username and password)")
    camera = login_at(host, username, password,
                      xaddr=device.get("xaddr") if device.get("host") == host else None)
    model = camera.info.get("model") or device.get("name") or "camera"
    maker = camera.info.get("manufacturer") or ""
    name = f"{maker} {model}".strip() if maker and maker.lower() not in model.lower() \
        else model
    head = head_for(model)
    built = {
        "id": f"onvif:{host}", "name": name, "host": host,
        "xaddr": camera.xaddr, "where": host, "ptz": camera.has_ptz,
        **head, "ready": True,
    }
    if config_path is not None:
        save_secret(Path(config_path), built["id"],
                    {"username": username, "password": password})
    kind = "it has a pan/tilt head" if camera.has_ptz else "fixed, no pan/tilt"
    return (f"logged in to the {name} ({kind})", built)


def ready_here(device: dict, config_path) -> str:
    """"" when this workshop could run the camera right now, else why not."""
    if not load_secret(Path(config_path), device.get("id", "")).get("username"):
        return "its camera account isn't on this workshop"
    host = device.get("host") or ""
    port = urllib.parse.urlparse(device.get("xaddr") or "").port or 80
    try:
        socket.create_connection((host, port), timeout=3).close()
    except OSError:
        return f"can't reach the camera at {host or 'no address'}"
    return ""


def export_secret(device: dict, config_path) -> dict:
    return load_secret(Path(config_path), device.get("id", ""))


def import_secret(device: dict, config_path, secret: dict) -> None:
    if secret.get("username") and secret.get("password"):
        save_secret(Path(config_path), device.get("id", ""),
                    {"username": secret["username"], "password": secret["password"]})


def setup(config: dict, device: dict) -> dict:
    config["device"] = {k: v for k, v in device.items()
                        if k not in ("status", "blocked", "manual", "robot")}
    return config


def plan(config: dict, args: dict) -> str:
    return ""  # a camera's body is its device; nothing to plan


def checks(config: dict, device: dict) -> dict:
    out = {"camera": {"title": "camera",
                      "steps": ["Make sure the camera's privacy mode is off."],
                      "sig": "camera"}}
    if device.get("ptz"):
        out["pan"] = {"title": "pan",
                      "steps": ["Watch the camera: it will turn right about 45°, then back."],
                      "sig": "pan"}
        out["tilt"] = {"title": "tilt",
                       "steps": ["Watch the camera: it will tilt up about 20°, then back."],
                       "sig": "tilt"}
        out["home"] = {"title": "home",
                       "steps": ["Aim the camera at what it should watch (in the Tapo app "
                                 "or by hand), then run this check."],
                       "sig": "home"}
    return out


def problems(config: dict):
    device = config.get("device") or {}
    if not device.get("host"):
        return (["the camera has no address yet"], [])
    return ([], [])


def body(config: dict, device: dict) -> dict:
    name = device.get("name") or "camera"
    host = device.get("host") or ""
    rows = [["camera", f"{name} · {host}", ""]]
    if device.get("ptz"):
        rows.append(["pan/tilt",
                     f"turns about {int(device.get('pan_degrees') or 360)}° "
                     f"and tilts about {int(device.get('tilt_degrees') or 90)}°", ""])
    rows.append(["home view",
                 "remembered" if device.get("home")
                 else "not set yet (the last check)", ""])
    return {"head": ["Part", "What it is", "Pins"], "rows": rows}


async def run_check(device: dict, config: dict, check_id: str,
                    config_path=None) -> dict:
    """One build check. The Stream is stopped right after, per spec."""
    from onvif_body import heading_text, move_units

    if config_path is not None:
        secret = load_secret(Path(config_path), device.get("id", ""))
        username, password = secret.get("username", ""), secret.get("password", "")
    else:
        username = password = ""
    if not username:
        raise ValueError("the camera account isn't on this workshop yet")
    result: dict = {}

    def blocking() -> dict:
        camera = login_at(device["host"], username, password,
                          xaddr=device.get("xaddr") or None)
        stream = Stream(camera.rtsp_url(), awake=0)
        try:
            if check_id == "camera":
                jpeg = stream.jpeg()
                return {"expect": "the photo shows what the camera is facing",
                        "image": jpeg}
            if check_id == "pan":
                last_dir: dict = {}
                ux, uy = move_units(45, 0, device, last_dir)
                camera.ptz_relative_move(ux, uy)
                time.sleep(0.4)
                camera.wait_still()
                time.sleep(1)
                ux, uy = move_units(-45, 0, device, last_dir)
                camera.ptz_relative_move(ux, uy)
                time.sleep(0.4)
                camera.wait_still()
                return {"expect": "it turns right about 45°, then back"}
            if check_id == "tilt":
                last_dir = {}
                ux, uy = move_units(0, 20, device, last_dir)
                camera.ptz_relative_move(ux, uy)
                time.sleep(0.4)
                camera.wait_still()
                time.sleep(1)
                ux, uy = move_units(0, -20, device, last_dir)
                camera.ptz_relative_move(ux, uy)
                time.sleep(0.4)
                camera.wait_still()
                return {"expect": "it tilts up about 20°, then back down"}
            if check_id == "home":
                time.sleep(0.4)
                pos = camera.wait_still()
                device["home"] = [round(pos[0], 6), round(pos[1], 6)]
                if isinstance(config.get("device"), dict):
                    config["device"]["home"] = device["home"]
                jpeg = stream.jpeg()
                return {"expect": "the photo shows its home view — run it again if not",
                        "image": jpeg,
                        "home": device["home"]}
            raise ValueError(f"unknown check '{check_id}'")
        finally:
            stream.stop()

    result = await asyncio.to_thread(blocking)
    return result


# spec 09 calls this `check`; keep one name working both ways
async def check_entry(device: dict, config: dict, check_id: str, config_path=None):
    return await run_check(device, config, check_id, config_path=config_path)


check = check_entry  # noqa: F811


def command(config_path: str, config: dict):
    driver = Path(__file__).parent.parent / "onvif" / "onvif_body.py"
    return ([sys.executable, "-u", str(driver), str(config_path)], {})
