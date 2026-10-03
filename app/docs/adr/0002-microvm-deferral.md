# ADR-0002: Defer microVM (Firecracker) isolation tier

**Status:** Accepted
**Date:** 2026-08-31

## Context

Phase 4's original design target (`phase4.md`, `security-agent-research-reviewed.md` §3) calls
for a microVM isolation tier alongside a lightweight (bubblewrap) tier. `/dev/kvm` is present on
this machine (AMD-V), so Firecracker is technically launchable, but a real, safe microVM tier
needs a guest kernel, minimal rootfs, vsock-based exec/output plumbing, and an image supply
chain — a multi-day infrastructure project distinct from writing sandbox argv flags.

## Alternatives considered

1. Build a minimal, direct Firecracker integration (skip E2B's own control plane) this session.
2. Build the executor abstraction and the lightweight tier only; leave microVM as an explicit,
   loud `NotImplementedError` stub.
3. Silently document the microVM tier as available without actually building/testing it.

## Decision

Option 2, chosen with the user directly (see conversation history) after weighing the risk:
a half-built microVM tier that *claims* the "microVM" name without passing its own
escape/egress/resource tests would be actively worse than not having the tier at all, since a
future reader could reasonably trust the label. `agent/sandbox/executor.py`'s `MicroVMExecutor`
raises `NotImplementedError` with an explicit message rather than downgrading silently to a
weaker tier under the same name.

01-security-agent-main-direction.md independently reaches the same conclusion ("เลื่อน:
Firecracker/microVM — ทำเมื่อมี exploit-grade arbitrary code และทีมพร้อมดูแล
kernel/rootfs/vsock/image supply chain"), and local-security-agent-review-phase4.md calls the
original decision to cut it "ถูกต้องที่สุดในเฟสนี้" (the most correct call in this phase). All
three independent sources now agree microVM stays deferred until Phase 7, when arbitrary
exploit-grade code execution actually requires it.

## Security consequences

The bubblewrap tier is a strong, kernel-namespace-enforced local isolation boundary (verified:
no filesystem path outside explicit binds, no network device including loopback bridging to the
host, RLIMIT-capped resources) but is explicitly *not* microVM-grade — a kernel exploit inside
the sandboxed process's namespace could still reach the host kernel, which a hardware-virtualized
guest would not permit. Anything requiring that stronger guarantee (arbitrary/exploit-grade code
execution, per Phase 7's scope) must not run under `bubblewrap` and must wait for this tier.

## Migration / rollback

Additive only — implementing the microVM tier later means writing a new
`MicroVMExecutor.run()` body; no existing code needs to change. `EXECUTORS["microvm"]` is already
wired into `agent/sandbox/executor.py`'s registry, so enabling it is a one-line swap once the
implementation exists and passes its own escape/egress/resource test suite (mirroring
`agent/sandbox/test_isolation.py`'s structure).
