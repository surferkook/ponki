"""The virtual desk: a small world two virtual devices share.

A desk against a wall, drawn with Pillow: a window, a shelf, a plant, a
mug, an orange, a rubber duck and a desk lamp. The desk camera looks at
it on a pan head; the desk lamp's colour and nod show up in the camera's
photos, so a robot that asks the lamp to change can check with its own
eyes (and SAM 3.1 can find everything on the desk).

The world is one JSON file (HOUSE_DESK_DIR, default <repo>/data/desk),
written atomically, so the camera and the lamp can run as separate
driver processes on the same workshop.
"""
from __future__ import annotations

import io
import json
import math
import os
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

W, H = 2400, 900           # the whole desk, left to right
VIEW_W = 1300              # what the camera sees at once
OUT = (1040, 720)          # photo size
PAN_LIMIT = 40             # degrees each way
S = 2                      # drawn at 2x, then scaled down (smooth edges)

COLORS = {
    "warm": (255, 196, 110), "white": (255, 250, 235), "red": (240, 40, 40),
    "orange": (255, 140, 30), "yellow": (255, 225, 40), "green": (60, 210, 80),
    "blue": (50, 110, 255), "purple": (160, 70, 230), "pink": (255, 105, 180),
}
LAMP_X = 1760              # the lamp's base, in desk pixels

_DEFAULT = {"lamp": {"color": "off", "tilt": 0}, "camera": {"pan": 0}}


def world_dir() -> Path:
    env = os.environ.get("HOUSE_DESK_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent.parent / "data" / "desk"


def load() -> dict:
    try:
        data = json.loads((world_dir() / "world.json").read_text(encoding="utf-8"))
    except Exception:
        data = {}
    out = json.loads(json.dumps(_DEFAULT))
    for key in out:
        if isinstance(data.get(key), dict):
            out[key].update(data[key])
    return out


def update(part: str, **values) -> dict:
    """Change one part of the world (atomically) and return the new world."""
    world = load()
    world[part].update(values)
    folder = world_dir()
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f"world.json.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(world), encoding="utf-8")
    os.replace(tmp, folder / "world.json")
    return world


# -- drawing -------------------------------------------------------------------

def _p(*xy):
    return [v * S for v in xy]


def _sky(hour: float):
    if 7 <= hour < 17:
        return (150, 200, 245), (205, 230, 250), None
    if 17 <= hour < 20 or 5 <= hour < 7:
        return (240, 150, 110), (250, 205, 150), None
    return (25, 35, 75), (55, 65, 110), (240, 240, 210)


def _scene(world: dict, hour: float) -> Image.Image:
    img = Image.new("RGB", (W * S, H * S), (236, 224, 204))
    d = ImageDraw.Draw(img)
    # the wall, a little darker towards the desk
    for y in range(0, 560 * S, 4):
        t = y / (560 * S)
        c = tuple(int(a + (b - a) * t) for a, b in zip((240, 230, 212), (222, 206, 182)))
        d.rectangle([0, y, W * S, y + 4], fill=c)
    # the window and the sky behind it
    top, bottom, moon = _sky(hour)
    for y in range(90 * S, 430 * S, 4):
        t = (y - 90 * S) / (340 * S)
        c = tuple(int(a + (b - a) * t) for a, b in zip(top, bottom))
        d.rectangle([180 * S, y, 620 * S, y + 4], fill=c)
    if moon:
        d.ellipse(_p(500, 130, 550, 180), fill=moon)
    else:
        d.ellipse(_p(250, 330, 420, 400), fill=(245, 248, 252))
        d.ellipse(_p(330, 310, 470, 390), fill=(250, 252, 255))
    d.rectangle(_p(180, 90, 620, 430), outline=(250, 250, 248), width=14 * S)
    d.line(_p(400, 90, 400, 430), fill=(250, 250, 248), width=8 * S)
    d.line(_p(180, 260, 620, 260), fill=(250, 250, 248), width=8 * S)
    # a framed picture of mountains
    d.rectangle(_p(880, 120, 1160, 320), fill=(120, 85, 55))
    d.rectangle(_p(896, 136, 1144, 304), fill=(215, 232, 240))
    d.polygon(_p(896, 304, 990, 190, 1060, 260, 1100, 220, 1144, 270, 1144, 304), fill=(95, 135, 110))
    d.polygon(_p(975, 208, 990, 190, 1006, 210), fill=(250, 250, 250))
    # a shelf with books
    d.rectangle(_p(1480, 300, 2180, 318), fill=(150, 105, 65))
    x = 1510
    for w, h, c in ((34, 120, (180, 60, 60)), (28, 104, (60, 90, 160)), (40, 130, (70, 140, 90)),
                    (30, 110, (220, 180, 70)), (36, 124, (110, 70, 140)), (26, 98, (60, 60, 60))):
        d.rectangle(_p(x, 300 - h, x + w, 300), fill=c)
        x += w + 4
    d.ellipse(_p(1950, 220, 2050, 300), fill=(90, 160, 170))
    # the desk
    d.rectangle(_p(0, 560, W, H), fill=(176, 124, 80))
    for i in range(0, H - 560, 23):
        y = 560 + i
        d.line(_p(0, y, W, y + 6), fill=(160, 110, 70), width=2 * S)
    d.rectangle(_p(0, 556, W, 566), fill=(140, 96, 60))
    d.rectangle(_p(0, 870, W, H), fill=(120, 80, 50))
    # a notebook and a pencil
    d.polygon(_p(420, 790, 640, 780, 680, 850, 440, 862), fill=(245, 245, 238))
    d.line(_p(470, 806, 630, 800), fill=(170, 190, 230), width=2 * S)
    d.line(_p(476, 822, 640, 815), fill=(170, 190, 230), width=2 * S)
    d.line(_p(700, 840, 820, 800), fill=(240, 190, 40), width=8 * S)
    # a plant in a pot
    for cx, cy, rx, ry in ((290, 470, 60, 26), (350, 450, 70, 28), (320, 420, 50, 24),
                           (260, 430, 46, 22), (380, 490, 60, 24), (300, 500, 70, 26)):
        d.ellipse(_p(cx - rx, cy - ry, cx + rx, cy + ry), fill=(70, 150, 80), outline=(50, 120, 60), width=2 * S)
    d.polygon(_p(255, 520, 385, 520, 370, 640, 270, 640), fill=(200, 110, 70))
    d.rectangle(_p(245, 510, 395, 530), fill=(185, 98, 60))
    # a stack of books
    for i, (c, off) in enumerate((((60, 90, 150), 0), ((190, 70, 60), 14), ((230, 200, 120), 6))):
        y = 640 - i * 30
        d.rectangle(_p(640 + off, y - 28, 840 + off, y), fill=c)
    # a blue mug
    d.rectangle(_p(980, 600, 1080, 700), fill=(50, 90, 170))
    d.ellipse(_p(980, 585, 1080, 615), fill=(40, 70, 140))
    d.ellipse(_p(980, 685, 1080, 715), fill=(50, 90, 170))
    d.ellipse(_p(1062, 620, 1122, 680), outline=(50, 90, 170), width=12 * S)
    # the orange
    cx, cy, r = 1310, 720, 46
    for k in range(r, 0, -2):
        t = k / r
        c = (255, int(150 - 40 * t), int(20 + 10 * (1 - t)))
        off = int((1 - t) * 14)
        d.ellipse(_p(cx - k - off, cy - k - off, cx + k - off, cy + k - off), fill=c)
    d.ellipse(_p(cx - 6, cy - r - 4, cx + 4, cy - r + 6), fill=(90, 70, 40))
    d.ellipse(_p(cx + 2, cy - r - 14, cx + 30, cy - r + 2), fill=(60, 140, 60))
    # a rubber duck
    d.ellipse(_p(2100, 700, 2230, 790), fill=(255, 215, 40))
    d.ellipse(_p(2150, 640, 2220, 710), fill=(255, 215, 40))
    d.polygon(_p(2215, 670, 2255, 680, 2215, 690), fill=(250, 140, 30))
    d.ellipse(_p(2190, 660, 2200, 670), fill=(30, 30, 30))
    _lamp(img, world.get("lamp") or {})
    return img


def _lamp(img: Image.Image, lamp: dict) -> None:
    color = COLORS.get(str(lamp.get("color") or "off"))
    tilt = float(lamp.get("tilt") or 0)       # a nod tips the shade down
    grey, dark, shade = (70, 75, 85), (45, 48, 55), (60, 120, 150)
    bx, by = LAMP_X, 790                      # the base
    ex, ey = bx + 50, 590                     # the elbow
    hx, hy = bx - 120, 500 + tilt * 0.8       # where the arm holds the shade
    theta = math.radians(118 - tilt)          # the shade opens down and to the left
    ax, ay = math.cos(theta), math.sin(theta)
    px, py = -ay, ax                          # across the shade
    mx, my = hx + ax * 120, hy + ay * 120     # the middle of its mouth
    if color:  # a pool of light on the desk, under the shade
        lx = mx + ax * (780 - my) / max(ay, 0.2)
        pool = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(pool).ellipse(_p(lx - 190, 700, lx + 190, 850), fill=color + (120,))
        pool = pool.filter(ImageFilter.GaussianBlur(28 * S))
        img.paste(pool, (0, 0), pool)
    d = ImageDraw.Draw(img)
    d.ellipse(_p(bx - 80, by - 16, bx + 80, by + 22), fill=dark)
    d.line(_p(bx, by, ex, ey), fill=grey, width=14 * S)
    d.line(_p(ex, ey, hx, hy), fill=grey, width=12 * S)
    d.ellipse(_p(ex - 14, ey - 14, ex + 14, ey + 14), fill=dark)
    d.polygon(_p(hx + px * 22, hy + py * 22, mx + px * 75, my + py * 75,
                 mx - px * 75, my - py * 75, hx - px * 22, hy - py * 22), fill=shade)
    d.ellipse(_p(hx - 16, hy - 16, hx + 16, hy + 16), fill=dark)
    d.line(_p(mx + px * 75, my + py * 75, mx - px * 75, my - py * 75),
           fill=color or (225, 225, 215), width=12 * S)
    if color:  # the bulb glowing in the mouth of the shade
        glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        gx, gy = mx + ax * 10, my + ay * 10
        ImageDraw.Draw(glow).ellipse(_p(gx - 70, gy - 45, gx + 70, gy + 45), fill=color + (210,))
        glow = glow.filter(ImageFilter.GaussianBlur(14 * S))
        img.paste(glow, (0, 0), glow)


def _jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def view(pan: float | None = None, world: dict | None = None) -> bytes:
    """A photo from the desk camera, pointing `pan` degrees (left is negative)."""
    world = world or load()
    if pan is None:
        pan = float(world["camera"].get("pan") or 0)
    pan = max(-PAN_LIMIT, min(PAN_LIMIT, pan))
    hour = time.localtime().tm_hour + time.localtime().tm_min / 60
    scene = _scene(world, hour)
    left = (pan + PAN_LIMIT) / (2 * PAN_LIMIT) * (W - VIEW_W)
    crop = scene.crop((int(left * S), 0, int((left + VIEW_W) * S), H * S))
    return _jpeg(crop.resize(OUT, Image.LANCZOS))


def closeup(world: dict | None = None) -> bytes:
    """The lamp, close: what a build check shows."""
    world = world or load()
    scene = _scene(world, 12)
    crop = scene.crop(((LAMP_X - 420) * S, 420 * S, (LAMP_X + 260) * S, H * S))
    return _jpeg(crop.resize((680, 480), Image.LANCZOS))


def pan_words(pan: float) -> str:
    if pan <= -25:
        return "looking at the left of the desk (window and plant)"
    if pan >= 25:
        return "looking at the right of the desk (lamp and duck)"
    if abs(pan) < 8:
        return "looking at the middle of the desk"
    return f"turned {abs(round(pan))}° {'left' if pan < 0 else 'right'} of the middle"
