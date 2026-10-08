"""Tests for the isolation-availability probe. The property that matters is not "the probe
returns something" but "an unavailable tier refuses to run instead of quietly becoming a weaker
one" — that silent degradation is the specific defect this module was written to prevent, so
most checks below assert a raise, not a value.

Runs on any host: the bubblewrap-present and bubblewrap-absent paths are both asserted against
whatever this host actually is, by reading the probe's own verdict first rather than assuming
one. Run directly: `python3 -m agent.sandbox.test_availability`.
"""
from __future__ import annotations

import platform

from . import availability as av

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}  {detail if not condition else ''}")


def main() -> int:
    print("\n== direct is always available, and says it provides no isolation ==")
    ok, reason = av.probe(av.TIER_DIRECT)
    check("direct probes available", ok, f"reason={reason!r}")
    check(
        "direct's description does not claim isolation",
        "no kernel isolation" in av.TIER_DESCRIPTION[av.TIER_DIRECT],
        av.TIER_DESCRIPTION[av.TIER_DIRECT],
    )

    print("\n== microvm is unavailable and never degrades ==")
    ok, reason = av.probe(av.TIER_MICROVM)
    check("microvm probes unavailable", not ok, f"reason={reason!r}")
    try:
        av.resolve_tier(av.TIER_MICROVM)
        check("resolve_tier('microvm') raises", False, "returned instead of raising")
    except av.IsolationUnavailableError:
        check("resolve_tier('microvm') raises IsolationUnavailableError", True)

    print("\n== unknown tier names are rejected, not guessed ==")
    for bad in ("nonsense", "", "BUBBLEWRAP", "bwrap"):
        try:
            av.probe(bad)
            check(f"probe({bad!r}) raises ValueError", False, "returned instead of raising")
        except ValueError:
            check(f"probe({bad!r}) raises ValueError", True)
        try:
            av.resolve_tier(bad)
            check(f"resolve_tier({bad!r}) raises ValueError", False, "returned")
        except ValueError:
            check(f"resolve_tier({bad!r}) raises ValueError", True)
        except av.IsolationUnavailableError:
            check(f"resolve_tier({bad!r}) raises ValueError not IsolationUnavailable", False,
                  "wrong exception type — an unknown name is a caller bug, not a host condition")

    print("\n== bubblewrap: whichever way this host falls, the verdict is consistent ==")
    bw_ok, bw_reason = av.probe(av.TIER_BUBBLEWRAP)
    check("bubblewrap probe returns a non-empty reason either way", bool(bw_reason),
          f"ok={bw_ok} reason={bw_reason!r}")

    if platform.system() != "Linux":
        check("non-Linux host reports bubblewrap unavailable", not bw_ok, f"reason={bw_reason!r}")
        check("the reason names the platform", platform.system() in bw_reason, bw_reason)

    if bw_ok:
        tier, reason = av.resolve_tier(av.TIER_BUBBLEWRAP)
        check("available bubblewrap resolves to itself", tier == av.TIER_BUBBLEWRAP, tier)
        check("resolving an available tier needs no fallback flag", bool(reason), reason)
    else:
        try:
            av.resolve_tier(av.TIER_BUBBLEWRAP)
            check("unavailable bubblewrap refuses to run", False,
                  "resolve_tier returned a tier instead of raising — THIS IS THE BUG THIS "
                  "MODULE EXISTS TO PREVENT")
        except av.IsolationUnavailableError as e:
            check("unavailable bubblewrap raises instead of degrading", True)
            check("the refusal explains why", bw_reason.split(";")[0][:20] in str(e), str(e)[:120])
            check("the refusal names the explicit opt-out", "direct" in str(e), str(e)[:120])

        tier, reason = av.resolve_tier(av.TIER_BUBBLEWRAP, allow_direct_fallback=True)
        check("explicit authorization permits the fallback", tier == av.TIER_DIRECT, tier)
        check("the fallback reason records that it WAS a fallback",
              "fell back" in reason and "authorization" in reason, reason)
        check("the fallback reason names the tier that was unavailable",
              av.TIER_BUBBLEWRAP in reason, reason)

    print("\n== describe_host is internally consistent ==")
    host = av.describe_host()
    check("every tier is described", set(host["tiers"]) == set(av.TIERS), str(set(host["tiers"])))
    check("direct is always reported available", host["tiers"][av.TIER_DIRECT]["available"])
    strongest = host["strongest_available"]
    check("strongest_available is itself available", host["tiers"][strongest]["available"],
          strongest)
    check(
        "kernel_isolation_available agrees with strongest_available",
        host["kernel_isolation_available"] == (strongest != av.TIER_DIRECT),
        f"strongest={strongest} flag={host['kernel_isolation_available']}",
    )
    check(
        "a host with no kernel isolation does not claim any",
        host["kernel_isolation_available"] or strongest == av.TIER_DIRECT,
        str(host),
    )
    check("platform is reported", bool(host["platform"]), str(host["platform"]))

    print("\n== the module imports without Unix-only dependencies ==")
    import importlib
    import sys
    saved = sys.modules.pop("agent.sandbox.availability", None)
    try:
        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

        def no_resource(name, *a, **kw):
            if name == "resource":
                raise ImportError("simulated: no `resource` module on this platform")
            return real_import(name, *a, **kw)

        if isinstance(__builtins__, dict):
            __builtins__["__import__"] = no_resource
        else:
            __builtins__.__import__ = no_resource
        try:
            importlib.import_module("agent.sandbox.availability")
            check("availability imports with `resource` unavailable", True)
        except ImportError as e:
            check("availability imports with `resource` unavailable", False, str(e))
        finally:
            if isinstance(__builtins__, dict):
                __builtins__["__import__"] = real_import
            else:
                __builtins__.__import__ = real_import
    finally:
        if saved is not None:
            sys.modules["agent.sandbox.availability"] = saved

    print("\n== the bubblewrap tier does not claim a seccomp filter it cannot load ==")
    import tempfile
    from pathlib import Path as _Path

    from . import availability as avail
    from . import seccomp_profile
    from .executor import _profile_digest

    desc = avail.TIER_DESCRIPTION[avail.TIER_BUBBLEWRAP]
    check("the description makes the filter conditional rather than asserting it",
          "when one can be loaded" in desc, desc)
    host = avail.describe_host()
    check("describe_host reports seccomp availability as its own field",
          host.get("seccomp_available") == seccomp_profile.available(),
          str(host.get("seccomp_available")))

    print("\n== the profile digest fingerprints the filter that loaded, not the one declared ==")
    # Runs with or without pyseccomp: _profile_digest takes the accepted list as an argument,
    # which is the point of the change — the digest no longer asks the module what it WANTED
    # to deny.
    ws = _Path(tempfile.mkdtemp(prefix="avail-digest-"))
    declared = sorted(seccomp_profile.DENIED_SYSCALLS)
    loaded = _profile_digest(ws, True, declared)
    check("dropping one loaded rule changes the digest",
          _profile_digest(ws, True, declared[:-1]) != loaded)
    check("the same loaded set is stable", _profile_digest(ws, True, list(declared)) == loaded)
    check("order does not matter", _profile_digest(ws, True, list(reversed(declared))) == loaded)
    check("a run with no filter differs from a filtered one",
          _profile_digest(ws, False, None) != loaded)
    # The real regression: before this change the digest was computed from the DECLARED list
    # whatever actually loaded, so a partially-loaded filter was indistinguishable from a
    # complete one. Passing None still records the declared list, but tags its source — so even
    # with an identical syscall set the two fingerprint differently and a reader can tell which
    # question the digest answered.
    check("a 'declared' digest is distinguishable from a 'loaded' one with the same syscalls",
          _profile_digest(ws, True, None) != loaded,
          "declared and loaded sources must not collide")

    print(f"\n{len(PASS)}/{len(PASS)+len(FAIL)} checks passed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
