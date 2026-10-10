"""Render Finding objects to SARIF 2.1.0 (OASIS) — a rendering target, not the native format
(§8d: "Don't make SARIF native, poor fit for narrative findings — render *to* it")."""
from __future__ import annotations

from .model import Finding, can_submit, submission_blocker

_SEVERITY_TO_LEVEL = {
    "critical": "error",
    "high": "error",
    "medium": "warning",
    "low": "note",
    "info": "note",
}


def render_sarif(findings: list[Finding], tool_name: str = "localai-security-agent") -> dict:
    rule_ids_seen: dict[str, dict] = {}
    results = []
    for f in findings:
        rule_id = f.tool
        if rule_id not in rule_ids_seen:
            rule_ids_seen[rule_id] = {
                "id": rule_id,
                "shortDescription": {"text": f"Findings produced by the {rule_id} tool"},
            }
        results.append(
            {
                "ruleId": rule_id,
                "level": _SEVERITY_TO_LEVEL[f.severity],
                "message": {"text": f"{f.title}\n\n{f.description}"},
                "locations": [
                    {"physicalLocation": {"artifactLocation": {"uri": f.target}}}
                ],
                "properties": {
                    "finding_id": f.finding_id,
                    "severity": f.severity,
                    "confidence": f.confidence,
                    "status": f.status,
                    "demonstrated_impact": f.demonstrated_impact,
                    "cvss": f.cvss,
                    "cvss_version": f.cvss_version,
                    "cvss_vector": f.cvss_vector,
                    "cwe_ids": f.cwe_ids,
                    "cve_ids": f.cve_ids,
                    "affected_component": f.affected_component,
                    "preconditions": f.preconditions,
                    "remediation": f.remediation,
                    "observation_refs": f.observation_refs,
                    "evidence_refs": f.evidence_refs,
                    "audit_refs": f.audit_refs,
                    "reproduction_recipe_ref": f.reproduction_recipe_ref,
                    "references": f.references,
                    "first_seen": f.first_seen,
                    "last_verified": f.last_verified,
                    "verifier": f.verifier,
                    "limitations": f.limitations,
                    "discovered_at": f.discovered_at,
                    "reviewed_by": f.reviewed_by,
                    "reviewed_at": f.reviewed_at,
                    # §8.6.4 / §8.6.5 #1 — assurance level and the submission boundary's verdict.
                    "confirmed_by": f.confirmed_by,
                    "rule_disagreed": f.rule_disagreed,
                    "submittable": can_submit(f),
                    "submission_blocker": submission_blocker(f),
                },
            }
        )

    return {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": tool_name, "rules": list(rule_ids_seen.values())}},
                "results": results,
            }
        ],
    }
