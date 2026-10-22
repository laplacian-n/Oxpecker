"""Real-browser tests for agent/web/static/index.html — the UI the operator designed and handed
over (adopted wholesale in PR #27). Runs a real headless Chromium against a real running instance
of agent.web.dev_server (the orchestrator the UI actually targets — it serves every /api/* and
/browser/* route the UI calls; agent.web.server is the older, smaller scaffold and does not have
pipeline/browser/default-system-prompt). The server is spawned as a subprocess on a free port with
its own throwaway state/engagements tree.

No model is needed. The previous suite gated on a reachable llama-server and drove whole turns
through the real model; this project runs inference through OpenRouter now, not a local llama, so
that gate only ever skipped. Chat rendering is instead exercised the way the old
TestVerdictParsingAndRendering already did — by feeding the UI's real SSE handler
(handleServerEvent) and render path synthetically in the browser, which is exactly the code a live
turn drives, minus the network round-trip. Everything else (sessions, engagements, the §4.2
snapshot behind the Flow view, the approvals drawer, the graph/notebook/findings views) is driven
against the real server with no model in the loop.

Skips gracefully only if playwright itself is missing.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError as _e:  # pragma: no cover - environment-dependent
    # A missing test-only dependency is a SKIP, not an ERROR. As an ImportError this module
    # failed loudly in any environment without playwright -- including CI -- and a loader error
    # is indistinguishable at a glance from a real breakage in the code under test.
    raise unittest.SkipTest(f"playwright not installed, so the browser suite cannot run: {_e}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestFrontendBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A real uvicorn process, not the in-process TestClient, because this file's whole point is
        # to run the shipped static/index.html in a real browser against a real server. Its own
        # throwaway state/engagements tree (via the env-var overrides agent/config.py exposes) keeps
        # the run from writing into agent/state or the repo's engagements/. No web-UI key file is
        # written into it, so require_api_key leaves every route open (key is None -> open).
        cls.tmp_dir = Path(tempfile.mkdtemp(prefix="frontend-test-"))
        cls.state_dir = cls.tmp_dir / "state"
        cls.engagements_root = cls.tmp_dir / "engagements"
        cls.state_dir.mkdir(parents=True)
        cls.engagements_root.mkdir(parents=True)
        env = {
            **os.environ,
            "AGENT_STATE_DIR": str(cls.state_dir),
            "AGENT_ENGAGEMENTS_ROOT": str(cls.engagements_root),
        }
        cls.port = _free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "agent.web.dev_server:app",
             "--host", "127.0.0.1", "--port", str(cls.port), "--log-level", "warning"],
            env=env,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                urllib.request.urlopen(cls.base_url, timeout=1)
                break
            except Exception:
                if cls.proc.poll() is not None:
                    raise RuntimeError("web server exited before it came up")
                time.sleep(0.3)
        else:
            cls.proc.terminate()
            raise RuntimeError("web server did not start in time")

        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True, args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        import shutil
        shutil.rmtree(cls.tmp_dir, ignore_errors=True)

    def setUp(self):
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []
        self.page = self.browser.new_page()
        self.page.on("console", lambda m: self.console_errors.append(m.text) if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.page_errors.append(str(e)))
        self.addCleanup(self.page.close)

    # ── helpers ──────────────────────────────────────────────────────────────────────────────
    def _open(self):
        """Load the app and wait until its client JS is live."""
        self.page.goto(self.base_url, timeout=15000)
        # The whole UI is one inline script; these globals existing means it parsed and ran.
        self.page.wait_for_function(
            "typeof render==='function' && typeof handleServerEvent==='function' && typeof S==='object'",
            timeout=10000,
        )

    def _enter_chat(self, thread=None):
        """Drop straight into a chat on a synthetic session, bypassing session creation and SSE —
        handleServerEvent keys off S.activeSession, so this is all its render path needs."""
        self._open()
        self.page.evaluate(
            """(thread) => {
                S.activeSession='t-synthetic';
                S.sessions=[{id:'t-synthetic',title:'Synthetic session',sub:'now',live:true,ts:Date.now()/1000}];
                S.threads['t-synthetic']=thread||[];
                S.view='chat';
                render();
            }""",
            thread,
        )
        self.page.wait_for_selector("#chatView", timeout=5000)

    def _emit(self, evt):
        """Feed one event through the real SSE handler, exactly as connectSSE would."""
        self.page.evaluate("(evt) => handleServerEvent(evt)", evt)

    def _set(self, js_body):
        self.page.evaluate("() => { %s }" % js_body)

    def _transcript_contains(self, text, timeout=6000):
        self.page.wait_for_function(
            "(t) => document.getElementById('transcript') && document.getElementById('transcript').innerText.includes(t)",
            arg=text, timeout=timeout,
        )

    def assertNoJsErrors(self):
        # Resource-load failures (the logo asset, Google Fonts) are not JS errors and are expected
        # in a bare test environment with no bundled logo and no internet; the real signals are
        # uncaught exceptions (pageerror) and console.error calls from the UI's own code.
        code_errors = [m for m in self.console_errors if "Failed to load resource" not in m]
        self.assertEqual(code_errors, [], "browser console reported errors")
        self.assertEqual(self.page_errors, [], "an uncaught JS exception occurred")


class TestShell(TestFrontendBase):
    def test_home_loads_clean(self):
        self._open()
        self.page.wait_for_selector("#homeInput", timeout=5000)
        body = self.page.text_content("body")
        self.assertIn("Ready when you are.", body)
        self.assertIn("Oxpecker", body)
        self.assertTrue(self.page.is_visible("#sendBtn"))
        self.assertNoJsErrors()

    def test_sidebar_has_new_session_and_search(self):
        self._open()
        self.page.wait_for_selector("#sessionSearch", timeout=5000)
        self.assertTrue(self.page.is_visible("[data-action='goHome']"))
        self.assertTrue(self.page.is_visible("[data-action='toggleApprovals']"))
        self.assertNoJsErrors()


class TestConfigMenus(TestFrontendBase):
    def test_mode_menu_switches_to_autonomous_and_reveals_depth(self):
        self._open()
        self.page.wait_for_selector("#homeInput")
        self.page.click("[data-action='openMenu'][data-key='mode']")
        self.page.wait_for_selector("[data-action='pickMenu'][data-key='mode']", timeout=3000)
        items = self.page.query_selector_all("[data-action='pickMenu'][data-key='mode']")
        self.assertEqual(len(items), 2)  # Assistant, Autonomous (guided is no longer a menu choice)
        self.page.click("[data-action='pickMenu'][data-value='autonomous']")
        self.assertEqual(self.page.evaluate("() => S.mode"), "autonomous")
        # Autonomous reveals the depth pill (tier) that Assistant mode hides.
        self.page.wait_for_selector("[data-action='openMenu'][data-key='tier']", timeout=3000)
        self.assertNoJsErrors()

    def test_isolation_menu_switches_to_this_computer(self):
        self._open()
        self.page.wait_for_selector("#homeInput")
        self.page.click("[data-action='openMenu'][data-key='iso']")
        self.page.wait_for_selector("[data-action='pickMenu'][data-key='iso']", timeout=3000)
        self.page.click("[data-action='pickMenu'][data-value='this_computer']")
        self.assertEqual(self.page.evaluate("() => S.isolation"), "this_computer")
        self.assertNoJsErrors()


class TestSyntheticTurn(TestFrontendBase):
    def test_user_message_and_assistant_answer_render(self):
        self._enter_chat()
        self._emit({"type": "message", "record": {"role": "user", "content": "Reply with exactly: pong"}})
        self._set("S.running=true; S.elapsed=0; render();")
        self._emit({"type": "assistant_delta", "text": "pong"})
        self._emit({"type": "message", "record": {"role": "assistant", "content": "pong"}})
        self._emit({"type": "task_done"})
        self._transcript_contains("Reply with exactly: pong")
        self._transcript_contains("pong")
        self.assertFalse(self.page.evaluate("() => S.running"))
        self.assertNoJsErrors()

    def test_tool_chain_collapses_to_worked_for_duration(self):
        # A turn with a tool call renders a collapsible activity chain; once the run stops it
        # collapses to "Worked for Ns", stamped from the elapsed clock (stampDuration).
        self._enter_chat()
        self._emit({"type": "message", "record": {"role": "user", "content": "scan the host"}})
        self._set("S.running=true; S.elapsed=7; render();")
        self._emit({"type": "message", "record": {
            "role": "assistant", "content": "(calling tools…)",
            "reasoning_content": "I will run a light service scan first.",
            "tool_calls": [{"function": {"name": "run_command",
                                         "arguments": "{\"command\": \"nmap -sV 10.0.0.1\"}"}}],
        }})
        self._emit({"type": "message", "record": {
            "role": "tool", "tool_name": "run_command",
            "content": "{\"output\": \"22/tcp open ssh OpenSSH 8.9\", \"exit_code\": 0}"}})
        self._emit({"type": "message", "record": {"role": "assistant", "content": "Found an open SSH port."}})
        self._emit({"type": "task_done"})
        self._transcript_contains("Found an open SSH port.")
        # Settled, the chain collapses to a "Worked for Ns" header, stamped from the elapsed clock.
        tx = self.page.text_content("#transcript")
        self.assertIn("Worked", tx)
        self.assertIn("7", tx)                 # the stamped duration
        # Expanding it reveals the steps: the tool's label, its command verbatim, and the output.
        self.page.click("[data-action='toggleChain']")
        self._transcript_contains("nmap -sV 10.0.0.1")
        self.assertIn("Run", self.page.text_content("#transcript"))  # run_command's label
        # Expanding the step itself reveals the captured output block.
        self.page.locator("[data-action='toggleStep']", has_text="Run").click()
        self._transcript_contains("22/tcp open ssh")
        self.assertNoJsErrors()


class TestStreaming(TestFrontendBase):
    def test_reasoning_then_answer_stream_into_live_elements(self):
        self._enter_chat()
        self._emit({"type": "message", "record": {"role": "user", "content": "think then answer"}})
        self._set("S.running=true; render();")
        # First reasoning delta creates #streaming-reasoning; later deltas patch it in place.
        self._emit({"type": "reasoning_delta", "text": "Let me think about this. "})
        self.page.wait_for_selector("#streaming-reasoning", timeout=4000)
        self._emit({"type": "reasoning_delta", "text": "Almost there."})
        self.assertIn("Almost there.", self.page.text_content("#streaming-reasoning"))
        self.assertIn("Thinking", self.page.text_content("#transcript"))
        # Then the answer streams into #streaming-content.
        self._emit({"type": "assistant_delta", "text": "The answer is 42."})
        self.page.wait_for_selector("#streaming-content", timeout=4000)
        self.assertIn("The answer is 42.", self.page.text_content("#streaming-content"))
        self.assertNoJsErrors()


class TestToolPanel(TestFrontendBase):
    def test_open_tool_panel_shows_input_and_output(self):
        thread = [
            {"role": "user", "content": "run it"},
            {"role": "assistant", "content": "(calling tools…)",
             "tool_calls": [{"function": {"name": "run_command",
                                          "arguments": "{\"command\": \"whoami\"}"}}]},
            {"role": "tool", "tool_name": "run_command",
             "content": "{\"output\": \"root\", \"exit_code\": 0}"},
            {"role": "assistant", "content": "You are root."},
        ]
        self._enter_chat(thread)
        # The settled chain renders collapsed; expand it so the tool step (and its panel opener) show.
        self.page.click("[data-action='toggleChain']")
        self.page.wait_for_selector("[data-action='openToolPanel']", timeout=4000)
        self.page.click("[data-action='openToolPanel']")
        # renderPanel opens with an "Output" <pre>; the captured output lands there.
        self.page.wait_for_function(
            "() => document.querySelector('[data-action=\"closePanel\"]') !== null", timeout=4000)
        panel = self.page.text_content("main")
        self.assertIn("Tool output", panel)
        self.assertIn("root", panel)
        self.assertNoJsErrors()


class TestActivityRows(TestFrontendBase):
    def test_autonomous_events_render_as_activity_rows(self):
        self._enter_chat()
        self._set("S.running=true; render();")
        self._emit({"type": "autonomous_event", "event": {"type": "phase_entered", "phase": "RECON"}})
        self._emit({"type": "autonomous_event", "event": {"type": "worker_spawned",
                                                          "detail": "w1 → service scan on 10.0.0.1"}})
        self._emit({"type": "autonomous_event", "event": {"type": "finding_recorded",
                                                          "detail": "F-001 · SQL injection (critical)"}})
        # Wait for the LAST row emitted (render() is rAF-debounced, so the finding row lands a frame
        # after the worker row; waiting on the worker row and reading immediately raced it).
        self._transcript_contains("F-001")
        tx = self.page.text_content("#transcript")
        self.assertIn("worker spawned", tx.lower())   # the type chip, underscores -> spaces
        self.assertIn("w1 → service scan on 10.0.0.1", tx)
        self._emit({"type": "autonomous_done", "result": {"status": "completed", "reason": "3 findings"}})
        self._transcript_contains("Autonomous run finished — completed")
        self.assertNoJsErrors()


class TestInjectionFlag(TestFrontendBase):
    def test_gating_injection_flag_surfaces_a_warning(self):
        # The new UI surfaces prompt injection through the injection_flagged SSE event, not the old
        # inline [TOOL OUTPUT ... VERDICT:...] markers. Only a flag that actually gates the session
        # (tainted) is shown; a benign one is kept out of the conversation.
        self._enter_chat()
        self._emit({"type": "injection_flagged", "tainted": True, "tool": "http_request",
                    "verdict": "suspicious", "reasons": ["instruction-like text in an HTML comment"]})
        self._transcript_contains("Prompt injection flagged")
        tx = self.page.text_content("#transcript")
        self.assertIn("suspicious", tx)
        self.assertIn("passive recon", tx)
        self.assertNoJsErrors()

    def test_non_gating_injection_flag_is_not_shown(self):
        self._enter_chat()
        self._emit({"type": "injection_flagged", "tainted": False, "tool": "knowledge_fetch",
                    "verdict": "benign", "reasons": ["encoded blob"]})
        # Nothing added to the thread — the transcript stays on its empty-state copy.
        self.assertNotIn("injection", (self.page.text_content("#transcript") or "").lower())
        self.assertNoJsErrors()


class TestApprovalsDrawer(TestFrontendBase):
    def test_opens_empty_and_closes(self):
        self._open()
        self.page.wait_for_selector("#homeInput")
        self.page.click("[data-action='toggleApprovals']")
        self.page.wait_for_function(
            "() => document.body.innerText.includes('No pending approvals')", timeout=3000)
        # Three elements carry data-action=toggleApprovals when open (sidebar button, backdrop,
        # close icon); the close icon sits above the backdrop, so click that one.
        self.page.locator("[data-action='toggleApprovals']").last.click()
        self.page.wait_for_function("() => S.approvalsOpen === false", timeout=3000)
        self.assertNoJsErrors()

    def test_pending_approval_card_renders_approve_and_decline(self):
        self._enter_chat()
        self._set("""S.approvalsOpen=true;
            S.approvals=[{request_id:'ap-1',description:'Active exploitation step — time-based SQLi probe',
                          command:"POST /login username=admin' AND SLEEP(5)-- -"}];
            render();""")
        self.page.wait_for_function(
            "() => document.body.innerText.includes('time-based SQLi probe')", timeout=3000)
        body = self.page.text_content("body")
        self.assertIn("Approve", body)
        self.assertIn("Decline", body)
        self.assertNoJsErrors()


class TestEngagements(TestFrontendBase):
    def test_lists_default_engagement_and_creates_a_new_one(self):
        self._open()
        self.page.wait_for_selector("#homeInput")
        self.page.click("[data-action='openEngagements']")
        self.page.wait_for_function(
            "() => document.body.innerText.includes('lab-default')", timeout=5000)

        self.page.click("[data-action='newEngagement']")
        self.page.wait_for_selector("#neId", timeout=3000)
        self.page.fill("#neId", "test-eng")
        self.page.fill("#neScope", "10.9.9.0/24")
        self.page.click("[data-action='saveEng']")
        # Real create against the server; on success the engagement pill becomes the new id.
        self.page.wait_for_function("() => S.engagementId === 'test-eng'", timeout=5000)
        # And it is really persisted — it comes back from /api/engagements.
        engs = self.page.evaluate("async () => (await api.listEngagements()).map(e => e.engagement_id)")
        self.assertIn("test-eng", engs)
        self.assertNoJsErrors()


class TestWorkspaceViews(TestFrontendBase):
    def test_graph_view_opens_with_its_tabs(self):
        self._enter_chat()
        self.page.click("[data-action='openGraph']")
        self.page.wait_for_selector("#graphView", timeout=5000)
        view = self.page.text_content("#graphView")
        self.assertIn("Hypothesis Tree", view)
        self.assertIn("Notebook", view)
        self.assertIn("Findings", view)
        self.assertIn("hypotheses", view)  # the "N hypotheses" counter
        self.page.click("[data-action='closeGraph']")
        self.page.wait_for_function("() => S.view === 'chat'", timeout=3000)
        self.assertNoJsErrors()

    def test_flow_view_opens_with_its_lanes(self):
        self._enter_chat()
        self.page.click("[data-action='openFlow']")
        self.page.wait_for_function("() => S.view === 'flow'", timeout=5000)
        self.page.wait_for_function(
            "() => document.querySelector('main').innerText.includes('CALL PIPELINE')", timeout=5000)
        main = self.page.text_content("main")
        for lane in ("INPUTS", "CONTROL", "HYPOTHESES", "WAVE", "CALL PIPELINE", "STORES"):
            self.assertIn(lane, main, lane)
        self.assertIn("Strategist", main)
        self.assertNoJsErrors()

    def test_browser_view_opens(self):
        self._enter_chat()
        self.page.click("[data-action='openBrowser']")
        self.page.wait_for_function("() => S.view === 'browser'", timeout=5000)
        # The browser surface renders whether or not a frame ever paints (no tabs / start failure
        # both have a defined empty state); the point here is it opens without a JS exception.
        self.assertNoJsErrors()


if __name__ == "__main__":
    unittest.main()
