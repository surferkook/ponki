"""The rover link (spec 07): render the on-rover program, and the TCP
server the rover dials. One JSON object per line; the rover speaks first
with a hello carrying its key; the workshop pings every 3 s.
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets as _secrets
import socket
import time
from pathlib import Path

PORT = 8220
PING_EVERY = 3
MOVE_TIMEOUT = 30
TURN_SIGN = -1  # rover turn(+) is right; the house's + is left
ROVER_PY = Path(__file__).with_name("rover.py")


class Offline(RuntimeError):
    pass


def render(ssid: str, password: str, host: str, port: int, key: str) -> bytes:
    text = ROVER_PY.read_text(encoding="utf-8")
    for placeholder, value in (('"{ssid}"', ssid), ('"{password}"', password),
                               ('"{host}"', host), ('"{key}"', key)):
        text = text.replace(placeholder, json.dumps(value))
    text = text.replace("{port}", str(int(port)), 1)
    compile(text, "rover.py", "exec")  # a typo must never reach the board
    return text.encode("utf-8")


def version() -> int:
    match = re.search(r"^VERSION = (\d+)", ROVER_PY.read_text(encoding="utf-8"),
                      re.M)
    return int(match.group(1)) if match else 0


def new_key() -> str:
    return _secrets.token_hex(8)


def my_address(towards: str = "192.0.2.1") -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((towards, 9))
        return sock.getsockname()[0]
    finally:
        sock.close()


def free_port(start: int = PORT, taken=()) -> int:
    for port in range(start, start + 50):
        if port in taken:
            continue
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("0.0.0.0", port))
            probe.close()
            return port
        except OSError:
            continue
    raise RuntimeError("no free port for the rover")


# -- the workshop's secrets (spec 09's store, local until the workshop lands)

def secrets_dir(config_path: Path | None = None) -> Path:
    import os

    env = os.environ.get("HOUSE_SECRETS_DIR")
    if env:
        return Path(env)
    if config_path is not None and config_path.parent.name == "robots":
        return config_path.parent.parent / "secrets"
    return Path("data/secrets")


def save_secret(device_id: str, secret: dict,
                config_path: Path | None = None) -> Path:
    folder = secrets_dir(config_path)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{device_id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(secret, ensure_ascii=False), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    path.chmod(0o600)
    return path


def load_secret(device_id: str, config_path: Path | None = None) -> dict:
    path = secrets_dir(config_path) / f"{device_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class RoverLink:
    def __init__(self, port: int, key: str, on_change=None) -> None:
        self.port = port
        self.key = key
        self.on_change = on_change
        self.online = False
        self.hello: dict = {}
        self._server: asyncio.Server | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._ids = 0
        self._pinger: asyncio.Task | None = None

    async def serve(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, "0.0.0.0", self.port)
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
        if self._server is not None:
            self._server.close()

    async def wait(self, timeout: float) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.online:
                return self.hello
            await asyncio.sleep(0.1)
        raise Offline(f"the rover didn't call in within {timeout:g} s")

    def _notify(self, online: bool, hello: dict) -> None:
        if self.on_change is not None:
            # a background task, never inline: on_change typically calls the
            # rover, whose answer only the read loop can deliver
            asyncio.ensure_future(self._safe_notify(online, hello))

    async def _safe_notify(self, online: bool, hello: dict) -> None:
        try:
            result = self.on_change(online, hello)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            pass

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            hello = json.loads(line.decode("utf-8"))
        except Exception:
            writer.close()
            return
        if hello.get("hello") != "house-rover" or hello.get("key") != self.key:
            writer.close()  # not our rover: hang up
            return
        if self._writer is not None:
            self._writer.close()  # a new link replaces an old one
        self._writer = writer
        self.hello = {**hello, "from": peer[0] if peer else ""}
        self.online = True
        self._notify(True, self.hello)
        self._pinger = asyncio.ensure_future(self._ping_loop(writer))
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8"))
                except ValueError:
                    continue
                fut = self._pending.pop(msg.get("id"), None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except Exception:
            pass
        finally:
            if self._pinger is not None:
                self._pinger.cancel()
            if self._writer is writer:  # only the current link clears state
                self._writer = None
                self.online = False
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(Offline("the rover went offline"))
                self._pending.clear()
                self._notify(False, self.hello)

    async def _ping_loop(self, writer: asyncio.StreamWriter) -> None:
        while True:
            await asyncio.sleep(PING_EVERY)
            try:
                await self.call("ping", timeout=5)
            except Exception:
                return

    async def call(self, do: str, timeout: float | None = None, **args):
        if not self.online or self._writer is None:
            raise Offline("the rover isn't connected (is it switched on and "
                          "on the Wi-Fi?)")
        if timeout is None:
            timeout = MOVE_TIMEOUT if do in ("drive", "turn") else 5
        self._ids = self._ids % 1_000_000 + 1
        mid = self._ids
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        self._writer.write((json.dumps({"id": mid, "do": do, **args}) + "\n")
                           .encode("utf-8"))
        try:
            await self._writer.drain()
            msg = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise Offline(f"the rover didn't answer ({do})")
        if not msg.get("ok"):
            raise RuntimeError(f"the rover says: {msg.get('error', 'it failed')}")
        return msg.get("ret")


async def with_rover(port: int, key: str, fn, timeout: float = 30):
    """A temporary server for one builder step, then hang up."""
    link = RoverLink(port, key)
    server = await asyncio.start_server(link._handle, "0.0.0.0", port,
                                        reuse_address=True)
    link._server = server
    try:
        await link.wait(timeout)
        return await fn(link)
    finally:
        await link.close()
        server.close()


async def wait_for_rover(port: int, key: str, timeout: float = 45) -> dict:
    began = time.time()

    async def grab(link: RoverLink):
        return link.hello

    hello = await with_rover(port, key, grab, timeout=timeout)
    return {**hello, "after": round(time.time() - began, 1)}
