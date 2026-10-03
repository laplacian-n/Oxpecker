"""Stdio MCP server exposing the Phase-3 security tools — every call goes through the broker
(agent/broker/broker.py), never directly to http_recon.run()/port_discovery.run(). Same
Topology B placement as mcp_tools_server.py: runs on the client machine, not the GPU box.

M5.5's `knowledge_search`/`knowledge_fetch` channels (agent/internet/dispatcher.py) are exposed
here too, broker-mediated like the two Phase-3 tools — same audit/evidence/rate-limit/taint-
escalation path, gated by their own `knowledge_search`/`knowledge_fetch` action classes (an RoE
must explicitly allow them, they are not implied by `passive_recon`). Their own internal
budget/SSRF/provider-allowlist checks (agent/internet/policy.py, agent/internet/budget.py) run
independently of and in addition to the broker's RoE-class gate — two different, complementary
things being checked, not redundant layers.
"""
from __future__ import annotations

import argparse

from mcp.server.mcpserver import MCPServer

from . import config
from .broker.broker import Broker
from .broker.contracts import ActionRequest
from .engagement.store import HYPOTHESIS_STATUSES, EngagementStore
from .findings.model import VALID_CONFIDENCE, VALID_SEVERITIES, VALID_STATUS, Finding, FindingsStore
from .internet import dispatcher as internet_dispatcher
from .internet.dispatcher import KNOWLEDGE_FETCH_SCHEMA, KNOWLEDGE_SEARCH_SCHEMA
from .internet.osint import record_out_of_scope
from .security_tools import http_recon, port_discovery

# Defined here (not in agent/browser/service.py, which imports playwright at module level) so
# `agent/loop.py` can reference this schema to build its tools=[...] list without needing
# playwright installed just to know the tool exists — only actually calling it needs playwright,
# and that import happens lazily inside _browser_fetch() below.
BROWSER_FETCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "browser_fetch",
        "description": (
            "Render a single in-scope URL in an isolated, non-root, ephemeral headless browser "
            "(full JS execution) and return the post-render HTML and title — for JS-heavy/SPA "
            "content http_recon's plain HTTP fetch can't see. Every subresource the page loads "
            "is still validated against the current RoE the same way http_recon validates its "
            "target; an out-of-scope subresource is blocked, not fetched. A materially heavier "
            "capability than http_recon (full JS execution against the target), so it has its "
            "own browser_recon action class — denied unless the current RoE explicitly allows it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
            },
            "required": ["url"],
        },
    },
}

OSINT_RECORD_SCHEMA = {
    "type": "function",
    "function": {
        "name": "osint_record",
        "description": (
            "Record something noticed incidentally while working in-scope — a subdomain "
            "mentioned in a page, an email address in response content, a linked third-party "
            "service — as an out-of-scope observation. Never use this to actively probe or "
            "fetch the thing you noticed; it is a filing cabinet for incidental discoveries, "
            "not a discovery tool. Not broker-mediated (makes no network call of its own, so "
            "nothing to gate)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "observation_type": {
                    "type": "string",
                    "description": "e.g. 'subdomain', 'email', 'third_party_service'",
                },
                "content": {"type": "string", "description": "What you observed, verbatim."},
            },
            "required": ["observation_type", "content"],
        },
    },
}

# The autonomous pipeline driver (agent/pipeline/autonomous_driver.py) needs the model to
# persist structured state itself during ANALYSIS/VALIDATION — a human reading prose and
# hand-calling EngagementStore.create_hypothesis()/FindingsStore.add() (what a real end-to-end
# run required before these existed, 2026-09-01) doesn't scale to an unattended run. All three
# below are, like osint_record, not broker-mediated: no network call of their own, just a
# structured engagement-state write.
RECORD_HYPOTHESIS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "record_hypothesis",
        "description": (
            "Record a testable hypothesis formed from the recorded observations (ANALYSIS "
            "phase). Persists it to engagement state as 'open', ready for a VALIDATION task to "
            "pick up — a hypothesis only described in your reply text, never recorded here, is "
            "invisible to the rest of the pipeline."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short name for the hypothesis."},
                "description": {
                    "type": "string",
                    "description": "What you'd need to observe to confirm or refute it, and why "
                    "the existing observations suggest it — reference the actual data, don't "
                    "invent supporting detail that wasn't recorded.",
                },
                "priority": {"type": "string", "enum": ["low", "medium", "high"]},
            },
            "required": ["title", "description"],
        },
    },
}

UPDATE_HYPOTHESIS_STATUS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_hypothesis_status",
        "description": (
            "Record the outcome of testing a hypothesis (VALIDATION phase) — 'confirmed' or "
            "'refuted' only after you actually tested it with a tool this phase, never on "
            "inference alone. A refuted hypothesis is a real, complete result; recording it is "
            "not optional just because the answer was negative."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "hypothesis_id": {"type": "string"},
                "status": {"type": "string", "enum": list(HYPOTHESIS_STATUSES)},
                "evidence": {
                    "type": "string",
                    "description": "The specific request/response or observation that supports "
                    "this verdict — not a paraphrase.",
                },
            },
            "required": ["hypothesis_id", "status", "evidence"],
        },
    },
}

RECORD_FINDING_SCHEMA = {
    "type": "function",
    "function": {
        "name": "record_finding",
        "description": (
            "Record a finding for the report, after a hypothesis has been tested (VALIDATION "
            "phase). severity/confidence/status must reflect only what was actually "
            "demonstrated — default confidence/status to 'needs_validation' unless you directly "
            "confirmed it with a tool this phase, matching this project's own reporting "
            "discipline (never self-promote to 'confirmed' without verified evidence)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "severity": {"type": "string", "enum": list(VALID_SEVERITIES)},
                "target": {"type": "string"},
                "description": {"type": "string"},
                "remediation": {"type": "string"},
                "demonstrated_impact": {
                    "type": "string",
                    "description": "What was actually shown to work, not theorized.",
                },
                "confidence": {"type": "string", "enum": list(VALID_CONFIDENCE)},
                "status": {"type": "string", "enum": list(VALID_STATUS)},
                "hypothesis_id": {
                    "type": "string",
                    "description": "The hypothesis this finding validates, if any.",
                },
            },
            "required": ["title", "severity", "target", "description", "remediation"],
        },
    },
}


def _browser_fetch(policy, url: str) -> dict:
    # Imported lazily: agent/browser/service.py imports playwright, an optional dependency
    # (requirements-phase6.txt) not every deployment of this server needs installed if it
    # never calls this specific tool.
    from .browser.service import browser_fetch

    return browser_fetch(policy, url)

server = MCPServer("localai-phase3-security-tools")

_broker: Broker
_session_id: str
_device_id: str
_engagement_id: str = "lab-default"


def _channel_result_to_dict(result) -> dict:
    return {
        "channel": result.channel,
        "ok": result.ok,
        "content": result.content,
        "provenance": result.provenance,
        "scan_verdict": result.scan_verdict,
    }


@server.tool(name="http_recon", description=http_recon.SCHEMA["function"]["description"])
def http_recon_tool(url: str, verify_cert: bool = True) -> dict:
    req = ActionRequest(
        tool="http_recon",
        arguments={"url": url, "verify_cert": verify_cert},
        session_id=_session_id,
        device_id=_device_id,
        engagement_id=_engagement_id,
    )
    resp = _broker.dispatch(
        req, executor=lambda pol, args: http_recon.run(args["url"], pol, args["verify_cert"])
    )
    return resp.to_dict()


@server.tool(name="port_discovery", description=port_discovery.SCHEMA["function"]["description"])
def port_discovery_tool(host: str, ports: list[int]) -> dict:
    req = ActionRequest(
        tool="port_discovery",
        arguments={"host": host, "ports": ports},
        session_id=_session_id,
        device_id=_device_id,
        engagement_id=_engagement_id,
    )
    resp = _broker.dispatch(
        req, executor=lambda pol, args: port_discovery.run(args["host"], args["ports"], pol)
    )
    return resp.to_dict()


@server.tool(name="knowledge_search", description=KNOWLEDGE_SEARCH_SCHEMA["function"]["description"])
def knowledge_search_tool(query: str) -> dict:
    req = ActionRequest(
        tool="knowledge_search",
        arguments={"query": query},
        session_id=_session_id,
        device_id=_device_id,
        engagement_id=_engagement_id,
    )
    resp = _broker.dispatch(
        req,
        executor=lambda pol, args: _channel_result_to_dict(
            internet_dispatcher.knowledge_search(_session_id, args["query"])
        ),
    )
    return resp.to_dict()


@server.tool(name="knowledge_fetch", description=KNOWLEDGE_FETCH_SCHEMA["function"]["description"])
def knowledge_fetch_tool(url: str) -> dict:
    req = ActionRequest(
        tool="knowledge_fetch",
        arguments={"url": url},
        session_id=_session_id,
        device_id=_device_id,
        engagement_id=_engagement_id,
    )
    resp = _broker.dispatch(
        req,
        executor=lambda pol, args: _channel_result_to_dict(
            internet_dispatcher.knowledge_fetch(_session_id, args["url"])
        ),
    )
    return resp.to_dict()


def _engagement_store_or_error(caller_tool: str) -> tuple[EngagementStore | None, dict | None]:
    """Shared by osint_record and the three record_*/update_hypothesis_status tools below — all
    write to engagement state, all need the same real-store-exists check osint_record already
    had. Returns (store, None) on success, (None, error_dict) otherwise."""
    engagement_dir = config.ENGAGEMENTS_ROOT / _engagement_id
    if not (engagement_dir / "state.db").exists():
        return None, {
            "ok": False,
            "detail": (
                f"no M5.2 engagement state store for engagement_id={_engagement_id!r} — nothing "
                f"was recorded. {caller_tool} needs an engagement created via "
                "agent.engagement.intake (engagements/<id>/), not the legacy single-engagement "
                "default."
            ),
        }
    return EngagementStore(engagement_dir), None


@server.tool(name="osint_record", description=OSINT_RECORD_SCHEMA["function"]["description"])
def osint_record_tool(observation_type: str, content: str) -> dict:
    store, error = _engagement_store_or_error("osint_record")
    if error is not None:
        return error
    observation_id = record_out_of_scope(store, observation_type, content, source=f"model:{_session_id}")
    return {"ok": True, "observation_id": observation_id}


@server.tool(name="record_hypothesis", description=RECORD_HYPOTHESIS_SCHEMA["function"]["description"])
def record_hypothesis_tool(title: str, description: str, priority: str = "medium") -> dict:
    store, error = _engagement_store_or_error("record_hypothesis")
    if error is not None:
        return error
    hypothesis_id = store.create_hypothesis(title, description, priority=priority)
    return {"ok": True, "hypothesis_id": hypothesis_id}


@server.tool(
    name="update_hypothesis_status",
    description=UPDATE_HYPOTHESIS_STATUS_SCHEMA["function"]["description"],
)
def update_hypothesis_status_tool(hypothesis_id: str, status: str, evidence: str) -> dict:
    store, error = _engagement_store_or_error("update_hypothesis_status")
    if error is not None:
        return error
    try:
        current = store.get_hypothesis(hypothesis_id)
    except Exception as e:
        return {"ok": False, "detail": f"hypothesis {hypothesis_id!r} not found: {e}"}
    add_for = evidence if status == "confirmed" else None
    add_against = evidence if status == "refuted" else None
    new_version = store.update_hypothesis(
        hypothesis_id, expected_version=current["version"], status=status,
        add_evidence_for=add_for, add_evidence_against=add_against,
    )
    return {"ok": True, "hypothesis_id": hypothesis_id, "version": new_version}


@server.tool(name="record_finding", description=RECORD_FINDING_SCHEMA["function"]["description"])
def record_finding_tool(
    title: str, severity: str, target: str, description: str, remediation: str,
    demonstrated_impact: str = "", confidence: str = "needs_validation",
    status: str = "needs_validation", hypothesis_id: str = "",
) -> dict:
    store, error = _engagement_store_or_error("record_finding")
    if error is not None:
        return error
    try:
        finding = Finding(
            title=title, severity=severity, target=target, description=description,
            remediation=remediation, tool="model", session_id=_session_id,
            engagement_id=_engagement_id, confidence=confidence, status=status,
            demonstrated_impact=demonstrated_impact or None,
            verifier=f"model:{_session_id}",
        )
    except ValueError as e:
        return {"ok": False, "detail": f"invalid finding fields: {e}"}
    FindingsStore(_engagement_id).add(finding)
    # Links immediately, not deferred to generate_report — a finding recorded mid-VALIDATION
    # should already be visible to close_engagement()'s accounting if CLOSEOUT is reached before
    # generate_report happens to re-link it (generate_report's own link_finding call is
    # idempotent — see agent/pipeline/executor.py — so this doesn't double-count).
    store.link_finding(finding.finding_id, status="draft")
    result = {"ok": True, "finding_id": finding.finding_id}
    if hypothesis_id:
        # Best-effort: the finding is already recorded either way, so a bad/stale hypothesis_id
        # shouldn't fail the whole call — but the caller should still be told, not left to
        # assume the link happened silently.
        try:
            current = store.get_hypothesis(hypothesis_id)
            store.update_hypothesis(
                hypothesis_id, expected_version=current["version"],
                add_evidence_for=f"finding {finding.finding_id}: {title}",
            )
        except Exception as e:
            result["hypothesis_link_warning"] = f"finding recorded, but not linked to hypothesis_id {hypothesis_id!r}: {e}"
    return result


@server.tool(name="browser_fetch", description=BROWSER_FETCH_SCHEMA["function"]["description"])
def browser_fetch_tool(url: str) -> dict:
    req = ActionRequest(
        tool="browser_fetch",
        arguments={"url": url},
        session_id=_session_id,
        device_id=_device_id,
        engagement_id=_engagement_id,
    )
    resp = _broker.dispatch(req, executor=lambda pol, args: _browser_fetch(pol, args["url"]))
    return resp.to_dict()


def resolve_engagement_dir(engagement_id: str) -> object:
    """Found live (2026-09-01, first real end-to-end pipeline run): main() previously always
    constructed Broker() with no engagement_dir at all, so it silently enforced the legacy
    default engagement/ RoE/scope/deny regardless of --engagement-id — a named engagement
    created via agent.engagement.intake (whose own CLI tells the caller to use
    Broker(engagement_dir=...)) was completely decorative for actual policy enforcement; only
    EngagementStore (pipeline state/phase) and audit labeling ever looked at --engagement-id.
    Falls back to the legacy config.ENGAGEMENT_DIR when no named engagement directory exists
    (roe.json absent) — preserves every existing default-CLI/test behavior that never created a
    named engagement at all. A standalone function (not inlined in main()) so this resolution
    logic is testable without spawning the real stdio server / parsing real argv.
    """
    named_engagement_dir = config.ENGAGEMENTS_ROOT / engagement_id
    if (named_engagement_dir / "roe.json").exists():
        return named_engagement_dir
    return config.ENGAGEMENT_DIR


def main() -> None:
    global _broker, _session_id, _device_id, _engagement_id
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--engagement-id", default="lab-default")
    parser.add_argument(
        "--use-approval-queue", action="store_true",
        help="Phase 6: block on agent.broker.approval_queue instead of this subprocess's own "
        "stdin (which the MCP protocol already owns) for approval-required dispatches — for a "
        "UI or anything else resolving requests out-of-band.",
    )
    args = parser.parse_args()

    _session_id = args.session_id
    _device_id = args.device_id
    _engagement_id = args.engagement_id
    engagement_dir = resolve_engagement_dir(_engagement_id)
    _broker = Broker(engagement_dir=engagement_dir, use_approval_queue=args.use_approval_queue)
    server.run()


if __name__ == "__main__":
    main()
