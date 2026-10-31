"""The mBot2 family (spec 07): Makeblock mBot2 rovers (CyberPi).

Found over USB (CH340, vendor 1a86 — shared with Arduino clones, so we
ask the board who it is), put on the Wi-Fi by uploading our rover program
over USB, then wireless: the rover dials its workshop over TCP.
CLAIMS_PORTS: the workshop scans this family first, because opening a
CH340 port resets the board.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "mbot2"))

from cyberpi_link import CyberPi  # noqa: E402
from rover_link import (  # noqa: E402
    free_port, load_secret, my_address, new_key, render, save_secret,
    version, wait_for_rover, with_rover)

NAME = "mbot2"
TITLE = "mBot2 rover"
SCAN = "mBot2 rovers on USB"
FIXTURE = None
CLAIMS_PORTS = True
PLAN_PARAMS = {}
CONNECT = {
    "label": "Put it on the Wi-Fi",
    "hint": "Your Wi-Fi's name and password (a 2.4 GHz network: the mBot2 "
            "can't join 5 GHz). They go into the rover's own program and "
            "stay on this workshop; the builder never sees the password.",
    "fields": [
        {"id": "ssid", "label": "Wi-Fi name", "secret": False},
        {"id": "password", "label": "Wi-Fi password", "secret": True},
    ],
}
CHECK_HINT = ("Give it room before the drive and turn checks: away from "
              "the table edge, or wheels in the air.")
EXAMPLE = {
    "device": {"id": "mbot2:ECE3342F1308", "name": "Makeblock mBot2",
               "port": "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0",
               "usb": "1-1.1", "mac": "ECE3342F1308", "firmware": "44.01.013",
               "battery": 100, "where": "ttyUSB0 (USB)",
               "program": "house-rover 2", "host": "192.0.2.37",
               "rover_port": 8220, "ready": True,
               "status": "on the Wi-Fi (192.0.2.60) · battery 100%"},
    "plan": {"name": "Vroom-Vroom",
             "description": "A small Makeblock mBot2 rover with encoder "
                            "motors, a colour screen, five LEDs, a speaker, "
                            "a gyro and an ultrasonic distance sensor.",
             "personality": "cheerful and careful"},
}
GUIDE = """A small rover with two encoder motors (exact centimetres and degrees), a
colour screen, five LEDs, a speaker, a gyro, and an ultrasonic distance
sensor at the front; nothing to wire. Found over USB-C, switched on (switch
on the side of the base), shows as "Makeblock mBot2"; asking it who it is
takes about 10 s, because opening the port resets it.
Connect = the person types the Wi-Fi name and password into the form, never
into this conversation (stop them if they start). Upload takes ~5 s, then
up to 90 s for it to call in; its screen says what it's doing; 2.4 GHz
only; only program slot 1 is replaced. After Connect, tell them the cable
can come out.
Body = a name, a description and a personality wish; there are no parts.
Checks: screen + sound, sensors, then drive and turn after asking for room
(30 cm free in front, away from any table edge, or wheels in the air);
the moves come back to where they started.
"""


def devices_file() -> Path:
    return Path(os.environ.get("HOUSE_DATA_DIR", "data")) / "devices" \
        / "mbot2.json"


def _known_sockets() -> dict:
    try:
        data = json.loads(devices_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _remember_socket(socket_name: str, device_id: str) -> None:
    known = _known_sockets()
    known[socket_name] = device_id
    path = devices_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(known, indent=1), encoding="utf-8")
    tmp.replace(path)


def _serial_ports() -> list[dict]:
    """ttyUSB/ttyACM nodes with their USB socket names and by-id paths."""
    out = []
    for node in sorted(Path("/sys/class/tty").glob("ttyUSB*")) \
            + sorted(Path("/sys/class/tty").glob("ttyACM*")):
        device_link = node / "device"
        try:
            usb_dir = device_link.resolve()
        except OSError:
            continue
        while usb_dir != usb_dir.parent and not (usb_dir / "idVendor").exists():
            usb_dir = usb_dir.parent
        vendor = ""
        if (usb_dir / "idVendor").exists():
            vendor = (usb_dir / "idVendor").read_text().strip()
        dev_node = f"/dev/{node.name}"
        by_id = ""
        for link in Path("/dev/serial/by-id").glob("*") if \
                Path("/dev/serial/by-id").exists() else []:
            try:
                if link.resolve() == Path(dev_node):
                    by_id = str(link)
                    break
            except OSError:
                continue
        out.append({"node": dev_node, "port": by_id or dev_node,
                    "usb": usb_dir.name, "vendor": vendor})
    return out


def identify(port: str) -> dict | None:
    """Ask a board who it is. None = not ours (or not answering)."""
    try:
        with CyberPi(port) as board:
            # the board is slow between script calls: ask patiently, and a
            # slow firmware/battery answer must not lose the board
            def ask(code):
                try:
                    value = board.script(code, timeout=10)
                except TimeoutError:
                    value = None
                time.sleep(0.5)
                return value

            shield = ask("cyberpi.get_shield()")
            mac = ask("cyberpi.get_mac_address()")
            firmware = ask("cyberpi.get_firmware_version()")
            battery = ask("cyberpi.get_battery()")
            printed = board.output()
    except TimeoutError:
        return None
    if not mac:
        return None
    mac_text = str(mac).replace(":", "").upper()
    program = "house-rover" if "HOUSE " in printed else "other"
    return {"shield": str(shield), "mac": mac_text,
            "firmware": str(firmware), "battery": battery,
            "program": program}


def discover(taken=None) -> list[dict]:
    taken = taken or {}
    known = _known_sockets()
    devices: list[dict] = []
    for entry in _serial_ports():
        if entry["vendor"] != "1a86":
            continue  # not a CH340: not ours to claim
        socket_name = entry["usb"]
        device_id = known.get(socket_name)
        if device_id and device_id in taken:
            # a running rover: report without opening (opening resets it)
            devices.append({"id": device_id, "name": "Makeblock mBot2",
                            "port": entry["port"], "usb": socket_name,
                            "where": f"{Path(entry['node']).name} (USB)",
                            "family": NAME, "ready": False,
                            "robot": taken[device_id], "status": "in use"})
            continue
        try:
            info = identify(entry["port"])
        except Exception as exc:
            devices.append({"id": f"mbot2:{Path(entry['node']).name}",
                            "name": "Makeblock CyberPi",
                            "port": entry["port"], "usb": socket_name,
                            "where": f"{Path(entry['node']).name} (USB)",
                            "family": NAME, "ready": False,
                            "unsupported": True, "status": f"couldn't ask it: {exc}"})
            continue
        if info is None:
            continue  # another family's device: skip silently
        device_id = f"mbot2:{info['mac']}"
        _remember_socket(socket_name, device_id)
        if info["shield"] != "mbot2":
            devices.append({"id": device_id, "name": "Makeblock CyberPi",
                            "port": entry["port"], "usb": socket_name,
                            "mac": info["mac"], "family": NAME, "ready": False,
                            "unsupported": True,
                            "status": "a CyberPi on its own (not in an mBot2 "
                                      "base): not supported yet"})
            continue
        device = {"id": device_id, "name": "Makeblock mBot2",
                  "port": entry["port"], "usb": socket_name,
                  "mac": info["mac"], "firmware": info["firmware"],
                  "battery": info["battery"], "family": NAME, "ready": False,
                  "where": f"{Path(entry['node']).name} (USB)",
                  "status": f"battery {info['battery']}% · needs to join "
                            f"the Wi-Fi (Connect)"}
        if info["program"].startswith("house-rover"):
            device["program"] = f"house-rover {version()}"
        if device_id in taken:
            device.update(robot=taken[device_id], status="in use")
        devices.append(device)
    return devices


def _wifi_networks() -> list[str]:
    try:
        out = subprocess.run(["nmcli", "-t", "-f", "SSID,FREQ", "dev", "wifi",
                              "list"], capture_output=True, text=True,
                             timeout=15).stdout
    except Exception:
        return []
    names = []
    for line in out.splitlines():
        if ":" not in line:
            continue
        ssid = line.rsplit(":", 1)[0].replace("\\:", ":")
        if ssid:
            names.append(ssid)
    return names


def _check_ssid(ssid: str, networks: list[str]) -> None:
    if not networks or ssid in networks:
        return
    near = difflib.get_close_matches(ssid, networks, n=1, cutoff=0.75)
    if near:
        raise ValueError(f"this workshop doesn't see a Wi-Fi called '{ssid}' "
                         f"— did you mean '{near[0]}'?")


def _listen(board: CyberPi, said: list[str], stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            text = board.output()
        except Exception:
            return
        for line in text.splitlines():
            if "HOUSE " in line:
                said.append(line.split("HOUSE ", 1)[1])
        time.sleep(0.2)


def connect(device: dict, fields: dict, config_path=None):
    ssid = (fields.get("ssid") or "").strip()
    password = fields.get("password") or ""
    if not ssid or not password:
        raise ValueError("the Wi-Fi name and password are both needed")
    _check_ssid(ssid, _wifi_networks())
    host = my_address()
    key = new_key()
    port = int(device.get("rover_port") or free_port())
    program = render(ssid, password, host, port, key)
    board = CyberPi(device["port"])
    try:
        board.upload(program)
        board.restart(ready=False)  # send nothing: let the program run
        said: list[str] = []
        stop = threading.Event()
        listener = threading.Thread(target=_listen, args=(board, said, stop),
                                    daemon=True)
        listener.start()
        try:
            import asyncio

            hello = asyncio.run(wait_for_rover(port, key, 90))
        except Exception as exc:
            last = said[-1] if said else "nothing yet"
            hint = ""
            if any("Wi-Fi" in line for line in said):
                hint = (" Check the Wi-Fi name and password (and that it's "
                        "a 2.4 GHz network).")
            raise ValueError(
                f"the program is on the rover, but it didn't call in over "
                f"Wi-Fi ({exc}). Its last words: '{last}'.{hint}")
        finally:
            stop.set()
    finally:
        board.close()
    save_secret(device["id"], {"ssid": ssid, "password": password, "key": key},
                Path(config_path) if config_path else None)
    updated = {**device, "program": f"house-rover {version()}", "host": host,
               "rover_port": port, "ip": hello.get("from"),
               "battery": hello.get("battery"), "ready": True,
               "status": f"on the Wi-Fi ({hello.get('from')}) · battery "
                         f"{hello.get('battery')}%"}
    return (f"the rover's program is on, and it called in over Wi-Fi "
            f"from {hello.get('from')} after {hello.get('after')} s. "
            f"The USB cable can come out now.", updated)


def update(device: dict, config_path=None):
    secret = load_secret(device.get("id", ""),
                         Path(config_path) if config_path else None)
    if not secret.get("ssid") or not secret.get("key"):
        raise ValueError("it was never put on the Wi-Fi from this workshop — "
                         "Connect it first")
    if not device.get("rover_port"):
        raise ValueError("it was never put on the Wi-Fi from this workshop — "
                         "Connect it first")
    port_path = device.get("port") or ""
    if not port_path or not Path(port_path).exists():
        raise ValueError("plug it into this workshop with its USB cable "
                         "first (and switch it on)")
    host = my_address()
    program = render(secret["ssid"], secret["password"], host,
                     int(device["rover_port"]), secret["key"])
    board = CyberPi(port_path)
    try:
        board.upload(program)
        board.restart(ready=False)
        said: list[str] = []
        stop = threading.Event()
        listener = threading.Thread(target=_listen, args=(board, said, stop),
                                    daemon=True)
        listener.start()
        deadline = time.time() + 90
        try:
            while time.time() < deadline:
                if any("with the house" in line for line in said):
                    break
                time.sleep(0.5)
            else:
                last = said[-1] if said else "nothing yet"
                raise ValueError("the new program is on, but it didn't call "
                                 f"back in (its last words: '{last}')")
        finally:
            stop.set()
    finally:
        board.close()
    updated = {**device, "program": f"house-rover {version()}", "host": host}
    return (f"the rover runs house-rover {version()} now and is back "
            f"with the house; the USB cable can come out", updated)


def setup(config: dict, device: dict) -> dict:
    return config


def plan(config: dict, args: dict) -> str:
    return ""


def checks(config: dict, device: dict) -> dict:
    room = "Give it room: 30 cm free in front of it, away from any table " \
           "edge — or hold it up with its wheels in the air."
    return {
        "screen": {"title": "screen", "steps": ["Watch its screen."],
                   "sig": "screen"},
        "sensors": {"title": "sensors",
                    "steps": ["It reads its battery, gyro and distance sensor."],
                    "sig": "sensors"},
        "drive": {"title": "drive", "steps": [room], "sig": "drive"},
        "turn": {"title": "turn", "steps": [room], "sig": "turn"},
        "eyes": {"title": "eyes",
                 "steps": ["Watch the two eyes at its front."],
                 "sig": "eyes"},
    }


def problems(config: dict):
    device = config.get("device") or {}
    if not device.get("rover_port"):
        return (["it isn't on the Wi-Fi yet (Connect)"], [])
    return ([], [])


def body(config: dict, device: dict) -> dict:
    link = (f"Wi-Fi → its workshop ({device.get('host')}:{device.get('rover_port')})"
            if device.get("rover_port") else "USB only so far (Connect puts "
                                           "it on the Wi-Fi)")
    return {"head": ["Part", "What it is", "Pins"], "rows": [
        ["wheels", "two encoder motors: exact centimetres and degrees", ""],
        ["screen", "a colour screen that shows faces and words", ""],
        ["lights", "five LEDs", ""],
        ["speaker", "beeps and tones", ""],
        ["eyes", "the ultrasonic distance sensor's blue LEDs", ""],
        ["sensors", "battery, gyro heading, distance in front", ""],
        ["link", link, ""],
    ]}


async def check(device: dict, config: dict, check_id: str,
                config_path=None) -> dict:
    secret = load_secret(device.get("id", ""),
                         Path(config_path) if config_path else None)
    key = secret.get("key", "")
    port = int(device.get("rover_port") or 8220)

    async def run(link) -> dict:
        if check_id == "screen":
            await link.call("face", mood="happy", timeout=5)
            await link.call("light", color="green", timeout=5)
            await link.call("beep", hz=880, s=0.5, timeout=10)
            await asyncio.sleep(4)  # a moment to look; the face stays up
            await link.call("light", color="off", timeout=5)
            return {"expect": "a happy face (^_^) on its screen, its lights "
                              "green for a moment, and a beep"}
        if check_id == "sensors":
            ret = await link.call("sense", timeout=5)
            dist = ret.get("distance_cm")
            where = f"{dist} cm to the nearest thing in front" \
                if dist is not None else "nothing seen in front (or no " \
                                         "distance sensor)"
            return {"expect": "the readings make sense (put a hand in front "
                              "of it and run it again: the distance drops)",
                    "reading": f"battery {ret.get('battery')}% · heading "
                               f"{ret.get('heading')}° · {where}"}
        if check_id == "drive":
            await link.call("drive", cm=10, speed=30, timeout=30)
            await link.call("drive", cm=-10, speed=30, timeout=30)
            return {"expect": "it drives 10 cm forward, then back to where "
                              "it was"}
        if check_id == "turn":
            from rover_link import TURN_SIGN

            await link.call("reset_heading", timeout=5)
            await link.call("turn", degrees=TURN_SIGN * 90, speed=30,
                            timeout=30)
            ret = await link.call("sense", timeout=5)
            heading = ret.get("heading")
            await link.call("turn", degrees=-TURN_SIGN * 90, speed=30,
                            timeout=30)
            return {"expect": "it turns a quarter to ITS left "
                              "(counter-clockwise seen from above), then back",
                    "reading": f"heading after the first turn: {heading}°"}
        if check_id == "eyes":
            await link.call("eyes", level=100, timeout=5)
            await asyncio.sleep(1.5)
            await link.call("eyes", expression="wink", timeout=10)
            await link.call("eyes", level=0, timeout=5)
            return {"expect": "the two eyes at its front light up blue, "
                              "then wink, then go dark"}
        raise ValueError(f"unknown check '{check_id}'")

    return await with_rover(port, key, run, timeout=20)


def command(config_path: str, config: dict):
    driver = Path(__file__).parent.parent / "mbot2" / "mbot2_body.py"
    return ([sys.executable, "-u", str(driver), str(config_path)], {})
