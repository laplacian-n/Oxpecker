"""The "this computer" isolation handoff: the server authorises a tool, parks its I/O, and the
desktop app runs it and posts the result back.

Two layers are covered:
  - the broker (client_jobs.ClientJobBroker) in isolation — round trip, not-connected, timeout;
  - dev_server's seam — a session with isolation="this_computer" delegates run_command to the
    broker instead of its sandbox, and the HTTP job channel (/api/client-jobs/*) carries it.

Run directly: `python3 -m unittest agent.web.test_client_jobs`.
"""
from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from . import client_jobs
from . import dev_server


class BrokerTest(unittest.TestCase):
    def test_round_trip(self):
        broker = client_jobs.ClientJobBroker()
        result_box = {}

        def submitter():
            result_box["r"] = broker.submit("s1", "run_command", {"argv": ["echo", "hi"]},
                                            timeout_s=5.0)

        t = threading.Thread(target=submitter)
        t.start()
        job = broker.claim(wait_s=2.0)
        self.assertIsNotNone(job)
        self.assertEqual(job["kind"], "run_command")
        self.assertEqual(job["session_id"], "s1")
        self.assertEqual(job["payload"]["argv"], ["echo", "hi"])
        self.assertTrue(broker.deliver(job["id"], {"ok": True, "output": "hi"}))
        t.join(timeout=5)
        self.assertEqual(result_box["r"], {"ok": True, "output": "hi"})

    def test_not_connected_fails_fast(self):
        broker = client_jobs.ClientJobBroker()
        with patch.object(client_jobs, "CONNECT_GRACE_S", 0.2):
            t0 = time.time()
            with self.assertRaises(client_jobs.ExecutorNotConnected):
                broker.submit("s1", "run_command", {"argv": ["echo"]}, timeout_s=10.0)
            # Gave up on the grace, not the (much longer) tool timeout.
            self.assertLess(time.time() - t0, 3.0)

    def test_claimed_but_no_result_times_out(self):
        broker = client_jobs.ClientJobBroker()
        box = {}

        def submitter():
            try:
                broker.submit("s1", "run_command", {"argv": ["sleep"]}, timeout_s=0.3)
            except client_jobs.ClientJobTimeout:
                box["timed_out"] = True

        t = threading.Thread(target=submitter)
        t.start()
        job = broker.claim(wait_s=2.0)          # claim, but never deliver
        self.assertIsNotNone(job)
        t.join(timeout=5)
        self.assertTrue(box.get("timed_out"))

    def test_claim_times_out_with_no_jobs(self):
        broker = client_jobs.ClientJobBroker()
        t0 = time.time()
        self.assertIsNone(broker.claim(wait_s=0.2))
        self.assertLess(time.time() - t0, 2.0)

    def test_deliver_unknown_job_is_false(self):
        broker = client_jobs.ClientJobBroker()
        self.assertFalse(broker.deliver("nope", {"ok": True}))


class DelegationTest(unittest.TestCase):
    def _session(self, isolation):
        return dev_server.Session(session_id="t-" + isolation, isolation=isolation)

    def test_this_computer_run_command_delegates(self):
        """A this_computer session hands run_command to the broker; a fake executor answers."""
        sess = self._session("this_computer")
        done = threading.Event()

        def fake_executor():
            job = dev_server._CLIENT_JOBS.claim(wait_s=3.0)
            if job:
                dev_server._CLIENT_JOBS.deliver(
                    job["id"], {"ok": True, "output": "from-client", "exit_code": 0})
            done.set()

        t = threading.Thread(target=fake_executor)
        t.start()
        res = dev_server._run_tool("run_command", {"argv": ["echo", "hi"]}, sess)
        done.wait(timeout=5)
        t.join(timeout=5)
        self.assertTrue(res["ok"])
        self.assertEqual(res["output"], "from-client")
        self.assertEqual(res.get("isolation"), "this_computer")

    def test_this_computer_not_connected_is_clean_error(self):
        sess = self._session("this_computer")
        with patch.object(client_jobs, "CONNECT_GRACE_S", 0.2):
            res = dev_server._run_tool("run_command", {"argv": ["echo", "hi"]}, sess)
        self.assertFalse(res["ok"])
        self.assertFalse(res["executor_connected"])
        self.assertIn("desktop app", res["error"])

    def test_this_computer_still_screens_destructive(self):
        """Delegation happens only AFTER the server-side screens — a destructive command is
        refused before any job is parked, so no executor is even needed."""
        sess = self._session("this_computer")
        res = dev_server._run_tool("run_command", {"command": "rm -rf /"}, sess)
        self.assertFalse(res["ok"])
        self.assertIn("destructive", res["error"])


if __name__ == "__main__":
    unittest.main()


class JobChannelHttpTest(unittest.TestCase):
    """The desktop app's HTTP view of the broker: /next long-polls, /result hands back."""

    def setUp(self):
        import shutil
        import tempfile
        from pathlib import Path
        from fastapi.testclient import TestClient
        from .. import config

        tmp = Path(tempfile.mkdtemp(prefix="client-jobs-http-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        # No key file -> middleware passes through, so the channel is reachable without a header
        # here; auth itself is covered by test_dev_server_auth.
        p = patch.object(config, "WEB_UI_API_KEY_FILE", tmp / "no-key.txt")
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(dev_server.app)
        self.addCleanup(self.client.close)

    def test_next_empty_returns_null_job(self):
        r = self.client.get("/api/client-jobs/next", params={"wait": 0.1})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["job"])

    def test_submit_claim_deliver_over_http(self):
        sess = dev_server.Session(session_id="http-tc", isolation="this_computer")
        box = {}

        def submitter():
            box["r"] = dev_server._run_tool("run_command", {"argv": ["echo", "ok"]}, sess)

        t = threading.Thread(target=submitter)
        t.start()
        # Poll the channel the way the desktop app does.
        job = None
        for _ in range(30):
            job = self.client.get("/api/client-jobs/next", params={"wait": 0.5}).json()["job"]
            if job:
                break
        self.assertIsNotNone(job)
        self.assertEqual(job["kind"], "run_command")
        r = self.client.post(f"/api/client-jobs/{job['id']}/result",
                             json={"result": {"ok": True, "output": "ok"}})
        self.assertTrue(r.json()["delivered"])
        t.join(timeout=5)
        self.assertEqual(box["r"]["output"], "ok")
