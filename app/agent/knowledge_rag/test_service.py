"""KnowledgeRAGService's keyword-bonus re-ranking — pure function, no embedding server needed.
Regression test for a real bug found live: querying "find command sudo privilege escalation"
against GTFOBins ranked install/setcap/chattr above `find` itself, because every generic
"privilege escalation (sudo)" chunk shares near-identical boilerplate description text that
dominates cosine similarity over the one distinguishing word (the binary name)."""
from __future__ import annotations

import re
import unittest

from .service import _keyword_bonus

_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")


def _qt(query: str) -> set[str]:
    return set(_TOKEN_RE.findall(query.lower()))


class KeywordBonusTest(unittest.TestCase):
    def test_exact_tag_match_gets_a_bonus(self):
        hit = {"tags": ["find", "shell", "sudo", "linux"], "title": "find — Shell (sudo)"}
        q = "find command exec sh sudo"
        self.assertGreater(_keyword_bonus(q, hit, _qt(q)), 0.0)

    def test_no_overlap_gets_no_bonus(self):
        hit = {"tags": ["install", "privilege-escalation"], "title": "install — Privilege escalation"}
        q = "find command exec spawn shell"
        self.assertEqual(_keyword_bonus(q, hit, _qt(q)), 0.0)

    def test_title_only_overlap_is_smaller_than_tag_overlap(self):
        tag_hit = {"tags": ["find"], "title": "unrelated title"}
        title_hit = {"tags": [], "title": "something about find here"}
        q = "find"
        qt = _qt(q)
        self.assertGreater(_keyword_bonus(q, tag_hit, qt), _keyword_bonus(q, title_hit, qt))

    def test_empty_query_gets_no_bonus(self):
        hit = {"tags": ["find"], "title": "find"}
        self.assertEqual(_keyword_bonus("", hit, set()), 0.0)

    def test_missing_tags_field_does_not_crash(self):
        hit = {"title": "find — Shell (sudo)"}
        q = "find"
        self.assertGreaterEqual(_keyword_bonus(q, hit, _qt(q)), 0.0)


if __name__ == "__main__":
    unittest.main()
