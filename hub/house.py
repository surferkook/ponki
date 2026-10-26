"""The House: links, manifests, acts, the feed, photos, remembrance (spec 02).

The House never branches on a device family. It sees ids, records, manifests
and actions. Souls (spec 03) plug into the hooks marked for them.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import re
import shutil
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import apilog, imaging
from .registry import PROTOCOL, Registry
from .soul import Soul

FIXTURES = ("workshop", "speaker", "mic")
ACT_TIMEOUT = 20.0
MAX_ASK_DEPTH = 3
SEEN_KEEP = 200
VOICE_KEEP = 200
EVENTS_KEEP = 1000
LISTENER_BACKLOG = 500
# the room (spec 03)
TURN_WAIT = 60.0
TURN_GAP = 0.4
CHATTER_DEPTH = 2
EXCHANGE_DEPTH = 10
ROOM_KEEP = 12
ASK_WAIT = 8.0
PROMISE_WAIT = 60.0
WATCH_SECONDS = 12
_SOUND_NAME = re.compile(r"beep|chirp|tune|song|melody|sound|buzz|play_music")

FLOOR_PROMPT = (
    "You keep the conversation floor in a house of robots. A robot just said "
    "something out loud. Decide whether ONE robot who heard it would naturally "
    "react out loud, as a real housemate would, or everyone stays quiet. Stay "
    "quiet most of the time: react only to a greeting or goodnight to the room, "
    "a question to the room, a robot hearing its own name, or something that "
    "clearly concerns that robot. Never pile on, never react to small talk "
    "between others, never keep an exchange going just to talk, and never pick "
    "the one who just spoke. After a reply to a person's question, prefer quiet "
    "unless a robot was named. Some robots keep promises or habits: if the line "
    "is clearly the moment for one — it happened, just now: not a plan, an "
    "attempt or a question — put that robot in also, whoever else reacts; never "
    "for its own lines. When open is false, react must be null; only promises "
    "count. Reply with JSON only: "
    '{"react": "<robot id>" | null, "also": ["<robot id>", ...], "why": "<a few words>"}'
)


def camera_action(manifest: dict) -> str | None:
    """The manifest's photo action (returns image), `look` preferred (spec 05)."""
    actions = manifest.get("actions") or {}
    image_actions = [name for name, a in actions.items()
                     if isinstance(a, dict) and a.get("returns") == "image"]
    if "look" in image_actions:
        return "look"
    return image_actions[0] if image_actions else None


def _is_sound(manifest: dict, action: str) -> bool:
    entry = (manifest.get("actions") or {}).get(action) or {}
    if entry.get("sound"):
        return True
    if "sound" in entry:
        return bool(entry.get("sound"))
    return bool(_SOUND_NAME.search(action))


def _atomic_write(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _load_json(path: Path, default):
    try:
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            return value
    except (ValueError, OSError):
        pass
    return default


@dataclass
class Link:
    ws: object
    manifest: dict
    state: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)
    since: float = field(default_factory=time.time)


class House:
    def __init__(self, provider, human: str, data_dir: Path) -> None:
        self.provider = provider
        self.model = getattr(provider, "model", None) or "muse-spark-1.3"
        self.provider_name = getattr(provider, "name", None) or "meta"
        self.human = human or "your human"
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "souls").mkdir(exist_ok=True)
        (self.data_dir / "seen").mkdir(exist_ok=True)
        (self.data_dir / "voice").mkdir(exist_ok=True)
        (self.data_dir / "departed").mkdir(exist_ok=True)
        apilog.configure(self.data_dir)

        self.registry = Registry(self.data_dir / "registry.json")
        self.bodies: dict[str, Link] = {}
        self.fixtures: dict[str, Link] = {}
        self.souls: dict[str, Soul] = {}
        self.manifests: dict[str, dict] = _load_json(self.data_dir / "manifests.json", {})
        self.settings: dict = {"chatter": True, "always_on": False, "mic": None}
        saved_settings = _load_json(self.data_dir / "house.json", {})
        if isinstance(saved_settings, dict):
            self.settings.update(saved_settings)
        self.departed: list[dict] = _load_json(self.data_dir / "departed" / "index.json", [])
        self.listeners: set[asyncio.Queue] = set()
        self.seen_keep = SEEN_KEEP
        self.started = time.time()
        self._act_counter = 0
        self._tasks: set[asyncio.Task] = set()
        self.events: deque = deque(maxlen=EVENTS_KEEP)
        self._load_events()
        self._counters: dict[str, int] = _load_json(self.data_dir / "seen" / "counters.json", {})
        self._voice_next = self._max_on_disk(self.data_dir / "voice", ".wav")
        self.room: deque = deque(maxlen=ROOM_KEEP)
        self._speaking: dict[str, int] = {}
        self._voice_lock = asyncio.Lock()
        self._voice_spans: deque = deque(maxlen=20)
        self._last_addressed: tuple[str, float] | None = None
        self.voice_error: str | None = None

    # -- small helpers -------------------------------------------------------
    def _load_events(self) -> None:
        path = self.data_dir / "events.jsonl"
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()[-EVENTS_KEEP:]
            for line in lines:
                try:
                    self.events.append(json.loads(line))
                except ValueError:
                    continue
            path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in self.events),
                            encoding="utf-8")

    @staticmethod
    def _max_on_disk(folder: Path, suffix: str) -> int:
        best = 0
        if folder.exists():
            for item in folder.iterdir():
                if item.suffix == suffix and item.stem.isdigit():
                    best = max(best, int(item.stem))
        return best

    def _save_manifests(self) -> None:
        _atomic_write(self.data_dir / "manifests.json", self.manifests)

    def _save_settings(self) -> None:
        _atomic_write(self.data_dir / "house.json", self.settings)

    def _save_counters(self) -> None:
        _atomic_write(self.data_dir / "seen" / "counters.json", self._counters)

    def _save_departed(self) -> None:
        _atomic_write(self.data_dir / "departed" / "index.json", self.departed)

    def spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def soul(self, rid: str) -> Soul:
        if rid not in self.souls:
            self.souls[rid] = Soul(self, rid, self.provider)
        return self.souls[rid]

    def manifest(self, rid: str) -> dict:
        link = self.bodies.get(rid)
        if link:
            return link.manifest
        return self.manifests.get(rid, {})

    def online(self, rid: str) -> bool:
        return rid in self.bodies

    def fixtures_of(self, kind: str) -> dict[str, Link]:
        return {k: v for k, v in self.fixtures.items() if v.manifest.get("kind") == kind}

    @property
    def workshops(self) -> dict[str, Link]:
        return self.fixtures_of("workshop")

    def residents(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for rec in self.registry.all(kind="robot"):
            out[rec["id"]] = rec.get("display_name") or rec["id"]
        for rid, man in self.manifests.items():
            out.setdefault(rid, man.get("display_name") or rid)
        return out

    def label(self, key: str) -> str:
        rid = key[1:] if key.startswith("@") else key
        rec = self.registry.get(rid)
        if rec and rec.get("display_name"):
            return rec["display_name"]
        return rid

    def resolve(self, who: str) -> str | None:
        wanted = (who or "").strip().lower()
        if not wanted:
            return None
        for rid, man in self.manifests.items():
            if rid.lower() == wanted or (man.get("display_name") or "").lower() == wanted:
                return rid
        for rid in self.bodies:
            if rid.lower() == wanted:
                return rid
        return self.registry.by_display_name(who.strip())

    # -- feed ------------------------------------------------------------------
    def emit(self, kind: str, robot: str, **data) -> None:
        event = {"t": time.time(), "kind": kind, "robot": robot or "", **data}
        self.events.append(event)
        try:
            with (self.data_dir / "events.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass
        for queue in list(self.listeners):
            if queue.qsize() < LISTENER_BACKLOG:
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass

    # -- attach / detach ---------------------------------------------------------
    @staticmethod
    async def _close(ws, code: int) -> None:
        try:
            result = ws.close(code=code)
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass

    @staticmethod
    def _fail_pending(link: Link, message: str) -> None:
        for fut in list(link.pending.values()):
            if not fut.done():
                fut.set_result({"ok": False, "error": message})
        link.pending.clear()

    async def attach(self, ws, manifest: dict) -> str:
        rid = manifest["name"]
        kind = manifest.get("kind")
        protocol = int(manifest.get("protocol") or 0)
        if kind in FIXTURES:
            old = self.fixtures.get(rid)
            if old is not None:
                self._fail_pending(old, "reconnected")
                await self._close(old.ws, 4000)
            self.fixtures[rid] = Link(ws=ws, manifest=dict(manifest),
                                      state=dict(manifest.get("state") or {}))
            self.registry.adopt(rid, kind=kind, display_name=rid, protocol=protocol)
            self.emit("fixture_online", rid, fixture=kind)
            if kind == "mic":
                self.spawn(self._tell_mics())
            if kind == "workshop":
                self.spawn(self._adopt_from_workshop(rid))
            return f"@{rid}"

        old = self.bodies.get(rid)
        if old is not None:
            self._fail_pending(old, "body reconnected")
            await self._close(old.ws, 4000)
        soul = self.soul(rid)
        display = (soul.display_name or manifest.get("display_name")
                   or rid[:1].upper() + rid[1:])
        self.registry.adopt(rid, kind="robot", display_name=display)
        record = self.registry.upsert(rid, protocol=protocol)
        if protocol > PROTOCOL:
            self.emit("error", rid,
                      text=f"its body speaks protocol {protocol}; this hub knows {PROTOCOL}")
        stored = dict(manifest)
        stored["display_name"] = record.get("display_name") or display  # the record wins
        self.bodies[rid] = Link(ws=ws, manifest=stored,
                                state=dict(manifest.get("state") or {}))
        self.manifests[rid] = stored
        self._save_manifests()
        self.emit("online", rid, description=stored.get("description", ""),
                  actions=sorted((stored.get("actions") or {}).keys()))
        self.spawn(self._birth(rid, stored))
        return rid

    async def _birth(self, rid: str, manifest: dict) -> None:
        try:
            soul = self.soul(rid)
            await soul.ensure_born(manifest)
            if not soul.data.get("portrait"):
                await soul.paint(manifest)
        except Exception as exc:
            self.emit("error", rid, text=f"birth failed: {exc}")

    async def _adopt_from_workshop(self, wid: str) -> None:
        """Spec 09 hook: learn which robots a workshop runs (1 s grace)."""
        try:
            await asyncio.sleep(1)
            result = await self.act(f"@{wid}", "robots", {}, timeout=15)
            entries = result.get("data")
            if not isinstance(entries, list):
                try:
                    entries = json.loads(result.get("text") or "[]")
                except ValueError:
                    entries = []
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict):
                    continue
                rid = entry.get("name") or entry.get("id")
                if not rid:
                    continue
                if entry.get("kind", "robot") == "robot" and rid not in self.manifests \
                        and self.registry.get(rid) is None:
                    continue  # not ours to record
                fields: dict = {"kind": entry.get("kind", "robot"), "family": entry.get("family"),
                                "workshop": wid, "runner": "workshop",
                                "display_name": entry.get("display_name")}
                if entry.get("device"):
                    fields["device"] = {"id": entry["device"]} if isinstance(entry["device"], str) else entry["device"]
                self.registry.adopt(rid, **fields)
        except Exception:
            pass

    # -- hearing (spec 06) ---------------------------------------------------------
    def house_mic(self) -> str | None:
        mics = self.fixtures_of("mic")
        chosen = self.settings.get("mic")
        if chosen in mics:
            return chosen
        return sorted(mics)[0] if mics else None

    async def _tell_mics(self) -> None:
        """Each mic: listen only if always-on and it is the house mic."""
        chosen = self.house_mic()
        for mid in self.fixtures_of("mic"):
            on = bool(self.settings.get("always_on")) and mid == chosen
            try:
                await self.act(f"@{mid}", "listen", {"on": on}, timeout=10)
            except Exception:
                pass

    def hear(self, text: str, to: str | None = None, via: str = "house mic"):
        """Who spoken words are for: named, else page, else last addressed."""
        from . import hearing as hearing_mod

        target = hearing_mod.addressed(text, self.residents())
        if target is None and to and self.resolve(to):
            target = self.resolve(to)
        if target is None and self._last_addressed is not None:
            rid, when = self._last_addressed
            if time.time() - when < 600 and rid in self.residents():
                target = rid
        if target is None:
            self.emit("unheard", "", text=text, via=via)
            return None
        self._last_addressed = (target, time.time())
        self.emit("heard", target, text=text, frm=self.human, via=via)
        self.spawn(self._run_turn(target, text, "human"))
        return target

    async def heard_audio(self, mic: str, audio: str, seconds: float,
                          to: str | None = None, push: bool = False) -> dict:
        """One sentence from a mic: transcribe it, route it, return the result."""
        from . import hearing as hearing_mod

        if not push:
            if not self.settings.get("always_on"):
                return {"text": "", "ignored": True}
            if mic != self.house_mic():
                return {"text": "", "ignored": True}
            now = time.time()
            if self._voice_lock.locked():
                return {"text": "", "ignored": True}
            for started, ended, _rid in list(self._voice_spans):
                if started - 0.5 < now and ended + 1.0 > now - seconds - 0.5:
                    return {"text": "", "ignored": True}  # our own voice, not a person
        try:
            result = await hearing_mod.transcribe(
                audio, keywords=sorted(self.residents().values()))
        except Exception as exc:
            self.emit("error", "", text=str(exc))
            return {"text": "", "error": str(exc)}
        billed = int(result.get("billed_seconds") or 0)
        apilog.write({"t": time.time(), "kind": "transcribe", "who": mic,
                      "engine": result.get("engine"), "seconds": seconds,
                      "usd": billed * 0.18 / 3600 if billed else 0,
                      "text": str(result.get("text", ""))[:200]})
        text = (result.get("text") or "").strip()
        if not text:
            return {"text": ""}
        if not push:
            named = hearing_mod.addressed(text, self.residents())
            if len(text.split()) < 2 and named is None:
                return {"text": "", "dropped": True}  # a stray "thanks" or a cough
        via = "push-to-talk" if push else "house mic"
        target = self.hear(text, to=to, via=via)
        return {**result, "text": text, "to": target}

    # spec 02's spawn of a mic sentence, kept under its old name
    async def _heard_audio(self, mic: str, audio: str, seconds: float) -> None:
        await self.heard_audio(mic, audio, seconds)

    def detach(self, key: str, ws) -> None:
        is_fixture = key.startswith("@")
        rid = key[1:] if is_fixture else key
        store = self.fixtures if is_fixture else self.bodies
        link = store.get(rid)
        if link is None or link.ws is not ws:
            return  # a replaced connection must not remove its successor
        self._fail_pending(link, "disconnected" if is_fixture else "body disconnected")
        del store[rid]
        if is_fixture:
            self.emit("fixture_offline", rid, fixture=link.manifest.get("kind"))
        else:
            self.emit("offline", rid)

    # -- body messages ---------------------------------------------------------
    async def on_body_message(self, key: str, msg: dict) -> None:
        is_fixture = key.startswith("@")
        rid = key[1:] if is_fixture else key
        link = (self.fixtures if is_fixture else self.bodies).get(rid)
        mtype = msg.get("type")
        if mtype == "result":
            if link is not None:
                fut = link.pending.get(str(msg.get("id")))
                if fut is not None and not fut.done():
                    fut.set_result(msg)
            return
        if link is None:
            return
        if is_fixture:
            if mtype == "state" and isinstance(msg.get("state"), dict):
                link.state.update(msg["state"])
            elif mtype == "input" and msg.get("audio") and link.manifest.get("kind") == "mic":
                self.spawn(self._heard_audio(rid, msg["audio"], float(msg.get("seconds") or 0)))
            return
        if mtype == "state" and isinstance(msg.get("state"), dict):
            link.state.update(msg["state"])
            self.emit("state", rid, state=dict(link.state))
        elif mtype == "input":
            text = (msg.get("text") or "").strip()
            if not text:
                return
            if msg.get("from") == "human":
                self.emit("heard", rid, text=text, frm=self.human)
                source = "human"
            else:
                self.emit("noticed", rid, text=text)
                source = "event"
            self.spawn(self._run_turn(rid, text, source))
        elif mtype == "log":
            self.emit("log", rid, text=str(msg.get("text", ""))[:500])

    async def _run_turn(self, rid: str, text: str, source: str, chain: tuple = (),
                        depth: int = 0, promised: bool = False) -> str:
        try:
            return await self.soul(rid).turn(text, source=source, chain=chain,
                                             depth=depth, promised=promised)
        except Exception as exc:
            self.emit("error", rid, text=f"turn failed: {exc}")
            raise

    async def _wait_turn(self, rid: str) -> None:
        """Wait while a housemate is speaking, then a breath (spec 03 §11)."""
        waited = False
        deadline = time.time() + TURN_WAIT
        while time.time() < deadline:
            if any(count > 0 for other, count in self._speaking.items() if other != rid):
                waited = True
                await asyncio.sleep(0.15)
            else:
                break
        if waited:
            await asyncio.sleep(TURN_GAP)

    # -- actions ---------------------------------------------------------------
    async def act(self, key: str, action: str, args: dict | None = None,
                  timeout: float = ACT_TIMEOUT) -> dict:
        args = args or {}
        is_fixture = key.startswith("@")
        rid = key[1:] if is_fixture else key
        link = (self.fixtures if is_fixture else self.bodies).get(rid)
        if link is None:
            text = "that fixture is offline" if is_fixture else "your body is offline"
            return {"ok": False, "text": text, "data": None}
        actions = link.manifest.get("actions") or {}
        if action not in actions:
            return {"ok": False, "text": f"this body has no action '{action}'", "data": None}
        is_voice = action == link.manifest.get("voice")
        is_sound = not is_fixture and not is_voice and _is_sound(link.manifest, action)
        if not is_fixture:
            if not is_voice and action != "stop":
                await self._wait_turn(rid)
            self.emit("act", rid, action=action, args=args)
        if is_sound:  # a sound holds the floor like speech, until it has finished
            await self._voice_lock.acquire()
            self._speaking[rid] = self._speaking.get(rid, 0) + 1
        self._act_counter += 1
        act_id = str(self._act_counter)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        link.pending[act_id] = fut
        result: dict
        try:
            try:
                send = link.ws.send_json({"type": "act", "id": act_id, "action": action, "args": args})
                if inspect.isawaitable(send):
                    await send
            except Exception as exc:
                return {"ok": False, "text": f"couldn't reach the body: {exc}", "data": None}
            try:
                result = await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                shown = f"{timeout:g}"
                result = {"ok": False, "error": f"'{action}' timed out after {shown}s"}
                if not is_fixture:
                    self.emit("act_failed", rid, action=action, text=result["error"])
                return {"ok": False, "text": result["error"], "data": None}
        finally:
            link.pending.pop(act_id, None)
            if is_sound:
                self._speaking[rid] = max(0, self._speaking.get(rid, 1) - 1)
                self._voice_lock.release()

        if not result.get("ok"):
            text = result.get("error") or "failed"
            if not is_fixture:
                self.emit("act_failed", rid, action=action, text=text)
            return {"ok": False, "text": text, "data": None}
        data = result.get("data")
        if isinstance(data, str):
            text = data
        elif data is None:
            text = "done"
        else:
            text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        out: dict = {"ok": True, "text": text, "data": data}
        if result.get("image"):
            out["image"] = result["image"]
            if not is_fixture:
                url = self.store_photo(rid, result["image"])
                out["image_url"] = url
                self.emit("saw", rid, image=url)
        return out

    # -- house tools: locate, watch, draw, look_up (specs 03/05) ----------------------
    def _store_eye(self, rid: str, image_b64: str) -> str:
        return self.store_photo(rid, image_b64)

    @staticmethod
    def _photo_label(rid: str, url: str | None) -> str:
        if url:
            tail = url.rsplit("/", 1)[-1]
            if tail.split(".")[0].isdigit():
                return f"{rid} #{tail.split('.')[0]}"
        return f"{rid} photo"

    async def locate(self, rid: str, what: str) -> dict:
        """Fresh photo → SAM → numbered overlay + words (spec 05)."""
        from . import grounding

        cam = grounding.camera_action(self.manifest(rid))
        if not cam:
            return {"ok": False, "text": "your body has no camera"}
        concepts = [c.strip() for c in (what or "").split(",") if c.strip()][:4]
        if not concepts:
            return {"ok": False, "text": "say what to look for"}
        self.emit("locating", rid, what=what)
        shot = await self.act(rid, cam, {})
        if not shot.get("ok") or not shot.get("image"):
            return {"ok": False, "text": shot.get("text", "couldn't take a photo")}
        label = self._photo_label(rid, shot.get("image_url"))
        started = time.time()
        try:
            res = await grounding.segment(shot["image"], concepts)
        except Exception as exc:
            apilog.sam_call(rid, "locate", what, photo=label, error=str(exc))
            self.emit("act_failed", rid, action="locate",
                      text=f"couldn't search the photo: {exc}")
            return {"ok": False, "text": f"couldn't search the photo: {exc}"}
        found = res.get("found") or {}
        total = sum(len(v) for v in found.values())
        ms = res.get("ms", int((time.time() - started) * 1000))
        apilog.sam_call(rid, "locate", what, photo=label,
                        found=f"{total} found")
        text = grounding.describe(found, ms, concepts)
        url = self._store_eye(rid, res.get("overlay", ""))
        self.emit("located", rid, what=what, image=url, found=found,
                  ms=ms, text=text)
        return {"ok": True, "text": text, "image": res.get("overlay"),
                "usd": 0.0025 * len(concepts)}

    async def watch(self, rid: str, what: str, seconds=None) -> dict:
        """Record a clip, track what is named, strip + GIF/video (spec 05)."""
        from . import grounding

        man = self.manifest(rid)
        if "record" not in (man.get("actions") or {}):
            return {"ok": False, "text": "your camera can't record clips"}
        if not (what or "").strip():
            return {"ok": False, "text": "say what to watch"}
        concepts = [c.strip() for c in what.split(",") if c.strip()][:3] or [what]
        try:
            secs = int(seconds) if seconds is not None else WATCH_SECONDS
        except (TypeError, ValueError):
            secs = WATCH_SECONDS
        secs = max(3, min(20, secs))
        # Start the camera only once the watcher has finished speaking: sleep
        # briefly, then wait while its own line is being made or said (<=30 s).
        await asyncio.sleep(0.3)
        deadline = time.time() + 30
        while self._speaking.get(rid, 0) > 0 and time.time() < deadline:
            await asyncio.sleep(0.15)
        self.emit("watching", rid, what=what, seconds=secs)
        result = await self.act(rid, "record", {"seconds": secs},
                                timeout=secs + 60)
        if not result.get("ok"):
            return result
        data = result.get("data")
        if not isinstance(data, dict) or not data.get("video"):
            result = dict(result)  # a body that returns no clip: pass it through
            result.setdefault("what", what)
            return result
        try:
            res = await grounding.track(data["video"], concepts,
                                        times=data.get("times"))
        except Exception as exc:
            apilog.sam_call(rid, "watch", what,
                            photo=f"{rid} clip ({secs} s, not kept)",
                            error=str(exc))
            self.emit("act_failed", rid, action="watch",
                      text=f"couldn't track it: {exc}")
            return {"ok": False, "text": f"couldn't track it: {exc}"}
        objects = res.get("objects") or []
        moved = sum(1 for o in objects if o.get("moved"))
        apilog.sam_call(rid, "watch", what,
                        photo=f"{rid} clip ({secs} s, not kept)",
                        found=f"{len(objects)} tracked, {moved} moved")
        text = grounding.describe_track(what, res)
        url = self._store_eye(rid, res.get("strip", ""))
        self.emit("watched", rid, what=what, image=url, text=text,
                  ms=res.get("ms", 0), moved=bool(moved))
        if res.get("job"):
            self.spawn(self._watch_media(rid, what, res["job"], url))
        frames = int(res.get("frames") or data.get("frames") or 0)
        return {"ok": True, "text": text, "image": res.get("strip"),
                "usd": frames * len(concepts) * 0.0002}

    async def _watch_media(self, rid: str, what: str, job: str,
                           strip_url: str) -> None:
        from . import grounding

        try:
            res = await grounding.media(job)
        except Exception:
            return  # the strip already told the story; media is a bonus
        tail = strip_url.rsplit("/", 1)[-1]
        if not tail.split(".")[0].isdigit():
            return
        n = tail.split(".")[0]
        folder = self.data_dir / "seen" / rid
        folder.mkdir(parents=True, exist_ok=True)
        out: dict = {"what": what, "ms": res.get("ms", 0)}
        if res.get("gif"):
            (folder / f"{n}.gif").write_bytes(base64.b64decode(res["gif"]))
            out["gif"] = f"/api/robots/{rid}/seen/{n}.gif"
        if res.get("video"):
            (folder / f"{n}.mp4").write_bytes(base64.b64decode(res["video"]))
            out["video"] = f"/api/robots/{rid}/seen/{n}.mp4"
        if len(out) > 2:
            self.emit("watch_media", rid, **out)

    async def draw(self, rid: str, picture: str, for_: str = "") -> dict:
        from . import portrait as portrait_mod

        picture = (picture or "").strip()
        if not picture:
            return {"ok": False, "text": "say what to draw"}
        who = for_ or ""
        self.emit("drawing", rid, what=picture[:200], to=who or self.human,
                  **{"for": who} if who else {})
        started = time.time()
        try:
            image = await portrait_mod.paint(rid, picture, side=1024, fmt="JPEG",
                                             label="drawing")
            image_b64 = image if isinstance(image, str) else \
                base64.b64encode(image).decode("ascii")
            if isinstance(image, str):  # a stub may hand back raw bytes as text
                try:
                    base64.b64decode(image, validate=True)
                except Exception:
                    image_b64 = base64.b64encode(image.encode()).decode("ascii")
        except Exception as exc:
            self.emit("act_failed", rid, action="draw", text=f"couldn't draw it: {exc}")
            return {"ok": False, "text": f"couldn't draw it: {exc}"}
        url = self._store_eye(rid, image_b64)
        ms = int((time.time() - started) * 1000)
        self.emit("drew", rid, what=picture[:200], to=who or self.human,
                  image=url, ms=ms, **{"for": who} if who else {})
        text = f"Here's what you drew{' for ' + who if who else ''}: it's on the " \
               f"house's app for everyone to see (attached)."
        return {"ok": True, "text": text, "image": image_b64, "usd": 0.01}

    async def look_up(self, rid: str, question: str) -> dict:
        from . import lookup

        question = (question or "").strip()
        if not question:
            return {"ok": False, "text": "say what to look up"}
        self.emit("looking_up", rid, what=question)
        try:
            result = await lookup.ask(question, who=rid)
        except Exception as exc:
            self.emit("act_failed", rid, action="look_up", text=f"couldn't look it up: {exc}")
            return {"ok": False, "text": f"couldn't look it up: {exc}"}
        titles = "; ".join(s.get("title", "") for s in result.get("sources", []))
        self.emit("looked_up", rid, what=question, text=result.get("answer", ""),
                  sources=result.get("sources", []), ms=result.get("ms", 0))
        return {"ok": True, "usd": result.get("usd", 0.0),
                "text": f"{result.get('answer', '')} (sources: {titles}; "
                        f"{self.human} can open them in the app)"}

    # -- photos ------------------------------------------------------------------
    def _photo_numbers(self, rid: str) -> list[int]:
        folder = self.data_dir / "seen" / rid
        nums: list[int] = []
        if folder.exists():
            for item in folder.iterdir():
                if item.suffix == ".jpg" and item.stem.isdigit():
                    nums.append(int(item.stem))
        return sorted(nums)

    def _carried_fingerprints(self) -> set[str]:
        fps: set[str] = set()
        seen_ids: set[str] = set()
        for rid, soul in self.souls.items():
            seen_ids.add(rid)
            fps.update(apilog.images_in(soul.data.get("conversation")))
        souls_dir = self.data_dir / "souls"
        if souls_dir.exists():
            for path in souls_dir.glob("*.json"):
                if path.stem in seen_ids:
                    continue
                data = _load_json(path, {})
                if isinstance(data, dict):
                    fps.update(apilog.images_in(data.get("conversation")))
        return fps

    def store_photo(self, rid: str, image_b64: str) -> str:
        nums = self._photo_numbers(rid)
        n = max([self._counters.get(rid, 0)] + nums) + 1
        self._counters[rid] = n
        self._save_counters()
        folder = self.data_dir / "seen" / rid
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{n}.jpg").write_bytes(base64.b64decode(image_b64))
        apilog.register(imaging.for_model(image_b64), f"{rid} #{n}")
        self._prune_seen(rid)
        return f"/api/robots/{rid}/seen/{n}.jpg"

    def _prune_seen(self, rid: str) -> None:
        nums = self._photo_numbers(rid)
        excess = nums[: max(0, len(nums) - self.seen_keep)]
        if not excess:
            return
        carried = self._carried_fingerprints()
        labels = apilog.label_index()
        folder = self.data_dir / "seen" / rid
        for n in excess:
            fp = labels.get(f"{rid} #{n}")
            if fp is None:
                try:
                    raw = (folder / f"{n}.jpg").read_bytes()
                    fp = apilog.fingerprint(imaging.for_model(base64.b64encode(raw).decode()) or "")
                except OSError:
                    fp = ""
            if fp and fp in carried:
                continue  # carried photos are never rotated away
            for suffix in (".jpg", ".gif", ".mp4"):
                try:
                    (folder / f"{n}{suffix}").unlink()
                except OSError:
                    pass
            if fp:
                apilog.unregister(fp)

    def delete_photo(self, rid: str, n: int) -> dict:
        folder = self.data_dir / "seen" / rid
        jpg = folder / f"{n}.jpg"
        if not jpg.exists():
            raise KeyError(f"{rid} has no photo #{n}")
        labels = apilog.label_index()
        fp = labels.get(f"{rid} #{n}")
        if fp is None:
            raw = jpg.read_bytes()
            model = imaging.for_model(base64.b64encode(raw).decode("ascii"))
            fp = apilog.fingerprint(model or "")
        note = f"[photo {rid} #{n} was deleted by {self.human}]"
        conversations = 0
        loaded = set(self.souls.keys())
        for soul in list(self.souls.values()):
            conversations += soul.forget_image(fp, note)
        souls_dir = self.data_dir / "souls"
        if souls_dir.exists():
            for path in souls_dir.glob("*.json"):
                if path.stem in loaded:
                    continue
                data = _load_json(path, {})
                if not isinstance(data, dict):
                    continue
                count = apilog.forget_image(data.get("conversation"), fp, note)
                if count:
                    conversations += count
                    _atomic_write(path, data)
        files: list[str] = []
        for suffix in (".jpg", ".gif", ".mp4"):
            target = folder / f"{n}{suffix}"
            if target.exists():
                target.unlink()
                files.append(target.name)
        if fp:
            apilog.unregister(fp)
        prefix = f"/api/robots/{rid}/seen/{n}."
        kept: deque = deque(maxlen=EVENTS_KEEP)
        for event in self.events:
            event = dict(event)
            for field_name in ("image", "gif", "video"):
                value = event.get(field_name)
                if isinstance(value, str) and value.startswith(prefix):
                    event[field_name] = None
                    event["photo_deleted"] = True
            kept.append(event)
        self.events = kept
        (self.data_dir / "events.jsonl").write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in self.events), encoding="utf-8")
        apilog.write({"t": time.time(), "kind": "photo_deleted",
                      "photo": f"{rid} #{n}", "files": files, "conversations": conversations})
        self.emit("photo_deleted", rid, n=n, prefix=prefix)
        return {"files": files, "conversations": conversations}

    def forget_unkept_photos(self, rid: str) -> int:
        soul = self.soul(rid)
        fps = apilog.images_in(soul.data.get("conversation"))
        by_fp = {v: k for k, v in apilog.label_index().items()}
        removed = 0
        note = f"[an older photo, no longer kept, was removed by {self.human}]"
        for fp in set(fps):
            label = by_fp.get(fp, "")
            match = re.fullmatch(rf"{re.escape(rid)} #(\d+)", label or "")
            if not match:
                continue
            if (self.data_dir / "seen" / rid / f"{match.group(1)}.jpg").exists():
                continue
            removed += soul.forget_image(fp, note)
        if removed:
            apilog.write({"t": time.time(), "kind": "photos_forgotten",
                          "robot": rid, "count": removed})
        return removed

    # -- voice clips (kept: the last 30) ------------------------------------------
    CLIP_KEEP = 30

    def save_clip(self, wav: bytes) -> tuple[int, str]:
        self._voice_next += 1
        n = self._voice_next
        folder = self.data_dir / "voice"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{n}.wav").write_bytes(wav)
        try:
            (folder / f"{n - self.CLIP_KEEP}.wav").unlink()
        except OSError:
            pass
        return n, f"/api/voice/{n}.wav"

    # -- rename --------------------------------------------------------------------
    async def rename(self, rid: str, new: str) -> str:
        new = " ".join((new or "").split())[:40]
        if not new:
            raise ValueError("the new name is empty")
        other = self.resolve(new)
        if other is not None and other != rid:
            raise ValueError(f"{new} is already the name of another robot here")
        record = self.registry.get(rid)
        old = (record or {}).get("display_name") or rid[:1].upper() + rid[1:]
        self.registry.upsert(rid, display_name=new)
        man = self.manifests.get(rid)
        if man is not None:
            if man.get("description") and old:
                man["description"] = re.sub(rf"\b{re.escape(old)}\b", new, man["description"])
            man["display_name"] = new
            self._save_manifests()
        link = self.bodies.get(rid)
        if link is not None:
            link.manifest["display_name"] = new
            if man is not None:
                link.manifest["description"] = man.get("description", link.manifest.get("description"))
        soul = self.soul(rid)
        soul.set_display_name(new)
        if soul.persona and old:
            await soul.set_persona(re.sub(rf"\b{re.escape(old)}\b", new, soul.persona))
            soul.set_display_name(new)
        workshop = (record or {}).get("workshop")
        if workshop and workshop in self.workshops:
            description = (man or {}).get("description", "")
            await self.act(f"@{workshop}", "configure_robot",
                           {"name": rid, "display_name": new, "description": description}, timeout=20)
        for other_id in self.residents():
            if other_id != rid:
                self.soul(other_id).add_news(
                    f"{old} has a new name: {new}. Same robot, same housemate; "
                    f"call them {new} from now on.")
        self.emit("renamed", rid, old=old, display_name=new)
        return new

    # -- remembrance: moving on, coming back, forgetting --------------------------
    def remove(self, rid: str, forget: bool = False, workshop: str | None = None) -> dict | None:
        if rid in self.bodies:
            raise ValueError(f"{rid} is home — switch its body off first")
        man = self.manifests.pop(rid, None)
        if man is not None:
            self._save_manifests()
        soul = self.souls.pop(rid, None)
        soul_data: dict = {}
        soul_path = self.data_dir / "souls" / f"{rid}.json"
        if soul is not None:
            soul.save()
            soul_data = soul.data
        elif soul_path.exists():
            loaded = _load_json(soul_path, {})
            soul_data = loaded if isinstance(loaded, dict) else {}
        record = self.registry.pop(rid)
        who = (man or {}).get("display_name") or rid[:1].upper() + rid[1:]

        if forget:
            if soul_path.exists():
                soul_path.unlink()
            seen_dir = self.data_dir / "seen" / rid
            if seen_dir.exists():
                for jpg in seen_dir.glob("*.jpg"):
                    try:
                        raw = jpg.read_bytes()
                        model = imaging.for_model(base64.b64encode(raw).decode("ascii"))
                        if model:
                            apilog.unregister(apilog.fingerprint(model))
                    except OSError:
                        pass
                shutil.rmtree(seen_dir, ignore_errors=True)
            if rid in self._counters:
                del self._counters[rid]
                self._save_counters()
            self.emit("removed", rid)
            for other_id in self.residents():
                self.soul(other_id).add_news(
                    f"{who} has moved out of the house for good. Don't count on them anymore.")
            return None

        now = time.time()
        entry_id = f"{rid}-{int(now)}"
        entry = {
            "id": entry_id, "name": rid, "display_name": who,
            "description": (man or {}).get("description", ""),
            "born": soul_data.get("born"), "departed": now,
            "color": soul_data.get("color", ""), "tagline": soul_data.get("tagline", ""),
            "portrait": bool(soul_data.get("portrait")),
            "workshop": workshop or (record or {}).get("workshop"),
            "record": record,
        }
        archive = self.data_dir / "departed" / entry_id
        archive.mkdir(parents=True, exist_ok=True)
        _atomic_write(archive / "manifest.json", man or {})
        if soul_path.exists():
            shutil.move(str(soul_path), str(archive / "soul.json"))
        else:
            _atomic_write(archive / "soul.json", soul_data)
        seen_dir = self.data_dir / "seen" / rid
        if seen_dir.exists():
            shutil.move(str(seen_dir), str(archive / "seen"))
        if rid in self._counters:
            del self._counters[rid]
            self._save_counters()
        self.departed.append(entry)
        self._save_departed()
        self.emit("moved_on", rid, display_name=who, id=entry_id)
        for other_id in self.residents():
            self.soul(other_id).add_news(
                f"{who} has moved on. Their body has been retired, so {who} won't answer "
                f"anymore. But {who} was part of this house, and what you remember of "
                f"{who} stays with you. If {who} comes up, speak of them warmly, the way "
                f"you'd remember an old friend who will always be with us.")
        return entry

    def _retract_moved_on(self, who: str) -> None:
        prefix = f"{who} has moved on."
        for other_id in self.residents():
            soul = self.soul(other_id)
            kept = [line for line in soul.data.get('news', []) if not str(line).startswith(prefix)]
            if len(kept) != len(soul.data.get('news', [])):
                soul.data["news"] = kept
                soul.save()

    def bring_back(self, entry_id: str) -> dict:
        entry = next((e for e in self.departed if e.get("id") == entry_id), None)
        if entry is None:
            raise ValueError("nobody by that id moved on")
        rid = entry["name"]
        if rid in self.manifests or (self.data_dir / "souls" / f"{rid}.json").exists():
            raise ValueError(f"a robot called {rid} lives here now")
        who = entry.get("display_name") or rid
        archive = self.data_dir / "departed" / entry_id
        soul_archive = archive / "soul.json"
        if soul_archive.exists():
            (self.data_dir / "souls").mkdir(exist_ok=True)
            shutil.move(str(soul_archive), str(self.data_dir / "souls" / f"{rid}.json"))
        seen_archive = archive / "seen"
        seen_target = self.data_dir / "seen" / rid
        if seen_archive.exists() and not seen_target.exists():
            shutil.move(str(seen_archive), str(seen_target))
        nums = self._photo_numbers(rid)
        if nums:
            self._counters[rid] = max(nums)
            self._save_counters()
        manifest = _load_json(archive / "manifest.json", {})
        if isinstance(manifest, dict) and manifest:
            self.manifests[rid] = manifest
            self._save_manifests()
        if entry.get("record"):
            self.registry.restore(entry["record"])
        self.departed = [e for e in self.departed if e.get("id") != entry_id]
        self._save_departed()
        shutil.rmtree(archive, ignore_errors=True)
        self.souls.pop(rid, None)
        for other_id in self.residents():
            if other_id == rid:
                continue
            soul = self.soul(other_id)
            had_unheard = any(str(line).startswith(f"{who} has moved on.") for line in soul.data.get("news", []))
            if had_unheard:
                soul.data["news"] = [line for line in soul.data.get("news", [])
                                     if not str(line).startswith(f"{who} has moved on.")]
                soul.save()
            else:
                soul.add_news(f"{who} is back! {who} had moved on, but has come home again, "
                              f"with all of {who}'s memories. Welcome them back.")
        self.emit("came_back", rid, display_name=who)
        return dict(entry)

    def erase_departed(self, entry_id: str) -> dict:
        entry = next((e for e in self.departed if e.get("id") == entry_id), None)
        if entry is None:
            raise ValueError("nobody by that id moved on")
        who = entry.get("display_name") or entry["name"]
        shutil.rmtree(self.data_dir / "departed" / entry_id, ignore_errors=True)
        self.departed = [e for e in self.departed if e.get("id") != entry_id]
        self._save_departed()
        self._retract_moved_on(who)
        self.emit("removed", entry["name"])
        return dict(entry)

    def remembered(self, rid: str) -> dict | None:
        if rid in self.manifests or (self.data_dir / "souls" / f"{rid}.json").exists():
            return None
        matches = [e for e in self.departed if e.get("name") == rid]
        return dict(matches[-1]) if matches else None

    def remembrance(self, entry_id: str) -> tuple[dict, dict]:
        entry = next((e for e in self.departed if e.get("id") == entry_id), None)
        if entry is None:
            raise ValueError("nobody by that id moved on")
        soul_data = _load_json(self.data_dir / "departed" / entry_id / "soul.json", {})
        return dict(entry), soul_data if isinstance(soul_data, dict) else {}

    # -- the room: said, the floor, speaking (spec 03) --------------------------------
    def _names_housemate(self, text: str, listener_ids: list[str]) -> bool:
        for rid in listener_ids:
            for name in {rid, self.label(rid)}:
                if name and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text or "", re.I):
                    return True
        return False

    def said(self, rid: str, text: str, to: str, depth: int, private: bool) -> None:
        self.room.append({"t": time.time(), "who": rid, "to": to, "text": text})
        listeners = [other for other in self.bodies if other != rid]
        for other in listeners:
            self.soul(other).hear(rid, text[:300])
        named = self._names_housemate(text, listeners)
        limit = EXCHANGE_DEPTH if named else CHATTER_DEPTH
        if not self.settings.get("chatter", True) or not listeners or depth >= EXCHANGE_DEPTH:
            return
        open_ = depth < limit and not private
        has_promises = any(self.soul(other).promises for other in listeners)
        if open_ or has_promises:
            self.spawn(self._run_floor(rid, text, listeners, open_, depth, to))

    async def _run_floor(self, speaker: str, text: str, listeners: list[str],
                         open_: bool, depth: int, to: str) -> None:
        said_to = "a person" if to == "human" else \
            ("nobody in particular" if to == "event" else to)
        present = []
        for other in sorted(listeners):
            soul = self.soul(other)
            entry = {"name": other,
                     "who": (soul.persona.split(". ")[0] + ".")[:200] if soul.persona else "",
                     "body": (self.manifest(other).get("description") or "")[:200]}
            if soul.promises:
                entry["promises"] = [
                    ("[habit] " if p.get("habit") else "") + p["text"] for p in soul.promises]
            present.append(entry)
        recent = [f"{line['who']}: {line['text']}" for line in list(self.room)[:-1][-5:]]
        request = {"said_by": speaker, "said": text, "open": open_, "said_to": said_to,
                   "present": present, "recent": recent}
        why, pick, also = "", None, []
        try:
            raw = await self.provider.complete(FLOOR_PROMPT,
                                               json.dumps(request, ensure_ascii=False),
                                               max_tokens=2000, who="floor")
            decision = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
            why = str(decision.get("why") or "")[:160]
            if open_:
                pick = decision.get("react")
            also = [r for r in (decision.get("also") or [])
                    if r in listeners and r != pick and self.soul(r).promises]
        except Exception as exc:
            why = f"no decision ({type(exc).__name__})"
            pick, also = None, []
        if pick is not None and pick not in listeners:
            pick = None
        if not open_ and not also and pick is None:
            return
        if pick is None and not also:
            return
        self.emit("floor", speaker, react=pick, also=also, why=why)
        for other in also:
            self.emit("overheard", other, frm=speaker, promise=True)
            self.spawn(self._floor_turn(other, speaker, text, depth, promised=True))
        if pick is not None:
            self.emit("overheard", pick, frm=speaker)
            await self._floor_turn(pick, speaker, text, depth, promised=False)

    async def _floor_turn(self, rid: str, speaker: str, text: str, depth: int,
                          promised: bool) -> None:
        try:
            await self.soul(rid).turn(text, source=f"heard:{speaker}",
                                      depth=depth + 1, promised=promised)
        except Exception as exc:
            label = "couldn't keep a promise" if promised else "couldn't react"
            self.emit("error", rid, text=f"{label}: {exc}")

    def _lead_in(self) -> float:
        lead = 0.0
        for link in self.fixtures_of("speaker").values():
            try:
                lead = max(lead, float(link.state.get("lead_in") or 0))
            except (TypeError, ValueError):
                pass
        return lead

    async def _make_clip(self, rid: str, text: str):
        """Synthesise this robot's line in its own voice; keep the clip."""
        from . import voice as voice_mod

        v = voice_mod.clean(self.soul(rid).data.get("voice")) \
            or voice_mod.default_for(rid)
        try:
            wav, ms = await voice_mod.synth(text, v)
        except Exception as exc:
            self.voice_error = str(exc)
            raise
        self.voice_error = None
        n, url = self.save_clip(wav)
        return n, url, ms

    async def _play_clip(self, rid: str, url: str, ms: int, text: str) -> None:
        self.emit("voice", rid, url=url, ms=ms, text=text)
        for sid in self.fixtures_of("speaker"):
            self.spawn(self.act(f"@{sid}", "play", {"url": url}, timeout=60))
        await asyncio.sleep(ms / 1000 + self._lead_in() + 0.4)  # hold the floor

    async def speak(self, rid: str, text: str) -> None:
        self._speaking[rid] = self._speaking.get(rid, 0) + 1
        try:
            man = self.manifest(rid)
            has_voice = bool(man.get("voice"))
            own = man.get("voice") if rid in self.bodies else None
            chirps = man.get("voice_style") == "chirps"
            clip_task = None
            # The clip is made before the lock, on purpose: synthesis runs
            # while the previous speaker is still talking.
            if not has_voice or chirps:
                clip_task = asyncio.ensure_future(self._make_clip(rid, text))
            # a robot with its own speaker, offline, says nothing: the house
            # voice doesn't stand in for a speaker that exists
            started = time.time()
            async with self._voice_lock:
                try:
                    if own and chirps:
                        words = max(1, len(text.split()))
                        await self.act(rid, own,
                                       {"text": text,
                                        "seconds": min(3.0, 0.4 + 0.1 * words),
                                        "early": self._lead_in()}, timeout=60)
                    elif own:
                        await self.act(rid, own, {"text": text}, timeout=60)
                except Exception:
                    pass  # a voice error leaves the robot silent; the turn succeeds
                clip = None
                if clip_task is not None:
                    try:
                        clip = await clip_task
                    except Exception:
                        clip = None
                if clip:
                    _n, url, ms = clip
                    await self._play_clip(rid, url, ms, text)
                self._voice_spans.append((started, time.time(), rid))
        finally:
            self._speaking[rid] = max(0, self._speaking.get(rid, 1) - 1)

    def view_for(self, rid: str) -> str:
        now = datetime.now()
        hour = now.hour
        part = ("night" if hour < 6 else "morning" if hour < 12 else
                "afternoon" if hour < 18 else "evening" if hour < 23 else "night")
        lines = ["House update.",
                 f"It's {now.strftime('%A %d %B')}, around {hour:02d}:00 ({part})."]
        link = self.bodies.get(rid)
        if link is None:
            lines.append("Your body is offline: you can think and talk to housemates, "
                         "but physical actions fail until it reconnects.")
        else:
            state = json.dumps(link.state, ensure_ascii=False, sort_keys=True)
            lines.append(f"Your body is connected and working. State: {state}")
        lines.append("Housemates:")
        others = [(other, self.label(other)) for other in sorted(self.residents())
                  if other != rid]
        if not others:
            lines.append("nobody else yet")
        for other, display in others:
            man = self.manifest(other)
            actions = sorted(name for name, a in (man.get("actions") or {}).items()
                             if not (a or {}).get("hidden"))
            home = "home" if other in self.bodies else "body offline"
            lines.append(f"- {display} ({home}): {man.get('description', '')} "
                         f"Can: {', '.join(actions)}.")
        gone: dict[str, dict] = {}
        for entry in self.departed:
            gone[entry["name"]] = entry
        if gone:
            lines.append("Moved on (lived here once; remembered, but they can't "
                         "answer anymore):")
            for name, entry in sorted(gone.items()):
                if name == rid:
                    continue
                when = datetime.fromtimestamp(float(entry.get("departed") or 0)).strftime("%d %B %Y")
                lines.append(f"- {entry.get('display_name') or name}, moved on {when}: "
                             f"{entry.get('description', '')}")
        return "\n".join(lines)

    # -- settings --------------------------------------------------------------------
    def set_setting(self, key: str, value) -> None:
        old_mic, old_always = self.settings.get("mic"), self.settings.get("always_on")
        self.settings[key] = value
        self._save_settings()
        self.emit("settings", "", settings=dict(self.settings))
        if key in ("always_on", "mic") and (self.settings.get("mic") != old_mic
                                            or self.settings.get("always_on") != old_always):
            self.spawn(self._tell_mics())

    # -- robot-to-robot asks (spec 03 §13) --------------------------------------------
    async def ask(self, frm: str, to: str, message: str, chain: tuple = ()) -> str:
        if (to or "").strip() == frm:
            raise LookupError("that's you!")
        resolved = self.resolve(to or "")
        if resolved == frm:
            raise LookupError("that's you!")
        entry = None
        wanted = (to or "").strip().lower()
        for candidate in self.departed:
            if candidate.get("name", "").lower() == wanted or \
                    (candidate.get("display_name") or "").lower() == wanted:
                entry = candidate
        if resolved is None and entry is not None and self.remembered(entry["name"]):
            display = entry.get("display_name") or entry["name"]
            raise LookupError(f"{display} has moved on and can't answer anymore; "
                              f"they live on in what the house remembers.")
        if resolved is None:
            mates = sorted(r for r in self.residents() if r != frm)
            raise LookupError(f"there's no robot called '{to}' here. "
                              f"Housemates: {', '.join(mates)}.")
        if resolved in chain:
            raise LookupError(f"{resolved} is already waiting on you in this conversation — "
                              f"answer them instead.")
        if len(chain) >= MAX_ASK_DEPTH:
            raise LookupError("that's a long chain of robots asking robots — "
                              "handle it yourself for now.")
        self.emit("ask", frm, to=resolved, text=message)
        self.spawn(self.speak(frm, message))  # asked out loud
        self.room.append({"t": time.time(), "who": frm, "to": resolved, "text": message})
        for other in self.bodies:
            if other not in (frm, resolved):
                self.soul(other).hear(frm, message[:300])
        await asyncio.sleep(0)  # let the question take the voice lock first
        return await self.soul(resolved).turn(message, source=frm, chain=chain)

    def rename_equipment(self, key: str, new: str) -> str:
        rid = key[1:] if key.startswith("@") else key
        new = " ".join((new or "").split())[:40]
        if not new:
            raise ValueError("the new name is empty")
        record = self.registry.get(rid)
        if record is None or record.get("kind") == "robot":
            raise ValueError(f"no workshop or equipment called {key}")
        clash = self.registry.by_display_name(new)
        if clash is not None and clash != rid:
            raise ValueError(f"{new} is already the name of something else here")
        self.registry.upsert(rid, display_name=new)
        self.emit("equipment_renamed", rid, display_name=new)
        return new

