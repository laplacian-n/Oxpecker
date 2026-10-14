"""dev_server serves the new operator client (CLIENT_UI_DESIGN.md) at /app/, same-origin, so its
relative API calls and ?key= auth reach the backend with no CORS. These pin: the SPA entry and its
assets are served when the bundle is built; a server that has not built it gets a helpful 503, not
a blank page; /app redirects to /app/ (so ./assets/ resolves); and the path cannot escape dist.

Run directly: `python3 -m unittest agent.web.test_client_serving`.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .. import config
from . import dev_server


class ClientServingTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        self.tmp = Path(tempfile.mkdtemp(prefix="client-serving-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._key_patch = patch.object(config, "WEB_UI_API_KEY_FILE", self.tmp / "no-key.txt")
        self._key_patch.start()
        self.addCleanup(self._key_patch.stop)
        self.client = TestClient(dev_server.app)
        self.addCleanup(self.client.close)

    def _build_fake_dist(self) -> Path:
        dist = self.tmp / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<!doctype html><title>Oxpecker</title><div id=root></div>")
        (dist / "assets" / "index-abc.js").write_text("console.log('client')")
        return dist

    def test_serves_index_when_built(self):
        with patch.object(dev_server, "CLIENT_DIST", self._build_fake_dist()):
            resp = self.client.get("/app/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Oxpecker", resp.text)
        self.assertIn("id=root", resp.text)

    def test_serves_an_asset_when_built(self):
        with patch.object(dev_server, "CLIENT_DIST", self._build_fake_dist()):
            resp = self.client.get("/app/assets/index-abc.js")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("client", resp.text)

    def test_unknown_path_falls_back_to_index_for_the_spa(self):
        with patch.object(dev_server, "CLIENT_DIST", self._build_fake_dist()):
            resp = self.client.get("/app/some/deep/route")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("id=root", resp.text)  # HashRouter entry, not a 404

    def test_helpful_503_when_not_built(self):
        with patch.object(dev_server, "CLIENT_DIST", self.tmp / "does-not-exist"):
            resp = self.client.get("/app/")
        self.assertEqual(resp.status_code, 503)
        self.assertIn("not built", resp.text)

    def test_bare_app_redirects_to_trailing_slash(self):
        resp = self.client.get("/app", follow_redirects=False)
        self.assertIn(resp.status_code, (307, 308))
        self.assertEqual(resp.headers["location"], "/app/")

    def test_path_cannot_escape_dist(self):
        dist = self._build_fake_dist()
        secret = self.tmp / "secret.txt"
        secret.write_text("TOP SECRET")
        with patch.object(dev_server, "CLIENT_DIST", dist):
            resp = self.client.get("/app/../secret.txt")
        # a traversal attempt resolves outside dist, so it is not served as a file — it falls back
        # to the SPA index instead of leaking the file
        self.assertNotIn("TOP SECRET", resp.text)


if __name__ == "__main__":
    unittest.main()
