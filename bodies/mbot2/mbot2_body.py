"""The mBot2 rover's body (spec 07): actions over the rover link.

Run by the workshop: python -u bodies/mbot2/mbot2_body.py <robot.json>
The rover dials this process over Wi-Fi (RoverLink); this body dials the
hub like every body. The rover's key comes from the workshop's secrets,
never from the config.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "sdk"))
sys.path.insert(0, str(Path(__file__).parent))

from body import Body  # noqa: E402
from rover_link import (  # noqa: E402
    MOVE_TIMEOUT, TURN_SIGN, Offline, RoverLink, load_secret)

BACK_MAX = 10
FRONT_MARGIN = 8   # cm kept between its nose and whatever its distance sensor sees ahead
OFFLINE_TEXT = "offline (switched off, or out of Wi-Fi)"
MOODS = ["happy", "sad", "surprised", "sleepy", "love", "wink", "angry",
         "neutral"]
COLORS = ["red", "green", "blue", "yellow", "purple", "white", "orange",
          "off"]
EYES = ["happy", "wink", "thinking", "dizzy", "standby"]


def signed_degrees(value: float) -> str:
    word = "left" if value > 0 else "right" if value < 0 else ""
    return f"{abs(value):g}° {word}".strip()


class RoverBody:
    def __init__(self, config: dict, config_path: Path) -> None:
        self.config = config
        self.config_path = config_path
        device = dict(config.get("device") or {})
        secret = load_secret(device.get("id", ""), config_path)
        self.link = RoverLink(int(device.get("rover_port") or 8220),
                              secret.get("key", ""),
                              on_change=self._on_change)
        self.display = config.get("display_name") or config.get("name", "Rover")
        self.body = Body(
            config.get("name") or "vroom-vroom",
            config.get("description") or "A small rover.",
            display_name=config.get("display_name"),
            persona_hint=config.get("persona_hint"),
            personality=config.get("personality"))
        self.body.state["rover"] = OFFLINE_TEXT
        self.body.state["reachable"] = False  # the house counts it away until the rover calls in
        self._register()

    # -- link state ------------------------------------------------------------
    async def _on_change(self, online: bool, hello: dict) -> None:
        if online:
            await self.body.set_state(rover="online", reachable=True,
                                      battery=hello.get("battery"))
            try:  # best effort: its screen shows who it is
                await self.link.call("name", text=self.display, timeout=5)
            except Exception:
                pass
        else:
            await self.body.set_state(rover=OFFLINE_TEXT, reachable=False)

    async def battery_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            if self.link.online:
                try:
                    ret = await self.link.call("ping", timeout=5)
                    if isinstance(ret, dict) and ret.get("battery") is not None:
                        await self.body.set_state(battery=ret["battery"])
                except Exception:
                    pass  # the next round tries again

    # -- actions -----------------------------------------------------------------
    async def drive(self, cm, speed=50):
        sent = cm
        note = ""
        if cm < -BACK_MAX:
            sent = -BACK_MAX
            note = " (backing up is blind, so only 10 cm: turn around to go " \
                   "further back)"
        elif cm > 0:
            # its eyes look ahead before every forward move: it stops short of what's
            # there instead of pushing it (a guide's centimetres are a guess from a photo)
            ahead = await self._distance()
            if ahead is not None and cm > ahead - FRONT_MARGIN:
                room = max(0, round(ahead - FRONT_MARGIN))
                if room < 3:
                    return (f"didn't move: something is {ahead:.0f} cm in front of me "
                            f"(asked for {cm:g} cm)")
                sent = room
                note = (f" and stopped: something is {ahead:.0f} cm in front of me "
                        f"(asked for {cm:g} cm)")
        ret = await self.link.call("drive", cm=sent, speed=speed,
                                   timeout=MOVE_TIMEOUT)
        amount = sent
        if isinstance(ret, dict) and ret.get("amount") is not None:
            amount = ret["amount"]
        return f"drove {amount:g} cm{note}"

    async def turn(self, degrees, speed=50):
        before = await self._heading()
        await self.link.call("turn", degrees=degrees * TURN_SIGN, speed=speed,
                             timeout=MOVE_TIMEOUT)
        result = f"turned {signed_degrees(degrees)}"
        after = await self._heading()
        if before is not None and after is not None and abs(degrees) < 340:
            turned = (after - before + 180) % 360 - 180
            result += f" (the gyro measured {signed_degrees(turned)})"
        return result

    async def _distance(self):
        try:
            ret = await self.link.call("sense", timeout=5)
            if isinstance(ret, dict) and ret.get("distance_cm") is not None:
                return float(ret["distance_cm"])
        except Exception:
            pass
        return None  # nothing seen (or no reading): the drive goes as asked

    async def _heading(self):
        try:
            ret = await self.link.call("sense", timeout=5)
            if isinstance(ret, dict):
                value = ret.get("heading")
                return float(value) if value is not None else None
        except Exception:
            pass
        return None

    def _register(self) -> None:
        body = self.body

        @body.action("drive", "Drive straight: + forward, − backward, in cm "
                              "(forward up to 100 per move; it stops short "
                              "of anything its distance sensor sees ahead, "
                              "and says so). Backing up is "
                              "BLIND (nothing sees behind you): at most 10 cm "
                              "per move. To go back further, turn around "
                              "first and drive forward. Answers when it has "
                              "stopped.",
                     {"cm": {"type": "number"},
                      "speed": {"type": "number"}}, required=["cm"])
        async def drive(cm, speed=50):
            return await self.drive(float(cm), float(speed))

        @body.action("turn", "Turn on the spot: + left, − right, in degrees. "
                             "Answers when it has stopped.",
                     {"degrees": {"type": "number"},
                      "speed": {"type": "number"}}, required=["degrees"])
        async def turn(degrees, speed=50):
            return await self.turn(float(degrees), float(speed))

        @body.action("stop", "Stop the wheels now.", {})
        async def stop():
            await self.link.call("stop", timeout=5)
            return "stopped"

        @body.action("face", "Show a face on your screen.",
                     {"mood": {"type": "string", "enum": MOODS}})
        async def face(mood):
            await self.link.call("face", mood=mood, timeout=5)
            return f"showing a {mood} face"

        @body.action("show", "Show text on your screen (up to 60 characters).",
                     {"text": {"type": "string"}})
        async def show(text):
            await self.link.call("show", text=str(text)[:60], timeout=5)
            return "on the screen"

        @body.action("light", "Set your five LEDs to a colour.",
                     {"color": {"type": "string", "enum": COLORS}})
        async def light(color):
            await self.link.call("light", color=color, timeout=5)
            return f"lights {color}"

        @body.action("eyes", "Your eyes (the distance sensor at your front "
                             "has blue LEDs): a brightness 0-100 (0 = off), "
                             "or an expression they play.",
                     {"level": {"type": "number"},
                      "expression": {"type": "string", "enum": EYES}},
                     required=[])
        async def eyes(level=None, expression=None):
            if expression is not None:
                await self.link.call("eyes", expression=expression, timeout=10)
                return f"eyes: {expression}"
            level = 100 if level is None else level
            await self.link.call("eyes", level=level, timeout=5)
            return f"eyes at {level:g}%"

        @body.action("beep", "Beep.", {"hz": {"type": "number"},
                                       "seconds": {"type": "number"}},
                     required=[], sound=True)
        async def beep(hz=880, seconds=0.2):
            await self.link.call("beep", hz=hz, s=seconds, timeout=10)
            return "beeped"

        @body.action("sense", "Read your sensors: battery %, heading "
                              "(degrees, from when you started or last "
                              "reset: it means nothing after you were picked "
                              "up and put down; a turn tells you how far it "
                              "really turned), distance to the nearest "
                              "thing in front of you (cm, up to 3 m; null = "
                              "nothing seen), tilt.", {})
        async def sense():
            return await self.link.call("sense", timeout=5)

        @body.action("reset_heading", "Make the way you're facing now "
                                      "heading 0.", {})
        async def reset_heading():
            await self.link.call("reset_heading", timeout=5)
            return "heading reset to 0"


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python mbot2_body.py <robot.json>")
        raise SystemExit(2)
    config_path = Path(sys.argv[1])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    rover = RoverBody(config, config_path)
    rover.body.run(rover.link.serve, rover.battery_loop())


if __name__ == "__main__":
    main()
