"""Tests for M5.3 -> real execution wiring (agent/pipeline/executor.py). Uses the real Juice
Shop lab container (127.0.0.1:3000, already running for this project's Layer-4 eval milestones)
and the real default Broker/RoE — matching this project's established preference for real
targets over mocks wherever feasible, since the entire point of this module is that a planned
task actually reaches a real tool through the real broker and updates real state.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from .. import config
from ..broker.broker import Broker
from ..engagement.store import EngagementStore
from .executor import (
    ExecutionOutcome,
    TASK_EXECUTORS,
    TASKS_REQUIRING_MODEL,
    _url_from_open_ports,
    run_pending_tasks,
)
from .orchestrator import PipelineOrchestrator


class TestUrlFromOpenPorts(unittest.TestCase):
    """No lab container needed — pure derivation logic."""

    def test_no_http_ports_open_yields_nothing(self):
        self.assertIsNone(_url_from_open_ports("10.0.0.1", [22, 5432]))

    def test_conventional_ports_omit_the_port_number(self):
        self.assertEqual(_url_from_open_ports("h", [80]), "http://h/")
        self.assertEqual(_url_from_open_ports("h", [443]), "https://h/")

    def test_alternate_ports_are_included(self):
        self.assertEqual(_url_from_open_ports("h", [3000]), "http://h:3000/")
        self.assertEqual(_url_from_open_ports("h", [8443]), "https://h:8443/")

    def test_https_preferred_over_http_when_both_open(self):
        self.assertEqual(_url_from_open_ports("h", [80, 443]), "https://h/")
        self.assertEqual(_url_from_open_ports("h", [8080, 8443]), "https://h:8443/")


def _juiceshop_reachable() -> bool:
    try:
        urllib.request.urlopen("http://127.0.0.1:3000/", timeout=3)
        return True
    except (urllib.error.URLError, ConnectionRefusedError):
        return False


class TestExecutorBase(unittest.TestCase):
    def setUp(self):
        if not _juiceshop_reachable():
            self.skipTest("Juice Shop lab container not reachable at 127.0.0.1:3000")
        self.tmp = Path(tempfile.mkdtemp(prefix="pipeline-executor-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = EngagementStore(self.tmp / "eng-1")
        self.broker = Broker(engagement_dir=config.ENGAGEMENT_DIR, confirm_fn=lambda p: True)
        self.session_id = f"executor-test-{time.time_ns()}"


class TestPortDiscoveryExecution(TestExecutorBase):
    def test_real_port_discovery_updates_services_and_observations(self):
        asset_id = self.store.upsert_asset("host", "127.0.0.1")
        orch = PipelineOrchestrator(self.store, "web_api")
        orch.advance_if_ready()  # -> RECON
        orch.plan_tasks()

        outcomes = run_pending_tasks(orch, self.broker, self.session_id)
        port_outcomes = [o for o in outcomes if o.task_type == "port_discovery"]
        self.assertEqual(len(port_outcomes), 1)
        self.assertTrue(port_outcomes[0].ok, port_outcomes[0].detail)

        services = self.store.list_services(asset_id)
        self.assertTrue(any(s["port"] == 3000 for s in services))
        observations = self.store.list_observations(asset_id)
        self.assertTrue(any(o["observation_type"] == "port_scan_result" for o in observations))

    def test_task_marked_done_with_result(self):
        self.store.upsert_asset("host", "127.0.0.1")
        orch = PipelineOrchestrator(self.store, "web_api")
        orch.advance_if_ready()
        orch.plan_tasks()
        run_pending_tasks(orch, self.broker, self.session_id)

        tasks = self.store.list_tasks(phase="RECON")
        port_task = next(t for t in tasks if t["task_type"] == "port_discovery")
        self.assertEqual(port_task["status"], "done")
        self.assertIsNotNone(port_task["result_json"])


class TestHttpReconExecution(TestExecutorBase):
    def test_asset_without_url_metadata_fails_gracefully(self):
        # Exercises _execute_http_recon in isolation — no port_discovery ever ran for this
        # asset, so there's genuinely no evidence of a URL yet, regardless of what else happens
        # to be listening on this box. (Through the full pipeline, port_discovery and http_recon
        # are planned together and port_discovery always runs first in the same batch — see
        # test_url_derived_from_port_discovery_lets_http_recon_succeed below.)
        from .executor import _execute_http_recon

        asset_id = self.store.upsert_asset("host", "127.0.0.1")  # no metadata
        task_id = self.store.create_task("RECON", "http_recon", params={"entity_id": asset_id})
        orch = PipelineOrchestrator(self.store, "web_api")
        task = next(t for t in self.store.list_tasks() if t["task_id"] == task_id)

        outcome = _execute_http_recon(orch, self.broker, self.session_id, task)
        self.assertFalse(outcome.ok)
        self.assertIn("no known URL", outcome.detail)

    def test_url_derived_from_port_discovery_lets_http_recon_succeed(self):
        # No metadata.url set anywhere — port_discovery must find an HTTP(S) port open and set
        # it itself, in time for the http_recon task (planned in the same batch) to use it.
        # DEFAULT_PORTS pinned to just the lab's known port so incidental services elsewhere on
        # this dev box (other local servers on other DEFAULT_PORTS entries) can't flip which
        # port wins the https-preferred ordering in _url_from_open_ports.
        self.store.upsert_asset("host", "127.0.0.1")
        orch = PipelineOrchestrator(self.store, "web_api")
        orch.advance_if_ready()
        orch.plan_tasks()

        with patch("agent.pipeline.executor.DEFAULT_PORTS", [3000]):
            outcomes = run_pending_tasks(orch, self.broker, self.session_id)
        http_outcome = next(o for o in outcomes if o.task_type == "http_recon")
        self.assertTrue(http_outcome.ok, http_outcome.detail)

        asset = self.store.list_assets()[0]
        self.assertEqual(json.loads(asset["metadata_json"]), {"url": "http://127.0.0.1:3000/"})

    def test_port_discovery_never_overwrites_an_explicitly_set_url(self):
        self.store.upsert_asset("host", "127.0.0.1", metadata={"url": "http://127.0.0.1:3000/special-path"})
        orch = PipelineOrchestrator(self.store, "web_api")
        orch.advance_if_ready()
        orch.plan_tasks()

        with patch("agent.pipeline.executor.DEFAULT_PORTS", [3000, 8443]):
            run_pending_tasks(orch, self.broker, self.session_id)

        asset = self.store.list_assets()[0]
        self.assertEqual(json.loads(asset["metadata_json"]), {"url": "http://127.0.0.1:3000/special-path"})

    def test_asset_with_url_metadata_succeeds(self):
        self.store.upsert_asset("host", "127.0.0.1", metadata={"url": "http://127.0.0.1:3000/"})
        orch = PipelineOrchestrator(self.store, "web_api")
        orch.advance_if_ready()
        orch.plan_tasks()

        outcomes = run_pending_tasks(orch, self.broker, self.session_id)
        http_outcome = next(o for o in outcomes if o.task_type == "http_recon")
        self.assertTrue(http_outcome.ok, http_outcome.detail)

        observations = self.store.list_observations()
        http_obs = [o for o in observations if o["observation_type"] == "http_recon_result"]
        self.assertEqual(len(http_obs), 1)
        self.assertIn("X-Recruiting", http_obs[0]["content"])  # the known Juice Shop header


class TestServiceFingerprintExecution(TestExecutorBase):
    def test_reconfirms_a_discovered_service(self):
        asset_id = self.store.upsert_asset("host", "127.0.0.1")
        self.store.upsert_service(asset_id, 3000, "tcp")
        orch = PipelineOrchestrator(self.store, "network")
        orch.advance_if_ready()  # -> RECON
        orch.plan_tasks()  # creates port_discovery + service_fingerprint tasks

        outcomes = run_pending_tasks(orch, self.broker, self.session_id)
        fp_outcome = next(o for o in outcomes if o.task_type == "service_fingerprint")
        self.assertTrue(fp_outcome.ok, fp_outcome.detail)

        observations = self.store.list_observations(asset_id)
        self.assertTrue(any(o["observation_type"] == "service_reconfirm_result" for o in observations))


class TestModelReasoningTasksNotAutoExecuted(TestExecutorBase):
    def test_review_observations_left_pending(self):
        self.store.upsert_asset("host", "127.0.0.1", metadata={"url": "http://127.0.0.1:3000/"})
        orch = PipelineOrchestrator(self.store, "web_api")
        orch.advance_if_ready()
        orch.plan_tasks()
        run_pending_tasks(orch, self.broker, self.session_id)
        orch.advance_if_ready()  # -> ANALYSIS
        orch.plan_tasks()

        outcomes = run_pending_tasks(orch, self.broker, self.session_id)
        review_outcomes = [o for o in outcomes if o.task_type == "review_observations"]
        self.assertEqual(len(review_outcomes), 1)
        self.assertTrue(review_outcomes[0].skipped)
        self.assertFalse(review_outcomes[0].ok)

        tasks = self.store.list_tasks(phase="ANALYSIS")
        review_task = next(t for t in tasks if t["task_type"] == "review_observations")
        self.assertEqual(review_task["status"], "pending")  # untouched, not faked

    def test_validate_hypothesis_left_pending(self):
        self.store.create_hypothesis("Reflected XSS", "search param unescaped")
        orch = PipelineOrchestrator(self.store, "web_api")
        orch.store.transition_phase("RECON", "x", expected_version=0)
        orch.store.transition_phase("ANALYSIS", "x", expected_version=1)
        orch.store.transition_phase("VALIDATION", "x", expected_version=2)
        orch.plan_tasks()

        outcomes = run_pending_tasks(orch, self.broker, self.session_id)
        self.assertTrue(all(o.skipped for o in outcomes if o.task_type == "validate_hypothesis"))


class TestReportAndCloseoutExecution(TestExecutorBase):
    def test_generate_report_writes_file(self):
        orch = PipelineOrchestrator(self.store, "web_api")
        for phase, reason in [("RECON", "a"), ("ANALYSIS", "b"), ("VALIDATION", "c"), ("REPORT", "d")]:
            current = self.store.get_phase()
            self.store.transition_phase(phase, reason, expected_version=current["version"])
        orch.plan_tasks()

        outcomes = run_pending_tasks(orch, self.broker, self.session_id)
        report_outcome = next(o for o in outcomes if o.task_type == "generate_report")
        self.assertTrue(report_outcome.ok, report_outcome.detail)
        report_path = self.tmp / "eng-1" / "report.md"
        self.assertTrue(report_path.exists())

    def test_closeout_closes_the_engagement(self):
        orch = PipelineOrchestrator(self.store, "web_api")
        for phase, reason in [
            ("RECON", "a"), ("ANALYSIS", "b"), ("VALIDATION", "c"), ("REPORT", "d"), ("CLOSEOUT", "e"),
        ]:
            current = self.store.get_phase()
            self.store.transition_phase(phase, reason, expected_version=current["version"])
        orch.plan_tasks()

        outcomes = run_pending_tasks(orch, self.broker, self.session_id)
        closeout_outcome = next(o for o in outcomes if o.task_type == "closeout")
        self.assertTrue(closeout_outcome.ok, closeout_outcome.detail)
        self.assertIsNotNone(self.store.get_closeout())


class TestGenerateReportLinksFindingsForCloseout(TestExecutorBase):
    """Found live (2026-09-01, first real end-to-end pipeline run): FindingsStore (what the
    rendered report reads from) and EngagementStore's own findings_lifecycle table (what
    close_engagement() counts as closed/outstanding) were two disconnected systems — a real run
    produced a report with 1 finding and a closeout summary of "0 closed, 0 outstanding" moments
    later. generate_report must link every finding it renders into the lifecycle table."""

    def test_finding_is_linked_and_counted_outstanding_at_closeout(self):
        from ..findings.model import Finding, FindingsStore

        engagement_id = self.store.db_path.parent.name
        findings_dir = self.tmp / "findings"
        finding = Finding(
            title="test finding", severity="info", target="http://127.0.0.1:3000",
            description="d", remediation="r", tool="http_recon",
            session_id=self.session_id, engagement_id=engagement_id,
        )
        FindingsStore(engagement_id, findings_dir=findings_dir).add(finding)

        orch = PipelineOrchestrator(self.store, "web_api")
        for phase, reason in [("RECON", "a"), ("ANALYSIS", "b"), ("VALIDATION", "c"), ("REPORT", "d")]:
            current = self.store.get_phase()
            self.store.transition_phase(phase, reason, expected_version=current["version"])

        with patch("agent.pipeline.executor.FindingsStore") as mock_store_cls:
            mock_store_cls.return_value = FindingsStore(engagement_id, findings_dir=findings_dir)
            orch.plan_tasks()
            run_pending_tasks(orch, self.broker, self.session_id)

        lifecycle = self.store.list_findings_lifecycle()
        self.assertEqual(len(lifecycle), 1)
        self.assertEqual(lifecycle[0]["finding_id"], finding.finding_id)
        self.assertEqual(lifecycle[0]["status"], "reported")

        current = self.store.get_phase()
        self.store.transition_phase("CLOSEOUT", "e", expected_version=current["version"])
        result = self.store.close_engagement(closed_by="test", summary="test closeout")
        self.assertEqual(result["outstanding_count"], 1)
        self.assertEqual(result["findings_closed_count"], 0)


class TestUnknownTaskType(TestExecutorBase):
    def test_unregistered_task_type_skipped_not_crashed(self):
        self.store.create_task("RECON", "not_a_real_task_type")
        self.store.transition_phase("RECON", "x", expected_version=0)
        orch = PipelineOrchestrator(self.store, "web_api")

        outcomes = run_pending_tasks(orch, self.broker, self.session_id)
        unknown_outcomes = [o for o in outcomes if o.task_type == "not_a_real_task_type"]
        self.assertEqual(len(unknown_outcomes), 1)
        self.assertTrue(unknown_outcomes[0].skipped)


class TestExecutorRegistry(unittest.TestCase):
    def test_task_requiring_model_never_in_executors_table(self):
        for task_type in TASKS_REQUIRING_MODEL:
            self.assertNotIn(task_type, TASK_EXECUTORS)


if __name__ == "__main__":
    unittest.main()
