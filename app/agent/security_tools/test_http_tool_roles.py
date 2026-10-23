"""http_recon maps, http_request tests — the two HTTP tools must keep distinct, non-overlapping
roles in what the model is told, or the model routes injection payloads through the passive-gated
recon tool (observed live: 108 injection payloads sent via http_recon before http_request existed).
Pins that each schema steers the model to the other for the job it is not for."""
from __future__ import annotations

import unittest

from . import http_recon, http_request


class HttpToolRolesTest(unittest.TestCase):
    def test_recon_says_it_is_mapping_only_and_points_payloads_at_http_request(self):
        desc = http_recon.SCHEMA["function"]["description"].lower()
        self.assertIn("http_request", desc)          # it names the active tool
        self.assertIn("payload", desc)               # and warns about payloads
        self.assertTrue("not for" in desc or "do not" in desc or "never" in desc)

    def test_request_is_the_active_payload_tool(self):
        desc = http_request.SCHEMA["function"]["description"].lower()
        self.assertTrue("payload" in desc or "exploitation" in desc or "injection" in desc)
        # distinct action-class gating is the whole reason the split matters
        from ..broker.broker import TOOL_ACTION_CLASS
        self.assertEqual(TOOL_ACTION_CLASS["http_recon"], "passive_recon")
        self.assertEqual(TOOL_ACTION_CLASS["http_request"], "active_web_request")


if __name__ == "__main__":
    unittest.main()
