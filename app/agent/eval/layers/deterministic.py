"""Layer 1 — deterministic regression: policy, sandbox, audit, evidence. Each of these already
has its own committed, real test suite (built and tested in earlier Phase-3/4 work); this layer
just runs them as subprocesses (not re-implementing their logic) and aggregates pass/fail into
one machine-readable result, per 01-security-agent-main-direction.md M4.4's four-layer eval
design. Subprocess, not import-and-call-main(), deliberately — these modules accumulate PASS/FAIL
into module-level globals, so importing and calling main() twice in one process would silently
double-count; a fresh subprocess per suite matches how a human actually runs them and needs no
global-state bookkeeping.
"""
from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass

SUITES = [
    ("policy_bypass", "agent.broker.test_policy_bypass"),
    ("sandbox_isolation", "agent.sandbox.test_isolation"),
    ("evidence_lifecycle", "agent.evidence.test_lifecycle"),
    ("injection_quarantine", "agent.broker.test_injection_quarantine"),
]

_COUNT_RE = re.compile(r"(\d+)/(\d+) checks passed")


@dataclass
class SuiteResult:
    suite_id: str
    passed_count: int
    total_count: int
    exit_code: int
    ok: bool


def run(timeout_s: float = 120) -> list[SuiteResult]:
    results = []
    for suite_id, module in SUITES:
        proc = subprocess.run(
            [sys.executable, "-m", module], capture_output=True, text=True, timeout=timeout_s
        )
        m = _COUNT_RE.search(proc.stdout)
        passed, total = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        results.append(
            SuiteResult(
                suite_id=suite_id,
                passed_count=passed,
                total_count=total,
                exit_code=proc.returncode,
                ok=proc.returncode == 0 and passed == total and total > 0,
            )
        )
        print(f"  [{'PASS' if results[-1].ok else 'FAIL'}] {suite_id}: {passed}/{total}")
    return results
