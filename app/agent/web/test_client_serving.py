"""The front door is the classic single-file UI at /, and the React operator client that once
lived at /app/ is retired AND deleted: every path under /app redirects to /. These pin both
halves — that / serves the classic UI (one committed HTML document, no build step), and that
nothing under /app can strand an operator in a UI that no longer exists — so the retirement cannot
silently regress into trying to serve a client that is not there any more.

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

    def test_root_serves_the_classic_ui(self):
        """/ is the classic single-file UI's door — the whole product, served directly (not a
        redirect). It is one self-contained HTML document read from disk per request, so it is the
        same front door on every machine; there is no bundle to build."""
        resp = self.client.get("/", follow_redirects=False)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("<!doctype html>", resp.text.lower())
        self.assertIn("<title>Oxpecker</title>", resp.text)
        self.assertEqual(resp.headers.get("cache-control"), "no-store")

    def test_classic_alias_also_serves_it(self):
        # /classic is kept as an alias of /, read from disk per request (a cached copy reads as
        # "nothing changed" after the server's UI is updated).
        resp = self.client.get("/classic")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])
        self.assertIn("<title>Oxpecker</title>", resp.text)
        self.assertEqual(resp.headers.get("cache-control"), "no-store")

    def test_app_root_redirects_to_the_classic_ui(self):
        """/app/ does not serve anything of its own any more; it sends the operator to / . This is
        the un-trap: a desktop build still opening /app/, or a bookmark, lands on the UI that
        exists rather than one that was removed."""
        for p in ("/app", "/app/"):
            resp = self.client.get(p, follow_redirects=False)
            self.assertIn(resp.status_code, (307, 308), p)
            self.assertEqual(resp.headers["location"], "/", p)

    def test_every_path_under_app_redirects_including_old_asset_urls(self):
        """Not just the root: a deep route or an old ./assets/ URL redirects too, so nothing of the
        removed SPA can load (a half-loaded shell that then 404s its own assets is its own trap)."""
        for p in ("/app/some/deep/route", "/app/assets/index-abc.js", "/app/flow"):
            resp = self.client.get(p, follow_redirects=False)
            self.assertIn(resp.status_code, (307, 308), p)
            self.assertEqual(resp.headers["location"], "/", p)


if __name__ == "__main__":
    unittest.main()
