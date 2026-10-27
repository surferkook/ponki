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
_cache: dict = {"at": 0.0, "key": None, "open": {}}


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


def own_addresses() -> list[str]:
    """This machine's own private IPv4 addresses (strings)."""
    addresses: list[str] = []
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return addresses
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        iface = parts[1].rstrip(":")
        if iface == "lo" or iface.startswith(("docker", "br-", "veth", "tailscale")):
            continue
        try:
            iface_addr = ipaddress.IPv4Interface(parts[3])
        except ValueError:
            continue
        if not iface_addr.ip.is_private:
            continue
        text = str(iface_addr.ip)
        if text not in addresses:
            addresses.append(text)
    return addresses


def hosts() -> list[str]:
    """Every host address in this machine's private /24s."""
    out: list[str] = []
    for net in own_subnets():
        for host in net.hosts():
            out.append(str(host))
    return out


def sweep(ports, targets=None) -> dict[str, set[int]]:
    """{host: {open ports}}; targets defaults to hosts(). Cached 20 s."""
    port_set = {int(p) for p in ports}
    target_list = hosts() if targets is None else [str(t) for t in targets]
    key = (frozenset(port_set), tuple(target_list))
    now = time.monotonic()
    if _cache["key"] == key and now - _cache["at"] < CACHE_SECONDS:
        return {h: set(ps) for h, ps in _cache["open"].items()}
    probes: list[tuple[str, int]] = [
        (host, port) for host in target_list for port in sorted(port_set)]

    def probe(target: tuple[str, int]) -> tuple[str, int, bool]:
        host, port = target
        try:
            with socket.create_connection((host, port), timeout=0.6):
                return host, port, True
        except OSError:
            return host, port, False

    found: dict[str, set[int]] = {}
    if probes:
        with concurrent.futures.ThreadPoolExecutor(max_workers=512) as pool:
            for host, port, is_open in pool.map(probe, probes):
                if is_open:
                    found.setdefault(host, set()).add(port)
    _cache.update(at=now, key=key, open=found)
    return {h: set(ps) for h, ps in found.items()}


def forget() -> None:
    _cache.update(at=0.0, key=None, open={})


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
