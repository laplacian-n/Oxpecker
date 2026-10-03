"""Constrained port discovery — Phase 3's second narrow tool.

A minimal TCP-connect scanner, not an nmap wrapper: nmap wasn't installed on this box, and
making it genuinely "constrained" would mean allow-listing a large, exploit-capable flag
surface (NSE scripts, OS fingerprinting, etc.) — more attack surface than this tool needs.
A purpose-built connect-scan is narrower by construction: TCP connect only, no other
capability exists to lock down. Same DNS-rebinding-safe pattern as http_recon — resolve once
via the broker, connect to the validated IP for every port, never re-resolve mid-scan.
"""
from __future__ import annotations

import socket
import time

from .. import config
from ..broker import scope_check
from ..broker.kill_switch import KillSwitch
from ..broker.policy import Policy

SCHEMA = {
    "type": "function",
    "function": {
        "name": "port_discovery",
        "description": (
            f"Constrained TCP-connect port scan of a single in-scope host. Up to "
            f"{config.PORT_DISCOVERY_MAX_PORTS} ports per call, connect-scan only (no OS "
            "detection, no scripts, no UDP)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "e.g. 127.0.0.1"},
                "ports": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": f"Up to {config.PORT_DISCOVERY_MAX_PORTS} port numbers.",
                },
            },
            "required": ["host", "ports"],
        },
    },
}


def run(host: str, ports: list[int], policy: Policy) -> dict:
    if len(ports) > config.PORT_DISCOVERY_MAX_PORTS:
        raise ValueError(
            f"{len(ports)} ports requested, exceeds blast-radius limit of "
            f"{config.PORT_DISCOVERY_MAX_PORTS} per call"
        )
    for p in ports:
        if not (0 < p < 65536):
            raise ValueError(f"invalid port: {p}")

    result = scope_check.validate_target(host, policy)
    if not result.allowed:
        raise PermissionError(f"{host!r} not in scope: {result.reason}")

    kill_switch = KillSwitch()
    open_ports, closed_ports, filtered_ports = [], [], []
    for port in ports:
        if kill_switch.is_engaged():
            return {
                "ok": False,
                "error": "kill switch engaged mid-scan",
                "open_ports": open_ports,
                "closed_ports": closed_ports,
                "filtered_ports": filtered_ports,
                "scanned_before_stop": len(open_ports) + len(closed_ports) + len(filtered_ports),
                "_policy_rule": result.policy_rule,
            }
        state = _probe(result.validated_ip, port)
        {"open": open_ports, "closed": closed_ports, "filtered": filtered_ports}[state].append(port)

    return {
        "ok": True,
        "host": host,
        "validated_ip": result.validated_ip,
        "open_ports": open_ports,
        "closed_ports": closed_ports,
        "filtered_ports": filtered_ports,
        "_policy_rule": result.policy_rule,
        "_exit_metadata": {"ports_scanned": len(ports)},
    }


def _probe(ip: str, port: int) -> str:
    sock = socket.socket(socket.AF_INET if ":" not in ip else socket.AF_INET6, socket.SOCK_STREAM)
    sock.settimeout(config.PORT_DISCOVERY_CONNECT_TIMEOUT_S)
    try:
        result = sock.connect_ex((ip, port))
        if result == 0:
            return "open"
        return "closed"
    except socket.timeout:
        return "filtered"
    except OSError:
        return "filtered"
    finally:
        sock.close()
