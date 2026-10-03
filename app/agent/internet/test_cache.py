"""Tests for M5.5 internet cache/provenance."""
from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path

from .cache import InternetCache


class TestInternetCache(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="internet-cache-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cache = InternetCache(cache_dir=self.tmp)

    def test_miss_returns_none(self):
        self.assertIsNone(self.cache.get("knowledge_fetch", "http://example.test/"))

    def test_put_then_get_roundtrips(self):
        self.cache.put(
            "knowledge_fetch", "http://example.test/", "page content",
            provenance={"url": "http://example.test/", "fetched_via": "test"},
        )
        record = self.cache.get("knowledge_fetch", "http://example.test/")
        self.assertEqual(record["content"], "page content")
        self.assertEqual(record["provenance"]["url"], "http://example.test/")

    def test_expired_entry_returns_none(self):
        self.cache.put("knowledge_fetch", "http://example.test/", "content", provenance={})
        record = self.cache.get("knowledge_fetch", "http://example.test/", ttl_s=0)
        time.sleep(0.01)
        self.assertIsNone(self.cache.get("knowledge_fetch", "http://example.test/", ttl_s=0))

    def test_different_channels_dont_collide(self):
        self.cache.put("knowledge_fetch", "same-query", "fetch content", provenance={})
        self.cache.put("knowledge_search", "same-query", "search content", provenance={})
        self.assertEqual(self.cache.get("knowledge_fetch", "same-query")["content"], "fetch content")
        self.assertEqual(self.cache.get("knowledge_search", "same-query")["content"], "search content")


if __name__ == "__main__":
    unittest.main()
