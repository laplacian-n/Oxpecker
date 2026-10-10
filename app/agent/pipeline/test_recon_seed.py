"""Tests for the deterministic RECON seeder (recon_seed.py).

RECON must produce testable child hypotheses even when the model's recon pass writes none — that
gap left every autonomous engagement closing out after one shallow pass with an empty graph. These
tests pin what the deterministic rules extract from an http_recon result.
"""
from __future__ import annotations

import unittest

from .recon_seed import build_recon_hypotheses

_RECON = {
    "ok": True,
    "final_url": "https://t.example/",
    "final_status": 200,
    "final_headers": {
        "Server": "Apache/2.4.29",
        "X-Powered-By": "PHP/7.2.1",
        "Set-Cookie": "session=abc",
        "Content-Type": "text/html",
    },
    "body_excerpt": (
        '<html><form action="/login" method="post"><input name="u"></form>'
        '<a href="/admin">a</a><a href="/api/orders?id=1">o</a>'
        '<script src="/static/app.js"></script>'
        '<script>fetch("/api/user/profile");const k="/api/v2/search?q=";</script>'
        '<a href="https://other.example/x">external</a></html>'
    ),
}


class ReconSeedTest(unittest.TestCase):
    def setUp(self):
        self.hs = build_recon_hypotheses(_RECON, "https://t.example/")
        self.claims = " || ".join(h["claim"] for h in self.hs)

    def test_flags_missing_security_headers(self):
        for token in ("Content-Security-Policy", "Strict-Transport-Security",
                      "X-Frame-Options", "X-Content-Type-Options"):
            self.assertIn(token, self.claims)

    def test_flags_version_disclosure(self):
        self.assertIn("Apache/2.4.29", self.claims)
        self.assertIn("PHP/7.2.1", self.claims)

    def test_flags_insecure_cookie(self):
        self.assertTrue(any("cookie" in h["claim"].lower() and "secure" in h["claim"].lower()
                            for h in self.hs))

    def test_flags_form_present(self):
        self.assertTrue(any("form" in h["claim"].lower() for h in self.hs))

    def test_discovers_endpoints_from_html_and_js(self):
        surfaces = " ".join((h.get("surface") or "") for h in self.hs)
        for path in ("/admin", "/api/orders", "/static/app.js", "/api/user/profile"):
            self.assertIn(path, surfaces)

    def test_stays_same_origin(self):
        # the external link must NOT become a child — it is outside this root's scope
        self.assertNotIn("other.example", " ".join((h.get("surface") or "") for h in self.hs))

    def test_every_hypothesis_is_well_formed(self):
        for h in self.hs:
            for field in ("title", "claim", "rationale", "impact", "confidence_band",
                          "confidence_reason", "phase_created", "origin_type"):
                self.assertIn(field, h)
            self.assertEqual(h["phase_created"], "RECON")
            self.assertEqual(h["origin_type"], "tool_observation")
            self.assertIn(h["confidence_band"], ("low", "medium", "high"))
            self.assertTrue(1 <= h["impact"] <= 5)

    def test_empty_or_failed_recon_seeds_nothing(self):
        self.assertEqual(build_recon_hypotheses({"ok": False}, "https://t.example/"), [])
        self.assertEqual(build_recon_hypotheses({}, "https://t.example/"), [])


if __name__ == "__main__":
    unittest.main()
