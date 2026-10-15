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

    def test_root_redirects_to_the_operator_client(self):
        """/ is the operator client's door; the classic single-file UI moved to /classic.

        A redirect rather than serving the client's index.html at /, because the built shell asks
        for ./assets/... (vite base "./"), which only resolves under a path ending in '/'.
        """
        resp = self.client.get("/", follow_redirects=False)
        self.assertEqual(resp.status_code, 307)
        self.assertEqual(resp.headers["location"], "/app/")

    def test_the_redirect_does_not_depend_on_whether_the_client_was_built(self):
        """Both ways round, which is the whole point of making it unconditional.

        "Serve the client when it is built, the classic UI otherwise" was the obvious design and
        is the wrong one: / would then behave differently on a machine that has run the build and
        one that has not — a developer's checkout versus CI, or the same box before and after
        `npm run build` — and every test touching / would silently depend on that. /app/ is the
        one place that handles an unbuilt client, and it links to /classic.
        """
        with patch.object(dev_server, "CLIENT_DIST", self._build_fake_dist()):
            built = self.client.get("/", follow_redirects=False)
        with patch.object(dev_server, "CLIENT_DIST", self.tmp / "does-not-exist"):
            unbuilt = self.client.get("/", follow_redirects=False)
        self.assertEqual((built.status_code, built.headers["location"]), (307, "/app/"))
        self.assertEqual((unbuilt.status_code, unbuilt.headers["location"]), (307, "/app/"))

    def test_the_classic_ui_is_retired_to_classic_and_still_served(self):
        # Retired, not deleted: the unbuilt-client page below links to it, so it has to be there
        # for that link to mean anything, and it is still the only UI with features the operator
        # client has only just grown.
        resp = self.client.get("/classic")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])
        self.assertIn("<title>Oxpecker</title>", resp.text)
        # A living dashboard read from disk per request: a cached copy reads as "nothing changed"
        # after the server's UI is updated.
        self.assertEqual(resp.headers.get("cache-control"), "no-store")

    def test_the_unbuilt_page_points_at_an_address_that_exists(self):
        """It used to point at /, which now redirects back to the page showing the message.

        Asserted by following the link rather than by matching the href text: a link that reads
        /classic and 404s is the same dead end as one that loops.
        """
        with patch.object(dev_server, "CLIENT_DIST", self.tmp / "does-not-exist"):
            resp = self.client.get("/app/")
        self.assertEqual(resp.status_code, 503)
        self.assertIn("/classic", resp.text)
        self.assertNotIn("href='/'", resp.text)
        self.assertEqual(self.client.get("/classic").status_code, 200)

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


class EnsureClientBuiltTest(unittest.TestCase):
    """_ensure_client_built gives one-command launch a working UI: it builds the bundle when it is
    missing and npm is present, and is never fatal otherwise."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ensure-build-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "client").mkdir()
        (self.tmp / "client" / "package.json").write_text("{}")
        self.dist = self.tmp / "client" / "dist"

    def _patch_dist(self):
        return patch.object(dev_server, "CLIENT_DIST", self.dist)

    def test_builds_when_missing_and_npm_present(self):
        calls = []

        def runner(args, **kw):
            calls.append(args)
            if args[:2] == ["npm", "install"]:
                (self.tmp / "client" / "node_modules").mkdir(exist_ok=True)

        with self._patch_dist(), patch("shutil.which", return_value="/usr/bin/npm"):
            ran = dev_server._ensure_client_built(runner=runner)
        self.assertTrue(ran)
        self.assertIn(["npm", "install"], calls)
        self.assertIn(["npm", "run", "build"], calls)

    def test_skips_when_already_built(self):
        self.dist.mkdir(parents=True)
        called = []
        with self._patch_dist():
            ran = dev_server._ensure_client_built(runner=lambda *a, **k: called.append(a))
        self.assertFalse(ran)
        self.assertEqual(called, [])

    def test_no_npm_is_not_fatal(self):
        with self._patch_dist(), patch("shutil.which", return_value=None):
            ran = dev_server._ensure_client_built(runner=lambda *a, **k: self.fail("must not run"))
        self.assertFalse(ran)

    def test_a_build_failure_is_swallowed(self):
        def boom(*a, **k):
            raise RuntimeError("npm exploded")
        with self._patch_dist(), patch("shutil.which", return_value="/usr/bin/npm"):
            ran = dev_server._ensure_client_built(runner=boom)
        self.assertFalse(ran)  # logged, not raised


if __name__ == "__main__":
    unittest.main()
