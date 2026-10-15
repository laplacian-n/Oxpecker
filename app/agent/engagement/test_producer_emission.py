"""A1 — the structured-store producers emit their §4.2 events to the engagement log, so the Work
surface's tree/notebook/findings light up with real data instead of only synthetic test events.
Each is a break-it-first assertion: the mutation happens, and the matching event is on the log.
Telemetry is best-effort (it never breaks the mutation), so these also pin that it is actually
wired, not silently swallowed.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.engagement.event_log import EngagementEventLog


class ProducerEmissionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="producer-emit-"))
        self.root = self.tmp / "engagements"
        self.eng_dir = self.root / "eng1"
        self.eng_dir.mkdir(parents=True)
        # emit writes to config.ENGAGEMENTS_ROOT/<id>/events.db; with the engagement dir under the
        # patched root, that is exactly eng_dir/events.db — where the services' own stores live.
        self._p = patch("agent.config.ENGAGEMENTS_ROOT", self.root)
        self._p.start()
        self.addCleanup(self._p.stop)
        self._findings_p = patch("agent.config.FINDINGS_DIR", self.tmp / "findings")
        self._findings_p.start()
        self.addCleanup(self._findings_p.stop)

    def _kinds(self):
        log = EngagementEventLog.open_if_exists(self.eng_dir)
        return [e["kind"] for e in log.read_since(0)] if log else []

    def test_hypothesis_graph_mutations_emit_graph_node_changed(self):
        from agent.hypothesis_graph.service import HypothesisGraphService

        svc = HypothesisGraphService(self.eng_dir)
        r = svc.add_hypothesis(
            title="SQLi on /login", claim="the login form is injectable",
            phase_created="ANALYSIS", rationale="error-based signal in the response",
            origin_type="tool_observation", impact=4, confidence_band="medium",
            confidence_reason="one reflected error",
        )
        self.assertEqual(self._kinds().count("graph_node_changed"), 1)  # creation
        svc.set_verdict(f"H-{r['ordinal']}", "refuted", confidence_band="high", confidence_reason="patched")
        svc.park(f"H-{r['ordinal']}", "shelving for now")
        # creation + verdict + park = three node-changed events for this node.
        self.assertEqual(self._kinds().count("graph_node_changed"), 3)

    def test_notebook_add_note_emits_note_added(self):
        from agent.notebook.service import NotebookService

        NotebookService(self.eng_dir).add_note(category="todo", note="probe /admin for IDOR")
        self.assertEqual(self._kinds().count("note_added"), 1)

    def test_findings_record_emits_finding_recorded(self):
        from agent.findings.model import Finding, FindingsStore

        store = FindingsStore("eng1", findings_dir=self.tmp / "findings")
        store.add(Finding(
            title="IDOR", severity="high", target="https://x/item", description="d",
            remediation="r", tool="http_request", session_id="s1", engagement_id="eng1",
        ))
        self.assertEqual(self._kinds().count("finding_recorded"), 1)

    def _payloads(self, kind):
        log = EngagementEventLog.open_if_exists(self.eng_dir)
        return [e["payload"] for e in (log.read_since(0) if log else []) if e["kind"] == kind]

    def test_notebook_add_note_also_emits_artifact_stored(self):
        """A note is a worker's output landing in a store, so the flow view needs the edge
        (§6.3.1 "where the output goes"). note_added drives the tab's count; it carries no store,
        so it cannot draw the edge — artifact_stored is the one that can."""
        from agent.notebook.service import NotebookService

        r = NotebookService(self.eng_dir).add_note(category="todo", note="probe /admin for IDOR")
        [a] = self._payloads("artifact_stored")
        self.assertEqual(a["store"], "notebook")
        self.assertEqual(a["ref"], r["note_id"])
        self.assertEqual(a["artifact"], f"N-{r['ordinal']}")

    def test_findings_add_also_emits_artifact_stored(self):
        from agent.findings.model import Finding, FindingsStore

        store = FindingsStore("eng1", findings_dir=self.tmp / "findings")
        f = store.add(Finding(
            title="IDOR on /item", severity="high", target="https://x/item", description="d",
            remediation="r", tool="http_request", session_id="s1", engagement_id="eng1",
        ))
        [a] = self._payloads("artifact_stored")
        self.assertEqual(a["store"], "findings")
        self.assertEqual(a["ref"], f.finding_id)
        self.assertEqual(a["artifact"], "IDOR on /item")

    def test_an_operator_note_is_not_an_agent_artifact(self):
        """The operator's own note belongs in the notebook tab (note_added), but it is not output
        the agent produced. Emitting artifact_stored for it would draw an edge from the worker
        pool to the notebook for work no worker did — the inverse of the §6.3.1 signal."""
        from agent.notebook.service import NotebookService

        NotebookService(self.eng_dir).add_operator_note("check the staging host too")
        self.assertEqual(self._kinds().count("note_added"), 1)
        self.assertEqual(self._payloads("artifact_stored"), [])

    def test_the_storing_worker_is_named_when_one_is_running(self):
        """Attribution: inside a wave worker's thread the artifact must name the worker, so the
        flow view draws the edge from that worker's node rather than the anonymous pool."""
        from agent.engagement import emit as event_emit
        from agent.notebook.service import NotebookService

        event_emit.set_current_worker("w3")
        self.addCleanup(event_emit.set_current_worker, None)
        NotebookService(self.eng_dir).add_note(category="todo", note="x")
        self.assertEqual(self._payloads("artifact_stored")[0]["worker"], "w3")


if __name__ == "__main__":
    unittest.main()
