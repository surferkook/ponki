"""The camera driver: an ONVIF camera as a Ponki body (spec 05).

Run: python onvif_body.py <robot.json>
The config's `device` part says where the camera is and how its head moves;
the camera account lives only in the workshop's data/secrets/, never here.

The hard part is a photo that is really *now*: the Stream below keeps only
the newest frame and reopens itself when frames stall (see spec 05).
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "sdk"))
sys.path.insert(0, str(Path(__file__).parent))

from body import Body, Image  # noqa: E402
from onvif_client import Camera, OnvifError, login_at  # noqa: E402

STALL = 3.0
OPEN_WAIT = 12.0


# -- head geometry (pure, tested) ----------------------------------------------

def degrees_to_units(degrees: float, range_degrees: float) -> float:
    return 2.0 * float(degrees) / float(range_degrees)


def move_units(pan_deg: float, tilt_deg: float, device: dict,
               last_dir: dict) -> tuple[float, float]:
    """Physical units for a relative move, backlash only on reversal."""
    pan_range = float(device.get("pan_degrees") or 360)
    tilt_range = float(device.get("tilt_degrees") or 90)
    pan_sign = float(device.get("pan_sign") or 1)
    backlash = float(device.get("backlash") or 0)
    ux = degrees_to_units(pan_deg, pan_range) * pan_sign
    uy = degrees_to_units(tilt_deg, tilt_range)
    if ux != 0:
        direction = 1 if ux > 0 else -1
        if last_dir.get("pan") not in (None, 0, direction):
            ux += backlash * direction
        last_dir["pan"] = direction
    if uy != 0:
        direction = 1 if uy > 0 else -1
        if last_dir.get("tilt") not in (None, 0, direction):
            uy += backlash * direction
        last_dir["tilt"] = direction
    return ux, uy


def heading_text(position, device: dict) -> str:
    pan_range = float(device.get("pan_degrees") or 360)
    tilt_range = float(device.get("tilt_degrees") or 90)
    pan_sign = float(device.get("pan_sign") or 1)
    home = device.get("home")
    if home:
        pan_deg = (position[0] - home[0]) * pan_range / 2.0 * pan_sign
        tilt_deg = (position[1] - home[1]) * tilt_range / 2.0
        if abs(pan_deg) < 0.5 and abs(tilt_deg) < 0.5:
            return "at your home view"
        pan_word = f"{abs(round(pan_deg))}° {'right' if pan_deg > 0 else 'left'}"
        tilt_word = f"{abs(round(tilt_deg))}° {'up' if tilt_deg > 0 else 'down'}"
        return f"{pan_word} and {tilt_word} from your home view"
    pan_deg = position[0] * pan_range / 2.0 * pan_sign
    tilt_deg = position[1] * tilt_range / 2.0
    return f"pan {pan_deg:+.0f}°, tilt {tilt_deg:+.0f}°"


def caption_time(wall: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(wall))


# -- the stream: fresh frames only ----------------------------------------------

def _opencv_capture(url: str):
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
    import cv2

    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # newest frame only: no backlog
    except Exception:
        pass
    return cap


def _encode_frame(frame) -> bytes | None:
    if isinstance(frame, (bytes, bytearray)):
        return bytes(frame)
    try:
        import cv2
        import numpy as np  # noqa: F401

        ok, buf = cv2.imencode(".jpg", frame,
                               [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        return buf.tobytes() if ok else None
    except Exception:
        try:
            from PIL import Image as PilImage

            buf = io.BytesIO()
            PilImage.fromarray(frame).save(buf, format="JPEG", quality=92)
            return buf.getvalue()
        except Exception:
            return None


class Stream:
    """One RTSP session, only the newest frame kept, stall-proof.

    Rules (spec 05): a generation counter so an abandoned reader can never
    overwrite a fresh one; frames from the first second after an open are
    skipped (grey until a keyframe); no qualifying frame in STALL seconds
    means the stream is stalled and is reopened.
    """

    def __init__(self, url: str, awake: float = 60.0, capture_factory=None):
        import atexit

        self.url = url
        self.awake = float(awake)
        atexit.register(self.stop)  # OpenCV aborts if a capture outlives exit
        self._factory = capture_factory or _opencv_capture
        self._lock = threading.Lock()
        self._generation = 0
        self._cap = None
        self._thread: threading.Thread | None = None
        self._frame: tuple[bytes, float, float] | None = None  # jpeg, wall, mono
        self._opened_mono = 0.0
        self._last_request = 0.0
        self.shot_at = 0.0

    # reader thread
    def _reader(self, generation: int, cap) -> None:
        while True:
            with self._lock:
                if generation != self._generation:
                    return
            try:
                ok, frame = cap.read()
            except Exception:
                ok, frame = False, None
            if not ok or frame is None:
                time.sleep(0.05)
                continue
            # keep the raw frame; encoding full-res JPEGs of every frame on
            # the Pi is slower than the stream, so encoding here makes the
            # "newest" frame fall seconds behind what it shows
            with self._lock:
                if generation != self._generation:
                    return
                self._frame = (frame, time.time(), time.monotonic())

    def _open_locked(self) -> None:
        self._generation += 1
        generation = self._generation
        old = self._cap
        if old is not None:
            try:
                old.release()
            except Exception:
                pass
        self._frame = None
        self._cap = self._factory(self.url)
        self._opened_mono = time.monotonic()
        self._thread = threading.Thread(target=self._reader,
                                        args=(generation, self._cap), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            if self._cap is not None:
                try:
                    self._cap.release()
                except Exception:
                    pass
                self._cap = None
            self._frame = None
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)  # let the reader leave read() before exit

    def maybe_close(self) -> None:
        with self._lock:
            open_now = self._cap is not None
            idle = time.monotonic() - self._last_request
        if open_now and self._last_request and idle > self.awake:
            self.stop()

    def jpeg(self, fresh_after: float = 0.0) -> bytes:
        """A frame decoded after max(fresh_after, now). Raises OnvifError."""
        request = time.monotonic()
        with self._lock:
            self._last_request = request
            was_open = self._cap is not None
            if not was_open:
                self._open_locked()
        wait = OPEN_WAIT if not was_open else STALL
        reopened = False
        deadline = request + wait
        while True:
            with self._lock:
                frame = self._frame
                opened = self._opened_mono
            if frame is not None:
                raw, wall, mono = frame
                if mono > max(fresh_after, request) and mono >= opened + 1.0:
                    data = _encode_frame(raw)  # encode only the frame handed out
                    if data is not None:
                        self.shot_at = wall
                        return data
            now = time.monotonic()
            if now >= deadline:
                if was_open and not reopened:
                    # stalled: a fresh session, up to OPEN_WAIT more
                    with self._lock:
                        self._last_request = now
                        self._open_locked()
                    reopened = True
                    deadline = now + OPEN_WAIT
                    continue
                raise OnvifError(
                    "the camera's video stream gives no new picture (wrong account, "
                    "privacy mode on, or too many viewers?)")
            time.sleep(0.05)


# -- recording a clip (PyAV, its own session) -------------------------------------

def record_clip(url: str, seconds: float, av_module=None, clock=time.monotonic) -> dict:
    """Copy packets to an MP4 from the first keyframe, evenly stamped."""
    av = av_module
    if av is None:
        import av as av_module_real

        av = av_module_real
    seconds = max(2.0, min(20.0, float(seconds)))
    in_container = av.open(url, options={"rtsp_transport": "tcp",
                                         "timeout": "10000000"})
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp.close()
    frames = 0
    times: list[float] = []
    try:
        stream = in_container.streams.video[0]
        out = av.open(tmp.name, mode="w", format="mp4",
                      options={"movflags": "+faststart"})
        try:
            out_stream = out.add_stream_from_template(stream)
            started = None
            for packet in in_container.demux(stream):
                if started is None and not getattr(packet, "is_keyframe", False):
                    continue
                if started is None:
                    started = clock()
                arrival = clock() - started
                if frames and arrival > seconds:
                    break
                packet.pts = frames * 3600  # 25 fps in a 1/90000 time base
                packet.dts = frames * 3600
                try:
                    packet.stream = out_stream
                except Exception:
                    pass
                out.mux(packet)
                times.append(round(arrival, 3))
                frames += 1
        finally:
            out.close()
    finally:
        try:
            in_container.close()
        except Exception:
            pass
    data = Path(tmp.name).read_bytes()
    Path(tmp.name).unlink(missing_ok=True)
    return {"video": base64.b64encode(data).decode("ascii"),
            "seconds": times[-1] if times else 0.0,
            "frames": frames, "times": times}


# -- secrets -----------------------------------------------------------------------

def secrets_dir_for(config_path: Path) -> Path:
    env = os.environ.get("HOUSE_SECRETS_DIR")
    if env:
        return Path(env)
    if config_path.parent.name == "robots":
        return config_path.parent.parent / "secrets"
    return Path("data/secrets")


def load_secret(config_path: Path, device_id: str) -> dict:
    path = secrets_dir_for(config_path) / f"{device_id}.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (ValueError, OSError):
            pass
    return {}


def save_secret(config_path: Path, device_id: str, secret: dict) -> Path:
    folder = secrets_dir_for(config_path)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{device_id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(secret, ensure_ascii=False), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    path.chmod(0o600)
    return path


# -- the robot -----------------------------------------------------------------

class CameraRobot:
    def __init__(self, config: dict, config_path: Path):
        self.config = config
        self.config_path = config_path
        self.device = dict(config.get("device") or {})
        self.camera: Camera | None = None
        self.stream: Stream | None = None
        self.last_dir: dict = {}
        self.last_use = 0.0
        self._lock = threading.Lock()
        self.body: Body | None = None

    # camera session
    def ensure_camera(self) -> Camera:
        with self._lock:
            if self.camera is not None:
                return self.camera
            secret = load_secret(self.config_path, self.device.get("id", ""))
            username = secret.get("username") or ""
            password = secret.get("password") or ""
            if not username:
                raise OnvifError(
                    "the camera account isn't on this workshop yet — connect the "
                    "camera through Add a robot first")
            host = self.device.get("host") or ""
            self.camera = login_at(host, username, password,
                                   xaddr=self.device.get("xaddr") or None)
            self.device["ptz"] = self.camera.has_ptz
            return self.camera

    def drop_session(self) -> None:
        with self._lock:
            self.camera = None
            if self.stream is not None:
                self.stream.stop()
                self.stream = None

    def ensure_stream(self) -> Stream:
        camera = self.ensure_camera()
        with self._lock:
            if self.stream is None:
                awake = float(os.environ.get("HOUSE_EYE_AWAKE", "60") or 60)
                self.stream = Stream(camera.rtsp_url(), awake=awake)
            return self.stream

    def mark_use(self) -> None:
        self.last_use = time.monotonic()

    def photo(self, fresh_after: float = 0.0) -> tuple[bytes, float]:
        try:
            stream = self.ensure_stream()
            data = stream.jpeg(fresh_after=fresh_after)
            return data, stream.shot_at
        except OnvifError:
            self.drop_session()
            raise
        except Exception as exc:
            self.drop_session()
            raise OnvifError(str(exc))

    def position(self) -> tuple[float, float]:
        return self.ensure_camera().ptz_status()

    async def report_heading(self) -> str:
        try:
            pos = await asyncio.to_thread(self.position)
            text = heading_text(pos, self.device)
        except Exception:
            text = "camera unreachable"
        if self.body is not None:
            await self.body.set_state(heading=text)
        return text

    # action implementations (blocking work in threads)
    def _turn_blocking(self, pan: float, tilt: float) -> tuple[bytes, float, str]:
        camera = self.ensure_camera()
        ux, uy = move_units(pan, tilt, self.device, self.last_dir)
        camera.ptz_relative_move(ux, uy)
        time.sleep(0.4)
        try:
            pos = camera.wait_still()
        except OnvifError:
            time.sleep(3)
            pos = camera.ptz_status()
        settled = time.monotonic()
        time.sleep(0.3)
        data, shot = self.photo(fresh_after=settled)
        return data, shot, heading_text(pos, self.device)

    def save_home(self) -> tuple[float, float]:
        """The view a person framed by hand becomes the home view, in the
        config the workshop starts this driver with, so it survives restarts."""
        pos = self.ensure_camera().wait_still()
        home = [round(pos[0], 6), round(pos[1], 6)]
        self.device["home"] = home
        config = dict(self.config)
        config["device"] = {**(config.get("device") or {}), "home": home}
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(config, indent=2))
        tmp.replace(self.config_path)
        self.config = config
        return pos

    def _home_blocking(self) -> tuple[bytes, float, str]:
        camera = self.ensure_camera()
        home = self.device["home"]
        camera.ptz_absolute_move(float(home[0]), float(home[1]))
        self.last_dir = {}  # an absolute move takes up the slack itself
        time.sleep(0.4)
        try:
            pos = camera.wait_still()
        except OnvifError:
            time.sleep(3)
            pos = camera.ptz_status()
        settled = time.monotonic()
        time.sleep(0.3)
        data, shot = self.photo(fresh_after=settled)
        return data, shot, heading_text(pos, self.device)

    async def settle_home_loop(self) -> None:
        home_after = float(self.device.get("home_after") or 90)
        while True:
            await asyncio.sleep(5)
            try:
                if self.body is not None and not self.body.state.get("heading"):
                    await self.report_heading()  # say where we point, once known
                if self.stream is not None:
                    self.stream.maybe_close()
                home = self.device.get("home")
                if not home or not self.last_use:
                    continue
                if time.monotonic() - self.last_use < home_after:
                    continue
                # reset the idle clock either way: check again in home_after
                self.last_use = time.monotonic()

                def go_home() -> tuple[float, float]:
                    camera = self.ensure_camera()
                    pos = camera.ptz_status()
                    if (abs(pos[0] - home[0]) > 0.01
                            or abs(pos[1] - home[1]) > 0.01):
                        camera.ptz_absolute_move(float(home[0]), float(home[1]))
                        self.last_dir = {}
                        time.sleep(0.4)
                        pos = camera.wait_still()
                    return pos

                pos = await asyncio.to_thread(go_home)
                if self.body is not None:
                    await self.body.set_state(
                        heading=heading_text(pos, self.device))
            except Exception:
                continue  # camera rebooting or off Wi-Fi: try next round


def build_body(config: dict, config_path: Path) -> tuple[Body, CameraRobot]:
    robot = CameraRobot(config, config_path)
    device = robot.device
    body = Body(
        config.get("name") or "keiki",
        config.get("description") or "A camera robot.",
        display_name=config.get("display_name"),
        persona_hint=config.get("persona_hint"),
        personality=config.get("personality"),
    )
    robot.body = body
    ptz = bool(device.get("ptz", True))
    home = device.get("home")

    @body.action("look", "Look through your camera right now (returns a photo of "
                         "where you're facing).", returns="image")
    async def look():
        robot.mark_use()
        data, shot = await asyncio.to_thread(robot.photo)
        return Image(data, f"what your camera sees (photo taken "
                           f"{caption_time(shot)}, just now)")

    if ptz:
        pan_range = int(device.get("pan_degrees") or 360)
        tilt_range = int(device.get("tilt_degrees") or 90)
        home_after = int(device.get("home_after") or 90)
        description = (
            "Turn your head, relative to where it points now: pan + = right, "
            "− = left; tilt + = up, − = down (degrees). "
            f"Your head pans about {pan_range}° in all and tilts about "
            f"{tilt_range}°. Returns a photo once you've turned.")
        if home:
            description += (
                f" After about {home_after} s without looking around you drift "
                "back to your home view by yourself (look_home does it right away).")

        @body.action("turn", description,
                     {"pan": {"type": "number"}, "tilt": {"type": "number"}},
                     required=[])
        async def turn(pan=0, tilt=0):
            robot.mark_use()
            data, shot, heading = await asyncio.to_thread(
                robot._turn_blocking, float(pan or 0), float(tilt or 0))
            await body.set_state(heading=heading)
            return Image(data, f"turned {float(pan or 0):+g}° pan, "
                               f"{float(tilt or 0):+g}° tilt — this is what you "
                               f"see now (photo taken {caption_time(shot)}, just now)")

    if ptz and home:
        @body.action("look_home", "Turn back to your home view (exactly), the one "
                                  "you're set up to watch. Returns a photo once "
                                  "you're there.", returns="image")
        async def look_home():
            robot.mark_use()
            data, shot, heading = await asyncio.to_thread(robot._home_blocking)
            await body.set_state(heading=heading)
            return Image(data, f"back at your home view — this is what you see "
                               f"now (photo taken {caption_time(shot)}, just now)")

    if ptz:
        @body.action("set_home", "Make where you point now your home view.",
                     required=[], hidden=True)
        async def set_home():
            robot.mark_use()
            pos = await asyncio.to_thread(robot.save_home)
            await body.set_state(heading=heading_text(pos, robot.device))
            return "this is my home view now"

    @body.action("record", "Record a short clip (the house's watch uses this).",
                 {"seconds": {"type": "number"}}, required=[], hidden=True)
    async def record(seconds=8):
        robot.mark_use()
        camera = await asyncio.to_thread(robot.ensure_camera)
        url = camera.rtsp_url()
        result = await asyncio.to_thread(record_clip, url, float(seconds or 8))
        robot.mark_use()
        return result

    return body, robot


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python onvif_body.py <robot.json>")
        raise SystemExit(2)
    config_path = Path(sys.argv[1])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    body, robot = build_body(config, config_path)
    body.run(robot.settle_home_loop())


if __name__ == "__main__":
    main()
