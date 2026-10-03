"""Tests for M5.4 skill library (signing, integrity, read-only loading)."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from .library import SkillIntegrityError, SkillLibrary, SkillNotFoundError
from .schema import Skill
from .signing import sign, sign_file, verify, verify_file


def _write_skill(dir_: Path, **overrides) -> Path:
    data = dict(
        skill_id="test-skill-1",
        version="1.0",
        title="Test skill",
        description="A skill for tests",
        guidance="Do the thing carefully.",
        applicable_phases=["VALIDATION"],
        applicable_profiles=["web_api"],
        preconditions="",
        references=[],
        author="test",
        created_at="2026-08-31",
    )
    data.update(overrides)
    path = dir_ / f"{data['skill_id']}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


class TestSchema(unittest.TestCase):
    def test_empty_guidance_rejected(self):
        with self.assertRaises(ValueError):
            Skill(skill_id="x", version="1", title="t", description="d", guidance="   ")

    def test_bad_skill_id_rejected(self):
        with self.assertRaises(ValueError):
            Skill(skill_id="bad id!", version="1", title="t", description="d", guidance="g")


class TestSigning(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skills-signing-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.key = b"0" * 32

    def test_sign_and_verify_roundtrip(self):
        content = b"some skill content"
        sig = sign(content, key=self.key)
        self.assertTrue(verify(content, sig, key=self.key))

    def test_tampered_content_fails_verify(self):
        content = b"some skill content"
        sig = sign(content, key=self.key)
        self.assertFalse(verify(b"tampered content", sig, key=self.key))

    def test_wrong_key_fails_verify(self):
        content = b"some skill content"
        sig = sign(content, key=self.key)
        self.assertFalse(verify(content, sig, key=b"1" * 32))

    def test_sign_file_and_verify_file(self):
        path = _write_skill(self.tmp)
        sign_file(path, key=self.key)
        self.assertTrue(verify_file(path, key=self.key))

    def test_missing_sig_file_fails_closed(self):
        path = _write_skill(self.tmp)
        self.assertFalse(verify_file(path, key=self.key))

    def test_tampered_file_after_signing_fails(self):
        path = _write_skill(self.tmp)
        sign_file(path, key=self.key)
        path.write_text(path.read_text() + "\n# tampered\n")
        self.assertFalse(verify_file(path, key=self.key))


class TestSkillLibrary(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skills-library-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.key = b"0" * 32
        self.lib = SkillLibrary(library_dir=self.tmp, signing_key=self.key)

    def test_signed_skill_loads(self):
        path = _write_skill(self.tmp)
        sign_file(path, key=self.key)
        skills, problems = self.lib.load_all()
        self.assertEqual(len(skills), 1)
        self.assertEqual(problems, [])
        self.assertEqual(skills[0].skill_id, "test-skill-1")

    def test_unsigned_skill_reported_as_problem_not_silently_dropped(self):
        _write_skill(self.tmp)  # no sign_file() call
        skills, problems = self.lib.load_all()
        self.assertEqual(skills, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("signature", problems[0]["reason"])

    def test_tampered_skill_reported_as_problem(self):
        path = _write_skill(self.tmp)
        sign_file(path, key=self.key)
        path.write_text(path.read_text() + "\n# tampered\n")
        skills, problems = self.lib.load_all()
        self.assertEqual(skills, [])
        self.assertEqual(len(problems), 1)

    def test_verify_false_bypasses_signature_check(self):
        _write_skill(self.tmp)  # unsigned
        skills, problems = self.lib.load_all(verify=False)
        self.assertEqual(len(skills), 1)
        self.assertEqual(problems, [])

    def test_get_returns_matching_skill(self):
        path = _write_skill(self.tmp)
        sign_file(path, key=self.key)
        skill = self.lib.get("test-skill-1")
        self.assertEqual(skill.title, "Test skill")

    def test_get_unknown_raises_not_found(self):
        with self.assertRaises(SkillNotFoundError):
            self.lib.get("nonexistent")

    def test_get_tampered_raises_integrity_error_not_not_found(self):
        path = _write_skill(self.tmp)
        sign_file(path, key=self.key)
        path.write_text(path.read_text() + "\n# tampered\n")
        with self.assertRaises(SkillIntegrityError):
            self.lib.get("test-skill-1")

    def test_filter_by_phase_and_profile(self):
        p1 = _write_skill(self.tmp, skill_id="web-skill", applicable_phases=["VALIDATION"], applicable_profiles=["web_api"])
        sign_file(p1, key=self.key)
        p2 = _write_skill(self.tmp, skill_id="net-skill", applicable_phases=["RECON"], applicable_profiles=["network"])
        sign_file(p2, key=self.key)
        web_validation = self.lib.for_phase_and_profile("VALIDATION", "web_api")
        self.assertEqual([s.skill_id for s in web_validation], ["web-skill"])
        net_recon = self.lib.for_phase_and_profile("RECON", "network")
        self.assertEqual([s.skill_id for s in net_recon], ["net-skill"])

    def test_unscoped_skill_matches_every_phase_and_profile(self):
        path = _write_skill(self.tmp, skill_id="universal-skill", applicable_phases=[], applicable_profiles=[])
        sign_file(path, key=self.key)
        matches = self.lib.for_phase_and_profile("RECON", "network")
        self.assertEqual([s.skill_id for s in matches], ["universal-skill"])

    def test_malformed_yaml_reported_as_problem(self):
        path = self.tmp / "broken.yaml"
        path.write_text("not: valid: yaml: [")
        sign_file(path, key=self.key)
        skills, problems = self.lib.load_all()
        self.assertEqual(skills, [])
        self.assertEqual(len(problems), 1)


if __name__ == "__main__":
    unittest.main()
