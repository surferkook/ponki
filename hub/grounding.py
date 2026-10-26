"""The hub's client for the finding service, and results into words (spec 05).

The finding service (SAM 3.1, 127.0.0.1:8201) does the seeing; this module
calls it and turns its answers into the text a soul reads.
"""
from __future__ import annotations

import os

import httpx

SAM_URL = os.environ.get("HOUSE_SAM_URL", "http://127.0.0.1:8201")


class GroundingError(Exception):
    """The finder failed, in plain words."""


def camera_action(manifest: dict) -> str | None:
    """The action that returns an image (`look` preferred), else None."""
    actions = (manifest or {}).get("actions") or {}
    image_actions = sorted(
        name for name, a in actions.items()
        if isinstance(a, dict) and a.get("returns") == "image")
    if "look" in image_actions:
        return "look"
    return image_actions[0] if image_actions else None


async def _post(path: str, payload: dict, timeout: float, what: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{SAM_URL}{path}", json=payload)
    except httpx.HTTPError as exc:
        raise GroundingError(f"the object finder isn't reachable ({exc})")
    if resp.status_code == 503:
        raise GroundingError(
            "the object finder is still starting up or has no model access yet")
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", "")
        except Exception:
            detail = resp.text[:200]
        if path.startswith("/track"):
            raise GroundingError(f"tracking failed ({resp.status_code}: {detail})")
        if path.startswith("/media"):
            raise GroundingError(f"making the clip failed ({detail})")
        raise GroundingError(f"the object finder failed ({resp.status_code}: {detail})")
    return resp.json()


async def segment(image_b64: str, concepts: list[str], threshold: float = 0.4) -> dict:
    return await _post("/locate", {"image": image_b64, "concepts": concepts,
                                   "threshold": threshold}, 90, "locate")


async def track(video_b64: str, concepts: list[str], times=None) -> dict:
    return await _post("/track", {"video": video_b64, "concepts": concepts,
                                  "times": times or []}, 600, "track")


async def media(job: str) -> dict:
    return await _post(f"/media/{job}", {}, 600, "media")


async def health() -> dict:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{SAM_URL}/health")
        data = resp.json()
        return {"state": data.get("state", "error"), "model": data.get("model"),
                "error": data.get("error")}
    except Exception:
        return {"state": "down", "model": None, "error": "unreachable"}


# -- words -------------------------------------------------------------------------

def _position_words(x: float, y: float) -> str:
    horiz = "left" if x < 0.33 else "right" if x > 0.67 else "centre"
    vert = "top" if y < 0.33 else "bottom" if y > 0.67 else "middle"
    if horiz == "centre" and vert == "middle":
        return "dead centre"
    return f"{vert} {horiz}"


def describe(found: dict, ms: float, concepts: list[str] | None = None) -> str:
    """One block per concept, then offsets between the best matches."""
    concepts = concepts or list(found.keys())
    blocks: list[str] = []
    best: list[tuple[str, dict]] = []
    for concept in concepts:
        items = found.get(concept) or []
        if not items:
            blocks.append(
                f"No '{concept}' in view (searched in {ms / 1000:.1f}s). "
                "The annotated photo is attached. If you can see it there, "
                "locate it again by what it looks like (its colour and what it is).")
            continue
        best.append((concept, items[0]))
        lines = [f"Found {len(items)} × '{concept}' (numbered on the attached "
                 f"photo; x/y are 0–1 from the top-left; centre of view = 0.5, 0.5):"]
        for item in items:
            cx, cy = item["center"]
            lines.append(
                f"#{item['n']}: {_position_words(cx, cy)} at x={cx:.2f}, y={cy:.2f}; "
                f"covers {item['area'] * 100:.1f}% of the view; "
                f"confidence {item['score']:.2f}")
        blocks.append("\n".join(lines))
    for i in range(len(best)):
        for j in range(i + 1, len(best)):
            to_concept, a = best[i]
            from_concept, b = best[j]
            dx = a["center"][0] - b["center"][0]
            dy = a["center"][1] - b["center"][1]
            horiz = f"{abs(dx):.2f} to the {'right' if dx >= 0 else 'left'}"
            vert = f"{abs(dy):.2f} {'lower' if dy >= 0 else 'higher'}"
            blocks.append(
                f"From #{b['n']} ({from_concept}) to #{a['n']} ({to_concept}): "
                f"{horiz} and {vert} in the picture (dx={dx:+.2f}, dy={dy:+.2f}).")
    return "\n".join(blocks)


def _shape_words(peak: dict) -> str:
    parts = []
    wider, taller = peak.get("wider", 0), peak.get("taller", 0)
    if abs(wider) >= 0.05:
        parts.append(f"{abs(wider) * 100:.0f}% {'wider' if wider > 0 else 'narrower'}")
    if abs(taller) >= 0.05:
        parts.append(f"{abs(taller) * 100:.0f}% {'taller' if taller > 0 else 'shorter'}")
    return " and ".join(parts) if parts else "shifted"


def describe_track(what: str, res: dict) -> str:
    objects = res.get("objects") or []
    duration = res.get("duration", 0)
    frames = res.get("frames", 0)
    keytimes = ", ".join(f"{t:.1f} s" for t in res.get("keytimes") or [])
    concepts = sorted({o["concept"] for o in objects}) or [what]
    named = "' and '".join(concepts)
    first = (f"Watched for {duration:.1f} s ({frames} frames). "
             f"The attached strip shows {len(res.get('keytimes') or [])} key moments "
             f"({keytimes}), with each tracked '{named}' marked by number in the "
             f"same colour throughout.")
    if not objects:
        return f"{first}\nNo '{what}' was found in the clip."
    lines = [first]
    for obj in objects:
        head = (f"#{obj['n']} ({obj['concept']}): in view "
                f"{obj['seen'][0]:.1f}-{obj['seen'][1]:.1f} s; ")
        if obj["moved"]:
            active = obj.get("active") or obj["seen"]
            line = (f"{head}MOVED between {active[0]:.1f} and {active[1]:.1f} s, "
                    f"most at {obj['peak']['t']:.1f} s "
                    f"(its outline {_shape_words(obj['peak'])}); ")
            line += "back to how it started." if obj["back"] else "not back to how it started."
        else:
            line = f"{head}stayed still."
        lines.append(line)
    if res.get("job"):
        lines.append(
            f"A GIF of #{res.get('gif_of')} cut out through the movement and an "
            "annotated video of the whole clip (everything numbered) are being "
            "made; they'll appear in the app in half a minute or so, ready to "
            "save and share.")
    lines.append("The numbers say where and how much; look at the strip to say "
                 "what happened (a kick, a wave…).")
    return "\n".join(lines)
