"""Oxpecker control MCP — drive and OBSERVE the live Oxpecker agent end to end from an MCP client.

This is the complete bridge: Claude (Code/Desktop) does everything the web UI can, structurally,
by wrapping every operator-facing dev_server REST endpoint — so Claude can run an engagement from
start to finish without touching the UI. It both SEES the run (the §4.2 flow/worker snapshot, the
hypothesis graph, notebook, findings, pending approvals/consults, the agent's live browser frame)
and ACTS on it (chat, steer, launch/stop a pentest, approve or decline a held request, answer a
phase check-in, park/reopen a hypothesis, review a finding, create an engagement, drive the
browser). The UI is just another REST client over the same server, so this is equivalent to
driving the screen — minus the pixels.

Requires the dev_server running (it is what runs the model, the tools, the graph):
    python -m agent.web.dev_server --host 127.0.0.1 --port 7777 --provider openrouter --model <id>

Config:
  - OXPECKER_URL       the server base (default http://127.0.0.1:7777)
  - OXPECKER_API_KEY   the web-UI API key, if the server has one. If unset, the key is read from
                       agent/state/web_ui_api_key.txt next to this package, so a local install
                       needs no extra wiring.

Register with Claude Code (.mcp.json):

    "oxpecker": { "command": "python", "args": ["-m", "agent.oxpecker_control_mcp"],
                  "env": { "PYTHONUTF8": "1", "OXPECKER_URL": "http://127.0.0.1:7777" } }
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# FastMCP is the real server API in the released `mcp` 1.x (the old MCPServer path never existed).
from mcp.server import FastMCP

try:  # Image lets the browser-frame tool hand Claude an actual picture, not a base64 blob.
    from mcp.server.fastmcp import Image
except Exception:  # pragma: no cover - older mcp
    Image = None

server = FastMCP("oxpecker")
BASE = os.environ.get("OXPECKER_URL", "http://127.0.0.1:7777").rstrip("/")


def _api_key() -> str:
    k = os.environ.get("OXPECKER_API_KEY")
    if k:
        return k.strip()
    try:
        p = Path(__file__).resolve().parent / "state" / "web_ui_api_key.txt"
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""
    except Exception:
        return ""


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    key = _api_key()
    if key:
        h["X-API-Key"] = key  # the header static/index.html sends; see dev_server._key_supplied
    return h


def _read_err(e: urllib.error.HTTPError) -> dict:
    try:
        body = json.loads(e.read())
        detail = body.get("detail", body)
    except Exception:
        detail = str(e)
    return {"ok": False, "status": e.code, "error": detail}


def _get(path: str, timeout: int = 30):
    try:
        req = urllib.request.Request(BASE + path, headers=_headers(), method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return _read_err(e)
    except Exception as e:
        return _down_err(e)


def _send(path: str, method: str, body: dict | None = None, timeout: int = 30):
    try:
        data = json.dumps(body or {}).encode() if method in ("POST", "PUT", "PATCH") else None
        req = urllib.request.Request(BASE + path, data=data, headers=_headers(), method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {"ok": True}
    except urllib.error.HTTPError as e:
        return _read_err(e)
    except Exception as e:
        return _down_err(e)


def _post(path: str, body: dict | None = None, timeout: int = 30):
    return _send(path, "POST", body, timeout)


def _delete(path: str, timeout: int = 30):
    return _send(path, "DELETE", None, timeout)


def _down_err(e: Exception) -> dict:
    return {"ok": False, "error": f"cannot reach Oxpecker dev_server at {BASE} ({e}). "
                                  f"Start it: python -m agent.web.dev_server --port 7777"}


def _tool_calls_of(m: dict) -> list:
    return m.get("tool_calls") or (m.get("extra") or {}).get("tool_calls") or []


def _q(engagement_id: str) -> str:
    return urllib.parse.quote(engagement_id, safe="")


# The web UI's default grant (dev_server.DEFAULT_ACTION_CLASSES; test_action_classes pins the two
# in step). An engagement needs at least one class or the autonomous pipeline refuses to run, so a
# create that names none gets these, exactly as the UI's New-engagement form does.
_DEFAULT_ACTION_CLASSES = ["passive_recon", "active_web_request", "active_scan_light",
                           "browser_recon", "knowledge_search"]


# ── Health / sessions ────────────────────────────────────────────────────────
@server.tool(name="oxpecker_health",
             description="Check the live Oxpecker backend: model/provider, RAG doc count, active sessions.")
def oxpecker_health() -> dict:
    return _get("/api/health")


@server.tool(name="oxpecker_sessions", description="List Oxpecker sessions (id, title, message count, live).")
def oxpecker_sessions() -> dict:
    return {"sessions": _get("/api/sessions")}


@server.tool(name="oxpecker_session",
             description="Get one session's transcript — messages, tool calls, final answers (last `limit`).")
def oxpecker_session(session_id: str, limit: int = 60) -> dict:
    data = _get(f"/api/sessions/{session_id}")
    if isinstance(data, dict) and data.get("ok") is False:
        return data
    msgs = []
    for m in data.get("messages", [])[-limit:]:
        e = {"role": m.get("role"), "content": (m.get("content") or "")[:1200]}
        tc = _tool_calls_of(m)
        if tc:
            e["tool_calls"] = tc
        if m.get("tool_name") or (m.get("extra") or {}).get("tool_name"):
            e["tool_name"] = m.get("tool_name") or m["extra"]["tool_name"]
        msgs.append(e)
    return {"session_id": session_id, "messages": msgs, "total": len(data.get("messages", []))}


@server.tool(name="oxpecker_delete_session", description="Delete a session and its transcript.")
def oxpecker_delete_session(session_id: str) -> dict:
    return _delete(f"/api/sessions/{session_id}")


# ── Chat / drive a run ───────────────────────────────────────────────────────
@server.tool(name="oxpecker_ask",
             description="Send a message to the Oxpecker agent and wait for the reply. Pass session_id to "
                         "continue a conversation, or omit to start a new one. Returns the answer plus any "
                         "tools it ran.")
def oxpecker_ask(message: str, session_id: str = "", engagement_id: str = "lab-default",
                 security_tools: bool = True, timeout: int = 180) -> dict:
    try:
        if not session_id:
            created = _post("/api/sessions", {"use_security_tools": security_tools,
                                              "engagement_id": engagement_id})
            if created.get("ok") is False:
                return created
            session_id = created["session_id"]
        done = threading.Event()
        status = {"type": None, "error": None}

        def sse():
            try:
                key = _api_key()
                url = BASE + f"/api/sessions/{session_id}/events" + (f"?key={urllib.parse.quote(key)}" if key else "")
                resp = urllib.request.urlopen(url, timeout=timeout + 10)
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("data: "):
                        try:
                            evt = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue
                        if evt.get("type") in ("task_done", "task_error"):
                            status.update(type=evt["type"], error=evt.get("error"))
                            done.set()
                            return
            except Exception:
                done.set()

        threading.Thread(target=sse, daemon=True).start()
        time.sleep(0.3)
        sent = _post(f"/api/sessions/{session_id}/messages", {"content": message})
        if isinstance(sent, dict) and sent.get("ok") is False:
            return sent
        done.wait(timeout=timeout)
    except Exception as e:
        return _down_err(e)

    data = _get(f"/api/sessions/{session_id}")
    answer, tools = "", []
    for m in data.get("messages", []):
        for tc in _tool_calls_of(m):
            tools.append({"name": tc.get("name") or (tc.get("function") or {}).get("name"),
                          "arguments": tc.get("arguments") or (tc.get("function") or {}).get("arguments")})
        if m.get("role") == "assistant" and m.get("content") and not m["content"].startswith("(calling"):
            answer = m["content"]
    return {"session_id": session_id, "answer": answer, "tools_used": tools,
            "status": status["type"] or "timeout", "error": status["error"]}


@server.tool(name="oxpecker_steer",
             description="Steer a RUNNING task mid-flight: the instruction is picked up at the agent's next "
                         "step instead of queuing a new turn. Use it to redirect an autonomous run.")
def oxpecker_steer(session_id: str, message: str) -> dict:
    return _post(f"/api/sessions/{session_id}/steer", {"message": message})


@server.tool(name="oxpecker_set_stage",
             description="Set a guided session's pentest stage (RECON | ANALYSIS | VALIDATION | REPORT).")
def oxpecker_set_stage(session_id: str, stage: str) -> dict:
    return _post(f"/api/sessions/{session_id}/stage", {"stage": stage})


@server.tool(name="oxpecker_run_pentest",
             description="Launch the full autonomous pipeline (RECON→CLOSEOUT) against a target. tier is "
                         "low|medium|high (high = parallel waves + strategist + hypothesis graph). Returns a "
                         "session_id immediately — watch it with oxpecker_snapshot / oxpecker_session.")
def oxpecker_run_pentest(target: str, engagement_id: str = "lab-default", tier: str = "high",
                         user_message: str = "") -> dict:
    try:
        created = _post("/api/sessions", {"use_security_tools": True, "engagement_id": engagement_id})
        if created.get("ok") is False:
            return created
        sid = created["session_id"]
        msg = user_message or f"Penetration-test {target}. Find and verify vulnerabilities with evidence."
        started = _post(f"/api/sessions/{sid}/pipeline/start",
                        {"engagement_id": engagement_id, "profile": "web_api", "mode": "autonomous",
                         "user_message": msg, "tier": tier})
        if isinstance(started, dict) and started.get("ok") is False:
            return started
        return {"ok": True, "session_id": sid, "target": target, "tier": tier,
                "note": "running — poll oxpecker_snapshot(engagement_id) and oxpecker_session(session_id)"}
    except Exception as e:
        return _down_err(e)


@server.tool(name="oxpecker_stop", description="Stop a running autonomous engagement / agent turn.")
def oxpecker_stop(session_id: str) -> dict:
    r = _post(f"/api/sessions/{session_id}/pipeline/stop")
    if isinstance(r, dict) and r.get("ok") is False:
        r = _post(f"/api/sessions/{session_id}/autonomous/stop")
    return r


# ── Observe the live run (§4.2 read-model) ───────────────────────────────────
@server.tool(name="oxpecker_snapshot",
             description="The live flow/worker read-model for an engagement: the running wave and its "
                         "dispatched experiments, every worker (hypothesis, method, status, outcome, in-flight "
                         "tool), the in-flight call pipeline, the model roster (calls/tokens/cost), stored "
                         "artifacts and pending approvals. This is what the Flow view draws — the way to watch "
                         "a run without the UI.")
def oxpecker_snapshot(engagement_id: str = "lab-default", at: int = 0) -> dict:
    return _get(f"/api/engagements/{_q(engagement_id)}/snapshot" + (f"?at={at}" if at else ""))


@server.tool(name="oxpecker_approvals",
             description="Pending approvals — held tool calls awaiting a human yes/no. Resolve them with "
                         "oxpecker_approve, or an autonomous run blocks here. Pass session_id to scope.")
def oxpecker_approvals(session_id: str = "") -> dict:
    return {"approvals": _get("/api/approvals" + (f"?session_id={session_id}" if session_id else ""))}


@server.tool(name="oxpecker_consults",
             description="Pending phase check-ins — the agent asking the operator whether to continue. Answer "
                         "with oxpecker_answer_consult. Pass session_id to scope.")
def oxpecker_consults(session_id: str = "") -> dict:
    return {"consults": _get("/api/consults" + (f"?session_id={session_id}" if session_id else ""))}


@server.tool(name="oxpecker_events",
             description="A bounded tail of an engagement's §4.2 event stream (structure/verdicts, no raw tool "
                         "output): up to `limit` events, waiting up to `wait_s` for new ones. Use for a live "
                         "play-by-play; oxpecker_snapshot is the folded current state.")
def oxpecker_events(engagement_id: str = "lab-default", after: int = 0, limit: int = 50,
                    wait_s: int = 6) -> dict:
    out: list = []
    try:
        key = _api_key()
        url = (BASE + f"/api/engagements/{_q(engagement_id)}/events"
               + f"?after={after}" + (f"&key={urllib.parse.quote(key)}" if key else ""))
        resp = urllib.request.urlopen(url, timeout=wait_s + 5)
        deadline = time.time() + wait_s
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data: "):
                try:
                    out.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    pass
            if len(out) >= limit or time.time() > deadline:
                break
    except Exception as e:
        return _down_err(e)
    last = out[-1]["seq"] if out and isinstance(out[-1], dict) and "seq" in out[-1] else after
    return {"events": out[:limit], "last_seq": last}


@server.tool(name="oxpecker_engagements", description="List engagements with their in-scope targets.")
def oxpecker_engagements() -> dict:
    return {"engagements": _get("/api/engagements")}


@server.tool(name="oxpecker_findings",
             description="Findings recorded for an engagement (title, severity, target, description, reviewed).")
def oxpecker_findings(engagement_id: str = "lab-default") -> dict:
    return _get(f"/api/engagements/{_q(engagement_id)}/findings")


@server.tool(name="oxpecker_graph",
             description="The hypothesis graph (nodes + edges) for an engagement — the agent's reasoning tree.")
def oxpecker_graph(engagement_id: str = "lab-default") -> dict:
    return _get(f"/api/engagements/{_q(engagement_id)}/hypothesis-graph")


@server.tool(name="oxpecker_graph_node",
             description="One hypothesis node in full: claim, status/verdict, confidence, priority, attempts.")
def oxpecker_graph_node(engagement_id: str, ordinal: int) -> dict:
    return _get(f"/api/engagements/{_q(engagement_id)}/hypothesis-graph/nodes/{ordinal}")


@server.tool(name="oxpecker_notebook", description="The working notebook (notes) for an engagement.")
def oxpecker_notebook(engagement_id: str = "lab-default") -> dict:
    return _get(f"/api/engagements/{_q(engagement_id)}/notebook")


# ── Act on the run (the operator decisions) ──────────────────────────────────
@server.tool(name="oxpecker_approve",
             description="Resolve a held approval by request_id: approved=True runs the tool call, False "
                         "declines it (the action does NOT run). Unblocks an autonomous run.")
def oxpecker_approve(request_id: str, approved: bool, resolved_by: str = "mcp") -> dict:
    return _post(f"/api/approvals/{request_id}/resolve", {"approved": approved, "resolved_by": resolved_by})


@server.tool(name="oxpecker_answer_consult",
             description="Answer a phase check-in by request_id. 'continue' (or 'yes'/'ok') proceeds; anything "
                         "else is taken as an instruction / a stop.")
def oxpecker_answer_consult(request_id: str, answer: str, resolved_by: str = "mcp") -> dict:
    return _post(f"/api/consults/{request_id}/resolve", {"answer": answer, "resolved_by": resolved_by})


@server.tool(name="oxpecker_graph_action",
             description="Operator action on a hypothesis node: action='park' (set aside, with a reason), "
                         "'reopen' (un-park), or 'note' (attach a note via `text`).")
def oxpecker_graph_action(engagement_id: str, ordinal: int, action: str, reason: str = "",
                          text: str = "") -> dict:
    return _post(f"/api/engagements/{_q(engagement_id)}/hypothesis-graph/nodes/{ordinal}/operator-action",
                 {"action": action, "reason": reason, "text": text})


@server.tool(name="oxpecker_resolve_note",
             description="Mark a notebook note resolved or unresolved: action='resolve' | 'unresolve'.")
def oxpecker_resolve_note(engagement_id: str, ordinal: int, action: str = "resolve", reason: str = "") -> dict:
    return _post(f"/api/engagements/{_q(engagement_id)}/notebook/notes/{ordinal}/resolve",
                 {"action": action, "reason": reason})


@server.tool(name="oxpecker_review_finding",
             description="Mark a finding human-reviewed (clears the 'not reviewed' banner).")
def oxpecker_review_finding(engagement_id: str, finding_id: str, reviewed_by: str = "mcp") -> dict:
    return _post(f"/api/engagements/{_q(engagement_id)}/findings/{finding_id}/review",
                 {"reviewed_by": reviewed_by})


@server.tool(name="oxpecker_delete_finding", description="Delete a finding from an engagement.")
def oxpecker_delete_finding(engagement_id: str, finding_id: str) -> dict:
    return _delete(f"/api/engagements/{_q(engagement_id)}/findings/{finding_id}")


@server.tool(name="oxpecker_create_engagement",
             description="Create (or replace) an engagement/scope. allow_targets is required (hosts, domains "
                         "or CIDRs in scope); deny_targets are carved out. allowed_action_classes defaults to "
                         "the server's safe defaults when empty.")
def oxpecker_create_engagement(engagement_id: str, allow_targets: list[str], deny_targets: list[str] | None = None,
                               description: str = "", allowed_action_classes: list[str] | None = None,
                               authorized_by: str = "mcp") -> dict:
    body = {"engagement_id": engagement_id, "allow_targets": allow_targets,
            "deny_targets": deny_targets or [], "description": description or (allow_targets[0] if allow_targets else ""),
            "allowed_action_classes": allowed_action_classes or list(_DEFAULT_ACTION_CLASSES),
            "authorized_by": authorized_by}
    return _post("/api/engagements", body)


@server.tool(name="oxpecker_delete_engagement", description="Delete an engagement and its scope.")
def oxpecker_delete_engagement(engagement_id: str) -> dict:
    return _delete(f"/api/engagements/{_q(engagement_id)}")


# ── The agent's live browser ─────────────────────────────────────────────────
@server.tool(name="oxpecker_browser_start", description="Start the engagement's live headless browser.")
def oxpecker_browser_start(engagement_id: str = "lab-default") -> dict:
    return _post(f"/api/engagements/{_q(engagement_id)}/browser/start")


@server.tool(name="oxpecker_browser_stop", description="Stop the engagement's live browser.")
def oxpecker_browser_stop(engagement_id: str = "lab-default") -> dict:
    return _post(f"/api/engagements/{_q(engagement_id)}/browser/stop")


@server.tool(name="oxpecker_browser_navigate",
             description="Navigate the live browser to a URL (operator navigation; still scope-gated on the "
                         "server, which refuses an out-of-scope URL).")
def oxpecker_browser_navigate(engagement_id: str, url: str) -> dict:
    return _post(f"/api/engagements/{_q(engagement_id)}/browser/navigate", {"url": url})


@server.tool(name="oxpecker_browser_click",
             description="Click the live browser at viewport coordinates (x, y). button = left|right|middle.")
def oxpecker_browser_click(engagement_id: str, x: int, y: int, button: str = "left") -> dict:
    return _post(f"/api/engagements/{_q(engagement_id)}/browser/input",
                 {"action": "click", "x": x, "y": y, "button": button})


@server.tool(name="oxpecker_browser_frame",
             description="Fetch the latest frame of the agent's live browser as an image, so you can SEE what "
                         "it is doing. Pass `since` (the last seq you saw) to get only a newer frame. Returns "
                         "the image, or a note when nothing is open / no newer frame yet.")
def oxpecker_browser_frame(engagement_id: str = "lab-default", since: int = 0):
    try:
        req = urllib.request.Request(
            BASE + f"/api/engagements/{_q(engagement_id)}/browser/frame?since={since}",
            headers=_headers(), method="GET")
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status == 204:
                return {"ok": True, "frame": None, "note": "no newer frame since that seq"}
            data = r.read()
            seq = r.headers.get("X-Frame-Seq")
    except urllib.error.HTTPError as e:
        return _read_err(e)
    except Exception as e:
        return _down_err(e)
    if Image is not None:
        # Hand Claude the actual picture; the seq rides in the note via a follow-up call if needed.
        return Image(data=data, format="jpeg")
    import base64
    return {"ok": True, "seq": seq, "content_type": "image/jpeg",
            "image_base64": base64.b64encode(data).decode()}


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
