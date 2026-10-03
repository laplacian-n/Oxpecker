"""Tests for the Phase 6 web UI backend (agent/web/server.py). Session/approval routing and SSE
event delivery are tested with AgentLoop.run_task() mocked (fast, no live model needed);
TestLiveEndToEnd exercises the real flow against the actual running llama-server, skipped
gracefully if it isn't reachable.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from .. import config
from ..broker.approval_queue import ApprovalQueue
from ..broker.consult_queue import ConsultQueue
from ..loop import TaskResult
from ..pipeline.autonomous_driver import AutonomousRunResult
from . import server as server_mod


class TestWebServerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="web-server-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._sessions_dir_patcher = patch("agent.config.SESSIONS_DIR", self.tmp / "sessions")
        self._sessions_dir_patcher.start()
        self.addCleanup(self._sessions_dir_patcher.stop)
        self._audit_dir_patcher = patch("agent.config.AUDIT_DIR", self.tmp / "audit")
        self._audit_dir_patcher.start()
        self.addCleanup(self._audit_dir_patcher.stop)
        self._steer_dir_patcher = patch("agent.loop_control.steer.STEER_DIR", self.tmp / "steer")
        self._steer_dir_patcher.start()
        self.addCleanup(self._steer_dir_patcher.stop)
        self._approval_patcher = patch.object(
            server_mod, "_approval_queue", ApprovalQueue(queue_dir=self.tmp / "approvals")
        )
        self._approval_patcher.start()
        self.addCleanup(self._approval_patcher.stop)
        self._sessions_registry_patcher = patch.object(server_mod, "_sessions", {})
        self._sessions_registry_patcher.start()
        self.addCleanup(self._sessions_registry_patcher.stop)
        self._consult_patcher = patch.object(
            server_mod, "_consult_queue", ConsultQueue(queue_dir=self.tmp / "consults")
        )
        self._consult_patcher.start()
        self.addCleanup(self._consult_patcher.stop)
        self._engagements_root_patcher = patch(
            "agent.config.ENGAGEMENTS_ROOT", self.tmp / "engagements"
        )
        self._engagements_root_patcher.start()
        self.addCleanup(self._engagements_root_patcher.stop)
        self._kb_patcher = patch("agent.config.TECHNIQUE_KB_PATH", self.tmp / "technique_kb.db")
        self._kb_patcher.start()
        self.addCleanup(self._kb_patcher.stop)
        self._findings_dir_patcher = patch("agent.config.FINDINGS_DIR", self.tmp / "findings")
        self._findings_dir_patcher.start()
        self.addCleanup(self._findings_dir_patcher.stop)
        # config.WEB_UI_API_KEY_FILE is a bound default (STATE_DIR / "..." computed once at
        # config.py's *import* time) — every other STATE_DIR-derived patch above only redirects
        # a path that's re-read fresh at call time, but this one isn't, so it must be patched
        # explicitly or this whole test file's isolation from the real machine's key-file state
        # is incomplete. Found live 2026-09-05: generating a real key on this box mid-test-run
        # 401'd every test here that doesn't send an Authorization header — every test in this
        # file, not just TestApiKeyAuth's own. Points at a guaranteed-nonexistent path by
        # default; TestApiKeyAuth._set_key() patches it again, per-test, to something that does.
        self._api_key_patcher = patch("agent.config.WEB_UI_API_KEY_FILE", self.tmp / "no-such-key-file")
        self._api_key_patcher.start()
        self.addCleanup(self._api_key_patcher.stop)
        self.client = TestClient(server_mod.app)


class TestApiKeyAuth(TestWebServerBase):
    def _set_key(self, key: str) -> Path:
        key_file = self.tmp / "web_ui_api_key.txt"
        key_file.write_text(key + "\n")  # trailing newline, like a human-edited file
        patcher = patch("agent.config.WEB_UI_API_KEY_FILE", key_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        return key_file

    def test_no_key_file_is_unauthenticated_exactly_like_before(self):
        # WEB_UI_API_KEY_FILE not patched at all in this test -> resolves to the real default
        # path, which doesn't exist in this sandboxed test env either way. This is the behavior
        # every other test in this file relies on.
        resp = self.client.post("/api/sessions", json={})
        self.assertEqual(resp.status_code, 200)

    def test_missing_auth_header_401s_when_a_key_is_configured(self):
        self._set_key("s3cret")
        resp = self.client.post("/api/sessions", json={})
        self.assertEqual(resp.status_code, 401)

    def test_wrong_key_401s(self):
        self._set_key("s3cret")
        resp = self.client.post(
            "/api/sessions", json={}, headers={"Authorization": "Bearer wrong"}
        )
        self.assertEqual(resp.status_code, 401)

    def test_correct_bearer_header_is_accepted(self):
        self._set_key("s3cret")
        resp = self.client.post(
            "/api/sessions", json={}, headers={"Authorization": "Bearer s3cret"}
        )
        self.assertEqual(resp.status_code, 200)

    def test_query_param_fallback_is_accepted_for_eventsource_routes(self):
        # A real EventSource can't set headers — the ?key= param is the escape hatch for it.
        self._set_key("s3cret")
        session_id = self.client.post(
            "/api/sessions", json={}, headers={"Authorization": "Bearer s3cret"}
        ).json()["session_id"]
        resp = self.client.get(f"/api/sessions/{session_id}?key=s3cret")
        self.assertEqual(resp.status_code, 200)

    def test_static_page_is_never_gated(self):
        self._set_key("s3cret")
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)


class TestSessionLifecycle(TestWebServerBase):
    def test_create_session_returns_expected_fields(self):
        resp = self.client.post("/api/sessions", json={})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("session_id", body)
        self.assertEqual(body["profile"], config.PROFILE_SAFE_DEFAULT)
        self.assertFalse(body["use_security_tools"])

    def test_security_tools_session_enables_the_hypothesis_graph(self):
        captured = {}

        def spy(*a, **kw):
            captured.clear()
            captured.update(kw)
            return MagicMock()

        with patch.object(server_mod, "AgentLoop", side_effect=spy):
            self.client.post("/api/sessions", json={"use_security_tools": True})
        self.assertTrue(captured["use_hypothesis_graph"])  # panel would stay empty otherwise

        with patch.object(server_mod, "AgentLoop", side_effect=spy):
            self.client.post("/api/sessions", json={})
        self.assertFalse(captured["use_hypothesis_graph"])

    def test_reasoning_stream_callback_pushes_reasoning_delta_events(self):
        # Separate from on_stream (content) — the model's live "thinking" trace has its own SSE
        # event type so the frontend can render it in its own collapsible block.
        captured = {}

        def spy(*a, **kw):
            captured.clear()
            captured.update(kw)
            return MagicMock()

        with patch.object(server_mod, "AgentLoop", side_effect=spy):
            session_id = self.client.post("/api/sessions", json={}).json()["session_id"]

        self.assertIn("on_reasoning_stream", captured)
        self.assertIsNotNone(captured["on_reasoning_stream"])
        self.assertIsNotNone(captured["on_stream"])
        self.assertIsNot(captured["on_reasoning_stream"], captured["on_stream"])

        handle = server_mod._sessions[session_id]
        captured["on_reasoning_stream"]("let me think")
        self.assertEqual(handle.events.get_nowait(), {"type": "reasoning_delta", "text": "let me think"})

    def test_new_session_has_empty_messages_before_anything_sent(self):
        session_id = self.client.post("/api/sessions", json={}).json()["session_id"]
        resp = self.client.get(f"/api/sessions/{session_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["messages"], [])

    def test_unknown_session_404s(self):
        resp = self.client.get("/api/sessions/nonexistent-session")
        self.assertEqual(resp.status_code, 404)

    def test_created_session_appears_in_list(self):
        session_id = self.client.post("/api/sessions", json={}).json()["session_id"]
        resp = self.client.get("/api/sessions")
        self.assertTrue(any(s["session_id"] == session_id for s in resp.json()))

    def test_session_list_derives_a_title_from_the_first_operator_message(self):
        session_id = self.client.post("/api/sessions", json={}).json()["session_id"]
        handle = server_mod._sessions[session_id]
        handle.loop.session.append("user", "ANALYSIS phase. Review the observations and record hypotheses.")
        handle.loop.session.append("user", "scan http://127.0.0.1:3000 for auth issues")
        handle.loop.session.append("assistant", "ok")
        row = next(s for s in self.client.get("/api/sessions").json() if s["session_id"] == session_id)
        self.assertEqual(row["title"], "scan http://127.0.0.1:3000 for auth issues")  # driver prompt skipped
        self.assertEqual(row["message_count"], 3)

    def test_delete_session_removes_the_transcript(self):
        session_id = self.client.post("/api/sessions", json={}).json()["session_id"]
        server_mod._sessions[session_id].loop.session.append("user", "hi")
        self.assertEqual(self.client.delete(f"/api/sessions/{session_id}").json()["deleted"], True)
        self.assertEqual(self.client.get(f"/api/sessions/{session_id}").status_code, 404)
        self.assertFalse(any(s["session_id"] == session_id for s in self.client.get("/api/sessions").json()))

    def test_delete_refuses_a_running_session(self):
        session_id = self.client.post("/api/sessions", json={}).json()["session_id"]
        server_mod._sessions[session_id].running = True
        try:
            self.assertEqual(self.client.delete(f"/api/sessions/{session_id}").status_code, 409)
        finally:
            server_mod._sessions[session_id].running = False


class TestMessagingAndEvents(TestWebServerBase):
    def test_message_runs_task_and_delivers_events(self):
        session_id = self.client.post("/api/sessions", json={}).json()["session_id"]

        handle = server_mod._sessions[session_id]

        # patch.object(Class, "run_task", ...) replaces the class attribute with a plain Mock,
        # which isn't a descriptor — instance.run_task(x) calls the mock as mock(x), no implicit
        # `self`. side_effect closes over `handle.loop` instead of taking it as a parameter.
        def fake_run_task(user_input):
            handle.loop.session.append("user", user_input)
            handle.loop.session.append("assistant", "final answer")
            return TaskResult("ok", message="final answer")

        with patch.object(server_mod.AgentLoop, "run_task", side_effect=fake_run_task) as mock_run:
            resp = self.client.post(f"/api/sessions/{session_id}/messages", json={"content": "hello"})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["status"], "started")

            deadline = time.time() + 5
            while handle.running and time.time() < deadline:
                time.sleep(0.05)
            self.assertFalse(handle.running, "background task never finished")

        stored = self.client.get(f"/api/sessions/{session_id}").json()["messages"]
        roles = [m["role"] for m in stored]
        self.assertEqual(roles, ["user", "assistant"])

    def test_second_message_while_running_is_rejected(self):
        session_id = self.client.post("/api/sessions", json={}).json()["session_id"]
        handle = server_mod._sessions[session_id]
        handle.running = True
        try:
            resp = self.client.post(f"/api/sessions/{session_id}/messages", json={"content": "x"})
            self.assertEqual(resp.status_code, 409)
        finally:
            handle.running = False

    def test_task_error_is_reported_not_swallowed(self):
        session_id = self.client.post("/api/sessions", json={}).json()["session_id"]
        with patch.object(server_mod.AgentLoop, "run_task", side_effect=RuntimeError("boom")):
            self.client.post(f"/api/sessions/{session_id}/messages", json={"content": "hello"})
            deadline = time.time() + 5
            handle = server_mod._sessions[session_id]
            while handle.running and time.time() < deadline:
                time.sleep(0.05)
        events = []
        while True:
            try:
                events.append(handle.events.get_nowait())
            except Exception:
                break
        self.assertTrue(any(e["type"] == "task_error" and "boom" in e["error"] for e in events))


class TestSteer(TestWebServerBase):
    def test_steer_message_reaches_the_steer_channel(self):
        session_id = self.client.post("/api/sessions", json={}).json()["session_id"]
        resp = self.client.post(f"/api/sessions/{session_id}/steer", json={"message": "redirect"})
        self.assertEqual(resp.status_code, 200)

        from ..loop_control.steer import SteerChannel

        self.assertEqual(SteerChannel(session_id).take_pending(), "redirect")

    def test_steer_on_unknown_session_404s(self):
        resp = self.client.post("/api/sessions/nonexistent/steer", json={"message": "x"})
        self.assertEqual(resp.status_code, 404)


class TestApprovals(TestWebServerBase):
    def test_pending_approval_visible_and_resolvable(self):
        request_id = server_mod._approval_queue.submit(
            session_id="s1", tool="local_confirm", arguments={}, reason="run rm -rf /tmp/x?"
        )
        pending = self.client.get("/api/approvals").json()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["request_id"], request_id)

        resp = self.client.post(f"/api/approvals/{request_id}/resolve", json={"approved": True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "approved")

        pending_after = self.client.get("/api/approvals").json()
        self.assertEqual(pending_after, [])

    def test_resolving_unknown_request_is_a_clean_400_not_a_500(self):
        resp = self.client.post("/api/approvals/nonexistent/resolve", json={"approved": True})
        self.assertEqual(resp.status_code, 400)

    def test_web_confirm_fn_resolves_via_the_approval_queue(self):
        confirm_fn = server_mod._make_web_confirm_fn("s2")
        results = {}

        import threading

        def call_confirm():
            results["approved"] = confirm_fn("dangerous thing?")

        t = threading.Thread(target=call_confirm)
        t.start()

        deadline = time.time() + 5
        while not server_mod._approval_queue.list_pending(session_id="s2") and time.time() < deadline:
            time.sleep(0.05)
        pending = server_mod._approval_queue.list_pending(session_id="s2")
        self.assertEqual(len(pending), 1)
        server_mod._approval_queue.resolve(pending[0]["request_id"], approved=True, resolved_by="test")
        t.join(timeout=5)
        self.assertTrue(results.get("approved"))


class TestIndexPage(TestWebServerBase):
    def test_root_serves_html(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])
        self.assertIn("localAI agent", resp.text)


class TestLiveEndToEnd(TestWebServerBase):
    """No mocking: a real session, a real message, a real llama-server round trip through the
    real AgentLoop.run_task(). Skips gracefully if the model server isn't reachable, matching
    this project's established real-over-mocked preference wherever the dependency allows it."""

    def test_real_message_produces_a_real_assistant_reply(self):
        import requests

        try:
            requests.get(f"{config.LLAMA_SERVER_URL}/health", timeout=3)
        except Exception as e:
            self.skipTest(f"llama-server not reachable: {e}")

        session_id = self.client.post("/api/sessions", json={}).json()["session_id"]
        resp = self.client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": "Reply with exactly the word: pong"},
        )
        self.assertEqual(resp.status_code, 200)

        handle = server_mod._sessions[session_id]
        deadline = time.time() + 60
        while handle.running and time.time() < deadline:
            time.sleep(0.2)
        self.assertFalse(handle.running, "real task never finished within 60s")

        messages = self.client.get(f"/api/sessions/{session_id}").json()["messages"]
        roles = [m["role"] for m in messages]
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)
        assistant_msgs = [m["content"] for m in messages if m["role"] == "assistant"]
        self.assertTrue(any(m.strip() for m in assistant_msgs), "assistant produced no content")


class TestEngagementEndpoints(TestWebServerBase):
    def test_create_and_list_engagement(self):
        resp = self.client.post("/api/engagements", json={
            "engagement_id": "web-test-eng",
            "allow_targets": ["127.0.0.1"],
            "allowed_action_classes": ["passive_recon"],
            "authorized_by": "test operator",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["engagement_id"], "web-test-eng")

        listing = self.client.get("/api/engagements").json()
        self.assertEqual([e["engagement_id"] for e in listing], ["web-test-eng"])
        self.assertEqual(listing[0]["allowed_action_classes"], ["passive_recon"])

    def test_creates_an_asset_per_single_host_target(self):
        """Found live: this endpoint originally only wrote roe.json/scope.txt/deny.txt — zero
        EngagementStore assets, so the pipeline orchestrator could never leave INTAKE and
        Autonomous mode would report "blocked" immediately on every UI-created engagement."""
        from ..engagement.store import EngagementStore

        self.client.post("/api/engagements", json={
            "engagement_id": "asset-test-eng",
            "allow_targets": ["127.0.0.1", "10.0.0.0/24"],
            "allowed_action_classes": ["passive_recon"],
            "authorized_by": "test operator",
        })
        store = EngagementStore(self.tmp / "engagements" / "asset-test-eng")
        assets = store.list_assets()
        self.assertEqual([a["identifier"] for a in assets], ["127.0.0.1"])  # CIDR entry skipped

    def test_invalid_engagement_rejected_with_400(self):
        resp = self.client.post("/api/engagements", json={
            "engagement_id": "bad-eng",
            "allow_targets": [],  # empty — intake.py requires at least one
            "allowed_action_classes": ["passive_recon"],
            "authorized_by": "test operator",
        })
        self.assertEqual(resp.status_code, 400)

    def test_duplicate_engagement_id_rejected(self):
        body = {
            "engagement_id": "dup-eng", "allow_targets": ["127.0.0.1"],
            "allowed_action_classes": ["passive_recon"], "authorized_by": "test operator",
        }
        self.assertEqual(self.client.post("/api/engagements", json=body).status_code, 200)
        self.assertEqual(self.client.post("/api/engagements", json=body).status_code, 400)


class TestAutonomousEndpoints(TestWebServerBase):
    def _make_session(self) -> str:
        return self.client.post("/api/sessions", json={}).json()["session_id"]

    def test_unknown_mode_rejected(self):
        session_id = self._make_session()
        resp = self.client.post(
            f"/api/sessions/{session_id}/autonomous/start",
            json={"engagement_id": "e", "mode": "not-a-real-mode"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_status_before_any_run_is_inactive(self):
        session_id = self._make_session()
        resp = self.client.get(f"/api/sessions/{session_id}/autonomous/status")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["active"])

    def test_start_reports_active_then_result_once_finished(self):
        session_id = self._make_session()
        mock_driver = MagicMock()
        mock_driver.mode = "autonomous"
        mock_driver.engagement_id = "e1"
        mock_driver.stop_event = threading.Event()
        # autonomous_status() reads driver.store for the live plan/todo view — a bare MagicMock
        # for get_phase()/list_tasks() would hand back non-JSON-serializable MagicMock objects.
        mock_driver.store.get_phase.return_value = {"current_phase": "RECON"}
        mock_driver.store.list_tasks.return_value = [
            {"task_id": "t1", "task_type": "port_discovery", "status": "running", "params_json": None},
        ]
        finished = threading.Event()

        def fake_run():
            finished.wait(timeout=5)
            return AutonomousRunResult("closed_out", "done", ["RECON"], "CLOSEOUT")

        mock_driver.run.side_effect = fake_run

        with patch.object(server_mod, "AutonomousDriver", return_value=mock_driver):
            resp = self.client.post(
                f"/api/sessions/{session_id}/autonomous/start",
                json={"engagement_id": "e1", "mode": "autonomous"},
            )
            self.assertEqual(resp.status_code, 200)

            status = self.client.get(f"/api/sessions/{session_id}/autonomous/status").json()
            self.assertTrue(status["active"])
            self.assertEqual(status["mode"], "autonomous")
            self.assertEqual(status["current_phase"], "RECON")
            self.assertEqual(status["tasks"], [
                {"task_id": "t1", "task_type": "port_discovery", "status": "running", "entity_id": None},
            ])

            finished.set()
            deadline = time.time() + 5
            while time.time() < deadline:
                status = self.client.get(f"/api/sessions/{session_id}/autonomous/status").json()
                if not status["active"]:
                    break
                time.sleep(0.1)

        self.assertFalse(status["active"])
        self.assertEqual(status["result"]["status"], "closed_out")
        self.assertEqual(status["result"]["final_phase"], "CLOSEOUT")

    def test_status_surfaces_entity_id_from_task_params(self):
        session_id = self._make_session()
        mock_driver = MagicMock()
        mock_driver.mode = "autonomous"
        mock_driver.engagement_id = "e1"
        mock_driver.stop_event = threading.Event()
        mock_driver.store.get_phase.return_value = {"current_phase": "RECON"}
        mock_driver.store.list_tasks.return_value = [
            {"task_id": "t1", "task_type": "http_recon", "status": "pending",
             "params_json": json.dumps({"entity_type": "asset", "entity_id": "a-123"})},
            {"task_id": "t2", "task_type": "generate_report", "status": "done", "params_json": None},
        ]
        never_finishes = threading.Event()
        mock_driver.run.side_effect = lambda: (never_finishes.wait(timeout=5), AutonomousRunResult("blocked", "x"))[1]

        with patch.object(server_mod, "AutonomousDriver", return_value=mock_driver):
            self.client.post(f"/api/sessions/{session_id}/autonomous/start",
                             json={"engagement_id": "e1", "mode": "autonomous"})
            status = self.client.get(f"/api/sessions/{session_id}/autonomous/status").json()
            never_finishes.set()

        by_id = {t["task_id"]: t for t in status["tasks"]}
        self.assertEqual(by_id["t1"]["entity_id"], "a-123")
        self.assertIsNone(by_id["t2"]["entity_id"])

    def test_stop_sets_the_driver_stop_event(self):
        session_id = self._make_session()
        mock_driver = MagicMock()
        mock_driver.mode = "autonomous"
        mock_driver.engagement_id = "e1"
        mock_driver.stop_event = threading.Event()
        mock_driver.run.side_effect = lambda: AutonomousRunResult("stopped_by_operator", "x")

        with patch.object(server_mod, "AutonomousDriver", return_value=mock_driver):
            self.client.post(
                f"/api/sessions/{session_id}/autonomous/start",
                json={"engagement_id": "e1", "mode": "autonomous"},
            )
            resp = self.client.post(f"/api/sessions/{session_id}/autonomous/stop")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(mock_driver.stop_event.is_set())

    def test_stop_without_a_started_run_404s(self):
        session_id = self._make_session()
        resp = self.client.post(f"/api/sessions/{session_id}/autonomous/stop")
        self.assertEqual(resp.status_code, 404)

    def test_second_start_while_active_is_rejected(self):
        session_id = self._make_session()
        mock_driver = MagicMock()
        mock_driver.mode = "autonomous"
        mock_driver.engagement_id = "e1"
        mock_driver.stop_event = threading.Event()
        never_finishes = threading.Event()

        def fake_run():
            never_finishes.wait(timeout=5)
            return AutonomousRunResult("blocked", "x")

        mock_driver.run.side_effect = fake_run

        with patch.object(server_mod, "AutonomousDriver", return_value=mock_driver):
            self.client.post(
                f"/api/sessions/{session_id}/autonomous/start",
                json={"engagement_id": "e1", "mode": "autonomous"},
            )
            resp = self.client.post(
                f"/api/sessions/{session_id}/autonomous/start",
                json={"engagement_id": "e1", "mode": "autonomous"},
            )
            never_finishes.set()
        self.assertEqual(resp.status_code, 409)


class TestHypothesisGraphEndpoints(TestWebServerBase):
    def _seed_graph(self, engagement_id: str):
        from ..hypothesis_graph.service import HypothesisGraphService

        eng_dir = self.tmp / "engagements" / engagement_id
        eng_dir.mkdir(parents=True, exist_ok=True)
        svc = HypothesisGraphService(eng_dir)
        root = svc.add_hypothesis(
            title="Recon surface mapped", claim="HTTP surface on :3000 exposes standard headers.",
            phase_created="RECON", rationale="http_recon observed the response headers directly.",
            origin_type="tool_observation", impact=2, confidence_band="high",
            confidence_reason="observed directly", origin_ref="msg-root",
        )
        child = svc.add_hypothesis(
            title="IDOR on /api/orders/{id}", claim="/api/orders/{id} may not enforce object-level auth.",
            phase_created="ANALYSIS", rationale="sequential ids seen in the client bundle",
            origin_type="ai_inference", impact=5, confidence_band="medium",
            confidence_reason="clue with a real alternative", primary_parent_id=root["hypothesis_id"],
            surface="/api/orders/{id}", planned_tests=2,
        )
        xid = svc.start_attempt(child["ordinal"], method_summary="GET /api/orders/2 as user A",
                                chat_start_message_id="msg-attempt")["experiment_id"]
        svc.complete_attempt(
            xid, status="completed", observed_result="200 — returned another account's order",
            observation_summary="cross-user read succeeded", polarity="supports", strength="strong",
            input_tokens=100, output_tokens=50, chat_result_message_id="msg-result",
        )
        svc.set_active_path([root["ordinal"], child["ordinal"]], "reproduced cross-user read")
        return root, child

    def test_overview_empty_for_engagement_without_a_graph(self):
        resp = self.client.get("/api/engagements/no-graph-eng/hypothesis-graph")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["exists"])
        self.assertEqual(body["nodes"], [])
        # the guard must not have created the db as a side effect
        self.assertFalse((self.tmp / "engagements" / "no-graph-eng" / "hypothesis_graph.db").exists())

    def test_overview_returns_nodes_edges_and_active_path(self):
        self._seed_graph("graph-eng")
        body = self.client.get("/api/engagements/graph-eng/hypothesis-graph").json()
        self.assertTrue(body["exists"])
        self.assertEqual({n["ordinal"] for n in body["nodes"]}, {1, 2})
        child = next(n for n in body["nodes"] if n["ordinal"] == 2)
        self.assertEqual(child["attempt_count"], 1)
        self.assertEqual(child["surface"], "/api/orders/{id}")
        self.assertIn(child["priority_tier"], {"now", "next", "later", "parked"})
        self.assertEqual(body["graph_state"]["active_path_reason"], "reproduced cross-user read")
        self.assertEqual(len(body["graph_state"]["active_path"]), 2)

    def test_node_detail_has_attempts_observations_and_chat_anchors(self):
        self._seed_graph("graph-eng2")
        body = self.client.get("/api/engagements/graph-eng2/hypothesis-graph/nodes/2").json()
        self.assertEqual(body["ordinal"], 2)
        self.assertEqual(body["claim"], "/api/orders/{id} may not enforce object-level auth.")
        self.assertEqual(len(body["experiments"]), 1)
        exp = body["experiments"][0]
        self.assertEqual(exp["chat_start_message_id"], "msg-attempt")
        self.assertEqual(exp["chat_result_message_id"], "msg-result")
        self.assertEqual(exp["observations"][0]["polarity"], "supports")
        self.assertIn("components", body["priority"])

    def test_node_detail_surfaces_the_engine_confidence_estimate(self):
        self._seed_graph("graph-eng-ai")
        body = self.client.get("/api/engagements/graph-eng-ai/hypothesis-graph/nodes/2").json()
        self.assertIn("suggested_confidence", body)
        # the seeded attempt recorded one strong supporting observation
        self.assertEqual(body["suggested_confidence"]["band"], "medium")
        self.assertTrue(body["suggested_confidence"]["reason"])

    def test_overview_edges_include_a_non_primary_link(self):
        from ..hypothesis_graph.service import HypothesisGraphService

        root, child = self._seed_graph("graph-eng-edges")
        svc = HypothesisGraphService(self.tmp / "engagements" / "graph-eng-edges")
        svc.link(child["ordinal"], root["ordinal"], "supports", reason="the finding backs the recon summary")
        edges = self.client.get("/api/engagements/graph-eng-edges/hypothesis-graph").json()["edges"]
        kinds = [e["edge_type"] for e in edges]
        self.assertIn("supports", kinds)      # the non-primary link
        self.assertIn("spawned_by", kinds)    # the auto-created primary lineage

    def test_overview_includes_the_ranked_open_queue(self):
        self._seed_graph("graph-eng-queue")
        body = self.client.get("/api/engagements/graph-eng-queue/hypothesis-graph").json()
        self.assertIn("ranked_open", body)
        self.assertTrue(body["ranked_open"])
        for r in body["ranked_open"]:
            self.assertIn(r["priority_tier"], {"now", "next", "later", "parked"})
            self.assertIn(r["lifecycle_status"], {"open", "queued", "running"})  # only actionable

    def test_overview_flags_a_hypothesis_depending_on_a_refuted_one(self):
        from ..hypothesis_graph.service import HypothesisGraphService

        eng_dir = self.tmp / "engagements" / "graph-eng-stale"
        eng_dir.mkdir(parents=True, exist_ok=True)
        svc = HypothesisGraphService(eng_dir)
        root = svc.add_hypothesis(title="root", claim="c", phase_created="RECON", rationale="r",
                                  origin_type="ai_inference", impact=3, confidence_band="low",
                                  confidence_reason="x")
        svc.add_hypothesis(title="child", claim="c2", phase_created="ANALYSIS", rationale="r2",
                           origin_type="ai_inference", impact=3, confidence_band="low",
                           confidence_reason="x", primary_parent_id=root["hypothesis_id"])
        svc.set_verdict(root["ordinal"], "refuted")
        nodes = self.client.get("/api/engagements/graph-eng-stale/hypothesis-graph").json()["nodes"]
        child_node = next(n for n in nodes if n["ordinal"] == 2)
        self.assertIn("refuted", child_node["stale_reason"] or "")

    def test_node_detail_404_for_unknown_ordinal(self):
        self._seed_graph("graph-eng3")
        resp = self.client.get("/api/engagements/graph-eng3/hypothesis-graph/nodes/99")
        self.assertEqual(resp.status_code, 404)

    def test_node_detail_404_when_engagement_has_no_graph(self):
        resp = self.client.get("/api/engagements/nope/hypothesis-graph/nodes/1")
        self.assertEqual(resp.status_code, 404)

    def test_operator_can_park_and_reopen_a_hypothesis(self):
        self._seed_graph("op-eng")
        r = self.client.post("/api/engagements/op-eng/hypothesis-graph/nodes/2/operator-action",
                             json={"action": "park", "reason": "lower priority than the CSP finding"})
        self.assertEqual(r.status_code, 200)
        node = next(n for n in self.client.get("/api/engagements/op-eng/hypothesis-graph").json()["nodes"]
                    if n["ordinal"] == 2)
        self.assertEqual(node["lifecycle_status"], "parked")
        detail = self.client.get("/api/engagements/op-eng/hypothesis-graph/nodes/2").json()
        self.assertTrue(detail["park_reason"].startswith("[operator]"))

        r = self.client.post("/api/engagements/op-eng/hypothesis-graph/nodes/2/operator-action",
                             json={"action": "reopen"})
        self.assertEqual(r.status_code, 200)
        node = next(n for n in self.client.get("/api/engagements/op-eng/hypothesis-graph").json()["nodes"]
                    if n["ordinal"] == 2)
        self.assertEqual(node["lifecycle_status"], "open")

    def test_operator_note_shows_in_node_detail_without_touching_history(self):
        self._seed_graph("op-note-eng")
        r = self.client.post("/api/engagements/op-note-eng/hypothesis-graph/nodes/2/operator-action",
                             json={"action": "note", "text": "confirm scope with the customer first"})
        self.assertEqual(r.status_code, 200)
        detail = self.client.get("/api/engagements/op-note-eng/hypothesis-graph/nodes/2").json()
        self.assertEqual([n["text"] for n in detail["operator_notes"]],
                         ["confirm scope with the customer first"])
        self.assertEqual(detail["claim"], "/api/orders/{id} may not enforce object-level auth.")
        # it is also mirrored into the working notebook (refs: H-2) so it reaches the model's
        # every-turn digest — the graph digest itself only shows hypothesis lines, not notes.
        self.assertTrue(r.json().get("mirrored_to_notebook"))
        nb = self.client.get("/api/engagements/op-note-eng/notebook").json()
        mirrored = next(n for n in nb["notes"] if "operator" in n["tags"])
        self.assertEqual(mirrored["note"], "confirm scope with the customer first")
        self.assertEqual(mirrored["refs"], ["H-2"])

    def test_park_without_a_reason_is_400(self):
        self._seed_graph("op-bad-eng")
        r = self.client.post("/api/engagements/op-bad-eng/hypothesis-graph/nodes/2/operator-action",
                             json={"action": "park", "reason": "  "})
        self.assertEqual(r.status_code, 400)

    def test_operator_action_on_unknown_node_404(self):
        self._seed_graph("op-404-eng")
        r = self.client.post("/api/engagements/op-404-eng/hypothesis-graph/nodes/99/operator-action",
                             json={"action": "reopen"})
        self.assertEqual(r.status_code, 404)

    def test_operator_action_on_engagement_without_a_graph_404(self):
        r = self.client.post("/api/engagements/never/hypothesis-graph/nodes/1/operator-action",
                             json={"action": "reopen"})
        self.assertEqual(r.status_code, 404)

    def test_unknown_operator_action_is_400(self):
        self._seed_graph("op-unk-eng")
        r = self.client.post("/api/engagements/op-unk-eng/hypothesis-graph/nodes/2/operator-action",
                             json={"action": "delete"})
        self.assertEqual(r.status_code, 400)

    def test_get_session_messages_include_message_id(self):
        session_id = self.client.post("/api/sessions", json={}).json()["session_id"]
        handle = server_mod._sessions[session_id]
        handle.loop.session.append("user", "hi")
        handle.loop.session.append("assistant", "hello")
        messages = self.client.get(f"/api/sessions/{session_id}").json()["messages"]
        self.assertEqual([m["role"] for m in messages], ["user", "assistant"])
        self.assertTrue(all(m.get("message_id") for m in messages))


class TestNotebookEndpoints(TestWebServerBase):
    def _seed(self, engagement_id: str):
        from ..notebook.service import NotebookService

        eng_dir = self.tmp / "engagements" / engagement_id
        eng_dir.mkdir(parents=True, exist_ok=True)
        svc = NotebookService(eng_dir)
        svc.add_note(category="recon", note="server is Express, no CSP/HSTS")
        svc.add_note(category="dead-end", note="tried JWT alg=none, kid traversal, weak HS256 — none present")
        svc.add_note(category="todo", note="revisit /api/proxy once auth is captured")
        return svc

    def test_overview_empty_without_a_notebook(self):
        body = self.client.get("/api/engagements/no-nb/notebook").json()
        self.assertFalse(body["exists"])
        self.assertEqual(body["notes"], [])
        self.assertFalse((self.tmp / "engagements" / "no-nb" / "notebook.db").exists())

    def test_overview_returns_notes_and_counts(self):
        self._seed("nb-eng")
        body = self.client.get("/api/engagements/nb-eng/notebook").json()
        self.assertTrue(body["exists"])
        self.assertEqual(body["counts"], {"recon": 1, "dead-end": 1, "todo": 1})
        cats = {n["category"] for n in body["notes"]}
        self.assertEqual(cats, {"recon", "dead-end", "todo"})

    def test_resolve_and_reopen_a_todo(self):
        self._seed("nb-eng2")
        r = self.client.post("/api/engagements/nb-eng2/notebook/notes/3/resolve",
                             json={"action": "resolve", "reason": "tested, not vulnerable"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "resolved")
        r = self.client.post("/api/engagements/nb-eng2/notebook/notes/3/resolve",
                             json={"action": "reopen", "reason": "new lead"})
        self.assertEqual(r.json()["status"], "open")

    def test_resolve_without_reason_is_400(self):
        self._seed("nb-eng3")
        r = self.client.post("/api/engagements/nb-eng3/notebook/notes/3/resolve",
                             json={"action": "resolve", "reason": "  "})
        self.assertEqual(r.status_code, 400)

    def test_resolve_unknown_note_404(self):
        self._seed("nb-eng4")
        r = self.client.post("/api/engagements/nb-eng4/notebook/notes/99/resolve",
                             json={"action": "resolve", "reason": "x"})
        self.assertEqual(r.status_code, 404)

    def test_resolve_on_engagement_without_notebook_404(self):
        r = self.client.post("/api/engagements/nope/notebook/notes/1/resolve",
                             json={"action": "resolve", "reason": "x"})
        self.assertEqual(r.status_code, 404)

    def test_technique_kb_endpoint_and_forget(self):
        from ..notebook.service import NotebookService

        eng_dir = self.tmp / "engagements" / "kb-eng"
        eng_dir.mkdir(parents=True, exist_ok=True)
        NotebookService(eng_dir).add_note(
            category="technique", note="prototype-pollution merge gadget. Works where user objects are merged.",
            tags=["prototype-pollution"], surface="/profile")
        body = self.client.get("/api/technique-kb").json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["techniques"][0]["source_engagement"], "kb-eng")
        ordinal = body["techniques"][0]["ordinal"]
        self.assertEqual(self.client.delete(f"/api/technique-kb/{ordinal}").json()["deleted"], True)
        self.assertEqual(self.client.get("/api/technique-kb").json()["count"], 0)


class TestFindingsEndpoints(TestWebServerBase):
    def _seed(self, engagement_id: str):
        from ..findings.model import Finding, FindingsStore

        store = FindingsStore(engagement_id, findings_dir=self.tmp / "findings")
        f = Finding(
            title="Reflected XSS", severity="high", target="http://127.0.0.1:3000/search",
            description="d", remediation="r", tool="http_recon",
            session_id="s1", engagement_id=engagement_id,
        )
        return store, store.add(f)

    def test_list_empty_for_engagement_without_findings(self):
        body = self.client.get("/api/engagements/no-findings/findings").json()
        self.assertEqual(body, {"findings": [], "count": 0, "reviewed_count": 0})

    def test_list_returns_the_finding_unreviewed(self):
        self._seed("f-eng")
        body = self.client.get("/api/engagements/f-eng/findings").json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["reviewed_count"], 0)
        self.assertIsNone(body["findings"][0]["reviewed_by"])

    def test_review_marks_it_and_shows_up_in_the_list(self):
        _, f = self._seed("f-eng2")
        r = self.client.post(
            f"/api/engagements/f-eng2/findings/{f.finding_id}/review",
            json={"reviewed_by": "alice"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["reviewed_by"], "alice")
        self.assertIsNotNone(r.json()["reviewed_at"])

        body = self.client.get("/api/engagements/f-eng2/findings").json()
        self.assertEqual(body["reviewed_count"], 1)

    def test_review_empty_identity_is_400(self):
        _, f = self._seed("f-eng3")
        r = self.client.post(
            f"/api/engagements/f-eng3/findings/{f.finding_id}/review",
            json={"reviewed_by": "   "},
        )
        self.assertEqual(r.status_code, 400)

    def test_review_unknown_finding_404s(self):
        self._seed("f-eng4")
        r = self.client.post(
            "/api/engagements/f-eng4/findings/not-a-real-id/review",
            json={"reviewed_by": "alice"},
        )
        self.assertEqual(r.status_code, 404)


class TestConsultEndpoints(TestWebServerBase):
    def test_list_and_resolve(self):
        rid = server_mod._consult_queue.submit(session_id="s1", question="continue?")
        pending = self.client.get("/api/consults").json()
        self.assertEqual([p["request_id"] for p in pending], [rid])

        resp = self.client.post(f"/api/consults/{rid}/resolve", json={"answer": "continue"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get("/api/consults").json(), [])

    def test_resolve_unknown_request_400s(self):
        resp = self.client.post("/api/consults/nonexistent/resolve", json={"answer": "stop"})
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
