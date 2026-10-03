"""Working Notebook — store/service semantics. No live model needed."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from .service import NotebookService
from .store import NotebookStore, NotebookValidationError, NotFoundError
from .technique_kb import TechniqueKB


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="notebook-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = NotebookStore(self.tmp)

    def test_ordinals_sequential_and_never_reused(self):
        a = self.store.add_note(category="recon", note="a")
        b = self.store.add_note(category="auth", note="b")
        self.assertEqual([a["ordinal"], b["ordinal"]], [1, 2])
        self.store.set_status(a["note_id"], "resolved", reason="done")
        c = self.store.add_note(category="misc", note="c")
        self.assertEqual(c["ordinal"], 3)  # not 1 reused

    def test_empty_and_bad_category_rejected(self):
        with self.assertRaises(NotebookValidationError):
            self.store.add_note(category="recon", note="   ")
        with self.assertRaises(ValueError):
            self.store.add_note(category="not-a-category", note="x")

    def test_tags_lowercased_and_stripped(self):
        r = self.store.add_note(category="injection", note="x", tags=[" XSS ", "", "Reflected"])
        self.assertEqual(self.store.get(r["note_id"])["tags"], ["xss", "reflected"])

    def test_invariants_and_event_log(self):
        self.store.add_note(category="recon", note="a")
        self.store.add_note(category="todo", note="b")
        self.assertEqual(self.store.check_invariants(), [])
        kinds = [e["kind"] for e in self.store.list_events()]
        self.assertEqual(kinds, ["note.added", "note.added"])


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="notebook-svc-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.kb = TechniqueKB(self.tmp / "technique_kb.db")   # never touch the real global KB
        self.svc = NotebookService(self.tmp, kb=self.kb)

    def test_resolve_and_reopen_by_ordinal(self):
        n = self.svc.add_note(category="todo", note="revisit /api/proxy once auth is captured")
        r = self.svc.resolve_note(f"N-{n['ordinal']}", "auth captured, tested — not vulnerable")
        self.assertEqual(r["status"], "resolved")
        self.assertIsNone(r["advisory"])  # todo is a lifecycle category
        r2 = self.svc.resolve_note(str(n["ordinal"]), "reopen: new lead suggests it is reachable")
        self.assertEqual(r2["status"], "open")

    def test_resolving_a_non_lifecycle_note_is_advisory(self):
        n = self.svc.add_note(category="recon", note="stale insight")
        r = self.svc.resolve_note(f"N-{n['ordinal']}", "no longer relevant")
        self.assertEqual(r["status"], "resolved")
        self.assertIn("not a todo/dead-end", r["advisory"])

    def test_search_matches_text_tag_category_and_surface(self):
        self.svc.add_note(category="injection", note="reflected XSS in search box", tags=["xss"], surface="/search")
        self.svc.add_note(category="auth", note="JWT alg none is closed", tags=["jwt"])
        self.assertEqual(len(self.svc.search("xss")), 1)
        self.assertEqual(len(self.svc.search("/search")), 1)
        self.assertEqual(len(self.svc.search("jwt")), 1)
        self.assertEqual(len(self.svc.search("", category="auth")), 1)
        self.assertEqual(len(self.svc.search("nothing-matches")), 0)

    def test_digest_empty_prompts_the_model(self):
        block = self.svc.build_context_block()
        self.assertIn("NOTEBOOK — empty", block)
        self.assertIn("dead-end", block)
        self.assertIn("not for hypotheses", block)

    def test_digest_pins_dead_ends_and_todos_then_recent(self):
        self.svc.add_note(category="recon", note="server is Express")
        self.svc.add_note(category="dead-end", note="tried JWT alg=none, kid traversal, weak HS256 — none present")
        self.svc.add_note(category="todo", note="check /api/proxy once authed")
        self.svc.add_note(category="technique", note="constructor.prototype gadget bypassed the sanitizer")
        block = self.svc.build_context_block()
        self.assertIn("RULED OUT (do not retry):", block)
        self.assertIn("OPEN TODO", block)
        self.assertIn("RECENT:", block)
        # a resolved dead-end drops out of the pinned section
        de = next(n for n in self.svc.store.list_notes(category="dead-end"))
        self.svc.resolve_note(f"N-{de['ordinal']}", "re-checked, still absent")
        self.assertNotIn("RULED OUT", self.svc.build_context_block())

    def test_operator_note_is_pinned_in_its_own_digest_section(self):
        r = self.svc.add_operator_note("focus on the checkout flow, not the blog",
                                       refs=["H-4"], surface="/checkout")
        note = self.svc.store.get_by_ordinal(r["ordinal"])
        self.assertEqual(note["category"], "misc")
        self.assertEqual(note["tags"], ["operator"])
        self.assertEqual(note["refs"], ["H-4"])
        # bury it behind newer model notes — it must still show, and above RECENT
        for i in range(15):
            self.svc.add_note(category="recon", note=f"observation {i}")
        block = self.svc.build_context_block()
        self.assertIn("FROM THE OPERATOR", block)
        self.assertIn("focus on the checkout flow", block)
        self.assertLess(block.index("FROM THE OPERATOR"), block.index("RECENT:"))
        # and the operator can retire it from the notebook panel
        self.svc.resolve_note(f"N-{r['ordinal']}", "done, checkout covered")
        self.assertNotIn("FROM THE OPERATOR", self.svc.build_context_block())

    def test_overview_shape_for_the_web_panel(self):
        self.svc.add_note(category="injection", note="x", tags=["xss"], surface="/s", refs=["H-3"])
        ov = self.svc.overview()
        self.assertEqual(ov["counts"], {"injection": 1})
        self.assertEqual(ov["notes"][0]["refs"], ["H-3"])
        self.assertIn("line", ov["notes"][0])

    def test_bad_ref_rejected(self):
        with self.assertRaises(NotebookValidationError):
            self.svc.resolve_note("not-a-ref", "x")
        with self.assertRaises(NotFoundError):
            self.svc.resolve_note("N-99", "x")

    def test_promote_a_todo_into_a_hypothesis(self):
        from ..hypothesis_graph.service import HypothesisGraphService

        graph = HypothesisGraphService(self.tmp)  # same engagement dir
        n = self.svc.add_note(category="todo", note="/api/proxy takes a url param — likely SSRF once authed",
                              surface="/api/proxy")
        r = self.svc.promote_note(
            f"N-{n['ordinal']}", graph, title="SSRF via /api/proxy",
            claim="/api/proxy fetches attacker-controlled URLs server-side",
            phase="ANALYSIS", impact=4, confidence_band="medium", confidence_reason="param seen",
        )
        self.assertEqual(r["hypothesis_ordinal"], 1)
        hyp = graph.store.get_by_ordinal(1)
        self.assertIn("promoted from notebook todo note N-1", hyp["rationale"])
        self.assertEqual(hyp["surface"], "/api/proxy")  # inherited from the note
        note = self.svc.store.get_by_ordinal(n["ordinal"])
        self.assertEqual(note["status"], "resolved")
        self.assertIn("H-1", note["refs"])

    def test_technique_note_overflows_to_the_global_kb_and_recalls(self):
        self.svc.add_note(category="technique",
                          note="constructor.prototype pollution gadget bypassed the sanitizer on /profile. Try it wherever user objects are merged server-side.",
                          tags=["prototype-pollution"], surface="/profile")
        self.assertEqual(self.kb.count(), 1)
        # a note in a DIFFERENT engagement recalls it (same global kb passed in)
        other = NotebookService(self.tmp / "eng-b", kb=self.kb)
        hits = other.recall_techniques(surface="/profile")
        self.assertEqual(len(hits), 1)
        self.assertIn("constructor.prototype", hits[0]["body"])
        hits2 = other.recall_techniques("prototype pollution")
        self.assertEqual(len(hits2), 1)
        # digest mentions the KB
        self.assertIn("technique(s) saved from past engagements", self.svc.build_context_block())

    def test_technique_kb_dedups_on_repeat(self):
        for _ in range(3):
            self.svc.add_note(category="technique", note="same trick, same words.", tags=["x"])
        self.assertEqual(self.kb.count(), 1)
        # only a non-technique note doesn't touch the kb
        self.svc.add_note(category="recon", note="server is Express")
        self.assertEqual(self.kb.count(), 1)

    def test_digest_auto_surfaces_a_relevant_technique_from_another_engagement(self):
        eng_a = NotebookService(self.tmp / "eng-a", kb=self.kb)
        eng_a.add_note(
            category="technique",
            note="GraphQL introspection on /graphql leaked a hidden deleteAllUsers mutation not in the docs.",
            tags=["graphql", "introspection"], surface="/graphql",
        )
        eng_b = NotebookService(self.tmp / "eng-b", kb=self.kb)
        eng_b.add_note(category="recon", note="found a GraphQL endpoint, about to try introspection",
                       surface="/graphql")
        block = eng_b.build_context_block()
        self.assertIn("RELEVANT FROM PAST ENGAGEMENTS", block)
        self.assertIn("deleteAllUsers", block)
        self.assertIn("from eng-a", block)

    def test_digest_never_auto_surfaces_a_technique_from_its_own_engagement(self):
        n = self.svc.add_note(
            category="technique",
            note="GraphQL introspection on /graphql leaked a hidden deleteAllUsers mutation not in the docs.",
            tags=["graphql"], surface="/graphql",
        )
        self.svc.add_note(category="recon", note="found a GraphQL endpoint, about to try introspection",
                          surface="/graphql")
        block = self.svc.build_context_block()
        # the note's own text still shows up via RECENT — only the KB auto-surface echo is excluded
        self.assertNotIn("RELEVANT FROM PAST ENGAGEMENTS", block)
        self.assertIn(f"N-{n['ordinal']}", block)

    def test_promote_needs_the_graph_and_an_open_note(self):
        n = self.svc.add_note(category="todo", note="x")
        with self.assertRaises(NotebookValidationError):
            self.svc.promote_note(f"N-{n['ordinal']}", None, title="t", claim="c", phase="RECON",
                                  impact=3, confidence_band="low", confidence_reason="r")
        from ..hypothesis_graph.service import HypothesisGraphService
        graph = HypothesisGraphService(self.tmp)
        self.svc.resolve_note(f"N-{n['ordinal']}", "not pursuing")
        with self.assertRaises(NotebookValidationError):
            self.svc.promote_note(f"N-{n['ordinal']}", graph, title="t", claim="c", phase="RECON",
                                  impact=3, confidence_band="low", confidence_reason="r")


if __name__ == "__main__":
    unittest.main()
