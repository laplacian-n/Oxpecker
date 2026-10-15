"""The request bodies the dev server's routes accept, as Pydantic models.

Lifted out of dev_server.py unchanged. Each class is a field declaration with builtin types
only: none of them reads module-level state, references Session, or imports dev_server, which is
what made them safe to move. The one thing this file does NOT own is the routes that take them;
those stay in dev_server.py and see these names through the re-export there, so
`dev_server.<Name>` keeps resolving for the tests and for the route signatures.

See test_module_split_hazard.py for why the state-bearing names must not move the same way.
"""
from __future__ import annotations

from pydantic import BaseModel


class CreateSessionRequest(BaseModel):
    profile: str = "safe"
    use_security_tools: bool = False
    engagement_id: str = "lab-default"
    dangerous_local: bool = False
    # "cloud" (run on the server, sandboxed) or "this_computer" (run on the operator's desktop
    # app, from their machine's network). Scope is enforced on the server either way.
    isolation: str = "cloud"
    isolation_tier: str = "bubblewrap"
    thinking: bool = False
    # Optional custom system prompt for the assistant (chat) path — for testing how the prompt
    # affects the model's willingness on authorised tasks. Blank uses DEFAULT_SYSTEM_PROMPT. It
    # never relaxes the server-side gates (scope, broker, deny list, injection guard), which run
    # regardless of what the prompt says.
    system_prompt: str = ""


class MessageRequest(BaseModel):
    content: str
    guided: bool | None = None   # follow the staged pentest protocol for this turn
    stage: str | None = None     # operator-set stage for this turn (overrides detection)


class StageRequest(BaseModel):
    stage: str


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
    allow_targets: list[str] = []
    deny_targets: list[str] = []
    program: dict | None = None
    allowed_action_classes: list[str] = []
    authorized_by: str = ""
    valid_hours: float = 24.0


class OperatorGraphActionRequest(BaseModel):
    action: str
    reason: str = ""
    text: str = ""


class AutonomousStartRequest(BaseModel):
    engagement_id: str
    profile: str = "web_api"
    mode: str
    user_message: str = ""
    # Orchestration tier for this run (low|medium|high): low = one agent, no strategist/graph, just
    # context + tool calls; high = the full parallel wave design. None uses the engagement default.
    tier: str | None = None


class ReviewFindingRequest(BaseModel):
    reviewed_by: str


class NotebookResolveRequest(BaseModel):
    action: str = "resolve"
    reason: str = ""


class ClientJobResultRequest(BaseModel):
    result: dict


class ThinkingRequest(BaseModel):
    thinking: bool


class EngagementRunRequest(BaseModel):
    mode: str = "autonomous"
    profile: str = "web_api"
    user_message: str = ""
    tier: str | None = None
