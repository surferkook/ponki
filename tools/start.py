"""Start Ponki on this machine, after ./setup.sh (macOS or Linux).

    .venv/bin/python tools/start.py              the house, on this machine only
    .venv/bin/python tools/start.py --lan        the house, with the door for robots
                                                 (:8210) open to your network
    .venv/bin/python tools/start.py --workshop   only a workshop, for a house elsewhere

The house is the hub (http://localhost:8200), a workshop with the virtual
desk that also looks for real devices, and each service that has its own
.venv: finding (services/sam), the voice (services/voice), the ears
(services/ears). Settings come from .env; whatever is already in the
environment wins. Everything runs in the foreground, its lines tagged with
where they came from, and Ctrl-C stops it all. If one part stops, the rest
stop too, so nothing is left half running.

For a house that starts with the machine, see "Run it as a service" in
docs/SETUP.md.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVICES = {  # name: (folder, port, the hub's setting for it)
    "finding": ("services/sam", 8201, "HOUSE_SAM_URL"),
    "voice": ("services/voice", 8202, "HOUSE_VOICE_URL"),
    "ears": ("services/ears", 8203, "HOUSE_EARS_URL"),
}
APPS = {"finding": "sam_server:app", "voice": "voice_server:app", "ears": "ears_server:app"}


def settings() -> dict:
    env = dict(os.environ)
    path = REPO / ".env"
    if path.exists():
        from dotenv import dotenv_values

        for key, value in dotenv_values(path).items():
            if value is not None and key not in env:
                env[key] = value
    return env


def plan(args, env: dict) -> list[tuple[str, list[str], dict, Path]]:
    """(tag, argv, env, cwd) for each process to run."""
    venv_python = str(REPO / ".venv" / "bin" / "python")
    out = []
    workshop_env = dict(env)
    if args.workshop:
        out.append(("workshop", [venv_python, "-u", "bodies/workshop/workshop.py"], workshop_env, REPO))
        return out

    hub_env = dict(env)
    port = int(env.get("HOUSE_PORT") or 8200)
    hub_env["HOUSE_PORT"] = str(port)
    if args.lan:
        hub_env.update(HOUSE_HOST="0.0.0.0", HOUSE_BODY_PORT=env.get("HOUSE_BODY_PORT") or "8210",
                       PONKI_BODY_BIND="0.0.0.0")
    else:
        hub_env.update(HOUSE_HOST=env.get("HOUSE_HOST") or "127.0.0.1")
        hub_env.pop("HOUSE_BODY_PORT", None)
    for name, (folder, default_port, setting) in SERVICES.items():
        venv = REPO / folder / ".venv" / "bin" / "python"
        if not venv.exists():
            if name == "voice":
                hub_env["HOUSE_VOICE_OPTIONAL"] = "on"  # off, not broken
            if name == "ears":
                hub_env["HOUSE_EARS_URL"] = "off"
            continue
        service_port = int(env.get(f"PONKI_{name.upper()}_PORT") or default_port)
        hub_env[setting] = f"http://127.0.0.1:{service_port}"
        out.append((name, [str(venv), "-m", "uvicorn", APPS[name], "--host", "127.0.0.1",
                           "--port", str(service_port)], dict(env), REPO / folder))
    out.insert(0, ("hub", [venv_python, "-m", "hub.serve"], hub_env, REPO))

    workshop_env.update(HOUSE_HUB=f"ws://127.0.0.1:{port}",
                        HOUSE_WORKSHOP_NAME=env.get("HOUSE_WORKSHOP_NAME") or "desk-workshop",
                        HOUSE_DESK=env.get("HOUSE_DESK") or "on")
    out.append(("workshop", [venv_python, "-u", "bodies/workshop/workshop.py"], workshop_env, REPO))
    return out


def relay(tag: str, stream) -> None:
    width = 8
    for line in iter(stream.readline, b""):
        sys.stdout.write(f"{tag:<{width}}| {line.decode('utf-8', 'replace')}")
        sys.stdout.flush()


def wait_for_hub(port: int, hub: subprocess.Popen | None, seconds: float = 60) -> bool:
    import urllib.request

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if hub is not None and hub.poll() is not None:
            return False  # it stopped: its lines say why
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/info", timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--lan", action="store_true",
                      help="open the web UI and the door for robots to your network")
    mode.add_argument("--workshop", action="store_true", help="only a workshop")
    args = ap.parse_args()

    if not (REPO / ".venv" / "bin" / "python").exists():
        print("No .venv here yet: run ./setup.sh first.", file=sys.stderr)
        return 1
    env = settings()
    if args.workshop and env.get("HOUSE_HUB", "ws://192.0.2.7:8200") == "ws://192.0.2.7:8200":
        print("Set HOUSE_HUB in .env to the house's door for robots (./setup.sh --workshop asks).",
              file=sys.stderr)
        return 1
    if args.lan:
        print("The web UI has no login: --lan is for a network you trust, or a tailnet.")

    procs: list[tuple[str, subprocess.Popen]] = []
    stopping = threading.Event()
    crashed: list[str] = []

    def stop(*_):
        stopping.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    port = int(env.get("HOUSE_PORT") or 8200)
    try:
        for tag, argv, child_env, cwd in plan(args, env):
            if tag == "workshop" and not args.workshop:
                hub = dict(procs).get("hub")
                if not wait_for_hub(port, hub):
                    print("the hub didn't start; see its lines above", file=sys.stderr)
                    crashed.append("hub")
                    stopping.set()
                    break
                print(f"the house: http://localhost:{port}", flush=True)
            if stopping.is_set():
                break
            proc = subprocess.Popen(argv, env=child_env, cwd=str(cwd), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            procs.append((tag, proc))
            threading.Thread(target=relay, args=(tag, proc.stdout), daemon=True).start()
        while not stopping.is_set():
            for tag, proc in procs:
                if proc.poll() is not None:
                    print(f"{tag} stopped (exit {proc.returncode}); stopping the rest", flush=True)
                    crashed.append(tag)
                    stopping.set()
                    break
            stopping.wait(0.5)
    finally:
        # each part leads its own process group: a workshop's robots go with it
        for _tag, proc in reversed(procs):
            group(proc, signal.SIGTERM)
        deadline = time.monotonic() + 10
        for _tag, proc in procs:
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
            group(proc, signal.SIGKILL)  # anything of its group still left
    return 1 if crashed else 0


def group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


if __name__ == "__main__":
    sys.exit(main())
