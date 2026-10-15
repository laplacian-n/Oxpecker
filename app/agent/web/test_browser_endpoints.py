"""The live browser's HTTP surface: start, navigate, frame, stop.

The line these routes exist to keep sharp is who acted. The operator navigates through them and
is deliberately NOT broker-mediated — a person acting in their own hands is how an account gets
handed to the agent, and routing it through the approval machinery would mean asking the operator
to approve the operator. The agent's navigation is broker-mediated and does not come through here
at all.

What is NOT a difference is scope, and that is the assertion that matters most below: an operator
driving the shared profile is refused an out-of-scope URL exactly as the agent would be, because
the gate lives inside the browser and does not care who asked.
"""
from __future__ import annotations

import http.server
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from ..browser.service import browser_session
from ..isolated_state import isolated_state


class _Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h1 id=t>0</h1><script>let n=0;"
            b"setInterval(()=>{document.getElementById('t').textContent=++n},200)"
            b"</script></body></html>")

    def log_message(self, *a):
        pass


class BrowserEndpointsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import ipaddress  # noqa: F401 - used via the policy the server builds

        try:
            from ..broker.policy import Policy

            pol = Policy(engagement_id="probe", allowed_action_classes=set(),
                         allow_networks=[], allow_hostnames=set(), deny_networks=[],
                         deny_hostnames=set(), policy_version="v1", valid_until=time.time() + 60)
            with browser_session(pol):
                pass
        except Exception as e:  # noqa: BLE001
            raise unittest.SkipTest(
                f"no launchable Chromium ({type(e).__name__}: {str(e).splitlines()[0][:160]})")
        cls.in_scope = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Page)
        threading.Thread(target=cls.in_scope.serve_forever, daemon=True).start()
        cls.out_of_scope = http.server.ThreadingHTTPServer(("127.0.0.2", 0), _Page)
        threading.Thread(target=cls.out_of_scope.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.in_scope.shutdown()
        cls.out_of_scope.shutdown()

    def setUp(self):
        from . import dev_server as d
        from ..browser import live

        self.d = d
        self.live = live
        self.tmp = Path(tempfile.mkdtemp(prefix="browser-endpoints-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(live.stop_all)

        self._iso = isolated_state(self.tmp / "state")
        self._iso.__enter__()
        self.addCleanup(self._iso.__exit__, None, None, None)
        p = patch("agent.config.ENGAGEMENTS_ROOT", self.tmp / "engagements")
        p.start()
        self.addCleanup(p.stop)

        host, port = self.in_scope.server_address
        self.url = f"http://{host}:{port}/"
        self.bad_url = "http://{}:{}/".format(*self.out_of_scope.server_address)

        self.eng_id = "browser-endpoint-eng"
        eng = d.Engagement(engagement_id=self.eng_id, allow_targets=[f"{host}/32"],
                           allowed_action_classes=["browser_recon"], authorized_by="op")
        d._engagements[self.eng_id] = eng
        self.addCleanup(d._engagements.pop, self.eng_id, None)
        self.client = TestClient(d.app)

    def _start(self):
        r = self.client.post(f"/api/engagements/{self.eng_id}/browser/start")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    # -- the operator's door ---------------------------------------------------------------

    def test_start_opens_the_browser_and_begins_streaming(self):
        body = self._start()
        self.assertTrue(body["streaming"])
        self.assertTrue(self.live._browsers[self.eng_id].alive)

    def test_start_is_idempotent(self):
        self._start()
        first = self.live._browsers[self.eng_id]
        self._start()
        self.assertIs(self.live._browsers[self.eng_id], first,
                      "a second start replaced the browser, which would drop the operator's login")

    def test_the_operator_can_navigate_in_scope(self):
        self._start()
        r = self.client.post(f"/api/engagements/{self.eng_id}/browser/navigate",
                             json={"url": self.url})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"], r.text)

    def test_the_operator_is_refused_an_out_of_scope_url(self):
        """The assertion this file exists for. Operator navigation is not brokered, which must
        never be read as 'not gated': the egress handler inside the browser refuses the request
        whoever asked, so an operator cannot walk the shared profile out of the RoE."""
        self._start()
        r = self.client.post(f"/api/engagements/{self.eng_id}/browser/navigate",
                             json={"url": self.bad_url})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["ok"], f"an out-of-scope navigation succeeded: {r.text}")
        self.assertTrue(r.json().get("denied_requests"), r.text)

    def test_navigate_without_a_url_is_a_400(self):
        self._start()
        r = self.client.post(f"/api/engagements/{self.eng_id}/browser/navigate", json={})
        self.assertEqual(r.status_code, 400, r.text)

    def test_an_unknown_engagement_is_a_404(self):
        r = self.client.post("/api/engagements/no-such-engagement/browser/start")
        self.assertEqual(r.status_code, 404, r.text)

    # -- the frame ------------------------------------------------------------------------

    def test_a_frame_comes_back_as_a_jpeg_with_its_sequence(self):
        self._start()
        self.client.post(f"/api/engagements/{self.eng_id}/browser/navigate", json={"url": self.url})
        deadline = time.monotonic() + 15
        r = None
        while time.monotonic() < deadline:
            r = self.client.get(f"/api/engagements/{self.eng_id}/browser/frame")
            if r.status_code == 200:
                break
            time.sleep(0.2)
        self.assertEqual(r.status_code, 200, "no frame arrived within 15s")
        self.assertEqual(r.headers["content-type"], "image/jpeg")
        self.assertTrue(r.content.startswith(b"\xff\xd8\xff"), "not a JPEG")
        self.assertGreater(int(r.headers["X-Frame-Seq"]), 0)

    def test_asking_for_a_frame_you_already_have_is_a_204(self):
        """What makes polling cheap enough to feel live: a still page answers with nothing rather
        than resending a picture the viewer is already showing."""
        self._start()
        self.client.post(f"/api/engagements/{self.eng_id}/browser/navigate", json={"url": self.url})
        deadline = time.monotonic() + 15
        seq = 0
        while time.monotonic() < deadline:
            r = self.client.get(f"/api/engagements/{self.eng_id}/browser/frame")
            if r.status_code == 200:
                seq = int(r.headers["X-Frame-Seq"])
                break
            time.sleep(0.2)
        self.assertGreater(seq, 0, "no frame to begin with")

        # Freeze the stream FIRST, then read the sequence. Reading it before the stop races a
        # frame already in flight: it lands after the read, the sequence advances, and the 204
        # that the endpoint correctly withholds looks like a bug in the endpoint.
        self.client.post(f"/api/engagements/{self.eng_id}/browser/stop")
        settled = self.client.get(f"/api/engagements/{self.eng_id}/browser/frame")
        self.assertEqual(settled.status_code, 200, settled.text)
        seq = int(settled.headers["X-Frame-Seq"])

        again = self.client.get(f"/api/engagements/{self.eng_id}/browser/frame",
                                params={"since": seq})
        self.assertEqual(again.status_code, 204,
                         f"a frame the viewer already has was resent (seq {seq})")

    def test_a_frame_before_any_browser_exists_is_a_404(self):
        r = self.client.get(f"/api/engagements/{self.eng_id}/browser/frame")
        self.assertEqual(r.status_code, 404, r.text)

    # -- stopping -------------------------------------------------------------------------

    def test_stop_pauses_the_stream_but_keeps_the_browser(self):
        """Pausing the view must not cost the sign-in it exists to show: a bare session cookie
        dies with the browser process, so stop() stops streaming and nothing else."""
        self._start()
        r = self.client.post(f"/api/engagements/{self.eng_id}/browser/stop")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["streaming"])
        self.assertTrue(self.live._browsers[self.eng_id].alive,
                        "stop closed the browser, which would drop the operator's session")

    def test_stopping_a_browser_that_was_never_started_is_not_an_error(self):
        r = self.client.post(f"/api/engagements/{self.eng_id}/browser/stop")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["streaming"])


if __name__ == "__main__":
    unittest.main()
