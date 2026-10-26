"""The pins driver (spec 04): serial ⇄ hub, as a body.

    python bodies/pins/pins_body.py <config.json> [--check]

One generic sketch (firmware/pins) turns a Uno/Nano into a board the house
configures over a text protocol; this driver turns a config of parts into
a robot. Every hardware call runs in a thread, because serial blocks.
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "sdk"))
sys.path.insert(0, str(Path(__file__).parent))

from body import Body  # noqa: E402
import parts as parts_lib  # noqa: E402

log = logging.getLogger("pins_body")


# -- the simulated board ---------------------------------------------------------

class SimBoard:
    """Same pins interface, no serial: for tests and demos."""

    def __init__(self) -> None:
        self.modes: dict[int, str] = {}
        self.values: dict[int, int] = {}
        self._events: list[str] = ["HELLO house-pins 2 sim"]
        self.broken = False

    def info(self) -> str:
        return "INFO house-pins 2 sim"

    def poll_events(self) -> list[str]:
        events, self._events = self._events, []
        return events

    def mode(self, pin: int, mode: str) -> None:
        print(f"sim board: MODE {pin} {mode}")
        self.modes[pin] = mode
        if mode == "pullup":
            self.values[pin] = 1

    def write(self, pin: int, value: int) -> None:
        print(f"sim board: W {pin} {value}")
        if self.modes.get(pin) not in ("out", "pwm", "servo"):
            raise RuntimeError("board: pin is not an output")
        self.values[pin] = value

    def read(self, pin: int) -> int:
        if self.modes.get(pin) == "analog":
            return 600 + (int(time.time()) % 7)
        return self.values.get(pin, 1)

    def tone(self, pin: int, hz: int, ms: int) -> None:
        print(f"sim board: TONE {pin} {hz} {ms}")

    def watch(self, pin: int) -> None:
        print(f"sim board: WATCH {pin}")


# -- the serial link ---------------------------------------------------------------

class SerialLink:
    """One board on a real port. Commands hold a lock and wait for one reply."""

    def __init__(self, port: str) -> None:
        import serial

        self.port = port
        self.ser = serial.Serial(port, 115200, timeout=0.1, exclusive=True)
        self._lock = threading.Lock()
        self._replies: queue.Queue = queue.Queue()
        self._events: queue.Queue = queue.Queue()
        self._pending: list[str] = []
        self.broken = False
        self.broken_why = ""
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        buf = b""
        try:
            while True:
                chunk = self.ser.read(64)
                if chunk:
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        line = raw.decode("utf-8", "replace").strip()
                        if not line:
                            continue
                        if line.startswith("E ") or line.startswith("HELLO"):
                            self._events.put(line)
                        else:
                            self._replies.put(line)
        except Exception as exc:  # a read/write OSError marks the link broken
            self.broken = True
            self.broken_why = f"the board's USB link is down ({exc})"

    def poll_events(self) -> list[str]:
        events = list(self._pending)
        self._pending = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return events

    def wait_hello(self, seconds: float = 4.0) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                line = self._events.get(timeout=max(0.05, deadline - time.time()))
            except queue.Empty:
                break
            if line.startswith("HELLO"):
                return True
            self._pending.append(line)
        return False

    def command(self, cmd: str) -> str:
        with self._lock:
            if self.broken:
                raise RuntimeError(self.broken_why)
            while True:
                try:
                    self._replies.get_nowait()  # clear stale replies
                except queue.Empty:
                    break
            try:
                self.ser.write((cmd + "\n").encode())
            except Exception as exc:
                self.broken = True
                self.broken_why = f"the board's USB link is down ({exc})"
                raise RuntimeError(self.broken_why)
            try:
                reply = self._replies.get(timeout=2.0)
            except queue.Empty:
                raise RuntimeError(f"the board didn't answer '{cmd}'")
            if reply.startswith("ERR "):
                raise RuntimeError(f"board: {reply[4:]}")
            return reply

    def info(self) -> str:
        return self.command("INFO")

    def mode(self, pin: int, mode: str) -> None:
        self.command(f"MODE {pin} {mode}")

    def write(self, pin: int, value: int) -> None:
        self.command(f"W {pin} {value}")

    def read(self, pin: int) -> int:
        reply = self.command(f"R {pin}")
        return int(reply.split()[2])

    def tone(self, pin: int, hz: int, ms: int) -> None:
        self.command(f"TONE {pin} {hz} {ms}")

    def watch(self, pin: int) -> None:
        self.command(f"WATCH {pin}")

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass


# -- building the body ---------------------------------------------------------------

def build_part(pid: str, part_config: dict, board, pins, tell):
    cls = parts_lib.KINDS[part_config["kind"]]
    return cls(pid, part_config, board, pins, tell)


def build_body(config: dict, pins):
    """(body, parts, rgb_parts, piezo_parts) for a validated config."""
    board = parts_lib.BOARDS[config.get("board") or "uno"]
    body = Body(config["name"], config.get("description", ""),
                display_name=config.get("display_name"),
                persona_hint=config.get("persona_hint"),
                personality=config.get("personality"),
                voice="chirp" if config.get("voice") else None,
                voice_style="chirps" if config.get("voice") else None)

    async def tell(text: str) -> None:
        await body.tell(text)

    part_objs = [build_part(pid, part_config, board, pins, tell)
                 for pid, part_config in (config.get("parts") or {}).items()]
    rgbs = [p for p in part_objs if isinstance(p, parts_lib.RgbLedPart)]
    piezos = {p.id: p for p in part_objs if isinstance(p, parts_lib.PiezoPart)}

    def refresh_rgbs() -> None:
        for rgb in rgbs:  # tone() disturbs PWM on 3/11: write the colour again
            rgb.refresh()

    for part in part_objs:
        for action in part.actions():
            def make_handler(act, owner):
                async def handler(**kwargs):
                    result = await asyncio.to_thread(act.run, **kwargs)
                    if isinstance(owner, parts_lib.PiezoPart):
                        await asyncio.to_thread(refresh_rgbs)
                    return result
                return handler
            body.action(action.name, action.description, _properties(action.params),
                        required=_required(action.params), hidden=action.hidden,
                        sound=action.sound or isinstance(part, parts_lib.PiezoPart))(
                make_handler(action, part))

    voice_id = config.get("voice")
    if voice_id and voice_id in piezos:
        piezo = piezos[voice_id]

        async def chirp(text: str, seconds: float = 0.0, early: float = 0.0):
            notes = parts_lib.chirp_notes(text or "", float(seconds or 0))
            while notes and notes[-1][0] == 0:  # the closing pause is silence
                notes.pop()
            if not notes:
                notes = [(1200, 60)]
            total = sum(ms for _hz, ms in notes) / 1000

            def play() -> None:
                piezo.play(notes)
                refresh_rgbs()
            if early and float(early) > 0:
                threading.Thread(target=play, daemon=True).start()
                await asyncio.sleep(max(0.0, total - float(early)))
                return "chirping"
            await asyncio.to_thread(play)
            return "chirped"

        body.action("chirp", "Speak in chirps (the house speaker then says your words).",
                    {"text": {"type": "string"},
                     "seconds": {"type": "number", "minimum": 0},
                     "early": {"type": "number", "minimum": 0}},
                    required=["text"], sound=True)(chirp)

    sensors = [p for p in part_objs if type(p).sense is not parts_lib.Part.sense]
    if sensors:
        abouts = ", ".join(p.about for p in sensors)

        async def sense():
            merged: dict = {}
            for part in sensors:
                merged.update(await asyncio.to_thread(part.sense))
            return merged
        body.action("sense", f"Read your sensors ({abouts}).", {})(sense)

    ids = [p.id for p in part_objs]

    async def self_test(part: str):
        owner = next(p for p in part_objs if p.id == part)
        observed = await asyncio.to_thread(owner.self_test)
        if isinstance(owner, parts_lib.PiezoPart):
            await asyncio.to_thread(refresh_rgbs)
        text = f"ran the {part} self-test — expected: {owner.test_expect}"
        if observed is not None:
            text += f" · observed: {observed}"
        return text
    body.action("self_test", "Run one part's self-test (for checking the wiring).",
                {"part": {"type": "string", "enum": ids}}, hidden=True)(self_test)
    return body, part_objs


def _properties(params: dict) -> dict:
    return params


def _required(params: dict):
    return list(params.keys())


# -- the reading loop -----------------------------------------------------------------

async def reading_round(body: Body, part_objs: list, pins) -> None:
    for line in pins.poll_events():
        if line.startswith("HELLO"):
            for part in part_objs:
                await asyncio.to_thread(part.setup)
            await body.tell("Your board just restarted (power blip?) — "
                            "you're set up again.")
        elif line.startswith("E "):
            _e, pin, value = line.split()
            for part in part_objs:
                if int(pin) in part.pin.values():
                    await part.on_pin(int(pin), int(value))
    for part in part_objs:
        if type(part).sense is parts_lib.Part.sense:
            continue
        try:
            reading = await asyncio.to_thread(part.sense)
            state = part.state(reading)
            if state:
                await body.set_state(**state)
            await part.on_reading(reading)
        except Exception:
            pass  # a serial hiccup; next round


async def reading_loop(body: Body, part_objs: list, pins) -> None:
    while True:
        if getattr(pins, "broken", False):
            log.error("board link lost (%s) — restarting", pins.broken_why)
            await body.set_state(board="reconnecting")
            await asyncio.sleep(0.5)
            os._exit(3)  # the supervisor starts a fresh driver
        await reading_round(body, part_objs, pins)
        await asyncio.sleep(0.5)


# -- main ------------------------------------------------------------------------------

def pick_port(config: dict) -> str:
    port = os.environ.get("HOUSE_PINS_PORT") or config.get("port")
    if port:
        return port
    candidates = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
    return candidates[0] if candidates else ""


def open_link(port: str):
    if port.startswith("sim"):
        return SimBoard()
    while not os.path.exists(port):
        log.error("board %s isn't plugged in — waiting for it", port)
        while not os.path.exists(port):
            time.sleep(2)
    time.sleep(1)  # let the USB device settle
    link = SerialLink(port)
    if not link.wait_hello(4.0):
        info = link.info()  # a board without auto-reset
    else:
        info = link.info()
    if "house-pins" not in info:
        raise SystemExit(f"the board on {port} isn't running house-pins "
                         f"(it said '{info}') — flash firmware/pins")
    return link


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="The pins driver: one board, one robot.")
    parser.add_argument("config")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(format="%(asctime)s %(name)s %(message)s", level=logging.INFO)
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    problems, warnings = parts_lib.validate(config)
    for warning in warnings:
        print(f"warning: {warning}")
    if problems:
        print("can't start: " + "; ".join(problems))
        return 1
    if args.check:
        print(f"{config['name']}: config OK ({len(config.get('parts') or {})} parts)")
        return 0
    port = pick_port(config)
    if not port:
        print("can't start: no board port (set HOUSE_PINS_PORT or plug a board in)")
        return 1
    pins = open_link(port)
    body, part_objs = build_body(config, pins)

    async def setup_and_run() -> None:
        for part in part_objs:
            await asyncio.to_thread(part.setup)
        await body.run_async(reading_loop(body, part_objs, pins))

    asyncio.run(setup_and_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
