"""ONVIF over SOAP, standard library only (spec 05).

Discovery, probe, login, profiles, stream address, pan/tilt position and
moves for a Tapo-style IP camera. Responses are read by local tag name,
ignoring namespaces, because cameras disagree about prefixes.

Run standalone to probe a camera (never prints the password or stream URL):
    python onvif_client.py 192.0.2.39 <username> <password>
"""
from __future__ import annotations

import base64
import hashlib
import html
import os
import re
import secrets as _secrets
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

ONVIF_PORTS = (2020, 80, 8000, 8080)
DEVICE_PATH = "/onvif/device_service"


class OnvifError(Exception):
    """A camera problem in plain words."""


# -- XML helpers (local tag names) -------------------------------------------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_all(node, name: str) -> list:
    return [el for el in node.iter() if _local(el.tag) == name]


def _find(node, name: str):
    for el in node.iter():
        if _local(el.tag) == name:
            return el
    return None


def _text(node, name: str, default: str = "") -> str:
    el = _find(node, name) if node is not None else None
    return (el.text or "").strip() if el is not None and el.text else default


def _parse_xml(raw: bytes):
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        raise OnvifError("the camera's answer wasn't ONVIF")


# -- WS-Security ---------------------------------------------------------------

def wsse_digest(nonce: bytes, created: str, password: str) -> str:
    return base64.b64encode(
        hashlib.sha1(nonce + created.encode("utf-8") + password.encode("utf-8")).digest()
    ).decode("ascii")


def wsse_header(username: str, password: str, clock_offset: float = 0.0,
                nonce: bytes | None = None, now: datetime | None = None) -> str:
    """A UsernameToken with PasswordDigest, timed by the camera's clock."""
    nonce = nonce if nonce is not None else _secrets.token_bytes(16)
    moment = (now or datetime.now(timezone.utc)) + timedelta(seconds=clock_offset)
    created = moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    digest = wsse_digest(nonce, created, password)
    user = html.escape(username, quote=False)
    return (
        '<wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/'
        'oasis-200401-wss-wssecurity-secext-1.0.xsd">'
        "<wsse:UsernameToken>"
        f"<wsse:Username>{user}</wsse:Username>"
        f'<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/'
        f'oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password>'
        f'<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/'
        f'oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
        f'{base64.b64encode(nonce).decode("ascii")}</wsse:Nonce>'
        f'<wsu:Created xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/'
        f'oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</wsu:Created>'
        "</wsse:UsernameToken></wsse:Security>"
    )


_ENVELOPE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:tt="http://www.onvif.org/ver10/schema" '
    'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
    'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
    'xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl">'
    "<s:Header>{header}</s:Header><s:Body>{body}</s:Body></s:Envelope>"
)


class Camera:
    """One logged-in camera. All methods raise OnvifError in plain words."""

    def __init__(self, xaddr: str, username: str, password: str, timeout: float = 10.0):
        self.xaddr = xaddr
        self.username = username
        self.password = password
        self.timeout = timeout
        self.clock_offset = 0.0
        self.info: dict = {}
        self.media_xaddr = ""
        self.ptz_xaddr = ""
        self.profile_token = ""
        self.has_ptz = False
        self.stream_uri = ""

    # -- transport -----------------------------------------------------------
    def _post(self, url: str, body: str, auth: bool = True):
        header = wsse_header(self.username, self.password, self.clock_offset) if auth else ""
        data = _ENVELOPE.format(header=header, body=body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/soap+xml; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()  # a stalling camera must not crash the reader
            except Exception:
                raw = b""
            fault = ""
            if raw:
                try:
                    fault = _text(_parse_xml(raw), "Text") or _text(_parse_xml(raw), "Reason")
                except OnvifError:
                    fault = ""
            if exc.code in (400, 401, 403) and (
                    not fault or re.search(r"auth|login|password|user|token|security",
                                           fault, re.I)):
                raise OnvifError(
                    f"the camera refused the login ({fault or exc.code}) "
                    f"— check the camera account")
            if fault:
                raise OnvifError(f"the camera said: {fault}")
            raise OnvifError(f"the camera said: HTTP {exc.code}")
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            raise OnvifError(f"can't reach the camera at {url} ({exc})")
        root = _parse_xml(raw)
        fault_el = _find(root, "Fault")
        if fault_el is not None:
            fault = _text(fault_el, "Text") or _text(fault_el, "Reason")
            if fault:
                if re.search(r"auth|login|password|user|token|security", fault, re.I):
                    raise OnvifError(
                        f"the camera refused the login ({fault}) — check the camera account")
                raise OnvifError(f"the camera said: {fault}")
        return root

    # -- device service --------------------------------------------------------
    def sync_clock(self) -> float:
        """GetSystemDateAndTime without login; keep the camera/our offset."""
        root = self._post(self.xaddr, "<tds:GetSystemDateAndTime/>", auth=False)
        utc = _find(root, "UTCDateTime")
        if utc is None:
            raise OnvifError("the camera's answer wasn't ONVIF")
        date_text = _text(utc, "Date")
        time_text = _text(utc, "Time")
        cam = None
        if "-" in date_text and ":" in time_text:
            try:
                cam = datetime.strptime(f"{date_text} {time_text}",
                                        "%Y-%m-%d %H:%M:%S")
            except ValueError:
                cam = None
        if cam is None:
            # ONVIF's structured form: Date{Year,Month,Day}, Time{Hour,Minute,Second}
            date_el = _find(utc, "Date")
            time_el = _find(utc, "Time")
            try:
                cam = datetime(
                    int(_text(date_el, "Year")), int(_text(date_el, "Month")),
                    int(_text(date_el, "Day")), int(_text(time_el, "Hour")),
                    int(_text(time_el, "Minute")), int(_text(time_el, "Second")))
            except (ValueError, TypeError):
                raise OnvifError("the camera's answer wasn't ONVIF")
        cam = cam.replace(tzinfo=timezone.utc)
        self.clock_offset = (cam - datetime.now(timezone.utc)).total_seconds()
        return self.clock_offset

    def get_device_info(self) -> dict:
        root = self._post(self.xaddr, "<tds:GetDeviceInformation/>")
        self.info = {
            "manufacturer": _text(root, "Manufacturer"),
            "model": _text(root, "Model"),
            "firmware": _text(root, "FirmwareVersion"),
            "serial": _text(root, "SerialNumber"),
        }
        return self.info

    def get_profiles(self) -> None:
        root = self._post(self.media_xaddr, "<trt:GetProfiles/>")
        profiles = _find_all(root, "Profiles")
        if not profiles:
            raise OnvifError("the camera has no media profiles")
        first = profiles[0]
        self.profile_token = first.attrib.get("token", "")
        has_ptz_config = _find(first, "PTZConfiguration") is not None
        self.has_ptz = bool(self.ptz_xaddr) and has_ptz_config
        # the PTZ XAddr came from capabilities; ptz service exists iff non-empty above
        # (get_capabilities always fills a fallback, so re-check via a marker:)
        self.has_ptz = has_ptz_config and bool(self._ptz_seen)

    _ptz_seen = False

    def get_stream_uri(self) -> str:
        body = (
            "<trt:GetStreamUri><trt:StreamSetup>"
            '<tt:Stream>RTP-Unicast</tt:Stream>'
            "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>"
            "</trt:StreamSetup>"
            f"<trt:ProfileToken>{html.escape(self.profile_token)}</trt:ProfileToken>"
            "</trt:GetStreamUri>")
        root = self._post(self.media_xaddr, body)
        uri = _text(root, "Uri")
        if not uri:
            raise OnvifError("the camera gave no stream address")
        self.stream_uri = uri
        return uri

    def rtsp_url(self) -> str:
        """The stream address with the account added. Never logged anywhere."""
        uri = self.stream_uri or self.get_stream_uri()
        parts = urllib.parse.urlsplit(uri)
        user = urllib.parse.quote(self.username, safe="")
        password = urllib.parse.quote(self.password, safe="")
        netloc = f"{user}:{password}@{parts.hostname}"
        if parts.port:
            netloc += f":{parts.port}"
        return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path,
                                        parts.query, parts.fragment))

    def login(self) -> "Camera":
        self.sync_clock()
        self.get_device_info()
        # capabilities first (record whether a PTZ service really exists)
        root = self._post(
            self.xaddr,
            "<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>")
        caps = _find(root, "Capabilities")
        self._ptz_seen = False
        if caps is not None:
            for service in caps:
                name = _local(service.tag)
                if name == "Media":
                    self.media_xaddr = _text(service, "XAddr", self.media_xaddr)
                elif name == "PTZ":
                    self.ptz_xaddr = _text(service, "XAddr", self.ptz_xaddr)
                    self._ptz_seen = True
        self.media_xaddr = self.media_xaddr or self.xaddr
        self.get_profiles()
        self.get_stream_uri()
        return self

    # -- PTZ -------------------------------------------------------------------
    def _ptz(self, body: str):
        return self._post(self.ptz_xaddr or self.xaddr, body)

    def ptz_status(self) -> tuple[float, float]:
        body = ("<tptz:GetStatus>"
                f"<tptz:ProfileToken>{html.escape(self.profile_token)}</tptz:ProfileToken>"
                "</tptz:GetStatus>")
        root = self._ptz(body)
        position = _find(root, "Position")
        pan_tilt = _find(position, "PanTilt") if position is not None else None
        if pan_tilt is None:
            raise OnvifError("the camera gave no head position")
        return (float(pan_tilt.attrib.get("x", 0)), float(pan_tilt.attrib.get("y", 0)))

    def ptz_relative_move(self, x: float, y: float) -> None:
        body = ("<tptz:RelativeMove>"
                f"<tptz:ProfileToken>{html.escape(self.profile_token)}</tptz:ProfileToken>"
                f'<tptz:Translation><tt:PanTilt x="{x:.6f}" y="{y:.6f}" '
                'space="http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationGenericSpace"/>'
                "</tptz:Translation></tptz:RelativeMove>")
        self._ptz(body)

    def ptz_absolute_move(self, x: float, y: float) -> None:
        body = ("<tptz:AbsoluteMove>"
                f"<tptz:ProfileToken>{html.escape(self.profile_token)}</tptz:ProfileToken>"
                f'<tptz:Position><tt:PanTilt x="{x:.6f}" y="{y:.6f}" '
                'space="http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace"/>'
                "</tptz:Position></tptz:AbsoluteMove>")
        self._ptz(body)

    def ptz_continuous_move(self, x: float, y: float) -> None:
        # In the client for completeness; nothing in the house uses it.
        body = ("<tptz:ContinuousMove>"
                f"<tptz:ProfileToken>{html.escape(self.profile_token)}</tptz:ProfileToken>"
                f'<tptz:Velocity><tt:PanTilt x="{x:.3f}" y="{y:.3f}" '
                'space="http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace"/>'
                "</tptz:Velocity></tptz:ContinuousMove>")
        self._ptz(body)

    def ptz_stop(self) -> None:
        body = ("<tptz:Stop>"
                f"<tptz:ProfileToken>{html.escape(self.profile_token)}</tptz:ProfileToken>"
                "<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom>"
                "</tptz:Stop>")
        self._ptz(body)

    def wait_still(self, timeout: float = 20.0) -> tuple[float, float]:
        """Poll position every 0.2s; still = moved <=0.002 for 0.6s (not MoveStatus)."""
        last = self.ptz_status()
        still_since = time.monotonic()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.2)
            pos = self.ptz_status()
            if abs(pos[0] - last[0]) <= 0.002 and abs(pos[1] - last[1]) <= 0.002:
                if time.monotonic() - still_since >= 0.6:
                    return pos
            else:
                still_since = time.monotonic()
            last = pos
        return last


# -- login / probe entry points -------------------------------------------------

def device_xaddr(host: str, port: int) -> str:
    return f"http://{host}:{port}{DEVICE_PATH}"


def login_at(host: str, username: str, password: str,
             xaddr: str | None = None) -> Camera:
    """Log in, trying the ONVIF ports for a bare host. A refused login stops at once."""
    if xaddr:
        return Camera(xaddr, username, password).login()
    last_error: OnvifError | None = None
    for port in ONVIF_PORTS:
        cam = Camera(device_xaddr(host, port), username, password, timeout=5.0)
        try:
            cam.sync_clock()
        except OnvifError as exc:
            if "refused the login" in str(exc):
                raise
            last_error = exc
            continue
        # clock answered on this port: now the real login (auth failure stops here)
        cam.get_device_info()
        return _finish_login(cam)
    raise last_error or OnvifError(f"can't reach the camera at {host}")


def _finish_login(cam: Camera) -> Camera:
    root = cam._post(
        cam.xaddr,
        "<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>")
    caps = _find(root, "Capabilities")
    cam._ptz_seen = False
    if caps is not None:
        for service in caps:
            name = _local(service.tag)
            if name == "Media":
                cam.media_xaddr = _text(service, "XAddr", cam.media_xaddr)
            elif name == "PTZ":
                cam.ptz_xaddr = _text(service, "XAddr", cam.ptz_xaddr)
                cam._ptz_seen = True
    cam.media_xaddr = cam.media_xaddr or cam.xaddr
    cam.get_profiles()
    cam.get_stream_uri()
    return cam


def probe_datetime(xaddr: str, timeout: float = 1.5) -> bool:
    """Unauthenticated GetSystemDateAndTime: does this host speak ONVIF?"""
    try:
        Camera(xaddr, "", "", timeout=timeout).sync_clock()
        return True
    except Exception:
        return False


# -- WS-Discovery -----------------------------------------------------------------

PROBE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
    'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
    "<s:Header><a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>"
    "<a:MessageID>urn:uuid:{mid}</a:MessageID>"
    "<a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To></s:Header>"
    "<s:Body><Probe xmlns=\"http://schemas.xmlsoap.org/ws/2005/04/discovery\">"
    "<Types>dn:NetworkVideoTransmitter</Types></Probe></s:Body></s:Envelope>"
)


def parse_probe_matches(raw: bytes, sender: str = "") -> list[dict]:
    """Canned-answer-friendly: XAddrs (sender's preferred), name/hardware scopes."""
    root = _parse_xml(raw)
    out: list[dict] = []
    for match in _find_all(root, "ProbeMatch"):
        xaddrs = (_text(match, "XAddrs") or "").split()
        chosen = ""
        for addr in xaddrs:
            host = urllib.parse.urlsplit(addr).hostname or ""
            if sender and host == sender:
                chosen = addr
                break
        if not chosen and xaddrs:
            chosen = xaddrs[0]
        scopes = (_text(match, "Scopes") or "").split()
        name = hardware = ""
        for scope in scopes:
            decoded = urllib.parse.unquote(scope)
            if "/name/" in decoded:
                name = decoded.split("/name/", 1)[1].replace("_", " ")
            elif "/hardware/" in decoded:
                hardware = decoded.split("/hardware/", 1)[1].replace("_", " ")
        host = urllib.parse.urlsplit(chosen).hostname if chosen else sender
        out.append({"id": f"onvif:{host}", "host": host, "xaddr": chosen,
                    "name": name or hardware, "hardware": hardware})
    return out


def discover_ws(timeout: float = 2.5) -> list[dict]:
    import uuid

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.5)
    message = PROBE.format(mid=uuid.uuid4()).encode("utf-8")
    try:
        sock.sendto(message, ("239.255.255.250", 3702))
        deadline = time.monotonic() + timeout
        found: dict[str, dict] = {}
        while time.monotonic() < deadline:
            try:
                raw, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                for dev in parse_probe_matches(raw, sender=addr[0]):
                    if dev["host"]:
                        found[dev["host"]] = dev
            except OnvifError:
                continue
        return list(found.values())
    finally:
        sock.close()


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("usage: python onvif_client.py <host> <username> <password>")
        raise SystemExit(2)
    camera = login_at(sys.argv[1], sys.argv[2], sys.argv[3])
    print(f"{camera.info.get('manufacturer', '')} {camera.info.get('model', '')} "
          f"firmware {camera.info.get('firmware', '')} · ptz={camera.has_ptz} · "
          f"profile={camera.profile_token}")
