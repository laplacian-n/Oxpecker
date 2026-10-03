"""Tests for M5.5 knowledge_search/knowledge_fetch policy (provider allowlist, SSRF-relevant
URL/IP-range blocking)."""
from __future__ import annotations

import socket
import unittest
from unittest.mock import patch

from .policy import (
    FetchPolicyError,
    SearchPolicyError,
    check_knowledge_fetch_allowed,
    check_knowledge_search_allowed,
)


class TestKnowledgeSearchPolicy(unittest.TestCase):
    def test_no_providers_configured_denies_everything(self):
        with self.assertRaises(SearchPolicyError):
            check_knowledge_search_allowed("acme-search")

    def test_provider_in_allowlist_permitted(self):
        check_knowledge_search_allowed("acme-search", allowed_providers=frozenset({"acme-search"}))

    def test_provider_not_in_allowlist_denied(self):
        with self.assertRaises(SearchPolicyError):
            check_knowledge_search_allowed("evil-search", allowed_providers=frozenset({"acme-search"}))


class TestKnowledgeFetchPolicy(unittest.TestCase):
    def test_non_http_scheme_rejected(self):
        with self.assertRaises(FetchPolicyError):
            check_knowledge_fetch_allowed("ftp://example.com/file")

    def test_no_host_rejected(self):
        with self.assertRaises(FetchPolicyError):
            check_knowledge_fetch_allowed("http:///path")

    def test_loopback_blocked(self):
        with self.assertRaises(FetchPolicyError):
            check_knowledge_fetch_allowed("http://127.0.0.1/")

    def test_dns_failure_reported(self):
        with self.assertRaises(FetchPolicyError):
            check_knowledge_fetch_allowed("http://this-does-not-resolve.invalid/")

    def test_cloud_metadata_ip_blocked(self):
        with self.assertRaises(FetchPolicyError):
            check_knowledge_fetch_allowed("http://169.254.169.254/latest/meta-data/")

    def test_private_range_via_dns_rebinding_blocked(self):
        # simulate a hostname that resolves to a private IP — same DNS-rebinding shape
        # agent/broker/test_policy_bypass.py already exercises for the RoE-scoped path
        real_getaddrinfo = socket.getaddrinfo

        def fake_getaddrinfo(host, *args, **kwargs):
            if host == "rebinding.example.test":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]
            return real_getaddrinfo(host, *args, **kwargs)

        with patch("socket.getaddrinfo", side_effect=fake_getaddrinfo):
            with self.assertRaises(FetchPolicyError):
                check_knowledge_fetch_allowed("http://rebinding.example.test/")

    def test_public_looking_ip_allowed(self):
        with patch(
            "socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
        ):
            check_knowledge_fetch_allowed("http://example.test/")  # must not raise


if __name__ == "__main__":
    unittest.main()
