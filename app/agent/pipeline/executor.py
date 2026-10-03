"""M5.3 -> execution: the piece the orchestrator's own docstring flagged as separate, larger
integration work — connecting a planned task to an actual broker-mediated tool call and feeding
the result back into engagement state (services/observations), then marking the task done/failed.

Deliberately a small, explicit per-task_type dispatch table (`TASK_EXECUTORS`), not a generic
"call anything" mechanism — only a handful of task types exist (`agent/pipeline/profiles.py`) and
each needs different argument resolution from state. `review_observations` and
`validate_hypothesis` are NOT in this table on purpose: forming a hypothesis from raw
observations, and deciding how to test one, are exactly the judgment calls this project's own
Phase 5 design says should stay with the model rather than being faked by a deterministic
shortcut — `run_pending_tasks()` leaves those tasks pending and reports them as
"requires model reasoning," it does not invent a fake automated answer for them.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable

from .. import config
from ..broker.broker import Broker
from ..broker.contracts import ActionRequest
from ..findings.model import FindingsStore
from ..findings.report import render_markdown
from ..security_tools import http_recon, port_discovery
from .orchestrator import PipelineOrchestrator

DEFAULT_PORTS = [21, 22, 25, 53, 80, 110, 143, 443, 445, 3000, 3080, 3306, 3389, 5432, 8080, 8443]
TASKS_REQUIRING_MODEL = {"review_observations", "validate_hypothesis"}

# Which of DEFAULT_PORTS actually speak HTTP(S) — used to derive an asset's metadata.url from
# what port_discovery actually found open, never guessed blind. https preferred over http when
# both are open; within a scheme, the conventional port (443/80) preferred over an alternate one.
_HTTPS_PORTS = (443, 8443)
_HTTP_PORTS = (80, 3000, 3080, 8080)


def _url_from_open_ports(host: str, open_ports: list[int]) -> str | None:
    open_set = set(open_ports)
    for port in _HTTPS_PORTS:
        if port in open_set:
            return f"https://{host}/" if port == 443 else f"https://{host}:{port}/"
    for port in _HTTP_PORTS:
        if port in open_set:
            return f"http://{host}/" if port == 80 else f"http://{host}:{port}/"
    return None


@dataclass
class ExecutionOutcome:
    task_id: str
    task_type: str
    ok: bool
    detail: str
    skipped: bool = False


def _params(task: dict) -> dict:
    return json.loads(task["params_json"]) if task["params_json"] else {}


def _find_asset(orch: PipelineOrchestrator, asset_id: str) -> dict | None:
    return next((a for a in orch.store.list_assets() if a["asset_id"] == asset_id), None)


def _execute_port_discovery(
    orch: PipelineOrchestrator, broker: Broker, session_id: str, task: dict
) -> ExecutionOutcome:
    asset_id = _params(task).get("entity_id")
    asset = _find_asset(orch, asset_id)
    if asset is None:
        return ExecutionOutcome(task["task_id"], task["task_type"], False, f"asset {asset_id!r} not found")

    req = ActionRequest(
        tool="port_discovery", arguments={"host": asset["identifier"], "ports": DEFAULT_PORTS},
        session_id=session_id, device_id="pipeline-executor",
    )
    resp = broker.dispatch(
        req, executor=lambda pol, args: port_discovery.run(args["host"], args["ports"], pol)
    )
    if resp.status != "succeeded":
        return ExecutionOutcome(task["task_id"], task["task_type"], False, f"{resp.status}: {resp.detail}")

    open_ports = resp.output.get("open_ports", [])
    for port in open_ports:
        orch.store.upsert_service(asset_id, port, "tcp")
    orch.store.add_observation(
        "port_scan_result", json.dumps(resp.output), "port_discovery",
        asset_id=asset_id, evidence_ref=resp.evidence_digest,
    )
    # Give the http_recon task (planned unconditionally alongside this one, per profiles.py) a
    # URL to work with — derived from what actually answered, not guessed at asset-creation time.
    # No open HTTP(S) port -> no metadata.url -> _execute_http_recon fails with a clear reason
    # rather than this silently making one up. Never overwrites a URL someone already set
    # (explicitly at asset creation, or by a prior scan) — a rescan finding a *different* port
    # open should not clobber a deliberately-chosen one.
    existing_metadata = json.loads(asset["metadata_json"]) if asset.get("metadata_json") else {}
    if "url" not in existing_metadata:
        url = _url_from_open_ports(asset["identifier"], open_ports)
        if url:
            orch.store.upsert_asset(asset["asset_type"], asset["identifier"], metadata={"url": url})
    return ExecutionOutcome(task["task_id"], task["task_type"], True, f"succeeded: {resp.policy_rule}")


def _execute_http_recon(
    orch: PipelineOrchestrator, broker: Broker, session_id: str, task: dict
) -> ExecutionOutcome:
    asset_id = _params(task).get("entity_id")
    asset = _find_asset(orch, asset_id)
    if asset is None:
        return ExecutionOutcome(task["task_id"], task["task_type"], False, f"asset {asset_id!r} not found")

    metadata = json.loads(asset["metadata_json"]) if asset.get("metadata_json") else {}
    url = metadata.get("url")
    if not url:
        return ExecutionOutcome(
            task["task_id"], task["task_type"], False,
            f"no known URL for asset {asset_id!r} — set metadata={{'url': ...}} when the asset "
            "is created (agent.engagement.store.upsert_asset), this executor never guesses one",
        )

    req = ActionRequest(
        tool="http_recon", arguments={"url": url, "verify_cert": True},
        session_id=session_id, device_id="pipeline-executor",
    )
    resp = broker.dispatch(
        req, executor=lambda pol, args: http_recon.run(args["url"], pol, args["verify_cert"])
    )
    if resp.status != "succeeded":
        return ExecutionOutcome(task["task_id"], task["task_type"], False, f"{resp.status}: {resp.detail}")

    orch.store.add_observation(
        "http_recon_result", json.dumps(resp.output), "http_recon",
        asset_id=asset_id, evidence_ref=resp.evidence_digest,
    )
    return ExecutionOutcome(task["task_id"], task["task_type"], True, f"succeeded: {resp.policy_rule}")


def _execute_service_fingerprint(
    orch: PipelineOrchestrator, broker: Broker, session_id: str, task: dict
) -> ExecutionOutcome:
    service_id = _params(task).get("entity_id")
    services = orch.store.list_services()
    service = next((s for s in services if s["service_id"] == service_id), None)
    if service is None:
        return ExecutionOutcome(task["task_id"], task["task_type"], False, f"service {service_id!r} not found")
    asset = _find_asset(orch, service["asset_id"])
    if asset is None:
        return ExecutionOutcome(task["task_id"], task["task_type"], False, f"asset for service {service_id!r} not found")

    req = ActionRequest(
        tool="port_discovery", arguments={"host": asset["identifier"], "ports": [service["port"]]},
        session_id=session_id, device_id="pipeline-executor",
    )
    resp = broker.dispatch(
        req, executor=lambda pol, args: port_discovery.run(args["host"], args["ports"], pol)
    )
    if resp.status != "succeeded":
        return ExecutionOutcome(task["task_id"], task["task_type"], False, f"{resp.status}: {resp.detail}")

    orch.store.add_observation(
        "service_reconfirm_result", json.dumps(resp.output), "port_discovery",
        asset_id=service["asset_id"], service_id=service_id, evidence_ref=resp.evidence_digest,
    )
    return ExecutionOutcome(task["task_id"], task["task_type"], True, f"succeeded: {resp.policy_rule}")


def _execute_generate_report(
    orch: PipelineOrchestrator, broker: Broker, session_id: str, task: dict
) -> ExecutionOutcome:
    engagement_id = orch.store.db_path.parent.name
    findings = FindingsStore(engagement_id).list_all()
    markdown = render_markdown(findings, engagement_id)
    report_path = orch.store.db_path.parent / "report.md"
    report_path.write_text(markdown)
    # Found live (2026-09-01, first real end-to-end pipeline run): FindingsStore (what the
    # report above is rendered from) and EngagementStore's own findings_lifecycle table (what
    # close_engagement() counts as closed/outstanding) are two disconnected systems — nothing
    # bridged them, so a real run produced a report with 1 finding and a closeout summary of "0
    # closed, 0 outstanding" moments later, silently wrong. generate_report is the one place
    # that already sees every finding going into the report, so it links each one here — a
    # singleton per-engagement task (TaskTemplate.per == "engagement"), so this runs once per
    # engagement under normal pipeline flow, not repeatedly re-stomping a later "closed" status.
    for f in findings:
        orch.store.link_finding(f.finding_id, status="reported")
    return ExecutionOutcome(
        task["task_id"], task["task_type"], True,
        f"wrote {report_path} ({len(findings)} finding(s))",
    )


def _execute_closeout(
    orch: PipelineOrchestrator, broker: Broker, session_id: str, task: dict
) -> ExecutionOutcome:
    result = orch.store.close_engagement(closed_by="pipeline-executor", summary="automated closeout")
    return ExecutionOutcome(
        task["task_id"], task["task_type"], True,
        f"closed: {result['findings_closed_count']} closed, {result['outstanding_count']} outstanding",
    )


TASK_EXECUTORS: dict[str, Callable[[PipelineOrchestrator, Broker, str, dict], ExecutionOutcome]] = {
    "port_discovery": _execute_port_discovery,
    "http_recon": _execute_http_recon,
    "service_fingerprint": _execute_service_fingerprint,
    "generate_report": _execute_generate_report,
    "closeout": _execute_closeout,
}


def run_pending_tasks(
    orch: PipelineOrchestrator, broker: Broker, session_id: str, max_tasks: int | None = None
) -> list[ExecutionOutcome]:
    """Executes every pending task in the current phase that has a deterministic executor,
    updating its status (done/failed) and version in the state store. Tasks in
    TASKS_REQUIRING_MODEL are left pending and reported as skipped — this function never
    fabricates a hypothesis or a validation result on the model's behalf."""
    phase = orch.store.get_phase()["current_phase"]
    tasks = [t for t in orch.store.list_tasks(phase=phase) if t["status"] == "pending"]
    outcomes: list[ExecutionOutcome] = []
    # The broker rate-limits each (session_id, action_class) pair (agent/broker/broker.py's
    # ACTION_CLASS_COOLDOWN_S) — a real, correct control for a single ad hoc call, but a batch
    # of same-phase tasks dispatched back-to-back through one session_id would otherwise trip it
    # on the second call. Pacing dispatches here (rather than loosening the broker's cooldown)
    # keeps the rate limit meaningful for everything else that shares it.
    cooldown_s = max(config.ACTION_CLASS_COOLDOWN_S.values(), default=1.0)
    dispatched_any = False
    for task in tasks:
        if max_tasks is not None and len(outcomes) >= max_tasks:
            break
        task_type = task["task_type"]
        if task_type in TASKS_REQUIRING_MODEL:
            outcomes.append(
                ExecutionOutcome(task["task_id"], task_type, False, "requires model reasoning, not executed", skipped=True)
            )
            continue
        executor = TASK_EXECUTORS.get(task_type)
        if executor is None:
            outcomes.append(
                ExecutionOutcome(task["task_id"], task_type, False, f"no executor registered for task_type {task_type!r}", skipped=True)
            )
            continue
        if dispatched_any:
            time.sleep(cooldown_s)
        dispatched_any = True
        orch.store.update_task(task["task_id"], expected_version=task["version"], status="running")
        outcome = executor(orch, broker, session_id, task)
        orch.store.update_task(
            task["task_id"], expected_version=task["version"] + 1,
            status="done" if outcome.ok else "failed",
            result={"detail": outcome.detail},
        )
        outcomes.append(outcome)
    return outcomes
