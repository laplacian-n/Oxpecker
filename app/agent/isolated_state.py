"""Redirect every config path that writes under the state directory into a temp root.

A test that forgets one path writes into the operator's real `agent/state`, and nothing says so:
the test passes, and an audit record or an evidence blob from a test run is indistinguishable
from a real engagement's once it is sitting in the same directory. For a tool whose value rests
on a trustworthy audit trail and evidence store, that is not cosmetic.

Five script-style test files each hand-maintained their own `with patch(...)` list, and each
missed something different — evidence blobs in three of them, audit records, taint records and
queued approvals in the other two. The fix is not five corrected lists; a sixth file would
repeat it. `config` holds 22 Path attributes under STATE_DIR, so the list is derived from
`config` at call time instead: a state path added to config is isolated the day it is added,
without anyone remembering to come back here.

    with isolated_state(tmp):
        ...                       # every state write lands under tmp

Paths outside STATE_DIR (ENGAGEMENT_DIR, ENGAGEMENTS_ROOT, the knowledge index) are deliberately
not touched — they are inputs, and a caller that wants them redirected says so with `also=`.
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from unittest.mock import patch

from . import config

# If introspection finds fewer than this, it is broken, and a helper that patches nothing lets
# every caller leak while still passing — the exact failure it exists to prevent. Floored under
# the real count (22 at the time of writing) so adding or removing a path never trips it.
MIN_STATE_PATHS = 15


def state_path_attrs() -> dict[str, Path]:
    """Config attributes that are Paths under STATE_DIR, mapped to their location relative to it."""
    state_dir = Path(config.STATE_DIR).resolve()
    out: dict[str, Path] = {}
    for name in dir(config):
        if name.startswith("_"):
            continue
        value = getattr(config, name)
        if not isinstance(value, Path):
            continue
        try:
            out[name] = Path(value).resolve().relative_to(state_dir)
        except ValueError:
            continue  # outside the state dir: an input, not something a test writes
    return out


@contextlib.contextmanager
def isolated_state(root: Path, *, also: dict[str, Path] | None = None):
    """Patch every STATE_DIR-relative config path to the same place under `root`.

    `also` patches further config attributes by name, for the paths outside STATE_DIR a given
    test does want moved (typically ENGAGEMENTS_ROOT or ENGAGEMENT_DIR).
    """
    root = Path(root)
    attrs = state_path_attrs()
    if len(attrs) < MIN_STATE_PATHS:
        raise RuntimeError(
            f"only found {len(attrs)} state paths on agent.config (expected at least "
            f"{MIN_STATE_PATHS}); introspection is broken and this would isolate almost nothing"
        )

    root.mkdir(parents=True, exist_ok=True)
    patches = [patch.object(config, name, root / rel) for name, rel in attrs.items()]
    for name, value in (also or {}).items():
        patches.append(patch.object(config, name, Path(value)))

    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        for p in _module_state_patches(root):
            stack.enter_context(p)
        yield root


def _module_state_patches(root: Path) -> list:
    """Module-level state that holds a path and so survives patching `config`.

    Three of these were found by converting the script-style tests, and each produced a failure
    that looked like something else:

      * `dev_server._brokers` caches one Broker per engagement, and a Broker binds its evidence
        and idempotency paths at construction. A broker built under an earlier test's isolation
        root is reused after that root is deleted, so the next test's dispatches write nowhere
        and its assertions fail for reasons nothing in its own file explains.
      * `debug_trace.TRACE_DIR` is a module constant computed at import, so patching config does
        not move it — and one test deliberately sets it to an unwritable path to check the
        failure path, without restoring it. Everything that ran afterwards traced into
        `/proc/nonexistent/`.
      * `debug_trace._traces` is an in-memory cache keyed by session; a stale entry outlives the
        directory it describes.

    Only patched where the module is already imported — this must not drag the web runtime in
    for a caller that has nothing to do with it.
    """
    out = []
    dev_server = sys.modules.get("agent.web.dev_server")
    if dev_server is not None and hasattr(dev_server, "_brokers"):
        out.append(patch.object(dev_server, "_brokers", {}))

    debug_trace = sys.modules.get("agent.web.debug_trace")
    if debug_trace is not None:
        if hasattr(debug_trace, "TRACE_DIR"):
            out.append(patch.object(debug_trace, "TRACE_DIR", root / "debug_trace"))
        if hasattr(debug_trace, "_traces"):
            out.append(patch.object(debug_trace, "_traces", {}))
    return out
