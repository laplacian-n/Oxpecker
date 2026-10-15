"""The persistent per-engagement browser: it must persist, it must stay in scope, and one
engagement's logged-in session must never be reachable from another.

Break-it-first throughout. The two properties that matter pull against each other — a profile
that survives is what makes a login reusable, and it is also what makes a leak between
engagements catastrophic — so both directions are asserted, not just the happy one.
"""
from __future__ import annotations

import http.server
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ..broker.policy import Policy
from . import live
from .service import browser_session


class _CookieHandler(http.server.BaseHTTPRequestHandler):
    """Sets a cookie on /set, and reports what came back on /check."""

    def do_GET(self):  # noqa: N802 - stdlib's spelling
        got = self.headers.get("Cookie", "")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        if self.path.startswith("/set-persistent"):
            # A real login's "remember me" cookie: it carries an expiry, so the browser writes it
            # into the profile on disk.
            self.send_header("Set-Cookie", "session=handed-over; Path=/; Max-Age=86400")
        elif self.path.startswith("/set"):
            # A bare session cookie: no expiry, so it lives in memory for the life of the browser
            # process and is never written to the profile.
            self.send_header("Set-Cookie", "session=handed-over; Path=/")
        self.end_headers()
        if self.path.startswith("/ticking"):
            # Repaints on its own five times a second, with nothing driving it from our side.
            self.wfile.write(
                b"<html><body><h1 id=t>0</h1><script>let n=0;"
                b"setInterval(()=>{document.getElementById('t').textContent=++n},200)"
                b"</script></body></html>")
            return
        self.wfile.write(f"<html><body>COOKIE[{got}]</body></html>".encode())

    def do_GET_repaint(self):  # pragma: no cover - routed from do_GET
        pass

    def log_message(self, *a):  # keep the test output readable
        pass


def _start_server(host: str) -> http.server.ThreadingHTTPServer:
    srv = http.server.ThreadingHTTPServer((host, 0), _CookieHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _policy_allowing(host: str) -> Policy:
    import ipaddress

    return Policy(
        engagement_id="live-test",
        allowed_action_classes={"passive_recon", "browser_recon"},
        allow_networks=[ipaddress.ip_network(f"{host}/32")],
        allow_hostnames=set(),
        deny_networks=[],
        deny_hostnames=set(),
        policy_version="v1",
        valid_until=time.time() + 3600,
    )


class LiveBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Same convention as test_service.py: a host without a launchable Chromium skips rather
        # than reporting failures that look like defects in the thing under test.
        try:
            with browser_session(_policy_allowing("127.0.0.1")):
                pass
        except Exception as e:  # noqa: BLE001
            raise unittest.SkipTest(
                f"no launchable Chromium ({type(e).__name__}: {str(e).splitlines()[0][:160]}). "
                "Set OXPECKER_BROWSER_EXECUTABLE to a Chromium binary to run these."
            )
        cls.in_scope = _start_server("127.0.0.1")
        cls.out_of_scope = _start_server("127.0.0.2")

    @classmethod
    def tearDownClass(cls):
        cls.in_scope.shutdown()
        cls.out_of_scope.shutdown()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="live-browser-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(live.stop_all)
        self.policy = _policy_allowing("127.0.0.1")

    def _url(self, path="/", server=None):
        srv = server or self.in_scope
        h, p = srv.server_address[0], srv.server_address[1]
        return f"http://{h}:{p}{path}"

    def _engagement(self, name):
        d = self.tmp / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _goto(url):
        def fn(page):
            page.goto(url, timeout=15000)
            return page.content()
        return fn

    # -- the point of the thing ------------------------------------------------------------

    def test_a_cookie_set_on_one_call_is_still_there_on_the_next(self):
        """The whole reason this path exists: sign in once, and the agent's later calls are that
        session. browser_fetch cannot do this and must not — test_service pins that."""
        d = self._engagement("eng-a")
        b = live.get("eng-a", d, self.policy)
        b.call(self._goto(self._url("/set")))
        body = b.call(self._goto(self._url("/check")))
        self.assertIn("session=handed-over", body, body[:200])

    def test_a_cookie_with_an_expiry_survives_the_browser_being_restarted(self):
        """Persistence is the profile on disk, not the live process: a login whose cookie carries
        an expiry — a "remember me" — is still there after the browser is recycled."""
        d = self._engagement("eng-restart")
        b = live.get("eng-restart", d, self.policy)
        b.call(self._goto(self._url("/set-persistent")))
        live.stop("eng-restart")

        b2 = live.get("eng-restart", d, self.policy)
        self.assertIsNot(b2, b)
        body = b2.call(self._goto(self._url("/check")))
        self.assertIn("session=handed-over", body, body[:200])

    def test_a_bare_session_cookie_does_NOT_survive_a_restart(self):
        """The limit of the promise, pinned so nobody discovers it mid-engagement.

        A cookie with no expiry lives in the browser process's memory and is never written to the
        profile, so stopping the browser ends that login — persistent profile or not. This is
        Chromium's behaviour, not something this module chose, and plenty of real sites issue
        exactly this kind of cookie. The operational consequence: do not stop an engagement's
        browser between the operator signing in and the agent finishing with it.

        Making these survive is possible — read the cookies out on stop and put them back on
        start — but that writes live auth material into the engagement directory as a deliberate
        act, which is a decision for the operator to take knowingly, not a default."""
        d = self._engagement("eng-session-cookie")
        b = live.get("eng-session-cookie", d, self.policy)
        b.call(self._goto(self._url("/set")))
        self.assertIn("session=handed-over", b.call(self._goto(self._url("/check"))))

        live.stop("eng-session-cookie")
        body = live.get("eng-session-cookie", d, self.policy).call(self._goto(self._url("/check")))
        self.assertNotIn("handed-over", body, body[:200])

    def test_one_engagements_session_is_not_reachable_from_another(self):
        """The other direction, and the one that would be a real incident: a login handed to
        engagement A must never ride along into engagement B's traffic."""
        a, b = self._engagement("eng-1"), self._engagement("eng-2")
        live.get("eng-1", a, self.policy).call(self._goto(self._url("/set")))
        body = live.get("eng-2", b, self.policy).call(self._goto(self._url("/check")))
        self.assertNotIn("handed-over", body, body[:200])

    def test_the_profile_lives_inside_the_engagement_directory(self):
        d = self._engagement("eng-where")
        live.get("eng-where", d, self.policy).call(self._goto(self._url("/")))
        self.assertTrue((d / "browser_profile").is_dir(),
                        f"no profile under {d}: {list(d.iterdir())}")

    # -- a page per worker, one profile -----------------------------------------------------

    @staticmethod
    def _url_now():
        def fn(page):
            return page.url
        return fn

    def test_each_worker_drives_its_own_page_and_does_not_move_anothers(self):
        """The semantic that makes parallel workers safe on one browser: worker A navigating must
        not drag worker B's page with it. Without a page per worker, B's next click lands on A's
        destination — a wrong result, not a slow one."""
        d = self._engagement("eng-workers")
        b = live.get("eng-workers", d, self.policy)
        b.call(self._goto(self._url("/set")), worker="w0")
        b.call(self._goto(self._url("/check")), worker="w1")
        w0, w1 = b.call(self._url_now(), worker="w0"), b.call(self._url_now(), worker="w1")
        self.assertTrue(w0.endswith("/set"), w0)
        self.assertTrue(w1.endswith("/check"), w1)
        self.assertNotEqual(w0, w1)

    def test_workers_share_the_one_logged_in_profile(self):
        """The other half of the split: separate pages, one cookie jar. A login one worker holds
        is a login every worker holds, because the context — not the page — owns the cookies."""
        d = self._engagement("eng-share-cookie")
        b = live.get("eng-share-cookie", d, self.policy)
        b.call(self._goto(self._url("/set")), worker="w0")       # w0 signs in
        body = b.call(self._goto(self._url("/check")), worker="w1")  # w1 is already signed in
        self.assertIn("session=handed-over", body, body[:200])

    def test_the_operator_page_is_not_whatever_a_worker_last_touched(self):
        """The operator watches their own page, not a random worker's. A worker navigating away
        must leave the operator's page where the operator left it."""
        d = self._engagement("eng-operator-page")
        b = live.get("eng-operator-page", d, self.policy)
        b.call(self._goto(self._url("/check")))  # operator (worker=None)
        b.call(self._goto(self._url("/set")), worker="w0")
        self.assertTrue(b.call(self._url_now()).endswith("/check"))

    def test_a_fresh_worker_page_is_scope_gated_too(self):
        """A page created later for a new worker must carry the same egress gate — the gate is on
        the context, so it should, but this is the assertion that keeps it that way."""
        d = self._engagement("eng-worker-scope")
        b = live.get("eng-worker-scope", d, self.policy)
        with self.assertRaises(Exception):
            b.call(self._goto(self._url("/", server=self.out_of_scope)), worker="w9")
        self.assertTrue(b.denied_requests, "the request was not recorded as denied")
        self.assertIn("127.0.0.2", str(b.denied_requests))

    def test_closing_a_worker_page_frees_it_and_spares_the_operator(self):
        """A finished worker's page is closed so pages do not pile up for the whole engagement;
        the operator's page is never closed this way, and a closed worker id is reusable."""
        d = self._engagement("eng-close-page")
        b = live.get("eng-close-page", d, self.policy)
        b.call(self._goto(self._url("/")), worker="w0")
        b.close_page("w0")
        b.close_page(live.OPERATOR_PAGE)  # refused: the operator's page survives
        self.assertIn("COOKIE[", b.call(self._goto(self._url("/"))))        # operator still works
        self.assertIn("COOKIE[", b.call(self._goto(self._url("/")), worker="w0"))  # w0 recreated

    def test_close_worker_page_is_a_safe_no_op_without_a_browser(self):
        """A worker that never drove the browser, on an engagement with none running, must not
        raise — the wave calls this in its finally for every worker."""
        live.close_worker_page("no-such-engagement", "w0")  # must not raise

    def test_close_worker_page_closes_that_workers_page_only(self):
        d = self._engagement("eng-reg-close")
        b = live.get("eng-reg-close", d, self.policy)
        b.call(self._goto(self._url("/")), worker="w0")
        live.close_worker_page("eng-reg-close", "w0")
        self.assertNotIn("w0", b._pages)                       # the worker page is gone
        self.assertIn(live.OPERATOR_PAGE, b._pages)            # the operator's survives
        self.assertTrue(b.alive)                               # the browser is left running

    # -- the gate is the same gate ---------------------------------------------------------

    def test_an_out_of_scope_navigation_is_blocked_on_the_persistent_path_too(self):
        """A persistent profile must not come with a weaker egress gate. This is the same
        context.route handler the one-shot path uses; if it ever stops being wired up here, the
        login-holding browser becomes the one that can reach anything."""
        d = self._engagement("eng-scope")
        b = live.get("eng-scope", d, self.policy)
        with self.assertRaises(Exception):
            b.call(self._goto(self._url("/", server=self.out_of_scope)))
        self.assertTrue(b.denied_requests, "the request was not recorded as denied")
        self.assertIn("127.0.0.2", str(b.denied_requests))

    def test_an_in_scope_navigation_still_succeeds(self):
        """The other direction: a gate that blocks everything would pass the test above."""
        d = self._engagement("eng-ok")
        body = live.get("eng-ok", d, self.policy).call(self._goto(self._url("/")))
        self.assertIn("COOKIE[", body)

    # -- the registry ----------------------------------------------------------------------

    def test_concurrent_first_callers_get_exactly_one_browser(self):
        """Read-then-create is one operation or it is a bug. Two Chromiums opened on the same
        profile directory is a corrupted profile, not just wasted work."""
        d = self._engagement("eng-race")
        barrier = threading.Barrier(6)
        got: list = []
        errors: list = []

        def grab():
            try:
                barrier.wait(timeout=30)
                got.append(live.get("eng-race", d, self.policy))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=grab) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(errors, [], str(errors[:2]))
        self.assertEqual(len(got), 6)
        self.assertEqual(len({id(b) for b in got}), 1,
                         "more than one browser was started for one engagement")

    def test_a_page_can_be_driven_from_a_thread_that_did_not_create_it(self):
        """The reason this module owns a thread at all: Playwright's sync objects raise
        'cannot switch to a different thread' when driven from another one, and the web runtime
        serves requests on many threads. If this ever regresses, every request but the first
        fails in a way that looks like a browser bug."""
        d = self._engagement("eng-threads")
        b = live.get("eng-threads", d, self.policy)
        b.call(self._goto(self._url("/")))  # first use on this thread

        out: list = []

        def other_thread():
            try:
                out.append(b.call(self._goto(self._url("/"))))
            except Exception as e:  # noqa: BLE001
                out.append(e)

        t = threading.Thread(target=other_thread)
        t.start()
        t.join(timeout=60)
        self.assertTrue(out, "the other thread produced nothing")
        self.assertNotIsInstance(out[0], BaseException, f"cross-thread call failed: {out[0]!r}")
        self.assertIn("COOKIE[", out[0])

    def test_using_a_stopped_browser_is_an_error_not_a_hang(self):
        d = self._engagement("eng-stopped")
        b = live.get("eng-stopped", d, self.policy)
        b.call(self._goto(self._url("/")))
        b.stop()
        with self.assertRaises(live.BrowserUnavailableError):
            b.call(self._goto(self._url("/")))

    # -- the live view ---------------------------------------------------------------------

    def test_frames_arrive_while_nobody_is_driving_the_browser(self):
        """The property the pump exists for, and the one most likely to regress silently.

        Sync Playwright dispatches events only while the owning thread is inside a Playwright
        call. Measured on this machine: a thread parked on its command queue received 0 frames in
        3 seconds from a page repainting five times a second; the same window with a cheap call
        every 100ms delivered 18. Without the pump the live view updates only when someone
        happens to issue a command, which looks like a flaky browser rather than a design flaw.
        """
        d = self._engagement("eng-cast")
        b = live.get("eng-cast", d, self.policy)
        b.call(self._goto(self._url("/ticking")))
        b.start_screencast(quality=40, max_width=640, max_height=480)
        self.assertTrue(b.screencasting)

        # No calls at all in this window: whatever arrives, the pump delivered.
        time.sleep(2.0)
        first = b.latest_frame()
        self.assertIsNotNone(first, "no frame arrived while the browser was idle")
        jpeg, seq, meta = first
        self.assertTrue(jpeg.startswith(b"\xff\xd8\xff"), "not a JPEG")
        self.assertGreater(seq, 0)
        self.assertIn("deviceWidth", meta)

        time.sleep(1.0)
        later = b.latest_frame()
        self.assertGreater(later[1], seq, "the frame did not advance on a repainting page")

    def test_stopping_the_screencast_keeps_the_last_frame(self):
        """A paused view should hold the last thing it saw rather than going blank — and the
        thread must go back to parking on its queue, with no pump and no cost."""
        d = self._engagement("eng-cast-stop")
        b = live.get("eng-cast-stop", d, self.policy)
        b.call(self._goto(self._url("/ticking")))
        b.start_screencast(quality=40, max_width=640, max_height=480)
        time.sleep(1.5)
        self.assertIsNotNone(b.latest_frame(), "no frame to begin with")
        seq = b.latest_frame()[1]

        b.stop_screencast()
        self.assertFalse(b.screencasting)
        self.assertIsNotNone(b.latest_frame(), "the last frame was thrown away")

        time.sleep(1.0)
        self.assertEqual(b.latest_frame()[1], seq,
                         "frames kept arriving after the screencast was stopped")

    def test_there_is_no_frame_before_the_screencast_starts(self):
        """A viewer must be able to tell "nothing yet" from "a stale picture"."""
        d = self._engagement("eng-cast-none")
        b = live.get("eng-cast-none", d, self.policy)
        b.call(self._goto(self._url("/")))
        self.assertIsNone(b.latest_frame())
        self.assertFalse(b.screencasting)


if __name__ == "__main__":
    unittest.main()
