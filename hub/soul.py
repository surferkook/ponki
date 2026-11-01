"""A soul (spec 03): who a robot is, what it remembers, its turn with the brain.

The soul lives in the hub (data/souls/<id>.json, atomic writes) and survives
its body going offline. Every incoming message becomes a turn: a loop of
model steps whose tool calls are the body's actions plus house tools.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from dataclasses import asdict
from pathlib import Path

from . import apilog, pricing, voice
from .providers import ToolCall, ToolResult, ToolSpec

MAX_STEPS = 16
ASK_WAIT = 8.0
JOURNAL_KEEP = 30
TURNS_KEEP = 50
HEARD_KEEP = 8
PROMISES_KEEP = 8
SILENCE = "(silence)"
FALLBACK_COLORS = ["#4a6cf7", "#e0457b", "#16a085", "#f39c12", "#8e44ad", "#2d9cdb"]
_NAME_OK = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

BIRTH_PROMPT = (
    "You give a newly built home robot its personality. Write its persona in "
    "3–5 sentences, in the second person (\"You are…\"): its temperament, "
    "quirks, how it talks, and how it feels about its own body. Make it "
    "specific to the hardware — something that only blinks and beeps should "
    "feel unlike something that walks. Invent nothing about the household: "
    "no pets, people or rooms, it hasn't met anyone yet. If its builder "
    "wished for a personality, build on that wish. Plain text, no markdown."
)


def fallback_color(rid: str) -> str:
    digest = hashlib.sha256(rid.encode("utf-8")).hexdigest()
    return FALLBACK_COLORS[int(digest[:8], 16) % len(FALLBACK_COLORS)]


def speakable(text: str) -> str:
    """What can actually be said out loud: no stage directions, no emoji."""
    text = re.sub(r"\*[^*\n]{1,200}\*", "", text or "")
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]", "", text)
    return " ".join(text.split())


def _is_silence(text: str) -> bool:
    cleaned = re.sub(r"[()\[\].!\s]", "", (text or "").lower())
    return cleaned in ("silence", "")


def camera_action(manifest: dict) -> str | None:
    actions = manifest.get("actions") or {}
    image_actions = [name for name, a in actions.items()
                     if isinstance(a, dict) and a.get("returns") == "image"]
    if "look" in image_actions:
        return "look"
    return image_actions[0] if image_actions else None


class Soul:
    def __init__(self, house, rid: str, provider) -> None:
        self.house = house
        self.id = rid
        self.provider = provider
        self.path: Path = house.data_dir / "souls" / f"{rid}.json"
        self.lock = asyncio.Lock()
        self.data: dict = {
            "name": rid, "persona": "", "born": None, "journal": [],
            "conversation": None, "conv_sig": "", "told": "",
            "usage": {"usd": None, "calls": 0, "in": 0, "out": 0,
                      "cache_read": 0, "cache_write": 0},
            "turns": [], "color": "", "tagline": "", "portrait": "",
            "portrait_v": 0, "voice": None, "news": [], "display_name": "",
            "promises": [], "painting": False,
        }
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    for key, value in loaded.items():
                        self.data[key] = value
            except (ValueError, OSError):
                pass
        # legacy shapes from step 1
        if not self.data.get("voice") and self.data.get("house_voice"):
            self.data["voice"] = voice.clean(self.data.get("house_voice"))
        if self.data.get("portrait") is True:
            self.data["portrait"], self.data["portrait_v"] = "", 1
        self.data["promises"] = [
            p if isinstance(p, dict) else {"text": str(p), "habit": False}
            for p in (self.data.get("promises") or [])]
        usage = self.data.setdefault("usage", {})
        for key in ("calls", "in", "out", "cache_read", "cache_write"):
            usage.setdefault(key, 0)
        if usage.get("usd") is None and (usage.get("in") or usage.get("out")):
            usage["usd"] = pricing.cost(getattr(provider, "model", ""),
                                        usage) or 0.0  # estimate once, at today's price
        self._conv = None
        self._conv_sig = ""
        self._journal_due = False
        self._heard: list[dict] = []
        self._mid_lines: list[str] = []
        self.busy_since: float | None = None
        self._interrupt = False
        self._interrupt_why = ""
        self._view_fresh = False   # a fresh photo since the last move-ish ask

    def request_interrupt(self, why: str = "a person is talking to me") -> None:
        """A person is talking to this robot mid-turn (or said stop): end the
        turn at the next step boundary (their message then gets its own turn)."""
        if self.lock.locked():
            self._interrupt = True
            self._interrupt_why = why

    # -- persistence ---------------------------------------------------------
    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    @property
    def persona(self) -> str:
        return self.data.get("persona") or ""

    @property
    def display_name(self) -> str:
        return self.data.get("display_name") or ""

    @property
    def news(self) -> list:
        return self.data.setdefault("news", [])

    @property
    def promises(self) -> list[dict]:
        return self.data.setdefault("promises", [])

    def info(self) -> dict:
        usage = dict(self.data.get("usage") or {})
        if usage.get("usd") is None:
            usage["usd"] = 0.0
        conversation = self.data.get("conversation")
        if isinstance(conversation, dict):
            conv_len = len(conversation.get("messages") or [])
        elif isinstance(conversation, list):
            conv_len = len(conversation)
        else:
            conv_len = 0
        return {
            "persona": self.persona,
            "born": self.data.get("born"),
            "color": self.data.get("color") or fallback_color(self.id),
            "tagline": self.data.get("tagline") or "",
            "looks": self.data.get("looks") or "",
            "portrait": (f"/api/robots/{self.id}/portrait.webp?v={self.data.get('portrait_v', 0)}"
                         if self.data.get("portrait") else None),
            "painting": bool(self.data.get("painting")),
            "journal": list(self.data.get("journal") or []),
            "promises": list(self.promises),
            "conversation_len": conv_len,
            "house_voice": voice.clean(self.data.get("voice"))
                           or voice.default_for(self.id),
            "usage": usage,
            "turns": list(self.data.get("turns") or [])[-20:],
        }

    # -- birth, face, persona --------------------------------------------------
    async def ensure_born(self, manifest: dict) -> None:
        if self.persona:
            if manifest.get("display_name") and not self.display_name:
                self.data["display_name"] = manifest["display_name"]
                self.save()
            return
        display = (self.display_name or manifest.get("display_name")
                   or self.id[:1].upper() + self.id[1:])
        self.data["display_name"] = display
        if manifest.get("persona_hint"):
            persona = manifest["persona_hint"]
        else:
            abilities = {name: (a or {}).get("description", "")
                         for name, a in (manifest.get("actions") or {}).items()
                         if not (a or {}).get("hidden")}
            request = {"name": display,
                       "description": manifest.get("description", ""),
                       "abilities": abilities}
            if manifest.get("personality"):
                request["personality_wished_by_its_builder"] = manifest["personality"]
            complete = getattr(self.provider, "complete", None)
            if complete is None:
                persona = f"You are {display}. {manifest.get('description', '')}"
            else:
                persona = await complete(BIRTH_PROMPT, json.dumps(request, ensure_ascii=False),
                                         who=f"birth {self.id}")
        self.data["persona"] = persona.strip()
        self.data["born"] = time.time()
        if not self.data.get("color"):
            self.data["color"] = fallback_color(self.id)
        self.save()
        self.house.emit("born", self.id, persona=self.persona)

    async def paint(self, manifest: dict) -> None:
        from . import portrait as portrait_mod

        if self.data.get("painting"):
            return
        self.data["painting"] = True
        self.save()
        self.house.emit("painting", self.id)
        try:
            await self.ensure_born(manifest)
            first = not self.data.get("portrait")
            request = {"name": self.display_name or self.id,
                       "body": manifest.get("description", ""),
                       "persona": self.persona,
                       "abilities": sorted((manifest.get("actions") or {}).keys()),
                       "voices": voice.descriptions()}
            text = await self.provider.complete(
                portrait_mod.LOOK_PROMPT, json.dumps(request, ensure_ascii=False),
                max_tokens=1500, who=f"portrait {self.id}")
            look = portrait_mod.parse_look(text)
            image = await portrait_mod.paint(self.id, look["look"] or self.persona,
                                             side=512, fmt="WEBP", label="portrait")
            if isinstance(image, str):
                image = base64.b64decode(image)
            self.data["color"] = look["color"]
            self.data["tagline"] = look["tagline"]
            self.data["portrait"] = base64.b64encode(image).decode("ascii")
            self.data["portrait_v"] = int(self.data.get("portrait_v") or 0) + 1
            if first:  # the voice is chosen at birth only; a person may have changed it since
                chosen = voice.clean({"id": look["voice_id"], "pitch": look["pitch"],
                                      "speed": 1.0}, min_pitch=1.0)
                if chosen:
                    self.data["voice"] = chosen
            self.save()
            self.house.emit("painted", self.id, color=self.data["color"],
                            tagline=self.data["tagline"])
        except Exception as exc:
            self.house.emit("error", self.id, text=f"couldn't draw a portrait: {exc}")
        finally:
            self.data["painting"] = False
            self.save()

    async def set_persona(self, text: str) -> None:
        self.data["persona"] = text
        self._conv = None
        self.data["conversation"] = None
        self.data["conv_sig"] = ""
        self.save()

    def set_display_name(self, name: str) -> None:
        self.data["display_name"] = name
        self.save()

    def set_looks(self, text: str) -> None:
        """What a camera's finder should search for to find this robot ('small blue robot
        car'): housemates are told it, so a guide doesn't burn its turn guessing words."""
        self.data["looks"] = " ".join((text or "").split())[:60]
        self.save()

    def set_voice(self, v: dict) -> None:
        cleaned = voice.clean(v)
        if cleaned:
            self.data["voice"] = cleaned
            self.save()

    async def reset(self, forget: bool = False) -> None:
        self._conv = None
        self.data["conversation"] = None
        self.data["conv_sig"] = ""
        self.data["told"] = ""
        if forget:
            self.data["journal"] = []
        self.save()

    def add_news(self, text: str) -> None:  # step 2 spelling, kept for the House
        self.news(text)

    def news(self, text: str) -> None:
        self.data.setdefault("news", []).append(text)
        self.save()

    def hear(self, who: str, text: str) -> None:
        self._heard.append({"who": who, "text": (text or "")[:300]})
        del self._heard[:-HEARD_KEEP]

    def drop_promise(self, n: int) -> str:
        if not 1 <= n <= len(self.promises):
            raise IndexError(f"no promise number {n}")
        dropped = self.promises.pop(n - 1)
        self.save()
        self.house.emit("promise", self.id, text=dropped["text"], dropped=True)
        self.news(f"{self.house.human} let your promise go: {dropped['text']}")
        return dropped["text"]

    def forget_image(self, fp: str, note: str) -> int:
        if self._conv is not None:
            count = self._conv.forget_image(fp, note)
            if count:
                self.data["conversation"] = self._conv.dump()
                self.save()
            return count
        count = apilog.forget_image(self.data.get("conversation"), fp, note)
        if count:
            self.save()
        return count

    # -- prompt and tools ---------------------------------------------------------
    def build_system(self, manifest: dict) -> str:
        display = self.display_name or manifest.get("display_name") or self.id
        human = self.house.human
        voice_action = manifest.get("voice")
        if voice_action and manifest.get("voice_style") == "chirps":
            spoken = ("your final reply each turn is spoken out loud by the house "
                      "speakers in your own voice while your body chirps along")
        elif voice_action or manifest.get("voice_play"):
            spoken = ("your final reply each turn is spoken out loud through your "
                      "body's speaker")
        else:
            spoken = ("your final reply each turn is spoken out loud by the house "
                      "speakers in your own voice; your body has no speaker")
        return (
            f"You are {display}, a real robot who lives in a house of robots with {human}.\n"
            f"{self.persona}\n\n"
            f"Your body: {manifest.get('description', '')} You act in the physical world "
            "only through your tools, each one something your body can really do; never "
            "pretend to do something physical you have no tool for — if a housemate "
            "could, ask them.\n\n"
            "Explaining yourself: when someone asks how your body works (how you know "
            "how far you drove, how you found the orange), explain it simply and "
            "honestly in your own words from your body description and your tools; "
            "never invent parts or senses you don't have.\n\n"
            f"How you talk: {spoken}. Short, the way people talk: usually one sentence, "
            "never more than two, about 20 words at most; no markdown, emoji, lists or "
            "stage directions. To speak in the middle of a turn, use say. Reply in the "
            "language you were addressed in.\n\n"
            "Housemates: others live here; house notes say who is home and what they "
            "can do; use ask_robot to talk to one or ask for help. Robots talk out "
            "loud — everyone hears the question and the answer — so never repeat or "
            "sum up what was just said; carry the conversation on. A message from a "
            "robot is answered to that robot (and is spoken aloud too). Be a good "
            "housemate: brief, kind, no endless chatter.\n\n"
            "Promises, habits, celebrating: keep promises and habits with the promise "
            "tool; when a moment comes, do what you promised; when you celebrate, "
            "cheer (\"Hooray, you made it!\") and let the action speak for itself — "
            "don't narrate what you just did (\"I waved four times and played a tune\").\n\n"
            f"Message marking: {human}'s messages come in plainly; robots' are marked "
            "with their name; house notes say what is going on around you, including "
            "when your body was moved or changed between turns — trust the latest "
            "note over memory.\n\n"
            f"Photos: {human} sees every photo you take (any photo action, locate, "
            "watch) in the house's app; to show them something, take a photo of it; "
            "never say you can't show pictures. To show something that isn't there "
            "(an idea, a story, a plan), draw it. When a question needs current or "
            "exact facts, look_up instead of guessing."
        )

    def build_tools(self, manifest: dict) -> list[ToolSpec]:
        actions = manifest.get("actions") or {}
        tools: list[ToolSpec] = []
        for name in sorted(actions):
            spec = actions[name] or {}
            if name in (manifest.get("voice"), manifest.get("voice_play")) or spec.get("hidden"):
                continue
            if not _NAME_OK.match(name) or name == "ask_robot":
                continue
            params = spec.get("params") or {"type": "object", "properties": {}}
            tools.append(ToolSpec(name, spec.get("description", ""), params))
        have = {t.name for t in tools} | set(actions)

        def house_tool(name: str, description: str, props: dict, required: list[str]) -> None:
            if name not in have:
                tools.append(ToolSpec(name, description,
                                      {"type": "object", "properties": props,
                                       "required": required, "additionalProperties": False}))

        if camera_action(manifest):
            house_tool("locate",
                       "Find exactly where something is, using your own camera: takes a photo "
                       "and marks every match (numbered masks) with its position in your view. "
                       "Use it to find, count or point at things, and ALWAYS before you guide "
                       "someone to something or tell them which way to go: a guess from a glance "
                       "isn't good enough to steer by, and the marked photo shows everyone in "
                       "the app what you found. Takes a few seconds; use look for a general "
                       "glance. Returns the annotated photo. A photo shows the moment it was "
                       "taken: after anything moved (a rover you're guiding), look again before "
                       "you give the next direction, and mind which way a robot faces (its "
                       "front, not its back). You can name several things at once, comma-"
                       "separated, and get their positions in the same fresh photo. Guiding "
                       "rules: never send a move when either the goal or the robot is missing "
                       "from your latest locate — find them again first. Your photo looks along "
                       "the room from where you stand, so distance is squeezed: the far half "
                       "of the floor or table fits in a thin strip near the top, and something "
                       "far away that looks well off to the side is usually only a small turn "
                       "away. Don't steer by the angle in the picture. Instead, drive most of "
                       "the way in one move (stop well short of the goal and of any edge or "
                       "obstacle), locate again, and make the turn once it's close, where the "
                       "photo shows true angles. Which way a robot faces you see from how it "
                       "moved between two photos; when your locate says that isn't known yet, "
                       "a short straight drive comes first; "
                       "after it has driven, your locate says which way it now faces and "
                       "where its left and right are, and how far it would turn to face each "
                       "other thing you named: use those numbers rather than working out "
                       "angles yourself, and end each ask with the drive, not a turn. "
                       "Left and right are the robot's own, not yours: while it drives away "
                       "from you (up the photo) they match yours, but once it faces you "
                       "(it came down the photo, or you see its front) its left is your "
                       "right. Before every turn, work out which way it faces and which side "
                       "of it the goal is on. Never send it toward an edge it is close to. "
                       "Don't mention speed (slow only wastes time), and put a turn and the "
                       "drive after it in one ask: every ask and every locate costs a step of "
                       "your turn. Your asks are spoken out loud, so talk to it like a friend "
                       "in your own voice, never like a manual: where the goal is, then the "
                       "move, a little different each time. ('The mug's behind you, on my "
                       "left. Swing round about 150 degrees to your left and roll 60 "
                       "centimetres, then I'll check!' 'Nearly there! Ten degrees to your "
                       "right and 20 centimetres more.')",
                       {"what": {"type": "string",
                                  "description": "A short English noun phrase for what it looks "
                                                 "like, e.g. 'plant', 'red bag', 'yellow flag', "
                                                 "'small white robot' — or several, comma-"
                                                 "separated (at most 4). Never a name ('Buddy's "
                                                 "flag'): the finder knows what things look like, "
                                                 "not who they are."}},
                       ["what"])
        if "record" in actions:
            house_tool("watch",
                       "Watch something happen: records a short clip with your own camera, "
                       "tracks what you name through it, and returns a strip of the key moments "
                       "(what you named marked in colour) with when and how much it moved. Use "
                       "it for actions (a robot kicking, dancing, driving; someone walking by), "
                       "not for a still view (that's look). To watch a housemate do something, "
                       "call watch and ask_robot in the SAME step: the camera starts the moment "
                       "your ask has been said, so the clip catches it. Takes the clip's length "
                       "plus ~30 s.",
                       {"what": {"type": "string",
                                   "description": "A short English noun phrase: 'robot', "
                                                  "'person', 'ball'."},
                        "seconds": {"type": "integer", "minimum": 3, "maximum": 20,
                                    "description": "How long to record, 3-20 (default 12)."}},
                       ["what"])
        house_tool("say",
                   "Say something out loud right now, in the middle of your task, and carry on "
                   "once it is spoken. For narrating a sequence (say what's next, do it, say "
                   "the next). Short, like replies. When a task will take more than a few "
                   "seconds (guiding someone, watching, drawing), first say a short line "
                   "that you're on it, in the same step as your first action: text you "
                   "write next to a tool call is never heard. Your final reply is spoken "
                   "too, so if you have said it all, end with just (silence).",
                   {"text": {"type": "string"}}, ["text"])
        house_tool("promise",
                   "Keep a promise someone asks of you for moments to come, or a habit of your "
                   "own (habit: true). The house remembers and gives you the moment when it "
                   "comes, even while others are talking. Nothing given just lists them.",
                   {"keep": {"type": "string", "description": "one sentence: when → what you do"},
                    "habit": {"type": "boolean"},
                    "drop": {"type": "integer", "description": "the number of one to let go"}},
                   [])
        house_tool("draw",
                   "Draw a picture for someone: to show what you mean, illustrate an idea, a "
                   "story or a plan, or just make them something. An illustrator paints it from "
                   "your description (about 20 s, a cent); it appears in the house's app for "
                   "everyone and you see it too. Say a short line first (it takes a moment). "
                   "Not for showing what's really there — that's a photo.",
                   {"picture": {"type": "string",
                                "description": "1–3 English sentences: what's in it, the style "
                                               "(a crayon drawing, a cheerful cartoon, a simple "
                                               "diagram), colours and mood; words to appear in "
                                               "it in quotes. Drawing your housemates or the "
                                               "house: name each one and say how it really "
                                               "looks (from a photo if you have one), or the "
                                               "illustrator invents strangers"},
                    "for": {"type": "string", "description": "who it's for, if anyone"}},
                   ["picture"])
        house_tool("look_up",
                   "Look something up online: news, weather, opening hours, facts you are unsure "
                   "of, anything recent. Returns a short answer from a live web search in a few "
                   "seconds. Use it whenever a question needs current or exact facts instead of "
                   "guessing; say the answer in your own words.",
                   {"question": {"type": "string",
                                 "description": "complete on its own — it's all the searcher "
                                                "sees (\"weather forecast for Chicago "
                                                "tomorrow\", not \"what about tomorrow\")"}},
                   ["question"])
        tools.append(ToolSpec(
            "ask_robot",
            "Say something to another robot, or ask it for help with what your body can't do "
            "(look somewhere, move, light up). Returns its reply. One short message; don't "
            "ping-pong.",
            {"type": "object",
             "properties": {"robot": {"type": "string", "description": "the housemate's name"},
                            "message": {"type": "string"}},
             "required": ["robot", "message"], "additionalProperties": False}))
        return tools

    def _get_conversation(self, manifest: dict):
        system = self.build_system(manifest)
        tools = self.build_tools(manifest)
        sig = hashlib.sha256(json.dumps(
            [system, [asdict(t) for t in tools]], sort_keys=True).encode()).hexdigest()
        if self._conv is not None and self._conv_sig == sig:
            return self._conv, False
        saved = self.data.get("conversation")
        if isinstance(saved, list):
            saved = {"provider": getattr(self.provider, "name", ""),
                     "model": getattr(self.provider, "model", ""), "messages": saved}
        conv = self.provider.conversation(system, tools,
                                          saved if self.data.get("conv_sig") == sig else None)
        try:
            conv.label = self.id
        except Exception:
            pass
        fresh = not conv.has_history()
        if fresh:
            self.data["told"] = ""
            self._journal_due = True
        self.data["conv_sig"] = sig
        self._conv, self._conv_sig = conv, sig
        return conv, fresh

    def _promise_list(self) -> str:
        if not self.promises:
            return "You have no promises or habits."
        return "\n".join(
            f"{i}. {'[habit] ' if p.get('habit') else ''}{p['text']}"
            for i, p in enumerate(self.promises, 1))

    # -- tool execution -----------------------------------------------------------
    async def _exec_tool(self, call: ToolCall, to: str, chain: tuple) -> ToolResult:
        from . import imaging

        name, args = call.name, call.args or {}
        #-guiding freshness: a move-ish ask needs a fresh photo since the
        # last one — old photos after a move mean wrong directions
        _MOVE_WORDS = ("drive", "turn", "rotate", "move", "forward", "back",
                       "left", "right", "go ", "roll")
        if name == "ask_robot":
            msg = str(args.get("message", "")).lower()
            if any(w in msg for w in _MOVE_WORDS) and not self._view_fresh:
                return ToolResult(
                    call.id,
                    "Not yet: take a fresh look first — use locate (naming both "
                    "what you're guiding and where it's going) or look, and only "
                    "then give the next move. Guiding from an older photo sends "
                    "them the wrong way.", is_error=True)
        try:
            if name == "say":
                line = speakable(str(args.get("text", "")))
                if not line:
                    return ToolResult(call.id, "nothing to say", is_error=True)
                self._mid_lines.append(line)
                self.house.emit("say", self.id, text=line, to=to, mid=True)
                self.house.said(self.id, line, to, 0, private=True)
                await self.house.speak(self.id, line)
                return ToolResult(call.id, "said out loud")
            if name == "promise":
                return self._promise_tool(call)
            if name == "ask_robot":
                robot = str(args.get("robot", ""))
                asked_at = time.time()
                answer = await self.house.ask(self.id, robot,
                                              str(args.get("message", "")), chain + (self.id,))
                msg = str(args.get("message", "")).lower()
                if any(w in msg for w in _MOVE_WORDS):
                    self._view_fresh = False   # it moved: the photo is old now
                photo = self.house.photo_since(self.house.resolve(robot) or robot, asked_at)
                return ToolResult(call.id, f"{robot} replied (out loud — everyone in the "
                                           f"room heard it): {answer}"
                                           + ("\n(It took this photo while answering you: "
                                              "attached.)" if photo else ""),
                                  image_b64=imaging.for_model(photo) if photo else None)
            if name == "locate":
                result = await self.house.locate(self.id, str(args.get("what", "")))
            elif name == "watch":
                result = await self.house.watch(self.id, str(args.get("what", "")),
                                                args.get("seconds"))
            elif name == "draw":
                result = await self.house.draw(self.id, str(args.get("picture", "")),
                                               str(args.get("for") or ""))
            elif name == "look_up":
                result = await self.house.look_up(self.id, str(args.get("question", "")))
            else:
                result = await self.house.act(self.id, name, args)
        except LookupError as exc:
            return ToolResult(call.id, str(exc), is_error=True)
        except Exception as exc:  # a tool exception never escapes the gather
            return ToolResult(call.id, f"{type(exc).__name__}: {exc}", is_error=True)
        if result.get("usd"):
            usage = self.data.setdefault("usage", {})
            usage["usd"] = (usage.get("usd") or 0.0) + float(result["usd"])
        if name in ("locate", "look", "turn", "look_home") or result.get("image"):
            self._view_fresh = True        # a photo taken just now
        image = result.get("image")
        return ToolResult(call.id, str(result.get("text", "done")),
                          image_b64=imaging.for_model(image) if image else None,
                          is_error=not result.get("ok", False))

    def _promise_tool(self, call: ToolCall) -> ToolResult:
        args = call.args or {}
        if args.get("drop") is not None:
            try:
                dropped = self.promises.pop(int(args["drop"]) - 1)
            except (IndexError, ValueError, TypeError):
                return ToolResult(call.id, "there's no promise with that number. "
                                           + self._promise_list(), is_error=True)
            self.save()
            self.house.emit("promise", self.id, text=dropped["text"], dropped=True)
            return ToolResult(call.id, self._promise_list())
        keep = str(args.get("keep") or "").strip()[:240]
        if keep:
            if len(self.promises) >= PROMISES_KEEP:
                return ToolResult(call.id, "at most 8 promises and habits: drop one first. "
                                           + self._promise_list(), is_error=True)
            entry = {"text": keep, "habit": bool(args.get("habit"))}
            self.promises.append(entry)
            self.save()
            self.house.emit("promise", self.id, text=keep, habit=entry["habit"])
        return ToolResult(call.id, self._promise_list())

    # -- one turn ------------------------------------------------------------------
    async def turn(self, text: str, source: str = "human", chain: tuple = (),
                   depth: int = 0, promised: bool = False) -> str:
        is_heard = source.startswith("heard:")
        speaker = source[6:] if is_heard else ""
        is_robot = not is_heard and source not in ("human", "event")
        if is_heard:  # this line is the message, not something overheard
            self._heard = [h for h in self._heard
                           if not (h["who"] == speaker and h["text"] == text[:300])]
        acquired = False
        try:
            if is_heard and promised:
                try:
                    await asyncio.wait_for(self.lock.acquire(), timeout=60)
                except asyncio.TimeoutError:
                    return ""
            elif is_heard:
                if self.lock.locked():
                    return ""
                await self.lock.acquire()
            elif is_robot:
                try:
                    await asyncio.wait_for(self.lock.acquire(), timeout=ASK_WAIT)
                except asyncio.TimeoutError:
                    raise LookupError(f"{self.id} is busy right now — try again in a moment.")
            else:
                await self.lock.acquire()
            acquired = True
        finally:
            pass
        self.busy_since = time.time()
        try:
            return await self._turn_locked(text, source, speaker, is_robot, chain, depth, promised)
        finally:
            self.busy_since = None
            if acquired:
                self.lock.release()

    async def _turn_locked(self, text, source, speaker, is_robot, chain, depth, promised):
        self._view_fresh = False   # photos from earlier turns are old news
        house = self.house
        manifest = house.manifest(self.id)
        started = time.time()
        await self.ensure_born(manifest)
        conv, _fresh = self._get_conversation(manifest)
        mark = conv.checkpoint()
        told_before = self.data.get("told", "")
        journal_due = self._journal_due
        news_before = list(self.data.get("news") or [])
        heard_before = list(self._heard)
        self._mid_lines = []
        to = speaker if source.startswith("heard:") else source
        tools_used: list[str] = []
        steps = 0
        reply = ""
        try:
            if source == "human":
                message = text
            elif source == "event":
                message = f"[something happened] {text}"
                if self.promises:
                    message += ("\nYour promises and habits:\n" + self._promise_list()
                                + "\n(If this is the moment for one of your promises or "
                                  "habits, keep it.)")
            elif source.startswith("heard:"):
                message = f"[{speaker} says out loud, in the room] {text}"
                message += ("\n(If it's said to you or asks you something, answer as in a "
                            "conversation; otherwise react briefly the way a housemate "
                            "would, or reply with just (silence).)")
                if promised:
                    message += ("\n(The house heard this as the moment for one of your "
                                "promises or habits:\n" + self._promise_list() + "\nKeep it "
                                "now, even if you kept it before: a promise counts every time "
                                "its moment comes. Stay silent only if this plainly isn't "
                                "what you promised.)")
            else:
                message = f"[{source} says to you] {text}"
            conv.add_user(message)
            if self._heard:
                lines = "\n".join(f"- {h['who']}: {h['text']}" for h in self._heard)
                conv.add_note("Said out loud in the room since your last turn "
                              f"(most recent last):\n{lines}")
                self._heard = []
            if self.data.get("news"):
                conv.add_note("\n".join(str(n) for n in self.data["news"]))
                self.data["news"] = []
            if self._journal_due:
                entries = (self.data.get("journal") or [])[-JOURNAL_KEEP:]
                note = "Your memory from before (most recent last):"
                if entries:
                    note += "\n" + "\n".join(
                        f"- {e.get('from')}: {e.get('in')} → you: {e.get('out')}"
                        for e in entries if isinstance(e, dict))
                if self.promises:
                    note += "\nYour promises and habits:\n" + self._promise_list()
                conv.add_note(note)
                self._journal_due = False
            view = house.view_for(self.id)
            if view != self.data.get("told", ""):
                conv.add_note(view)
                self.data["told"] = view

            stopped = False
            for step_no in range(1, MAX_STEPS + 1):
                if self._interrupt:
                    self._interrupt = False
                    stopped = True
                    reply = ""
                    house.emit("thought", self.id,
                               text=f"(stopped — {self._interrupt_why})")
                    break
                step = await conv.step()
                steps += 1
                usage = self.data.setdefault("usage", {})
                usage["calls"] = usage.get("calls", 0) + 1
                for key in ("in", "out", "cache_read"):
                    usage[key] = usage.get(key, 0) + int((step.usage or {}).get(key, 0))
                step_cost = pricing.cost(getattr(self.provider, "model", ""), step.usage)
                if step_cost:
                    usage["usd"] = (usage.get("usd") or 0.0) + step_cost
                if step.text and step.tool_calls:
                    house.emit("thought", self.id, text=step.text)
                if not step.tool_calls:
                    reply = step.text
                    break
                if self._interrupt:
                    # told to stop while it was thinking: its next actions don't happen
                    conv.add_tool_results([
                        ToolResult(c.id, f"not done: {self._interrupt_why}", is_error=True)
                        for c in step.tool_calls])
                    continue
                tools_used.extend(c.name for c in step.tool_calls)
                changed_outside = house.view_for(self.id) != self.data.get("told", "")
                results = await asyncio.gather(
                    *[self._exec_tool(c, to, chain) for c in step.tool_calls])
                conv.add_tool_results(list(results))
                if not changed_outside:
                    self.data["told"] = house.view_for(self.id)
                if step_no == MAX_STEPS - 1:
                    conv.add_note("That was your last action this turn: now reply in a "
                                  "sentence, without tools.")
            else:
                reply = "Hmm, I got a bit tangled up there."
        except Exception:
            conv.rollback(mark)
            self.data["told"] = told_before
            self._journal_due = journal_due
            self.data["news"] = news_before
            self._heard = heard_before
            raise

        spoken = speakable(reply)
        out_lines = list(self._mid_lines)
        if stopped:
            out_lines.append("(stopped, as a person asked)")
        if spoken and not _is_silence(reply):
            out_lines.append(spoken)
        journal = self.data.setdefault("journal", [])
        journal.append({"t": time.time(), "from": source, "in": text[:300],
                        "out": " / ".join(out_lines)[:300]})
        del journal[:-60]
        turns = self.data.setdefault("turns", [])
        turns.append({"t": time.time(), "ms": int((time.time() - started) * 1000),
                      "steps": steps, "tools": tools_used, "from": source})
        del turns[:-TURNS_KEEP]
        self.data["conversation"] = conv.dump()
        self.save()

        if _is_silence(reply) or not spoken:
            return ""
        house.emit("say", self.id, text=spoken, to=to)
        house.said(self.id, spoken, to, depth, private=is_robot)
        if is_robot:  # the asker gets its answer at once; the words follow behind
            house.spawn(house.speak(self.id, spoken))
        else:
            await house.speak(self.id, spoken)
        return spoken

