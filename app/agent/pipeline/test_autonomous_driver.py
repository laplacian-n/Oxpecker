"""Tests for the autonomous pipeline driver (agent/pipeline/autonomous_driver.py) — added
2026-09-01 so the "autonomous"/"consult" modes don't need a human hand-gluing each phase, which
a real end-to-end run against Juice Shop required before these existed.

RECON's deterministic tasks run for real against the Juice Shop lab container (same
"real targets over mocks" preference agent/pipeline/test_executor.py already established, and
the same reason: this driver's whole point is that planned work actually reaches a real broker
and updates real state). ANALYSIS/VALIDATION's judgment-call tasks mock AgentLoop entirely (no
live llama-server needed) — the mock's job is to simulate what a real model turn would do by
directly calling the same EngagementStore/FindingsStore methods the real
record_hypothesis/update_hypothesis_status/record_finding MCP tools call, so the driver's own
"did the expected state actually change" detection gets genuinely exercised, not bypassed.
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

from .. import config
from ..broker.consult_queue import ConsultQueue
from ..engagement.store import EngagementStore
from ..loop import TaskResult
from .autonomous_driver import (
    MAX_JUDGMENT_TASK_ATTEMPTS,
    AutonomousDriver,
)


def _juiceshop_reachable() -> bool:
    try:
        urllib.request.urlopen("http://127.0.0.1:3000/", timeout=3)
        return True
    except (urllib.error.URLError, ConnectionRefusedError):
        return False


class AutonomousDriverTestBase(unittest.TestCase):
    def setUp(self):
        if not _juiceshop_reachable():
            self.skipTest("Juice Shop lab container not reachable at 127.0.0.1:3000")
        self.tmp = Path(tempfile.mkdtemp(prefix="autonomous-driver-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.engagement_id = f"autonomous-test-{time.time_ns()}"
        self.engagement_dir = self.tmp / self.engagement_id
        self._engagements_root_patcher = patch(
            "agent.pipeline.autonomous_driver.config.ENGAGEMENTS_ROOT", self.tmp
        )
        self._engagements_root_patcher.start()
        self.addCleanup(self._engagements_root_patcher.stop)
        self.store = EngagementStore(self.engagement_dir)
        self.store.upsert_asset("host", "127.0.0.1", metadata={"url": "http://127.0.0.1:3000"})
        self.events = []

    def _write_roe(self, valid_until: str = "2099-01-01T00:00:00Z") -> None:
        self.engagement_dir.mkdir(parents=True, exist_ok=True)
        (self.engagement_dir / "roe.json").write_text(
            f'{{"engagement_id": "{self.engagement_id}", "valid_until": "{valid_until}"}}'
        )

    def _make_driver(self, mode: str = "autonomous", **kwargs) -> AutonomousDriver:
        return AutonomousDriver(
            engagement_id=self.engagement_id, profile_name="web_api", mode=mode,
            session_id=f"session-{self.engagement_id}", on_event=self.events.append, **kwargs,
        )


def _mock_agent_loop_factory(engagement_dir: Path, on_review, on_validate):
    """Builds a stand-in for autonomous_driver.AgentLoop: run_task() dispatches on the prompt's
    phase marker to the given callback, which mutates engagement state directly the way a real
    model tool call would, then returns a real TaskResult."""

    def factory(**kwargs):
        mock_loop = MagicMock()

        def run_task(prompt: str):
            store = EngagementStore(engagement_dir)
            if "ANALYSIS phase" in prompt:
                return on_review(store)
            if "VALIDATION phase" in prompt:
                hyp_id = prompt.split("hypothesis_id=")[-1].strip("'.")
                return on_validate(store, hyp_id)
            raise AssertionError(f"unexpected prompt: {prompt!r}")

        mock_loop.run_task.side_effect = run_task
        mock_loop.close.return_value = None
        return mock_loop

    return factory


class TestFullRunToCloseout(AutonomousDriverTestBase):
    def test_autonomous_mode_reaches_closeout_with_a_confirmed_finding(self):
        self._write_roe()

        def on_review(store: EngagementStore) -> TaskResult:
            store.create_hypothesis("Missing HSTS", "no Strict-Transport-Security header seen")
            return TaskResult("ok", "formed 1 hypothesis")

        def on_validate(store: EngagementStore, hyp_id: str) -> TaskResult:
            current = store.get_hypothesis(hyp_id)
            store.update_hypothesis(
                hyp_id, expected_version=current["version"], status="confirmed",
                add_evidence_for="re-fetch confirmed header absent",
            )
            return TaskResult("ok", "confirmed")

        factory = _mock_agent_loop_factory(self.engagement_dir, on_review, on_validate)
        with patch("agent.pipeline.autonomous_driver.AgentLoop", side_effect=factory):
            driver = self._make_driver()
            result = driver.run()

        self.assertEqual(result.status, "closed_out")
        self.assertEqual(result.final_phase, "CLOSEOUT")
        self.assertIn("RECON", result.phases_completed)
        self.assertIn("ANALYSIS", result.phases_completed)
        self.assertIn("VALIDATION", result.phases_completed)
        hyps = self.store.list_hypotheses(status="confirmed")
        self.assertEqual(len(hyps), 1)
        report_path = self.engagement_dir / "report.md"
        self.assertTrue(report_path.exists())
        event_types = [e["type"] for e in self.events]
        self.assertIn("phase_advanced", event_types)
        self.assertIn("run_complete", event_types)

    def test_validate_hypothesis_prompt_names_the_in_scope_target(self):
        # Regression: the judgment-task loop's system prompt only says "targets in scope will be
        # reachable" abstractly, and the model was seen defaulting to 127.0.0.1:3000 (a lab it's
        # over-anchored on) then abandoning when scope-check denied it. The per-hypothesis prompt
        # must state the concrete target from the asset store.
        self._write_roe()
        self.store.create_hypothesis("Some claim", "some description")
        hyp_id = self.store.list_hypotheses()[0]["hypothesis_id"]
        captured = {}

        def fake_run_model_task(task, prompt):
            captured["prompt"] = prompt
            return TaskResult("ok", "done")

        driver = self._make_driver()
        with patch.object(driver, "_run_model_task", side_effect=fake_run_model_task):
            driver._run_validate_hypothesis({"task_id": "t1", "params_json": None}, hyp_id)

        self.assertIn("In-scope target", captured["prompt"])
        self.assertIn("127.0.0.1", captured["prompt"])
        self.assertIn("rule out the mundane explanation", captured["prompt"])
        self.assertTrue(captured["prompt"].rstrip().endswith(f"hypothesis_id={hyp_id!r}."))

    def test_zero_hypotheses_is_not_a_blocker_and_skips_validation(self):
        self._write_roe()

        def on_review(store: EngagementStore) -> TaskResult:
            return TaskResult("ok", "nothing stood out")  # legitimate, no hypothesis created

        def on_validate(store, hyp_id):
            raise AssertionError("VALIDATION should never run with zero hypotheses")

        factory = _mock_agent_loop_factory(self.engagement_dir, on_review, on_validate)
        with patch("agent.pipeline.autonomous_driver.AgentLoop", side_effect=factory):
            driver = self._make_driver()
            result = driver.run()

        self.assertEqual(result.status, "closed_out")
        self.assertEqual(self.store.list_hypotheses(), [])


class TestJudgmentTaskNoProgressBlocks(AutonomousDriverTestBase):
    def test_validate_hypothesis_stuck_open_blocks_after_max_attempts(self):
        self._write_roe()
        self.store.create_hypothesis("t", "d")

        def on_review(store: EngagementStore) -> TaskResult:
            return TaskResult("ok", "already have a hypothesis")

        def on_validate(store, hyp_id) -> TaskResult:
            return TaskResult("ok", "inconclusive, no verdict recorded")  # never changes status

        factory = _mock_agent_loop_factory(self.engagement_dir, on_review, on_validate)
        with patch("agent.pipeline.autonomous_driver.AgentLoop", side_effect=factory):
            driver = self._make_driver()
            result = driver.run()

        self.assertEqual(result.status, "blocked")
        self.assertIn("no progress", result.reason)
        self.assertEqual(self.store.get_phase()["current_phase"], "VALIDATION")
        tasks = self.store.list_tasks(phase="VALIDATION")
        validate_task = next(t for t in tasks if t["task_type"] == "validate_hypothesis")
        self.assertEqual(validate_task["status"], "failed")


class TestHardBlockers(AutonomousDriverTestBase):
    def test_kill_switch_engaged_blocks_immediately(self):
        self._write_roe()
        with patch("agent.pipeline.autonomous_driver.KillSwitch") as mock_ks_cls:
            mock_ks_cls.return_value.is_engaged.return_value = True
            driver = self._make_driver()
            result = driver.run()
        self.assertEqual(result.status, "blocked")
        self.assertIn("kill switch", result.reason)
        self.assertEqual(result.phases_completed, [])

    def test_expired_valid_until_blocks_immediately(self):
        self._write_roe(valid_until="2020-01-01T00:00:00Z")
        driver = self._make_driver()
        result = driver.run()
        self.assertEqual(result.status, "blocked")
        self.assertIn("valid_until", result.reason)

    def test_wall_clock_budget_exceeded(self):
        self._write_roe()
        driver = self._make_driver(max_wall_clock_s=0)
        result = driver.run()
        self.assertEqual(result.status, "wall_clock_exceeded")

    def test_stop_event_set_before_run_stops_immediately(self):
        self._write_roe()
        driver = self._make_driver()
        driver.stop_event.set()
        result = driver.run()
        self.assertEqual(result.status, "stopped_by_operator")
        self.assertEqual(result.phases_completed, [])

    def test_stop_event_interrupts_a_pending_consult_wait(self):
        """A consult window can be up to consult_timeout_s (an hour by default) — an explicit
        stop request must take effect promptly during that wait, not only at the next loop
        iteration boundary."""
        self._write_roe()

        def on_review(store, *_):
            return TaskResult("ok", "x")

        def on_validate(store, hyp_id):
            return TaskResult("ok", "x")

        factory = _mock_agent_loop_factory(self.engagement_dir, on_review, on_validate)
        result_holder: dict = {}

        with patch("agent.pipeline.autonomous_driver.AgentLoop", side_effect=factory):
            driver = self._make_driver(mode="consult", consult_timeout_s=30)

            def run_driver():
                result_holder["result"] = driver.run()

            t = threading.Thread(target=run_driver, daemon=True)
            t.start()
            queue = ConsultQueue()
            deadline = time.time() + 10
            while time.time() < deadline and not queue.list_pending(session_id=f"session-{self.engagement_id}"):
                time.sleep(0.05)
            self.assertTrue(queue.list_pending(session_id=f"session-{self.engagement_id}"), "no consult request appeared")

            stop_requested_at = time.monotonic()
            driver.stop_event.set()
            t.join(timeout=5)
            elapsed = time.monotonic() - stop_requested_at

        self.assertFalse(t.is_alive(), "driver did not stop promptly after stop_event was set")
        self.assertLess(elapsed, 3, "stop took too long — consult wait isn't actually interruptible")
        self.assertEqual(result_holder["result"].status, "stopped_by_operator")


class TestConsultMode(AutonomousDriverTestBase):
    def test_consult_checkpoint_stops_when_operator_says_stop(self):
        self._write_roe()

        def on_review(store, *_):
            return TaskResult("ok", "x")

        def on_validate(store, hyp_id):
            return TaskResult("ok", "x")

        factory = _mock_agent_loop_factory(self.engagement_dir, on_review, on_validate)

        def answer_stop_when_asked():
            queue = ConsultQueue()
            deadline = time.time() + 10
            while time.time() < deadline:
                pending = queue.list_pending(session_id=f"session-{self.engagement_id}")
                if pending:
                    queue.resolve(pending[0]["request_id"], answer="stop", resolved_by="test")
                    return
                time.sleep(0.05)
            raise AssertionError("no consult request appeared within 10s")

        with patch("agent.pipeline.autonomous_driver.AgentLoop", side_effect=factory):
            driver = self._make_driver(mode="consult", consult_timeout_s=5)
            t = threading.Thread(target=answer_stop_when_asked, daemon=True)
            t.start()
            result = driver.run()
            t.join(timeout=5)

        self.assertEqual(result.status, "stopped_by_operator")
        self.assertIn("RECON", result.phases_completed)
        # Stopped after the first checkpoint — never reached CLOSEOUT.
        self.assertNotEqual(result.final_phase, "CLOSEOUT")

    def test_consult_checkpoint_continues_when_operator_says_continue(self):
        """A real run passes through several phase-boundary checkpoints (RECON, ANALYSIS,
        VALIDATION, REPORT), not just one — the answerer thread must keep answering every new
        one until the driver actually finishes, not just the first. Runs the driver itself in a
        background thread so the main thread can act as a continuous answerer; a short
        consult_timeout_s means a bug here fails in seconds, not by hanging for an hour."""
        self._write_roe()

        def on_review(store, *_):
            return TaskResult("ok", "x")

        def on_validate(store, hyp_id):
            return TaskResult("ok", "x")

        factory = _mock_agent_loop_factory(self.engagement_dir, on_review, on_validate)
        result_holder: dict = {}

        def run_driver():
            with patch("agent.pipeline.autonomous_driver.AgentLoop", side_effect=factory):
                driver = self._make_driver(mode="consult", consult_timeout_s=5)
                result_holder["result"] = driver.run()

        driver_thread = threading.Thread(target=run_driver, daemon=True)
        driver_thread.start()

        queue = ConsultQueue()
        answered: set[str] = set()
        deadline = time.time() + 20
        while driver_thread.is_alive() and time.time() < deadline:
            for p in queue.list_pending(session_id=f"session-{self.engagement_id}"):
                if p["request_id"] not in answered:
                    queue.resolve(p["request_id"], answer="continue", resolved_by="test")
                    answered.add(p["request_id"])
            time.sleep(0.05)
        driver_thread.join(timeout=10)

        self.assertFalse(driver_thread.is_alive(), "driver never finished — a checkpoint was never answered")
        self.assertEqual(result_holder["result"].status, "closed_out")
        self.assertGreaterEqual(len(answered), 2)  # at least RECON and ANALYSIS checkpoints


class TestEngagementBootstrap(unittest.TestCase):
    """_bootstrap_engagement() makes a run start from a usable engagement without the operator
    hand-wiring RoE files and assets first (no Juice Shop needed for this)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bootstrap-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._p = patch("agent.pipeline.autonomous_driver.config.ENGAGEMENTS_ROOT", self.tmp)
        self._p.start()
        self.addCleanup(self._p.stop)

    def _driver(self, engagement_id):
        return AutonomousDriver(engagement_id=engagement_id, profile_name="web_api",
                                mode="autonomous", session_id="s", on_event=lambda e: None)

    def _write_policy(self, engagement_dir, scope_lines):
        engagement_dir.mkdir(parents=True, exist_ok=True)
        (engagement_dir / "roe.json").write_text(
            '{"engagement_id": "x", "valid_from": "2020-01-01T00:00:00Z", '
            '"valid_until": "2099-01-01T00:00:00Z", "allowed_action_classes": ["passive_recon"]}'
        )
        (engagement_dir / "scope.txt").write_text("\n".join(scope_lines) + "\n")
        (engagement_dir / "deny.txt").write_text("")

    def test_seeds_host_assets_from_scope_when_there_are_none(self):
        d = self._driver("scope-only-eng")
        self._write_policy(d.engagement_dir, ["10.0.0.5", "target.local"])
        self.assertEqual(d.store.list_assets(), [])
        d._bootstrap_engagement()
        ids = sorted(a["identifier"] for a in d.store.list_assets())
        self.assertEqual(ids, ["target.local"])  # hostname preferred; the bare /32 is skipped when a host exists

    def test_falls_back_to_single_address_networks(self):
        d = self._driver("ip-only-eng")
        self._write_policy(d.engagement_dir, ["127.0.0.1", "10.0.0.0/24"])
        d._bootstrap_engagement()
        ids = [a["identifier"] for a in d.store.list_assets()]
        self.assertEqual(ids, ["127.0.0.1"])  # the /24 range is not blanket-seeded

    def test_existing_assets_are_left_alone(self):
        d = self._driver("has-assets-eng")
        self._write_policy(d.engagement_dir, ["127.0.0.1"])
        d.store.upsert_asset("host", "192.168.1.10")
        d._bootstrap_engagement()
        self.assertEqual([a["identifier"] for a in d.store.list_assets()], ["192.168.1.10"])

    def test_lab_default_imports_the_legacy_roe_and_becomes_usable(self):
        d = self._driver("lab-default")
        # engagements/lab-default/ starts with just an empty state.db and no policy files
        self.assertFalse((d.engagement_dir / "roe.json").exists())
        d._bootstrap_engagement()
        for f in ("roe.json", "scope.txt", "deny.txt"):
            self.assertTrue((d.engagement_dir / f).exists(), f)
        self.assertTrue(d.store.list_assets())
        ready, _ = d.orch.check_transition()  # INTAKE -> RECON
        self.assertTrue(ready)


if __name__ == "__main__":
    unittest.main()
