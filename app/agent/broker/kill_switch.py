"""Emergency stop — §8b/§12 Phase-3 exit criterion "kill switch meets its target termination
time."

File-flag based: a plain existence check (no lock, no network round trip), so it's checked
before every dispatch at effectively zero cost. Target termination time: new dispatches stop
immediately (next is_engaged() check); an in-flight action stops within its own bounded
per-step timeout — port_discovery checks between each port (≤ PORT_DISCOVERY_CONNECT_TIMEOUT_S
worst case), http_recon is a single bounded request (≤ HTTP_RECON_TIMEOUT_S worst case). There
is no unboundedly-long single action in this tool roster to worry about beyond those caps.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .. import config


class KillSwitch:
    def __init__(self, flag_path: Path = config.KILL_SWITCH_PATH):
        self.flag_path = flag_path

    def is_engaged(self) -> bool:
        return self.flag_path.exists()

    def engage(self, reason: str) -> None:
        self.flag_path.parent.mkdir(parents=True, exist_ok=True)
        self.flag_path.write_text(json.dumps({"engaged_at": time.time(), "reason": reason}))

    def disengage(self) -> None:
        self.flag_path.unlink(missing_ok=True)

    def status(self) -> dict | None:
        if not self.flag_path.exists():
            return None
        return json.loads(self.flag_path.read_text())
