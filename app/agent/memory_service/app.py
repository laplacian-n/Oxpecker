"""FastAPI memory service — the "shared mind" (research doc §6).

Endpoint shape mirrors OpenAI Agents SDK's SQLiteSession (get_items/add_items/clear_session,
keyed by session_id) per the doc's own recommendation not to hand-roll this from zero, plus
list_sessions/get_session/condensed to match the §11 data model and Phase-2 exit criteria.

Auth is a separate credential/audience from llama-server's --api-key (§6). Device identity is
managed out-of-band via `agent/manage_devices.py`, not through this API — avoids a
bootstrapping chicken-and-egg (an endpoint that mints credentials would itself need a
credential to call).
"""
from __future__ import annotations

from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from .. import config
from . import db as db_mod
from .devices import DeviceStore

app = FastAPI(title="localai-memory-service")
store = db_mod.Store()
devices = DeviceStore()


def require_device(
    authorization: str | None = Header(default=None),
    x_device_id: str | None = Header(default=None, alias="X-Device-Id"),
) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if not x_device_id:
        raise HTTPException(status_code=401, detail="missing X-Device-Id header")
    token = authorization.removeprefix("Bearer ").strip()
    ok, reason = devices.verify(x_device_id, token)
    if not ok:
        raise HTTPException(status_code=401, detail=f"device auth failed: {reason}")
    return x_device_id


class Item(BaseModel):
    role: str
    content: str
    metadata: dict | None = None
    sensitivity_label: Literal["normal", "sensitive"] = "normal"


class AddItemsRequest(BaseModel):
    items: list[Item]
    expected_version: int


class CondensedRequest(BaseModel):
    content: str
    source_seq_start: int
    source_seq_end: int
    summarizer_config: str


@app.get("/sessions")
def list_sessions(device_id: str = Depends(require_device)):
    return {"sessions": store.list_sessions()}


@app.get("/sessions/{session_id}")
def get_session(session_id: str, device_id: str = Depends(require_device)):
    try:
        return store.get_session(session_id)
    except db_mod.NotFoundError:
        raise HTTPException(status_code=404, detail="session not found") from None


@app.get("/sessions/{session_id}/items")
def get_items(session_id: str, device_id: str = Depends(require_device)):
    return {"items": store.get_items(session_id)}


@app.post("/sessions/{session_id}/items")
def add_items(session_id: str, req: AddItemsRequest, device_id: str = Depends(require_device)):
    try:
        new_version = store.add_items(
            session_id,
            [i.model_dump() for i in req.items],
            origin_device=device_id,
            expected_version=req.expected_version,
        )
    except db_mod.ConflictError as e:
        raise HTTPException(
            status_code=409,
            detail={"message": "version conflict", "current_version": e.current_version},
        ) from None
    return {"version": new_version}


@app.get("/sessions/{session_id}/condensed")
def get_condensed(session_id: str, device_id: str = Depends(require_device)):
    result = store.get_condensed(session_id)
    if result is None:
        raise HTTPException(status_code=404, detail="no condensed memory for this session")
    return result


@app.put("/sessions/{session_id}/condensed")
def set_condensed(
    session_id: str, req: CondensedRequest, device_id: str = Depends(require_device)
):
    new_version = store.set_condensed(
        session_id, req.content, req.source_seq_start, req.source_seq_end, req.summarizer_config
    )
    return {"version": new_version}


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str, device_id: str = Depends(require_device)):
    deleted = store.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="session not found")
    return {"deleted": True}


@app.get("/health")
def health():
    return {"status": "ok"}
