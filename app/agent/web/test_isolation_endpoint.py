"""GET /api/isolation exists so no client carries its own list of sandbox tiers.

create_session validates `isolation_tier` against sandbox/availability.TIERS and answers 400 on an
unknown name, so a picker built from a hardcoded list offers the operator something the server
will refuse -- and the refusal reads as the session being rejected rather than as the picker being
out of date. Same failure mode as the action classes, which had drifted three ways before anyone
noticed, so the same remedy: derive, serve, and assert both directions.

The probe's own `reason` is passed through rather than summarised. A tier this host cannot provide
is a property of the host, and the only useful thing to show an operator is why.
"""
from __future__ import annotations

import unittest

from ..sandbox import availability as av
from . import dev_server


class IsolationEndpointTest(unittest.TestCase):
    def setUp(self):
        self.payload = dev_server.describe_isolation()

    def test_it_reports_exactly_the_tiers_the_sandbox_module_knows(self):
        """Both directions. A tier the server offers and availability does not know is one
        create_session will refuse; a tier availability gained and this does not report is one no
        operator can choose, which is the quieter half and the reason for set equality."""
        served = [t["tier"] for t in self.payload["tiers"]]
        self.assertEqual(
            set(served), set(av.TIERS),
            f"/api/isolation and sandbox.availability.TIERS disagree.\n"
            f"  served, not a known tier: {sorted(set(served) - set(av.TIERS)) or 'none'}\n"
            f"  a known tier, not served: {sorted(set(av.TIERS) - set(served)) or 'none'}",
        )

    def test_the_order_is_the_probe_s_own(self):
        # availability.TIERS runs weakest to strongest, and a picker should read in that order.
        # Sorting alphabetically would put bubblewrap before direct and microvm before wsl2,
        # which reads as a ranking and is not one.
        self.assertEqual([t["tier"] for t in self.payload["tiers"]], list(av.TIERS))

    def test_availability_and_reason_come_from_the_probe_unchanged(self):
        host = av.describe_host()
        for row in self.payload["tiers"]:
            probe = host["tiers"][row["tier"]]
            self.assertEqual(row["available"], bool(probe.get("available")), row["tier"])
            self.assertEqual(row["reason"], probe.get("reason", ""), row["tier"])

    def test_an_unavailable_tier_still_says_why(self):
        """The one thing the UI cannot invent.

        Asserted only for tiers this runner reports as unavailable, so the test is about the
        contract and not about which tiers a particular machine happens to have: on a Linux
        runner that is wsl2 and microvm, on a Windows host it would be bubblewrap.
        """
        unavailable = [t for t in self.payload["tiers"] if not t["available"]]
        self.assertTrue(unavailable, "no tier is unavailable on this host, so this check proved "
                                     "nothing -- if that is now true everywhere, delete it")
        for row in unavailable:
            self.assertTrue(row["reason"], f"{row['tier']} is unavailable with no reason given")

    def test_the_modes_are_the_pair_create_session_accepts(self):
        """WHERE the I/O runs is a different question from WHICH sandbox confines it, and the two
        were easy to conflate while nothing named them side by side. These two strings are what
        create_session validates `isolation` against."""
        self.assertEqual([m["mode"] for m in self.payload["modes"]], ["cloud", "this_computer"])
        for row in self.payload["modes"]:
            self.assertTrue(row["note"], f"mode {row['mode']} has no explanation")

    def test_the_default_tier_is_one_of_the_tiers_and_the_one_create_session_defaults_to(self):
        self.assertIn(self.payload["default_tier"], [t["tier"] for t in self.payload["tiers"]])
        self.assertEqual(self.payload["default_tier"],
                         dev_server.CreateSessionRequest().isolation_tier,
                         "the endpoint's default_tier and CreateSessionRequest's default differ, "
                         "so the UI would pre-select a tier the API does not")


if __name__ == "__main__":
    unittest.main()
