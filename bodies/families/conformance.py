"""Family conformance (the contract in families/__init__.py).

``check(fam, live=False) -> [problems]`` validates one family module:
constants, hook existence with exact first-parameter names, then the
builder-side pure hooks on a deepcopy of EXAMPLE. ``live=True`` also
runs ``discover({})``.

CLI (run from bodies/): ``python -m families.conformance [names...]
[--live]`` prints ``ok  <name>`` / ``FAIL <name>`` plus indented
reasons and exits 1 if any family failed.
"""
from __future__ import annotations

import copy
import inspect
import re
import sys

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# hook name -> exact first parameter names, in order (update unchecked)
HOOK_PARAMS: dict[str, tuple[str, ...]] = {
    "setup": ("config", "device"),
    "plan": ("config", "args"),
    "checks": ("config", "device"),
    "problems": ("config",),
    "body": ("config", "device"),
    "discover": ("taken",),
    "connect": ("device", "fields"),
    "check": ("device", "config", "check_id"),
    "command": ("config_path", "config"),
}


def _resolve(fam):
    """Accept a family module or a family name; return the module."""
    if isinstance(fam, str):
        try:
            from families import get as _get
        except ImportError:  # run with bodies/ on sys.path as top package
            from . import get as _get  # type: ignore
        return _get(fam)
    return fam


def _param_names(fn) -> list[str] | None:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None
    names: list[str] = []
    for param in sig.parameters.values():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL,
                          inspect.Parameter.VAR_KEYWORD):
            break
        if param.kind in (inspect.Parameter.POSITIONAL_ONLY,
                          inspect.Parameter.POSITIONAL_OR_KEYWORD):
            names.append(param.name)
    return names


def _check_constants(fam, problems: list[str]) -> None:
    def missing(attr: str) -> bool:
        if not hasattr(fam, attr):
            problems.append(f"missing {attr}")
            return True
        return False

    if not missing("NAME"):
        value = fam.NAME
        if not isinstance(value, str) or not NAME_RE.match(value):
            problems.append(
                f"NAME must match ^[a-z][a-z0-9_]{{0,31}}$, got {value!r}")
    if not missing("TITLE"):
        if not isinstance(fam.TITLE, str) or not fam.TITLE.strip():
            problems.append(f"TITLE must be a non-empty str, got {fam.TITLE!r}")
    if not missing("SCAN"):
        if not isinstance(fam.SCAN, str) or not fam.SCAN.strip():
            problems.append(f"SCAN must be a non-empty str, got {fam.SCAN!r}")
    if not missing("GUIDE"):
        if not isinstance(fam.GUIDE, str):
            problems.append(f"GUIDE must be a str, got {type(fam.GUIDE).__name__}")
        elif len(fam.GUIDE) < 40:
            problems.append(
                f"GUIDE must be at least 40 characters, got {len(fam.GUIDE)}")
    if not missing("CHECK_HINT"):
        if not isinstance(fam.CHECK_HINT, str):
            problems.append(
                f"CHECK_HINT must be a str, got {type(fam.CHECK_HINT).__name__}")
    if not missing("FIXTURE"):
        if fam.FIXTURE not in (None, "mic", "speaker"):
            problems.append(
                f'FIXTURE must be None, "mic" or "speaker", got {fam.FIXTURE!r}')
    # CLAIMS_PORTS is optional; when present it must be True.
    if hasattr(fam, "CLAIMS_PORTS") and fam.CLAIMS_PORTS is not True:
        problems.append(
            f"CLAIMS_PORTS must be True when present, got {fam.CLAIMS_PORTS!r}")
    if not missing("PLAN_PARAMS"):
        params = fam.PLAN_PARAMS
        if not isinstance(params, dict):
            problems.append(
                f"PLAN_PARAMS must be a dict, got {type(params).__name__}")
        else:
            # Spec shape: properties mapping. Tolerate a full object
            # schema ({"type": "object", "properties": {...}}) by
            # checking its properties instead.
            entries = params
            props = params.get("properties")
            if isinstance(props, dict) and "type" in params:
                entries = props
            for key, entry in entries.items():
                if key in ("type", "required", "properties") and entries is params \
                        and isinstance(props, dict):
                    continue
                if not isinstance(entry, dict) or "type" not in entry:
                    problems.append(
                        f"PLAN_PARAMS entry {key!r} must have a \"type\"")
    if not missing("CONNECT"):
        conn = fam.CONNECT
        if not isinstance(conn, dict):
            problems.append(f"CONNECT must be a dict, got {type(conn).__name__}")
        else:
            for key in ("label", "hint", "fields"):
                if key not in conn:
                    problems.append(f"CONNECT missing {key!r}")
            if "label" in conn and not isinstance(conn["label"], str):
                problems.append("CONNECT label must be a str")
            if "hint" in conn and not isinstance(conn["hint"], str):
                problems.append("CONNECT hint must be a str")
            if "fields" in conn:
                fields = conn["fields"]
                if not isinstance(fields, list):
                    problems.append("CONNECT fields must be a list")
                else:
                    for field in fields:
                        if not isinstance(field, dict) \
                                or "id" not in field or "label" not in field:
                            problems.append(
                                "CONNECT fields must have id and label")
                            break
                        if not isinstance(field["id"], str) \
                                or not isinstance(field["label"], str):
                            problems.append(
                                "CONNECT field id and label must be str")
                            break
                        if "secret" in field \
                                and not isinstance(field["secret"], bool):
                            problems.append(
                                "CONNECT field secret must be a bool")
                            break
                    if fields and isinstance(conn.get("label"), str) \
                            and not conn["label"].strip():
                        problems.append(
                            "a form (fields non-empty) needs a label")
    if not missing("EXAMPLE"):
        example = fam.EXAMPLE
        if not isinstance(example, dict):
            problems.append(
                f"EXAMPLE must be a dict, got {type(example).__name__}")
        else:
            if "device" not in example or "plan" not in example:
                problems.append('EXAMPLE must have "device" and "plan"')
            elif not isinstance(example["device"], dict) \
                    or not isinstance(example["plan"], dict):
                problems.append("EXAMPLE device and plan must be dicts")


def _check_hooks(fam, problems: list[str]) -> None:
    for hook, expected in HOOK_PARAMS.items():
        fn = getattr(fam, hook, None)
        if fn is None or not callable(fn):
            problems.append(f"missing hook {hook}")
            continue
        names = _param_names(fn)
        if names is None:
            problems.append(f"hook {hook} has no inspectable signature")
            continue
        if tuple(names[:len(expected)]) != expected:
            problems.append(
                f"hook {hook} must start with {expected}, got {tuple(names)}")
    # update is optional and unchecked.


def _check_example(fam, problems: list[str]) -> None:
    if not hasattr(fam, "EXAMPLE") or not isinstance(fam.EXAMPLE, dict):
        return
    example = fam.EXAMPLE
    device = example.get("device")
    plan_data = example.get("plan")
    if not isinstance(device, dict) or not isinstance(plan_data, dict):
        return  # already reported by the constants check
    if not isinstance(device.get("id"), str) or not device.get("id") \
            or not isinstance(device.get("name"), str) \
            or not device.get("name"):
        problems.append("EXAMPLE device must have str id and name")
        return
    for hook in ("setup", "plan", "checks", "problems", "body", "command"):
        if not callable(getattr(fam, hook, None)):
            return  # already reported by the hooks check
    try:
        work = copy.deepcopy(example)
        device = work["device"]
        plan_data = work["plan"]
        config: dict = {"family": getattr(fam, "NAME", None),
                        "device": device}
        returned = fam.setup(config, device)
        if isinstance(returned, dict):
            config = returned
        elif returned is not None:
            problems.append(
                f"setup must return a dict config, got "
                f"{type(returned).__name__}")
        for key in ("name", "display_name", "description", "personality"):
            if key in plan_data:
                config[key] = plan_data[key]
        if "display_name" not in config and "name" in plan_data:
            config["display_name"] = plan_data["name"]

        plan_ret = fam.plan(config, plan_data)
        if not isinstance(plan_ret, str):
            problems.append(
                f"plan must return str, got {type(plan_ret).__name__}")

        checks_ret = fam.checks(config, device)
        if not isinstance(checks_ret, dict):
            problems.append(
                f"checks must return a mapping of id to check, got "
                f"{type(checks_ret).__name__}")
        elif len(checks_ret) < 1:
            problems.append("checks must return at least one check")
        else:
            for check_id, entry in checks_ret.items():
                if not isinstance(entry, dict):
                    problems.append(f"check {check_id!r} must be a dict")
                    continue
                if not isinstance(entry.get("title"), str):
                    problems.append(
                        f"check {check_id!r} title must be a str")
                steps = entry.get("steps")
                if not isinstance(steps, list) \
                        or not all(isinstance(s, str) for s in steps):
                    problems.append(
                        f"check {check_id!r} steps must be a list of str")
                if not isinstance(entry.get("sig"), str):
                    problems.append(
                        f"check {check_id!r} sig must be a str")

        probs_ret = fam.problems(config)
        if not isinstance(probs_ret, tuple) or len(probs_ret) != 2 \
                or not isinstance(probs_ret[0], list) \
                or not isinstance(probs_ret[1], list):
            problems.append(
                "problems must return (problems, warnings) tuple of lists")
        elif probs_ret[0]:
            problems.append(
                f"EXAMPLE must have no problems, got {probs_ret[0]!r}")

        body_ret = fam.body(config, device)
        if not isinstance(body_ret, dict):
            problems.append(
                f"body must return a dict, got {type(body_ret).__name__}")
        elif body_ret:
            head = body_ret.get("head")
            rows = body_ret.get("rows")
            if not isinstance(head, list) \
                    or not all(isinstance(h, str) for h in head):
                problems.append("body head must be a list of str")
            elif not isinstance(rows, list):
                problems.append("body rows must be a list")
            else:
                for row in rows:
                    if not isinstance(row, (list, tuple)) \
                            or len(row) != len(head):
                        problems.append(
                            "body rows must each have the same length "
                            "as head")
                        break

        cmd_ret = fam.command("/tmp/example.json", config)
        if not isinstance(cmd_ret, (tuple, list)) or len(cmd_ret) != 2:
            problems.append("command must return (argv, env)")
        else:
            argv, env = cmd_ret
            if not isinstance(argv, list) or len(argv) < 2 \
                    or not all(isinstance(a, str) for a in argv):
                problems.append(
                    "command argv must be a list of at least two str")
            if not isinstance(env, dict) or not all(
                    isinstance(k, str) and isinstance(v, str)
                    for k, v in env.items()):
                problems.append("command env must be a dict of str to str")
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        problems.append(
            f"builder side crashed on the EXAMPLE: "
            f"{type(exc).__name__}: {exc}")


def _check_live(fam, problems: list[str]) -> None:
    discover = getattr(fam, "discover", None)
    if not callable(discover):
        return  # already reported by the hooks check
    try:
        found = discover({})
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        problems.append(
            f"discover crashed: {type(exc).__name__}: {exc}")
        return
    if not isinstance(found, list):
        problems.append(
            f"discover must return a list, got {type(found).__name__}")
        return
    for dev in found:
        if not isinstance(dev, dict):
            problems.append("discover devices must be dicts")
            continue
        if not isinstance(dev.get("id"), str) \
                or not isinstance(dev.get("name"), str) \
                or not isinstance(dev.get("ready"), bool) \
                or not isinstance(dev.get("status"), str):
            problems.append(
                "discover devices must have id, name, ready bool "
                "and status str")
            break


def check(fam, live: bool = False) -> list[str]:
    """Return the list of conformance problems for family *fam*."""
    try:
        module = _resolve(fam)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return [f"cannot load family {fam!r}: {type(exc).__name__}: {exc}"]
    problems: list[str] = []
    _check_constants(module, problems)
    _check_hooks(module, problems)
    _check_example(module, problems)
    if live:
        _check_live(module, problems)
    return problems


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    live = "--live" in args
    names = [a for a in args if a != "--live"]
    if not names:
        try:
            from families import NAMES
        except ImportError:
            from . import NAMES  # type: ignore
        names = list(NAMES)
    failed = False
    for name in names:
        try:
            try:
                from families import get as _get
            except ImportError:
                from . import get as _get  # type: ignore
            module = _get(name)
        except Exception as exc:  # noqa: BLE001 - CLI reports it
            print(f"FAIL {name}")
            print(f"  {type(exc).__name__}: {exc}")
            failed = True
            continue
        problems = check(module, live=live)
        if problems:
            print(f"FAIL {name}")
            for problem in problems:
                print(f"  {problem}")
            failed = True
        else:
            print(f"ok  {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
