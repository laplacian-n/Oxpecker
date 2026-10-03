"""Tests for M5.1 structured engagement intake."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from ..broker.policy import load_policy
from .intake import EngagementIntake, IntakeValidationError, create_engagement


def _base_kwargs(**overrides) -> dict:
    kwargs = dict(
        engagement_id="test-eng-1",
        description="Test engagement for intake unit tests",
        allow_targets=["127.0.0.1/32", "juice-shop.local"],
        valid_from="2020-01-01T00:00:00Z",
        valid_until="2099-01-01T00:00:00Z",
        allowed_action_classes=["passive_recon", "active_scan_light"],
        authorized_by="test-owner",
        operator_sign_off=True,
    )
    kwargs.update(overrides)
    return kwargs


class TestIntakeValidation(unittest.TestCase):
    def test_missing_sign_off_rejected(self):
        intake = EngagementIntake(**_base_kwargs(operator_sign_off=False))
        with self.assertRaises(IntakeValidationError) as cm:
            intake.validate()
        self.assertIn("operator_sign_off", str(cm.exception))

    def test_no_allow_targets_rejected(self):
        intake = EngagementIntake(**_base_kwargs(allow_targets=[]))
        with self.assertRaises(IntakeValidationError):
            intake.validate()

    def test_no_action_classes_rejected(self):
        intake = EngagementIntake(**_base_kwargs(allowed_action_classes=[]))
        with self.assertRaises(IntakeValidationError):
            intake.validate()

    def test_bad_time_window_rejected(self):
        intake = EngagementIntake(
            **_base_kwargs(valid_from="2099-01-01T00:00:00Z", valid_until="2020-01-01T00:00:00Z")
        )
        with self.assertRaises(IntakeValidationError):
            intake.validate()

    def test_malformed_target_rejected(self):
        intake = EngagementIntake(**_base_kwargs(allow_targets=["not a valid target!!"]))
        with self.assertRaises(IntakeValidationError):
            intake.validate()

    def test_bad_retention_class_rejected(self):
        intake = EngagementIntake(**_base_kwargs(evidence_retention_default="forever"))
        with self.assertRaises(IntakeValidationError):
            intake.validate()

    def test_valid_intake_passes(self):
        intake = EngagementIntake(**_base_kwargs())
        intake.validate()  # must not raise


class TestCreateEngagement(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engagements-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_creates_expected_files(self):
        intake = EngagementIntake(**_base_kwargs())
        eng_dir = create_engagement(intake, engagements_root=self.tmp)
        self.assertTrue((eng_dir / "roe.json").exists())
        self.assertTrue((eng_dir / "scope.txt").exists())
        self.assertTrue((eng_dir / "deny.txt").exists())

    def test_invalid_intake_writes_nothing(self):
        intake = EngagementIntake(**_base_kwargs(operator_sign_off=False))
        with self.assertRaises(IntakeValidationError):
            create_engagement(intake, engagements_root=self.tmp)
        self.assertFalse((self.tmp / "test-eng-1").exists())

    def test_duplicate_engagement_id_rejected(self):
        intake = EngagementIntake(**_base_kwargs())
        create_engagement(intake, engagements_root=self.tmp)
        with self.assertRaises(IntakeValidationError):
            create_engagement(intake, engagements_root=self.tmp)

    def test_created_engagement_loads_via_existing_policy_loader(self):
        intake = EngagementIntake(**_base_kwargs())
        eng_dir = create_engagement(intake, engagements_root=self.tmp)
        policy = load_policy(engagement_dir=eng_dir)
        self.assertEqual(policy.engagement_id, "test-eng-1")
        self.assertEqual(policy.allowed_action_classes, {"passive_recon", "active_scan_light"})
        self.assertIn("juice-shop.local", policy.allow_hostnames)

    def test_base_deny_always_present(self):
        intake = EngagementIntake(**_base_kwargs())
        eng_dir = create_engagement(intake, engagements_root=self.tmp)
        deny_text = (eng_dir / "deny.txt").read_text()
        self.assertIn("169.254.169.254/32", deny_text)

    def test_extra_deny_targets_included(self):
        intake = EngagementIntake(**_base_kwargs(deny_targets=["10.0.0.5/32"]))
        eng_dir = create_engagement(intake, engagements_root=self.tmp)
        deny_text = (eng_dir / "deny.txt").read_text()
        self.assertIn("10.0.0.5/32", deny_text)

    def test_roe_json_carries_rich_metadata(self):
        import json

        intake = EngagementIntake(
            **_base_kwargs(
                credential_handles=["vault:juice-shop-admin"],
                contacts=["owner@example.test"],
            )
        )
        eng_dir = create_engagement(intake, engagements_root=self.tmp)
        roe = json.loads((eng_dir / "roe.json").read_text())
        self.assertEqual(roe["credential_handles"], ["vault:juice-shop-admin"])
        self.assertEqual(roe["contacts"], ["owner@example.test"])


if __name__ == "__main__":
    unittest.main()
