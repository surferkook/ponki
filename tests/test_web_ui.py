"""Web UI tests (spec 10): one file, no secrets, routes and tokens agree."""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).parent.parent
PAGE = ROOT / "hub" / "static" / "index.html"


class Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str] = []
        self.links: list[str] = []
        self.metas: list[dict] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs) -> None:
        attrs = dict(attrs)
        if tag == "script":
            self.scripts.append(attrs.get("src", ""))
        if tag == "link":
            self.links.append(attrs.get("href", ""))
        if tag == "meta":
            self.metas.append(attrs)
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data) -> None:
        if self._in_title:
            self.title += data


def _vars(block: str) -> set[str]:
    return set(re.findall(r"(--[a-z0-9-]+)\s*:", block))


class WebUi(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = PAGE.read_text(encoding="utf-8")
        cls.parsed = Collector()
        cls.parsed.feed(cls.text)

    def test_file_shape(self) -> None:
        self.assertEqual(self.text.count("<style>"), 1)
        self.assertEqual(self.text.count("<script"), 1)
        self.assertIn("Ponki", self.parsed.title)
        self.assertTrue(any(m.get("name") == "viewport" for m in self.parsed.metas))

    def test_no_external_scripts_only_google_fonts(self) -> None:
        self.assertTrue(all(not src for src in self.parsed.scripts),
                        "no external script URLs")
        for href in self.parsed.links:
            if "stylesheet" in href or href.endswith(".css") or "css2" in href:
                self.assertIn("fonts.googleapis.com", href)

    def test_api_paths_exist_in_routes(self) -> None:
        from hub.main import create_app

        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(data_dir=Path(tmp), token="x")
        patterns = []
        for route in app.routes:
            path = getattr(route, "path", "")
            if path.startswith("/api/"):
                patterns.append([seg for seg in path.split("/") if seg])
        script = self.text.split("<script>", 1)[1]
        script = re.sub(r"\$\{[^}]*\}", "X", script)
        refs = set()
        for match in re.findall(r"/api/[A-Za-z0-9/._-]+", script):
            refs.add(match.rstrip("/."))
        missing = []
        for ref in sorted(refs):
            segs = [seg for seg in ref.split("/") if seg]
            ok = False
            for pattern in patterns:
                if len(pattern) != len(segs):
                    continue
                if all(p.startswith("{") or p == s for p, s in zip(pattern, segs)):
                    ok = True
                    break
            if not ok:
                missing.append(ref)
        self.assertEqual(missing, [])

    def test_light_and_dark_define_the_same_tokens(self) -> None:
        light = re.search(r":root\{(.*?)\}", self.text, re.S).group(1)
        dark = re.search(r':root\[data-theme="dark"\]\{(.*?)\}', self.text, re.S).group(1)
        self.assertEqual(_vars(light), _vars(dark))

    def test_every_emitted_kind_is_handled(self) -> None:
        kinds = set()
        for path in (ROOT / "hub").glob("*.py"):
            kinds.update(re.findall(r'emit\("([a-z_]+)"', path.read_text(encoding="utf-8")))
        for kind in kinds:
            self.assertIn(f'"{kind}"', self.text, f"event kind {kind} not handled in the UI")

    def test_portrait_endpoints_send_locked_down_csp(self) -> None:
        from fastapi.testclient import TestClient
        from hub.main import create_app

        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(data_dir=Path(tmp), token="x")
            client = TestClient(app, raise_server_exceptions=False)
            for url in ("/api/robots/eye/portrait.webp", "/api/departed/eye-1/portrait.webp"):
                response = client.get(url)
                self.assertIn("default-src 'none'",
                              response.headers.get("content-security-policy", ""), url)


if __name__ == "__main__":
    unittest.main()
