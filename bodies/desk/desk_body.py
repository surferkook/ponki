"""A body on the virtual desk: the desk camera or the desk lamp.

Run: python -u bodies/desk/desk_body.py data/robots/<id>.json
The config's "part" says which one (camera or lamp). Both change and read
the shared world in world.py; neither has a speaker, so the house speaks
for them.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sdk"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from body import Body, Image  # noqa: E402

import world  # noqa: E402

COLOURS = ["off", *world.COLORS]


def camera(body: Body) -> None:
    @body.action("look", "Look through your camera right now (returns a photo of "
                         "where you're facing).", returns="image")
    async def look():
        pan = world.load()["camera"]["pan"]
        jpeg = await asyncio.to_thread(world.view, pan)
        return Image(jpeg, f"photo from the desk camera, {world.pan_words(pan)}")

    @body.action("turn", "Turn your head by this many degrees: negative is left, "
                         f"positive is right. You reach {world.PAN_LIMIT}° each way "
                         "from the middle of the desk.",
                 {"pan": {"type": "number"}})
    async def turn(pan: float = 0):
        now = float(world.load()["camera"]["pan"])
        target = max(-world.PAN_LIMIT, min(world.PAN_LIMIT, now + float(pan)))
        world.update("camera", pan=target)
        await body.set_state(heading=world.pan_words(target))
        note = "" if target == now + float(pan) else " (as far as you go)"
        return f"turned to {round(target)}°{note}: {world.pan_words(target)}"

    @body.action("look_home", "Turn back to the middle of the desk. Returns a photo "
                              "once you're there.", returns="image")
    async def look_home():
        world.update("camera", pan=0)
        await body.set_state(heading=world.pan_words(0))
        jpeg = await asyncio.to_thread(world.view, 0)
        return Image(jpeg, "photo from the desk camera, looking at the middle of the desk")


def lamp(body: Body) -> None:
    @body.action("light", "Set your light: off, warm, white or a colour.",
                 {"color": {"type": "string", "enum": COLOURS}})
    async def light(color: str = "warm"):
        world.update("lamp", color=color)
        await body.set_state(light=color)
        return "light off" if color == "off" else f"light is {color}"

    @body.action("nod", "Nod your shade a few times (a yes, a hello).",
                 {"times": {"type": "integer", "minimum": 1, "maximum": 5}}, required=[])
    async def nod(times: int = 2):
        for _ in range(int(times)):
            world.update("lamp", tilt=25)
            await asyncio.sleep(0.6)
            world.update("lamp", tilt=0)
            await asyncio.sleep(0.4)
        return f"nodded {times} times"

    @body.action("blink", "Blink your light a few times, then go back to how it was.",
                 {"times": {"type": "integer", "minimum": 1, "maximum": 10}}, required=[])
    async def blink(times: int = 3):
        before = world.load()["lamp"]["color"]
        on = before if before != "off" else "warm"
        for _ in range(int(times)):
            world.update("lamp", color="off")
            await asyncio.sleep(0.4)
            world.update("lamp", color=on)
            await asyncio.sleep(0.4)
        world.update("lamp", color=before)
        return f"blinked {times} times"


def main() -> None:
    config: dict = {}
    if len(sys.argv) > 1:
        config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    device = config.get("device") or {}
    part = config.get("part") or device.get("part") or "lamp"
    # the builder's description is the person's idea of it; the device's own
    # text says what the body is and can do, so the soul always gets both
    description = " ".join(t for t in (config.get("description"), device.get("body")) if t)
    body = Body(config.get("name") or f"desk-{part}",
                description or f"A virtual desk {part}.",
                display_name=config.get("display_name"),
                persona_hint=config.get("persona_hint"),
                personality=config.get("personality"))
    (camera if part == "camera" else lamp)(body)

    async def show_state() -> None:
        now = world.load()
        if part == "camera":
            await body.set_state(heading=world.pan_words(now["camera"]["pan"]))
        else:
            await body.set_state(light=now["lamp"]["color"])

    body.run(show_state())


if __name__ == "__main__":
    main()
