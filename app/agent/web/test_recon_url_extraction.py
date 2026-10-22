"""The autonomous pipeline recons a non-standard port only if the operator's target URL reaches it.

Engagement scope is host-only (the broker gates hosts, not ports), so a service on e.g. :3000 is
reachable but the deterministic RECON seeder, left to the bare scope host, recons :80/:443 and seeds
nothing. dev_server pulls the target URLs out of the run message and passes them through; this pins
that extraction, including the trailing-punctuation bug that silently broke the port
("...:3000." kept the period, so urlparse found no port and the seeder fell back to the bare host).
"""
from __future__ import annotations

import unittest
from urllib.parse import urlparse

from . import dev_server


class ExtractReconUrlsTest(unittest.TestCase):
    def test_a_plain_url_is_taken_as_is(self):
        self.assertEqual(dev_server._extract_recon_urls("scan http://127.0.0.1:3000/login"),
                         ["http://127.0.0.1:3000/login"])

    def test_a_trailing_sentence_period_is_stripped_so_the_port_still_parses(self):
        urls = dev_server._extract_recon_urls(
            "Penetration-test http://127.0.0.1:3000. Find and verify vulnerabilities.")
        self.assertEqual(urls, ["http://127.0.0.1:3000"])
        self.assertEqual(urlparse(urls[0]).port, 3000)  # the bug: a kept '.' made this None

    def test_several_urls_and_other_trailing_punctuation(self):
        self.assertEqual(
            dev_server._extract_recon_urls("hit https://a.test:8443/) and http://b.test:3000,"),
            ["https://a.test:8443/", "http://b.test:3000"])

    def test_no_url_and_empty_are_safe(self):
        self.assertEqual(dev_server._extract_recon_urls("just scan the lab box"), [])
        self.assertEqual(dev_server._extract_recon_urls(None), [])
        self.assertEqual(dev_server._extract_recon_urls(""), [])


if __name__ == "__main__":
    unittest.main()
