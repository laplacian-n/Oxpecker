# ADR-0003: seccomp filtering for the bubblewrap tier

**Status:** Accepted
**Date:** 2026-08-31 (decided; implemented same day)

## Context

The bubblewrap tier currently enforces isolation via namespaces (`--unshare-all`) and resource
limits (`RLIMIT_AS`/`RLIMIT_CPU`/`RLIMIT_NOFILE` via `preexec_fn`). It does not filter syscalls.
Both independent reviews flag this: bubblewrap's own upstream documentation states explicitly
that it is a low-level primitive whose security model is defined entirely by the arguments
passed to it, not something that is "secure by default." Namespaces without seccomp still expose
the full host syscall surface to whatever runs inside the sandbox (subject to what namespaces and
capabilities restrict, which is real but is not the same restriction a syscall filter provides).

## Alternatives considered

1. Add a single, generic seccomp profile (e.g., adapt Docker's default profile) to every
   bubblewrap invocation regardless of what's running.
2. Design seccomp filters per capability/workload (a profile for the generic `run_command` tool
   may differ from a hypothetical future network-tool sandbox), verified against that specific
   workload's actual syscall needs.
3. Defer seccomp entirely until there's a concrete workload profile to filter for.

## Discussion

Both reviewers explicitly warn against option 1: "seccomp decisions must be workload-specific and
tested" (integration prompt, M4.1) and "ห้าม copy profile ทั่วไปแล้วอ้างว่าปลอดภัย" (don't copy a
generic profile and claim it's safe — local-security-agent-review-phase4.md §1.2). A copied
generic profile that happens to work for today's test commands but was never actually derived
from this tool's real syscall needs would create exactly the false-confidence problem ADR-0002
was written to avoid for the microVM tier.

Option 2 is the technically correct answer but requires characterizing the actual syscall surface
`run_command`'s current allowlisted binaries (`ls`, `cat`, `python3`, etc. — see
`config.COMMAND_ALLOWLIST`) need, which is real engineering work (strace-based profiling, testing
against denial of legitimate operations, not just denial of attack primitives) that this audit
pass did not do.

## Decision

**Option 2, scoped narrowly, accepted by the owner and implemented** — with one deliberate
adjustment from the original recommendation. Building a genuine minimal per-program *allowlist*
(the literal reading of option 2) would need strace-based profiling of every command in
`config.COMMAND_ALLOWLIST` — real, substantial work this pass still did not do, for the same
reason the original discussion gave. What was built instead, and is real: a default-allow /
explicit-*deny* profile (`agent/sandbox/seccomp_profile.py`'s `DENIED_SYSCALLS`) covering process
introspection (`ptrace` and its cross-process-memory relatives), namespace/mount manipulation,
kernel module/BPF loading, and a handful of kernel-info-leak/host-wide-state syscalls — chosen
because none of them have any legitimate use by `ls`/`cat`/`python3`/etc., so this doesn't risk
the "narrow allowlist breaks a legitimate command" failure mode a true allowlist would, while
still directly closing the gap the reviewers actually cared about (ptrace/mount/unshare/bpf
reachable from inside the sandbox). This is the ADR's own stated part (b) test criterion
("confirming a set of known dangerous syscalls are denied") implemented directly as the
mechanism, not just as a test.

Verified both required properties for real, not assumed: (a) `ls`, `cat`, `echo`, and `python3 -c`
all still succeed under the filter through the real `BubblewrapExecutor.run()` path; (b) a
process calling `ptrace(PTRACE_TRACEME, ...)` gets `EPERM` under the filter, and — a control the
original recommendation didn't ask for but which materially strengthens the evidence — the exact
same call succeeds when the filter is omitted, proving the denial in (b) is actually caused by
the seccomp filter and not some unrelated sandbox property. Loaded via bwrap's `--seccomp FD`
(compiled through `pyseccomp`, a pure-ctypes wrapper needing no compilation — added to
`requirements-sandbox-hardening.txt`). Degrades visibly, not silently, when `pyseccomp` isn't
importable: `BubblewrapExecutor.run()` logs a warning and runs without the filter rather than
crashing `run_command` or claiming protection that wasn't applied — and `sandbox_profile_digest`
now includes `seccomp_active`, so a run without the filter produces a visibly different digest
from one with it, rather than the audit trail implying uniform protection either way.

## Security consequences of staying at the current state

Without seccomp, a successful code-execution exploit *inside* the bubblewrap sandbox (e.g. via a
vulnerability in a tool the sandbox runs) retains access to whatever syscalls the namespace
configuration doesn't already block — this is a real, currently-open gap for the bubblewrap tier,
not a false one. It does not undermine the filesystem/network isolation properties already tested
and passing (those come from namespaces and mounts, which are independent of seccomp), but it
does mean the tier's overall guarantee is narrower than "arbitrary code cannot escalate," which
was never claimed for it (see ADR-0002 — that guarantee is reserved for the deferred microVM
tier).

## Migration / rollback

Additive: a seccomp filter can be added to `BubblewrapExecutor.run()`'s `bwrap_argv` construction
(via `--seccomp <fd>` passing a compiled BPF program) without touching any other module. Rollback
is deleting that one flag.

## Related, distinct gap: fork-bomb / process-count containment

Not a seccomp concern (seccomp filters syscalls, not process counts), but the same "don't fake a
resource-containment guarantee" principle applied here too, so it's recorded alongside this ADR.
Two approaches were tried in the same pass this ADR was written and both reverted:

1. `RLIMIT_NPROC` — wrong primitive; it counts against the real UID system-wide, not per sandbox
   invocation, and broke bubblewrap itself immediately on this host (the invoking user already
   runs far more processes than any sane per-sandbox cap).
2. cgroup v2 `pids.max` via a transient `systemd-run --user --scope` wrapper — correctly *scoped*
   (per-invocation), and it did cap a real fork bomb when tested standalone, but wrapping every
   bubblewrap call with it introduced a reproducible race: cancellation (`SIGTERM` to the wrapped
   process) sometimes failed to cascade to the sandboxed child, leaking orphaned sandboxed
   processes on the host — a regression in a property (reliable cancellation) that was previously
   100%. Full detail in `agent/sandbox/executor.py`'s comments and `docs/STATUS.md`.

Both reverted rather than shipped. Next candidate, untried: write to a self-owned cgroup v2
`pids.max` file directly via the cgroupfs, rather than delegating the cgroup's lifecycle to
`systemd-run`'s own process/scope semantics — this avoids the extra process layer that seems to
be where the signal-delivery race originates. Not resolved by this pass.
