"""Builder-written families (drafts): the hub-side review.

A builder (an AI following a family's guide) writes a brand-new family
for a device the house has never seen: a folder of small files, always
including ``family.py`` (plain data plus the ``connect`` / ``check``
hooks) and ``driver.py`` (the body that will run on the workshop).

``review`` is the hub-side gate. It is pure and never executes draft
code: ``family.py`` data is read with ``ast.parse`` +
``ast.literal_eval`` of top-level single-name assignments only, and
everything else is read off the syntax tree. It returns::

    {"problems": [str],   # what is wrong (draft must be fixed)
     "reaches": [str],    # what the code reaches for (shown, allowed)
     "meta": {...},       # the literal DATA read out of family.py
     "imports": {file: [top modules]},
     "hash": str,         # 16-hex content hash of the reviewed files
     "files": {...}}      # the reviewed files (house files excluded)

``approved.json`` and ``provenance.json`` are the house's own files in
a draft folder; they are excluded everywhere (not counted, not hashed,
not returned).

``Declared`` turns reviewed ``meta`` into a family-shaped object the
house can plan with, without importing any draft code: the real
``connect`` / ``check`` hooks only ever run inside the workshop
sandbox (see bodies/workshop/).
"""
from __future__ import annotations

import ast
import hashlib
import json
import re

MAX_FILES = 8
MAX_FILE_BYTES = 64 * 1024

HOUSE_FILES = frozenset({"approved.json", "provenance.json"})

FILE_NAME_RE = re.compile(r"^[a-z0-9_]{1,40}\.(py|md|txt|json)$")
FAMILY_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
MATCH_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{4}$")
CHECK_ID_RE = re.compile(r"^[a-z0-9_]+$")

# The literal DATA a draft family.py must declare (CHECK_HINT optional).
DATA_KEYS = ("NAME", "TITLE", "GUIDE", "MATCH", "CONNECT", "CHECKS",
             "BODY", "CHECK_HINT", "EXAMPLE")
REQUIRED_DATA = ("NAME", "TITLE", "GUIDE", "MATCH", "CONNECT", "CHECKS",
                 "BODY", "EXAMPLE")

# Built-in families; a draft may never take one of these names. The
# registry's NAMES are folded in too when they can be read.
_BUILTIN_FAMILIES = frozenset({"pins", "onvif", "robohon", "mic", "mbot2"})

# Reaches: powerful imports, shown to the human, never forbidden.
_IMPORT_REACHES = {
    "subprocess": "runs other programs",
    "socket": "opens network connections directly",
    "urllib": "fetches web addresses directly",
    "requests": "fetches web addresses directly",
    "http": "talks to the web directly",
    "shutil": "moves or deletes files",
    "ctypes": "calls into system libraries directly",
    "multiprocessing": "starts extra processes",
    "threading": "starts background threads",
}

# Reaches: powerful calls (bare names or attributes, e.g. os.system).
_CALL_REACHES = {
    "eval": "runs text as code",
    "exec": "runs text as code",
    "compile": "turns text into code",
    "__import__": "loads code by name",
    "system": "runs shell commands",
    "popen": "runs other programs",
    "remove": "deletes files",
    "unlink": "deletes files",
    "rmtree": "deletes whole folders",
}

_WRITE_MODE_CHARS = "wax+"


def _known_names(known_families) -> set:
    """Existing family names: the built-ins plus *known_families*."""
    names = set(_BUILTIN_FAMILIES)
    try:  # the registry, when this module is imported as families.drafts
        from . import NAMES
        names.update(NAMES)
    except Exception:
        try:
            from families import NAMES  # type: ignore
            names.update(NAMES)
        except Exception:
            pass
    if known_families:
        if isinstance(known_families, dict):
            names.update(str(k) for k in known_families)
        elif isinstance(known_families, str):
            names.add(known_families)
        else:
            try:
                names.update(str(k) for k in known_families)
            except TypeError:
                names.add(str(known_families))
    return names


def _top_modules(tree: ast.Module) -> list:
    """Sorted top-level module names imported anywhere in *tree*."""
    tops = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                tops.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                tops.add(node.module.split(".")[0])
    return sorted(tops)


def _reaches(trees: dict) -> list:
    """One "<file> · <what>: <plain meaning>" per reach, sorted."""
    found = set()
    for fname in sorted(trees):
        tree = trees[fname]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in _IMPORT_REACHES:
                        found.add(f"{fname} · import {top}: "
                                  f"{_IMPORT_REACHES[top]}")
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    top = node.module.split(".")[0]
                    if top in _IMPORT_REACHES:
                        found.add(f"{fname} · import {top}: "
                                  f"{_IMPORT_REACHES[top]}")
            elif isinstance(node, ast.Call):
                func = node.func
                called = None
                if isinstance(func, ast.Name):
                    called = func.id
                elif isinstance(func, ast.Attribute):
                    called = func.attr
                if called in _CALL_REACHES:
                    found.add(f"{fname} · {called}(): "
                              f"{_CALL_REACHES[called]}")
                is_open = (isinstance(func, ast.Name) and func.id == "open") \
                    or (isinstance(func, ast.Attribute) and func.attr == "open")
                if is_open:
                    mode = None
                    if len(node.args) >= 2 \
                            and isinstance(node.args[1], ast.Constant) \
                            and isinstance(node.args[1].value, str):
                        mode = node.args[1].value
                    for keyword in node.keywords:
                        if keyword.arg == "mode" \
                                and isinstance(keyword.value, ast.Constant) \
                                and isinstance(keyword.value.value, str):
                            mode = keyword.value.value
                    if mode and any(c in mode for c in _WRITE_MODE_CHARS):
                        found.add(f"{fname} · open() mode {mode!r}: "
                                  "opens a file for writing")
            elif isinstance(node, ast.Constant) \
                    and isinstance(node.value, str) \
                    and node.value.startswith("/dev/"):
                shown = node.value if len(node.value) <= 48 \
                    else node.value[:45] + "..."
                found.add(f'{fname} · "{shown}": '
                          "talks straight to a device file")
    return sorted(found)


def _read_data(tree: ast.Module, known: set):
    """Literal DATA out of family.py; returns (meta, problems)."""
    data: dict = {}
    nonliteral: set = set()
    for node in tree.body:
        name = None
        value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            name, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) \
                and isinstance(node.target, ast.Name) \
                and node.value is not None:
            name, value = node.target.id, node.value
        if name is None or name not in DATA_KEYS:
            continue
        try:
            data[name] = ast.literal_eval(value)
            nonliteral.discard(name)
        except Exception:
            data.pop(name, None)
            nonliteral.add(name)

    problems: list[str] = []
    for key in DATA_KEYS:
        if key in nonliteral:
            problems.append(
                f"family.py: {key} must be plain literals "
                "(literals only — no calls, names or expressions)")
    for key in REQUIRED_DATA:
        if key not in data and key not in nonliteral:
            problems.append(f"family.py: missing {key}")

    if "NAME" in data:
        value = data["NAME"]
        if not isinstance(value, str) or not FAMILY_NAME_RE.match(value):
            problems.append(
                "family.py: NAME must match ^[a-z][a-z0-9_]{1,31}$, "
                f"got {value!r}")
        elif value in known:
            problems.append(f"family.py: NAME {value!r} is already a family")
    if "TITLE" in data:
        if not isinstance(data["TITLE"], str) or not data["TITLE"].strip():
            problems.append("family.py: TITLE must be a non-empty string")
    if "GUIDE" in data:
        guide = data["GUIDE"]
        if not isinstance(guide, str):
            problems.append("family.py: GUIDE must be a string")
        elif len(guide) < 40:
            problems.append(
                f"family.py: GUIDE must be at least 40 characters, "
                f"got {len(guide)}")
    if "MATCH" in data:
        match = data["MATCH"]
        if not isinstance(match, list):
            problems.append(
                'family.py: MATCH must be a list of "vvvv:pppp" strings')
        elif not match:
            problems.append(
                'family.py: MATCH must list at least one "vvvv:pppp" pair')
        else:
            for entry in match:
                if not isinstance(entry, str) or not MATCH_RE.match(entry):
                    problems.append(
                        f"family.py: MATCH entry {entry!r} must look like "
                        '"vvvv:pppp" (lowercase hex)')
    if "CONNECT" in data:
        conn = data["CONNECT"]
        if not isinstance(conn, dict):
            problems.append("family.py: CONNECT must be a dict")
        else:
            fields = conn.get("fields")
            if "fields" not in conn:
                problems.append('family.py: CONNECT missing "fields"')
            elif not isinstance(fields, list):
                problems.append("family.py: CONNECT fields must be a list")
            else:
                for field in fields:
                    if not isinstance(field, dict) \
                            or not isinstance(field.get("id"), str) \
                            or not isinstance(field.get("label"), str):
                        problems.append(
                            "family.py: CONNECT fields must have "
                            "id and label strings")
                        break
                    if field.get("secret"):
                        problems.append(
                            f"family.py: CONNECT field {field['id']!r} "
                            "must not be secret (drafts never ask "
                            "for secrets)")
                        break
                if fields and isinstance(conn.get("label"), str) \
                        and not conn["label"].strip():
                    problems.append(
                        "family.py: CONNECT with fields needs a label")
    if "CHECKS" in data:
        checks = data["CHECKS"]
        if not isinstance(checks, dict):
            problems.append("family.py: CHECKS must be a dict")
        elif len(checks) < 1:
            problems.append("family.py: CHECKS must have at least one check")
        else:
            for check_id, entry in checks.items():
                if not isinstance(check_id, str) \
                        or not CHECK_ID_RE.match(check_id):
                    problems.append(
                        f"family.py: check id {check_id!r} must be lowercase")
                    continue
                if not isinstance(entry, dict):
                    problems.append(
                        f"family.py: check {check_id!r} must be a dict")
                    continue
                if not isinstance(entry.get("title"), str) \
                        or not entry["title"].strip():
                    problems.append(
                        f"family.py: check {check_id!r} needs a title")
                steps = entry.get("steps")
                if not isinstance(steps, list) \
                        or not all(isinstance(s, str) for s in steps):
                    problems.append(
                        f"family.py: check {check_id!r} steps must be "
                        "a list of strings")
    if "BODY" in data:
        body = data["BODY"]
        if not isinstance(body, dict):
            problems.append("family.py: BODY must be a dict")
        else:
            head, rows = body.get("head"), body.get("rows")
            if not isinstance(head, list) \
                    or not all(isinstance(h, str) for h in head):
                problems.append(
                    "family.py: BODY head must be a list of strings")
            elif not isinstance(rows, list):
                problems.append("family.py: BODY rows must be a list")
            else:
                for row in rows:
                    if not isinstance(row, (list, tuple)) \
                            or len(row) != len(head):
                        problems.append(
                            "family.py: BODY rows must each have the "
                            "same length as head")
                        break
    if "CHECK_HINT" in data and not isinstance(data["CHECK_HINT"], str):
        problems.append("family.py: CHECK_HINT must be a string")
    if "EXAMPLE" in data:
        example = data["EXAMPLE"]
        if not isinstance(example, dict):
            problems.append("family.py: EXAMPLE must be a dict")
        else:
            if not isinstance(example.get("device"), dict):
                problems.append("family.py: EXAMPLE needs a device dict")
            plan = example.get("plan")
            if not isinstance(plan, dict):
                problems.append("family.py: EXAMPLE needs a plan dict")
            else:
                for key in ("name", "description", "personality"):
                    if key not in plan:
                        problems.append(
                            f"family.py: EXAMPLE plan missing {key!r}")
                    elif not isinstance(plan[key], str):
                        problems.append(
                            f"family.py: EXAMPLE plan {key} must be "
                            "a string")

    meta = {key: data[key] for key in DATA_KEYS if key in data}
    return meta, problems


def _hook_problems(tree: ast.Module) -> list:
    problems: list[str] = []
    funcs: dict = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs[node.name] = node
    for hook, expected in (("connect", ("device", "fields")),
                           ("check", ("device", "config", "check_id"))):
        node = funcs.get(hook)
        if node is None:
            problems.append(f"family.py: missing {hook}()")
            continue
        args = [a.arg for a in node.args.posonlyargs + node.args.args]
        if tuple(args[:len(expected)]) != expected:
            problems.append(
                f"family.py: {hook}() must start with {expected}, "
                f"got {tuple(args)}")
    return problems


def _content_bytes(content) -> bytes:
    if isinstance(content, bytes):
        return content
    return str(content).encode("utf-8")


def review(files: dict, known_families=None) -> dict:
    """Review a draft family's files; see the module docstring."""
    kept = {name: content for name, content in (files or {}).items()
            if name not in HOUSE_FILES}
    problems: list[str] = []

    if len(kept) > MAX_FILES:
        problems.append(f"too many files: {len(kept)} (at most {MAX_FILES})")
    for name in sorted(kept):
        if not FILE_NAME_RE.match(str(name)):
            problems.append(
                f"bad file name {name!r} "
                "(want ^[a-z0-9_]{1,40}\\.(py|md|txt|json)$)")
        size = len(_content_bytes(kept[name]))
        if size > MAX_FILE_BYTES:
            problems.append(
                f"{name} is too big ({size} bytes, "
                f"at most {MAX_FILE_BYTES})")
    if "family.py" not in kept:
        problems.append("missing family.py")
    if "driver.py" not in kept:
        problems.append("missing driver.py")

    trees: dict = {}
    imports: dict = {}
    for name in sorted(kept):
        if not str(name).endswith(".py"):
            continue
        try:
            tree = ast.parse(_content_bytes(kept[name]).decode("utf-8"),
                             filename=str(name))
        except SyntaxError as exc:
            problems.append(
                f"{name}: syntax error on line {exc.lineno}: {exc.msg}")
            imports[name] = []
            continue
        except (UnicodeDecodeError, ValueError) as exc:
            problems.append(f"{name}: cannot read as Python: {exc}")
            imports[name] = []
            continue
        trees[name] = tree
        imports[name] = _top_modules(tree)

    reaches = _reaches(trees)

    meta: dict = {}
    if "family.py" in trees:
        meta, data_problems = _read_data(trees["family.py"],
                                         _known_names(known_families))
        problems.extend(data_problems)
        problems.extend(_hook_problems(trees["family.py"]))
    if "driver.py" in trees and "body" not in imports.get("driver.py", []):
        problems.append(
            "driver.py must import the body SDK (from body import Body)")

    digest = hashlib.sha256()
    for name in sorted(kept):
        digest.update(str(name).encode("utf-8"))
        digest.update(b"\0")
        digest.update(_content_bytes(kept[name]))
        digest.update(b"\0")

    return {"problems": problems, "reaches": reaches, "meta": meta,
            "imports": imports, "hash": digest.hexdigest()[:16],
            "files": dict(kept)}


class Declared:
    """A draft family as the house sees it, from reviewed meta only.

    The pure hooks are answered straight from the declared data; the
    real ``connect`` / ``check`` hooks never run on the hub — they run
    in the workshop sandbox and are reached through it, not through
    this object.
    """

    def __init__(self, meta: dict) -> None:
        self.meta = dict(meta or {})
        self.NAME = self.meta.get("NAME")
        self.TITLE = self.meta.get("TITLE")
        self.GUIDE = self.meta.get("GUIDE")
        conn = self.meta.get("CONNECT")
        self.CONNECT = conn if isinstance(conn, dict) and conn else {
            "label": "Nothing to connect", "hint": "", "fields": []}
        hint = self.meta.get("CHECK_HINT")
        self.CHECK_HINT = hint if isinstance(hint, str) else ""
        self.PLAN_PARAMS: dict = {}
        self.FIXTURE = None

    def setup(self, config, device):
        return config

    def plan(self, config, args) -> str:
        return ""

    def checks(self, config, device) -> dict:
        out: dict = {}
        declared = self.meta.get("CHECKS")
        if isinstance(declared, dict):
            for check_id, entry in declared.items():
                entry = entry if isinstance(entry, dict) else {}
                out[check_id] = {
                    "title": entry.get("title"),
                    "steps": entry.get("steps"),
                    "sig": json.dumps(entry, sort_keys=True),
                }
        return out

    def problems(self, config):
        return ([], [])

    def body(self, config, device) -> dict:
        declared = self.meta.get("BODY")
        return declared if isinstance(declared, dict) else {}
