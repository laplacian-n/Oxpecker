"""Terminal entry point: argparse, REPL, session resume."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import audit_log as audit_mod
from . import config
from .loop import AgentLoop


def main() -> int:
    parser = argparse.ArgumentParser(description="Terminal security-testing agent")
    parser.add_argument("--session", help="resume an existing session id")
    parser.add_argument("--workspace", help="workspace root (default: fresh temp dir)")
    parser.add_argument(
        "--profile",
        choices=[config.PROFILE_SAFE_DEFAULT, config.PROFILE_DIAGNOSTIC_THINKING],
        default=config.PROFILE_SAFE_DEFAULT,
    )
    parser.add_argument(
        "--dangerous-local",
        action="store_true",
        help="relax the network/privilege-escalation binary block after explicit confirmation",
    )
    parser.add_argument(
        "--mcp-tools",
        action="store_true",
        help="Phase 2: run tools via a client-local MCP subprocess instead of in-process",
    )
    parser.add_argument(
        "--device-id",
        help="Phase 2: use the shared memory service, authenticating as this device "
        "(register one first with `python3 -m agent.manage_devices add <id>`)",
    )
    parser.add_argument(
        "--device-token", help="Phase 2: token for --device-id (or set AGENT_DEVICE_TOKEN)"
    )
    parser.add_argument("--memory-url", default=config.MEMORY_SERVICE_URL)
    parser.add_argument("--memory-cert", default=str(config.MEMORY_SERVICE_CERT_PATH))
    parser.add_argument(
        "--security-tools",
        action="store_true",
        help="Phase 3: enable http_recon/port_discovery, mediated by the execution broker "
        "under the current engagement's Rules of Engagement (engagement/roe.json)",
    )
    parser.add_argument("--engagement-id", default="lab-default")
    parser.add_argument(
        "--isolation-tier",
        choices=["direct", "bubblewrap"],
        default="bubblewrap",
        help="Phase 4: run_command execution tier. 'bubblewrap' (default, ADR-0004) gives "
        "kernel-enforced filesystem/network isolation (no network device exists inside the "
        "sandbox at all); 'direct' is the original Phase-1 behavior (blocklist only) and "
        "requires explicit confirmation, same as --dangerous-local. Dual-mode is a per-session "
        "operator choice, not something the model can request per call.",
    )
    parser.add_argument("--verify-audit", metavar="SESSION_ID", help="verify a session's audit log and exit")
    parser.add_argument(
        "--kill-switch", choices=["engage", "disengage", "status"],
        help="Phase 3: control the emergency stop and exit (no session started)",
    )
    args = parser.parse_args()

    if args.kill_switch:
        from .broker.kill_switch import KillSwitch

        ks = KillSwitch()
        if args.kill_switch == "engage":
            ks.engage(reason="manual CLI engage")
            print("kill switch ENGAGED — all broker-mediated actions will be denied")
        elif args.kill_switch == "disengage":
            ks.disengage()
            print("kill switch disengaged")
        else:
            status = ks.status()
            print(f"kill switch: {'ENGAGED — ' + str(status) if status else 'not engaged'}")
        return 0

    if args.verify_audit:
        ok, msg = audit_mod.verify(args.verify_audit)
        print(("OK: " if ok else "FAILED: ") + msg)
        return 0 if ok else 1

    workspace_root = Path(args.workspace) if args.workspace else config.default_workspace_root()
    workspace_root.mkdir(parents=True, exist_ok=True)

    dangerous_local = args.dangerous_local
    if dangerous_local:
        print(
            "\n[!] --dangerous-local: network and privilege-escalation binaries will be "
            "PERMITTED. This relaxes a default safety control.\n"
        )
        if input("Type 'yes' to confirm: ").strip().lower() != "yes":
            print("Aborted.")
            return 1

    if args.isolation_tier == "direct":
        print(
            "\n[!] --isolation-tier direct: run_command will execute WITHOUT kernel-enforced "
            "namespace isolation (blocklist only — a command can still open network sockets "
            "in-process and reach the full host filesystem outside the workspace). ADR-0004's "
            "default is 'bubblewrap'; this relaxes that default.\n"
        )
        if input("Type 'yes' to confirm: ").strip().lower() != "yes":
            print("Aborted.")
            return 1

    memory = None
    if args.device_id:
        import os

        from .memory_client import RemoteSessionStore

        token = args.device_token or os.environ.get("AGENT_DEVICE_TOKEN")
        if not token:
            print("--device-id requires --device-token or AGENT_DEVICE_TOKEN")
            return 1
        session_id = args.session or RemoteSessionStore.new_session_id()
        memory = RemoteSessionStore(
            session_id,
            device_id=args.device_id,
            token=token,
            base_url=args.memory_url,
            verify=args.memory_cert,
        )

    loop = AgentLoop(
        workspace_root=workspace_root,
        session_id=args.session,
        profile=args.profile,
        dangerous_local=dangerous_local,
        memory=memory,
        use_mcp_tools=args.mcp_tools,
        use_security_tools=args.security_tools,
        bootstrap_engagement=args.security_tools,
        device_id=args.device_id or "local",
        engagement_id=args.engagement_id,
        isolation_tier=args.isolation_tier,
    )

    print(f"session:        {loop.session_id}")
    print(f"workspace:      {workspace_root}")
    print(f"profile:        {args.profile}")
    print(f"memory:         {'remote (' + args.memory_url + ')' if memory else 'local JSONL'}")
    print(f"tools:          {'MCP subprocess' if args.mcp_tools else 'in-process'}")
    print(f"security tools: {'enabled (' + args.engagement_id + ')' if args.security_tools else 'disabled'}")
    print(f"isolation:      {args.isolation_tier} (run_command only)")
    if args.profile == config.PROFILE_DIAGNOSTIC_THINKING:
        print("(diagnostic-thinking: tools are shown, never executed)")
    print("Ctrl-D or 'exit' to quit.\n")

    try:
        while True:
            try:
                user_input = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit"):
                break

            result = loop.run_task(user_input)
            if result.status == "ok":
                print(f"\n{result.message}\n")
            else:
                print(f"\n[{result.status}] {result.message}\n")
    finally:
        loop.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
