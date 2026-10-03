"""Entry point: runs the memory service over TLS (§6 "Simple LAN auth" — transport security
is required, not optional, once this leaves loopback)."""
from __future__ import annotations

import uvicorn

from . import config


def main() -> None:
    uvicorn.run(
        "agent.memory_service.app:app",
        host=config.MEMORY_SERVICE_HOST,
        port=config.MEMORY_SERVICE_PORT,
        ssl_certfile=str(config.MEMORY_SERVICE_CERT_PATH),
        ssl_keyfile=str(config.MEMORY_SERVICE_KEY_PATH),
        log_level="info",
    )


if __name__ == "__main__":
    main()
