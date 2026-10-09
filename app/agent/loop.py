"""The agent loop: iterate -> call LLM -> parse tool_calls -> execute -> feed back -> repeat.

Tool-executing turns (safe-default profile) always use non-streaming chat_completions — this
is a deliberate Phase-1 simplification versus the module layout's "stream" ambition: assembling
tool_calls from streamed deltas is a known extra failure surface, and the validated 62-step
benchmark (research doc §1) used complete responses. diagnostic-thinking is a separate,
single-shot, non-executing mode (research doc: "disabled for autonomous tool execution until
separately tested") — it shows what the model would do without a scrubbed-env, argv-execution
tool actually running.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from . import audit_log as audit_mod
from . import budget as budget_mod
from . import config
from . import injection_guard
from . import session as session_mod
from .engagement import emit as event_emit
from .engagement.locks import engagement_lock
from .engagement.spend import BudgetExhausted as SpendBudgetExhausted, SpendLedger
from .llama_client import LlamaClient
from .loop_control.steer import SteerChannel
from .prompts.compiler import compile_prompt
from .tools import read_file, run_command, write_file

log = logging.getLogger("agent.loop")

TOOL_SCHEMAS = [run_command.SCHEMA, read_file.SCHEMA, write_file.SCHEMA]
# All served by the one security_mcp_server.py subprocess — routes _run_tool() there.
SECURITY_MCP_TOOLS = {
    "http_recon", "port_discovery",
    "knowledge_search", "knowledge_fetch", "osint_record", "browser_fetch",
    "record_hypothesis", "update_hypothesis_status", "record_finding",
}
# Per-observation cap for _observation_context_block() — found live (2026-09-01) that a single
# realistic http_recon_result observation (full page body_excerpt + redirect hop detail) was
# 20,615 chars on its own; injecting it raw blew the context budget outright
# (context_budget_exhausted: 12973 > 12697 tokens) on a two-observation engagement, before any
# real recon volume. Bounded the same head+tail-with-marker way budget.py's own
# truncate_tool_result() already handles oversized tool results, just char-based here (no live
# tokenizer round trip needed for a formatting step called once per ANALYSIS turn).
MAX_OBSERVATION_CONTENT_CHARS = 800


def _truncate_for_context(text: str, limit: int = MAX_OBSERVATION_CONTENT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.7)]
    tail = text[-int(limit * 0.3):]
    return f"{head}...[truncated, {len(text) - limit} chars omitted]...{tail}"


# Of those, the ones that actually call Broker.dispatch() internally and so already get their
# own richer audit entry from Broker._finalize() (policy rule, policy version, audit digest) —
# a second generic entry in _execute_tool_call() would be redundant. osint_record is
# deliberately NOT in this set: it makes no network call and isn't broker-mediated (see
# agent/security_mcp_server.py's own docstring on it), so the generic audit entry below is the
# *only* audit record it gets — excluding it here would leave every osint_record call unaudited.
BROKER_MEDIATED_TOOLS = {
    "http_recon", "port_discovery", "knowledge_search", "knowledge_fetch", "browser_fetch",
}
# Local engagement-state bookkeeping — no network call, nothing for the broker to gate, routed
# to the same subprocess as the tools above but dispatched distinctly (see _dispatch()). Found
# live (2026-09-01): record_hypothesis/update_hypothesis_status/record_finding were added to
# SECURITY_MCP_TOOLS/tool_schemas but _dispatch() only special-cased the literal string
# "osint_record" — every one of these would have fallen through to "unknown tool" and been
# rejected, the exact "registered server-side but the model can't actually call it" gap this
# project already hit once before with the original 4 M5.5 tools.
LOCAL_BOOKKEEPING_TOOLS = {
    "osint_record", "record_hypothesis", "update_hypothesis_status", "record_finding",
}
# injection_guard exists to catch attacker-influenced content reaching the model (a scanned
# webpage, a target's file content) — its threat model doesn't apply to a tool whose entire
# output is a static, git-cloned, locally-built reference corpus the operator controls (not
# target-derived, not live internet content). Found live: querying security_reference_search for
# a GTFOBins entry tripped "shell_substitution_pattern" on the *first* real call — GTFOBins
# content IS shell-substitution syntax by design, so every non-trivial lookup would taint the
# session (M4.5's escalate-to-approval behavior) for a false reason. Exempted, not the whole
# scanner disabled — a genuinely target/internet-sourced tool still gets scanned.
INJECTION_SCAN_EXEMPT_TOOLS = {"security_reference_search"}


def _graph_tool_names():
    # Imported lazily so the hypothesis_graph package (and its sqlite schema init) is only touched
    # by sessions that actually enable the graph — keeps import cost off the default path.
    from .hypothesis_graph.tools import GRAPH_TOOL_NAMES

    return GRAPH_TOOL_NAMES


def _notebook_tool_names():
    from .notebook.tools import NOTEBOOK_TOOL_NAMES

    return NOTEBOOK_TOOL_NAMES


def _knowledge_rag_tool_names():
    from .knowledge_rag.tools import KNOWLEDGE_RAG_TOOL_NAMES

    return KNOWLEDGE_RAG_TOOL_NAMES


class TaskResult:
    def __init__(self, status: str, message: str = "", detail: dict | None = None):
        self.status = status  # "ok" | "budget_exhausted" | "loop_break" | "cancelled" | "error"
        self.message = message
        self.detail = detail or {}

    def __repr__(self) -> str:
        return f"TaskResult(status={self.status!r}, message={self.message!r})"


class AgentLoop:
    def __init__(
        self,
        workspace_root: Path,
        session_id: str | None = None,
        profile: str = config.DEFAULT_PROFILE,
        dangerous_local: bool = False,
        confirm_fn=None,
        memory=None,
        use_mcp_tools: bool = False,
        use_security_tools: bool = False,
        device_id: str = "local",
        engagement_id: str = "lab-default",
        isolation_tier: str = "bubblewrap",  # ADR-0004: flipped from "direct"
        seed: int | None = None,
        use_approval_queue: bool = False,
        use_hypothesis_graph: bool = False,
        bootstrap_engagement: bool = False,
        on_stream=None,
        on_reasoning_stream=None,
        force_no_think: bool = False,
        stop_event=None,
        client=None,
    ):
        # Default to the local llama-server (unchanged behaviour); a caller — notably a wave
        # worker that must not touch the GPU — may inject an OpenRouter-backed client instead
        # (agent/llm/openrouter_loop_client.py), which satisfies the same interface.
        self.client = client if client is not None else LlamaClient()
        # force_no_think: suppress thinking mode even in THINKING_ENABLED_PHASES. For callers
        # whose turn *must* end in a tool call (the autonomous driver's judgment tasks) — see
        # the comment in _run_safe_default where this is applied.
        self.force_no_think = force_no_think
        # on_stream(chunk: str): if set, final-answer generation streams and each content
        # fragment is handed over as it arrives (the web UI forwards these as SSE so the reply
        # types itself out instead of landing all at once). Off for the CLI / eval / autonomous
        # driver — they pass nothing and get the exact non-streaming path.
        self.on_stream = on_stream
        # on_reasoning_stream(chunk: str): same idea for the model's `reasoning_content` stream
        # (thinking-mode output llama-server sends as its own field — see the comment further
        # down where reasoning_content is read from the response). Independent of on_stream so a
        # caller can show live thinking without also wanting content deltas, or vice versa.
        self.on_reasoning_stream = on_reasoning_stream
        self.workspace_root = workspace_root
        self.hypothesis_graph = None
        self.notebook = None
        self.knowledge_rag = None
        self.profile = profile
        self.dangerous_local = dangerous_local
        self.isolation_tier = isolation_tier
        self.seed = seed
        # A cooperative stop, checked once per iteration. A wave worker (§2.5) is an AgentLoop
        # given the orchestrator's stop event; when the wall-clock is hit the orchestrator sets it
        # and the loop returns a 'cancelled' result at the next turn boundary, rather than being
        # force-killed mid-generation (a thread cannot be). What it had already recorded stays.
        self.stop_event = stop_event
        self._spend_ledger: SpendLedger | None = None
        self.engagement_id = engagement_id
        self.prompt_version: str | None = None  # set on first _system_message() call
        # Injected so main.py can supply input(); tests can supply an auto-yes/no stub.
        self.confirm_fn = confirm_fn or (lambda prompt: input(prompt).strip().lower() == "y")

        # Phase 2: `memory` is an optional RemoteSessionStore (agent/memory_client.py); falling
        # back to the local JSONL store keeps Phase 1 behavior unchanged when omitted — additive,
        # not a rewrite (research doc's own rule for each Phase-2+ increment).
        if memory is not None:
            self.session = memory
            self.session_id = memory.session_id
        else:
            self.session_id = session_id or session_mod.SessionStore.new_session_id()
            self.session = session_mod.SessionStore(self.session_id)
        self.audit = audit_mod.AuditLog(self.session_id)

        # Phase 2: MCP-ified generic tools run in a client-local subprocess (Topology B, §6)
        # instead of being called as plain Python functions in-process. Imported lazily so
        # Phase 1 keeps working on plain system Python with no `mcp`/`fastapi` deps installed
        # (those only live in the project .venv) unless Phase 2+ mode is actually requested.
        self.mcp_client = None
        self.security_mcp_client = None
        self.tool_schemas = list(TOOL_SCHEMAS)
        if use_mcp_tools:
            from .mcp_tool_client import MCPToolClient

            args = ["--workspace", str(workspace_root), "--isolation-tier", isolation_tier]
            if dangerous_local:
                args.append("--dangerous-local")
            self.mcp_client = MCPToolClient(config.MCP_SERVER_MODULE, args)

        if use_security_tools:
            # A real security session (web UI, CLI --security-tools) points at an engagement the
            # operator expects to just work. `lab-default` in particular has no state.db until
            # something makes one, and security_mcp_server's record_finding / osint_record /
            # record_hypothesis all fail closed without it. Off by default so unit tests that
            # construct a loop don't touch real engagement state; the entry points opt in.
            if bootstrap_engagement:
                from .engagement.bootstrap import bootstrap_engagement as _bootstrap_engagement_state

                try:
                    _bootstrap_engagement_state(engagement_id)
                except Exception:
                    log.warning("engagement bootstrap failed for %r", engagement_id, exc_info=True)

            from .mcp_tool_client import MCPToolClient
            from .security_mcp_server import (
                BROWSER_FETCH_SCHEMA, OSINT_RECORD_SCHEMA, RECORD_FINDING_SCHEMA,
                RECORD_HYPOTHESIS_SCHEMA, UPDATE_HYPOTHESIS_STATUS_SCHEMA,
            )
            from .internet.dispatcher import KNOWLEDGE_FETCH_SCHEMA, KNOWLEDGE_SEARCH_SCHEMA
            from .security_tools import http_recon, port_discovery

            security_mcp_args = [
                "--session-id", self.session_id, "--device-id", device_id,
                "--engagement-id", engagement_id,
            ]
            if use_approval_queue:
                # Phase 6 web UI: the subprocess's Broker can't read this process's stdin for a
                # synchronous confirm_fn prompt (it's a separate OS process on its own stdio
                # pipe, already claimed by the MCP protocol) — --use-approval-queue switches it
                # to blocking on agent.broker.approval_queue.ApprovalQueue instead, which a UI
                # (or anything else) can resolve out-of-band. Off by default: the CLI's
                # synchronous input()-based approval remains unchanged for every existing caller.
                security_mcp_args.append("--use-approval-queue")
            self.security_mcp_client = MCPToolClient(
                config.SECURITY_MCP_SERVER_MODULE, security_mcp_args,
            )
            # All served by the one security_mcp_server.py subprocess above. Each is still
            # individually gated by the broker's RoE allowed_action_classes check — listing a
            # schema here only makes the model *aware* a tool exists, it doesn't grant it
            # anything; knowledge_search/knowledge_fetch/browser_fetch all deny by default until
            # an RoE explicitly opts in (see agent/broker/broker.py's TOOL_ACTION_CLASS).
            self.tool_schemas += [
                http_recon.SCHEMA, port_discovery.SCHEMA,
                KNOWLEDGE_SEARCH_SCHEMA, KNOWLEDGE_FETCH_SCHEMA,
                OSINT_RECORD_SCHEMA, BROWSER_FETCH_SCHEMA,
            ]
            # General-knowledge RAG (GTFOBins/LOLBAS/PayloadsAllTheThings, offline/pre-embedded —
            # see agent/knowledge_rag/'s module docstrings) — a reference lookup, not a target
            # action or engagement state, so it rides alongside the security tools unconditionally
            # rather than behind use_hypothesis_graph. Degrades to a clean tool-result error if the
            # index hasn't been built yet or the embedding server isn't up; never blocks the loop.
            from .knowledge_rag.service import KnowledgeRAGService
            from .knowledge_rag.tools import SCHEMAS as KNOWLEDGE_RAG_SCHEMAS

            self.knowledge_rag = KnowledgeRAGService()
            self.tool_schemas += KNOWLEDGE_RAG_SCHEMAS
            # use_hypothesis_graph turns on the full structured working-memory surface: the
            # Hypothesis Graph (which subsumes the flat record_hypothesis/update_hypothesis_status
            # pair — so those two are NOT added, no double surface, flat prompt budget) AND the
            # Working Notebook (docs/working-notebook-spec.md — cross-cutting notes / techniques /
            # todos / dead-ends). record_finding stays either way. Graph off => exactly the prior
            # behavior: the three flat tools, no graph/notebook tools, no digests.
            if use_hypothesis_graph:
                from .hypothesis_graph.service import HypothesisGraphService
                from .hypothesis_graph.tools import SCHEMAS as GRAPH_SCHEMAS
                from .notebook.service import NotebookService
                from .notebook.tools import SCHEMAS as NOTEBOOK_SCHEMAS

                self.hypothesis_graph = HypothesisGraphService(config.ENGAGEMENTS_ROOT / engagement_id)
                self.notebook = NotebookService(config.ENGAGEMENTS_ROOT / engagement_id)
                self.tool_schemas += [RECORD_FINDING_SCHEMA, *GRAPH_SCHEMAS, *NOTEBOOK_SCHEMAS]
            else:
                self.tool_schemas += [
                    RECORD_HYPOTHESIS_SCHEMA, UPDATE_HYPOTHESIS_STATUS_SCHEMA, RECORD_FINDING_SCHEMA,
                ]

        self.completed_turns: list[budget_mod.Turn] = self._restore_turns()
        self._recent_calls: list[tuple[str, str]] = []
        self._turn_counter = len(self.completed_turns)

    def _spend(self) -> SpendLedger:
        """The engagement's spend ledger (§14.1 D), opened once. Resolved from config at call time
        (not bound at import) so a test patching ENGAGEMENTS_ROOT is honoured."""
        if self._spend_ledger is None:
            self._spend_ledger = SpendLedger(config.ENGAGEMENTS_ROOT / self.engagement_id)
        return self._spend_ledger

    def close(self) -> None:
        if self.mcp_client is not None:
            self.mcp_client.close()
        if self.security_mcp_client is not None:
            self.security_mcp_client.close()

    def _restore_turns(self) -> list[budget_mod.Turn]:
        """Rebuild working memory as one Turn per prior run_task call, split on user messages."""
        messages = self.session.load_as_messages()
        turns: list[budget_mod.Turn] = []
        current: list[dict] = []
        for m in messages:
            if m["role"] == "user" and current:
                turns.append(budget_mod.Turn(messages=current))
                current = []
            current.append(m)
        if current:
            turns.append(budget_mod.Turn(messages=current))
        return turns

    def _current_phase(self) -> str | None:
        """None unless `self.engagement_id` names an M5.1-created engagement with live M5.2
        phase state (`engagements/<id>/state.db`) — the common case today (the "lab-default"
        label used without an `agent.engagement.intake`-created directory) has no such file, so
        this returns None and the compiled prompt simply carries no phase-specific layer, same
        as before this was wired in. A state-store problem degrades to "no phase context" rather
        than breaking the loop's ability to produce a system message at all — logged, not
        silently swallowed."""
        state_db = config.ENGAGEMENTS_ROOT / self.engagement_id / "state.db"
        if not state_db.exists():
            return None
        try:
            from .engagement.store import EngagementStore

            store = EngagementStore(config.ENGAGEMENTS_ROOT / self.engagement_id)
            return store.get_phase()["current_phase"]
        except Exception:
            log.warning("phase lookup failed for engagement %r, compiling without phase context", self.engagement_id, exc_info=True)
            return None

    def _system_message(self) -> dict:
        """M4.7: compiled via the prompt registry (agent/prompts/) rather than a hardcoded
        string — same substance as the original Phase-1..4 system message (migrated into
        layered files, not rewritten), plus the authority/data-provenance/failure-behavior
        layers the review set's proposed core prompts all independently converged on. The
        compiled digest is stored on self.prompt_version for the audit trail and eval metadata
        — "record prompt digest and exact tool schema set per run" (integration prompt's own
        system-prompt/memory/database decisions section).

        Now also passes `phase` (M5.2 pipeline state, when the engagement has any — see
        `_current_phase()`) and `active_tools` (this session's actual tool schema names) so the
        compiled prompt includes phase-specific guidance and per-tool cards
        (`agent/prompts/phases/`, `agent/prompts/tools/`) when available — additive, and both
        args degrade to their prior no-op behavior (no phase layer, no tool cards) when absent.
        """
        active_tools = [schema["function"]["name"] for schema in self.tool_schemas]
        compiled = compile_prompt(
            {
                "workspace_root": str(self.workspace_root),
                "security_tools_enabled": self.security_mcp_client is not None,
                "in_scope_targets": self._in_scope_targets_str(),
            },
            phase=self._current_phase(),
            active_tools=active_tools,
        )
        self.prompt_version = compiled.digest
        return {"role": "system", "content": compiled.text}

    def _in_scope_targets_str(self) -> str:
        """The engagement's scope.txt allow-list, verbatim, for the system prompt — the broker
        enforces scope regardless, but stating it stops the model wasting a turn on a lab
        default it's over-anchored on (127.0.0.1:3000), getting denied, and abandoning. Reads
        scope.txt directly rather than via load_policy: a stale/expired roe.json shouldn't strip
        the target list out of the prompt. Empty for a non-security session, or if there's no
        scope.txt yet."""
        if self.security_mcp_client is None:
            return ""
        try:
            scope_path = config.ENGAGEMENTS_ROOT / self.engagement_id / "scope.txt"
            if not scope_path.exists():
                return ""
            entries = [
                line.strip() for line in scope_path.read_text().splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            return ", ".join(entries)
        except Exception:
            log.warning("scope lookup for the system prompt failed for %r", self.engagement_id, exc_info=True)
            return ""

    def _max_tokens(self) -> int:
        if self.profile == config.PROFILE_DIAGNOSTIC_THINKING:
            return config.DIAGNOSTIC_MAX_TOKENS
        if getattr(self, "_task_thinking_enabled", False):
            return config.ANALYSIS_THINKING_MAX_TOKENS
        return config.SAFE_DEFAULT_MAX_TOKENS

    def run_task(self, user_input: str) -> TaskResult:
        # Serialize whole runs against the same engagement — see agent/engagement/locks.py's own
        # docstring for the real duplicate-hypothesis bug this closes. Only when this session
        # actually touches engagement-scoped shared state (hypothesis graph / security tools) —
        # a plain chat session sharing the default engagement_id string but never reading/writing
        # its stores has nothing to serialize against and shouldn't pay for or wait on this.
        if self.hypothesis_graph is not None or self.security_mcp_client is not None:
            with engagement_lock(self.engagement_id):
                return self._run_task_inner(user_input)
        return self._run_task_inner(user_input)

    def _run_task_inner(self, user_input: str) -> TaskResult:
        if self.profile == config.PROFILE_DIAGNOSTIC_THINKING:
            return self._run_diagnostic(user_input)
        return self._run_safe_default(user_input)

    # -- diagnostic-thinking: single shot, no execution --------------------------------------

    def _run_diagnostic(self, user_input: str) -> TaskResult:
        self.session.append("user", user_input)
        system = self._system_message()
        messages = [system] + self.session.load_as_messages()
        resp = self.client.chat_completions(
            messages, tools=self.tool_schemas, max_tokens=self._max_tokens(), temperature=0.7,
            seed=self.seed,
        )
        choice = resp["choices"][0]["message"]
        content = choice.get("content") or ""
        # llama-server splits thinking-mode output into its own field rather than embedding
        # <think> tags in `content` (confirmed live 2026-09-01: content came back empty on a
        # thinking turn, with the entire reasoning trace in reasoning_content instead) — reading
        # only `content` silently discarded every reasoning token generated. Kept out of
        # `content`/session `load_as_messages()` on purpose: re-feeding a stale reasoning trace
        # into later turns as if it were a normal reply isn't how any of the reasoning-parser
        # conventions this model's tooling follows treat it, and would only bloat context.
        reasoning_content = choice.get("reasoning_content") or ""
        tool_calls = choice.get("tool_calls") or []
        extra = {}
        if tool_calls:
            extra["tool_calls"] = tool_calls
        if reasoning_content:
            extra["reasoning_content"] = reasoning_content
        self.session.append("assistant", content, extra=extra or None)
        detail = {"tool_calls_would_run": tool_calls, "usage": resp.get("usage")}
        if reasoning_content:
            detail["reasoning_content"] = reasoning_content
        return TaskResult("ok", message=content, detail=detail)

    # -- safe-default: full tool-executing loop -----------------------------------------------

    def _observation_context_block(self, phase: str | None) -> str | None:
        """ANALYSIS's own phase prompt (phases/analysis.md) tells the model to "review what
        RECON actually observed" — found live (2026-09-01 verification run, real Juice Shop
        observations seeded) that nothing actually supplied that data: the model duly obeyed the
        instruction to reason over "existing observations" by inventing a plausible-sounding but
        entirely fictitious set (Apache/2.4.42, an /admin endpoint) instead. Scoped to ANALYSIS
        only, not every phase: RECON/VALIDATION reach engagement state through their own tool
        calls, which is a real look at current data — ANALYSIS is the one phase that's
        deliberately tool-call-free by design, so this is its only way to see it at all.
        """
        if phase != "ANALYSIS":
            return None
        state_db = config.ENGAGEMENTS_ROOT / self.engagement_id / "state.db"
        if not state_db.exists():
            return None
        try:
            from .engagement.store import EngagementStore

            store = EngagementStore(config.ENGAGEMENTS_ROOT / self.engagement_id)
            observations = store.list_observations()
            hypotheses = store.list_hypotheses(status="open") + store.list_hypotheses(status="testing")
        except Exception:
            log.warning(
                "observation lookup failed for engagement %r, ANALYSIS proceeds without it",
                self.engagement_id, exc_info=True,
            )
            return None
        if not observations and not hypotheses:
            return None

        lines = [
            "RECORDED OBSERVATIONS AND HYPOTHESES (from engagement state — this is the actual "
            "data to reason over; do not invent observations not listed here):"
        ]
        for obs in observations:
            content = _truncate_for_context(obs["content"])
            lines.append(f"- [{obs['observation_type']}] {content} (source: {obs['source']})")
        for hyp in hypotheses:
            lines.append(f"- [existing hypothesis, {hyp['status']}] {hyp['title']}: {hyp['description']}")
        return "\n".join(lines)

    def _graph_context_block(self, phase: str | None) -> str | None:
        """Injected every turn when use_hypothesis_graph=True — unlike
        _observation_context_block (ANALYSIS-only), the graph's digest + active path + top-N
        actionable hypotheses matter in every phase: VALIDATION needs to know which hypothesis
        it's testing, RECON/other tool-calling turns still benefit from "what does the operator
        already know". See hypothesis_graph/service.py's build_context_block for the anti-
        forgetting design (digest = full-graph compressed view, never dropped; retrieval tools
        named so the model fetches instead of guessing)."""
        if self.hypothesis_graph is None:
            return None
        try:
            return self.hypothesis_graph.build_context_block(current_phase=phase)
        except Exception:
            log.warning(
                "hypothesis graph context build failed for engagement %r, proceeding without it",
                self.engagement_id, exc_info=True,
            )
            return None

    def _notebook_context_block(self) -> str | None:
        """The [NOTEBOOK] digest — injected every phase (a dead-end matters in VALIDATION as much
        as ANALYSIS). Bounded and built deterministically from stored notes, same anti-forgetting
        rule as the graph digest."""
        if self.notebook is None:
            return None
        try:
            return self.notebook.build_context_block()
        except Exception:
            log.warning("notebook context build failed for engagement %r, proceeding without it",
                        self.engagement_id, exc_info=True)
            return None

    def _run_safe_default(self, user_input: str) -> TaskResult:
        # Decided once per task, not per iteration: a phase transition mid-task shouldn't flip
        # thinking on/off between iterations of the same run_task() call. See config.py's
        # THINKING_ENABLED_PHASES comment for why ANALYSIS specifically, and why every other
        # phase keeps the fast /no_think path.
        phase = self._current_phase()
        # force_no_think overrides the phase rule: the autonomous driver's judgment tasks
        # (_run_model_task) run at ANALYSIS/VALIDATION but *must* call record_hypothesis /
        # update_hypothesis_status, and at this box's ~3 tok/s the model spends the whole
        # ANALYSIS_THINKING_MAX_TOKENS budget on reasoning and hits finish_reason:length before
        # it emits a single tool call — an empty turn, 0 hypotheses every run. Assistant-mode
        # ANALYSIS keeps thinking (a human is waiting, no tool call is required of it).
        self._task_thinking_enabled = (
            phase in config.THINKING_ENABLED_PHASES and not self.force_no_think
        )
        blocks = [b for b in (
            self._observation_context_block(phase),
            self._graph_context_block(phase),
            self._notebook_context_block(),
        ) if b]
        full_input = "\n\n".join([user_input, *blocks])
        suffixed = full_input if self._task_thinking_enabled else f"{full_input}\n\n{config.NO_THINK_SUFFIX}"
        self.session.append("user", suffixed)
        current_turn = budget_mod.Turn(messages=[{"role": "user", "content": suffixed}])
        system = self._system_message()

        def finish(result: TaskResult) -> TaskResult:
            # current_turn is complete (or as complete as it'll get) — fold it into working
            # memory so the *next* run_task() call still has this task's context. Session-log
            # persistence already happened incrementally via self.session.append(); this is
            # purely about in-memory budget state.
            self.completed_turns.append(current_turn)
            return result

        start = time.monotonic()
        iteration = 0
        while True:
            iteration += 1
            if self.stop_event is not None and self.stop_event.is_set():
                # Cooperative hard stop (§2.5 / A6): the wave's wall-clock fired, or an operator
                # cancelled. Return at the turn boundary so the partial work already recorded this
                # run stays on the trajectory.
                return finish(TaskResult("cancelled", "stop requested"))
            if iteration > config.MAX_ITERATIONS:
                return finish(TaskResult("budget_exhausted", "max iterations exceeded"))
            if time.monotonic() - start > config.TASK_WALL_CLOCK_S:
                return finish(TaskResult("budget_exhausted", "task wall-clock timeout exceeded"))

            # Phase 6: a pending steer message (agent/loop_control/steer.py) is injected as a
            # user-role message before this iteration's model call, letting an operator redirect
            # a running task without stopping it. Consumed once (take_pending() clears it), so a
            # message sent mid-generation is picked up on the *next* iteration, not lost and not
            # re-injected every loop.
            steer_message = SteerChannel(self.session_id).take_pending()
            if steer_message is not None:
                steer_content = f"[OPERATOR STEER] {steer_message}"
                current_turn.messages.append({"role": "user", "content": steer_content})
                self.session.append("user", steer_content)

            turns_for_budget = self.completed_turns + [current_turn]
            try:
                messages_for_api, remaining, evicted = budget_mod.fit_to_budget(
                    self.client, system, turns_for_budget, self.tool_schemas
                )
            except budget_mod.BudgetExhausted as e:
                return finish(TaskResult("budget_exhausted", str(e)))

            self.completed_turns = remaining[:-1] if remaining and remaining[-1] is current_turn else remaining
            if evicted:
                log.info("evicted %d turn(s) from working memory (still in session log)", len(evicted))

            # §14.1 D / §7.2: reserve the spend slot BEFORE the call so a crossed cap stops the
            # next call rather than degrading it — single-worker mode reserves then settles
            # immediately; the wave only splits the two moments. Default-unmetered means reserve
            # never refuses, so this is a no-op until a cap is set in roe.json. The estimate is 0
            # (the cost is not known until the response); the slot exists for the wave to pass a
            # real estimate. A ledger error must not break the run.
            try:
                _spend_rid = self._spend().reserve(0.0, worker=self.session_id)
            except SpendBudgetExhausted as e:
                return finish(TaskResult("budget_exhausted", str(e)))
            except Exception:  # noqa: BLE001 - telemetry/ledger failure never stops the run
                _spend_rid = None

            call_start = time.monotonic()
            resp = self.client.chat_completions(
                messages_for_api,
                tools=self.tool_schemas,
                max_tokens=self._max_tokens(),
                temperature=0.7,
                seed=self.seed,
                timeout_s=config.ANALYSIS_THINKING_REQUEST_TIMEOUT_S if self._task_thinking_enabled else None,
                on_delta=self.on_stream,
                on_reasoning_delta=self.on_reasoning_stream,
            )
            latency_ms = (time.monotonic() - call_start) * 1000
            # §4.2 model_call — feeds the flow view's model roster (§6.3). `usage.cost` is the
            # provider's own actual cost (OpenRouter reports it per response; local llama has
            # none, so 0). role is the profile this loop runs under.
            _usage = resp.get("usage") or {}
            _cost = float(_usage.get("cost") or 0.0)
            event_emit.emit(self.engagement_id, "model_call", {
                "model_id": resp.get("model") or "model",
                "role": self.profile,
                "prompt_tokens": _usage.get("prompt_tokens", 0),
                "completion_tokens": _usage.get("completion_tokens", 0),
                "cost": _cost,
            })
            # Settle the actual spend and publish the budget (§4.2 budget_updated). Never breaks
            # the run on a ledger error.
            if _spend_rid is not None:
                try:
                    self._spend().settle(_spend_rid, _cost)
                    event_emit.emit(self.engagement_id, "budget_updated", self._spend().snapshot())
                except Exception:  # noqa: BLE001
                    log.warning("failed to settle spend for this model call", exc_info=True)
            choice = resp["choices"][0]["message"]
            content = choice.get("content") or ""
            # See _run_diagnostic's comment: llama-server returns thinking-mode output in its own
            # field, not embedded in `content` — reading only `content` silently discards it.
            # Persisted via `extra` (audit/session visibility) but never fed back into
            # current_turn.messages/load_as_messages(), same reasoning as tool_calls already not
            # being replayed as if it were plain content.
            reasoning_content = choice.get("reasoning_content") or ""
            tool_calls = choice.get("tool_calls") or []

            assistant_msg = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            current_turn.messages.append(assistant_msg)
            session_extra = {}
            if tool_calls:
                session_extra["tool_calls"] = tool_calls
            if reasoning_content:
                session_extra["reasoning_content"] = reasoning_content
            assistant_record = self.session.append("assistant", content, extra=session_extra or None)

            if not tool_calls:
                final_detail = {"usage": resp.get("usage")}
                if reasoning_content:
                    final_detail["reasoning_content"] = reasoning_content
                return finish(TaskResult("ok", message=content, detail=final_detail))

            for call in tool_calls:
                # The Hypothesis Graph's chat anchors (docs/hypothesis-graph-ui-spec.md §5.8)
                # need a real message_id to jump to — this assistant turn's own record id is the
                # exact "creation"/"attempt start"/"result" anchor for whatever graph tool it
                # calls, so it's threaded through rather than left unset (which would make chat
                # anchors a UI stub with nothing to jump to).
                result = self._execute_tool_call(
                    call, self._turn_counter, latency_ms, resp, message_id=assistant_record["message_id"]
                )
                current_turn.messages.append(result["tool_message"])
                self.session.append(
                    "tool",
                    result["tool_message"]["content"],
                    extra={
                        "tool_call_id": result["tool_message"]["tool_call_id"],
                        "name": result["tool_message"]["name"],
                    },
                )
                if result.get("loop_break"):
                    return finish(
                        TaskResult(
                            "loop_break",
                            f"repeated identical call {config.REPEAT_THRESHOLD}x in a row: "
                            f"{result['tool_name']}({result['args_str']})",
                        )
                    )
            self._turn_counter += 1

    def _execute_tool_call(
        self, call: dict, turn_index: int, latency_ms: float, resp: dict, message_id: str | None = None,
    ) -> dict:
        fn = call["function"]
        tool_name = fn["name"]
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError as e:
            args = {}
            parse_error = str(e)
        else:
            parse_error = None

        call_key = (tool_name, json.dumps(args, sort_keys=True))
        self._recent_calls.append(call_key)
        loop_break = (
            len(self._recent_calls) >= config.REPEAT_THRESHOLD
            and len(set(self._recent_calls[-config.REPEAT_THRESHOLD :])) == 1
        )

        confirmed_by = None
        exec_start = time.monotonic()
        if parse_error:
            raw_result = {"ok": False, "error": f"malformed tool_call arguments: {parse_error}"}
            scope_decision = "rejected_malformed"
        else:
            raw_result, scope_decision, confirmed_by = self._dispatch(tool_name, args, message_id=message_id)
        exec_ms = (time.monotonic() - exec_start) * 1000

        raw_text = json.dumps(raw_result)
        if tool_name in INJECTION_SCAN_EXEMPT_TOOLS:
            scan_result = injection_guard.ScanResult(matched=False)
        else:
            scan_result = injection_guard.scan(raw_text)
        truncated = budget_mod.truncate_tool_result(self.client, raw_text)
        wrapped = injection_guard.wrap(truncated, scan_result)

        if scan_result.matched:
            print(f"\n[!] injection_guard flagged tool output: {', '.join(scan_result.reasons)}\n")
            # M4.5: mark the session tainted so the broker escalates subsequent
            # beyond-passive-recon actions to human approval for a bounded window — stdlib-only
            # import, safe on plain system Python (see broker/taint.py's own docstring).
            from .broker.taint import ENGAGEMENT_SHARED_TAINT_SOURCES, TaintStore

            # §14.1 C: a flagged tool whose vector is engagement-shared state (the browser
            # profile) escalates taint to the whole engagement, so a sibling worker sharing that
            # profile is scrutinised too; a session-local read stays on this session.
            TaintStore(self.session_id, engagement_id=self.engagement_id).mark(
                reason=", ".join(scan_result.reasons),
                verdict=scan_result.verdict,
                source=tool_name,
                shared=tool_name in ENGAGEMENT_SHARED_TAINT_SOURCES,
            )

        if tool_name not in BROKER_MEDIATED_TOOLS:
            # Broker-mediated tools (http_recon, port_discovery) already get a richer audit
            # entry from Broker._finalize() inside the security-tools MCP subprocess — policy
            # rule, policy version, and audit digest included. A second generic entry here
            # would just be redundant noise, not additional safety.
            self.audit.record(
                turn_index=turn_index,
                tool_name=tool_name,
                action_rationale=(fn.get("arguments") or "")[:300],
                arguments=args,
                raw_output=raw_text,
                sanitized_output=wrapped,
                scope_decision=scope_decision,
                approval_identity=confirmed_by,
                prompt_tokens=resp.get("usage", {}).get("prompt_tokens"),
                completion_tokens=resp.get("usage", {}).get("completion_tokens"),
                latency_ms=latency_ms + exec_ms,
                exit_code=raw_result.get("exit_code") if isinstance(raw_result, dict) else None,
                injection_flagged=scan_result.matched,
                # From the RESULT, not from self.isolation_tier: audit_log's contract is "the
                # tier the action ACTUALLY executed under", and the requested tier is not
                # evidence of that — resolve_tier can refuse, and a refusal reports no tier at
                # all. This was omitted entirely, so every sandboxed command execution in the
                # CLI recorded None, indistinguishable from an action with no isolation
                # dimension such as an HTTP request. The web path already passed it.
                isolation_tier=(
                    raw_result.get("isolation_tier") if isinstance(raw_result, dict) else None
                ),
                prompt_version=self.prompt_version,
            )

        tool_message = {
            "role": "tool",
            "tool_call_id": call.get("id", ""),
            "name": tool_name,
            "content": wrapped,
        }
        return {
            "tool_message": tool_message,
            "loop_break": loop_break,
            "tool_name": tool_name,
            "args_str": json.dumps(args),
        }

    def _run_tool(self, tool_name: str, args: dict) -> dict:
        """The actual execution step — in-process call (Phase 1) or MCP round trip (Phase 2).

        The confirmation gate and preflight block in _dispatch() run *before* this regardless
        of which path executes: that logic needs the human at the terminal, which is the
        client/orchestrator process, not the MCP tool subprocess (research doc §6: tool
        servers are dumb executors, policy stays with the orchestrator/broker).
        """
        if tool_name in SECURITY_MCP_TOOLS:
            if self.security_mcp_client is None:
                raise ValueError(f"{tool_name} called but use_security_tools=False")
            return self.security_mcp_client.call_tool(tool_name, args)

        if self.mcp_client is not None:
            return self.mcp_client.call_tool(tool_name, args)

        if tool_name == "read_file":
            return read_file.run(args.get("path", ""), self.workspace_root)
        if tool_name == "write_file":
            return write_file.run(
                args.get("path", ""),
                args.get("content", ""),
                self.workspace_root,
                args.get("mode", "overwrite"),
            )
        if tool_name == "run_command":
            return run_command.run(
                args.get("argv") or [],
                self.workspace_root,
                cwd=args.get("cwd"),
                timeout_s=args.get("timeout_s"),
                dangerous_local=self.dangerous_local,
                isolation_tier=self.isolation_tier,
            )
        raise ValueError(f"unknown tool: {tool_name}")

    def _dispatch(self, tool_name: str, args: dict, message_id: str | None = None) -> tuple[dict, str, str | None]:
        """Returns (result_dict, scope_decision, approval_identity_or_None)."""
        if tool_name in BROKER_MEDIATED_TOOLS:
            # The broker (running inside the security-tools MCP subprocess) makes its own
            # scope/rate/kill-switch/approval decisions and writes its own audit entry — the
            # scope_decision returned here is just for loop.py's own bookkeeping, not
            # authoritative (that's the broker's response `status`/`policy_rule`, inside the
            # result dict this call returns).
            result = self._run_tool(tool_name, args)
            return result, f"broker:{result.get('status', 'unknown')}", None

        if tool_name in LOCAL_BOOKKEEPING_TOOLS:
            # Routed to the same subprocess as the broker-mediated tools (it's local
            # bookkeeping, not a network call, so nothing for the broker to gate) but its
            # result shape is {"ok": ..., ...}, not an ActionResponse — handled distinctly so
            # scope_decision reads as what actually happened, not a fake "broker:unknown".
            result = self._run_tool(tool_name, args)
            return result, f"recorded:{result.get('ok', False)}", None

        if self.hypothesis_graph is not None and tool_name in _graph_tool_names():
            # Hypothesis Graph tools run IN-PROCESS against the state service, NOT the broker
            # (doc 2 §12: graph mutation is not a target action). Still audited via loop.py's own
            # generic entry below (they're not in BROKER_MEDIATED_TOOLS).
            from .hypothesis_graph.tools import dispatch as graph_dispatch

            # Chat anchors (docs/hypothesis-graph-ui-spec.md §5.8) need a real message_id — this
            # assistant turn's own id is exactly "the message that created/started/completed"
            # whatever the model is calling right now, so it's stamped on automatically rather
            # than trusting the model to pass one (it has no reason to know its own message id).
            if message_id is not None:
                args = dict(args)
                if tool_name == "graph_hypothesis_add":
                    args.setdefault("origin_ref", message_id)
                elif tool_name == "graph_attempt_start":
                    args.setdefault("chat_start_message_id", message_id)
                elif tool_name == "graph_attempt_complete":
                    args.setdefault("chat_result_message_id", message_id)
            result = graph_dispatch(self.hypothesis_graph, tool_name, args)
            return result, f"graph:{result.get('ok', False)}", None

        if self.notebook is not None and tool_name in _notebook_tool_names():
            # Working Notebook tools — in-process against the state service, not the broker (a
            # note is not a target action). This assistant turn's own message id is stamped on a
            # note_add so the web panel's "go to chat" jumps to where it was written.
            from .notebook.tools import dispatch as notebook_dispatch

            if message_id is not None:
                if tool_name == "note_add":
                    args = {**args, "chat_message_id": args.get("chat_message_id") or message_id}
                elif tool_name == "note_promote":
                    args = {**args, "origin_ref": args.get("origin_ref") or message_id}
            result = notebook_dispatch(self.notebook, tool_name, args, graph=self.hypothesis_graph)
            return result, f"notebook:{result.get('ok', False)}", None

        if self.knowledge_rag is not None and tool_name in _knowledge_rag_tool_names():
            from .knowledge_rag.tools import dispatch as knowledge_rag_dispatch

            result = knowledge_rag_dispatch(self.knowledge_rag, tool_name, args)
            return result, f"knowledge_rag:{result.get('ok', False)}", None

        if tool_name in ("read_file", "write_file"):
            return self._run_tool(tool_name, args), "allowed_local", None

        if tool_name == "run_command":
            argv = args.get("argv") or []
            blocked, reason = run_command.preflight(argv, self.dangerous_local)
            if blocked:
                return {"ok": False, "blocked": True, "error": reason}, "blocked_denylist", None

            confirmed_by = None
            if run_command.needs_confirmation(argv):
                approved = self.confirm_fn(f"\n[confirm] run: {' '.join(argv)}  [y/N] ")
                if not approved:
                    return {"ok": False, "error": "rejected by human confirmation"}, "denied_by_human", None
                confirmed_by = "human"

            result = self._run_tool(tool_name, args)
            decision = "confirmed_by_user" if confirmed_by else "allowed_local"
            return result, decision, confirmed_by

        return {"ok": False, "error": f"unknown tool: {tool_name}"}, "rejected_unknown_tool", None
