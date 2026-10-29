"""Families: everything the house knows about one kind of device.

This docstring IS the contract document. Every family module
(``families/<name>.py``) must provide the constants and hooks below with
these exact shapes. Nothing above layer 2 branches on device family;
callers go through this registry and these hooks only.

Constants
---------
NAME : str
    Family id matching ^[a-z][a-z0-9_]{0,31}$ (for example ``"pins"``).
TITLE : str
    Human title for headings, for example ``"Arduino board"``.
SCAN : str
    Plain words for the UI scan button / status, a non-empty string in
    plain words a person can read (no codes, no jargon).
GUIDE : str
    Builder guidance, a string of at least 40 characters (``len(GUIDE) >=
    40``). ``guides()`` publishes it under the family title.
CONNECT : dict
    ``{"label", "hint", "fields": [{"id", "label", "secret"?}]}`` --
    ``label`` and ``hint`` are strings; ``fields`` is a list of field
    dicts each with ``"id"`` and ``"label"`` strings and an optional
    ``"secret"`` boolean. A form (``fields`` non-empty) needs a label:
    when a form is shown, ``CONNECT["label"]`` must be non-empty.
PLAN_PARAMS : dict
    JSON-schema properties: a mapping of property name to a schema
    fragment, and every entry must carry a ``"type"``. An empty dict
    means the plan takes no parameters.
CHECK_HINT : str
    One-line hint shown before the checks (a string, may be empty only
    when there is truly nothing to warn about).
FIXTURE : None | "mic" | "speaker"
    ``None`` for ordinary families, or ``"mic"`` / ``"speaker"`` when the
    family is backed by the house mic / speaker fixture.
CLAIMS_PORTS : optional True
    Optional. Omit it normally; set it to ``True`` (the only allowed
    value when present) when the family claims serial/USB ports and the
    workshop must scan it first.
REACH : optional "network"
    Optional. Omit it for a device on a cable to one machine (it can
    only run there). ``"network"`` (the only allowed value when present)
    means any workshop that reaches the device and holds its account can
    run it, so the house can move it when its workshop leaves (see the
    stand-in hooks below).
EXAMPLE : dict
    ``{"device", "plan"}`` -- an example device dict and an example plan
    dict the builder side must handle without problems. Conformance runs
    the pure hooks on a deepcopy of this value.

Builder-side pure hooks (no hardware, no network, no secrets)
-------------------------------------------------------------
setup(config, device)
    ``setup(config, device)`` fills family defaults into ``config`` for
    ``device`` and returns the config dict.
plan(config, args) -> str
    ``plan(config, args) -> str`` validates / applies the plan arguments
    ``args`` against ``config`` and returns a string (``""`` when there
    is nothing to say). It returns ``str``, never a list.
checks(config, device) -> {id: {"title", "steps": [str], "sig": str}}
    ``checks(config, device)`` returns a mapping of check id to
    ``{"title", "steps": [str], "sig": str}`` with at least one check;
    ``steps`` is a list of strings and ``sig`` is a string signature of
    what the check covers.
problems(config) -> (problems, warnings)
    ``problems(config) -> (problems, warnings)`` returns a tuple of two
    lists ``(problems, warnings)``. On a valid example the problems list
    must be empty (warnings are allowed).
body(config, device) -> {} | {"head": [str], "rows": [[str]]}
    ``body(config, device)`` returns ``{}`` or
    ``{"head": [str], "rows": [[str]]}`` where every row has the same
    length as ``head``.

Workshop-side hooks (hardware / network live here)
--------------------------------------------------
discover(taken) -> [device]
    ``discover(taken)`` returns a list of device dicts, each with
    ``id``, ``name``, ``ready: bool`` and ``status`` (plus optional
    ``usb`` / ``robot`` / ``unsupported``). ``taken`` maps device id to
    the robot already using it. With ``live`` checking it is called as
    ``discover({})`` and must return a list, not raise.
connect(device, fields) -> (text, device)
    ``connect(device, fields) -> (text, device)`` performs the family
    connect step (flash, log in, join Wi-Fi, ...) from the form
    ``fields`` and returns ``(text, device)``: a human ``text`` string
    and the updated device dict.
check(device, config, check_id) -> {"expect", ...}
    ``check(device, config, check_id)`` runs one build check and returns
    ``{"expect", reading?, image?, audio?, device?}`` -- always
    ``"expect"`` (a string), optionally ``"reading"``, optionally
    ``"image"`` (JPEG bytes), optionally ``"audio"`` (WAV base64) and
    optionally an updated ``"device"``.
command(config_path, config) -> (argv, env)
    ``command(config_path, config) -> (argv, env)`` returns the driver
    command: ``argv`` is a list of at least two strings and ``env`` is a
    dict of ``str -> str``.
update(device) -> (text, device) [optional]
    ``update(device) -> (text, device)`` is optional: when present it
    refreshes the device in place and returns ``(text, device)``.
    Conformance does not check this hook.

Stand-in hooks [optional, for ``REACH = "network"``]
--------------------------------------------------
ready_here(device, config_path) -> str
    ``""`` when this workshop could run the device right now, else why
    not in plain words (no account here, can't reach it).
export_secret(device, config_path) -> dict
    The device's account as stored on this workshop (``{}`` if none),
    for handing to a stand-in workshop. The hub relays it in memory and
    never stores, logs or prompts with it.
import_secret(device, config_path, secret) -> None
    Store an account handed over by another workshop.

Registry
--------
``NAMES = ("pins", "onvif", "robohon", "mic", "mbot2")`` in this order;
``FAMILIES`` maps name -> module, imported eagerly. ``get(name)`` returns
the module and raises ``ValueError`` naming the known families for an
unknown name. ``guides()`` joins ``"## <TITLE> (family: <NAME>)\\n<GUIDE>"``
for each family in ``NAMES`` order.

Secrets
-------
``save_secret(device_id, values)`` / ``load_secret(device_id) -> dict``
store one JSON file per device in the secrets folder (the
``HOUSE_WORKSHOP_SECRETS`` environment variable, else
``<repo>/data/secrets``). The filename is the device id with every
character outside ``[A-Za-z0-9_.-]`` replaced by ``"_"`` (plus
``".json"``). The folder is mode ``0700`` and each file is created with
mode ``0600`` BEFORE any secret is written. ``load_secret`` returns
``{}`` on any error. Secrets are never printed, logged or committed.
"""
from __future__ import annotations

import importlib
import json
import os
import re
from pathlib import Path

NAMES: tuple[str, ...] = ("pins", "onvif", "robohon", "mic", "mbot2")

FAMILIES: dict = {}

for _name in NAMES:
    try:
        FAMILIES[_name] = importlib.import_module(f".{_name}", __package__)
    except Exception:
        # A family whose module is not present yet (or whose optional
        # dependencies are missing) must not stop `import families`.
        continue
del _name


def get(name: str):
    """Return the family module for *name*.

    Raises ValueError naming the known families for an unknown name.
    """
    mod = FAMILIES.get(name)
    if mod is not None:
        return mod
    if name in NAMES:
        # The module may have appeared since the eager import.
        try:
            mod = importlib.import_module(f".{name}", __package__)
            FAMILIES[name] = mod
            return mod
        except Exception:
            pass
    raise ValueError(
        f"unknown family {name!r} (known: {', '.join(NAMES)})")


def guides() -> str:
    """All family guides, one headed section per family in NAMES order."""
    parts: list[str] = []
    for name in NAMES:
        mod = FAMILIES.get(name)
        if mod is None:
            try:
                mod = get(name)
            except Exception:
                continue
        title = getattr(mod, "TITLE", name)
        guide = getattr(mod, "GUIDE", "")
        fam_name = getattr(mod, "NAME", name)
        parts.append(f"## {title} (family: {fam_name})\n{guide}")
    return "\n\n".join(parts)


# -- secrets -----------------------------------------------------------------

_SAFE = re.compile(r"[^A-Za-z0-9_.\-]")


def _secrets_dir() -> Path:
    env = os.environ.get("HOUSE_WORKSHOP_SECRETS")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent.parent / "data" / "secrets"


def _secret_path(device_id) -> Path:
    cleaned = _SAFE.sub("_", str(device_id))
    if not cleaned:
        cleaned = "_"
    return _secrets_dir() / f"{cleaned}.json"


def save_secret(device_id, values) -> Path:
    """Write *values* as the JSON secret for *device_id*; return the path."""
    folder = _secrets_dir()
    folder.mkdir(parents=True, exist_ok=True)
    try:
        folder.chmod(0o700)
    except OSError:
        pass
    path = _secret_path(device_id)
    # Create (or truncate) the file with mode 0600 BEFORE writing secrets.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1  # ownership moved to handle
            json.dump(values, handle, ensure_ascii=False)
            handle.write("\n")
    finally:
        if fd >= 0:
            os.close(fd)
    return path


def load_secret(device_id) -> dict:
    """Return the stored secret dict for *device_id*, {} on any error."""
    try:
        data = json.loads(_secret_path(device_id).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
