"""Tests for M5.3 pipeline profiles + orchestrator."""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import yaml

from ..engagement.store import EngagementStore
from ..skills.library import SkillLibrary
from ..skills.signing import sign_file
from .orchestrator import Budget, PipelineOrchestrator, UnknownProfileError


class TestOrchestratorBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pipeline-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = EngagementStore(self.tmp / "eng-1")


class TestUnknownProfile(TestOrchestratorBase):
    def test_unknown_profile_rejected(self):
        with self.assertRaises(UnknownProfileError):
            PipelineOrchestrator(self.store, "made_up_profile")


class TestIntakeToRecon(TestOrchestratorBase):
    def test_not_ready_with_no_assets(self):
        orch = PipelineOrchestrator(self.store, "web_api")
        ready, reason = orch.check_transition()
        self.assertFalse(ready)

    def test_ready_once_asset_exists(self):
        self.store.upsert_asset("host", "juice-shop.local")
        orch = PipelineOrchestrator(self.store, "web_api")
        ready, reason = orch.check_transition()
        self.assertTrue(ready)
        self.assertIn("RECON", reason)

    def test_advance_if_ready_transitions(self):
        self.store.upsert_asset("host", "juice-shop.local")
        orch = PipelineOrchestrator(self.store, "web_api")
        advanced = orch.advance_if_ready()
        self.assertTrue(advanced)
        self.assertEqual(self.store.get_phase()["current_phase"], "RECON")


class TestReconPlanningWebApi(TestOrchestratorBase):
    def setUp(self):
        super().setUp()
        self.store.upsert_asset("host", "juice-shop.local")
        self.orch = PipelineOrchestrator(self.store, "web_api")
        self.orch.advance_if_ready()  # INTAKE -> RECON

    def test_plan_tasks_creates_one_per_template_per_asset(self):
        created = self.orch.plan_tasks()
        self.assertEqual(len(created), 2)  # port_discovery + http_recon, one asset
        tasks = self.store.list_tasks(phase="RECON")
        self.assertEqual({t["task_type"] for t in tasks}, {"port_discovery", "http_recon"})

    def test_plan_tasks_idempotent(self):
        self.orch.plan_tasks()
        created_again = self.orch.plan_tasks()
        self.assertEqual(created_again, [])
        self.assertEqual(len(self.store.list_tasks(phase="RECON")), 2)

    def test_new_asset_gets_new_tasks_on_replan(self):
        self.orch.plan_tasks()
        self.store.upsert_asset("host", "10.0.0.9")
        created = self.orch.plan_tasks()
        self.assertEqual(len(created), 2)
        self.assertEqual(len(self.store.list_tasks(phase="RECON")), 4)

    def test_not_ready_while_tasks_pending(self):
        self.orch.plan_tasks()
        ready, reason = self.orch.check_transition()
        self.assertFalse(ready)
        self.assertIn("not terminal", reason)

    def test_ready_once_all_tasks_terminal(self):
        self.orch.plan_tasks()
        tasks = self.store.list_tasks(phase="RECON")
        for t in tasks:
            self.store.update_task(t["task_id"], expected_version=0, status="done")
        ready, reason = self.orch.check_transition()
        self.assertTrue(ready)

    def test_failed_task_counts_as_terminal(self):
        self.orch.plan_tasks()
        tasks = self.store.list_tasks(phase="RECON")
        self.store.update_task(tasks[0]["task_id"], expected_version=0, status="failed")
        self.store.update_task(tasks[1]["task_id"], expected_version=0, status="done")
        ready, _ = self.orch.check_transition()
        self.assertTrue(ready)  # a failed recon task doesn't block the pipeline forever

    def test_budget_exhaustion_forces_ready_regardless_of_pending_tasks(self):
        self.orch.plan_tasks()  # still has pending tasks
        tight_orch = PipelineOrchestrator(
            self.store, "web_api", budget=Budget(max_phase_wall_clock_s=0)
        )
        ready, reason = tight_orch.check_transition()
        self.assertTrue(ready)
        self.assertIn("budget exhausted", reason)


class TestNetworkProfileServiceDiscovery(TestOrchestratorBase):
    def test_service_fingerprint_appears_only_after_service_exists(self):
        self.store.upsert_asset("host", "10.0.0.5")
        orch = PipelineOrchestrator(self.store, "network")
        orch.advance_if_ready()  # -> RECON
        created = orch.plan_tasks()
        self.assertEqual({t for t in created}, set(created))  # sanity, real check below
        tasks = self.store.list_tasks(phase="RECON")
        self.assertEqual({t["task_type"] for t in tasks}, {"port_discovery"})  # no service yet

        asset_id = self.store.list_assets()[0]["asset_id"]
        self.store.upsert_service(asset_id, 22, "tcp", service_name="ssh")
        newly_created = orch.plan_tasks()
        self.assertEqual(len(newly_created), 1)
        tasks = self.store.list_tasks(phase="RECON")
        self.assertEqual({t["task_type"] for t in tasks}, {"port_discovery", "service_fingerprint"})


class TestValidationHypothesisDriven(TestOrchestratorBase):
    def setUp(self):
        super().setUp()
        self.store.upsert_asset("host", "juice-shop.local")
        self.orch = PipelineOrchestrator(self.store, "web_api")
        self.orch.advance_if_ready()  # -> RECON
        for t in self.orch.plan_tasks():
            self.store.update_task(t, expected_version=0, status="done")
        self.orch.advance_if_ready()  # -> ANALYSIS

    def test_analysis_ready_without_any_hypothesis(self):
        """Never 'must find a vulnerability' — ANALYSIS with zero hypotheses created is still a
        valid, completable phase, not a stuck one."""
        created = self.orch.plan_tasks()
        self.assertEqual(len(created), 1)  # review_observations, one asset
        self.store.update_task(created[0], expected_version=0, status="done")
        ready, _ = self.orch.check_transition()
        self.assertTrue(ready)

    def test_validation_tasks_generated_per_open_hypothesis(self):
        for t in self.orch.plan_tasks():
            self.store.update_task(t, expected_version=0, status="done")
        self.store.create_hypothesis("Reflected XSS", "search param unescaped")
        self.orch.advance_if_ready()  # -> VALIDATION
        created = self.orch.plan_tasks()
        self.assertEqual(len(created), 1)
        tasks = self.store.list_tasks(phase="VALIDATION")
        self.assertEqual(tasks[0]["task_type"], "validate_hypothesis")

    def test_confirmed_or_refuted_hypothesis_not_replanned(self):
        for t in self.orch.plan_tasks():
            self.store.update_task(t, expected_version=0, status="done")
        hid = self.store.create_hypothesis("Reflected XSS", "search param unescaped")
        self.store.update_hypothesis(hid, expected_version=0, status="confirmed")
        self.orch.advance_if_ready()  # -> VALIDATION
        created = self.orch.plan_tasks()
        self.assertEqual(created, [])  # only open/testing hypotheses get validate tasks


class TestFullPipelineReachesCloseout(TestOrchestratorBase):
    def test_web_api_profile_reaches_closeout(self):
        self.store.upsert_asset("host", "juice-shop.local")
        orch = PipelineOrchestrator(self.store, "web_api")
        # INTAKE -> RECON
        self.assertTrue(orch.advance_if_ready())
        for _ in range(len(PHASES_FOR_TEST) - 1):
            for t in orch.plan_tasks():
                self.store.update_task(t, expected_version=0, status="done")
            if not orch.advance_if_ready():
                break
        self.assertEqual(self.store.get_phase()["current_phase"], "CLOSEOUT")


PHASES_FOR_TEST = ("INTAKE", "RECON", "ANALYSIS", "VALIDATION", "REPORT", "CLOSEOUT")


def _write_and_sign_skill(lib_dir: Path, key: bytes, **overrides) -> Path:
    data = dict(
        skill_id="recon-web-skill",
        version="1.0",
        title="Web recon checklist",
        description="Curated guidance for RECON on a web_api engagement",
        guidance="Check for verbose error pages and default credentials.",
        applicable_phases=["RECON"],
        applicable_profiles=["web_api"],
        preconditions="",
        references=[],
        author="test",
        created_at="2026-08-31",
    )
    data.update(overrides)
    path = lib_dir / f"{data['skill_id']}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    sign_file(path, key=key)
    return path


class TestSkillLibraryWiring(TestOrchestratorBase):
    def setUp(self):
        super().setUp()
        self.skills_dir = self.tmp / "skills"
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.skills_key = b"0" * 32
        self.store.upsert_asset("host", "juice-shop.local")

    def test_no_skill_library_no_skill_ids_key(self):
        orch = PipelineOrchestrator(self.store, "web_api")
        orch.advance_if_ready()  # -> RECON
        created = orch.plan_tasks()
        tasks = self.store.list_tasks(phase="RECON")
        for t in tasks:
            params = json.loads(t["params_json"]) if t["params_json"] else {}
            self.assertNotIn("skill_ids", params)

    def test_matching_skill_attached_to_recon_tasks(self):
        _write_and_sign_skill(self.skills_dir, self.skills_key)
        lib = SkillLibrary(library_dir=self.skills_dir, signing_key=self.skills_key)
        orch = PipelineOrchestrator(self.store, "web_api", skill_library=lib)
        orch.advance_if_ready()  # -> RECON
        orch.plan_tasks()
        tasks = self.store.list_tasks(phase="RECON")
        self.assertTrue(tasks)
        for t in tasks:
            params = json.loads(t["params_json"])
            self.assertEqual(params.get("skill_ids"), ["recon-web-skill"])

    def test_non_matching_profile_skill_not_attached(self):
        _write_and_sign_skill(self.skills_dir, self.skills_key, applicable_profiles=["network"])
        lib = SkillLibrary(library_dir=self.skills_dir, signing_key=self.skills_key)
        orch = PipelineOrchestrator(self.store, "web_api", skill_library=lib)
        orch.advance_if_ready()  # -> RECON
        orch.plan_tasks()
        tasks = self.store.list_tasks(phase="RECON")
        for t in tasks:
            params = json.loads(t["params_json"]) if t["params_json"] else {}
            self.assertNotIn("skill_ids", params)

    def test_skills_for_current_phase_helper(self):
        _write_and_sign_skill(self.skills_dir, self.skills_key)
        lib = SkillLibrary(library_dir=self.skills_dir, signing_key=self.skills_key)
        orch = PipelineOrchestrator(self.store, "web_api", skill_library=lib)
        orch.advance_if_ready()  # -> RECON
        skills = orch.skills_for_current_phase()
        self.assertEqual([s.skill_id for s in skills], ["recon-web-skill"])

    def test_skills_for_current_phase_empty_without_library(self):
        orch = PipelineOrchestrator(self.store, "web_api")
        self.assertEqual(orch.skills_for_current_phase(), [])


if __name__ == "__main__":
    unittest.main()
