"""Oxpecker control MCP — drive and observe the LIVE Oxpecker agent from an MCP client.

This is the bridge: Claude (Code/Desktop) talks to the running Oxpecker (its local 4B brain,
tools, RAG, hypothesis graph) over MCP by wrapping dev_server's REST API. Use it to chat with
the agent, launch pentests, and pull back sessions/findings/graph/notebook for review.

Requires the dev_server running (it is the thing that runs the 4B):
    py agent/web/dev_server.py --port 7777
Override its URL with OXPECKER_URL. Register with Claude Code (.mcp.json):

    "oxpecker": { "command": "py", "args": ["-m", "agent.oxpecker_control_mcp"],
                  "env": { "PYTHONUTF8": "1" } }
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
import urllib.error

from mcp.server.mcpserver import MCPServer

server = MCPServer("oxpecker")
BASE = os.environ.get("OXPECKER_URL", "http://127.0.0.1:7777").rstrip("/")


def _get(path: str, timeout: int = 30):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read())


def _post(path: str, body: dict | None = None, timeout: int = 30):
    req = urllib.request.Request(BASE + path, data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _down_err(e: Exception) -> dict:
    return {"ok": False, "error": f"cannot reach Oxpecker dev_server at {BASE} ({e}). "
                                  f"Start it: py agent/web/dev_server.py --port 7777"}


def _tool_calls_of(m: dict) -> list:
    return m.get("tool_calls") or (m.get("extra") or {}).get("tool_calls") or []


# ── Health / sessions ────────────────────────────────────────────────────────
@server.tool(name="oxpecker_health",
             description="Check the live Oxpecker backend: model loaded, RAG doc count, active sessions.")
def oxpecker_health() -> dict:
    try:
        return _get("/api/health")
    except Exception as e:
        return _down_err(e)


@server.tool(name="oxpecker_sessions", description="List Oxpecker sessions (id, title, message count).")
def oxpecker_sessions() -> dict:
    try:
        return {"sessions": _get("/api/sessions")}
    except Exception as e:
        return _down_err(e)


@server.tool(name="oxpecker_session",
             description="Get one session's full transcript — messages, tool calls, final answers.")
def oxpecker_session(session_id: str, limit: int = 60) -> dict:
    try:
        data = _get(f"/api/sessions/{session_id}")
    except Exception as e:
        return _down_err(e)
    msgs = []
    for m in data.get("messages", [])[-limit:]:
        e = {"role": m.get("role"), "content": (m.get("content") or "")[:1200]}
        tc = _tool_calls_of(m)
        if tc:
            e["tool_calls"] = tc
        if m.get("tool_name"):
            e["tool_name"] = m["tool_name"]
        msgs.append(e)
    return {"session_id": session_id, "messages": msgs,
            "total": len(data.get("messages", []))}


# ── Chat with the agent ──────────────────────────────────────────────────────
@server.tool(name="oxpecker_ask",
             description="Send a message to the Oxpecker agent (its local 4B) and wait for the reply. "
                         "Pass session_id to continue a conversation, or omit to start a new one. "
                         "Returns the agent's answer plus any tools it ran.")
def oxpecker_ask(message: str, session_id: str = "", engagement_id: str = "lab-default",
                 security_tools: bool = True, timeout: int = 150) -> dict:
    try:
        if not session_id:
            session_id = _post("/api/sessions", {"use_security_tools": security_tools,
                                                 "engagement_id": engagement_id})["session_id"]
        # Watch SSE so we know when the turn finishes.
        done = threading.Event()
        status = {"type": None, "error": None}

        def sse():
            try:
                resp = urllib.request.urlopen(BASE + f"/api/sessions/{session_id}/events", timeout=timeout + 10)
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

        t = threading.Thread(target=sse, daemon=True)
        t.start()
        time.sleep(0.3)
        _post(f"/api/sessions/{session_id}/messages", {"content": message})
        done.wait(timeout=timeout)
    except Exception as e:
        return _down_err(e)

    data = _get(f"/api/sessions/{session_id}")
    msgs = data.get("messages", [])
    answer, tools = "", []
    for m in msgs:
        for tc in _tool_calls_of(m):
            tools.append({"name": tc.get("name"), "arguments": tc.get("arguments")})
        if m.get("role") == "assistant" and m.get("content") and not m["content"].startswith("(calling"):
            answer = m["content"]
    return {"session_id": session_id, "answer": answer, "tools_used": tools,
            "status": status["type"] or "timeout", "error": status["error"]}


@server.tool(name="oxpecker_run_pentest",
             description="Launch an autonomous (or consult) pentest engagement against a target URL/host. "
                         "Returns a session_id immediately — poll oxpecker_session / oxpecker_findings to "
                         "watch it (a full run takes a few minutes on the local 4B).")
def oxpecker_run_pentest(target: str, mode: str = "autonomous",
                         engagement_id: str = "lab-default") -> dict:
    if mode not in ("autonomous", "consult"):
        return {"ok": False, "error": "mode must be 'autonomous' or 'consult'"}
    try:
        sid = _post("/api/sessions", {"use_security_tools": True, "engagement_id": engagement_id})["session_id"]
        _post(f"/api/sessions/{sid}/autonomous/start",
              {"engagement_id": engagement_id, "mode": mode,
               "user_message": f"Penetration-test {target}. Find and verify vulnerabilities with evidence."})
        return {"ok": True, "session_id": sid, "target": target, "mode": mode,
                "note": "running — poll oxpecker_session(session_id) and oxpecker_findings()"}
    except Exception as e:
        return _down_err(e)


@server.tool(name="oxpecker_stop",
             description="Stop a running autonomous engagement / agent turn for a session.")
def oxpecker_stop(session_id: str) -> dict:
    try:
        return _post(f"/api/sessions/{session_id}/autonomous/stop")
    except Exception as e:
        return _down_err(e)


# ── Observe engagement state ─────────────────────────────────────────────────
@server.tool(name="oxpecker_engagements", description="List engagements with their in-scope targets.")
def oxpecker_engagements() -> dict:
    try:
        return {"engagements": _get("/api/engagements")}
    except Exception as e:
        return _down_err(e)


@server.tool(name="oxpecker_findings",
             description="Get the findings recorded for an engagement (title, severity, target, description).")
def oxpecker_findings(engagement_id: str = "lab-default") -> dict:
    try:
        return _get(f"/api/engagements/{engagement_id}/findings")
    except Exception as e:
        return _down_err(e)


@server.tool(name="oxpecker_graph",
             description="Get the hypothesis graph (nodes + edges) for an engagement — the agent's reasoning tree.")
def oxpecker_graph(engagement_id: str = "lab-default") -> dict:
    try:
        return _get(f"/api/engagements/{engagement_id}/hypothesis-graph")
    except Exception as e:
        return _down_err(e)


@server.tool(name="oxpecker_notebook", description="Get the working notebook (notes) for an engagement.")
def oxpecker_notebook(engagement_id: str = "lab-default") -> dict:
    try:
        return _get(f"/api/engagements/{engagement_id}/notebook")
    except Exception as e:
        return _down_err(e)


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
