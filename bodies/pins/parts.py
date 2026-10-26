"""The parts library (spec 04): what each kind of part is and can do.

Pure Python, no serial import — the hub imports this too. A robot is a
config file of parts; this library knows, per kind, which pins it needs,
how a person wires it, what the soul can do with it, how to prove it works
and which sensor events matter.
"""
from __future__ import annotations

import re
import time
import zlib
from dataclasses import dataclass, field

# -- boards ---------------------------------------------------------------------

@dataclass
class Board:
    name: str
    display: str
    digital: range          # usable pins
    pwm: set
    analog: dict            # "A0" -> pin number
    caveats: dict = field(default_factory=dict)

    def pin(self, value) -> int:
        if isinstance(value, int):
            return value
        text = str(value).strip().upper()
        if text in self.analog:
            return self.analog[text]
        if re.fullmatch(r"D?\d+", text):
            return int(text.lstrip("D"))
        raise ValueError(f"unknown pin '{value}'")

    def label(self, n: int) -> str:
        for name, number in self.analog.items():
            if number == n:
                return name
        return f"D{n}"


_UNO = Board(
    name="uno", display="Arduino Uno", digital=range(2, 20),
    pwm={3, 5, 6, 9, 10, 11},
    analog={f"A{i}": 14 + i for i in range(6)},
    caveats={
        3: "shares a timer with tone(): may flicker while a piezo plays",
        11: "shares a timer with tone(): may flicker while a piezo plays",
        9: "PWM stops working here once any servo is attached (Servo uses its timer)",
        10: "PWM stops working here once any servo is attached (Servo uses its timer)",
    })
BOARDS = {"uno": _UNO, "nano": _UNO}  # spec 04: the same pin map, the same object

# -- colours and sounds ------------------------------------------------------------

COLORS: dict[str, tuple[int, int, int]] = {
    "off": (0, 0, 0), "red": (255, 0, 0), "orange": (255, 80, 0),
    "yellow": (255, 180, 0), "green": (0, 255, 0), "cyan": (0, 200, 200),
    "blue": (0, 0, 255), "purple": (160, 0, 255), "pink": (255, 40, 120),
    "white": (255, 255, 255),
}


def _tune(notes, beat_ms: float, legato: float) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for hz, beats in notes:
        total = beat_ms * beats
        out.append((hz, round(total * legato)))
        out.append((0, round(total * (1 - legato))))
    return out


_FUNK = [523, 523, 466, 523, 0, 392, 0, 392, 523, 698, 659, 523, 0, 0, 0, 0]
_AXEL = [(698, 1), (831, .75), (698, .25), (698, .25), (932, .5), (698, .5),
         (622, .5), (698, 1), (1047, .75), (698, .25), (698, .25), (1109, .5),
         (1047, .5), (831, .5), (698, .5), (1047, .5), (1397, .5), (698, .25),
         (622, .25), (622, .25), (523, .25), (784, .5), (698, 2)]

SOUNDS: dict[str, list[tuple[int, int]]] = {
    "happy": [(880, 80), (1175, 80), (1568, 140)],
    "sad": [(660, 200), (520, 200), (392, 400)],
    "alert": [(1800, 90), (0, 60), (1800, 90), (0, 60), (1800, 90)],
    "curious": [(700, 90), (950, 90), (1250, 180)],
    "sleepy": [(523, 300), (440, 500)],
    "funkytown": _tune([(hz, 1) for hz in _FUNK * 2], 238, 0.7),
    "axel_f": _tune(_AXEL, 513, 0.85),
}


def play_notes(pins, pin: int, notes) -> None:
    """Play on a beat clock: serial round-trips never stretch the rhythm."""
    deadline = time.monotonic()
    for hz, ms in notes:
        if hz > 0:
            pins.tone(pin, hz, ms)
        deadline += ms / 1000
        wait = deadline - time.monotonic()
        if wait > 0:
            time.sleep(wait)


def watch_values(sample, seconds: float = 10.0, interval: float = 0.1) -> dict:
    """Sample for a while: {"first", "lowest", "highest"}."""
    started = time.monotonic()
    first = sample()
    lowest = highest = first
    while time.monotonic() - started < seconds:
        time.sleep(interval)
        value = sample()
        lowest, highest = min(lowest, value), max(highest, value)
    return {"first": first, "lowest": lowest, "highest": highest}


# -- the chirp generator ------------------------------------------------------------

_SLEEPY = re.compile(r"\b(good\s+night|goodnight|night|sleep\w*|bed\w*|tired|"
                     r"dream\w*|yawn\w*|quiet)\b", re.I)
_PHRASE = re.compile(r"[^,.!?;:—]+[,.!?;:—]?")
_WORD = re.compile(r"[^\W\d_]+|\d+", re.UNICODE)
_VOWELS = re.compile(r"[aeiouy]+", re.I)


def _syllables(word: str) -> int:
    return max(1, min(4, len(_VOWELS.findall(word)) or 1))


def chirp_notes(text: str, seconds: float = 0.0) -> list[tuple[int, int]]:
    """Notes that sound like the line, in robot. Deterministic per text."""
    import random

    rng = random.Random(zlib.crc32((text or "").encode("utf-8")))
    sleepy = bool(_SLEEPY.search(text or ""))
    base = 800.0 if sleepy else 1250.0
    slowness = 1.4 if sleepy else 1.0
    notes: list[tuple[int, int]] = []
    elapsed_ms = 0.0

    def add(hz: float, ms: float) -> None:
        nonlocal elapsed_ms
        pitch = 0 if hz <= 0 else int(min(2400, max(500, round(hz))))
        notes.append((pitch, int(round(ms))))
        elapsed_ms += ms

    for match in _PHRASE.finditer(text or ""):
        raw = match.group(0)
        punct = raw[-1] if raw and raw[-1] in ",.!?;:—" else ""
        words = _WORD.findall(raw)
        if not words:
            continue
        excited = punct == "!"
        top = base * (1.15 if excited else 1.0) * rng.uniform(0.95, 1.08)
        factors = [rng.uniform(0.85, 1.18) for _ in words]
        counts = [_syllables(w) for w in words]
        left = sum(counts)
        stop = False
        for wi, word in enumerate(words):
            for k in range(counts[wi]):
                gap = 12 if (k > 0 or (wi == 0 and k == 0)) else 40
                if wi > 0 and k == 0:
                    gap = 40
                add(0, gap * slowness)
                hz = top * factors[wi] * (1 - 0.25 * k / counts[wi])
                ms = rng.randint(50, 72)
                if k == 0 and len(word) > 3:
                    hz *= 1.1
                    ms += 20
                if punct == "?" and left <= 2:
                    hz = top * (1.35 if left == 2 else 1.7)
                elif punct == "!" and left == 1:
                    hz, ms = top * 1.5, 60
                elif punct and left == 1:
                    hz, ms = hz * 0.8, ms + 50
                add(hz, ms * slowness)
                left -= 1
                if seconds > 0 and elapsed_ms > (seconds + 0.3) * 1000 and left > 1:
                    add(hz * 0.8, 110 * slowness)  # one trailing note, then stop
                    stop = True
                    break
            if stop:
                break
        if stop:
            break
        pause = 260 if punct in ".!?" else 150 if punct else 60
        add(0, pause * slowness)
        if seconds > 0 and elapsed_ms >= seconds * 1000:
            break
    if not notes:
        return [(1200, 60)]
    return notes


# -- parts ---------------------------------------------------------------------------

@dataclass
class Action:
    name: str
    description: str
    params: dict
    run: object  # sync callable(**kwargs) -> str, run in a thread by the driver
    hidden: bool = False
    sound: bool = False


class Part:
    kind = ""
    summary = ""
    needs: dict = {}
    wiring: list = []
    test_expect = ""
    options = ""
    LISTEN = 10.0

    def __init__(self, part_id: str, config: dict, board: Board, pins, tell) -> None:
        self.id = part_id
        self.config = config or {}
        self.board = board
        self.pins = pins
        self.tell = tell
        self.about = self.config.get("about") or self.summary
        self.pin = {role: board.pin(value)
                    for role, value in (self.config.get("pins") or {}).items()}

    def setup(self) -> None:
        pass

    def actions(self) -> list:
        return []

    def sense(self) -> dict:
        return {}

    def state(self, reading: dict) -> dict:
        return {}

    async def on_pin(self, pin: int, value: int) -> None:
        pass

    async def on_reading(self, reading: dict) -> None:
        pass

    def self_test(self):
        return None


class LedPart(Part):
    kind = "led"
    summary = "a single LED"
    needs = {"signal": "digital"}
    wiring = ["LED long leg (+) → {signal} through a 220Ω resistor",
              "LED short leg (−) → GND rail"]
    test_expect = "the LED blinks three times"

    def setup(self) -> None:
        self.pins.mode(self.pin["signal"], "out")

    def actions(self):
        def run(on):
            self.pins.write(self.pin["signal"], 1 if on else 0)
            return f"{self.id} is {'on' if on else 'off'}"
        return [Action(self.id, f"Turn {self.about} on or off.",
                       {"on": {"type": "boolean"}}, run)]

    def self_test(self):
        for _ in range(3):
            self.pins.write(self.pin["signal"], 1)
            time.sleep(0.3)
            self.pins.write(self.pin["signal"], 0)
            time.sleep(0.3)
        return None


class RgbLedPart(Part):
    kind = "rgb_led"
    summary = "an RGB mood light (common cathode)"
    needs = {"r": "pwm", "g": "pwm", "b": "pwm"}
    wiring = ["The longest leg is the common cathode (−) → GND rail",
              "Red leg → 220Ω resistor → {r}",
              "Green leg → 220Ω resistor → {g}",
              "Blue leg → 220Ω resistor → {b}"]
    test_expect = ("the LED shows red, then green, then blue, then white, "
                   "then turns off")

    def __init__(self, *args) -> None:
        super().__init__(*args)
        self._last = COLORS["off"]

    def setup(self) -> None:
        for role in ("r", "g", "b"):
            self.pins.mode(self.pin[role], "pwm")
        self._write(self._last)

    def _write(self, rgb) -> None:
        self._last = rgb
        for role, value in zip(("r", "g", "b"), rgb):
            self.pins.write(self.pin[role], value)

    def refresh(self) -> None:
        """Write the remembered colour again (tone() can disturb PWM)."""
        self._write(self._last)

    def actions(self):
        def run(color):
            self._write(COLORS[color])
            return f"{self.id} is {color}"
        return [Action(self.id, f"Set the colour of {self.about}.",
                       {"color": {"type": "string", "enum": list(COLORS)}}, run)]

    def self_test(self):
        for name in ("red", "green", "blue", "white"):
            self._write(COLORS[name])
            time.sleep(0.6)
        self._write(COLORS["off"])
        return None


class ServoPart(Part):
    kind = "servo"
    summary = "a servo that turns to an angle"
    needs = {"signal": "digital"}
    wiring = ["Servo brown/black wire → GND rail",
              "Servo red wire → 5V rail",
              "Servo orange/yellow (signal) wire → {signal}"]
    test_expect = ("the servo swings to one side, the other side, "
                   "then back to the middle")
    options = "min, max, home (degrees)"

    def __init__(self, *args) -> None:
        super().__init__(*args)
        self.min = int(self.config.get("min", 0))
        self.max = int(self.config.get("max", 180))
        self.home = int(self.config.get("home", (self.min + self.max) // 2))

    def setup(self) -> None:
        self.pins.mode(self.pin["signal"], "servo")
        self.pins.write(self.pin["signal"], self.home)

    def _turn(self, angle: int) -> int:
        angle = max(self.min, min(self.max, int(angle)))
        self.pins.write(self.pin["signal"], angle)
        return angle

    def actions(self):
        def run(angle):
            return f"{self.id} at {self._turn(angle)}°"

        def wave(times):
            times = max(1, min(5, int(times)))
            span = (self.max - self.min) // 3
            for _ in range(times):
                self._turn(self.home - span)
                time.sleep(0.25)
                self._turn(self.home + span)
                time.sleep(0.25)
            self._turn(self.home)
            return f"waved {self.id} {times}x"
        return [Action(self.id,
                       f"Turn {self.about} to an angle ({self.home} is its rest position).",
                       {"angle": {"type": "integer", "minimum": self.min,
                                  "maximum": self.max}}, run),
                Action(f"{self.id}_wave", f"Wave {self.about} back and forth.",
                       {"times": {"type": "integer", "minimum": 1, "maximum": 5}}, wave)]

    def self_test(self):
        self._turn(self.min)
        time.sleep(0.7)
        self._turn(self.max)
        time.sleep(0.7)
        self._turn(self.home)
        return None


class PiezoPart(Part):
    kind = "piezo"
    summary = "a piezo beeper"
    needs = {"signal": "digital"}
    wiring = ["Piezo one leg → {signal}", "Piezo other leg → GND rail"]
    test_expect = "a rising three-note beep"

    def setup(self) -> None:
        self.pins.mode(self.pin["signal"], "out")

    def play(self, notes) -> None:
        play_notes(self.pins, self.pin["signal"], notes)

    def actions(self):
        def run(kind):
            self.play(SOUNDS[kind])
            return f"made a {kind} sound"
        return [Action(f"{self.id}_sound",
                       f"Make an expressive sound with {self.about}.",
                       {"kind": {"type": "string", "enum": list(SOUNDS)}}, run,
                       sound=True)]

    def self_test(self):
        self.play(SOUNDS["happy"])
        return None


class ButtonPart(Part):
    kind = "button"
    summary = "a push button"
    needs = {"signal": "digital"}
    wiring = ["Place the button across the breadboard's middle gap",
              "One leg → {signal}",
              "The diagonally opposite leg → GND rail (no resistor: the board's "
              "pull-up is used)"]
    test_expect = ("press the button (more than once is fine) within 10 seconds "
                   "of starting the test")
    options = "says (what the robot is told on a press)"

    def __init__(self, *args) -> None:
        super().__init__(*args)
        self._last_tell = 0.0

    def setup(self) -> None:
        self.pins.mode(self.pin["signal"], "pullup")
        self.pins.watch(self.pin["signal"])

    def sense(self) -> dict:
        return {f"{self.id}_pressed": self.pins.read(self.pin["signal"]) == 0}

    async def on_pin(self, pin: int, value: int) -> None:
        if value != 0:
            return
        now = time.monotonic()
        if now - self._last_tell < 0.3:  # host debounce, on top of the board's 30 ms
            return
        self._last_tell = now
        await self.tell(self.config.get("says") or f"Someone just pressed {self.about}!")

    def self_test(self):
        presses, previous, deadline = 0, 1, time.monotonic() + self.LISTEN
        last = 0.0
        while time.monotonic() < deadline:
            value = self.pins.read(self.pin["signal"])
            now = time.monotonic()
            if value == 0 and previous == 1 and now - last >= 0.3:
                presses += 1
                last = now
            previous = value
            time.sleep(0.05)
        return {"presses_noticed": presses}


class LightSensorPart(Part):
    kind = "light_sensor"
    summary = "a light sensor (photoresistor)"
    needs = {"signal": "analog"}
    wiring = ["5V rail → photoresistor → {signal}",
              "{signal} → 10kΩ resistor → GND rail"]
    test_expect = ("within 10 seconds of starting the test, cover the sensor "
                   "with your hand, then uncover it: the light level should "
                   "change a lot")

    def __init__(self, *args) -> None:
        super().__init__(*args)
        self._prev = None
        self._last_report = 0.0

    def setup(self) -> None:
        self.pins.mode(self.pin["signal"], "analog")

    def sense(self) -> dict:
        value = self.pins.read(self.pin["signal"])
        room = "dark" if value < 200 else "dim" if value < 500 else "bright"
        return {"light_level": value, "room_is": room}

    def state(self, reading: dict) -> dict:
        return {"room": reading["room_is"]} if "room_is" in reading else {}

    async def on_reading(self, reading: dict) -> None:
        new = reading.get("light_level")
        if new is None:
            return
        old, self._prev = self._prev, new
        if old is None:
            return
        sudden = abs(new - old) > 100 and max(old, new) >= 2 * max(1, min(old, new))
        now = time.monotonic()
        if sudden and now - self._last_report >= 60:
            self._last_report = now
            await self.tell(f"The room suddenly went {reading.get('room_is')} "
                            f"(light {old} → {new}).")

    def self_test(self):
        watched = watch_values(lambda: self.pins.read(self.pin["signal"]), self.LISTEN)
        return {"light_level": watched,
                "changed_a_lot": watched["highest"] - watched["lowest"] > 150}


class Tmp36Part(Part):
    kind = "tmp36"
    summary = "a TMP36 temperature sensor"
    needs = {"signal": "analog"}
    wiring = ["Flat side facing you: left leg → 5V rail",
              "Middle leg → {signal}", "Right leg → GND rail"]
    test_expect = ("the temperature should be close to the room's; pinch the "
                   "sensor for 10 seconds and it should rise a little")

    def setup(self) -> None:
        self.pins.mode(self.pin["signal"], "analog")

    def sense(self) -> dict:
        raw = self.pins.read(self.pin["signal"])
        return {"temperature_c": round((raw * 5 / 1024 - 0.5) * 100, 1)}

    def self_test(self):
        return {"temperature_c": watch_values(
            lambda: self.sense()["temperature_c"], self.LISTEN)}


class KnobPart(Part):
    kind = "knob"
    summary = "a knob (potentiometer)"
    needs = {"signal": "analog"}
    wiring = ["Outer leg → 5V rail", "Middle leg → {signal}",
              "Other outer leg → GND rail"]
    test_expect = ("turn the knob all the way both ways: it should reach "
                   "about 0 and 100")

    def setup(self) -> None:
        self.pins.mode(self.pin["signal"], "analog")

    def sense(self) -> dict:
        return {f"{self.id}_percent": round(self.pins.read(self.pin["signal"]) / 10.23)}

    def self_test(self):
        return {f"{self.id}_percent": watch_values(
            lambda: self.sense()[f"{self.id}_percent"], self.LISTEN)}


KINDS: dict[str, type] = {cls.kind: cls for cls in (
    LedPart, RgbLedPart, ServoPart, PiezoPart, ButtonPart, LightSensorPart,
    Tmp36Part, KnobPart)}

# -- validate, assign, wiring, catalog -------------------------------------------

_PREF = {"servo": [9, 10, 11, 12, 13, 7, 4, 2], "piezo": [8, 7, 4, 12, 13, 2]}
_NEED_PREF = {"digital": [2, 4, 7, 12, 13, 8], "pwm": [5, 6, 3, 11]}
PWM_LABELS = "D3, D5, D6, D9, D10, D11"


def validate(config: dict):
    problems: list[str] = []
    warnings: list[str] = []
    name = config.get("name") or ""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        problems.append("name must be a short word (letters, digits, - or _)")
    board = BOARDS.get(config.get("board") or "")
    if board is None:
        return [f"unknown board '{config.get('board')}' (known: "
                f"{', '.join(sorted(BOARDS))})"], []
    parts = config.get("parts") or {}
    used: dict[int, str] = {}
    has_servo = any((p or {}).get("kind") == "servo" for p in parts.values())
    has_piezo = any((p or {}).get("kind") == "piezo" for p in parts.values())
    for pid, part in parts.items():
        part = part or {}
        kind = part.get("kind")
        cls = KINDS.get(kind)
        if cls is None:
            problems.append(f"{pid}: unknown kind '{kind}' (known: "
                            f"{', '.join(sorted(KINDS))})")
            continue
        for role, need in cls.needs.items():
            raw = (part.get("pins") or {}).get(role)
            if raw is None:
                problems.append(f"{pid}: needs a pin for '{role}'")
                continue
            try:
                pin = board.pin(raw)
            except ValueError:
                problems.append(f"{pid}.{role}: unknown pin '{raw}'")
                continue
            label = board.label(pin)
            if pin not in board.digital:
                problems.append(f"{pid}.{role}: {label} isn't usable on {board.display}")
                continue
            if pin in used:
                problems.append(f"{label} is already used by {used[pin]}")
            else:
                used[pin] = f"{pid}.{role}"
            if need == "pwm" and pin not in board.pwm:
                problems.append(f"{pid}.{role}: {label} can't dim (PWM pins: {PWM_LABELS})")
            if need == "analog" and pin not in board.analog.values():
                problems.append(f"{pid}.{role}: {label} isn't an analog pin (A0–A5)")
            if need == "pwm" and pin in (9, 10) and has_servo:
                problems.append(f"{pid}.{role}: {label} — {board.caveats[pin]}")
            if need == "pwm" and pin in (3, 11) and has_piezo:
                warnings.append(f"{pid}.{role}: {label} — {board.caveats[pin]}")
    voice = config.get("voice")
    if voice and (parts.get(voice) or {}).get("kind") != "piezo":
        problems.append(f"voice: '{voice}' must be the id of a piezo part")
    return problems, warnings


def assign_pins(config: dict) -> dict:
    board = BOARDS.get(config.get("board") or "uno", _UNO)
    parts = config.setdefault("parts", {})
    used: set[int] = set()
    for part in parts.values():
        for raw in ((part or {}).get("pins") or {}).values():
            try:
                used.add(board.pin(raw))
            except ValueError:
                pass
    for pid, part in parts.items():
        part.setdefault("pins", {})
        cls = KINDS.get(part.get("kind"))
        if cls is None:
            continue
        for role, need in cls.needs.items():
            if part["pins"].get(role) is not None:
                continue
            pref = _PREF.get(cls.kind) or _NEED_PREF.get(need) or []
            candidates = list(pref)
            if need == "analog":
                candidates += [n for n in board.analog.values() if n not in candidates]
            elif need == "pwm":
                candidates += sorted(board.pwm)
            else:
                candidates += list(board.digital)
            for pin in candidates:
                if pin in used:
                    continue
                if need == "pwm" and pin not in board.pwm:
                    continue
                if need == "analog" and pin not in board.analog.values():
                    continue
                if need != "analog" and pin not in board.digital:
                    continue
                part["pins"][role] = board.label(pin) if need == "analog" else pin
                used.add(pin)
                break
    return config


def wiring(config: dict) -> dict:
    board = BOARDS.get(config.get("board") or "uno", _UNO)
    out: dict[str, list[str]] = {}
    for pid, part in (config.get("parts") or {}).items():
        cls = KINDS.get((part or {}).get("kind"))
        pins = (part or {}).get("pins") or {}
        if cls is None or any(pins.get(role) is None for role in cls.needs):
            out[pid] = []
            continue
        labels = {}
        for role in cls.needs:
            try:
                labels[role] = board.label(board.pin(pins[role]))
            except ValueError:
                labels = None
                break
        out[pid] = [step.format(**labels) for step in cls.wiring] if labels else []
    return out


def catalog() -> list[dict]:
    return [{"kind": cls.kind, "summary": cls.summary, "needs": dict(cls.needs),
             "wiring": list(cls.wiring), "test": cls.test_expect,
             "options": cls.options} for cls in KINDS.values()]

