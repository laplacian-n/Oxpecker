"""Tests for M5.2 authoritative engagement-state store."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from .store import (
    ConflictError,
    EngagementStore,
    InvalidTransitionError,
    NotFoundError,
)


class TestEngagementStoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engagement-store-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = EngagementStore(self.tmp / "eng-1")


class TestAssetModel(TestEngagementStoreBase):
    def test_upsert_asset_creates_then_updates(self):
        aid1 = self.store.upsert_asset("host", "10.0.0.5")
        aid2 = self.store.upsert_asset("host", "10.0.0.5", metadata={"os": "linux"})
        self.assertEqual(aid1, aid2)
        assets = self.store.list_assets()
        self.assertEqual(len(assets), 1)

    def test_service_unique_per_asset_port_protocol(self):
        aid = self.store.upsert_asset("host", "10.0.0.5")
        sid1 = self.store.upsert_service(aid, 443, "tcp", service_name="https")
        sid2 = self.store.upsert_service(aid, 443, "tcp", service_version="1.1")
        self.assertEqual(sid1, sid2)
        services = self.store.list_services(aid)
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0]["service_version"], "1.1")

    def test_endpoint_unique_per_asset_method_path(self):
        aid = self.store.upsert_asset("host", "juice-shop.local")
        eid1 = self.store.upsert_endpoint(aid, "GET", "/rest/products")
        eid2 = self.store.upsert_endpoint(aid, "GET", "/rest/products")
        self.assertEqual(eid1, eid2)
        eid3 = self.store.upsert_endpoint(aid, "POST", "/rest/products")
        self.assertNotEqual(eid1, eid3)

    def test_relationship_idempotent(self):
        aid = self.store.upsert_asset("host", "10.0.0.5")
        sid = self.store.upsert_service(aid, 443, "tcp")
        r1 = self.store.add_relationship("asset", aid, "service", sid, "hosts")
        r2 = self.store.add_relationship("asset", aid, "service", sid, "hosts")
        self.assertEqual(r1, r2)


class TestObservations(TestEngagementStoreBase):
    def test_add_and_list_observation(self):
        aid = self.store.upsert_asset("host", "10.0.0.5")
        oid = self.store.add_observation(
            "http_header", "Server: nginx/1.18", "http_recon", asset_id=aid,
            evidence_ref="sha256:deadbeef",
        )
        obs = self.store.list_observations(aid)
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["observation_id"], oid)
        self.assertEqual(obs[0]["evidence_ref"], "sha256:deadbeef")


class TestHypotheses(TestEngagementStoreBase):
    def test_create_and_get(self):
        hid = self.store.create_hypothesis(
            "Reflected XSS in search", "search param may be unescaped",
            priority="high", coverage_surface="/rest/products/search",
        )
        h = self.store.get_hypothesis(hid)
        self.assertEqual(h["status"], "open")
        self.assertEqual(h["version"], 0)
        self.assertEqual(h["evidence_for"], [])

    def test_update_with_correct_version_succeeds(self):
        hid = self.store.create_hypothesis("t", "d")
        new_version = self.store.update_hypothesis(
            hid, expected_version=0, status="testing", add_evidence_for="obs-1"
        )
        self.assertEqual(new_version, 1)
        h = self.store.get_hypothesis(hid)
        self.assertEqual(h["status"], "testing")
        self.assertEqual(h["evidence_for"], ["obs-1"])

    def test_update_with_stale_version_conflicts(self):
        hid = self.store.create_hypothesis("t", "d")
        self.store.update_hypothesis(hid, expected_version=0, status="testing")
        with self.assertRaises(ConflictError) as cm:
            self.store.update_hypothesis(hid, expected_version=0, status="confirmed")
        self.assertEqual(cm.exception.current_version, 1)

    def test_invalid_status_rejected(self):
        hid = self.store.create_hypothesis("t", "d")
        with self.assertRaises(ValueError):
            self.store.update_hypothesis(hid, expected_version=0, status="maybe")

    def test_unknown_hypothesis_not_found(self):
        with self.assertRaises(NotFoundError):
            self.store.get_hypothesis("nonexistent")

    def test_list_filters_by_status(self):
        h1 = self.store.create_hypothesis("t1", "d1")
        self.store.create_hypothesis("t2", "d2")
        self.store.update_hypothesis(h1, expected_version=0, status="confirmed")
        confirmed = self.store.list_hypotheses(status="confirmed")
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["hypothesis_id"], h1)


class TestCoverageLedger(TestEngagementStoreBase):
    def test_set_and_summarize(self):
        self.store.set_coverage("/rest/products/search", "sqli", "covered")
        self.store.set_coverage("/rest/products/search", "xss", "in_progress")
        self.store.set_coverage("/rest/user/login", "sqli", "not_started")
        summary = self.store.coverage_summary()
        self.assertEqual(summary["covered"], 1)
        self.assertEqual(summary["in_progress"], 1)
        self.assertEqual(summary["not_started"], 1)

    def test_upsert_same_surface_technique(self):
        cid1 = self.store.set_coverage("/x", "sqli", "not_started")
        cid2 = self.store.set_coverage("/x", "sqli", "covered")
        self.assertEqual(cid1, cid2)
        self.assertEqual(self.store.coverage_summary()["covered"], 1)

    def test_invalid_status_rejected(self):
        with self.assertRaises(ValueError):
            self.store.set_coverage("/x", "sqli", "done_done")


class TestPhaseTransitions(TestEngagementStoreBase):
    def test_starts_at_intake(self):
        phase = self.store.get_phase()
        self.assertEqual(phase["current_phase"], "INTAKE")
        self.assertEqual(phase["version"], 0)

    def test_forward_transition_succeeds(self):
        new_version = self.store.transition_phase("RECON", "scope confirmed", expected_version=0)
        self.assertEqual(new_version, 1)
        self.assertEqual(self.store.get_phase()["current_phase"], "RECON")

    def test_backward_transition_rejected(self):
        self.store.transition_phase("RECON", "go", expected_version=0)
        with self.assertRaises(InvalidTransitionError):
            self.store.transition_phase("INTAKE", "oops", expected_version=1)

    def test_stale_version_conflicts(self):
        self.store.transition_phase("RECON", "go", expected_version=0)
        with self.assertRaises(ConflictError):
            self.store.transition_phase("ANALYSIS", "go again", expected_version=0)

    def test_history_recorded(self):
        self.store.transition_phase("RECON", "scope confirmed", expected_version=0)
        self.store.transition_phase("ANALYSIS", "recon budget exhausted", expected_version=1)
        history = self.store.phase_history()
        self.assertEqual([h["phase"] for h in history], ["INTAKE", "RECON", "ANALYSIS"])
        self.assertIsNotNone(history[0]["exited_at"])
        self.assertEqual(history[0]["exit_reason"], "scope confirmed")
        self.assertIsNone(history[-1]["exited_at"])

    def test_invalid_phase_name_rejected(self):
        with self.assertRaises(ValueError):
            self.store.transition_phase("EXPLOIT_EVERYTHING", "yolo", expected_version=0)


class TestTasks(TestEngagementStoreBase):
    def test_create_and_update(self):
        tid = self.store.create_task("RECON", "port_scan", params={"asset": "10.0.0.5"})
        tasks = self.store.list_tasks(phase="RECON")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "pending")

        new_version = self.store.update_task(tid, expected_version=0, status="running")
        self.assertEqual(new_version, 1)
        new_version = self.store.update_task(
            tid, expected_version=1, status="done", result={"ports": [80, 443]}
        )
        self.assertEqual(new_version, 2)
        done = self.store.list_tasks(status="done")
        self.assertEqual(len(done), 1)

    def test_stale_version_conflicts(self):
        tid = self.store.create_task("RECON", "port_scan")
        self.store.update_task(tid, expected_version=0, status="running")
        with self.assertRaises(ConflictError):
            self.store.update_task(tid, expected_version=0, status="done")

    def test_unknown_task_not_found(self):
        with self.assertRaises(NotFoundError):
            self.store.update_task("nope", expected_version=0, status="running")


class TestFindingsLifecycleAndCloseout(TestEngagementStoreBase):
    def test_link_and_update_status(self):
        self.store.link_finding("finding-1")
        lifecycle = self.store.list_findings_lifecycle()
        self.assertEqual(lifecycle[0]["status"], "draft")
        self.store.update_finding_status("finding-1", "validated")
        lifecycle = self.store.list_findings_lifecycle(status="validated")
        self.assertEqual(len(lifecycle), 1)

    def test_update_unknown_finding_not_found(self):
        with self.assertRaises(NotFoundError):
            self.store.update_finding_status("nonexistent", "validated")

    def test_record_retest_sets_status_retested(self):
        self.store.link_finding("finding-1", status="validated")
        self.store.record_retest("finding-1", "still vulnerable", evidence_ref="sha256:abc")
        lifecycle = self.store.list_findings_lifecycle()
        self.assertEqual(lifecycle[0]["status"], "retested")
        retests = self.store.list_retests("finding-1")
        self.assertEqual(len(retests), 1)
        self.assertEqual(retests[0]["result"], "still vulnerable")

    def test_close_engagement_counts_outstanding(self):
        self.store.link_finding("finding-1", status="reported")
        self.store.link_finding("finding-2", status="closed")
        result = self.store.close_engagement("test-owner", "engagement complete")
        self.assertEqual(result["findings_closed_count"], 1)
        self.assertEqual(result["outstanding_count"], 1)
        self.assertEqual(self.store.get_closeout()["closed_by"], "test-owner")

    def test_double_closeout_rejected(self):
        self.store.close_engagement("owner", "done")
        with self.assertRaises(RuntimeError):
            self.store.close_engagement("owner", "done again")


class TestPersistence(TestEngagementStoreBase):
    def test_state_survives_reopening_store(self):
        aid = self.store.upsert_asset("host", "10.0.0.5")
        self.store.transition_phase("RECON", "go", expected_version=0)
        reopened = EngagementStore(self.tmp / "eng-1")
        self.assertEqual(reopened.get_phase()["current_phase"], "RECON")
        self.assertEqual(len(reopened.list_assets()), 1)
        self.assertEqual(reopened.list_assets()[0]["asset_id"], aid)


if __name__ == "__main__":
    unittest.main()
