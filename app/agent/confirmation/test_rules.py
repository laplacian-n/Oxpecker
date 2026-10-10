"""Tests for confirmation rules (§8.6.4 / §8.6.6).

The enforcement test at the bottom is the important one: every registered rule must ship at
least one positive and one negative fixture, and its check() must agree with both. A rule
with only passing cases is a rule that says yes to everything.
"""
from __future__ import annotations

import unittest

from .rules import (
    OPEN_PORT,
    VERSION_BANNER,
    Fixture,
    Rule,
    all_rules,
    get,
    register,
    rule_ref,
)


class TestRuleRef(unittest.TestCase):
    def test_rule_ref_formats_id_and_version(self):
        rule = Rule(
            rule_id="example",
            version=3,
            description="d",
            check=lambda obs: False,
            positive_fixtures=[],
            negative_fixtures=[],
        )
        self.assertEqual(rule_ref(rule), "example@3")
        self.assertEqual(rule_ref(OPEN_PORT), "open_port@1")


class TestRegister(unittest.TestCase):
    def test_register_rejects_duplicate_rule_id(self):
        duplicate = Rule(
            rule_id="open_port",
            version=99,
            description="imposter",
            check=lambda obs: True,
            positive_fixtures=[],
            negative_fixtures=[],
        )
        with self.assertRaises(ValueError):
            register(duplicate)
        # The rejected rule must not have replaced the original.
        self.assertIs(get("open_port"), OPEN_PORT)

    def test_get_returns_none_for_unknown_rule(self):
        self.assertIsNone(get("no_such_rule"))

    def test_all_rules_contains_both_example_rules(self):
        ids = {r.rule_id for r in all_rules()}
        self.assertIn("open_port", ids)
        self.assertIn("version_banner", ids)


class TestOpenPort(unittest.TestCase):
    def test_true_when_an_open_port_is_observed(self):
        obs = [
            {"type": "port", "port": 22, "state": "closed"},
            {"type": "port", "port": 80, "state": "open"},
        ]
        self.assertTrue(OPEN_PORT.check(obs))

    def test_false_when_only_closed_ports_are_observed(self):
        obs = [{"type": "port", "port": 22, "state": "closed"}]
        self.assertFalse(OPEN_PORT.check(obs))

    def test_false_for_non_port_observations(self):
        obs = [{"type": "banner", "value": "state open"}]
        self.assertFalse(OPEN_PORT.check(obs))

    def test_false_for_empty_observations(self):
        self.assertFalse(OPEN_PORT.check([]))


class TestVersionBanner(unittest.TestCase):
    def test_true_when_banner_contains_a_version(self):
        obs = [{"type": "banner", "value": "nginx/1.21.0"}]
        self.assertTrue(VERSION_BANNER.check(obs))

    def test_false_when_banner_has_no_version(self):
        obs = [{"type": "banner", "value": "nginx"}]
        self.assertFalse(VERSION_BANNER.check(obs))

    def test_false_when_version_is_on_a_non_banner_observation(self):
        obs = [{"type": "port", "state": "open", "value": "1.2"}]
        self.assertFalse(VERSION_BANNER.check(obs))

    def test_false_for_empty_observations(self):
        self.assertFalse(VERSION_BANNER.check([]))


class TestSection86_6Enforcement(unittest.TestCase):
    """§8.6.6: every rule ships >=1 positive and >=1 negative fixture, and check() agrees."""

    def test_every_registered_rule_has_fixtures_that_check_agrees_with(self):
        rules = all_rules()
        self.assertTrue(rules, "registry is empty; nothing to enforce")
        for rule in rules:
            with self.subTest(rule=rule_ref(rule)):
                self.assertGreaterEqual(
                    len(rule.positive_fixtures), 1,
                    f"{rule_ref(rule)} has no positive fixture (§8.6.6)",
                )
                self.assertGreaterEqual(
                    len(rule.negative_fixtures), 1,
                    f"{rule_ref(rule)} has no negative fixture (§8.6.6)",
                )
                for fx in rule.positive_fixtures:
                    self.assertIsInstance(fx, Fixture)
                    self.assertIs(
                        rule.check(fx.observations), True,
                        f"{rule_ref(rule)} positive fixture {fx.name!r} must be True",
                    )
                for fx in rule.negative_fixtures:
                    self.assertIsInstance(fx, Fixture)
                    self.assertIs(
                        rule.check(fx.observations), False,
                        f"{rule_ref(rule)} negative fixture {fx.name!r} must be False",
                    )


if __name__ == "__main__":
    unittest.main()
