"""Serve the house: everything on one port, and only what bodies need on another.

    python -m hub.serve

HOUSE_PORT (8200) is the whole house: the web UI, its API and the body
websocket. The UI has no login, so keep this port on this machine, a LAN
you trust or a tailnet.

HOUSE_BODY_PORT (unset: off; 8210 with Docker) is the door for workshops and
bodies on other machines. It answers only the body websocket (/ws/body,
which checks HOUSE_TOKEN in its hello) and house-voice clips for speakers
(GET /api/voice/<n>.wav with the token as a Bearer header). Everything else
is a 404, so this is the one port to open to other machines.

When PONKI_BODY_BIND says the body port is published beyond this machine,
a missing, default or short HOUSE_TOKEN stops the hub from starting.

HOUSE_HOST (0.0.0.0) is the address both ports listen on.
"""
from __future__ import annotations

import os
import socket
import sys

import uvicorn

# Tokens that ship in our own files: never good enough for an open port.
KNOWN_TOKENS = {"", "ponki-in-a-box", "change-me-to-a-long-random-string"}
MIN_TOKEN = 24
LOCAL_BINDS = {"", "127.0.0.1", "localhost", "::1"}


class BodyGate:
    """ASGI wrapper: requests that arrive on the body port reach only what bodies use."""

    def __init__(self, app, body_port: int | None, token: str):
        self.app = app
        self.body_port = body_port
        self.token = token

    def allowed(self, scope) -> bool:
        path = scope.get("path", "")
        if scope["type"] == "websocket":
            return path == "/ws/body"
        if scope.get("method") not in ("GET", "HEAD") or not path.startswith("/api/voice/"):
            return False
        if not self.token:
            return True
        headers = dict(scope.get("headers") or [])
        return headers.get(b"authorization", b"").decode("latin-1") == f"Bearer {self.token}"

    async def __call__(self, scope, receive, send):
        server = scope.get("server") or (None, None)
        if (self.body_port and scope["type"] in ("http", "websocket")
                and server[1] == self.body_port and not self.allowed(scope)):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4404})
                return
            await send({"type": "http.response.start", "status": 404,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
            await send({"type": "http.response.body",
                        "body": b"This is the house's door for robots; the web UI is on its own port.\n"})
            return
        await self.app(scope, receive, send)


def token_problem(token: str, body_bind: str) -> str | None:
    """Why this token can't guard a body port published beyond this machine, or None."""
    if body_bind.strip() in LOCAL_BINDS:
        return None
    if token in KNOWN_TOKENS:
        return "HOUSE_TOKEN is empty or still the example value"
    if len(token) < MIN_TOKEN:
        return f"HOUSE_TOKEN is shorter than {MIN_TOKEN} characters"
    return None


def bind(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.set_inheritable(True)
    return sock


def main() -> int:
    host = os.environ.get("HOUSE_HOST") or "0.0.0.0"
    port = int(os.environ.get("HOUSE_PORT") or 8200)
    body_port = int(os.environ.get("HOUSE_BODY_PORT") or 0) or None
    token = os.environ.get("HOUSE_TOKEN", "")
    if body_port:
        problem = token_problem(token, os.environ.get("PONKI_BODY_BIND", ""))
        if problem:
            print(f"Ponki won't open its door for robots to other machines: {problem}.\n"
                  "Put a long random one in .env, for example:\n"
                  "  HOUSE_TOKEN=" + "<" + "python3 -c 'import secrets; print(secrets.token_urlsafe(32))'" + ">\n"
                  "and give the same token to every workshop.", file=sys.stderr)
            return 2
        if body_port == port:
            print("HOUSE_BODY_PORT must differ from HOUSE_PORT", file=sys.stderr)
            return 2

    from hub.main import app  # after the checks: importing builds the house

    sockets = [bind(host, port)] + ([bind(host, body_port)] if body_port else [])
    print(f"the house on :{port}" + (f", the door for robots on :{body_port}" if body_port else ""),
          flush=True)
    config = uvicorn.Config(BodyGate(app, body_port, token), proxy_headers=False)
    uvicorn.Server(config).run(sockets=sockets)
    return 0


if __name__ == "__main__":
    sys.exit(main())
