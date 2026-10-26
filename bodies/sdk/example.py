"""A fake body for testing (spec 01): python bodies/sdk/example.py

Dials the hub as `tester`, waves, changes a light, takes generated photos
and toggles its `awake` state every 60 s. Needs HOUSE_HUB and HOUSE_TOKEN.
"""
from __future__ import annotations

import asyncio
import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from body import Body, Image  # noqa: E402

body = Body(
    "tester",
    "A fake test robot that lives only in software: it can wave, change a "
    "coloured light and take simple generated photos.",
    display_name="Tester",
)


@body.action("wave", "Wave hello a number of times.",
             {"times": {"type": "integer", "minimum": 1, "maximum": 10}})
async def wave(times: int = 1):
    return f"waved {times} times"


@body.action("light", "Set the colour of the test light.",
             {"color": {"type": "string", "enum": ["off", "red", "green", "blue"]}})
async def light(color: str = "off"):
    await body.set_state(light=color)
    return f"light is {color}"


@body.action("look", "Take a photo.", {}, returns="image")
async def look():
    from PIL import Image as PILImage, ImageDraw

    img = PILImage.new("RGB", (640, 400), (70, 110, 180))
    draw = ImageDraw.Draw(img)
    draw.rectangle([40, 40, 600, 360], outline=(255, 255, 255), width=4)
    draw.text((60, 60), time.strftime("%H:%M:%S"), fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return Image(buf.getvalue(), caption="a blue test card with the time on it")


@body.action("self_test", "Check the fake body is healthy.", {}, hidden=True)
async def self_test():
    return "all good"


async def awake_loop() -> None:
    awake = True
    await body.set_state(awake=True)
    while True:
        await asyncio.sleep(60)
        awake = not awake
        await body.set_state(awake=awake)


if __name__ == "__main__":
    body.run(awake_loop)
