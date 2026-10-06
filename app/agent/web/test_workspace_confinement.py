"""Tests for the file tools' workspace confinement, and for the bounds on caches that are
rewritten or held per session.

The confinement bug these were written for was real and verified before the fix: `read_file` and
`write_file` tested containment with `str(fp).startswith(str(DATA_DIR))`, a string-prefix test
rather than a containment test, so a sibling directory whose name merely began with the same
characters passed it. `dev_data_evil` read as inside `dev_data`, so the agent could read files
there and create new ones.

Run directly: `python3 -m agent.web.test_workspace_confinement`.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import tempfile
from unittest.mock import patch

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}  {detail if not condition else ''}")


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="oxpecker-confine-test-"))
    with patch("agent.config.AUDIT_DIR", tmp / "audit"), \
         patch("agent.config.EVIDENCE_KEY_PATH", tmp / "key.bin"), \
         patch("agent.config.EVIDENCE_DIR", tmp / "evidence"), \
         patch("agent.config.IDEMPOTENCY_CACHE_PATH", tmp / "idem.json"):
        from ..broker import broker as broker_mod
        from . import debug_trace as dt
        from . import dev_server as d

        dt.TRACE_DIR = tmp / "trace"
        s = d.Session(session_id="confine", engagement_id="lab-default",
                      isolation_tier="direct")

        d.DATA_DIR.mkdir(parents=True, exist_ok=True)
        sibling = d.DATA_DIR.parent / (d.DATA_DIR.name + "_evil")
        sibling.mkdir(parents=True, exist_ok=True)
        (sibling / "secret.txt").write_text("SIBLING CONTENTS")
        created = []
        try:
            print("\n== a sibling directory sharing the prefix is outside, and treated so ==")
            r = d._run_tool("read_file", {"path": f"../{sibling.name}/secret.txt"}, s)
            check("read_file refuses it", r.get("ok") is False, str(r)[:120])
            check("and does not leak the contents", "SIBLING CONTENTS" not in str(r), str(r)[:120])
            check("the reason says the path escaped",
                  "escapes the workspace" in str(r.get("error")), str(r.get("error"))[:110])

            target = sibling / "should-not-exist.txt"
            r = d._run_tool("write_file", {"path": f"../{sibling.name}/{target.name}",
                                           "content": "x"}, s)
            check("write_file refuses it", r.get("ok") is False, str(r)[:120])
            check("and creates nothing outside the workspace", not target.exists(),
                  f"{target} was created")

            print("\n== ordinary traversal is still blocked ==")
            for bad in ("../../../../etc/passwd", "../../..", "..", "a/../../outside.txt"):
                r = d._run_tool("read_file", {"path": bad}, s)
                check(f"{bad!r} refused", r.get("ok") is False, str(r)[:100])

            print("\n== a symlink pointing outside is blocked by resolution ==")
            link = d.DATA_DIR / "escape-link"
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(sibling / "secret.txt")
            created.append(link)
            r = d._run_tool("read_file", {"path": "escape-link"}, s)
            check("reading through the symlink is refused", r.get("ok") is False, str(r)[:110])
            check("and leaks nothing", "SIBLING CONTENTS" not in str(r), str(r)[:110])

            print("\n== legitimate paths inside the workspace still work ==")
            inside = d.DATA_DIR / "confine-note.txt"
            inside.write_text("inside the workspace")
            created.append(inside)
            r = d._run_tool("read_file", {"path": "confine-note.txt"}, s)
            check("a plain file reads", r.get("ok") is True and "inside" in r.get("content", ""),
                  str(r)[:110])
            r = d._run_tool("write_file", {"path": "sub/dir/new.txt", "content": "ok"}, s)
            check("a nested write succeeds", r.get("ok") is True, str(r)[:110])
            created.append(d.DATA_DIR / "sub")
            r = d._run_tool("read_file", {"path": "./sub/dir/new.txt"}, s)
            check("a './'-prefixed path reads", r.get("ok") is True, str(r)[:110])
        finally:
            shutil.rmtree(sibling, ignore_errors=True)
            for p in created:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                elif p.exists() or p.is_symlink():
                    p.unlink()

        print("\n== argv passed as a string is honoured, not reported as empty ==")
        r = d._run_tool("run_command", {"argv": "pwd"}, s)
        check("a string argv runs", r.get("ok") is True, str(r)[:120])
        r = d._run_tool("run_command", {"argv": []}, s)
        check("an empty argv is still refused with guidance",
              r.get("ok") is False and "argv" in str(r.get("error")), str(r)[:110])

        print("\n== a runner that raises reports no isolation tier ==")
        s2 = d.Session(session_id="confine-raise", engagement_id="lab-default",
                       isolation_tier="direct")
        with patch("agent.tools.run_command.run", side_effect=RuntimeError("boom")):
            r = d._run_tool("run_command", {"argv": ["pwd"]}, s2)
        check("the failure is reported", r.get("ok") is False, str(r)[:110])
        check("no tier is claimed for a command that did not complete",
              s2.effective_isolation_tier is None, str(s2.effective_isolation_tier))

        print("\n== the idempotency cache is bounded ==")
        # It is rewritten in full on every dispatch, and ActionRequest mints a fresh key per
        # request unless the caller supplies one, so unpruned it grows for the life of an install
        # and costs a larger serialization on every call.
        saved_cap = broker_mod.IDEMPOTENCY_CACHE_MAX_ENTRIES
        try:
            broker_mod.IDEMPOTENCY_CACHE_MAX_ENTRIES = 10
            b = broker_mod.Broker(
                policy_loader=lambda: d._scope.policy_from_engagement(
                    d._engagements["lab-default"]),
                confirm_fn=lambda prompt: False)

            class _Resp:
                def to_dict(self):
                    return {"stub": True}

            for i in range(30):
                b._save_idempotent(f"key-{i:03d}", _Resp())
            cache = json.loads((tmp / "idem.json").read_text())
            check("the cache stays at the cap", len(cache) <= 11, str(len(cache)))
            check("the oldest entries are the ones dropped",
                  "key-000" not in cache and "key-029" in cache, str(sorted(cache))[:110])
            check("no temp file is left behind", not (tmp / "idem.tmp").exists())
        finally:
            broker_mod.IDEMPOTENCY_CACHE_MAX_ENTRIES = saved_cap

        print("\n== per-session handle caches are bounded ==")
        saved_a, saved_t = d._AUDIT_LOG_CACHE_MAX, dt._TRACE_CACHE_MAX
        try:
            d._AUDIT_LOG_CACHE_MAX = dt._TRACE_CACHE_MAX = 5
            d._audit_logs.clear()
            dt._traces.clear()
            for i in range(20):
                d._audit_for(f"sess-{i}")
                dt.for_session(f"sess-{i}")
            check("the audit-log cache stays at the cap", len(d._audit_logs) <= 5,
                  str(len(d._audit_logs)))
            check("the trace cache stays at the cap", len(dt._traces) <= 5, str(len(dt._traces)))
            # Eviction must be safe: AuditLog re-reads the chain head from disk on every write,
            # so a rebuilt handle cannot fork the chain.
            from .. import audit_log as al
            sid = "sess-evicted"
            lg = d._audit_for(sid)
            for turn in range(2):
                lg.record(turn_index=turn, tool_name="t", action_rationale="r", arguments={},
                          raw_output="o", sanitized_output="o", scope_decision="allowed",
                          approval_identity=None, prompt_tokens=None, completion_tokens=None,
                          latency_ms=1.0, exit_code=0, injection_flagged=False)
            d._audit_logs.pop(sid, None)  # force a rebuild
            d._audit_for(sid).record(
                turn_index=2, tool_name="t", action_rationale="r", arguments={}, raw_output="o",
                sanitized_output="o", scope_decision="allowed", approval_identity=None,
                prompt_tokens=None, completion_tokens=None, latency_ms=1.0, exit_code=0,
                injection_flagged=False)
            ok, detail = al.verify(sid, audit_dir=tmp / "audit")
            check("the chain still verifies after a handle was evicted and rebuilt", ok, detail)
        finally:
            d._AUDIT_LOG_CACHE_MAX, dt._TRACE_CACHE_MAX = saved_a, saved_t

    print(f"\n{len(PASS)}/{len(PASS)+len(FAIL)} checks passed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
