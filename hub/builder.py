"""The builder (spec 09): builds stored as data/builds/<id>.json.

A Build is the whole sheet for one robot being made: the device chosen,
the config being planned, the checks, the conversation with the builder
brain, what it has cost, and whether it has become a robot yet.

The Builder is the brain's hands. It talks to a person through a
provider conversation (the souls' provider interface, builder role),
and to workshops only through ``house.act("@<workshop>", ...)``. It
never branches on a device family name: everything family-shaped comes
from the family modules' pure hooks (setup / plan / checks / problems /
body and the CONNECT / CHECK_HINT / FIXTURE / PLAN_PARAMS data).
"""
from __future__ import annotations

import asyncio
import base64
import copy
import inspect
import json
import re
import time
import uuid
from pathlib import Path

from . import apilog, hearing as hearing_mod, imaging, pricing
from .providers import ToolResult, ToolSpec, get_provider

try:  # the family registry (pure hooks live here on the hub side)
    from bodies import families as families_lib
    from bodies.families.drafts import Declared
except Exception:  # pragma: no cover - families are part of this repo
    families_lib = None  # type: ignore
    Declared = None  # type: ignore

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
MAX_STEPS = 10
MESSAGE_KEEP = 200

DRAFT_GUIDE = """Writing a new family for a device no family knows yet (a draft):
- Investigate read-only first: use inspect_device and listen_device to learn
  what the device is. Do not write to the device while investigating.
- Write the draft with list_files / read_file / write_file: family.py holds
  plain DATA (NAME, TITLE, GUIDE, MATCH, CONNECT, CHECKS, BODY, EXAMPLE) plus
  exactly two functions, connect(device, fields) and check(device, config,
  check_id); driver.py is the body that will run the robot, built on the body
  SDK (from body import Body); notes.md says what you learned.
- Then review_draft. Fix every problem it lists. Explain what the code reaches
  for (its "reaches") to the person in plain words.
- The person reads the code and presses Approve. Nothing in a draft runs
  before approval, and any change after approval needs a fresh approval.
  Never pressure the person to approve; if they are unsure, stop and explain.
- Only after approval: connect, plan, run the checks, then give_soul (which
  promotes the draft to a local family first).
- Draft code runs sandboxed: no network except the driver talking to the
  house, and no files outside its own folder.
- Order of trying things on an unknown device: identify the chip, listen to
  it boot, find the vendor library or protocol, try read-only calls, then
  reversible writes, then a tiny test, and only then the real thing."""

PROMPT = """You are the builder in a house of robots. You help {human}, who may
never have built anything, turn a device into a robot with its own soul.
You are warm, brief and concrete. You take one step at a time. You write
plain text: no emoji, no markdown tables, no code blocks in chat.

There are five steps:
1. Device: find a device on a workshop and choose it.
2. Connect: run the family's connect step. Some families need details
   (a password, an address). Those go in the form on the build sheet,
   never in this chat.
3. Body: agree a name and a plan for what the device will be.
4. Check: run each planned check, in order, and let the person judge
   what really happened.
5. Soul: when every check has passed, give it a soul and it starts up.

Equipment (a house microphone or speaker) is simpler: it gets a name
only, and the last step is "put to work", not a soul.

Secrets: you never handle secrets. Passwords and keys are typed into
the form fields on the build sheet, never in this chat. If a person
starts typing a password or key here, stop them at once, do not repeat
it back, and point them at the form on the build sheet.

Evidence over trust: a check's job is to show what the device really
did (a reading, a photo, a sound). Judge what the evidence shows and
ask the person what they saw; never just announce that something
worked because a tool said so.

Messages in [brackets] are events from the build sheet (a button the
person pressed, a check they marked), not words the person typed.

Your tools do the real work: find and choose devices, connect, plan,
run and mark checks, write draft families, and finally give_soul.
After a tool does something, say in one or two plain sentences what
happened and what the next single step is. If a tool fails, say what
went wrong in plain words and what could be tried next.

Family guides:
{guides}

{draft_guide}
"""


def _atomic_write(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(path)


def _family_module(ref):
    """A built-in family module, a Declared draft/local family, or None."""
    if not ref or ref == "new" or families_lib is None:
        return None
    if str(ref).startswith(("draft:", "local:")):
        return None  # resolved per-build from reviewed meta; see Builder
    try:
        return families_lib.get(str(ref))
    except Exception:
        return None


def family_catalog() -> list[dict]:
    """[{name, title, scan, connect}] for every built-in family."""
    out: list[dict] = []
    if families_lib is None:
        return out
    for name in families_lib.NAMES:
        try:
            fam = families_lib.get(name)
        except Exception:
            continue
        out.append({
            "name": getattr(fam, "NAME", name),
            "title": getattr(fam, "TITLE", name),
            "scan": getattr(fam, "SCAN", ""),
            "connect": copy.deepcopy(getattr(fam, "CONNECT", {}) or {}),
        })
    return out


def _plan_properties() -> dict:
    """The UNION of every family's PLAN_PARAMS properties."""
    props: dict = {}
    if families_lib is None:
        return props
    for name in families_lib.NAMES:
        try:
            fam = families_lib.get(name)
        except Exception:
            continue
        params = getattr(fam, "PLAN_PARAMS", {}) or {}
        if isinstance(params, dict) and isinstance(params.get("properties"), dict):
            entries = params["properties"]
        elif isinstance(params, dict):
            entries = params
        else:
            entries = {}
        for key, value in entries.items():
            if isinstance(value, dict):
                props.setdefault(key, copy.deepcopy(value))
    return props


class Build:
    """One build sheet, stored whole as data/builds/<id>.json."""

    def __init__(self, builder: "Builder", data: dict | None = None) -> None:
        self.builder = builder
        self.house = builder.house
        now = time.time()
        self.data: dict = {
            "id": uuid.uuid4().hex[:8],
            "created": now,
            "workshop": None,
            "device": None,
            "config": {},
            "checks": [],
            "messages": [],
            "conversation": None,
            "usage": {"usd": 0.0, "calls": 0},
            "robot": None,
            "name": None,
            "draft": None,
            "busy": False,
        }
        if data:
            for key, value in data.items():
                self.data[key] = value
        if not isinstance(self.data.get("config"), dict):
            self.data["config"] = {}
        if not isinstance(self.data.get("checks"), list):
            self.data["checks"] = []
        if not isinstance(self.data.get("messages"), list):
            self.data["messages"] = []
        usage = self.data.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        usage.setdefault("usd", 0.0)
        usage.setdefault("calls", 0)
        self.data["usage"] = usage
        self._lock: asyncio.Lock | None = None

    # -- stored fields ------------------------------------------------------
    @property
    def id(self) -> str:
        return str(self.data.get("id") or "")

    @property
    def created(self) -> float:
        return float(self.data.get("created") or 0)

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @property
    def workshop(self):
        return self.data.get("workshop")

    @workshop.setter
    def workshop(self, value) -> None:
        self.data["workshop"] = value

    @property
    def device(self):
        return self.data.get("device")

    @device.setter
    def device(self, value) -> None:
        self.data["device"] = value

    @property
    def config(self) -> dict:
        return self.data.setdefault("config", {})

    @config.setter
    def config(self, value) -> None:
        self.data["config"] = value

    @property
    def checks(self) -> list:
        return self.data.setdefault("checks", [])

    @checks.setter
    def checks(self, value) -> None:
        self.data["checks"] = value

    @property
    def messages(self) -> list:
        return self.data.setdefault("messages", [])

    @property
    def conversation(self):
        return self.data.get("conversation")

    @conversation.setter
    def conversation(self, value) -> None:
        self.data["conversation"] = value

    @property
    def usage(self) -> dict:
        return self.data.setdefault("usage", {"usd": 0.0, "calls": 0})

    @property
    def robot(self):
        return self.data.get("robot")

    @robot.setter
    def robot(self, value) -> None:
        self.data["robot"] = value

    @property
    def draft(self):
        return self.data.get("draft")

    @draft.setter
    def draft(self, value) -> None:
        self.data["draft"] = value

    @property
    def busy(self) -> bool:
        return bool(self.data.get("busy"))

    @busy.setter
    def busy(self, value) -> None:
        self.data["busy"] = bool(value)

    @property
    def family_ref(self) -> str:
        config = self.data.get("config") or {}
        if config.get("family"):
            return str(config["family"])
        device = self.data.get("device") or {}
        if isinstance(device, dict) and device.get("family"):
            return str(device["family"])
        return ""

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value) -> None:
        self.data[key] = value

    # -- behaviour (delegates to the Builder) --------------------------------
    def save(self) -> "Build":
        return self.builder.save(self)

    def sheet(self) -> dict:
        return self.builder.sheet(self)

    @property
    def stage(self) -> str:
        return self.builder.stage(self)

    async def choose_device(self, workshop: str, device_id: str) -> str:
        return await self.builder.choose_device(self, workshop, device_id)

    async def settle(self) -> None:
        return await self.builder.settle(self)

    async def connect(self, fields: dict | None = None) -> str:
        return await self.builder.connect(self, fields)

    async def plan(self, args: dict | None = None) -> str:
        return await self.builder.plan(self, args)

    async def check(self, cid: str) -> str:
        return await self.builder.check(self, cid)

    async def mark(self, cid: str, worked: bool, note: str | None = None) -> str:
        return await self.builder.mark(self, cid, worked, note)

    async def give_soul(self) -> str:
        return await self.builder.give_soul(self)


class Builder:
    """Loads every build at hub start; turns builds with the model."""

    def __init__(self, house, provider=None) -> None:
        self.house = house
        if provider is None:
            try:
                provider = get_provider("builder")
            except Exception:
                provider = getattr(house, "provider", None)
        self.provider = provider
        self.data_dir = Path(house.data_dir)
        self.builds_dir = self.data_dir / "builds"
        self.builds: dict[str, Build] = {}
        self._load()
        self._adopt_records()

    # -- storage --------------------------------------------------------------
    def _path(self, bid: str) -> Path:
        return self.builds_dir / f"{bid}.json"

    def _load(self) -> None:
        if not self.builds_dir.exists():
            return
        for path in sorted(self.builds_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue  # unreadable builds are skipped, never fatal
            if not isinstance(data, dict) or not data.get("id"):
                continue
            data["busy"] = False  # a restarted hub has nobody mid-turn
            build = Build(self, data)
            self.builds[build.id] = build

    def _adopt_records(self) -> None:
        """At hub start, the record adopts what finished builds know."""
        registry = getattr(self.house, "registry", None)
        if registry is None or not hasattr(registry, "adopt"):
            return
        for build in self.builds.values():
            if not build.robot:
                continue
            sheet = self.sheet(build)
            try:
                registry.adopt(
                    build.robot,
                    kind=sheet.get("fixture") or "robot",
                    family=sheet.get("family"),
                    display_name=(build.config.get("display_name")
                                  or build.robot),
                    device=build.config.get("device") or build.device,
                    workshop=build.workshop,
                    runner="workshop",
                    build=build.id,
                )
            except Exception:
                continue

    def create(self) -> Build:
        build = Build(self)
        while build.id in self.builds:  # 8 hex chars, but never collide
            build.data["id"] = uuid.uuid4().hex[:8]
        self.builds[build.id] = build
        self.save(build)
        return build

    # aliases other callers may reach for
    new_build = create
    create_build = create
    new = create

    def get(self, bid: str) -> Build:
        build = self.builds.get(str(bid))
        if build is None:
            raise KeyError(f"no build '{bid}'")
        return build

    def list_builds(self) -> list[Build]:
        return sorted(self.builds.values(), key=lambda b: b.created)

    def save(self, build: Build) -> Build:
        build.messages[:] = build.messages[-MESSAGE_KEEP:]
        _atomic_write(self._path(build.id), build.data)
        emit = getattr(self.house, "emit", None)
        if callable(emit):
            try:
                emit("build", "", id=build.id, stage=self.stage(build))
            except Exception:
                pass
        return build

    def forget_robot(self, rid: str) -> None:
        """Drop (locally) every build that made robot *rid*."""
        for build in list(self.builds.values()):
            if build.robot == rid or build.config.get("name") == rid:
                self.builds.pop(build.id, None)
                try:
                    self._path(build.id).unlink()
                except OSError:
                    pass

    async def discard(self, build: Build) -> None:
        """Delete a build, discarding its draft on its workshop."""
        if self._is_draft(build) and build.workshop:
            try:
                await self._act(f"@{build.workshop}", "draft_file",
                                {"draft": build.id, "op": "discard"})
            except Exception:
                pass  # the sheet goes either way; the draft folder is stale
        self.builds.pop(build.id, None)
        try:
            self._path(build.id).unlink()
        except OSError:
            pass
        folder = self.builds_dir / build.id
        if folder.is_dir():
            for photo in folder.glob("*.jpg"):
                try:
                    photo.unlink()
                except OSError:
                    pass
            try:
                folder.rmdir()
            except OSError:
                pass

    # -- family resolution ------------------------------------------------------
    def _is_draft(self, build: Build) -> bool:
        return build.family_ref.startswith("draft:") \
            or build.draft is not None

    def family(self, build: Build | None = None, ref: str | None = None):
        """The family object for *ref* (or the build's family)."""
        ref = ref if ref is not None else (build.family_ref if build else "")
        if not ref or ref == "new":
            return None
        if str(ref).startswith(("draft:", "local:")):
            meta: dict = {}
            if build is not None:
                cached = build.data.get("_draft_meta")
                if isinstance(cached, dict):
                    meta = cached
            if Declared is None:
                return None
            declared = Declared(meta)
            fallback_name = str(ref).split(":", 1)[1]
            if not getattr(declared, "NAME", None):
                declared.NAME = meta.get("NAME") or fallback_name
            if not getattr(declared, "TITLE", None):
                declared.TITLE = meta.get("TITLE") or declared.NAME
            declared.SCAN = meta.get("SCAN") or "devices"
            return declared
        return _family_module(str(ref))

    def family_title(self, build: Build | None = None,
                     ref: str | None = None) -> str:
        ref = ref if ref is not None else (build.family_ref if build else "")
        fam = self.family(build, ref)
        if fam is not None and getattr(fam, "TITLE", None):
            return str(fam.TITLE)
        return str(ref or "")

    async def _ensure_meta(self, build: Build) -> None:
        """Cache a draft family's reviewed meta (pure data, via review)."""
        if not build.family_ref.startswith(("draft:", "local:")):
            return
        if isinstance(build.data.get("_draft_meta"), dict):
            return
        if not build.workshop:
            return
        try:
            result = await self._act(f"@{build.workshop}", "draft_review",
                                     {"draft": build.id})
        except Exception:
            return
        data = result.get("data") if isinstance(result, dict) else None
        if isinstance(data, dict) and isinstance(data.get("meta"), dict):
            build.data["_draft_meta"] = data["meta"]

    # -- the sheet --------------------------------------------------------------
    @staticmethod
    def stage(build: Build) -> str:
        if build.robot:
            return "done"
        device = build.device if isinstance(build.device, dict) else {}
        if not device.get("id"):
            return "found"
        if not device.get("ready"):
            return "connect"
        if not str(build.data.get("name") or "").strip():
            return "body"
        if any(c.get("state") != "passed" for c in build.checks):
            return "check"
        return "soul"

    def sheet(self, build: Build) -> dict:
        fam = self.family(build)
        device = build.device if isinstance(build.device, dict) else None
        body: dict = {}
        if device and device.get("ready") and fam is not None:
            try:
                made = fam.body(copy.deepcopy(build.config),
                                copy.deepcopy(device))
                if isinstance(made, dict):
                    body = made
            except Exception:
                body = {}
        problems: list = []
        warnings: list = []
        if fam is not None and build.config:
            try:
                found = fam.problems(copy.deepcopy(build.config))
                if isinstance(found, (list, tuple)) and len(found) == 2:
                    problems = [str(p) for p in (found[0] or [])]
                    warnings = [str(w) for w in (found[1] or [])]
            except Exception:
                problems, warnings = [], []
        checks = []
        for entry in build.checks:
            if not isinstance(entry, dict):
                continue
            shown = {"id": entry.get("id"), "title": entry.get("title"),
                     "steps": list(entry.get("steps") or []),
                     "state": entry.get("state") or "todo"}
            for key in ("expect", "reading", "note", "image", "tested_at"):
                if entry.get(key) is not None:
                    shown[key] = copy.deepcopy(entry[key])
            checks.append(shown)
        return {
            "id": build.id,
            "created": build.created,
            "workshop": build.workshop,
            "name": build.data.get("name") or None,
            "family": build.family_ref or None,
            "family_title": self.family_title(build) or None,
            "connect": copy.deepcopy(getattr(fam, "CONNECT", {}) or {})
                       if fam is not None else {},
            "check_hint": getattr(fam, "CHECK_HINT", "") if fam is not None else "",
            "device": copy.deepcopy(device),
            "config": copy.deepcopy(build.config),
            "body": body,
            "checks": checks,
            "problems": problems,
            "warnings": warnings,
            "messages": copy.deepcopy(build.messages[-MESSAGE_KEEP:]),
            "robot": build.robot,
            "usage": copy.deepcopy(build.usage),
            "busy": build.busy,
            "stage": self.stage(build),
            "fixture": getattr(fam, "FIXTURE", None) if fam is not None else None,
            "draft": copy.deepcopy(build.draft),
        }

    # -- workshop acts -----------------------------------------------------------
    async def _act(self, key: str, action: str, args: dict | None = None,
                   timeout: float | None = None) -> dict:
        if timeout is None:
            result = await self.house.act(key, action, args or {})
        else:
            result = await self.house.act(key, action, args or {},
                                          timeout=timeout)
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, dict) else \
            {"ok": False, "text": str(result), "data": None}

    @staticmethod
    def _act_data(result: dict):
        """(text, data) from a House.act result, parsing JSON text too."""
        data = result.get("data")
        text = result.get("text") or ""
        if data is None and isinstance(text, str):
            try:
                data = json.loads(text)
            except ValueError:
                data = None
        return text, data

    def _append(self, build: Build, role: str, text: str) -> None:
        build.messages.append({"t": time.time(), "role": role,
                               "text": str(text)})
        build.messages[:] = build.messages[-MESSAGE_KEEP:]

    # -- the steps (also the builder's tools) --------------------------------------
    async def choose_device(self, build: Build, workshop: str,
                            device_id: str) -> str:
        result = await self._act(f"@{workshop}", "devices", {})
        if not result.get("ok"):
            raise ValueError(result.get("text") or "the workshop didn't answer")
        _text, data = self._act_data(result)
        devices = data if isinstance(data, list) else []
        found = [d for d in devices if isinstance(d, dict)]
        device = next((d for d in found
                       if str(d.get("id")) == str(device_id)), None)
        if device is None:
            listed = ", ".join(f"{workshop}:{d.get('id')}" for d in found) \
                or "none"
            raise ValueError(
                f"no device '{device_id}' on {workshop} (found: {listed})")
        if device.get("robot"):
            raise ValueError(
                f"that device already belongs to {device['robot']}: "
                "it is a robot, not a spare device")
        if device.get("unsupported"):
            raise ValueError(
                "that device is unsupported: "
                + str(device.get("status") or "no family can run it"))
        build.workshop = workshop
        build.device = copy.deepcopy(device)
        ref = str(device.get("family") or "")
        if ref == "new":
            build.config = {"family": f"draft:{build.id}"}
            build.draft = {}
            self.save(build)
            return (
                f"chosen: {device.get('name') or device_id} on {workshop} — "
                "no family knows it yet. Its draft family is "
                f"draft:{build.id}: investigate it read-only (inspect_device, "
                "listen_device), then write the draft family files with "
                "write_file (family.py, driver.py, notes.md), review_draft, "
                "fix any problems, explain the reaches in plain words, and "
                "let the person read it and press Approve before anything runs.")
        build.config = {"family": ref} if ref else {}
        await self.settle(build)
        title = self.family_title(build)
        reply = (f"chosen: {device.get('name') or device_id} ({title}) on "
                 f"{workshop} — {device.get('status') or ''}").rstrip()
        fam = self.family(build)
        if fam is not None:
            try:
                made = fam.body(copy.deepcopy(build.config),
                                copy.deepcopy(build.device))
            except Exception:
                made = {}
            if isinstance(made, dict) and made.get("rows"):
                lines = []
                if made.get("head"):
                    lines.append(" · ".join(str(h) for h in made["head"]))
                for row in made.get("rows") or []:
                    lines.append(" · ".join(str(c) for c in row))
                if lines:
                    reply += "\n" + "\n".join(lines)
        self.save(build)
        return reply

    async def settle(self, build: Build) -> None:
        """config.family, config.device (minus ready/status/robot/where),
        then the family's setup()."""
        await self._ensure_meta(build)
        device = build.device if isinstance(build.device, dict) else {}
        ref = build.family_ref
        if ref == "new":
            ref = f"draft:{build.id}"
        config = copy.deepcopy(build.config)
        if ref:
            config["family"] = ref
        stripped = {k: copy.deepcopy(v) for k, v in device.items()
                    if k not in ("ready", "status", "robot", "where")}
        config["device"] = stripped
        fam = self.family(build, ref)
        if fam is not None and hasattr(fam, "setup"):
            try:
                made = fam.setup(copy.deepcopy(config), copy.deepcopy(device))
            except TypeError:
                made = fam.setup(copy.deepcopy(config))
            if inspect.isawaitable(made):
                made = await made
            if isinstance(made, dict):
                config = made
                config["family"] = ref or config.get("family")
                if "device" not in config:
                    config["device"] = stripped
        if isinstance(config.get("device"), dict):
            config["device"] = {k: v for k, v in config["device"].items()
                                if k not in ("ready", "status", "robot",
                                             "where")}
        build.config = config
        self.save(build)

    async def connect(self, build: Build, fields: dict | None = None) -> str:
        await self._ensure_meta(build)
        fields = dict(fields or {})
        fam = self.family(build)
        connect = getattr(fam, "CONNECT", {}) or {} if fam is not None else {}
        wanted = connect.get("fields") or []
        if wanted and not fields:
            labels = ", ".join(str(f.get("label") or f.get("id"))
                               for f in wanted if isinstance(f, dict))
            raise ValueError(
                f"this needs {labels}: ask the person to fill it in there, "
                "never in the chat")
        if not build.workshop or not isinstance(build.device, dict):
            raise ValueError("choose a device first")
        result = await self._act(
            f"@{build.workshop}", "connect",
            {"family": build.family_ref, "device": build.device,
             "fields": fields}, timeout=300)
        if not result.get("ok"):
            raise ValueError(result.get("text") or "connect failed")
        text, data = self._act_data(result)
        family_text = text
        updated = None
        if isinstance(data, dict):
            family_text = str(data.get("text") or text)
            if isinstance(data.get("device"), dict):
                updated = data["device"]
        if updated is not None:
            merged = dict(build.device)
            merged.update(copy.deepcopy(updated))
            if build.device.get("family"):
                merged.setdefault("family", build.device["family"])
            build.device = merged
        await self.settle(build)
        self.save(build)
        return family_text

    async def plan(self, build: Build, args: dict | None = None) -> str:
        await self._ensure_meta(build)
        args = dict(args or {})
        name = str(args.get("name") or "")
        if not NAME_RE.match(name):
            raise ValueError(
                "the name must be lowercase and match "
                "^[a-z][a-z0-9_-]{0,31}$ (letters, numbers, - and _)")
        own = build.data.get("name") or build.robot
        if name != own:
            registry = getattr(self.house, "registry", None)
            taken = set()
            if registry is not None:
                try:
                    taken = {r.get("id") for r in registry.all()}
                except Exception:
                    taken = set()
            taken |= set(getattr(self.house, "manifests", {}) or {})
            taken |= set(getattr(self.house, "bodies", {}) or {})
            taken |= set(getattr(self.house, "fixtures", {}) or {})
            if name in taken:
                raise ValueError(f"{name} already lives in this house")
        fam = self.family(build)
        if fam is None:
            raise ValueError("choose a device first")
        fixture = getattr(fam, "FIXTURE", None)
        if not fixture:  # a robot (equipment gets a name only)
            if not str(args.get("description") or "").strip():
                raise ValueError("a robot needs a description")
            if not str(args.get("personality") or "").strip():
                raise ValueError("a robot needs a personality")
        config = copy.deepcopy(build.config)  # work on a copy until it plans
        config["name"] = name
        if args.get("display_name"):
            config["display_name"] = str(args["display_name"])
        if args.get("description"):
            config["description"] = str(args["description"])
        if args.get("personality"):
            config["personality"] = str(args["personality"])
        family_text = ""
        plan_hook = getattr(fam, "plan", None)
        if callable(plan_hook):
            made = plan_hook(config, dict(args))
            if inspect.isawaitable(made):
                made = await made
            family_text = str(made or "")
        build.config = config
        build.data["name"] = name
        planned = {}
        checks_hook = getattr(fam, "checks", None)
        if callable(checks_hook):
            made = checks_hook(copy.deepcopy(build.config),
                               copy.deepcopy(build.device or {}))
            if inspect.isawaitable(made):
                made = await made
            if isinstance(made, dict):
                planned = made
        existing = {c.get("id"): c for c in build.checks
                    if isinstance(c, dict)}
        merged_checks: list = []
        for cid, spec in planned.items():
            spec = spec if isinstance(spec, dict) else {}
            old = existing.get(cid)
            if old is not None and old.get("sig") == spec.get("sig"):
                kept = dict(old)
                kept["title"] = spec.get("title") or kept.get("title")
                kept["steps"] = list(spec.get("steps") or kept.get("steps") or [])
                kept["sig"] = spec.get("sig")
                merged_checks.append(kept)
            else:  # new, or what it covers changed: back to todo
                merged_checks.append({
                    "id": cid, "title": spec.get("title") or cid,
                    "steps": list(spec.get("steps") or []),
                    "state": "todo", "sig": spec.get("sig")})
        build.checks = merged_checks  # unplanned checks are dropped
        self.save(build)
        problems: list = []
        try:
            found = fam.problems(copy.deepcopy(build.config))
            if isinstance(found, (list, tuple)) and found:
                problems = [str(p) for p in (found[0] or [])]
        except Exception:
            problems = []
        order = ", ".join(c["title"] for c in merged_checks)
        if problems:
            reply = "plan saved, but it has problems:\n" + "\n".join(problems)
        else:
            reply = "plan saved."
        if family_text:
            reply += "\n" + family_text
        reply += f"\nchecks to do, in order: {order}" if order else \
            "\nchecks to do, in order: none"
        return reply

    def _find_check(self, build: Build, cid: str) -> dict:
        for entry in build.checks:
            if isinstance(entry, dict) and str(entry.get("id")) == str(cid):
                return entry
        raise ValueError(f"there is no check '{cid}' in this plan")

    async def check(self, build: Build, cid: str) -> str:
        entry = self._find_check(build, cid)
        if not build.workshop or not isinstance(build.device, dict):
            raise ValueError("choose a device first")
        entry["state"] = "testing"
        self.save(build)
        result = await self._act(
            f"@{build.workshop}", "check",
            {"family": build.family_ref, "device": build.device,
             "config": copy.deepcopy(build.config), "check": cid},
            timeout=60)
        if not result.get("ok"):
            entry["state"] = "todo"
            entry["note"] = result.get("text") or "the check failed"
            self.save(build)
            raise ValueError(entry["note"])
        _text, data = self._act_data(result)
        data = data if isinstance(data, dict) else {}
        expect = str(data.get("expect") or "")
        if isinstance(data.get("device"), dict):
            merged = dict(build.device)
            merged.update(copy.deepcopy(data["device"]))
            build.device = merged
            await self.settle(build)
        if data.get("reading") is not None:
            entry["reading"] = copy.deepcopy(data["reading"])
        audio = data.get("audio")
        if audio:
            heard = ""
            try:
                heard_result = await hearing_mod.transcribe(str(audio))
                heard = str((heard_result or {}).get("text") or "").strip()
                reading = entry.get("reading")
                if not isinstance(reading, dict):
                    reading = {}
                reading["heard"] = heard or "(nothing)"
                entry["reading"] = reading
            except Exception as exc:
                reading = entry.get("reading")
                if not isinstance(reading, dict):
                    reading = {}
                reading["heard"] = f"(couldn't transcribe: {exc})"
                entry["reading"] = reading
        image = data.get("image")
        if image:
            raw: bytes
            if isinstance(image, (bytes, bytearray)):
                raw = bytes(image)
                image_b64 = base64.b64encode(raw).decode("ascii")
            else:
                image_b64 = str(image)
                try:
                    raw = base64.b64decode(image_b64)
                except Exception:
                    raw = image_b64.encode("utf-8", "replace")
            folder = self.builds_dir / build.id
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{cid}.jpg").write_bytes(raw)
            entry["image"] = f"/api/builds/{build.id}/check/{cid}.jpg"
            try:
                apilog.register(imaging.for_model(image_b64),
                                f"build {build.id} · {cid}")
            except Exception:
                pass
        entry["state"] = "tested"
        entry["expect"] = expect
        entry["tested_at"] = time.time()
        self.save(build)
        reading = entry.get("reading")
        if reading is not None:
            shown = reading.get("heard") if isinstance(reading, dict) \
                else reading
            if shown is None:
                shown = json.dumps(reading, ensure_ascii=False)
            return (f"expected: {expect}\nreading: {shown}\n"
                    "Judge the reading against what should have happened, "
                    "then mark the check with mark_check to say whether it "
                    "really worked.")
        if entry.get("image"):
            return (f"expected: {expect}\n"
                    "A photo is attached: say what you see in it, say "
                    "whether it matches what should have happened, then "
                    "ask the person to confirm and mark the check.")
        return (f"{expect}\n"
                "Ask whether that's what they saw, then mark the check.")

    async def mark(self, build: Build, cid: str, worked: bool,
                   note: str | None = None) -> str:
        entry = self._find_check(build, cid)
        entry["state"] = "passed" if worked else "failed"
        if note:
            entry["note"] = str(note)
        self.save(build)
        remaining = [str(c.get("title") or c.get("id")) for c in build.checks
                     if isinstance(c, dict) and c.get("state") != "passed"]
        if not remaining:
            return "every check has passed — ready for its soul"
        return ("marked. checks still to do: " + ", ".join(remaining))

    async def give_soul(self, build: Build) -> str:
        stage = self.stage(build)
        if stage != "soul":
            reasons = {
                "done": "it already has its soul",
                "found": "choose a device first",
                "connect": "connect the device first",
                "body": "give it a name and a plan first",
                "check": "every check has to pass first",
            }
            raise ValueError(reasons.get(stage, f"not ready yet ({stage})"))
        await self._ensure_meta(build)
        fam = self.family(build)
        problems: list = []
        if fam is not None:
            try:
                found = fam.problems(copy.deepcopy(build.config))
                if isinstance(found, (list, tuple)) and found:
                    problems = [str(p) for p in (found[0] or [])]
            except Exception as exc:
                problems = [str(exc)]
        if problems:
            raise ValueError("it has problems: " + "; ".join(problems))
        config = copy.deepcopy(build.config)
        name = str(config.get("name") or "")
        if build.family_ref.startswith("draft:"):
            result = await self._act(
                f"@{build.workshop}", "draft_promote",
                {"draft": build.id, "build": build.id}, timeout=60)
            if not result.get("ok"):
                raise ValueError(result.get("text") or "promotion failed")
            text, data = self._act_data(result)
            ref = data if isinstance(data, str) and data.startswith("local:") \
                else (text.strip() if text.strip().startswith("local:") else "")
            if not ref:
                meta = build.data.get("_draft_meta") or {}
                if meta.get("NAME"):
                    ref = f"local:{meta['NAME']}"
            if not ref:
                raise ValueError("the draft was promoted but its new "
                                 "family name is unknown")
            config["family"] = ref
            build.config = config
            self.save(build)
        config.setdefault("name", name)
        result = await self._act(f"@{build.workshop}", "start_robot",
                                 {"config": config}, timeout=30)
        if not result.get("ok"):
            raise ValueError(result.get("text") or "starting it failed")
        build.config = config
        build.robot = name
        fixture = getattr(fam, "FIXTURE", None) if fam is not None else None
        registry = getattr(self.house, "registry", None)
        if registry is not None:
            registry.upsert(
                name, kind=fixture or "robot", family=config.get("family"),
                display_name=config.get("display_name") or name,
                device=config.get("device") or build.device,
                workshop=build.workshop, runner="workshop", build=build.id)
        self.save(build)
        if fixture == "mic":
            set_setting = getattr(self.house, "set_setting", None)
            if callable(set_setting):
                set_setting("mic", name)
            return (f"starting up; {name} is now the house mic — "
                    "the house will listen through it")
        if fixture:
            return (f"starting up; {name} is at work in the house")
        return "starting up; it will join the house and be born in a few seconds"

    # -- draft / device investigation helpers (tools) ------------------------------
    async def find_devices(self, build: Build, fresh: bool = False) -> str:
        workshops = getattr(self.house, "workshops", {}) or {}
        names = sorted(workshops)
        if not names:
            return "no workshop is online"
        lines: list[str] = []
        for workshop in names:
            result = await self._act(f"@{workshop}", "devices",
                                     {"fresh": bool(fresh)})
            if not result.get("ok"):
                lines.append(f"{workshop}: couldn't scan "
                             f"({result.get('text') or 'no answer'})")
                continue
            _text, data = self._act_data(result)
            for device in data if isinstance(data, list) else []:
                if not isinstance(device, dict):
                    continue
                lines.append(
                    f"{workshop}: {device.get('id')} — "
                    f"{device.get('name') or 'unknown device'} "
                    f"({device.get('family') or '?'}) — "
                    f"{device.get('status') or ''}")
        return "\n".join(lines) if lines else "no devices found"

    async def devices_on(self, workshop: str, fresh: bool = False) -> list:
        result = await self._act(f"@{workshop}", "devices",
                                 {"fresh": bool(fresh)})
        if not result.get("ok"):
            raise ValueError(result.get("text") or "the workshop didn't answer")
        _text, data = self._act_data(result)
        out = []
        for device in data if isinstance(data, list) else []:
            if not isinstance(device, dict):
                continue
            shown = copy.deepcopy(device)
            shown["workshop"] = workshop
            shown["family_title"] = self.family_title(None,
                                                      shown.get("family"))
            if device.get("robot"):
                shown["belongs"] = device["robot"]
            out.append(shown)
        return out

    async def inspect_device(self, build: Build) -> str:
        if not build.workshop or not isinstance(build.device, dict):
            raise ValueError("choose a device first")
        result = await self._act(f"@{build.workshop}", "inspect_device",
                                 {"device": build.device})
        if not result.get("ok"):
            raise ValueError(result.get("text") or "inspect failed")
        text, data = self._act_data(result)
        return text if data is None else \
            json.dumps(data, ensure_ascii=False)[:4000]

    async def listen_device(self, build: Build, baud=None,
                            seconds=None) -> str:
        if not build.workshop or not isinstance(build.device, dict):
            raise ValueError("choose a device first")
        args: dict = {"device": build.device}
        if baud is not None:
            args["baud"] = int(baud)
        if seconds is not None:
            args["seconds"] = float(seconds)
        result = await self._act(f"@{build.workshop}", "listen_device", args)
        if not result.get("ok"):
            raise ValueError(result.get("text") or "listening failed")
        text, _data = self._act_data(result)
        return text

    async def list_files(self, build: Build) -> str:
        result = await self._draft_act(build, {"op": "list"})
        text, data = self._act_data(result)
        if isinstance(data, dict):
            if not data:
                return "no draft files yet"
            return "\n".join(f"{name}: {lines} lines"
                             for name, lines in sorted(data.items()))
        return text

    async def read_file(self, build: Build, name: str) -> str:
        result = await self._draft_act(build,
                                       {"op": "read", "name": str(name)})
        text, _data = self._act_data(result)
        return text

    async def write_file(self, build: Build, name: str, content: str) -> str:
        await self._draft_act(build, {"op": "write", "name": str(name),
                                      "content": str(content)})
        if isinstance(build.draft, dict):
            build.draft.pop("approved", None)
            build.draft.pop("approved_hash", None)
        self.save(build)
        return "written"

    async def _draft_act(self, build: Build, args: dict) -> dict:
        if not build.workshop:
            raise ValueError("choose a device first (drafts live on its workshop)")
        result = await self._act(f"@{build.workshop}", "draft_file",
                                 {"draft": build.id, **args})
        if not result.get("ok"):
            raise ValueError(result.get("text") or "the draft action failed")
        return result

    async def review_draft(self, build: Build) -> str:
        if not build.workshop:
            raise ValueError("choose a device first")
        result = await self._act(f"@{build.workshop}", "draft_review",
                                 {"draft": build.id})
        if not result.get("ok"):
            raise ValueError(result.get("text") or "the review failed")
        _text, data = self._act_data(result)
        data = data if isinstance(data, dict) else {}
        if isinstance(data.get("meta"), dict):
            build.data["_draft_meta"] = data["meta"]
        build.draft = {"problems": list(data.get("problems") or []),
                       "reaches": list(data.get("reaches") or []),
                       "hash": data.get("hash"),
                       "imports": data.get("imports") or {}}
        self.save(build)
        lines = []
        problems = data.get("problems") or []
        lines.append("problems: " + ("; ".join(str(p) for p in problems)
                                     if problems else "none"))
        reaches = data.get("reaches") or []
        lines.append("reaches: " + ("\n".join(str(r) for r in reaches)
                                    if reaches else "none"))
        lines.append(f"hash: {data.get('hash')}")
        return "\n".join(lines)

    async def approve_draft(self, build: Build, hash_value: str) -> str:
        if not build.workshop:
            raise ValueError("choose a device first")
        result = await self._act(f"@{build.workshop}", "draft_approve",
                                 {"draft": build.id, "hash": str(hash_value or "")})
        if not result.get("ok"):
            raise ValueError(result.get("text") or "approval failed")
        if not isinstance(build.draft, dict):
            build.draft = {}
        build.draft["approved"] = True
        build.draft["approved_hash"] = str(hash_value or "")
        self.save(build)
        return "approved"

    # -- the conversation ---------------------------------------------------------
    def system_prompt(self, build: Build) -> str:
        guides = ""
        if families_lib is not None:
            try:
                guides = families_lib.guides()
            except Exception:
                guides = ""
        prompt = PROMPT.format(human=getattr(self.house, "human", "your human"),
                               guides=guides, draft_guide=DRAFT_GUIDE)
        try:
            sheet = json.dumps(self.sheet(build), ensure_ascii=False)[:6000]
        except Exception:
            sheet = "{}"
        return prompt + f"\n\nThe build sheet right now:\n{sheet}"

    def tools(self, build: Build) -> list[ToolSpec]:
        def spec(name: str, description: str, props: dict,
                 required: list[str] | None = None) -> ToolSpec:
            return ToolSpec(name, description, {
                "type": "object", "properties": props,
                "required": required or [], "additionalProperties": False})

        plan_props: dict = {
            "name": {"type": "string",
                     "description": "the robot's name: lowercase, ^[a-z][a-z0-9_-]{0,31}$"},
            "display_name": {"type": "string"},
            "description": {"type": "string",
                            "description": "what this robot is (a robot needs one)"},
            "personality": {"type": "string",
                            "description": "the personality wished for it (a robot needs one)"},
        }
        for key, value in _plan_properties().items():
            plan_props.setdefault(key, value)
        return [
            spec("find_devices", "Look for devices on every online workshop.", {}),
            spec("choose_device", "Choose one found device for this build.",
                 {"workshop": {"type": "string"},
                  "device": {"type": "string", "description": "the device id"}},
                 ["workshop", "device"]),
            spec("connect_device",
                 "Run the family's connect step. Only when the family needs "
                 "no form fields; if it needs fields, ask the person to fill "
                 "them in on the build sheet, never in the chat.", {}),
            spec("plan_robot", "Plan the robot: its name and body. A robot "
                 "needs a description and a personality; equipment (a mic or "
                 "speaker) needs a name only.", plan_props, ["name"]),
            spec("run_check", "Run one planned check and show its evidence.",
                 {"check": {"type": "string"}}, ["check"]),
            spec("mark_check", "Mark a check from what the person judged.",
                 {"check": {"type": "string"}, "worked": {"type": "boolean"},
                  "note": {"type": "string"}}, ["check", "worked"]),
            spec("give_soul", "Final step: start the robot (equipment: put "
                 "it to work). Only when every check has passed.", {}),
            spec("inspect_device", "Look closely at the chosen device "
                 "(read-only investigation, for a new family).", {}),
            spec("listen_device", "Listen on the chosen device's serial "
                 "port (read-only investigation).",
                 {"baud": {"type": "integer"}, "seconds": {"type": "number"}}),
            spec("list_files", "List the draft family files.", {}),
            spec("read_file", "Read one draft family file.",
                 {"name": {"type": "string"}}, ["name"]),
            spec("write_file", "Write one draft family file.",
                 {"name": {"type": "string"}, "content": {"type": "string"}},
                 ["name", "content"]),
            spec("review_draft", "Review the draft family: problems, "
                 "reaches and its content hash.", {}),
        ]

    build_tools = tools

    async def _exec_tool(self, build: Build, call) -> ToolResult:
        name, args = call.name, dict(call.args or {})
        try:
            if name == "find_devices":
                return ToolResult(call.id, await self.find_devices(build))
            if name == "choose_device":
                return ToolResult(call.id, await self.choose_device(
                    build, str(args.get("workshop") or ""),
                    str(args.get("device") or "")))
            if name == "connect_device":
                fam = self.family(build)
                connect = getattr(fam, "CONNECT", {}) or {} if fam else {}
                if (connect.get("fields") or []):
                    labels = ", ".join(
                        str(f.get("label") or f.get("id"))
                        for f in connect["fields"] if isinstance(f, dict))
                    return ToolResult(
                        call.id,
                        f"this needs {labels}: ask the person to fill it in "
                        "there, never in the chat", is_error=True)
                return ToolResult(call.id, await self.connect(build, {}))
            if name == "plan_robot":
                return ToolResult(call.id, await self.plan(build, args))
            if name == "run_check":
                text = await self.check(build, str(args.get("check") or ""))
                entry = next((c for c in build.checks
                              if str(c.get("id")) == str(args.get("check"))),
                             {})
                if entry.get("image"):
                    photo = self.builds_dir / build.id / \
                        f"{args.get('check')}.jpg"
                    image_b64 = None
                    if photo.exists():
                        image_b64 = base64.b64encode(
                            photo.read_bytes()).decode("ascii")
                    return ToolResult(
                        call.id,
                        "say what you see in the photo, say whether it "
                        "matches what should have happened, then ask the "
                        "person to confirm and mark the check.\n" + text,
                        image_b64=image_b64)
                return ToolResult(call.id, text)
            if name == "mark_check":
                return ToolResult(call.id, await self.mark(
                    build, str(args.get("check") or ""),
                    bool(args.get("worked")), args.get("note")))
            if name == "give_soul":
                return ToolResult(call.id, await self.give_soul(build))
            if name == "inspect_device":
                return ToolResult(call.id, await self.inspect_device(build))
            if name == "listen_device":
                return ToolResult(call.id, await self.listen_device(
                    build, args.get("baud"), args.get("seconds")))
            if name == "list_files":
                return ToolResult(call.id, await self.list_files(build))
            if name == "read_file":
                return ToolResult(call.id, await self.read_file(
                    build, str(args.get("name") or "")))
            if name == "write_file":
                return ToolResult(call.id, await self.write_file(
                    build, str(args.get("name") or ""),
                    str(args.get("content") or "")))
            if name == "review_draft":
                return ToolResult(call.id, await self.review_draft(build))
            return ToolResult(call.id, f"unknown tool '{name}'", is_error=True)
        except Exception as exc:
            return ToolResult(call.id, f"{type(exc).__name__}: {exc}",
                              is_error=True)

    async def turn(self, build: Build, text: str, role: str = "human",
                   _record: bool = True) -> dict:
        """One builder turn: <=10 model steps, tools one at a time."""
        if build.lock.locked():
            raise RuntimeError("the builder is still busy with the last step")
        async with build.lock:
            build.busy = True
            if _record:
                self._append(build, role, text)
            self.save(build)
            conv = None
            mark = 0
            try:
                conv = self.provider.conversation(
                    self.system_prompt(build), self.tools(build),
                    build.conversation)
                try:
                    conv.label = f"build {build.id}"
                except Exception:
                    pass
                mark = conv.checkpoint()
                conv.add_user(str(text) if role == "human"
                              else f"[{text}]")
                reply = ""
                for _step_no in range(1, MAX_STEPS + 1):
                    step = await conv.step()
                    build.usage["calls"] = int(build.usage.get("calls") or 0) + 1
                    cost = pricing.cost(getattr(self.provider, "model", ""),
                                        step.usage)
                    if cost:
                        build.usage["usd"] = \
                            float(build.usage.get("usd") or 0.0) + cost
                    if step.text and step.tool_calls:
                        self._append(build, "builder", step.text)
                        self.save(build)
                    if not step.tool_calls:
                        reply = step.text or ""
                        break
                    results = []
                    for call in step.tool_calls:  # one at a time, in order
                        results.append(await self._exec_tool(build, call))
                        self.save(build)
                    conv.add_tool_results(results)
                else:
                    reply = ("I got a bit tangled up — tell me where we are "
                             "and I'll pick it up from there.")
                if reply and reply.strip() and reply.strip() != "(silence)":
                    self._append(build, "builder", reply)
                build.conversation = conv.dump()
                build.busy = False
                self.save(build)
                return self.sheet(build)
            except Exception:
                if conv is not None:
                    try:
                        conv.rollback(mark)
                    except Exception:
                        pass
                build.busy = False
                self.save(build)
                raise

    async def press(self, build: Build, what: str, step) -> dict:
        """A build-sheet button: event line, then a builder turn about it."""
        error: Exception | None = None
        result_text = "done"
        try:
            outcome = step() if callable(step) else step
            if inspect.isawaitable(outcome):
                outcome = await outcome
            if isinstance(outcome, str) and outcome:
                result_text = outcome
            elif outcome is not None and not isinstance(outcome, dict):
                result_text = str(outcome)
        except Exception as exc:
            error = exc
            result_text = str(exc)
        if error is not None:
            self._append(build, "event", f"{what} — failed: {error}")
            self.save(build)
            await self.turn(build, f"on the build sheet: {what} → "
                                   f"failed: {error}", role="event",
                            _record=False)
            raise error
        self._append(build, "event", what)
        self.save(build)
        await self.turn(build, f"on the build sheet: {what} → {result_text}",
                        role="event", _record=False)
        return self.sheet(build)


__all__ = ["Build", "Builder", "DRAFT_GUIDE", "PROMPT",
           "family_catalog"]

