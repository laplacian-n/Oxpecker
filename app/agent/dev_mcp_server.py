"""Development MCP server for debugging, testing, and managing the Oxpecker pentest AI agent.

Exposes tools for: engagement management, session inspection, hypothesis graph queries,
notebook management, knowledge RAG testing, findings viewer, audit log, AI activity
tracing, health checks, and test running.

Usage — add to Claude Code's MCP config (~/.claude.json or project .claude/settings.json):

    {
        "mcpServers": {
            "oxpecker-dev": {
                "command": "python3",
                "args": ["-m", "agent.dev_mcp_server"],
                "cwd": "/path/to/localAI"
            }
        }
    }
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# `mcp.server.mcpserver.MCPServer` is not a path any released `mcp` 1.x provides, so this
# module could not be imported at all. FastMCP is the real API and takes exactly what this
# file already passes: FastMCP(name), .tool(name=, description=), .run(). Same fix as
# security_mcp_server.py, which carries the longer explanation; these three were left
# behind when it was made, and nothing noticed because nothing imports them.
from mcp.server import FastMCP

from . import config
from .engagement.store import EngagementStore

server = FastMCP("oxpecker-dev-tools")


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _engagement_dir(engagement_id: str) -> Path:
    return config.ENGAGEMENTS_ROOT / engagement_id


def _read_jsonl(path: Path, limit: int = 200) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    return out


def _sqlite_rows(db_path: Path, sql: str, params: tuple = ()) -> list[dict]:
    if not db_path.exists():
        return []
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def _file_age(path: Path) -> str:
    if not path.exists():
        return "missing"
    ts = path.stat().st_mtime
    ago = time.time() - ts
    if ago < 60:
        return f"{int(ago)}s ago"
    if ago < 3600:
        return f"{int(ago / 60)}m ago"
    if ago < 86400:
        return f"{int(ago / 3600)}h ago"
    return f"{int(ago / 86400)}d ago"


# ============================================================================
#  ENGAGEMENT MANAGEMENT
# ============================================================================

@server.tool(
    name="dev_list_engagements",
    description="List all engagements with their phase, hypothesis count, and last activity.",
)
def list_engagements() -> dict:
    root = config.ENGAGEMENTS_ROOT
    if not root.exists():
        return {"engagements": [], "root": str(root)}
    results = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        info: dict = {"id": d.name, "path": str(d)}
        scope = d / "scope.txt"
        if scope.exists():
            info["scope_targets"] = [l.strip() for l in scope.read_text().splitlines() if l.strip()]
        state_db = d / "state.db"
        if state_db.exists():
            rows = _sqlite_rows(state_db, "SELECT current_phase FROM phase_state LIMIT 1")
            if rows:
                info["phase"] = rows[0]["current_phase"]
        graph_db = d / "hypothesis_graph.db"
        if graph_db.exists():
            rows = _sqlite_rows(graph_db, "SELECT count(*) AS n FROM hypotheses")
            info["hypothesis_count"] = rows[0]["n"] if rows else 0
        notebook_db = d / "notebook.db"
        if notebook_db.exists():
            rows = _sqlite_rows(notebook_db, "SELECT count(*) AS n FROM notes")
            info["note_count"] = rows[0]["n"] if rows else 0
        info["last_modified"] = _file_age(d)
        results.append(info)
    return {"engagements": results, "root": str(root)}


@server.tool(
    name="dev_create_engagement",
    description="Create a new engagement with scope targets and optional RoE fields.",
)
def create_engagement(
    engagement_id: str,
    scope_targets: list[str],
    description: str = "",
    authorized_by: str = "dev-operator",
    allowed_actions: list[str] | None = None,
    deny_targets: list[str] | None = None,
) -> dict:
    eng_dir = _engagement_dir(engagement_id)
    if eng_dir.exists():
        return {"ok": False, "error": f"engagement '{engagement_id}' already exists"}
    eng_dir.mkdir(parents=True)
    (eng_dir / "scope.txt").write_text("\n".join(scope_targets) + "\n")
    if deny_targets:
        (eng_dir / "deny.txt").write_text("\n".join(deny_targets) + "\n")
    roe = {
        "engagement_id": engagement_id,
        "description": description,
        "allow_targets": scope_targets,
        "deny_targets": deny_targets or [],
        "allowed_action_classes": allowed_actions or ["passive_recon", "active_recon"],
        "authorized_by": authorized_by,
        "operator_sign_off": True,
        "valid_from": datetime.now(timezone.utc).isoformat(),
        "valid_until": "2099-12-31T23:59:59Z",
        "created_at": time.time(),
    }
    (eng_dir / "roe.json").write_text(json.dumps(roe, indent=2) + "\n")
    return {"ok": True, "engagement_id": engagement_id, "path": str(eng_dir)}


@server.tool(
    name="dev_delete_engagement",
    description="Delete an engagement directory and all its data. DESTRUCTIVE.",
)
def delete_engagement(engagement_id: str, confirm: bool = False) -> dict:
    if not confirm:
        return {"ok": False, "error": "pass confirm=true to delete"}
    eng_dir = _engagement_dir(engagement_id)
    if not eng_dir.exists():
        return {"ok": False, "error": f"engagement '{engagement_id}' not found"}
    shutil.rmtree(eng_dir)
    return {"ok": True, "deleted": engagement_id}


@server.tool(
    name="dev_engagement_detail",
    description="Get full details of an engagement: scope, RoE, phase, assets, services, observations, findings.",
)
def engagement_detail(engagement_id: str) -> dict:
    eng_dir = _engagement_dir(engagement_id)
    if not eng_dir.exists():
        return {"ok": False, "error": f"engagement '{engagement_id}' not found"}
    info: dict = {"engagement_id": engagement_id}
    scope = eng_dir / "scope.txt"
    if scope.exists():
        info["scope"] = [l.strip() for l in scope.read_text().splitlines() if l.strip()]
    deny = eng_dir / "deny.txt"
    if deny.exists():
        info["deny"] = [l.strip() for l in deny.read_text().splitlines() if l.strip()]
    roe = eng_dir / "roe.json"
    if roe.exists():
        info["roe"] = json.loads(roe.read_text())
    state_db = eng_dir / "state.db"
    if state_db.exists():
        store = EngagementStore(eng_dir)
        info["phase"] = store.get_phase()
        info["assets"] = store.list_assets()
        info["services"] = store.list_services()
        info["endpoints"] = store.list_endpoints()
        info["observations"] = store.list_observations()[:50]
        info["findings"] = store.list_findings_lifecycle()
        info["coverage"] = store.coverage_summary()
        info["phase_history"] = store.phase_history()
    info["files"] = [f.name for f in eng_dir.iterdir()]
    return info


# ============================================================================
#  SESSION MANAGEMENT
# ============================================================================

@server.tool(
    name="dev_list_sessions",
    description="List all session files with size, message count, and last modified time.",
)
def list_sessions(limit: int = 50) -> dict:
    sessions_dir = config.SESSIONS_DIR
    if not sessions_dir.exists():
        return {"sessions": [], "dir": str(sessions_dir)}
    files = sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    results = []
    for f in files[:limit]:
        lines = f.read_text().splitlines()
        msg_count = len([l for l in lines if l.strip()])
        roles: dict[str, int] = {}
        for line in lines:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                role = r.get("role", "?")
                roles[role] = roles.get(role, 0) + 1
            except json.JSONDecodeError:
                pass
        results.append({
            "session_id": f.stem,
            "messages": msg_count,
            "roles": roles,
            "size_kb": round(f.stat().st_size / 1024, 1),
            "last_modified": _file_age(f),
        })
    return {"sessions": results, "dir": str(sessions_dir)}


@server.tool(
    name="dev_session_inspect",
    description=(
        "View full conversation history of a session — messages, tool calls, reasoning content, "
        "timestamps. The primary debugging tool for understanding what the AI did and why."
    ),
)
def session_inspect(session_id: str, limit: int = 100, offset: int = 0) -> dict:
    path = config.SESSIONS_DIR / f"{session_id}.jsonl"
    if not path.exists():
        return {"ok": False, "error": f"session '{session_id}' not found"}
    records = _read_jsonl(path, limit=offset + limit)
    records = records[offset:]
    messages = []
    for r in records:
        msg: dict = {
            "role": r.get("role"),
            "content": r.get("content", "")[:2000],
            "timestamp": r.get("timestamp"),
            "message_id": r.get("message_id"),
        }
        if r.get("reasoning_content"):
            msg["reasoning_content"] = r["reasoning_content"][:1000]
        if r.get("tool_calls"):
            msg["tool_calls"] = r["tool_calls"]
        if r.get("tool_call_id"):
            msg["tool_call_id"] = r["tool_call_id"]
            msg["name"] = r.get("name")
        messages.append(msg)
    return {"session_id": session_id, "messages": messages, "total_in_file": len(_read_jsonl(path, limit=9999))}


@server.tool(
    name="dev_session_tool_calls",
    description="Extract all tool calls from a session — tool name, arguments, results, latency.",
)
def session_tool_calls(session_id: str) -> dict:
    path = config.SESSIONS_DIR / f"{session_id}.jsonl"
    if not path.exists():
        return {"ok": False, "error": f"session '{session_id}' not found"}
    records = _read_jsonl(path, limit=9999)
    calls = []
    pending: dict[str, dict] = {}
    for r in records:
        if r.get("tool_calls"):
            for tc in r["tool_calls"]:
                fn = tc.get("function", {})
                entry = {
                    "call_id": tc.get("id"),
                    "tool": fn.get("name"),
                    "arguments": fn.get("arguments", "")[:500],
                    "timestamp": r.get("timestamp"),
                }
                pending[tc.get("id", "")] = entry
                calls.append(entry)
        if r.get("tool_call_id") and r["tool_call_id"] in pending:
            pending[r["tool_call_id"]]["result_preview"] = r.get("content", "")[:500]
    return {"session_id": session_id, "tool_calls": calls, "count": len(calls)}


# ============================================================================
#  AI ACTIVITY — detailed view of what the AI read, wrote, searched, RAG'd
# ============================================================================

@server.tool(
    name="dev_ai_activity",
    description=(
        "Detailed trace of AI activity in a session: every tool call categorized — "
        "file reads, file writes, command executions, RAG searches, HTTP recon, "
        "hypothesis/notebook actions, with arguments and result previews."
    ),
)
def ai_activity(session_id: str) -> dict:
    path = config.SESSIONS_DIR / f"{session_id}.jsonl"
    if not path.exists():
        return {"ok": False, "error": f"session '{session_id}' not found"}
    records = _read_jsonl(path, limit=9999)
    activity: dict = {
        "file_reads": [],
        "file_writes": [],
        "commands": [],
        "rag_searches": [],
        "http_recon": [],
        "hypothesis_actions": [],
        "notebook_actions": [],
        "finding_actions": [],
        "other_tools": [],
    }
    result_map: dict[str, str] = {}
    for r in records:
        if r.get("role") == "tool" and r.get("tool_call_id"):
            result_map[r["tool_call_id"]] = r.get("content", "")[:800]

    for r in records:
        if not r.get("tool_calls"):
            continue
        for tc in r["tool_calls"]:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}
            call_id = tc.get("id", "")
            entry = {
                "tool": name,
                "args_summary": _summarize_args(name, args),
                "result_preview": result_map.get(call_id, "")[:400],
                "timestamp": r.get("timestamp"),
            }
            if name == "read_file":
                activity["file_reads"].append(entry)
            elif name == "write_file":
                activity["file_writes"].append(entry)
            elif name == "run_command":
                activity["commands"].append(entry)
            elif name in ("security_reference_search", "knowledge_search", "knowledge_fetch"):
                activity["rag_searches"].append(entry)
            elif name in ("http_recon", "port_discovery", "browser_fetch"):
                activity["http_recon"].append(entry)
            elif name.startswith("graph_") or name in ("record_hypothesis", "update_hypothesis_status"):
                activity["hypothesis_actions"].append(entry)
            elif name.startswith("note_") or name == "technique_recall":
                activity["notebook_actions"].append(entry)
            elif name == "record_finding":
                activity["finding_actions"].append(entry)
            else:
                activity["other_tools"].append(entry)

    summary = {k: len(v) for k, v in activity.items()}
    return {"session_id": session_id, "summary": summary, "activity": activity}


def _summarize_args(tool_name: str, args: dict) -> str:
    if tool_name == "read_file":
        return args.get("path", "?")
    if tool_name == "write_file":
        return f"{args.get('path', '?')} ({len(args.get('content', ''))} chars)"
    if tool_name == "run_command":
        return args.get("command", "?")[:200]
    if tool_name in ("security_reference_search", "knowledge_search"):
        return f"query: {args.get('query', '?')}"
    if tool_name == "http_recon":
        return args.get("url", "?")
    if tool_name == "graph_hypothesis_add":
        return f"title: {args.get('title', '?')}"
    if tool_name == "note_add":
        return f"[{args.get('category', '?')}] {args.get('note', '?')[:100]}"
    if tool_name == "record_finding":
        return f"[{args.get('severity', '?')}] {args.get('title', '?')}"
    return json.dumps(args)[:200]


# ============================================================================
#  HYPOTHESIS GRAPH
# ============================================================================

@server.tool(
    name="dev_hypothesis_list",
    description="List all hypotheses for an engagement with verdict, confidence, lifecycle status.",
)
def hypothesis_list(engagement_id: str, status: str = "") -> dict:
    db = _engagement_dir(engagement_id) / "hypothesis_graph.db"
    if not db.exists():
        return {"ok": False, "error": f"no hypothesis graph for '{engagement_id}'"}
    where = "WHERE lifecycle_status = ?" if status else ""
    params = (status,) if status else ()
    rows = _sqlite_rows(db, f"""
        SELECT hypothesis_id, ordinal, title, claim, lifecycle_status, verdict,
               confidence_band, impact, phase_created, origin_type,
               coverage, direct_tokens, created_at, updated_at
        FROM hypotheses {where}
        ORDER BY ordinal
    """, params)
    return {"engagement_id": engagement_id, "hypotheses": rows, "count": len(rows)}


@server.tool(
    name="dev_hypothesis_detail",
    description=(
        "Get full details of a hypothesis: experiments, observations, edges, operator notes, "
        "children. Accepts hypothesis_id or ordinal (e.g. 'H-3' or just '3')."
    ),
)
def hypothesis_detail(engagement_id: str, ref: str) -> dict:
    db = _engagement_dir(engagement_id) / "hypothesis_graph.db"
    if not db.exists():
        return {"ok": False, "error": f"no hypothesis graph for '{engagement_id}'"}
    hid = _resolve_ref(db, ref)
    if not hid:
        return {"ok": False, "error": f"hypothesis '{ref}' not found"}
    rows = _sqlite_rows(db, "SELECT * FROM hypotheses WHERE hypothesis_id = ?", (hid,))
    if not rows:
        return {"ok": False, "error": f"hypothesis '{ref}' not found"}
    hyp = rows[0]
    hyp["experiments"] = _sqlite_rows(db, """
        SELECT experiment_id, method_summary, status, observed_result,
               input_tokens, output_tokens, started_at, completed_at
        FROM experiments WHERE hypothesis_id = ? ORDER BY started_at
    """, (hid,))
    hyp["observations"] = _sqlite_rows(db, """
        SELECT observation_id, summary, polarity, strength, created_at
        FROM observations WHERE hypothesis_id = ? ORDER BY created_at
    """, (hid,))
    hyp["edges_from"] = _sqlite_rows(db, """
        SELECT edge_id, to_id, edge_type, reason, created_at
        FROM edges WHERE from_id = ?
    """, (hid,))
    hyp["edges_to"] = _sqlite_rows(db, """
        SELECT edge_id, from_id, edge_type, reason, created_at
        FROM edges WHERE to_id = ?
    """, (hid,))
    hyp["operator_notes"] = _sqlite_rows(db, """
        SELECT text, actor, created_at FROM operator_notes
        WHERE hypothesis_id = ? ORDER BY created_at
    """, (hid,))
    hyp["children"] = _sqlite_rows(db, """
        SELECT h.hypothesis_id, h.ordinal, h.title, h.lifecycle_status, h.verdict
        FROM hypotheses h JOIN edges e ON h.hypothesis_id = e.to_id
        WHERE e.from_id = ? AND e.edge_type = 'spawned_by'
    """, (hid,))
    return hyp


@server.tool(
    name="dev_hypothesis_tree",
    description="Get the full hypothesis tree as a nested structure showing parent-child relationships.",
)
def hypothesis_tree(engagement_id: str) -> dict:
    db = _engagement_dir(engagement_id) / "hypothesis_graph.db"
    if not db.exists():
        return {"ok": False, "error": f"no hypothesis graph for '{engagement_id}'"}
    hyps = _sqlite_rows(db, """
        SELECT hypothesis_id, ordinal, title, lifecycle_status, verdict,
               confidence_band, impact, primary_parent_id
        FROM hypotheses ORDER BY ordinal
    """)
    edges = _sqlite_rows(db, "SELECT from_id, to_id, edge_type FROM edges")
    state = _sqlite_rows(db, "SELECT * FROM graph_state LIMIT 1")
    return {
        "engagement_id": engagement_id,
        "hypotheses": hyps,
        "edges": edges,
        "graph_state": state[0] if state else {},
        "count": len(hyps),
    }


@server.tool(
    name="dev_graph_events",
    description="List hypothesis graph events chronologically — state changes, experiments, observations.",
)
def graph_events(engagement_id: str, limit: int = 100) -> dict:
    db = _engagement_dir(engagement_id) / "hypothesis_graph.db"
    if not db.exists():
        return {"ok": False, "error": f"no hypothesis graph for '{engagement_id}'"}
    events = _sqlite_rows(db, """
        SELECT event_id, event_type, entity_id, detail, created_at
        FROM events ORDER BY created_at DESC LIMIT ?
    """, (limit,))
    return {"engagement_id": engagement_id, "events": events}


@server.tool(
    name="dev_graph_check",
    description="Run invariant checks on the hypothesis graph — detect inconsistencies.",
)
def graph_check(engagement_id: str) -> dict:
    eng_dir = _engagement_dir(engagement_id)
    db = eng_dir / "hypothesis_graph.db"
    if not db.exists():
        return {"ok": False, "error": f"no hypothesis graph for '{engagement_id}'"}
    from .hypothesis_graph.store import HypothesisGraphStore
    store = HypothesisGraphStore(eng_dir)
    violations = store.check_invariants()
    return {
        "engagement_id": engagement_id,
        "ok": len(violations) == 0,
        "violations": violations,
        "graph_version": store.graph_version(),
    }


def _resolve_ref(db_path: Path, ref: str) -> str | None:
    ref = ref.strip()
    if ref.startswith("h_"):
        rows = _sqlite_rows(db_path, "SELECT hypothesis_id FROM hypotheses WHERE hypothesis_id = ?", (ref,))
        return rows[0]["hypothesis_id"] if rows else None
    num = ref.lstrip("HN-")
    if num.isdigit():
        rows = _sqlite_rows(db_path, "SELECT hypothesis_id FROM hypotheses WHERE ordinal = ?", (int(num),))
        return rows[0]["hypothesis_id"] if rows else None
    return None


# ============================================================================
#  NOTEBOOK
# ============================================================================

@server.tool(
    name="dev_notebook_list",
    description="List all notes in an engagement's notebook with category, status, surface.",
)
def notebook_list(engagement_id: str, category: str = "", status: str = "") -> dict:
    db = _engagement_dir(engagement_id) / "notebook.db"
    if not db.exists():
        return {"ok": False, "error": f"no notebook for '{engagement_id}'"}
    clauses = []
    params: list = []
    if category:
        clauses.append("category = ?")
        params.append(category)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    rows = _sqlite_rows(db, f"""
        SELECT note_id, ordinal, category, note, status, surface,
               resolve_reason, created_at, updated_at
        FROM notes {where} ORDER BY ordinal
    """, tuple(params))
    return {"engagement_id": engagement_id, "notes": rows, "count": len(rows)}


@server.tool(
    name="dev_notebook_detail",
    description="Get full details of a note including refs and events. Accepts note_id or ordinal (e.g. 'N-1').",
)
def notebook_detail(engagement_id: str, ref: str) -> dict:
    db = _engagement_dir(engagement_id) / "notebook.db"
    if not db.exists():
        return {"ok": False, "error": f"no notebook for '{engagement_id}'"}
    nid = _resolve_note_ref(db, ref)
    if not nid:
        return {"ok": False, "error": f"note '{ref}' not found"}
    rows = _sqlite_rows(db, "SELECT * FROM notes WHERE note_id = ?", (nid,))
    if not rows:
        return {"ok": False, "error": f"note '{ref}' not found"}
    note = rows[0]
    note["refs"] = _sqlite_rows(db, "SELECT ref FROM note_refs WHERE note_id = ?", (nid,))
    return note


@server.tool(
    name="dev_notebook_edit",
    description="Edit a note's text, category, or surface field.",
)
def notebook_edit(
    engagement_id: str,
    ref: str,
    note: str = "",
    category: str = "",
    surface: str = "",
) -> dict:
    db_path = _engagement_dir(engagement_id) / "notebook.db"
    if not db_path.exists():
        return {"ok": False, "error": f"no notebook for '{engagement_id}'"}
    nid = _resolve_note_ref(db_path, ref)
    if not nid:
        return {"ok": False, "error": f"note '{ref}' not found"}
    updates = []
    params: list = []
    if note:
        updates.append("note = ?")
        params.append(note)
    if category:
        updates.append("category = ?")
        params.append(category)
    if surface:
        updates.append("surface = ?")
        params.append(surface)
    if not updates:
        return {"ok": False, "error": "nothing to update"}
    updates.append("updated_at = ?")
    params.append(datetime.now(timezone.utc).isoformat())
    params.append(nid)
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(f"UPDATE notes SET {', '.join(updates)} WHERE note_id = ?", params)
        con.commit()
    finally:
        con.close()
    return {"ok": True, "note_id": nid}


@server.tool(
    name="dev_notebook_delete",
    description="Delete a note from the notebook. DESTRUCTIVE.",
)
def notebook_delete(engagement_id: str, ref: str, confirm: bool = False) -> dict:
    if not confirm:
        return {"ok": False, "error": "pass confirm=true to delete"}
    db_path = _engagement_dir(engagement_id) / "notebook.db"
    if not db_path.exists():
        return {"ok": False, "error": f"no notebook for '{engagement_id}'"}
    nid = _resolve_note_ref(db_path, ref)
    if not nid:
        return {"ok": False, "error": f"note '{ref}' not found"}
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("DELETE FROM note_refs WHERE note_id = ?", (nid,))
        con.execute("DELETE FROM notes WHERE note_id = ?", (nid,))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "deleted": nid}


@server.tool(
    name="dev_notebook_overview",
    description="Get notebook digest/overview — same context block the AI sees each turn.",
)
def notebook_overview(engagement_id: str) -> dict:
    eng_dir = _engagement_dir(engagement_id)
    db = eng_dir / "notebook.db"
    if not db.exists():
        return {"ok": False, "error": f"no notebook for '{engagement_id}'"}
    from .notebook.service import NotebookService
    svc = NotebookService(eng_dir)
    return {
        "overview": svc.overview(),
        "context_block": svc.build_context_block(),
    }


def _resolve_note_ref(db_path: Path, ref: str) -> str | None:
    ref = ref.strip()
    if ref.startswith("n_"):
        rows = _sqlite_rows(db_path, "SELECT note_id FROM notes WHERE note_id = ?", (ref,))
        return rows[0]["note_id"] if rows else None
    num = ref.lstrip("N-")
    if num.isdigit():
        rows = _sqlite_rows(db_path, "SELECT note_id FROM notes WHERE ordinal = ?", (int(num),))
        return rows[0]["note_id"] if rows else None
    return None


# ============================================================================
#  KNOWLEDGE RAG
# ============================================================================

@server.tool(
    name="dev_rag_search",
    description=(
        "Test a search query against the knowledge RAG index — returns ranked results with "
        "scores, sources, titles. Use to debug ranking and relevance."
    ),
)
def rag_search(query: str, top_k: int = 10, source: str = "") -> dict:
    from .knowledge_rag.service import KnowledgeRAGService
    svc = KnowledgeRAGService()
    result = svc.search(query, top_k=top_k, source=source or None)
    return result


@server.tool(
    name="dev_rag_stats",
    description="Get knowledge RAG index statistics — document count, sources, index file sizes.",
)
def rag_stats() -> dict:
    vectors_path = config.KNOWLEDGE_RAG_INDEX_PATH
    meta_path = config.KNOWLEDGE_RAG_META_PATH
    info: dict = {
        "vectors_path": str(vectors_path),
        "meta_path": str(meta_path),
        "vectors_exists": vectors_path.exists(),
        "meta_exists": meta_path.exists(),
    }
    if vectors_path.exists():
        info["vectors_size_mb"] = round(vectors_path.stat().st_size / (1024 * 1024), 1)
        info["vectors_modified"] = _file_age(vectors_path)
    if meta_path.exists():
        info["meta_size_mb"] = round(meta_path.stat().st_size / (1024 * 1024), 1)
        meta_lines = meta_path.read_text().splitlines()
        info["document_count"] = len(meta_lines)
        sources: dict[str, int] = {}
        for line in meta_lines:
            try:
                m = json.loads(line)
                src = m.get("source", "unknown")
                sources[src] = sources.get(src, 0) + 1
            except json.JSONDecodeError:
                pass
        info["sources"] = sources
    return info


@server.tool(
    name="dev_rag_health",
    description="Check if the embedding server (for RAG) is running and responsive.",
)
def rag_health() -> dict:
    try:
        from .knowledge_rag.embed_client import EmbedClient
        client = EmbedClient()
        healthy = client.health()
        return {"ok": healthy, "url": config.KNOWLEDGE_RAG_EMBED_SERVER_URL}
    except Exception as e:
        return {"ok": False, "error": str(e), "url": config.KNOWLEDGE_RAG_EMBED_SERVER_URL}


# ============================================================================
#  FINDINGS
# ============================================================================

@server.tool(
    name="dev_findings_list",
    description="List all findings for an engagement — title, severity, target, status.",
)
def findings_list(engagement_id: str) -> dict:
    eng_dir = _engagement_dir(engagement_id)
    state_db = eng_dir / "state.db"
    if not state_db.exists():
        return {"ok": False, "error": f"no state.db for '{engagement_id}'"}
    findings_dir = config.FINDINGS_DIR
    lifecycle = _sqlite_rows(state_db, """
        SELECT finding_id, status, linked_at, updated_at FROM findings_lifecycle
        ORDER BY linked_at
    """)
    results = []
    for fl in lifecycle:
        fid = fl["finding_id"]
        finding_path = findings_dir / f"{fid}.json"
        if finding_path.exists():
            finding = json.loads(finding_path.read_text())
            fl["detail"] = {
                "title": finding.get("title"),
                "severity": finding.get("severity"),
                "target": finding.get("target"),
                "confidence": finding.get("confidence"),
            }
        results.append(fl)
    return {"engagement_id": engagement_id, "findings": results, "count": len(results)}


@server.tool(
    name="dev_finding_detail",
    description="Get full details of a specific finding by ID.",
)
def finding_detail(finding_id: str) -> dict:
    finding_path = config.FINDINGS_DIR / f"{finding_id}.json"
    if not finding_path.exists():
        return {"ok": False, "error": f"finding '{finding_id}' not found"}
    return json.loads(finding_path.read_text())


# ============================================================================
#  AUDIT LOG
# ============================================================================

@server.tool(
    name="dev_audit_list",
    description=(
        "List audit log entries for a session — tool calls with scope decisions, "
        "latency, token usage, injection flags."
    ),
)
def audit_list(session_id: str, limit: int = 100) -> dict:
    audit_dir = config.AUDIT_DIR
    path = audit_dir / f"{session_id}.jsonl"
    if not path.exists():
        return {"ok": False, "error": f"no audit log for session '{session_id}'"}
    entries = _read_jsonl(path, limit=limit)
    summary = []
    for e in entries:
        summary.append({
            "entry_id": e.get("entry_id"),
            "turn_index": e.get("turn_index"),
            "tool_name": e.get("tool_name"),
            "scope_decision": e.get("scope_decision"),
            "latency_ms": e.get("latency_ms"),
            "prompt_tokens": e.get("prompt_tokens"),
            "completion_tokens": e.get("completion_tokens"),
            "injection_flagged": e.get("injection_flagged"),
            "timestamp": e.get("timestamp"),
            "arguments_preview": json.dumps(e.get("arguments", {}))[:200],
        })
    return {"session_id": session_id, "entries": summary, "count": len(summary)}


@server.tool(
    name="dev_audit_verify",
    description="Verify the integrity of a session's audit chain — detect tampering.",
)
def audit_verify(session_id: str) -> dict:
    try:
        from .audit_log import verify
        ok, message = verify(session_id)
        return {"ok": ok, "message": message, "session_id": session_id}
    except Exception as e:
        return {"ok": False, "error": str(e), "session_id": session_id}


# ============================================================================
#  HEALTH / BUDGET MONITOR
# ============================================================================

@server.tool(
    name="dev_server_health",
    description=(
        "Check health of all backend services — llama-server, embedding server, "
        "kill switch status, disk usage of state directories."
    ),
)
def server_health() -> dict:
    import urllib.request
    import urllib.error

    status: dict = {}

    # llama-server
    try:
        req = urllib.request.Request(f"{config.LLAMA_SERVER_URL}/health", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            status["llama_server"] = {"ok": resp.status == 200, "url": config.LLAMA_SERVER_URL}
    except Exception as e:
        status["llama_server"] = {"ok": False, "error": str(e), "url": config.LLAMA_SERVER_URL}

    # embedding server
    try:
        req = urllib.request.Request(f"{config.KNOWLEDGE_RAG_EMBED_SERVER_URL}/health", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            status["embed_server"] = {"ok": resp.status == 200, "url": config.KNOWLEDGE_RAG_EMBED_SERVER_URL}
    except Exception as e:
        status["embed_server"] = {"ok": False, "error": str(e), "url": config.KNOWLEDGE_RAG_EMBED_SERVER_URL}

    # kill switch
    status["kill_switch"] = {
        "engaged": config.KILL_SWITCH_PATH.exists(),
        "path": str(config.KILL_SWITCH_PATH),
    }

    # state dir sizes
    for name, path in [
        ("sessions", config.SESSIONS_DIR),
        ("audit", config.AUDIT_DIR),
        ("findings", config.FINDINGS_DIR),
        ("engagements", config.ENGAGEMENTS_ROOT),
    ]:
        if path.exists():
            total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            status[f"{name}_size_mb"] = round(total / (1024 * 1024), 2)
        else:
            status[f"{name}_size_mb"] = 0

    # config summary
    status["config"] = {
        "llama_server_url": config.LLAMA_SERVER_URL,
        "max_iterations": config.MAX_ITERATIONS,
        "reserved_output_tokens": config.RESERVED_OUTPUT_TOKENS,
        "tool_call_timeout_s": config.TOOL_CALL_TIMEOUT_S,
        "task_wall_clock_s": config.TASK_WALL_CLOCK_S,
        "web_ui_port": config.WEB_UI_PORT,
    }
    return status


@server.tool(
    name="dev_budget_status",
    description=(
        "Check llama-server's current context window usage — n_ctx, how many tokens are "
        "loaded, model info. Requires llama-server running."
    ),
)
def budget_status() -> dict:
    import urllib.request
    import urllib.error

    try:
        req = urllib.request.Request(f"{config.LLAMA_SERVER_URL}/props", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            props = json.loads(resp.read())
    except Exception as e:
        return {"ok": False, "error": f"llama-server /props failed: {e}"}

    result: dict = {"ok": True}
    result["n_ctx"] = props.get("n_ctx")
    result["model"] = props.get("default_generation_settings", {}).get("model")
    result["reserved_output"] = config.RESERVED_OUTPUT_TOKENS
    result["safety_margin"] = config.SAFETY_MARGIN

    try:
        req2 = urllib.request.Request(f"{config.LLAMA_SERVER_URL}/health", method="GET")
        with urllib.request.urlopen(req2, timeout=5) as resp2:
            health = json.loads(resp2.read())
            result["slots_idle"] = health.get("slots_idle")
            result["slots_processing"] = health.get("slots_processing")
    except Exception:
        pass
    return result


# ============================================================================
#  TEST RUNNER
# ============================================================================

@server.tool(
    name="dev_run_test",
    description=(
        "Run a specific test file or test case. Examples: "
        "'agent/test_loop_steer.py', "
        "'agent/test_loop_steer.py::TestSteerAway::test_repeat_detected', "
        "'agent/knowledge_rag/test_service.py'."
    ),
)
def run_test(test_path: str, verbose: bool = True) -> dict:
    project_dir = config.PROJECT_DIR
    cmd = [sys.executable, "-m", "pytest", test_path]
    if verbose:
        cmd.append("-v")
    try:
        result = subprocess.run(
            cmd,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout[-3000:] if len(result.stdout) > 3000 else result.stdout,
            "stderr": result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
            "command": " ".join(cmd),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "test timed out after 120s", "command": " ".join(cmd)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@server.tool(
    name="dev_run_all_tests",
    description="Run the full fast test suite (same as pre-commit hook — excludes browser/corpus_src).",
)
def run_all_tests() -> dict:
    project_dir = config.PROJECT_DIR
    cmd = [sys.executable, "-m", "pytest", "-v"]
    test_files = []
    agent_dir = project_dir / "agent"
    for f in sorted(agent_dir.rglob("test_*.py")):
        rel = f.relative_to(project_dir)
        parts = rel.parts
        if "web" in parts or "browser" in parts or "corpus_src" in parts:
            continue
        test_files.append(str(rel))
    cmd.extend(test_files)
    try:
        result = subprocess.run(
            cmd,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=300,
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "test_count": len(test_files),
            "stdout": result.stdout[-4000:] if len(result.stdout) > 4000 else result.stdout,
            "stderr": result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "tests timed out after 300s"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ============================================================================
#  DEV_SERVER (web UI MVP) STATE  — reads agent/web/dev_data/state.json directly,
#  no running dev_server needed. This is where the Windows MVP keeps its sessions/
#  findings (the full-agent SQLite tools above target the Linux agent instead).
# ============================================================================

_DEV_DATA = Path(__file__).resolve().parent / "web" / "dev_data"


def _load_devstate() -> dict | None:
    p = _DEV_DATA / "state.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


@server.tool(
    name="dev_devserver_state",
    description="Overview of the dev_server (web UI MVP) persisted state — sessions, engagements, "
                "findings. Reads agent/web/dev_data/state.json; no running server needed.",
)
def devserver_state() -> dict:
    st = _load_devstate()
    if not st:
        return {"ok": False, "error": "no agent/web/dev_data/state.json yet (run the dev_server once)"}
    return {
        "sessions": [
            {"session_id": s["session_id"], "messages": len(s.get("messages", [])),
             "engagement": s.get("engagement_id"), "summarized": bool(s.get("summary"))}
            for s in st.get("sessions", [])
        ],
        "engagements": [{"id": e["engagement_id"], "targets": e.get("allow_targets", [])}
                        for e in st.get("engagements", [])],
        "findings_count": {k: len(v) for k, v in st.get("findings", {}).items()},
    }


@server.tool(
    name="dev_devserver_session",
    description="Inspect one dev_server session — recent messages, tool calls, and the compaction "
                "summary. The primary tool for debugging what the dev_server's agent actually did.",
)
def devserver_session(session_id: str, limit: int = 50) -> dict:
    st = _load_devstate()
    if not st:
        return {"ok": False, "error": "no state.json"}
    s = next((x for x in st.get("sessions", []) if x["session_id"] == session_id), None)
    if not s:
        return {"ok": False, "error": f"session '{session_id}' not found"}
    msgs = []
    for m in s.get("messages", [])[-limit:]:
        extra = m.get("extra") or {}
        e: dict = {"role": m.get("role"), "content": (m.get("content") or "")[:800]}
        tc = m.get("tool_calls") or extra.get("tool_calls")
        if tc:
            e["tool_calls"] = tc
        tn = m.get("tool_name") or extra.get("tool_name")
        if tn:
            e["tool_name"] = tn
        if m.get("reasoning_content") or extra.get("reasoning_content"):
            e["reasoning"] = (m.get("reasoning_content") or extra.get("reasoning_content"))[:400]
        msgs.append(e)
    return {"session_id": session_id, "total_messages": len(s.get("messages", [])),
            "summary": s.get("summary", ""), "summary_upto": s.get("summary_upto", 0),
            "engagement": s.get("engagement_id"), "messages": msgs}


@server.tool(
    name="dev_devserver_findings",
    description="List findings recorded by the dev_server (by engagement), from dev_data/state.json.",
)
def devserver_findings() -> dict:
    st = _load_devstate()
    if not st:
        return {"ok": False, "error": "no state.json"}
    return {"findings": st.get("findings", {})}


# ============================================================================
#  ENTRYPOINT
# ============================================================================

def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
