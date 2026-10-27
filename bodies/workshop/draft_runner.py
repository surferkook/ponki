"""The draft runner: runs INSIDE the sandbox (see workshop/sandbox.py).

    draft_runner.py <draft folder>                  # hook mode (stdin)
    draft_runner.py <draft folder> --driver <config.json>

Hook mode reads one JSON request ``{"hook", "args"}`` from stdin, calls
``family.<hook>(*args)`` and answers with a single last stdout line:
a NUL byte, ``HOUSE-RESULT ``, then the JSON result (bytes encoded as
``{"__bytes__": base64}``, tuples as lists; failures as
``{"error": "<Type>: <msg>", "trace": [last 6 traceback lines]}``).
The special hook ``"_probe"`` reports ``{"missing": [...]}`` — the
draft's imported modules that cannot be imported here — and the family
itself must import for the probe to answer at all.

Driver mode runs the draft's ``driver.py`` as ``__main__`` with
``argv == [driver, config]``; the driver is a body and runs until the
workshop stops it, so there is no result line.
"""
from __future__ import annotations

import ast
import asyncio
import base64
import importlib
import inspect
import json
import os
import runpy
import sys
import traceback
from pathlib import Path

MARKER = "\0HOUSE-RESULT "


def _setup_paths(folder: Path) -> None:
    """Draft folder, bodies/ and bodies/sdk first on sys.path."""
    bodies = Path(__file__).resolve().parent.parent
    front = [str(folder), str(bodies), str(bodies / "sdk")]
    extras: list[str] = []
    # python -I drops the user site; add it back when it is bound in.
    try:
        import site
        user_site = site.getusersitepackages()
        if user_site and Path(user_site).is_dir():
            extras.append(user_site)
    except Exception:
        pass
    try:  # HOME is /tmp in the sandbox; the passwd home is the real one
        import pwd
        home = pwd.getpwuid(os.getuid()).pw_dir
        candidate = str(Path(home) / ".local" / "lib"
                        / f"python{sys.version_info.major}."
                        f"{sys.version_info.minor}" / "site-packages")
        if Path(candidate).is_dir() and candidate not in extras:
            extras.append(candidate)
    except Exception:
        pass
    rest = [p for p in sys.path if p and p not in front and p not in extras]
    sys.path[:] = front + extras + rest


def _encode(value):
    """JSON-able copy: bytes as {"__bytes__": b64}, tuples as lists."""
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes__": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {(key if isinstance(key, str) else str(key)): _encode(item)
                for key, item in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _emit(payload) -> None:
    sys.stdout.write("\n" + MARKER + json.dumps(_encode(payload)) + "\n")
    sys.stdout.flush()


def _error_result(exc: BaseException) -> dict:
    return {"error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc().splitlines()[-6:]}


def _import_family():
    return importlib.import_module("family")


def _probe(folder: Path) -> dict:
    """{"missing": [...]} — draft imports that cannot be imported."""
    modules: list[str] = []
    for name in ("family.py", "driver.py"):
        path = folder / name
        if not path.exists():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"),
                             filename=name)
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            top = None
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top not in modules:
                        modules.append(top)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    top = node.module.split(".")[0]
                    if top not in modules:
                        modules.append(top)
    missing: list[str] = []
    for top in modules:
        if top in ("family", "driver"):
            continue
        try:
            importlib.import_module(top)
        except Exception:
            missing.append(top)
    return {"missing": missing}


def _run_hooks(folder: Path) -> None:
    try:
        request = json.loads(sys.stdin.read() or "{}")
    except ValueError as exc:
        _emit(_error_result(exc))
        return
    hook = request.get("hook") if isinstance(request, dict) else None
    hook_args = request.get("args") if isinstance(request, dict) else None
    if not isinstance(hook_args, list):
        hook_args = []
    try:
        family = _import_family()
    except Exception as exc:  # the family itself must import, always
        _emit(_error_result(exc))
        return
    try:
        if hook == "_probe":
            _emit(_probe(folder))
            return
        fn = getattr(family, hook) if isinstance(hook, str) else None
        if not callable(fn):
            raise AttributeError(f"family has no hook {hook!r}")
        result = fn(*hook_args)
        if inspect.isawaitable(result):
            result = asyncio.run(result)
        _emit(result)
    except Exception as exc:
        _emit(_error_result(exc))


def _run_driver(folder: Path, config: str) -> None:
    driver = folder / "driver.py"
    sys.argv = [str(driver), str(config)]
    runpy.run_path(str(driver), run_name="__main__")


def main() -> int:
    args = sys.argv[1:]
    if not args:
        _emit({"error": "ValueError: no draft folder given",
               "trace": []})
        return 0
    folder = Path(args[0]).resolve()
    _setup_paths(folder)
    if "--driver" in args:
        index = args.index("--driver")
        config = args[index + 1] if index + 1 < len(args) else ""
        try:
            _run_driver(folder, config)
        except SystemExit:
            raise
        except Exception:
            traceback.print_exc()
            return 1
        return 0
    _run_hooks(folder)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
