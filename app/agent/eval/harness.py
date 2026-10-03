"""Evaluation harness — §8d/§12 Phase-4 exit criterion: "benchmark results are reproducible
and reported with model, template, tool, target, seed, and policy versions." Four layers per
01-security-agent-main-direction.md M4.4 (all three review documents converge on this
structure): deterministic regression, model/tool under ambiguity (multi-seed), security
reasoning proxies, and end-to-end milestones against two independent live lab targets — not a
single "3/3 on one seed" number, which every reviewer independently flagged as insufficient
capability evidence.

Run: `python3 -m agent.eval.harness` (needs the .venv — MCP/security tools — and Juice Shop +
DVWA up). Takes several minutes: many real model calls across 4 layers and multiple seeds.
"""
from __future__ import annotations

import json
import platform
import time

from .. import config
from ..broker.policy import load_policy
from ..llama_client import LlamaClient
from ..prompts.compiler import compile_prompt
from .layers import deterministic, milestone, model_tool, security_reasoning


def _model_metadata(client: LlamaClient) -> dict:
    props = client.props()
    return {
        "model_path": props.get("model_path"),
        "model_ftype": props.get("model_ftype"),
        "n_ctx": props.get("default_generation_settings", {}).get("n_ctx"),
        "chat_template_hash": hash(props.get("chat_template", "")),  # cheap change-detector
    }


def _classify_failure(status: str) -> str:
    """A coarse but honest failure taxonomy — "it failed" isn't useful on its own; whether it
    failed because of infrastructure, the loop's own guards, or the model's judgment changes
    what should be fixed next. Deliberately conservative: only the statuses the loop itself
    reports are classified with confidence; everything else is left as model_judgment_error
    rather than guessed at more specifically than the evidence supports."""
    if status == "budget_exhausted":
        return "infrastructure_or_budget"
    if status == "loop_break":
        return "loop_detection_triggered"
    if status != "ok":
        return f"unexpected_status:{status}"
    return "model_judgment_error"


def run(engagement_id: str = "lab-default") -> dict:
    client = LlamaClient()
    model_meta = _model_metadata(client)
    policy = load_policy()
    started = time.time()

    print("== Layer 1: deterministic regression ==")
    layer1 = deterministic.run()

    print("\n== Layer 2: model/tool under ambiguity (multi-seed) ==")
    layer2 = model_tool.run(engagement_id=engagement_id)

    print("\n== Layer 3: security reasoning proxies ==")
    layer3 = security_reasoning.run(engagement_id=engagement_id)

    print("\n== Layer 4: end-to-end milestones (Juice Shop + DVWA) ==")
    layer4 = milestone.run(engagement_id=engagement_id)

    failure_taxonomy: dict[str, int] = {}
    for r in layer2["results"] + layer3 + layer4:
        if not r["passed"]:
            reason = _classify_failure(r["status"])
            failure_taxonomy[reason] = failure_taxonomy.get(reason, 0) + 1

    l1_pass = sum(1 for s in layer1 if s.ok)
    l2_pass = layer2["summary"]["passed_trials"]
    l2_total = layer2["summary"]["total_trials"]
    l3_pass = sum(1 for r in layer3 if r["passed"])
    l4_pass = sum(1 for r in layer4 if r["passed"])

    report = {
        "run_id": f"eval-{int(time.time())}",
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "duration_s": round(time.time() - started, 1),
        "reproducibility": {
            "model": model_meta,
            "seeds_used": model_tool.SEEDS,
            "temperature": 0.7,
            "policy_version": policy.policy_version,
            "prompt_version": compile_prompt(
                {"workspace_root": "<eval-task-workspace>", "security_tools_enabled": True}
            ).digest,
            "engagement_id": engagement_id,
            "targets": [
                "http://127.0.0.1:3000/ (OWASP Juice Shop)",
                "http://127.0.0.1:3080/ (DVWA)",
            ],
            "tools_tested": ["http_recon", "port_discovery", "run_command"],
            "host": platform.node(),
        },
        "layer_1_deterministic_regression": {
            "suites": [
                {"suite_id": s.suite_id, "passed": s.passed_count, "total": s.total_count, "ok": s.ok}
                for s in layer1
            ],
            "summary": f"{l1_pass}/{len(layer1)} suites fully passing",
        },
        "layer_2_model_tool_ambiguity": layer2,
        "layer_3_security_reasoning": layer3,
        "layer_4_milestones": layer4,
        "failure_taxonomy": failure_taxonomy,
        "summary": {
            "layer_1_suites_passing": f"{l1_pass}/{len(layer1)}",
            "layer_2_trials_passing": f"{l2_pass}/{l2_total}",
            "layer_3_tasks_passing": f"{l3_pass}/{len(layer3)}",
            "layer_4_milestones_passing": f"{l4_pass}/{len(layer4)}",
        },
    }

    config.EVAL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = config.EVAL_RESULTS_DIR / f"{report['run_id']}.json"
    out_path.write_text(json.dumps(report, indent=2))
    print(f"\nLayer 1 (deterministic):        {report['summary']['layer_1_suites_passing']} suites")
    print(f"Layer 2 (model/tool, multi-seed): {report['summary']['layer_2_trials_passing']} trials")
    print(f"Layer 3 (security reasoning):    {report['summary']['layer_3_tasks_passing']} tasks")
    print(f"Layer 4 (milestones):            {report['summary']['layer_4_milestones_passing']} milestones")
    if failure_taxonomy:
        print(f"Failure taxonomy: {failure_taxonomy}")
    print(f"report written to {out_path}")
    return report


if __name__ == "__main__":
    run()
