"""The LAN as families see it (spec 05): own subnets, a port sweep, names.

Sweep results are cached for 20 s, because every family scans during the
same "Look again", and forget() clears the cache.
"""
from __future__ import annotations

import concurrent.futures
import ipaddress
import socket
import ssl
import subprocess
import time

CACHE_SECONDS = 20.0
_cache: dict = {"at": 0.0, "ports": set(), "open": {}}


def own_subnets() -> list[ipaddress.IPv4Network]:
    """This machine's private IPv4 /24s (skipping lo/docker/br-/veth/tailscale)."""
    nets: list[ipaddress.IPv4Network] = []
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return nets
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        iface = parts[1].rstrip(":")
        if iface == "lo" or iface.startswith(("docker", "br-", "veth", "tailscale")):
            continue
        cidr = parts[3]
        try:
            iface_addr = ipaddress.IPv4Interface(cidr)
        except ValueError:
            continue
        if not iface_addr.ip.is_private:
            continue
        net = ipaddress.IPv4Network(f"{iface_addr.ip}/24", strict=False)
        if net not in nets:
            nets.append(net)
    return nets


def sweep(ports: set[int], timeout: float = 0.6) -> dict[str, set[int]]:
    """{host: {open ports}} across our /24s. Cached; forget() clears."""
    now = time.monotonic()
    if _cache["open"] and _cache["ports"] == set(ports) \
            and now - _cache["at"] < CACHE_SECONDS:
        return dict(_cache["open"])
    targets: list[tuple[str, int]] = []
    for net in own_subnets():
        for host in net.hosts():
            for port in sorted(ports):
                targets.append((str(host), port))

    def probe(target: tuple[str, int]) -> tuple[str, int, bool]:
        host, port = target
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return host, port, True
        except OSError:
            return host, port, False

    found: dict[str, set[int]] = {}
    if targets:
        with concurrent.futures.ThreadPoolExecutor(max_workers=512) as pool:
            for host, port, is_open in pool.map(probe, targets):
                if is_open:
                    found.setdefault(host, set()).add(port)
    _cache.update(at=now, ports=set(ports), open=found)
    return dict(found)


def forget() -> None:
    _cache.update(at=0.0, ports=set(), open={})


def name(host: str) -> str:
    try:
        return socket.gethostbyaddr(host)[0]
    except (OSError, socket.herror):
        return ""


def tls_certificate(host: str, port: int = 443) -> bytes:
    """The peer's DER certificate, unverified — a fingerprint only."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=3) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                return tls.getpeercert(binary_form=True) or b""
    except (OSError, ssl.SSLError):
        return b""


def tapo_model_from_name(hostname: str) -> str:
    """'c225.home' looks like a Tapo's reverse name → 'C225'."""
    first = (hostname or "").split(".")[0]
    if first and len(first) <= 6 and any(ch.isdigit() for ch in first) \
            and first[0].isalpha():
        return first.upper()
    return ""
