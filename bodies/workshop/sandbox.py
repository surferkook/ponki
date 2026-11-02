"""The workshop sandbox: bubblewrap for builder-written code.

Draft families are written by a builder the house does not fully
trust, so their hooks and drivers only ever run inside bubblewrap:
a read-only system, almost no /dev, no network (unless the driver
asks for the hub), no house data, and only the one device the human
picked. ``argv`` builds the command; ``run_hook`` runs one draft hook
and reads its answer back; ``driver_argv`` builds the long-running
driver command.

Self-test (from bodies/): ``python3 -m workshop.sandbox`` prints what
a sandboxed process can see.
"""
from __future__ import annotations

import base64
import glob
import json
import os
import re
import shutil
import site
import subprocess
import sys
from pathlib import Path

RESULT_MARKER = "\0HOUSE-RESULT "

_HERE = Path(__file__).resolve().parent
RUNNER = _HERE / "draft_runner.py"
BODIES = _HERE.parent

_NO_SANDBOX = ("this workshop has no sandbox (bubblewrap isn't "
               "installed), so it can't run builder-written code")


def _device_paths(device) -> list:
    """The /dev nodes that belong to *device* (and no others)."""
    paths: list[str] = []

    def add(path) -> None:
        if isinstance(path, str) and path.startswith("/dev/") \
                and path not in paths:
            paths.append(path)

    if not device:
        return paths
    if isinstance(device, (str, os.PathLike)):
        add(str(device))
        return paths
    if not isinstance(device, dict):
        return paths

    cards: set = set()
    nodes = device.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if isinstance(node, str):
                add(node)
            elif isinstance(node, dict):
                add(node.get("node"))
                if not node.get("node"):
                    add(node.get("stable"))
                node_path = node.get("node") or ""
                if node.get("kind") == "sound" or (
                        isinstance(node_path, str)
                        and node_path.startswith("/dev/snd/")):
                    if node.get("card") is not None:
                        cards.add(str(node.get("card")))
                    elif isinstance(node_path, str):
                        match = re.search(r"C(\d+)", node_path)
                        if match:
                            cards.add(match.group(1))
    for key in ("node", "port", "usb", "id"):
        add(device.get(key))
    if device.get("card") is not None:
        cards.add(str(device.get("card")))
    # A sound card is bound whole: control, every PCM, sequencer alike.
    for number in sorted(cards):
        for found in sorted(glob.glob(f"/dev/snd/*C{number}*")):
            add(found)
    return paths


def link_hops(path) -> list:
    """Every symlink met while resolving *path*, in order: (link, its target as written)."""
    hops: list = []
    todo = os.path.abspath(str(path))
    for _ in range(40):  # a symlink loop just stops here
        parts = Path(todo).parts
        current = Path(parts[0])
        for index, part in enumerate(parts[1:], 1):
            step = current / part
            if step.is_symlink():
                target = os.readlink(step)
                if (str(step), target) not in hops:
                    hops.append((str(step), target))
                resolved = Path(target) if os.path.isabs(target) else step.parent / target
                todo = os.path.normpath(str(resolved.joinpath(*parts[index + 1:])))
                break
            current = step
        else:
            return hops
    return hops


def argv(cmd, read=[], device=None, net=False, env={}) -> list:
    """The bubblewrap command list running *cmd* in the sandbox."""
    bwrap = shutil.which("bwrap") or shutil.which("bubblewrap")
    if not bwrap:
        raise RuntimeError(_NO_SANDBOX)

    args: list[str] = [bwrap]
    bound: set = set()

    def ro_bind(path) -> None:
        text = str(path)
        if text in bound:
            return
        bound.add(text)
        args.extend(["--ro-bind", text, text])

    def symlink_or_bind(path: str) -> None:
        entry = Path(path)
        try:
            if entry.is_symlink():
                args.extend(["--symlink", os.readlink(entry), path])
                return
        except OSError:
            pass
        if entry.is_dir() or entry.exists():
            ro_bind(path)

    for system_path in ("/usr", "/etc", "/sys"):
        ro_bind(system_path)
    for link_path in ("/bin", "/sbin"):
        symlink_or_bind(link_path)
    for lib_path in sorted(glob.glob("/lib*")):
        symlink_or_bind(lib_path)

    args.extend(["--proc", "/proc",
                 "--dev", "/dev",
                 "--tmpfs", "/tmp",
                 "--tmpfs", "/run"])

    ro_bind(BODIES)
    if sys.prefix:
        ro_bind(sys.prefix)
    # A venv's python is a symlink to the real one, which may live outside
    # /usr (pyenv, uv, Homebrew), behind more symlinks: recreate each link
    # that isn't already inside a bind, then bind the real install.
    for link, target in link_hops(sys.executable):
        if not any(link == b or link.startswith(b.rstrip("/") + "/") for b in bound):
            args.extend(["--symlink", target, link])
    if sys.base_prefix:
        ro_bind(os.path.realpath(sys.base_prefix))
    try:
        user_site = site.getusersitepackages()
    except Exception:
        user_site = None
    if user_site and Path(user_site).is_dir():
        ro_bind(user_site)
    for read_path in (read or []):
        ro_bind(read_path)

    for node_path in _device_paths(device):
        args.extend(["--dev-bind", node_path, node_path])

    args.append("--unshare-all")
    if net:
        args.append("--share-net")
    args.append("--clearenv")
    base_env = [("PATH", "/usr/bin:/bin"), ("HOME", "/tmp"),
                ("PYTHONDONTWRITEBYTECODE", "1")]
    for key, value in base_env + list((env or {}).items()):
        args.extend(["--setenv", str(key), str(value)])
    args.extend(["--die-with-parent", "--new-session",
                 "--chdir", "/tmp", "--"])
    args.extend(str(part) for part in cmd)
    return args


def _decode(value):
    """Undo draft_runner's encoding: __bytes__ dicts become bytes."""
    if isinstance(value, dict):
        if set(value) == {"__bytes__"} \
                and isinstance(value.get("__bytes__"), str):
            try:
                return base64.b64decode(value["__bytes__"])
            except Exception:
                return value
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


def _seconds(timeout) -> str:
    try:
        return "%g" % timeout
    except (TypeError, ValueError):
        return str(timeout)


def run_hook(folder, hook, args=None, device=None, timeout=30):
    """Run one draft family hook in the sandbox; return its result.

    The draft answers with one result line (see draft_runner); anything
    else — a crash before answering, a hook raising, a timeout — is
    raised here as a plain-words RuntimeError.
    """
    command = argv([sys.executable, "-I", str(RUNNER), str(folder)],
                   read=[folder], device=device, net=False, env={})
    payload = json.dumps({"hook": hook,
                          "args": list(args) if args is not None else []})
    try:
        proc = subprocess.run(command, input=payload, capture_output=True,
                              text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"took longer than {_seconds(timeout)} s") from None

    marker_line = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith(RESULT_MARKER):
            marker_line = line  # the LAST result line is the answer
    tail = (proc.stderr or "").strip()[-600:]
    if marker_line is None:
        raise RuntimeError(
            f"didn't answer (exit {proc.returncode}): {tail}")
    try:
        result = json.loads(marker_line[len(RESULT_MARKER):])
    except ValueError:
        raise RuntimeError(
            f"didn't answer (exit {proc.returncode}): {tail}") from None
    if isinstance(result, dict) and "error" in result:
        raise RuntimeError(
            f"the draft's {hook}() failed: {result['error']}")
    return _decode(result)


def driver_argv(folder, config_path, device=None) -> list:
    """The sandboxed command running the draft's driver as a body."""
    env = {key: os.environ[key] for key in ("HOUSE_HUB", "HOUSE_TOKEN")
           if key in os.environ}
    command = [sys.executable, "-I", str(RUNNER), str(folder),
               "--driver", str(config_path)]
    return argv(command, read=[folder, config_path], device=device,
                net=True, env=env)


_PROBE = r"""
import os, socket, sys
home = os.environ.get("HOME", "")
try:
    home_seen = sorted(os.listdir(home)) if home else []
except OSError as exc:
    home_seen = [f"<{type(exc).__name__}>"]
print(f"home ({home}): {home_seen}")
data_path = sys.argv[1] if len(sys.argv) > 1 else "data"
try:
    print(f"data ({data_path}): {sorted(os.listdir(data_path))}")
except OSError as exc:
    print(f"data ({data_path}): <{type(exc).__name__}>")
try:
    print(f"/dev: {sorted(os.listdir('/dev'))}")
except OSError as exc:
    print(f"/dev: <{type(exc).__name__}>")
try:
    sock = socket.create_connection(("1.1.1.1", 53), timeout=3)
    sock.close()
    print("network: reachable")
except OSError as exc:
    print(f"network: unreachable ({type(exc).__name__})")
"""


def main() -> int:
    """Print what a sandboxed process sees (the sandbox self-test)."""
    repo = BODIES.parent
    command = argv([sys.executable, "-c", _PROBE, str(repo / "data")],
                   net=False)
    proc = subprocess.run(command, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60)
    sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
