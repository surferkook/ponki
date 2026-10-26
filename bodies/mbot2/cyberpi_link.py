"""USB serial to the CyberPi (spec 07): Makeblock frames, script calls,
program upload and reset, from the workshop side.

Frame: F3 hdr len_lo len_hi body[len] ck F4, little-endian, hdr and ck are
sums mod 256. Everything outside a frame is the running program's print()
output. Opening the port resets the board; it takes 8-14 s to answer, so
callers ask until it does. No hardware here beyond pyserial.
"""
from __future__ import annotations

import json
import re
import struct
import time

BOOT_WAIT = 30.0
STAGING_PATH = "/flash/_xx_main.py"
PING_BODY = b"\x0d\x00\x00"


def frame(body: bytes) -> bytes:
    body = bytes(body)
    length = len(body)
    lo, hi = length & 0xFF, (length >> 8) & 0xFF
    hdr = (0xF3 + lo + hi) & 0xFF
    ck = sum(body) & 0xFF
    return b"\xF3" + bytes([hdr, lo, hi]) + body + bytes([ck]) + b"\xF4"


def script_frame(source: str, idx: int, reply: bool = True) -> bytes:
    raw = source.encode("utf-8")
    body = bytes([0x28, 0x01 if reply else 0x04]) \
        + struct.pack("<H", idx) + struct.pack("<H", len(raw)) + raw
    return frame(body)


class FrameReader:
    """Bytes in, frame bodies out; text between frames is kept."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.text = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buf.extend(data)
        bodies: list[bytes] = []
        while True:
            start = self._buf.find(b"\xF3")
            if start < 0:
                self.text.extend(self._buf)
                self._buf.clear()
                break
            if start > 0:
                self.text.extend(self._buf[:start])
                del self._buf[:start]
            if len(self._buf) < 4:
                break
            _f3, hdr, lo, hi = self._buf[0], self._buf[1], self._buf[2], self._buf[3]
            length = lo | (hi << 8)
            if hdr != (0xF3 + lo + hi) & 0xFF or length > 4096:
                self.text.append(self._buf[0])
                del self._buf[0]
                continue
            if len(self._buf) < length + 6:
                break
            body = bytes(self._buf[4:4 + length])
            ck, end = self._buf[4 + length], self._buf[5 + length]
            if ck != sum(body) & 0xFF or end != 0xF4:
                self.text.append(self._buf[0])
                del self._buf[0]
                continue
            bodies.append(body)
            del self._buf[:length + 6]
        return bodies

    def take_text(self) -> str:
        text = bytes(self.text).decode("utf-8", "replace")
        self.text.clear()
        return text


def pad_program(data: bytes) -> bytes:
    """Append newlines until len % 80 == 4 (whole 80B blocks + a final 4B)."""
    data = bytes(data)
    missing = (4 - len(data)) % 80
    return data + b"\n" * missing


def xor32(data: bytes) -> int:
    padded = data + b"\x00" * ((4 - len(data) % 4) % 4)
    value = 0
    for (word,) in struct.iter_unpack("<I", padded):
        value ^= word
    return value


def upload_frames(padded: bytes) -> list[bytes]:
    """Header frame, then 80-byte data blocks, the last 4 bytes alone."""
    header = b"\x01\x00\x5e\x01" + struct.pack("<H", 9 + len(STAGING_PATH)) \
        + b"\x00" + struct.pack("<I", len(padded)) \
        + struct.pack("<I", xor32(padded)) + STAGING_PATH.encode("ascii")
    frames = [frame(header)]
    offset = 0
    while offset < len(padded) - 4:
        block = padded[offset:offset + 80]
        body = b"\x01\x00\x5e\x02" + struct.pack("<H", 4 + len(block)) \
            + struct.pack("<I", offset) + block
        frames.append(frame(body))
        offset += 80
    last = padded[len(padded) - 4:]
    body = b"\x01\x00\x5e\x02" + struct.pack("<H", 4 + len(last)) \
        + struct.pack("<I", len(padded) - 4) + last
    frames.append(frame(body))
    return frames


def _parse_ret(body: bytes):
    text = body.decode("utf-8", "replace")
    start = text.find("{")
    if start < 0:
        return text
    raw = text[start:]
    cleaned = re.sub(r"\bNone\b", "null", raw)
    cleaned = re.sub(r"\bTrue\b", "true", cleaned)
    cleaned = re.sub(r"\bFalse\b", "false", cleaned)
    try:
        return json.loads(cleaned).get("ret")
    except ValueError:
        return text


class CyberPi:
    """One board on one serial port. `serial_obj` injects a fake for tests."""

    def __init__(self, port: str, boot_wait: float = BOOT_WAIT,
                 serial_obj=None, answer_timeout: float = 3.0) -> None:
        self.port = port
        self.boot_wait = boot_wait
        self.answer_timeout = answer_timeout
        self._factory = lambda: serial_obj  # noqa: E731
        if serial_obj is None:
            self._factory = self._open_serial
        self.ser = self._factory()
        self.reader = FrameReader()
        self._idx = 0
        self._wait_ready()

    def _open_serial(self):
        import serial

        ser = serial.Serial()
        ser.port = self.port
        ser.baudrate = 115200
        ser.timeout = 0.05
        ser.dtr = False  # set before opening: opening resets the board
        ser.rts = False
        ser.open()
        return ser

    # -- wire plumbing ------------------------------------------------------
    def _pump(self, seconds: float) -> list[bytes]:
        deadline = time.time() + seconds
        bodies: list[bytes] = []
        while time.time() < deadline:
            try:
                waiting = getattr(self.ser, "in_waiting", 0)
                data = self.ser.read(max(1, waiting)) if waiting else \
                    self.ser.read(1)
            except Exception:
                data = b""
            if data:
                bodies.extend(self.reader.feed(data))
                if bodies:
                    # a short grace for the rest of a split frame
                    data2 = self.ser.read(4096) if getattr(
                        self.ser, "in_waiting", 0) else b""
                    if data2:
                        bodies.extend(self.reader.feed(data2))
                    return bodies
            else:
                time.sleep(0.01)
        return bodies

    def _transact(self, payload: bytes, match, what: str, tries: int = 3,
                  timeout: float | None = None) -> bytes:
        timeout = self.answer_timeout if timeout is None else timeout
        for _ in range(tries):
            self.ser.write(payload)
            deadline = time.time() + timeout
            while time.time() < deadline:
                for body in self._pump(max(0.05, deadline - time.time())):
                    if match(body):
                        return body
        raise TimeoutError(f"the CyberPi didn't answer ({what})")

    def output(self) -> str:
        self._pump(0.05)
        return self.reader.take_text()

    # -- script calls ----------------------------------------------------------
    def _next_idx(self) -> int:
        self._idx = self._idx % 0xFFFF + 1
        return self._idx

    def script(self, code: str, timeout: float = 3.0):
        idx = self._next_idx()
        body = self._transact(
            script_frame(code, idx), 
            lambda b: len(b) >= 4 and b[0] == 0x28
            and (b[2] | (b[3] << 8)) == idx and b'"ret"' in b,
            "script", tries=1, timeout=timeout)
        return _parse_ret(body)

    def _wait_ready(self) -> None:
        deadline = time.time() + self.boot_wait
        while time.time() < deadline:
            try:
                self.script("1", timeout=0.8)
                return
            except TimeoutError:
                continue
        raise TimeoutError("the CyberPi didn't answer — is it switched on, "
                           "and not in use by mBlock?")

    # -- upload -------------------------------------------------------------------
    def _prepare(self) -> None:
        sources = [
            "try:\n    import config\nexcept:\n    pass",
            'try:\n    config.write_config("repl_enable", False)\nexcept:\n    pass',
        ]
        self._transact(frame(PING_BODY),
                       lambda b: len(b) >= 1 and b[0] == 0x0D, "upload mode")
        for source in sources:
            raw = source.encode("utf-8")
            body = bytes([0x28, 0x04]) + struct.pack("<H", 0) \
                + struct.pack("<H", len(raw)) + raw
            self._transact(
                frame(body),
                lambda b: b'"ret"' in b or len(b) == 18, "prepare")

    def upload(self, program: bytes, progress=None) -> int:
        padded = pad_program(program)
        frames = upload_frames(padded)
        total = len(frames) - 1  # blocks after the header
        self._prepare()
        self._prepare()
        header, blocks = frames[0], frames[1:]
        self._transact(
            header, lambda b: b"\x5e\xf0" in b or len(b) == 7, "program header")
        for i, block in enumerate(blocks, 1):
            self._transact(
                block, lambda b: b"\x5e\xf0" in b or len(b) == 7,
                f"program block {i}")
            if progress:
                progress(i, total)
        self._prepare()
        return len(padded)

    def restart(self, ready: bool = True) -> None:
        """A hard reset through the port: close, reopen, fresh reader."""
        try:
            self.ser.close()
        except Exception:
            pass
        time.sleep(0.3)
        self.ser = self._factory()
        self.reader = FrameReader()
        if ready:
            self._wait_ready()
        # with ready=False send nothing at all: any frame would put the
        # board into Makeblock's live mode and it couldn't join the Wi-Fi

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass

    def __enter__(self) -> "CyberPi":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
