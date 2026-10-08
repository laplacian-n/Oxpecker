"""Regression tests for four ways the broker could report something that did not happen.

Each of these was confirmed by execution before it was fixed, and each has the same shape: a
check that looked like it held, and a path where it silently did not. They are grouped because
they share the failure mode rather than the module.

Run directly: `python3 -m agent.broker.test_integrity`.
"""
from __future__ import annotations

import ipaddress
import json
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}  {detail if not condition else ''}")


def main() -> int:
    from .taint import TaintStore

    print("\n== the taint store is atomic, and an unreadable record fails closed ==")
    d = Path(tempfile.mkdtemp(prefix="taint-int-"))
    ts = TaintStore("s", taint_dir=d)
    check("a fresh session is clean", ts.is_tainted()[0] is False)
    ts.mark(reason="known_injection_phrasing", verdict="malicious", source="http_request")
    check("marking taints it", ts.is_tainted()[0] is True)

    good = ts.path.read_text()
    for name, content in [("a truncated file", good[: len(good) // 2]),
                          ("an empty file", ""),
                          ("whitespace only", "   \n"),
                          ("a JSON scalar", "42"),
                          ("a JSON list", "[]")]:
        ts.path.write_text(content)
        tainted, info = ts.is_tainted()
        check(f"{name} reads as TAINTED, not clean", tainted is True)
        check(f"and {name} says why", bool(info) and "could not be read" in info.get("reason", ""),
              str(info)[:90])

    ts.path.write_text(json.dumps({"tainted_until": time.time() + 300}))
    tainted, info = ts.is_tainted()
    check("a window with no history is tainted and still hands back a reason dict",
          tainted is True and isinstance(info, dict) and "reason" in info, str(info)[:90])

    ts.path.write_text(good)
    ts.clear()
    check("clear() really clears — an empty record is not confused with a torn one",
          ts.is_tainted()[0] is False)

    # The race the atomic write exists for: write_text truncates before writing, so a reader
    # landing in that window saw an empty file. Before the fix this counted in the thousands.
    TaintStore("race", taint_dir=d).mark(reason="i", verdict="malicious", source="h")
    stop, saw_clean = [False], [0]

    def writer():
        w = TaintStore("race", taint_dir=d)
        while not stop[0]:
            w.mark(reason="i", verdict="malicious", source="h")

    def reader():
        r = TaintStore("race", taint_dir=d)
        for _ in range(60000):
            if stop[0]:
                break
            if not r.is_tainted()[0]:
                saw_clean[0] += 1

    wt = threading.Thread(target=writer, daemon=True)
    rt = threading.Thread(target=reader)
    wt.start(); rt.start(); rt.join(timeout=20); stop[0] = True; wt.join(timeout=3)
    check("a concurrent reader never sees an actively-tainted session as clean",
          saw_clean[0] == 0, f"saw it clean {saw_clean[0]} times")
    check("and no temp files are left behind",
          not list(d.glob("*.tmp*")), str(list(d.glob('*.tmp*'))[:3]))

    print("\n== TLS verification off is classified as the harder action class, however it is spelled ==")
    # NOTE on import placement, learned the hard way while writing this file: `EvidenceStore`
    # captures `config.EVIDENCE_KEY_PATH` as a DEFAULT ARGUMENT (bound at import), while
    # `AuditLog` reads the same setting at call time. Import `agent.broker.broker` before
    # patching the config and the two resolve to different key files, so `_finalize`'s
    # audit/evidence digest cross-check fails for every dispatch — a test artefact that looks
    # exactly like a product bug. Everything that constructs a Broker is imported INSIDE the
    # patch block below for that reason.
    from .contracts import ActionRequest

    tmp = Path(tempfile.mkdtemp(prefix="broker-int-"))
    with patch("agent.config.AUDIT_DIR", tmp / "audit"), \
         patch("agent.config.EVIDENCE_KEY_PATH", tmp / "k.bin"), \
         patch("agent.config.EVIDENCE_DIR", tmp / "ev"), \
         patch("agent.config.EVIDENCE_INDEX_PATH", tmp / "ev-index.json"), \
         patch("agent.config.EVIDENCE_ACCESS_LOG_PATH", tmp / "ev-access.jsonl"), \
         patch("agent.config.STATE_DIR", tmp / "state"):
        from . import broker as broker_mod
        from .broker import _action_class_for  # noqa: F811 — see the import note above
        from .policy import Policy

        def pol():
            return Policy(
                engagement_id="int", allowed_action_classes={"active_scan_light"},
                allow_networks=[ipaddress.ip_network("127.0.0.1/32")], allow_hostnames=set(),
                deny_networks=[], deny_hostnames=set(),
                policy_version="t", valid_until=time.time() + 3600,
            )

        def cls(args):
            return _action_class_for(
                ActionRequest(tool="http_recon", arguments=args, session_id="s", device_id="d"))

        for value in (False, 0, "", "false", "False", "NO", "off", "none", None):
            check(f"verify_cert={value!r} is http_recon_insecure",
                  cls({"verify_cert": value}) == "http_recon_insecure",
                  str(cls({"verify_cert": value})))
        for value in (True, 1, "true", "yes", "on"):
            check(f"verify_cert={value!r} stays passive_recon",
                  cls({"verify_cert": value}) == "passive_recon",
                  str(cls({"verify_cert": value})))
        check("an absent verify_cert stays passive_recon", cls({}) == "passive_recon")

        def req(i):
            return ActionRequest(tool="port_discovery",
                                 arguments={"host": "127.0.0.1", "ports": [9]},
                                 session_id=f"s{i}", device_id="d", engagement_id="int")

        print("\n== concurrent dispatches all return a response; none raises after the fact ==")
        b = broker_mod.Broker(policy_loader=pol, confirm_fn=lambda p: True,
                              idempotency_cache_path=tmp / "idem.json")
        results, errors = [], []
        barrier = threading.Barrier(16)

        def one(i):
            barrier.wait()
            try:
                results.append(b.dispatch(req(i), lambda policy, a: {"ok": True}))
            except Exception as e:  # noqa: BLE001 — the bug WAS an unexpected exception type
                errors.append(f"{type(e).__name__}: {e}")

        threads = [threading.Thread(target=one, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("every dispatch returned an ActionResponse rather than raising",
              not errors, f"{len(errors)} raised: {errors[:2]}")
        check("all 16 are accounted for", len(results) == 16, str(len(results)))
        check("no shared temp file is left in the cache directory",
              not list((tmp / "idem.json").parent.glob("idem.json.tmp*")))

        cache = json.loads((tmp / "idem.json").read_text())
        check("and every dispatch's entry survived the read-modify-write",
              len(cache) == 16, f"{len(cache)} of 16 entries")

        print("\n== disengaging the kill switch mid-check denies rather than crashing ==")
        b2 = broker_mod.Broker(policy_loader=pol, confirm_fn=lambda p: True,
                               idempotency_cache_path=tmp / "idem2.json")
        with patch.object(b2.kill_switch, "is_engaged", lambda: True), \
             patch.object(b2.kill_switch, "status", lambda: None):
            try:
                r = b2.dispatch(req(99), lambda policy, a: {"ok": True})
                check("it returns a denial", r.status == "denied", r.status)
                check("attributed to the kill switch", r.policy_rule == "kill_switch",
                      r.policy_rule)
            except Exception as e:  # noqa: BLE001
                check("it returns a denial", False, f"raised {type(e).__name__}: {e}")

        print("\n== a tainted session with no history denies instead of raising ==")
        from .taint import TaintStore as TS
        sid = f"nohist-{time.time_ns()}"
        store = TS(sid, taint_dir=tmp / "state" / "broker" / "taint")
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text(json.dumps({"tainted_until": time.time() + 300}))
        b3 = broker_mod.Broker(policy_loader=pol, confirm_fn=lambda p: False,
                               idempotency_cache_path=tmp / "idem3.json")
        with patch.object(broker_mod, "TaintStore", lambda s: TS(
                s, taint_dir=tmp / "state" / "broker" / "taint")):
            try:
                r = b3.dispatch(
                    ActionRequest(tool="port_discovery",
                                  arguments={"host": "127.0.0.1", "ports": [9]},
                                  session_id=sid, device_id="d", engagement_id="int"),
                    lambda policy, a: {"ok": True})
                check("it returns a denial, not a TypeError", r.status == "denied", r.status)
                check("attributed to the taint escalation",
                      r.policy_rule == "approval_required_taint", r.policy_rule)
            except Exception as e:  # noqa: BLE001
                check("it returns a denial, not a TypeError", False,
                      f"raised {type(e).__name__}: {e}")

    print(f"\n{len(PASS)}/{len(PASS)+len(FAIL)} checks passed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
