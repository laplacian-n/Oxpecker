"""`isolated_state` must actually isolate, and what it cannot reach must be enumerated.

A test that writes into the operator's real `agent/state` leaves audit records and evidence
blobs that are indistinguishable from a live engagement's once they are in the same directory.
For a tool whose value rests on a trustworthy audit trail, that is not cosmetic — and nothing
reported it, because leaking is silent and the test still passes.

Two kinds of path escape a `patch("agent.config.X", ...)`:

  * a **frozen default argument** — `def __init__(self, p: Path = config.X)` binds whatever
    config held when the module was first imported. Eight of these were resolved at call time
    so that `isolated_state` reaches them; `agent/broker/broker.py` carries the note.
  * a **module-level constant** — `QUEUE_DIR = config.STATE_DIR / "broker" / "approval_queue"`
    is computed once at import and no amount of patching config moves it.

The second kind is what this file guards. `isolated_state` derives its redirect list from
`config`, so it covers config attributes completely and module constants not at all — and a
helper that silently covers less than it appears to is worse than no helper, because callers
stop checking. So the constants that still point into the real state dir are listed here, as a
shrink-only ratchet, rather than left to be discovered one leak at a time.
"""
from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path

from . import config
from .isolated_state import MIN_STATE_PATHS, isolated_state, state_path_attrs

# Module-level Path constants that still resolve under the real state dir while isolation is
# active. Each is a path `isolated_state` cannot redirect. Shrink only: the fix is to resolve
# the path from config at call time (see broker/approval_queue.py for the shape), not to add a
# line here.
UNREACHABLE_CONSTANTS = {
    "agent.recipes.store.RECIPES_DIR",
    "agent.skills.signing.SKILLS_KEY_PATH",
    "agent.broker.approval_queue.QUEUE_DIR",
    # Both resolve at call time already, but against their own module constant rather than
    # config, so patching config does not move them. CI's leak gate found these: the suite
    # writes agent/state/internet/{budgets,cache} there and not on a proxied machine, because
    # the test that reaches them needs real outbound network.
    "agent.internet.budget.BUDGETS_STATE_DIR",
    "agent.internet.cache.CACHE_DIR",
    "agent.web.control_panel.CERT_FILE",
    "agent.web.control_panel.CERT_KEY_FILE",
    "agent.web.control_panel.OPENROUTER_KEY_FILE",
    "agent.web.control_panel.PANEL_CONFIG_FILE",
    "agent.web.control_panel.TOKEN_FILE",
}

# Modules whose constants are checked. Imported explicitly so the scan does not depend on
# whatever else the suite happened to import first — an order-dependent guard is not a guard.
SCANNED = [
    "agent.broker.approval_queue", "agent.broker.taint", "agent.broker.kill_switch",
    "agent.broker.broker", "agent.broker.policy", "agent.evidence.store", "agent.audit_log",
    "agent.findings.model", "agent.trajectory.store", "agent.recipes.store",
    "agent.skills.signing", "agent.memory_service.db", "agent.memory_service.devices",
    "agent.web.control_panel", "agent.notebook.store", "agent.hypothesis_graph.store",
    "agent.web.debug_trace", "agent.web.dev_server",
    "agent.internet.budget", "agent.internet.cache",
]


class IsolatedStateTest(unittest.TestCase):
    maxDiff = None

    def test_it_redirects_every_config_path_under_the_state_dir(self):
        attrs = state_path_attrs()
        self.assertGreaterEqual(
            len(attrs), MIN_STATE_PATHS,
            f"only {len(attrs)} state paths found on config; a helper that redirects nothing "
            f"lets every caller leak while still passing",
        )
        real = Path(config.STATE_DIR).resolve()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with isolated_state(root):
                for name in attrs:
                    value = Path(getattr(config, name)).resolve()
                    self.assertTrue(
                        value == root or root in value.parents,
                        f"config.{name} is {value}, not under the isolation root {root}",
                    )
                    self.assertFalse(
                        value == real or real in value.parents,
                        f"config.{name} still points into the real state dir: {value}",
                    )
        # and it is restored on the way out, or it would poison everything that runs after.
        self.assertEqual(Path(config.STATE_DIR).resolve(), real)

    def test_the_paths_isolation_cannot_reach_are_the_ones_listed(self):
        for name in SCANNED:
            importlib.import_module(name)

        real = Path(config.STATE_DIR).resolve()
        with tempfile.TemporaryDirectory() as tmp:
            with isolated_state(Path(tmp)):
                stuck = set()
                for mod_name in SCANNED:
                    module = sys.modules[mod_name]
                    for attr in dir(module):
                        if attr.startswith("_") or not attr.isupper():
                            continue
                        value = getattr(module, attr, None)
                        if not isinstance(value, Path):
                            continue
                        resolved = Path(value).resolve()
                        if resolved == real or real in resolved.parents:
                            stuck.add(f"{mod_name}.{attr}")

        newly_stuck = sorted(stuck - UNREACHABLE_CONSTANTS)
        self.assertEqual(
            newly_stuck, [],
            "these module-level constants point into the real state dir and isolated_state "
            "cannot redirect them, so a test touching them writes into the operator's real "
            "state. Resolve the path from config at call time instead of adding a line to "
            f"UNREACHABLE_CONSTANTS: {newly_stuck}",
        )

        now_reachable = sorted(UNREACHABLE_CONSTANTS - stuck)
        self.assertEqual(
            now_reachable, [],
            "these are now reachable, so remove them from UNREACHABLE_CONSTANTS — a stale entry "
            f"hides the next one: {now_reachable}",
        )


if __name__ == "__main__":
    unittest.main()
