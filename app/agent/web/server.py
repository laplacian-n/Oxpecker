"""Phase 6 web UI backend — a plain FastAPI server, no build step, serving both the API and a
static single-page HTML/JS frontend (agent/web/static/index.html). Loopback-only by default
(127.0.0.1), matching this project's Phase-1 default-safe posture. If
`config.WEB_UI_API_KEY_FILE` exists, `require_api_key` below gates every `/api/*` route behind
it (unset -> unauthenticated, the exact prior behavior every existing test relies on) — see that
function's docstring for why this is deliberately lighter than the memory service's multi-device
`DeviceStore`.

Streaming here is both turn-level (each completed assistant message / tool call, pushed as it
happens) and, within a turn, token-level for content and reasoning deltas as `AgentLoop` streams
them from `LlamaClient` — all via the same Server-Sent Events channel
(`GET /api/sessions/{id}/events`), distinguished by event `type`.

Approval prompts (both `run_command`'s local confirm_fn and broker-mediated tools running in the
separate security_mcp_server.py subprocess) are bridged through the same
`agent.broker.approval_queue.ApprovalQueue` this project already built for exactly this —
`AgentLoop` gets a confirm_fn that submits-and-waits on that queue instead of calling `input()`
(which would block on this process's own stdin, not something a browser can drive), and
`use_approval_queue=True` switches the security_mcp_server.py subprocess's own Broker to the
same out-of-band mode. One `/api/approvals` surface handles both.
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config
from .. import session as session_mod
from ..broker.approval_queue import ApprovalQueue
from ..broker.consult_queue import ConsultQueue
from ..engagement.event_log import EngagementEventLog
from ..engagement.intake import EngagementIntake, IntakeValidationError, create_engagement
from ..engagement.store import EngagementStore
from . import engagement_access
from ..findings.model import FindingNotFoundError, FindingsStore
from ..hypothesis_graph.service import HypothesisGraphService
from ..hypothesis_graph.store import ConflictError, GraphValidationError, NotFoundError
from ..notebook.service import NotebookService
from ..notebook.store import NotFoundError as NotebookNotFound
from ..notebook.store import NotebookValidationError
from ..notebook.technique_kb import TechniqueKB
from ..loop import AgentLoop
from ..loop_control.steer import SteerChannel
from ..pipeline.autonomous_driver import VALID_MODES, AutonomousDriver, AutonomousRunResult

STATIC_DIR = Path(__file__).resolve().parent / "static"
WEB_APPROVAL_TIMEOUT_S = 600  # how long a local run_command confirm prompt waits for the UI


class WebSessionStore(session_mod.SessionStore):
    """Same on-disk persistence as the real SessionStore (nothing about *storage* changes) —
    append() also pushes the new record onto this session's live event queue, which is how the
    SSE endpoint below finds out something happened without polling the JSONL file."""

    def __init__(self, session_id: str, on_append):
        super().__init__(session_id)
        self._on_append = on_append

    def append(self, role: str, content: str, extra: dict | None = None) -> dict:
        record = super().append(role, content, extra)
        self._on_append(record)
        return record


class SessionHandle:
    def __init__(self, session_id: str, loop: AgentLoop):
        self.session_id = session_id
        self.loop = loop
        self.events: queue.Queue = queue.Queue()
        self.running = False
        self.lock = threading.Lock()
        # Autonomous/consult mode state — a session has at most one autonomous run active at a
        # time, tracked separately from `running` (which is about a single run_task() call);
        # an autonomous run is long-lived and drives many run_task()-equivalent turns itself.
        self.autonomous_driver: AutonomousDriver | None = None
        self.autonomous_result: AutonomousRunResult | None = None

    def push(self, event: dict) -> None:
        self.events.put(event)


_sessions: dict[str, SessionHandle] = {}
_approval_queue = ApprovalQueue()
_consult_queue = ConsultQueue()

# One EngagementEventLog per engagement_id, created on first append. A dict guarded by a lock so
# two concurrent requests for the same engagement share one log object (and therefore one
# process-local view) rather than racing to open two — the log's own on-disk append is already
# cross-process safe, this just avoids churning connections.
_event_logs: dict[str, EngagementEventLog] = {}
_event_logs_lock = threading.Lock()


def _engagement_event_log(engagement_id: str, *, create: bool) -> EngagementEventLog | None:
    """The engagement's event log. With create=False, returns None when the engagement has
    emitted nothing yet (no events.db), so a read does not bring a log into existence as a side
    effect — the same don't-create-on-a-GET guard the hypothesis-graph and notebook GETs use."""
    with _event_logs_lock:
        log_obj = _event_logs.get(engagement_id)
        if log_obj is not None:
            return log_obj
        engagement_dir = config.ENGAGEMENTS_ROOT / engagement_id
        log_obj = (
            EngagementEventLog(engagement_dir) if create
            else EngagementEventLog.open_if_exists(engagement_dir)
        )
        if log_obj is not None:
            _event_logs[engagement_id] = log_obj
        return log_obj


def _compose_sinks(*sinks):
    """Fan one event out to several callables; one sink raising never robs the others. The
    session queue and the engagement log are both fed from the driver's single `on_event`."""
    def composed(event: dict) -> None:
        for s in sinks:
            try:
                s(event)
            except Exception:  # noqa: BLE001
                log.warning("an event sink raised", exc_info=True)
    return composed


def _engagement_event_sink(engagement_id: str):
    """A callable that appends a pipeline event to this engagement's event log. The driver's own
    event dicts carry a `type`; it becomes the log `kind`, the rest becomes the payload. Until
    the wave model (§2.5) emits the typed §4.2 kinds directly, these land on the projection's raw
    timeline rather than a typed slice — recorded and replayable, not interpreted — which is the
    honest state of a pipe that exists before all its producers do."""
    def sink(event: dict) -> None:
        try:
            kind = event.get("type", "pipeline_event")
            payload = {k: v for k, v in event.items() if k != "type"}
            _engagement_event_log(engagement_id, create=True).append(kind, payload)
        except Exception:  # noqa: BLE001 - a telemetry append must never take down a run
            log.warning("failed to append pipeline event to the engagement log", exc_info=True)

    return sink


def _authorize_engagement_read(request: Request, engagement_id: str) -> None:
    """The §5.3 account boundary for every engagement read. Raises 404 when there is no such
    engagement (so another account's engagement is not even confirmed to exist), 403 when the
    caller's account does not own it. Called by both new endpoints, on connect and — because a
    reconnect is just another GET — again on resume, which is what `CLIENT_UI_DESIGN.md` §4.1.2
    requires: a reconnect with a `Last-Event-ID` for an engagement the caller no longer owns is
    refused, not resumed."""
    account = engagement_access.resolve_account(_presented_key(request))
    decision = engagement_access.owns(account, engagement_id)
    if decision is None:
        raise HTTPException(404, f"no engagement {engagement_id!r}")
    if not decision:
        raise HTTPException(403, f"engagement {engagement_id!r} belongs to another account")


def _check_api_version(request: Request) -> None:
    """§4.3: the client sends its API version; a server that cannot serve it says so plainly and
    the client refuses to run, rather than an old client half-working against a new server. The
    major version is the compatibility unit. Absent (an older client, or a test) is allowed, so
    this is additive — enforcement begins the moment a client actually declares a version."""
    declared = request.headers.get("x-api-version") or request.query_params.get("api_version")
    if not declared:
        return
    if declared.split(".")[0] != API_VERSION.split(".")[0]:
        raise HTTPException(
            409,
            f"client API version {declared} is incompatible with server {API_VERSION}; "
            "this endpoint will not serve a different major version",
        )


def _make_web_confirm_fn(session_id: str):
    def confirm_fn(prompt: str) -> bool:
        request_id = _approval_queue.submit(
            session_id=session_id, tool="local_confirm", arguments={"prompt": prompt}, reason=prompt
        )
        try:
            record = _approval_queue.wait_for_resolution(request_id, timeout_s=WEB_APPROVAL_TIMEOUT_S)
        except TimeoutError:
            return False
        return record["status"] == "approved"

    return confirm_fn


class CreateSessionRequest(BaseModel):
    profile: str = config.PROFILE_SAFE_DEFAULT
    use_security_tools: bool = False
    engagement_id: str = "lab-default"
    dangerous_local: bool = False
    isolation_tier: str = "bubblewrap"


class MessageRequest(BaseModel):
    content: str


class SteerRequest(BaseModel):
    message: str


class ApprovalResolveRequest(BaseModel):
    approved: bool
    resolved_by: str = "web-ui"


class ConsultResolveRequest(BaseModel):
    answer: str
    resolved_by: str = "web-ui"


class CreateEngagementRequest(BaseModel):
    engagement_id: str
    description: str = ""
    allow_targets: list[str]
    allowed_action_classes: list[str]
    authorized_by: str
    valid_hours: float = 24.0


class OperatorGraphActionRequest(BaseModel):
    action: str          # "park" | "reopen" | "note"
    reason: str = ""     # required for park
    text: str = ""       # required for note


class AutonomousStartRequest(BaseModel):
    engagement_id: str
    profile: str = "web_api"  # agent.pipeline.profiles.PROFILES key
    mode: str  # "autonomous" | "consult"


log = logging.getLogger("agent.web.server")

API_VERSION = "1.0.0"

app = FastAPI(title="Oxpecker Agent API", version=API_VERSION)

if config.WEB_UI_CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.WEB_UI_CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def _web_ui_key() -> str | None:
    if config.WEB_UI_API_KEY_FILE.exists():
        return config.WEB_UI_API_KEY_FILE.read_text().strip()
    return None


def _presented_key(request: Request) -> str:
    """The key the caller supplied — the Bearer header, or the `?key=` query param that
    EventSource must fall back to because it cannot set headers."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth.removeprefix("Bearer ").strip()
    return request.query_params.get("key", "")


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    """Gates every /api/* route behind the configured accounts, when any exist — none configured
    (no key file, no accounts file) means unauthenticated, exactly Phase 1's original
    loopback-is-the-boundary posture, so every existing test (none of which send an Authorization
    header) keeps passing unchanged.

    Validity is decided by `engagement_access.resolve_account`, which accepts the single
    `WEB_UI_API_KEY_FILE` key *and* any key listed in a `web_ui_accounts.json` — so a second
    account's valid key reaches its route (where the per-engagement owner check in §5.3 decides
    what it may read) instead of being turned away at the door as if it were a bad key. The
    single-key and no-key cases are unchanged.

    The static "/" page and its assets are never gated — the page shell itself carries no data,
    every real action goes through /api/*, and gating "/" too would need a full login redirect
    flow this single-operator tool doesn't need. Checked as ASGI middleware (not a FastAPI
    Depends()) so it applies uniformly without touching ~25 individual route decorators.

    EventSource (the SSE stream) can't set custom headers, so its one route additionally accepts
    the key as a `?key=` query param — a deliberate, narrow exception, not a general bypass (a
    bearer header still works there too; `_presented_key` checks both).
    """
    if not engagement_access.valid_accounts() or not request.url.path.startswith("/api/"):
        return await call_next(request)
    if engagement_access.resolve_account(_presented_key(request)) is None:
        return JSONResponse({"detail": "missing or invalid API key"}, status_code=401)
    return await call_next(request)


@app.get("/api/health")
def health_check() -> dict:
    return {
        "status": "ok",
        "version": API_VERSION,
        "sessions_active": len(_sessions),
        "timestamp": time.time(),
    }


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC_DIR / "index.html").read_text()


@app.post("/api/sessions")
def create_session(req: CreateSessionRequest) -> dict:
    session_id = session_mod.SessionStore.new_session_id()
    workspace_root = config.default_workspace_root()
    handle_ref: dict[str, SessionHandle | None] = {"handle": None}

    def on_append(record: dict) -> None:
        h = handle_ref["handle"]
        if h is not None:
            h.push({"type": "message", "record": record})

    def on_stream(chunk: str) -> None:
        h = handle_ref["handle"]
        if h is not None:
            h.push({"type": "assistant_delta", "text": chunk})

    def on_reasoning_stream(chunk: str) -> None:
        h = handle_ref["handle"]
        if h is not None:
            h.push({"type": "reasoning_delta", "text": chunk})

    memory = WebSessionStore(session_id, on_append)
    loop = AgentLoop(
        workspace_root=workspace_root,
        session_id=session_id,
        profile=req.profile,
        dangerous_local=req.dangerous_local,
        confirm_fn=_make_web_confirm_fn(session_id),
        memory=memory,
        on_stream=on_stream,
        on_reasoning_stream=on_reasoning_stream,
        use_security_tools=req.use_security_tools,
        # The Hypothesis Tree panel reads hypothesis_graph.db, which only the graph_* tools
        # write — with the graph off the model gets the flat record_hypothesis instead and the
        # panel stays empty. Turn it on whenever security tools are (that's a security-testing
        # session, where hypotheses are the point). Graph mutations don't need an EngagementStore,
        # so this works for lab-default too.
        use_hypothesis_graph=req.use_security_tools,
        # Create state.db + import the lab-default RoE + seed assets from scope, so record_finding
        # and osint_record work from the first turn (same bootstrap the autonomous driver runs).
        bootstrap_engagement=req.use_security_tools,
        device_id="web-ui",
        engagement_id=req.engagement_id,
        isolation_tier=req.isolation_tier,
        use_approval_queue=req.use_security_tools,
    )
    handle = SessionHandle(session_id, loop)
    handle_ref["handle"] = handle
    _sessions[session_id] = handle
    return {
        "session_id": session_id,
        "workspace_root": str(workspace_root),
        "profile": req.profile,
        "use_security_tools": req.use_security_tools,
        "engagement_id": req.engagement_id,
    }


_DRIVER_PROMPT_RE = re.compile(
    r"^\s*(INTAKE|RECON|ANALYSIS|VALIDATION|REPORT|CLOSEOUT)\s+phase[.:]|RECORDED OBSERVATIONS", re.I
)


def _session_summary(path: Path) -> tuple[str, int]:
    """(title, message_count) for the sidebar — title is the first operator-typed line, skipping
    the pipeline driver's own phase prompts so an autonomous run doesn't show up as a wall of
    'ANALYSIS phase. Review the observations…'."""
    title, count = "", 0
    try:
        with path.open() as f:
            for line in f:
                if not line.strip():
                    continue
                count += 1
                if title:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("role") != "user":
                    continue
                body = (rec.get("content") or "").strip()
                body = re.sub(r"\s*/no_?think\s*$", "", body)  # thinking-mode control suffix
                if not body or _DRIVER_PROMPT_RE.match(body) or body.startswith("[OPERATOR STEER]"):
                    continue
                title = " ".join(body.split())[:80]
    except OSError:
        pass
    return title, count


@app.get("/api/sessions")
def list_sessions() -> list[dict]:
    seen: set[str] = set()
    out = []
    if config.SESSIONS_DIR.exists():
        for path in sorted(config.SESSIONS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            session_id = path.stem
            seen.add(session_id)
            title, count = _session_summary(path)
            out.append({
                "session_id": session_id,
                "known_live": session_id in _sessions,
                "last_modified": path.stat().st_mtime,
                "title": title,
                "message_count": count,
            })
    # A freshly created session has no .jsonl file yet (SessionStore only creates one on the
    # first append()) but is still real and should be selectable — found via a real test that
    # created a session, never sent a message, and correctly expected it to show up here.
    for session_id, handle in _sessions.items():
        if session_id not in seen:
            out.append({"session_id": session_id, "known_live": True, "last_modified": time.time(),
                        "title": "", "message_count": 0})
    return out


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str) -> dict:
    handle = _sessions.get(session_id)
    if handle is not None and (handle.running or (handle.autonomous_driver is not None and handle.autonomous_result is None)):
        raise HTTPException(409, "this session has work in progress — stop it before deleting")
    _sessions.pop(session_id, None)
    path = session_mod.SessionStore(session_id).path
    existed = path.exists()
    path.unlink(missing_ok=True)
    return {"deleted": existed}


def _get_handle(session_id: str) -> SessionHandle:
    handle = _sessions.get(session_id)
    if handle is None:
        raise HTTPException(404, f"session {session_id!r} is not loaded in this server process "
                             "(restarting the server loses live sessions; the transcript itself "
                             "is still on disk)")
    return handle


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    store = session_mod.SessionStore(session_id)
    if not store.path.exists():
        if session_id not in _sessions:
            raise HTTPException(404, f"no session {session_id!r} on disk or in memory")
        return {"session_id": session_id, "messages": []}  # created, nothing sent yet
    # Map from the raw record stream rather than load_as_messages() so message_id survives to the
    # frontend — the Hypothesis Tree panel's chat-anchor buttons scroll the transcript to a
    # specific message_id, which the OpenAI-shaped messages list deliberately drops.
    messages = [
        {
            "role": r["role"],
            "content": r["content"],
            "message_id": r.get("message_id"),
            **({"name": r["name"]} if "name" in r else {}),
        }
        for r in store.load_all()
    ]
    return {"session_id": session_id, "messages": messages}


@app.post("/api/sessions/{session_id}/messages")
async def send_message(session_id: str, req: MessageRequest) -> dict:
    handle = _get_handle(session_id)
    with handle.lock:
        if handle.running:
            raise HTTPException(409, "a task is already running for this session")
        handle.running = True

    def run() -> None:
        try:
            result = handle.loop.run_task(req.content)
            handle.push({"type": "task_done", "status": result.status, "message": result.message})
        except Exception as e:  # noqa: BLE001 - surfaced to the UI, never a silent thread death
            handle.push({"type": "task_error", "error": f"{type(e).__name__}: {e}"})
        finally:
            with handle.lock:
                handle.running = False

    threading.Thread(target=run, daemon=True).start()
    return {"status": "started"}


@app.post("/api/sessions/{session_id}/steer")
def steer_session(session_id: str, req: SteerRequest) -> dict:
    _get_handle(session_id)  # 404s if unknown, even though SteerChannel itself is file-based
    SteerChannel(session_id).send(req.message, sender="web-ui")
    return {"status": "sent"}


@app.get("/api/sessions/{session_id}/events")
async def stream_events(session_id: str):
    handle = _get_handle(session_id)

    async def event_stream():
        while True:
            try:
                event = handle.events.get_nowait()
                yield f"data: {json.dumps(event)}\n\n"
            except queue.Empty:
                await asyncio.sleep(0.06)  # keeps streamed assistant_delta events feeling live

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.websocket("/api/sessions/{session_id}/ws")
async def websocket_events(websocket: WebSocket, session_id: str):
    """Bidirectional WebSocket for Electron/desktop clients. Sends the same events as the SSE
    endpoint, and accepts JSON commands: {"type": "message", "content": "..."} to send a chat
    message, {"type": "steer", "message": "..."} to steer a running task."""
    if engagement_access.valid_accounts():
        supplied = websocket.query_params.get("key", "")
        if engagement_access.resolve_account(supplied) is None:
            await websocket.close(code=4001, reason="missing or invalid API key")
            return

    handle = _sessions.get(session_id)
    if handle is None:
        await websocket.close(code=4004, reason=f"session {session_id!r} not loaded")
        return

    await websocket.accept()

    async def send_events():
        try:
            while True:
                try:
                    event = handle.events.get_nowait()
                    await websocket.send_json(event)
                except queue.Empty:
                    await asyncio.sleep(0.06)
        except (WebSocketDisconnect, Exception):
            pass

    send_task = asyncio.create_task(send_events())

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            if msg_type == "message":
                content = data.get("content", "").strip()
                if not content:
                    await websocket.send_json({"type": "error", "error": "empty message"})
                    continue
                with handle.lock:
                    if handle.running:
                        await websocket.send_json({"type": "error", "error": "task already running"})
                        continue
                    handle.running = True

                def run(c=content):
                    try:
                        result = handle.loop.run_task(c)
                        handle.push({"type": "task_done", "status": result.status, "message": result.message})
                    except Exception as e:
                        handle.push({"type": "task_error", "error": f"{type(e).__name__}: {e}"})
                    finally:
                        with handle.lock:
                            handle.running = False

                threading.Thread(target=run, daemon=True).start()
                await websocket.send_json({"type": "ack", "action": "message"})

            elif msg_type == "steer":
                message = data.get("message", "").strip()
                if message:
                    SteerChannel(session_id).send(message, sender="ws-client")
                    await websocket.send_json({"type": "ack", "action": "steer"})

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong", "timestamp": time.time()})

    except WebSocketDisconnect:
        pass
    finally:
        send_task.cancel()


@app.get("/api/approvals")
def list_approvals(session_id: str | None = None) -> list[dict]:
    return _approval_queue.list_pending(session_id=session_id)


@app.post("/api/approvals/{request_id}/resolve")
def resolve_approval(request_id: str, req: ApprovalResolveRequest) -> dict:
    try:
        return _approval_queue.resolve(request_id, approved=req.approved, resolved_by=req.resolved_by)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/consults")
def list_consults(session_id: str | None = None) -> list[dict]:
    return _consult_queue.list_pending(session_id=session_id)


@app.post("/api/consults/{request_id}/resolve")
def resolve_consult(request_id: str, req: ConsultResolveRequest) -> dict:
    try:
        return _consult_queue.resolve(request_id, answer=req.answer, resolved_by=req.resolved_by)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/engagements")
def create_engagement_endpoint(req: CreateEngagementRequest, request: Request) -> dict:
    now = time.time()
    intake = EngagementIntake(
        engagement_id=req.engagement_id,
        description=req.description,
        allow_targets=req.allow_targets,
        valid_from=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        valid_until=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + req.valid_hours * 3600)),
        allowed_action_classes=req.allowed_action_classes,
        authorized_by=req.authorized_by,
        operator_sign_off=True,  # submitting this form from the operator's own browser session
        # already is the affirmative sign-off agent.engagement.intake.py requires — there's no
        # second, different human this UI could obtain it from.
    )
    try:
        eng_dir = create_engagement(intake, engagements_root=config.ENGAGEMENTS_ROOT)
    except IntakeValidationError as e:
        raise HTTPException(400, str(e)) from e

    # §5.3: an owner on every engagement. The creator owns what they create — resolved from the
    # key on this request, which is the single default operator unless a multi-account map is
    # configured. Written into roe.json (where engagement_access.engagement_owner reads it) right
    # after create_engagement, so a freshly made engagement is owner-filtered from its first read.
    owner = engagement_access.resolve_account(_presented_key(request)) or config.WEB_UI_DEFAULT_ACCOUNT
    roe_path = eng_dir / "roe.json"
    roe = json.loads(roe_path.read_text())
    roe["owner"] = owner
    roe_path.write_text(json.dumps(roe, indent=2))

    # roe.json/scope.txt/deny.txt alone give the *policy* the pipeline can use — the
    # orchestrator plans work from EngagementStore assets, a separate structure this endpoint
    # didn't create at all until found live: an engagement made through this form had zero
    # assets, so PipelineOrchestrator.check_transition() could never leave INTAKE and Autonomous
    # mode would report "blocked" immediately on every single UI-created engagement. Each
    # single-host/hostname target becomes an asset automatically (a whole CIDR range doesn't map
    # to one scannable host, so those are skipped here — add assets for those separately).
    store = EngagementStore(eng_dir)
    for target in req.allow_targets:
        if "/" not in target:
            store.upsert_asset("host", target)

    return {"engagement_id": req.engagement_id, "engagement_dir": str(eng_dir), "owner": owner}


@app.get("/api/engagements")
def list_engagements() -> list[dict]:
    out = []
    if config.ENGAGEMENTS_ROOT.exists():
        for path in sorted(config.ENGAGEMENTS_ROOT.iterdir()):
            roe_path = path / "roe.json"
            if not roe_path.exists():
                continue
            try:
                roe = json.loads(roe_path.read_text())
            except json.JSONDecodeError:
                continue
            out.append({
                "engagement_id": path.name,
                "description": roe.get("description", ""),
                "valid_until": roe.get("valid_until"),
                "allowed_action_classes": roe.get("allowed_action_classes", []),
            })
    return out


@app.get("/api/engagements/{engagement_id}/snapshot")
def engagement_snapshot(engagement_id: str, request: Request, at: int | None = None) -> dict:
    """The read-model projection (§4.2) as of sequence number `at` (default: the latest), so a
    new window does not replay an eight-hour engagement from zero, and the time scrubber (§6.5)
    can ask for the state at an earlier point. §5.3-filtered like every read; §4.3 version-gated.

    Empty (never 404 for an existing engagement) when nothing has been emitted yet, so a surface
    can render a clean empty state — the same not-created-on-a-GET guard the other engagement
    GETs use."""
    _check_api_version(request)
    _authorize_engagement_read(request, engagement_id)
    event_log = _engagement_event_log(engagement_id, create=False)
    if event_log is None:
        from ..engagement.event_log import project
        return project([], at_seq=0, latest_seq=0)
    return event_log.snapshot(at_seq=at)


async def _sse_event_stream(engagement_id: str, after: int, is_disconnected, *, poll: float = 0.1):
    """The SSE body for `engagement_events`, factored out so the catch-up/resume/tail logic can
    be tested directly — Starlette's TestClient buffers a whole response before returning, so an
    infinite SSE generator cannot be consumed through it. `is_disconnected` is an async callable
    (the live request's `request.is_disconnected` in production, a fake in the test) and is the
    only thing that ends the loop, which is the correct lifetime: the stream lives as long as the
    client is connected.

    A control event goes first so the client can state the gap (§5.5 "you missed N events")
    without a second request. Then everything after `after` is replayed (a fresh subscriber uses
    0 — the whole log) and new events are tailed as they are appended, from this process or
    another; each carries its sequence number as the SSE `id:`, which the browser echoes back as
    `Last-Event-ID` on reconnect."""
    event_log = _engagement_event_log(engagement_id, create=False)
    latest = event_log.latest_seq() if event_log is not None else 0
    missed = max(latest - after, 0) if after else 0
    hello = {"seq": 0, "kind": "subscription_resumed",
             "payload": {"resumed_after": after, "latest_seq": latest, "missed": missed}}
    yield f"event: control\ndata: {json.dumps(hello)}\n\n"

    sent = after
    while True:
        if event_log is None:  # the log may not exist yet at connect; pick it up once it does
            event_log = _engagement_event_log(engagement_id, create=False)
        if event_log is not None:
            for ev in event_log.read_since(sent):
                sent = ev["seq"]
                yield f"id: {ev['seq']}\ndata: {json.dumps(ev)}\n\n"
        if await is_disconnected():
            break
        await asyncio.sleep(poll)  # poll: sees a cross-process append within one slice


@app.get("/api/engagements/{engagement_id}/events")
async def engagement_events(engagement_id: str, request: Request, last_event_id: int | None = None):
    """The engagement-level event stream (`CLIENT_UI_DESIGN.md` §4), Server-Sent Events.

    One-way is all that is needed — commands go over REST — and `EventSource` gives reconnection
    and `Last-Event-ID` for free. On connect (and, since a reconnect is just another GET, again
    on resume) the subscription is authorised against the caller's account — §4.1.2's
    refuse-don't-resume boundary, enforced here *before* the stream body begins, so a reconnect
    carrying a `Last-Event-ID` for an engagement the caller no longer owns is refused, not
    resumed."""
    _check_api_version(request)
    _authorize_engagement_read(request, engagement_id)
    # Last-Event-ID header wins (that is the browser's reconnect path); the query param is the
    # explicit non-browser equivalent.
    header_id = request.headers.get("last-event-id")
    after = int(header_id) if header_id and header_id.isdigit() else (last_event_id or 0)
    return StreamingResponse(
        _sse_event_stream(engagement_id, after, request.is_disconnected),
        media_type="text/event-stream",
    )


def _hypothesis_graph_dir(engagement_id: str) -> Path | None:
    """The engagement dir IF it already has a hypothesis_graph.db — else None. Constructing a
    HypothesisGraphStore has a side effect (executescript() creates the file), so a plain GET on
    an engagement that never touched the graph must NOT bring one into existence. Same guard
    `agent.hypothesis_graph.cli` and the security_mcp_server's `state.db` check already use."""
    engagement_dir = config.ENGAGEMENTS_ROOT / engagement_id
    if not (engagement_dir / "hypothesis_graph.db").exists():
        return None
    return engagement_dir


@app.get("/api/engagements/{engagement_id}/hypothesis-graph")
def hypothesis_graph_overview(engagement_id: str, phase: str | None = None) -> dict:
    """Graph-canvas payload (docs/hypothesis-graph-ui-spec.md §2/§10): every node at summary
    weight + every edge + the active-path state. Empty (never 404) for an engagement that hasn't
    formed a hypothesis yet, so the panel can render a clean empty state."""
    engagement_dir = _hypothesis_graph_dir(engagement_id)
    if engagement_dir is None:
        return {"exists": False, "nodes": [], "edges": [], "graph_state": None}
    overview = HypothesisGraphService(engagement_dir).overview(current_phase=phase)
    return {"exists": True, **overview}


@app.get("/api/engagements/{engagement_id}/hypothesis-graph/nodes/{ordinal}")
def hypothesis_graph_node(engagement_id: str, ordinal: int) -> dict:
    """Detail-drawer payload (UI spec §5): full hypothesis fields, each attempt with its
    observations, and the direct children / synthesis targets."""
    engagement_dir = _hypothesis_graph_dir(engagement_id)
    if engagement_dir is None:
        raise HTTPException(404, f"engagement {engagement_id!r} has no hypothesis graph")
    try:
        return HypothesisGraphService(engagement_dir).node_detail(str(ordinal))
    except (NotFoundError, GraphValidationError) as e:
        raise HTTPException(404, str(e)) from e


class NotebookResolveRequest(BaseModel):
    action: str = "resolve"   # "resolve" | "reopen"
    reason: str = ""


@app.get("/api/engagements/{engagement_id}/notebook")
def notebook_overview(engagement_id: str) -> dict:
    """The Working Notebook read model (docs/working-notebook-spec.md §6). Empty (never 404) for
    an engagement that hasn't written a note yet — same don't-create-the-db-on-a-GET guard as the
    hypothesis-graph endpoints."""
    engagement_dir = config.ENGAGEMENTS_ROOT / engagement_id
    if not (engagement_dir / "notebook.db").exists():
        return {"exists": False, "notes": [], "counts": {}, "version": 0}
    return {"exists": True, **NotebookService(engagement_dir).overview()}


@app.get("/api/technique-kb")
def technique_kb() -> dict:
    """The global cross-engagement technique library (docs/working-notebook-spec.md §7).
    Not engagement-scoped — it's the operator's accumulated know-how."""
    kb = TechniqueKB()
    return {"techniques": kb.all(), "count": kb.count()}


@app.delete("/api/technique-kb/{ordinal}")
def technique_kb_forget(ordinal: int) -> dict:
    return {"deleted": TechniqueKB().delete(ordinal)}


@app.post("/api/engagements/{engagement_id}/notebook/notes/{ordinal}/resolve")
def notebook_resolve(engagement_id: str, ordinal: int, req: NotebookResolveRequest) -> dict:
    """Operator resolve/reopen for a todo or dead-end note. The only write the panel does —
    a note's text/category are append-only and model-owned."""
    engagement_dir = config.ENGAGEMENTS_ROOT / engagement_id
    if not (engagement_dir / "notebook.db").exists():
        raise HTTPException(404, f"engagement {engagement_id!r} has no notebook")
    svc = NotebookService(engagement_dir)
    reason = req.reason.strip()
    if req.action == "reopen" and not reason.lower().startswith("reopen:"):
        reason = f"reopen: {reason or 'operator reopened'}"
    if req.action == "resolve" and not reason:
        raise HTTPException(400, "resolving a note needs a reason")
    try:
        return svc.resolve_note(str(ordinal), reason)
    except NotebookNotFound as e:
        raise HTTPException(404, str(e)) from e
    except NotebookValidationError as e:
        raise HTTPException(400, str(e)) from e


class ReviewFindingRequest(BaseModel):
    reviewed_by: str


@app.get("/api/engagements/{engagement_id}/findings")
def list_findings(engagement_id: str) -> dict:
    """Findings + human-review status (see Finding.reviewed_by's own comment on why this exists:
    recorded by the model is not the same thing as reviewed by a human). Never 404 — an
    engagement with no findings.jsonl yet just has an empty list, same not-created-on-a-GET
    guard as the hypothesis-graph/notebook endpoints."""
    # findings_dir passed explicitly, not left to FindingsStore's own bound default — that
    # default is captured once at model.py's *import* time (config.FINDINGS_DIR's value then),
    # so a test's patch("agent.config.FINDINGS_DIR", ...) done afterward would silently miss it
    # otherwise (the exact bug class handoff.md's load-bearing-facts section warns about).
    findings = FindingsStore(engagement_id, findings_dir=config.FINDINGS_DIR).list_all()
    reviewed_count = sum(1 for f in findings if f.reviewed_by)
    return {
        "findings": [f.to_dict() for f in findings],
        "count": len(findings),
        "reviewed_count": reviewed_count,
    }


@app.post("/api/engagements/{engagement_id}/findings/{finding_id}/review")
def review_finding(engagement_id: str, finding_id: str, req: ReviewFindingRequest) -> dict:
    """The only write this surface allows — marking a finding human-reviewed. Everything else
    about a finding is model-written and immutable from here, same as the notebook panel's
    read-only-plus-resolve shape."""
    if not req.reviewed_by.strip():
        raise HTTPException(400, "reviewed_by must be a non-empty identity")
    try:
        finding = FindingsStore(
            engagement_id, findings_dir=config.FINDINGS_DIR
        ).mark_reviewed(finding_id, req.reviewed_by)
    except FindingNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    return finding.to_dict()


@app.post("/api/engagements/{engagement_id}/hypothesis-graph/nodes/{ordinal}/operator-action")
def hypothesis_graph_operator_action(engagement_id: str, ordinal: int, req: OperatorGraphActionRequest) -> dict:
    """Operator write-path (UI spec §7.4). Only the reversible, non-history mutations: park a
    branch (with a reason — it lands in the every-turn graph digest, so the model sees a human
    shelved it), reopen it, or attach a note/constraint. claim / evidence / verdict history stay
    append-only and model-owned. Graph mutations aren't target actions, so this does NOT go
    through the broker (same as the model's own graph_* tools)."""
    engagement_dir = _hypothesis_graph_dir(engagement_id)
    if engagement_dir is None:
        raise HTTPException(404, f"engagement {engagement_id!r} has no hypothesis graph")
    svc = HypothesisGraphService(engagement_dir)
    try:
        if req.action == "park":
            if not req.reason.strip():
                raise HTTPException(400, "parking requires a reason (and, ideally, a reopen condition)")
            return svc.park(str(ordinal), req.reason.strip(), actor="operator")
        if req.action == "reopen":
            return svc.reopen(str(ordinal), actor="operator")
        if req.action == "note":
            if not req.text.strip():
                raise HTTPException(400, "a note needs text")
            result = svc.add_note(str(ordinal), req.text.strip(), actor="operator")
            # The graph digest shows hypothesis lines, not the operator notes attached to them —
            # so also mirror it into the working notebook (refs: H-<n>), where build_context_block
            # pins it every turn. This is how mid-run operator steering actually reaches the model.
            try:
                hyp = svc.store.get_by_ordinal(ordinal)
                NotebookService(engagement_dir).add_operator_note(
                    req.text.strip(), refs=[f"H-{ordinal}"], surface=hyp.get("surface"),
                )
                result = {**result, "mirrored_to_notebook": True}
            except Exception:
                log.warning("failed to mirror operator note into the notebook", exc_info=True)
            return result
        raise HTTPException(400, f"unknown action {req.action!r} (park | reopen | note)")
    except NotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ConflictError as e:
        raise HTTPException(409, "the hypothesis changed under this action — reload the graph and retry") from e
    except GraphValidationError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/sessions/{session_id}/autonomous/start")
def start_autonomous(session_id: str, req: AutonomousStartRequest) -> dict:
    handle = _get_handle(session_id)
    if req.mode not in VALID_MODES:
        raise HTTPException(400, f"mode must be one of {VALID_MODES}, got {req.mode!r}")
    with handle.lock:
        if handle.autonomous_driver is not None and handle.autonomous_result is None:
            raise HTTPException(409, "an autonomous run is already active for this session")
        driver = AutonomousDriver(
            engagement_id=req.engagement_id, profile_name=req.profile, mode=req.mode,
            session_id=session_id, device_id="web-ui",
            # Nested, not spread (`**event`) — AutonomousDriver._emit()'s own event dicts
            # already carry their own "type" key (e.g. "phase_entered"), which would silently
            # clobber the outer "autonomous_event" envelope type if flattened together into one
            # dict. Found while wiring the frontend's dispatch, before it ever shipped.
            #
            # The same event also lands in the engagement-level event log (§4): the session queue
            # is per-session and in-memory, the engagement log is the one ordered, persistent,
            # engagement-wide stream every client surface subscribes to.
            on_event=_compose_sinks(
                lambda event: handle.push({"type": "autonomous_event", "event": event}),
                _engagement_event_sink(req.engagement_id),
            ),
        )
        handle.autonomous_driver = driver
        handle.autonomous_result = None

    def run() -> None:
        try:
            result = driver.run()
        except Exception as e:  # noqa: BLE001 - surfaced to the UI, never a silent thread death
            handle.push({"type": "autonomous_error", "error": f"{type(e).__name__}: {e}"})
            result = AutonomousRunResult("blocked", f"driver error: {e}")
        with handle.lock:
            handle.autonomous_result = result
        handle.push({
            "type": "autonomous_done", "status": result.status, "reason": result.reason,
            "phases_completed": result.phases_completed, "final_phase": result.final_phase,
        })

    threading.Thread(target=run, daemon=True).start()
    return {"status": "started", "mode": req.mode, "engagement_id": req.engagement_id}


def _task_entity_id(task: dict) -> str | None:
    if not task.get("params_json"):
        return None
    try:
        return json.loads(task["params_json"]).get("entity_id")
    except (json.JSONDecodeError, AttributeError):
        return None


@app.get("/api/sessions/{session_id}/autonomous/status")
def autonomous_status(session_id: str) -> dict:
    handle = _get_handle(session_id)
    with handle.lock:
        if handle.autonomous_driver is None:
            return {"active": False}
        result = handle.autonomous_result
        driver = handle.autonomous_driver
        # A live plan/todo view of the current phase — PipelineOrchestrator has always tracked
        # this (agent.engagement.store's tasks table), it just never reached the UI as anything
        # more than a scrolling per-task log line. Read fresh from the store on every poll
        # (cheap — a single indexed SELECT) rather than cached, so status flips (pending ->
        # running -> done) show up live without the driver having to push a duplicate event
        # stream just for this.
        current_phase = driver.store.get_phase()["current_phase"]
        tasks = [
            {
                "task_id": t["task_id"], "task_type": t["task_type"], "status": t["status"],
                "entity_id": _task_entity_id(t),
            }
            for t in driver.store.list_tasks(phase=current_phase)
        ]
        return {
            "active": result is None,
            "mode": driver.mode,
            "engagement_id": driver.engagement_id,
            "current_phase": current_phase,
            "tasks": tasks,
            "result": None if result is None else {
                "status": result.status, "reason": result.reason,
                "phases_completed": result.phases_completed, "final_phase": result.final_phase,
            },
        }


@app.post("/api/sessions/{session_id}/autonomous/stop")
def stop_autonomous(session_id: str) -> dict:
    handle = _get_handle(session_id)
    with handle.lock:
        if handle.autonomous_driver is None:
            raise HTTPException(404, "no autonomous run has been started for this session")
        handle.autonomous_driver.stop_event.set()
    return {"status": "stop_requested"}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
