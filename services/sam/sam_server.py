"""The finding service: SAM 3.1 on Meta's Model API (spec 05).

    uvicorn sam_server:app --host 127.0.0.1 --port 8201

POST /locate finds things in one photo; POST /track follows them through a
clip and returns a key-moment strip; POST /media/{job} then makes the
cut-out GIF and the annotated video. One SAM request per concept, in
parallel, to the Responses endpoint. Masks are decoded by a long-lived
Node worker (@meta-sam/parser), with the Python package as fallback.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException
from PIL import Image, ImageDraw

ENDPOINT = os.environ.get("HOUSE_SAM_ENDPOINT", "https://api.meta.ai/v1/responses")
MODEL = os.environ.get("HOUSE_SAM_MODEL", "sam-3.1")
PALETTE = [(230, 25, 75), (66, 170, 245), (245, 220, 30),
           (60, 180, 75), (245, 130, 245), (255, 133, 27)]
JS_DIR = Path(__file__).parent / "js"


class SamError(Exception):
    """SAM or Meta failed, in plain words."""


# -- reading SAM's answer --------------------------------------------------------

_RECORD_START = re.compile(r"(?:^|,|>)\s*([0-9]+)(?=<\|)")
_TOKEN = re.compile(r"<\|([A-Za-z][A-Za-z0-9_.-]*)(?:;([^|]*))?\|>")
_FRAME_HEAD = re.compile(r"^<([0-9]+)f(?:;[^>]*)?>(.*)$")


def _fields(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (raw or "").split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            out[key.strip()] = value
    return out


def parse_records(text: str, video: bool = False) -> list[dict]:
    """Records from SAM's segmentation grammar. Raises SamError if garbled."""
    records: list[dict] = []
    for line in text.splitlines():
        frame = None
        head = _FRAME_HEAD.match(line.strip())
        if head:  # even image answers carry a <0f> frame header
            frame = int(head.group(1))
            line = head.group(2)
        elif video:
            continue
        starts = list(_RECORD_START.finditer(line))
        for i, start in enumerate(starts):
            end = starts[i + 1].start() if i + 1 < len(starts) else len(line)
            chunk = line[start.start():end]
            tokens = {m.group(1): _fields(m.group(2) or "")
                      for m in _TOKEN.finditer(chunk)}
            if "box" not in tokens or "mask" not in tokens:
                continue
            box_f = tokens["box"]
            mask_f = tokens["mask"]
            try:
                box = [float(box_f["x1"]), float(box_f["y1"]),
                       float(box_f["x2"]), float(box_f["y2"])]
            except (KeyError, ValueError):
                continue
            data = mask_f.get("data", "")
            parts = data.split(",", 2)
            if len(parts) != 3 or not parts[2]:
                continue
            try:
                height, width = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            marker = parts[2][0]
            if marker not in ("~", "!"):
                continue
            conf_raw = box_f.get("c", mask_f.get("c"))
            try:
                conf = float(conf_raw) if conf_raw is not None else 1.0
            except ValueError:
                conf = 1.0
            records.append({
                "id": start.group(1), "frame": frame, "box": box, "conf": conf,
                "mask": {"encoding": "lossless" if marker == "~" else "one_bit",
                         "payload": parts[2][1:], "marker": marker,
                         "width": width, "height": height},
            })
    if text.strip() and not records:
        raise SamError("SAM's answer didn't match the documented format")
    return records


# -- mask geometry -----------------------------------------------------------------

def fit_mask(mask: np.ndarray, box_w: int, box_h: int) -> np.ndarray:
    """Fit a decoded mask to its box (SAM scales big masks down)."""
    if box_w <= 0 or box_h <= 0:
        return mask
    if abs(mask.shape[1] - box_w) > 2 or abs(mask.shape[0] - box_h) > 2:
        img = Image.fromarray((mask > 0).astype(np.uint8) * 255)
        img = img.resize((box_w, box_h), Image.NEAREST)
        return (np.asarray(img) > 0).astype(np.uint8)
    return (mask > 0).astype(np.uint8)


def placed_mask(mask: np.ndarray, box, frame_w: int, frame_h: int):
    """(fitted mask, x0, y0, clipped views) placed at the box, clipped to frame."""
    x1, y1, x2, y2 = box
    box_w = int(round(x2)) - int(x1) + 1
    box_h = int(round(y2)) - int(y1) + 1
    fitted = fit_mask(mask, box_w, box_h)
    x0, y0 = int(x1), int(y1)
    fx0, fy0 = max(0, x0), max(0, y0)
    fx1, fy1 = min(frame_w, x0 + fitted.shape[1]), min(frame_h, y0 + fitted.shape[0])
    if fx1 <= fx0 or fy1 <= fy0:
        return fitted, x0, y0, None
    sub = fitted[fy0 - y0:fy1 - y0, fx0 - x0:fx1 - x0]
    return fitted, x0, y0, (fx0, fy0, fx1, fy1, sub)


def mask_centroid_area(mask: np.ndarray, box, frame_w: int, frame_h: int):
    """(centre normalised, area fraction) from the fitted, placed mask."""
    fitted, x0, y0, view = placed_mask(mask, box, frame_w, frame_h)
    if view is None:
        return (0.0, 0.0), 0.0
    fx0, fy0, fx1, fy1, sub = view
    ys, xs = np.nonzero(sub)
    if len(xs) == 0:
        return (0.0, 0.0), 0.0
    cx = (xs.mean() + fx0) / frame_w
    cy = (ys.mean() + fy0) / frame_h
    return (round(float(cx), 3), round(float(cy), 3)), \
        round(float(len(xs)) / (frame_w * frame_h), 4)


def tint_region(base: np.ndarray, view, colour) -> None:
    if view is None:
        return
    fx0, fy0, fx1, fy1, sub = view
    region = base[fy0:fy1, fx0:fx1].astype(np.float32)
    tint = np.array(colour, dtype=np.float32)
    mask3 = (sub > 0)[..., None]
    base[fy0:fy1, fx0:fx1] = np.where(
        mask3, region * 0.45 + tint * 0.55, region).astype(np.uint8)


# -- decoding masks: Node worker, Python fallback --------------------------------

class MaskDecoder:
    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._seq = 0

    def _worker(self) -> subprocess.Popen | None:
        script = JS_DIR / "decode.mjs"
        if not script.exists():
            return None
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        try:
            self._proc = subprocess.Popen(
                ["node", str(script)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, text=True, cwd=str(JS_DIR))
        except (OSError, ValueError):
            self._proc = None
        return self._proc

    def decode(self, masks: list[dict]) -> list[np.ndarray]:
        """One raster (uint8 HxW of 0/1) per mask dict."""
        if not masks:
            return []
        with self._lock:
            rasters = self._decode_node(masks)
        if rasters is not None and all(r is not None for r in rasters):
            return rasters  # type: ignore[return-value]
        # fallback: the Python package (about 20x slower)
        return [self._decode_python(mask) for mask in masks]

    def _decode_node(self, masks: list[dict]) -> list[np.ndarray | None] | None:
        proc = self._worker()
        if proc is None or proc.stdin is None or proc.stdout is None:
            return None
        try:
            wanted: dict[int, dict] = {}
            for mask in masks:
                self._seq += 1
                wanted[self._seq] = mask
                proc.stdin.write(json.dumps({
                    "id": self._seq,
                    "encoding": mask["encoding"],
                    "payload": mask.get("marker", "") + mask["payload"],
                    "width": mask["width"], "height": mask["height"]}) + "\n")
            proc.stdin.flush()
            got: dict[int, np.ndarray] = {}
            for _ in masks:
                line = proc.stdout.readline()
                if not line:
                    raise OSError("mask worker stopped")
                reply = json.loads(line)
                if reply.get("raster"):
                    mask = wanted[reply["id"]]
                    raw = base64.b64decode(reply["raster"])
                    got[reply["id"]] = np.frombuffer(raw, dtype=np.uint8).reshape(
                        mask["height"], mask["width"]).copy()
            return [got.get(seq) for seq in wanted]
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            self._proc = None
            return None

    @staticmethod
    def _decode_python(mask: dict) -> np.ndarray:
        from meta_sam_parser import SegmentationMask, decode_mask_to_raster

        raster = decode_mask_to_raster(SegmentationMask(
            encoding=mask["encoding"],
            payload=mask.get("marker", "") + mask["payload"],
            width=int(mask["width"]), height=int(mask["height"])))
        return np.frombuffer(bytes(raster), dtype=np.uint8).reshape(
            int(mask["height"]), int(mask["width"])).copy()


DECODER = MaskDecoder()


# -- calling Meta ------------------------------------------------------------------

async def call_sam(content: list[dict], timeout: float) -> str:
    key = os.environ.get("META_API_KEY", "")
    if not key:
        raise SamError("the finding service has no META_API_KEY")
    payload = {
        "model": MODEL,
        "metadata": {"include_confidence": "true", "mask_encoding": "one_bit"},
        "input": [{"type": "message", "role": "user", "content": content}],
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                ENDPOINT, json=payload,
                headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        raise SamError(f"Meta API unreachable: {exc}")
    if resp.status_code >= 400:
        raise SamError(f"Meta API {resp.status_code}: "
                       f"{resp.content[:300].decode('utf-8', 'replace')}")
    try:
        data = resp.json()
    except ValueError:
        raise SamError("Meta's answer wasn't JSON")
    texts: list[str] = []
    for item in data.get("output") or []:
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("text"):
                texts.append(str(part["text"]))
    return "".join(texts)


# -- motion (pure, tested) -----------------------------------------------------------

def compute_motion(frames: list[tuple[int, list, float]]) -> dict:
    """frames: sorted [(frame, box pixels, t)]. Median start, activity, peak."""
    frames = sorted(frames)
    boxes = [box for _, box, _ in frames]
    first = boxes[:5]
    start_w = float(np.median([b[2] - b[0] for b in first]))
    start_h = float(np.median([b[3] - b[1] for b in first]))
    start_cx = float(np.median([(b[0] + b[2]) / 2 for b in first]))
    start_cy = float(np.median([(b[1] + b[3]) / 2 for b in first]))
    larger = max(start_w, start_h, 1.0)
    activity: list[tuple[int, float, float]] = []  # frame, t, activity
    peak = (frames[0][0], frames[0][2], 0.0, 0.0, 0.0)
    for frame, box, t in frames:
        w, h = box[2] - box[0], box[3] - box[1]
        wider = w / start_w - 1 if start_w else 0.0
        taller = h / start_h - 1 if start_h else 0.0
        shift = (abs((box[0] + box[2]) / 2 - start_cx)
                 + abs((box[1] + box[3]) / 2 - start_cy)) / larger
        act = max(abs(wider), abs(taller), shift)
        activity.append((frame, t, act))
        if act > peak[2]:
            peak = (frame, t, act, wider, taller)
    active = [(f, t) for f, t, a in activity if a > 0.05]
    return {
        "moved": peak[2] > 0.10,
        "peak": {"t": round(peak[1], 1), "frame": peak[0],
                 "change": round(peak[2], 3),
                 "wider": round(peak[3], 3), "taller": round(peak[4], 3)},
        "active": [round(active[0][1], 1), round(active[-1][1], 1)] if active else None,
        "active_frames": [active[0][0], active[-1][0]] if active else None,
        "back": bool(activity) and activity[-1][2] < 0.05,
        "seen": [round(frames[0][2], 1), round(frames[-1][2], 1)],
        "_activity": activity,
    }


def choose_keyframes(objects: list[dict], times: list[float]) -> list[int]:
    """Frame indices for the strip: around the mover, or spread if still."""
    if not times:
        return []
    movers = [o for o in objects if o["moved"]]
    if movers:
        mover = max(movers, key=lambda o: o["peak"]["change"])
        active = mover.get("active") or mover["seen"]
        peak_t = mover["peak"]["t"]
        wanted = [max(0.0, active[0] - 0.5), peak_t,
                  (peak_t + active[1]) / 2, active[1] + 0.5]
        picks: list[int] = []
        for t in wanted:
            idx = min(range(len(times)), key=lambda i: abs(times[i] - t))
            if idx not in picks:
                picks.append(idx)
        # halfway pick is peak's frame +1 per spec when that reads better
        return picks[:4]
    if len(times) <= 3:
        return list(range(len(times)))
    return [0, len(times) // 2, len(times) - 1]


# -- video helpers (PyAV; only wanted frames kept) --------------------------------

def _open_video(video_b64: str):
    import av

    raw = base64.b64decode(video_b64)
    return av.open(io.BytesIO(raw)), raw


def probe_video(video_b64: str) -> tuple[list[float], float, float]:
    """(frame times, fps, duration). Frames are decoded and discarded."""
    container, _ = _open_video(video_b64)
    try:
        stream = container.streams.video[0]
        fps = float(stream.average_rate) if stream.average_rate else 25.0
        times: list[float] = []
        for i, frame in enumerate(container.decode(stream)):
            t = frame.time
            times.append(float(t) if t is not None else i / (fps or 25.0))
        duration = times[-1] if times else 0.0
        return times, fps, duration
    finally:
        container.close()


def decode_frames(video_b64: str, wanted: set[int], width: int | None = None):
    """{frame index: PIL image} for wanted frames only."""
    container, _ = _open_video(video_b64)
    try:
        stream = container.streams.video[0]
        out: dict[int, Image.Image] = {}
        for i, frame in enumerate(container.decode(stream)):
            if i in wanted:
                img = frame.to_image()
                if width and img.width > width:
                    img = img.resize((width, int(img.height * width / img.width)),
                                     Image.LANCZOS)
                out[i] = img
                if len(out) == len(wanted):
                    break
        return out
    finally:
        container.close()


def draw_object(img: Image.Image, record: dict, raster: np.ndarray,
                colour, number: int, scale: float = 1.0) -> None:
    arr = np.asarray(img).copy()
    frame_h, frame_w = arr.shape[:2]
    box = [v * scale for v in record["box"]]
    fitted, x0, y0, view = placed_mask(raster, box, frame_w, frame_h)
    tint_region(arr, view, colour)
    out = Image.fromarray(arr)
    draw = ImageDraw.Draw(out)
    cx = (box[0] + box[2]) / 2
    cy = (box[1] + box[3]) / 2
    r = 11
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=colour, outline=(0, 0, 0))
    draw.text((cx, cy), str(number), fill=(0, 0, 0), anchor="mm")
    img.paste(out, (0, 0))


# -- the service ---------------------------------------------------------------------

app = FastAPI(title="ponkihouse finding (SAM 3.1)")
JOBS: dict[str, dict] = {}
JOB_ORDER: list[str] = []


@app.get("/health")
async def health():
    if not os.environ.get("META_API_KEY"):
        return {"state": "error", "model": MODEL,
                "error": "META_API_KEY is not set for the finding service"}
    return {"state": "ready", "model": MODEL, "error": None}


def _clean_concepts(raw, limit: int) -> list[str]:
    out = []
    for item in raw or []:
        text = str(item).strip()
        if text:
            out.append(text)
    return out[:limit]


def _jpeg_b64(img: Image.Image, quality: int) -> str:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii")


@app.post("/locate")
async def locate(payload: dict):
    if not os.environ.get("META_API_KEY"):
        raise HTTPException(503, "the finding service has no META_API_KEY")
    concepts = _clean_concepts(payload.get("concepts"), 6)
    if not concepts:
        raise HTTPException(400, "say what to look for")
    threshold = float(payload.get("threshold", 0.4))
    started = time.time()
    try:
        img = Image.open(io.BytesIO(base64.b64decode(payload.get("image") or "")))
        img = img.convert("RGB")
    except Exception:
        raise HTTPException(400, "that photo couldn't be read")
    if max(img.size) > 4096:
        img.thumbnail((4096, 4096), Image.LANCZOS)
    frame_w, frame_h = img.size
    send_b64 = _jpeg_b64(img, 92)

    async def one(concept: str) -> list[dict]:
        text = await call_sam([
            {"type": "input_text", "text": concept},
            {"type": "input_image",
             "image_url": f"data:image/jpeg;base64,{send_b64}"}], timeout=60)
        return [r for r in parse_records(text) if r["conf"] >= threshold]

    try:
        per_concept = await asyncio.gather(*(one(c) for c in concepts))
    except SamError as exc:
        raise HTTPException(502, str(exc))
    # decode all masks in one batch (sorted by score per concept first)
    ordered: list[tuple[str, dict]] = []
    for concept, records in zip(concepts, per_concept):
        for record in sorted(records, key=lambda r: -r["conf"]):
            ordered.append((concept, record))
    rasters = await asyncio.to_thread(
        DECODER.decode, [r["mask"] for _, r in ordered]) if ordered else []
    found: dict[str, list[dict]] = {c: [] for c in concepts}
    overlay = np.asarray(img).copy()
    markers: list[dict] = []
    number = 0
    for (concept, record), raster in zip(ordered, rasters):
        if raster is None or int(raster.sum()) == 0:
            continue  # drop empty masks
        (cx, cy), area = mask_centroid_area(raster, record["box"], frame_w, frame_h)
        if area == 0:
            continue
        number += 1
        colour = PALETTE[(number - 1) % len(PALETTE)]
        fitted, x0, y0, view = placed_mask(raster, record["box"], frame_w, frame_h)
        tint_region(overlay, view, colour)
        pixels = int((view[4] > 0).sum()) if view else 0
        radius = min(frame_w / 60, max(7, (pixels ** 0.5) / 2.5))
        found[concept].append({
            "n": number, "score": round(record["conf"], 3),
            "box": [round(record["box"][0] / frame_w, 3),
                    round(record["box"][1] / frame_h, 3),
                    round(record["box"][2] / frame_w, 3),
                    round(record["box"][3] / frame_h, 3)],
            "center": [cx, cy], "area": area})
        markers.append({"n": number, "concept": concept, "colour": colour,
                        "x": cx * frame_w, "y": cy * frame_h, "radius": radius})
    overlay_img = Image.fromarray(overlay)
    draw = ImageDraw.Draw(overlay_img)
    for marker in markers:
        px, py, radius = marker["x"], marker["y"], marker["radius"]
        draw.ellipse([px - radius, py - radius, px + radius, py + radius],
                     fill=marker["colour"], outline=(0, 0, 0), width=2)
        draw.text((px, py), str(marker["n"]), fill=(0, 0, 0), anchor="mm")
        if len(concepts) > 1:
            concept = marker["concept"]
            short = concept if len(concept) <= 18 else concept[:18]
            draw.text((px + radius + 4, py), f"{marker['n']} {short}",
                      fill=(0, 0, 0), anchor="lm", stroke_width=2,
                      stroke_fill=(255, 255, 255))
    return {"width": frame_w, "height": frame_h,
            "ms": int((time.time() - started) * 1000),
            "found": found, "overlay": _jpeg_b64(overlay_img, 90)}


@app.post("/track")
async def track(payload: dict):
    if not os.environ.get("META_API_KEY"):
        raise HTTPException(503, "the finding service has no META_API_KEY")
    concepts = _clean_concepts(payload.get("concepts"), 3)
    if not concepts:
        raise HTTPException(400, "say what to watch")
    threshold = float(payload.get("threshold", 0.4))
    video_b64 = payload.get("video") or ""
    started = time.time()
    try:
        times, fps, duration = await asyncio.to_thread(probe_video, video_b64)
    except Exception as exc:
        raise HTTPException(400, f"that clip couldn't be read: {exc}")
    given = payload.get("times")
    if isinstance(given, list) and len(given) == len(times) and times:
        base = float(given[0])
        times = [float(t) - base for t in given]
        duration = times[-1] if times else duration  # real arrival times win

    async def one(concept: str) -> list[dict]:
        text = await call_sam([
            {"type": "input_text", "text": concept},
            {"type": "input_video",
             "video_url": f"data:video/mp4;base64,{video_b64}"}], timeout=300)
        return [r for r in parse_records(text, video=True) if r["conf"] >= threshold]

    try:
        per_concept = await asyncio.gather(*(one(c) for c in concepts))
    except SamError as exc:
        raise HTTPException(502, str(exc))

    def frame_time(idx: int) -> float:
        return times[idx] if 0 <= idx < len(times) else idx / (fps or 25.0)

    tracks: list[dict] = []  # {concept, id, frames: {frame: record}}
    for concept, records in zip(concepts, per_concept):
        by_id: dict[str, dict[int, dict]] = {}
        for record in records:
            if record["frame"] is not None:
                by_id.setdefault(record["id"], {})[record["frame"]] = record
        for oid, frames_map in by_id.items():
            if len(frames_map) >= 3:  # drop flickers
                tracks.append({"concept": concept, "id": oid,
                               "frames": frames_map})
    tracks.sort(key=lambda t: -len(t["frames"]))
    objects: list[dict] = []
    for i, tr in enumerate(tracks, 1):
        motion = compute_motion(
            [(f, r["box"], frame_time(f)) for f, r in tr["frames"].items()])
        motion.pop("_activity", None)
        objects.append({"n": i, "concept": tr["concept"], "id": tr["id"],
                        "frames": len(tr["frames"]), **motion})
        tr["n"] = i
        tr["motion"] = motion
    key_idx = choose_keyframes(objects, times)
    keytimes = [round(frame_time(i), 1) for i in key_idx]
    strip = ""
    if key_idx:
        images = await asyncio.to_thread(decode_frames, video_b64, set(key_idx))
        # decode only the masks drawn on the strip
        strip_masks: list[dict] = []
        strip_refs: list[tuple[int, dict]] = []  # (object n, record)
        for idx in key_idx:
            for tr in tracks:
                record = tr["frames"].get(idx)
                if record is not None:
                    strip_masks.append(record["mask"])
                    strip_refs.append((tr["n"], record))
        rasters = await asyncio.to_thread(DECODER.decode, strip_masks) \
            if strip_masks else []
        raster_by: dict[tuple[int, int], np.ndarray] = {}
        for (n, record), raster in zip(strip_refs, rasters):
            if raster is not None:
                raster_by[(n, record["frame"])] = raster
        tiles: list[Image.Image] = []
        for pos, idx in enumerate(key_idx):
            img = images.get(idx)
            if img is None:
                continue
            img = img.copy()
            if img.width > 640:
                img = img.resize((640, int(img.height * 640 / img.width)),
                                 Image.LANCZOS)
            scale = img.width / (images[idx].width or img.width)
            for tr in tracks:
                record = tr["frames"].get(idx)
                raster = raster_by.get((tr["n"], idx))
                if record is not None and raster is not None:
                    draw_object(img, record, raster,
                                PALETTE[(tr["n"] - 1) % len(PALETTE)],
                                tr["n"], scale=scale)
            label_draw = ImageDraw.Draw(img)
            label_draw.text((8, 8), f"{frame_time(idx):.1f} s", fill=(255, 255, 0),
                            stroke_width=2, stroke_fill=(0, 0, 0))
            tiles.append(img)
        if tiles:
            tile_w = max(t.width for t in tiles)
            tile_h = max(t.height for t in tiles)
            cols = 2 if len(tiles) == 4 else len(tiles)
            rows = (len(tiles) + cols - 1) // cols
            sheet = Image.new("RGB", (cols * tile_w + 6 * (cols + 1),
                                      rows * tile_h + 6 * (rows + 1)), "white")
            for pos, tile in enumerate(tiles):
                x = 6 + (pos % cols) * (tile_w + 6)
                y = 6 + (pos // cols) * (tile_h + 6)
                sheet.paste(tile, (x, y))
            strip = _jpeg_b64(sheet, 88)
    job = None
    gif_of = None
    movers = [o for o in objects if o["moved"]]
    if (payload.get("gif", True) or payload.get("video_out", True)) and objects:
        job = uuid.uuid4().hex[:12]
        gif_of = (max(movers, key=lambda o: o["peak"]["change"])["n"]
                  if movers else objects[0]["n"])
        JOBS[job] = {"video": video_b64, "tracks": [
            {"n": tr["n"], "concept": tr["concept"], "id": tr["id"],
             "frames": tr["frames"], "motion": tr.get("motion", {})}
            for tr in tracks], "times": times, "gif_of": gif_of, "fps": fps}
        JOB_ORDER.append(job)
        while len(JOB_ORDER) > 3:
            JOBS.pop(JOB_ORDER.pop(0), None)
    return {"job": job, "gif_of": gif_of, "duration": round(duration, 1),
            "frames": len(times), "fps": round(fps, 2), "objects": objects,
            "keytimes": keytimes, "strip": strip,
            "ms": int((time.time() - started) * 1000)}


@app.post("/media/{job}")
async def media(job: str):
    entry = JOBS.pop(job, None)  # one-shot: the job is consumed
    if entry is None:
        raise HTTPException(404, "that clip isn't kept anymore")
    if job in JOB_ORDER:
        JOB_ORDER.remove(job)
    started = time.time()
    try:
        gif_b64, video_b64_out = await asyncio.to_thread(_make_media, entry)
    except Exception as exc:
        raise HTTPException(502, f"making the clip failed: {exc}")
    return {"gif": gif_b64, "gif_of": entry["gif_of"], "video": video_b64_out,
            "ms": int((time.time() - started) * 1000)}


def _make_media(entry: dict) -> tuple[str, str]:
    from PIL import ImageFilter

    video_b64, times, tracks = entry["video"], entry["times"], entry["tracks"]
    by_n = {tr["n"]: tr for tr in tracks}
    gif_of = entry["gif_of"]
    mover = by_n.get(gif_of) or tracks[0]
    motion = mover.get("motion") or {}
    active = motion.get("active") or motion.get("seen") or [0.0, 0.0]
    lo, hi = max(0.0, active[0] - 0.5), active[1] + 0.5
    # GIF frames: active window, ~12 fps by real time, at most 90
    picks: list[int] = []
    last = -1e9
    for idx, t in enumerate(times):
        if lo <= t <= hi and t - last >= 1 / 12:
            picks.append(idx)
            last = t
    picks = picks[:90]
    gif_b64 = ""
    if picks:
        images = decode_frames(video_b64, set(picks))
        boxes = [mover["frames"][i]["box"] for i in picks if i in mover["frames"]]
        if boxes and images:
            x0 = min(b[0] for b in boxes)
            y0 = min(b[1] for b in boxes)
            x1 = max(b[2] for b in boxes)
            y1 = max(b[3] for b in boxes)
            margin_x, margin_y = (x1 - x0) * 0.12, (y1 - y0) * 0.12
            sample = next(iter(images.values()))
            crop = (max(0, int(x0 - margin_x)), max(0, int(y0 - margin_y)),
                    min(sample.width, int(x1 + margin_x)),
                    min(sample.height, int(y1 + margin_y)))
            masks = [mover["frames"][i]["mask"] for i in picks
                     if i in mover["frames"] and i in images]
            frame_ids = [i for i in picks if i in mover["frames"] and i in images]
            rasters = DECODER.decode(masks) if masks else []
            raster_by = dict(zip(frame_ids, rasters))
            frames_out: list[Image.Image] = []
            durations: list[int] = []
            for pos, idx in enumerate(picks):
                base = images.get(idx)
                if base is None:
                    continue
                frame_img = Image.new("RGB", base.size, "white")
                raster = raster_by.get(idx)
                record = mover["frames"].get(idx)
                if raster is not None and record is not None:
                    fitted, fx0, fy0, view = placed_mask(
                        raster, record["box"], base.width, base.height)
                    if view is not None:
                        vx0, vy0, vx1, vy1, sub = view
                        mask_img = Image.new("L", base.size, 0)
                        mask_arr = np.asarray(mask_img).copy()
                        mask_arr[vy0:vy1, vx0:vx1] = (sub > 0).astype(np.uint8) * 255
                        mask_img = Image.fromarray(mask_arr).filter(
                            ImageFilter.GaussianBlur(1.2))
                        frame_img.paste(base, (0, 0), mask_img)
                frame_img = frame_img.crop(crop)
                if frame_img.width > 480:
                    frame_img = frame_img.resize(
                        (480, int(frame_img.height * 480 / frame_img.width)),
                        Image.LANCZOS)
                frames_out.append(frame_img)
                nxt = times[picks[pos + 1]] if pos + 1 < len(picks) else times[idx] + 1 / 12
                durations.append(max(40, int((nxt - times[idx]) * 1000)))
            if frames_out:
                buf = io.BytesIO()
                frames_out[0].save(buf, format="GIF", save_all=True,
                                   append_images=frames_out[1:],
                                   duration=durations, loop=0)
                gif_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    video_out = _annotated_video(video_b64, times, tracks)
    return gif_b64, video_out


def _annotated_video(video_b64: str, times: list[float], tracks: list[dict]) -> str:
    import av
    from fractions import Fraction

    container, _ = _open_video(video_b64)
    import tempfile

    tmp_file = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp_file.close()
    tmp = tmp_file.name
    try:
        stream = container.streams.video[0]
        src_w = stream.codec_context.width or 1280
        out = av.open(tmp, mode="w", format="mp4",
                      options={"movflags": "+faststart"})
        try:
            out_stream = out.add_stream("libx264", rate=25)
            out_stream.width = 1280
            out_stream.height = max(2, int(720 * 1280 / max(src_w, 1))) & ~1
            out_stream.pix_fmt = "yuv420p"
            out_stream.options = {"crf": "23", "preset": "veryfast"}
            out_stream.time_base = Fraction(1, 1000)
            # masks for drawn frames are decoded lazily, in small batches
            pending: dict[int, list[tuple[dict, dict]]] = {}
            for tr in tracks:
                for idx, record in tr["frames"].items():
                    pending.setdefault(idx, []).append((tr, record))
            raster_cache: dict[tuple[int, int], np.ndarray] = {}
            last_pts = -1
            idx = -1
            for frame in container.decode(stream):
                idx += 1
                img = frame.to_image()
                scale = 1280 / img.width if img.width else 1.0
                if img.width != 1280:
                    height = max(2, int(img.height * scale)) & ~1
                    img = img.resize((1280, height), Image.LANCZOS)
                    if out_stream.height != height:
                        out_stream.height = height
                present = pending.get(idx) or []
                if present:
                    masks = [record["mask"] for _, record in present
                             if (record["frame"] or idx) is not None]
                    todo = [(tr, record) for tr, record in present
                            if (tr["n"], idx) not in raster_cache]
                    if todo:
                        decoded = DECODER.decode([r["mask"] for _, r in todo])
                        for (tr, record), raster in zip(todo, decoded):
                            if raster is not None:
                                raster_cache[(tr["n"], idx)] = raster
                    counts: dict[str, int] = {}
                    for tr, record in present:
                        counts[tr["concept"]] = counts.get(tr["concept"], 0) + 1
                        raster = raster_cache.get((tr["n"], idx))
                        if raster is not None:
                            draw_object(img, record, raster,
                                        PALETTE[(tr["n"] - 1) % len(PALETTE)],
                                        tr["n"], scale=scale)
                    t = times[idx] if idx < len(times) else idx / 25.0
                    parts = " · ".join(f"{c} {name}" for name, c in counts.items())
                    ImageDraw.Draw(img).text(
                        (8, 8), f"{t:.1f} s · {parts}", fill=(255, 255, 0),
                        stroke_width=2, stroke_fill=(0, 0, 0))
                video_frame = av.VideoFrame.from_image(img)
                t = times[idx] if idx < len(times) else idx / 25.0
                # real times can repeat (buffered bursts): always increasing
                video_frame.pts = max(int(t * 1000), last_pts + 1)
                last_pts = video_frame.pts
                video_frame.time_base = Fraction(1, 1000)
                for packet in out_stream.encode(video_frame):
                    out.mux(packet)
            for packet in out_stream.encode():
                out.mux(packet)
        finally:
            out.close()
    finally:
        container.close()
    data = Path(tmp).read_bytes()
    Path(tmp).unlink(missing_ok=True)
    return base64.b64encode(data).decode("ascii")

