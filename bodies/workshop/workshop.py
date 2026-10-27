"""The workshop fixture: a workbench where devices become robots.

Run on the machine with the hardware:  python workshop.py
Settings: HOUSE_HUB / HOUSE_TOKEN (the body SDK), HOUSE_WORKSHOP_NAME,
HOUSE_WORKSHOP_FAMILIES (comma list; default every built-in family
plus the promoted local ones), HOUSE_WORKSHOP_SIM=<n> (that many
simulated pins boards), HOUSE_WORKSHOP_ROBOTS (default
<repo>/data/robots), HOUSE_WORKSHOP_SECRETS (default
<repo>/data/secrets, read by the families' secret store).

The workshop scans its families for devices, connects and checks
them, and supervises the robots it has started: every robot config
in the robots folder is a child process, respawned when it exits
(a replugged board comes back on its own).
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import importlib.util
import inspect
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BODIES = _HERE.parent
_REPO = _HERE.parent.parent
for _p in (str(_BODIES / "sdk"), str(_BODIES), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from body import Body  # noqa: E402

try:
    from workshop import drafting  # bodies/ on sys.path
    from workshop import sandbox  # noqa: F401  (re-exported for callers)
except ImportError:  # run as a plain script in this folder
    import drafting  # type: ignore  # noqa: E402

import families  # noqa: E402
from families import usb as usb_lib  # noqa: E402

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
UNIT_RE = re.compile(r"^ponkihouse-[a-z0-9-]+$")
INSPECT_MODULES = ["cv2", "serial", "av", "numpy", "PIL", "usb", "hid",
                   "evdev", "smbus2", "pyaudio", "sounddevice",
                   "websockets", "bleak", "makeblock"]

_children: dict = {}  # robot name -> {"proc", "started", "device_id"}
_lock = threading.Lock()
_broken_reported: dict = {}  # robot name -> config mtime already printed


# -- paths and settings -------------------------------------------------------

def repo_root() -> Path:
    return _REPO


def robots_dir() -> Path:
    env = os.environ.get("HOUSE_WORKSHOP_ROBOTS")
    return Path(env) if env else _REPO / "data" / "robots"


def moved_on_dir() -> Path:
    return robots_dir() / "moved-on"


def workshop_name() -> str:
    return os.environ.get("HOUSE_WORKSHOP_NAME") \
        or socket.gethostname().split(".")[0]


def enabled_families() -> list:
    """Enabled family refs: HOUSE_WORKSHOP_FAMILIES or all + local."""
    raw = os.environ.get("HOUSE_WORKSHOP_FAMILIES")
    if raw:
        return [part.strip() for part in raw.split(",") if part.strip()]
    refs = list(families.NAMES)
    for local in drafting.local_ids():
        refs.append(f"local:{local}")
    return refs


def family_for(ref):
    """A family module, or a SandboxFamily for draft:/local: refs."""
    text = str(ref or "pins")
    if text.startswith(("draft:", "local:")):
        return drafting.resolve(text)
    return families.get(text)


def family_name_of(ref) -> str:
    text = str(ref or "pins")
    if text.startswith("local:"):
        return text[len("local:"):]
    if text.startswith("draft:"):
        fam = drafting.resolve(text)
        return fam.NAME or text
    return text


# -- robot configs and children ------------------------------------------------

def _config_path(name: str) -> Path:
    return robots_dir() / f"{name}.json"


def _read_config(name: str):
    try:
        data = json.loads(_config_path(name).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_config(name: str, config: dict) -> Path:
    path = _config_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return path


def _config_names() -> list:
    folder = robots_dir()
    out: list = []
    if folder.is_dir():
        for entry in sorted(folder.glob("*.json")):
            out.append(entry.stem)
    return out


def _device_id_of(config: dict):
    device = (config or {}).get("device")
    if isinstance(device, dict):
        return device.get("id")
    if isinstance(device, str):
        return device
    return (config or {}).get("port")


def _alive(record) -> bool:
    proc = (record or {}).get("proc")
    return proc is not None and proc.poll() is None


def running_names() -> list:
    with _lock:
        return sorted(n for n, r in _children.items() if _alive(r))


def taken_devices() -> dict:
    """{device id: robot name} for the robots this workshop runs."""
    taken: dict = {}
    with _lock:
        records = list(_children.items())
    for name, record in records:
        if not _alive(record):
            continue
        device_id = record.get("device_id")
        if device_id:
            taken[str(device_id)] = name
    return taken


def _stop_child(name: str) -> bool:
    """Terminate the child for *name* (already out of the supervisor)."""
    with _lock:
        record = _children.pop(name, None)
    if record is None:
        return False
    proc = record.get("proc")
    if proc is None or proc.poll() is not None:
        return proc is not None
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    return True


def _spawn(name: str):
    """Start robot *name* from its config; None when it cannot start."""
    path = _config_path(name)
    config = _read_config(name)
    if config is None:
        with _lock:
            _children.pop(name, None)  # missing config: drop it
        return None
    ref = config.get("family") or "pins"
    fam = family_for(ref)  # the robot's family, even if not enabled
    argv, env = fam.command(str(path), config)
    argv = [str(a) for a in argv]
    if shutil.which("systemd-cat"):
        argv = ["systemd-cat", "-t", f"ponkihouse-{name}"] + argv
    full_env = dict(os.environ)
    full_env.update({str(k): str(v) for k, v in (env or {}).items()})
    proc = subprocess.Popen(argv, env=full_env, cwd=str(_REPO))
    with _lock:
        _children[name] = {"proc": proc, "started": time.time(),
                           "device_id": _device_id_of(config)}
    return proc


def supervise(stop_event: threading.Event | None = None) -> None:
    """Spawn every robot at start; respawn the ones that exit."""
    for name in _config_names():
        if not NAME_RE.match(name):
            continue
        try:
            _spawn(name)
        except Exception as exc:  # a broken config never stops others
            print(f"workshop: {name} can't start: {exc}", flush=True)
    while stop_event is None or not stop_event.is_set():
        time.sleep(5)
        for name in _config_names():
            if not NAME_RE.match(name):
                continue
            with _lock:
                record = _children.get(name)
            if record is not None and _alive(record):
                continue
            if record is not None \
                    and time.time() - record.get("started", 0) <= 10:
                continue  # just exited; give it its 10 s
            try:
                _spawn(name)
                _broken_reported.pop(name, None)
            except Exception as exc:
                try:
                    mtime = _config_path(name).stat().st_mtime
                except OSError:
                    mtime = 0.0
                if _broken_reported.get(name) != mtime:
                    _broken_reported[name] = mtime
                    print(f"workshop: {name} can't start: {exc}",
                          flush=True)


# -- device scanning ------------------------------------------------------------

def _real(path) -> str:
    try:
        return os.path.realpath(str(path))
    except (OSError, TypeError, ValueError):
        return str(path)


def _claimed_paths(devices: list) -> dict:
    """{real path of a port/serial node: "name (family)"}."""
    claimed: dict = {}
    for device in devices:
        if not isinstance(device, dict):
            continue
        label = f"{device.get('name', 'device')} ({device.get('family', '')})"
        candidates: list = []
        if device.get("port"):
            candidates.append(device["port"])
        for node in device.get("nodes") or []:
            if isinstance(node, dict) and node.get("kind") == "serial":
                if node.get("node"):
                    candidates.append(node["node"])
                if node.get("stable"):
                    candidates.append(node["stable"])
        if isinstance(device.get("id"), str) \
                and device["id"].startswith("/dev/"):
            candidates.append(device["id"])
        for candidate in candidates:
            claimed[_real(candidate)] = label
    return claimed


def scan_devices(fresh: bool = False) -> list:
    if fresh:
        try:
            from families import net as net_lib
            net_lib.forget()  # fresh clears the net cache
        except Exception:
            pass
    sim = os.environ.get("HOUSE_WORKSHOP_SIM")
    if sim:
        os.environ.setdefault("HOUSE_SIMULATED_BOARDS", sim)
    taken = taken_devices()
    refs = enabled_families()

    def is_first(ref: str) -> bool:
        if ref.startswith("local:"):
            return True  # promoted local families scan first
        if ref.startswith("draft:"):
            return False
        try:
            return getattr(families.get(ref), "CLAIMS_PORTS", None) is True
        except Exception:
            return False

    first = [r for r in refs if is_first(r)]
    rest = [r for r in refs if r not in first]
    out: list = []

    def discover(ref: str, taken_map: dict) -> list:
        fam = family_for(ref)
        found = fam.discover(dict(taken_map))
        tagged: list = []
        for device in found or []:
            if isinstance(device, dict):
                device = dict(device)
                device["family"] = ref
            tagged.append(device)
        return tagged

    def run_group(group: list, taken_map: dict) -> list:
        results: list = []
        if not group:
            return results
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, len(group))) as pool:
            futures = {pool.submit(discover, ref, taken_map): ref
                       for ref in group}
            for future in concurrent.futures.as_completed(futures):
                ref = futures[future]
                try:
                    results.extend(future.result())
                except Exception as exc:  # one family raising is one entry
                    results.append({"family": ref,
                                    "error": f"{type(exc).__name__}: {exc}"})
        return results

    first_devices = run_group(first, taken)
    out.extend(first_devices)
    claimed = _claimed_paths(
        [d for d in first_devices if isinstance(d, dict)])
    merged_taken = dict(taken)
    merged_taken.update(claimed)
    for device in run_group(rest, merged_taken):
        if isinstance(device, dict) and device.get("id") \
                and _real(device["id"]) in claimed:
            continue  # already claimed by a first family
        out.append(device)

    listed_usb = {str(d.get("usb")) for d in out
                  if isinstance(d, dict) and d.get("usb")}
    for entry in usb_lib.devices():
        if str(entry.get("path")) in listed_usb:
            continue
        device = drafting.device_of(entry, taken)
        device.update(family="new", unknown=True,
                      status="no family knows it yet: the builder can "
                             "investigate it and write one")
        out.append(device)
    return out


# -- small helpers for the actions ----------------------------------------------

def _run_check_sync(fam, device, config, check_id) -> dict:
    result = fam.check(device, config, check_id)
    if inspect.isawaitable(result):
        result = asyncio.run(result)
    return result if isinstance(result, dict) else {"expect": str(result)}


def _own_unit() -> str:
    """This process's systemd unit (minus nothing), or ""."""
    try:
        text = Path("/proc/self/cgroup").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = re.search(r"(ponkihouse-[a-z0-9-]+\.service)", text)
    if match:
        return match.group(1)
    match = re.search(r"([a-z0-9-]+\.service)", text)
    return match.group(1) if match else ""


_ACCESS_RE = re.compile(r'"(?:GET|POST|PUT|DELETE|HEAD|PATCH|OPTIONS) [^"]*"\s+(?:200|204|304)\b')
_CLIENT_OK_RE = re.compile(r'"HTTP/1\.1 200')


def _quiet_filter(text: str) -> str:
    kept = []
    for line in text.splitlines():
        if _ACCESS_RE.search(line) or _CLIENT_OK_RE.search(line):
            continue
        kept.append(line)
    return "\n".join(kept)


def build_body() -> Body:
    body = Body(workshop_name(),
                "A workbench where devices become robots.",
                kind="workshop")

    @body.action("devices", "List the devices this workshop can see.",
                 {"fresh": {"type": "boolean"}})
    async def devices(fresh: bool = False):
        return await asyncio.to_thread(scan_devices, bool(fresh))

    @body.action("connect", "Run a family's connect step on a device.",
                 {"family": {"type": "string"},
                  "device": {"type": "object"},
                  "fields": {"type": "object"}})
    async def connect(family: str = "", device: dict | None = None,
                      fields: dict | None = None):
        device = device or {}
        owner = taken_devices().get(str(device.get("id")))
        if owner:
            raise ValueError(
                f"a robot ({owner}) is running on that device: stop it first")
        fam = family_for(family)

        def work():
            result = fam.connect(device, fields or {})
            if inspect.isawaitable(result):
                result = asyncio.run(result)
            return result

        text, updated = await asyncio.to_thread(work)
        return {"text": text, "device": updated}

    @body.action("check", "Run one build check on a device.",
                 {"family": {"type": "string"},
                  "device": {"type": "object"},
                  "config": {"type": "object"},
                  "check": {"type": "string"}})
    async def check(family: str = "", device: dict | None = None,
                    config: dict | None = None, check: str = ""):
        fam = family_for(family)
        result = await asyncio.to_thread(
            _run_check_sync, fam, device or {}, config or {}, check)
        result = dict(result)
        image = result.get("image")
        if isinstance(image, (bytes, bytearray)):
            result["image"] = base64.b64encode(bytes(image)).decode("ascii")
        return result

    @body.action("start_robot", "Write a robot config and start it.",
                 {"config": {"type": "object"}})
    async def start_robot(config: dict | None = None):
        config = dict(config or {})
        name = str(config.get("name") or "")
        if not NAME_RE.match(name):
            raise ValueError(
                f"robot names must match ^[a-z][a-z0-9_-]{{0,31}}$, got {name!r}")
        fam = family_for(config.get("family") or "pins")
        problems, _warnings = fam.problems(config)
        if problems:
            raise ValueError("; ".join(str(p) for p in problems))
        _write_config(name, config)
        _stop_child(name)  # stop the old one before spawning the new
        await asyncio.to_thread(_spawn, name)
        return "started"

    @body.action("update_robot", "Refresh a robot's device in place.",
                 {"name": {"type": "string"}})
    async def update_robot(name: str = ""):
        config = _read_config(name)
        if config is None:
            raise ValueError(f"no robot called {name!r}")
        fam = family_for(config.get("family") or "pins")
        update = getattr(fam, "update", None)
        device = config.get("device") or {}
        if not callable(update):
            return {"text": f"the {config.get('family') or 'pins'} family "
                            "has no update step", "device": device}

        def work():
            try:
                result = update(device, str(_config_path(name)))
            except TypeError:
                result = update(device)
            if inspect.isawaitable(result):
                result = asyncio.run(result)
            return result

        text, updated = await asyncio.to_thread(work)
        if isinstance(updated, dict):
            config["device"] = updated
            _write_config(name, config)
        return {"text": text, "device": updated}

    @body.action("stop_robot", "Stop a robot (and maybe forget it).",
                 {"name": {"type": "string"},
                  "forget": {"type": "boolean"},
                  "erase": {"type": "boolean"},
                  "away": {"type": "string"}})
    async def stop_robot(name: str = "", forget: bool = False,
                         erase: bool = False, away: str | None = None):
        was_running = name in running_names() or name in _children
        _stop_child(name)  # out of the supervisor FIRST, then terminated
        config_path = _config_path(name)
        set_aside = moved_on_dir() / f"{name}.json"
        if erase:
            for path in (config_path, set_aside):
                try:
                    path.unlink()
                except OSError:
                    pass
        elif forget:
            if config_path.exists():
                moved_on_dir().mkdir(parents=True, exist_ok=True)
                shutil.move(str(config_path), str(set_aside))
        return "stopped" if was_running else "wasn't running"

    @body.action("configure_robot", "Rename or redescribe a robot.",
                 {"name": {"type": "string"},
                  "display_name": {"type": "string"},
                  "description": {"type": "string"}})
    async def configure_robot(name: str = "",
                              display_name: str | None = None,
                              description: str | None = None):
        config = _read_config(name)
        if config is None:
            raise ValueError(f"no robot called {name!r}")
        if display_name:
            config["display_name"] = display_name
        if description:
            config["description"] = description
        _write_config(name, config)
        _stop_child(name)  # restart it on the new config
        await asyncio.to_thread(_spawn, name)
        return "configured"

    @body.action("restore_robot", "Bring back a set-aside robot.",
                 {"name": {"type": "string"}})
    async def restore_robot(name: str = ""):
        set_aside = moved_on_dir() / f"{name}.json"
        if not set_aside.exists():
            raise ValueError(f"there's no set-aside robot called {name!r}")
        if name in running_names():
            raise ValueError(f"{name} is already running")
        _config_path(name).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(set_aside), str(_config_path(name)))
        await asyncio.to_thread(_spawn, name)
        return "restored"

    @body.action("units", "List this machine's ponkihouse services.", {})
    async def units():
        def work():
            try:
                proc = subprocess.run(
                    ["systemctl", "list-units", "--type=service", "--all",
                     "ponkihouse-*", "--no-legend", "--plain"],
                    capture_output=True, text=True, timeout=20)
                text = proc.stdout or ""
            except Exception:
                return []
            own = _own_unit()
            out = []
            for line in text.splitlines():
                parts = line.split(None, 4)
                if not parts:
                    continue
                unit = parts[0]
                sub = parts[3] if len(parts) > 3 else ""
                description = parts[4] if len(parts) > 4 else ""
                bare = unit[:-len(".service")] if unit.endswith(".service") \
                    else unit
                out.append({"unit": bare, "active": sub == "running",
                            "description": description,
                            "self": unit == own})
            return out

        return await asyncio.to_thread(work)

    @body.action("logs", "Read this machine's journal.",
                 {"name": {"type": "string"},
                  "unit": {"type": "string"},
                  "lines": {"type": "integer"},
                  "quiet": {"type": "boolean"}})
    async def logs(name: str | None = None, unit: str | None = None,
                   lines: int = 200, quiet: bool = True):
        try:
            count = int(lines)
        except (TypeError, ValueError):
            count = 200
        count = max(10, min(1000, count))
        read = count * 5 if quiet else count

        def work() -> str:
            args = ["journalctl", "-n", str(read), "-o", "short-iso",
                    "--no-pager", "-q"]
            if name:
                args += ["-t", f"ponkihouse-{name}"]
            elif unit and UNIT_RE.match(str(unit)):
                args += ["-u", str(unit)]
            else:
                own = _own_unit() or "ponkihouse-workshop"
                args += ["-u", own if own.endswith(".service")
                         else own + ".service"]
            try:
                proc = subprocess.run(args, capture_output=True, text=True,
                                      timeout=30)
                text = proc.stdout or ""
            except Exception as exc:
                return f"can't read the journal: {exc}"
            if quiet:
                text = _quiet_filter(text)
            tail = text.splitlines()[-count:]
            return "\n".join(tail)

        return await asyncio.to_thread(work)

    @body.action("robots", "List the robots this workshop runs.", {})
    async def robots():
        out = []
        running = set(running_names())
        for name in _config_names():
            config = _read_config(name) or {}
            ref = config.get("family") or "pins"
            kind = "robot"
            try:
                fixture = getattr(family_for(ref), "FIXTURE", None)
                if fixture:
                    kind = fixture
            except Exception:
                pass
            started = None
            with _lock:
                record = _children.get(name)
            if record is not None and _alive(record):
                started = record.get("started")
            out.append({
                "name": name,
                "device": config.get("device"),
                "family": ref,
                "kind": kind,
                "display_name": config.get("display_name") or name,
                "running": name in running,
                "since": started,
            })
        return out

    @body.action("inspect_device", "Look closely at one device.",
                 {"device": {"type": "object"}})
    async def inspect_device(device=None):
        return await asyncio.to_thread(_inspect_device, device)

    @body.action("listen_device", "Listen on a serial device.",
                 {"device": {"type": "object"},
                  "baud": {"type": "integer"},
                  "seconds": {"type": "number"}})
    async def listen_device(device=None, baud: int = 115200,
                            seconds: float = 5):
        return await asyncio.to_thread(
            _listen_device, device or {}, baud, seconds)

    @body.action("draft_file", "List, read, write or delete draft files.",
                 {"draft": {"type": "string"}, "op": {"type": "string"},
                  "name": {"type": "string"},
                  "content": {"type": "string"}})
    async def draft_file(draft: str = "", op: str = "list",
                         name: str | None = None,
                         content: str | None = None):
        return await asyncio.to_thread(
            _draft_file, draft, op, name, content)

    @body.action("draft_review", "Review a draft family.",
                 {"draft": {"type": "string"}})
    async def draft_review(draft: str = ""):
        return await asyncio.to_thread(_draft_review, draft)

    @body.action("draft_approve", "Approve a reviewed draft.",
                 {"draft": {"type": "string"}, "hash": {"type": "string"}})
    async def draft_approve(draft: str = "", hash: str = ""):
        return await asyncio.to_thread(_draft_approve, draft, hash)

    @body.action("draft_promote", "Promote an approved draft to a family.",
                 {"draft": {"type": "string"}, "build": {"type": "string"}})
    async def draft_promote(draft: str = "", build: str = ""):
        return await asyncio.to_thread(
            drafting.promote, draft, build or draft)

    async def state_loop() -> None:
        stop = threading.Event()
        thread = threading.Thread(target=supervise, args=(stop,),
                                  daemon=True)
        thread.start()
        while True:
            fams = enabled_families()
            for local in drafting.local_ids():
                ref = f"local:{local}"
                if ref not in fams and local not in fams:
                    fams.append(ref)
            await body.set_state(families=fams,
                                 robots=sorted(_config_names()))
            await asyncio.sleep(10)

    body._workshop_state_loop = state_loop  # run by main()
    return body


# -- inspect / listen -----------------------------------------------------------

def _inspect_device(device) -> dict:
    dev = device if isinstance(device, dict) else {"id": device}
    entry = None
    for candidate in usb_lib.devices():
        if dev.get("usb") and candidate.get("path") == dev.get("usb"):
            entry = candidate
            break
        if str(dev.get("id") or "").endswith(str(candidate.get("path") or "")) \
                and candidate.get("path"):
            entry = candidate
            break
    report: dict = {"device": dev, "usb": entry,
                    "video": {}, "sound": {},
                    "python_modules": {}}
    nodes = (entry or {}).get("nodes") or dev.get("nodes") or []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("kind") == "video" and node.get("node"):
            text = ""
            try:
                proc = subprocess.run(
                    ["v4l2-ctl", "-d", node["node"],
                     "--all", "--list-formats-ext"],
                    capture_output=True, text=True, timeout=15)
                text = (proc.stdout or "") + (proc.stderr or "")
            except Exception as exc:
                text = f"can't ask {node['node']}: {exc}"
            report["video"][node["node"]] = text[-4000:]
        if node.get("kind") == "sound":
            card = node.get("card")
            if card is None and node.get("node"):
                match = re.search(r"pcmC(\d+)", str(node["node"]))
                card = match.group(1) if match else None
            if card is not None:
                try:
                    text = Path(f"/proc/asound/card{card}/stream0").read_text(
                        encoding="utf-8", errors="replace")
                except OSError as exc:
                    text = f"can't read the card: {exc}"
                report["sound"][str(card)] = text[-2000:]
    for module in INSPECT_MODULES:
        try:
            report["python_modules"][module] = \
                importlib.util.find_spec(module) is not None
        except Exception:
            report["python_modules"][module] = False
    return report


def _listen_device(device: dict, baud: int = 115200,
                   seconds: float = 5) -> str:
    owner = taken_devices().get(str((device or {}).get("id")))
    if owner:
        raise ValueError(
            f"a robot ({owner}) is running on that device: stop it first")
    port = (device or {}).get("port") or ""
    if not port:
        for node in (device or {}).get("nodes") or []:
            if isinstance(node, dict) and node.get("kind") == "serial" \
                    and node.get("node"):
                port = node["node"]
                break
    if not port and str((device or {}).get("id") or "").startswith("/dev/"):
        port = device["id"]
    if not port:
        raise ValueError("that device has no serial port to listen on")
    import serial  # pyserial, only needed here

    ser = serial.Serial()
    ser.port = port
    ser.baudrate = int(baud or 115200)
    ser.timeout = 0.2
    ser.dtr = False  # DTR/RTS low BEFORE open, so the board isn't reset
    ser.rts = False
    limit = min(float(seconds or 5), 15.0)
    chunks: list = []
    total = 0
    ser.open()
    try:
        deadline = time.time() + limit
        while time.time() < deadline and total < 8000:
            chunk = ser.read(min(1024, 8000 - total))
            if chunk:
                chunks.append(chunk)
                total += len(chunk)
    finally:
        ser.close()
    text = b"".join(chunks).decode("utf-8", "replace")
    return text if text else "(nothing)"


# -- drafts -----------------------------------------------------------------------

def _draft_file(draft: str, op: str, name=None, content=None):
    folder = drafting.draft_folder(draft)
    op = str(op or "list")
    if op == "list":
        out: dict = {}
        if folder.is_dir():
            for entry in sorted(folder.iterdir(), key=lambda p: p.name):
                if entry.is_file():
                    try:
                        text = entry.read_text(
                            encoding="utf-8", errors="replace")
                        out[entry.name] = len(text.splitlines())
                    except OSError:
                        continue
        return out
    if op == "discard":
        shutil.rmtree(folder, ignore_errors=True)  # promoted stays
        return "discarded"
    fname = str(name or "")
    if not fname or "/" in fname or "\\" in fname or fname in (".", ".."):
        raise ValueError("a file name is needed")
    path = folder / fname
    if op == "read":
        if not path.is_file():
            present = sorted(drafting.read_folder_files(folder))
            raise ValueError(
                f"no file {fname!r} in this draft "
                f"(files present: {', '.join(present) or 'none'})")
        return path.read_text(encoding="utf-8", errors="replace")
    if op == "write":
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(content if content is not None else "",
                        encoding="utf-8")
        return "written"
    if op == "delete":
        try:
            path.unlink()
        except OSError:
            pass
        return "deleted"
    raise ValueError(f"unknown draft file op {op!r}")


def _draft_review(draft: str) -> dict:
    files = drafting.read_folder_files(drafting.draft_folder(draft))
    reviewed = drafting.review_files(files)
    out = dict(reviewed)
    out["sources"] = files  # every file's text, for the human to read
    return out


def _draft_approve(draft: str, hash: str) -> str:
    folder = drafting.draft_folder(draft)
    reviewed = drafting.review_folder(folder)
    if str(hash or "") != reviewed.get("hash"):
        raise ValueError(
            "the code changed since you looked at it: look again")
    if reviewed.get("problems"):
        raise ValueError("fix these first: "
                         + "; ".join(reviewed["problems"]))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "approved.json").write_text(
        json.dumps({"hash": reviewed["hash"], "at": time.time()},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return "approved"


def main() -> None:
    import logging

    logging.basicConfig(format="%(asctime)s %(name)s %(message)s",
                        level=logging.INFO)
    body = build_body()
    body.run(body._workshop_state_loop())


if __name__ == "__main__":
    main()

