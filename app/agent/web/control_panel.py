"""Oxpecker control panel — an operator web UI for running the server side over the LAN.

The dev server's own web UI cannot start/stop/restart dev_server (a page it serves would be killing
its own process), so this is a SEPARATE small supervisor service. It:
  - serves a dark, Oxpecker-styled single page over HTTPS on the LAN (self-signed cert, auto-made);
  - gates every /api/* behind a generated admin token (constant-time compare);
  - starts/stops/restarts dev_server (chosen provider/model/host/port/RAG index) and the nomic
    embedding server as child processes, from a FIXED, validated action set — never arbitrary argv;
  - lets the operator enter the OpenRouter key (written 0600, never echoed, passed to dev_server in
    the environment, never on a command line) and rotate the web-UI API key;
  - shows connection/health, OpenRouter usage, and the per-engagement spend total.

Run it:  .venv/bin/python -m agent.web.control_panel --host 0.0.0.0 --port 7788
It prints the admin token and the https URL on startup. It drives everything else; you no longer
hand-run `python -m agent.web.dev_server ...`.

Security posture: this process can launch servers and holds secrets, so it is only as safe as the
admin token and the LAN it binds. The token is required on every action, secrets are write-only
through the API, and process control accepts only validated parameters (provider/model/port/paths),
never a free-form command. The embedding server is always launched CPU-only (-ngl 0); the panel
never offers a GPU flag.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .. import config
from ..llm import openrouter as _openrouter

log = logging.getLogger("oxpecker.control_panel")

# ── state files (all under the gitignored STATE_DIR, mode 0600) ───────────────
TOKEN_FILE = config.STATE_DIR / "control_panel_token.txt"
CERT_FILE = config.STATE_DIR / "control_panel_cert.pem"
CERT_KEY_FILE = config.STATE_DIR / "control_panel_key.pem"
PANEL_CONFIG_FILE = config.STATE_DIR / "control_panel_config.json"
OPENROUTER_KEY_FILE = config.STATE_DIR / _openrouter.KEY_FILENAME
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Roots scanned for prebuilt RAG indexes (a vectors.npy beside a meta.jsonl). Kept to a few known
# locations so discovery is bounded; extra roots can be added with $OXPECKER_RAG_ROOTS (os.pathsep).
_DEFAULT_RAG_ROOTS = [
    config.KNOWLEDGE_RAG_DIR,
    Path.home() / "pentestAI",
    Path.home() / "localAI",
    Path.home() / "oxpecker",
]

_MODEL_RE = re.compile(r"^[A-Za-z0-9._:\-/]{1,128}$")
_PROVIDERS = ("openrouter", "llama_cpp")
_HOSTS = ("0.0.0.0", "127.0.0.1")


def _write_private(path: Path, text: str) -> None:
    """Write a secret to a 0600 file, creating the parent and tightening the mode even on rewrite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with 0600 from the start so a reader never races a world-readable window.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ── admin token ───────────────────────────────────────────────────────────────
def ensure_token() -> str:
    try:
        existing = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    _write_private(TOKEN_FILE, tok + "\n")
    return tok


def _token_ok(request: Request, token: str) -> bool:
    supplied = request.headers.get("X-Admin-Token", "")
    if not supplied:
        # Allow it in a query param only for the SSE-less status probe convenience; still constant-time.
        supplied = request.query_params.get("token", "")
    return bool(supplied) and hmac.compare_digest(supplied, token)


# ── self-signed TLS cert ────────────────────────────────────────────────────────
def _primary_lan_ip() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))  # no packet is sent; this just picks the outbound iface
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


def ensure_cert() -> tuple[Path, Path]:
    """Generate a self-signed cert+key (localhost, 127.0.0.1 and the LAN IP in the SANs) once."""
    if CERT_FILE.exists() and CERT_KEY_FILE.exists():
        return CERT_FILE, CERT_KEY_FILE
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Oxpecker Control Panel")])
    sans: list[x509.GeneralName] = [x509.DNSName("localhost"),
                                    x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    lan = _primary_lan_ip()
    if lan:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(lan)))
        except ValueError:
            pass
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_private(CERT_KEY_FILE, key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()).decode())
    CERT_FILE.write_text(cert.public_bytes(serialization.Encoding.PEM).decode(), encoding="utf-8")
    return CERT_FILE, CERT_KEY_FILE


# ── OpenRouter + web-UI keys ────────────────────────────────────────────────────
def _read_openrouter_key() -> str | None:
    try:
        k = OPENROUTER_KEY_FILE.read_text(encoding="utf-8").strip()
        return k or None
    except (FileNotFoundError, OSError):
        return None


def _openrouter_key_last4() -> str | None:
    k = _read_openrouter_key()
    return k[-4:] if k and len(k) >= 4 else None


def _rotate_web_ui_key() -> str:
    new = secrets.token_urlsafe(24)
    _write_private(config.WEB_UI_API_KEY_FILE, new + "\n")
    return new


def _web_ui_key_set() -> bool:
    try:
        return bool(config.WEB_UI_API_KEY_FILE.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError):
        return False


# ── RAG index discovery ─────────────────────────────────────────────────────────
def _rag_roots() -> list[Path]:
    roots = list(_DEFAULT_RAG_ROOTS)
    extra = os.environ.get("OXPECKER_RAG_ROOTS", "")
    for p in extra.split(os.pathsep):
        if p.strip():
            roots.append(Path(p.strip()))
    return roots


def _npy_rows(vectors: Path) -> int | None:
    try:
        import numpy as np
        return int(np.load(vectors, mmap_mode="r").shape[0])
    except Exception:  # noqa: BLE001 — a bad/huge file just reports unknown
        return None


def discover_rag_indexes() -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for root in _rag_roots():
        try:
            if not root.exists():
                continue
            for vectors in root.rglob("vectors.npy"):
                meta = vectors.with_name("meta.jsonl")
                if not meta.exists():
                    continue
                key = str(vectors.resolve())
                if key in seen:
                    continue
                seen.add(key)
                # A readable label: the two path components above index/ usually name the project.
                parts = vectors.resolve().parts
                label = "/".join(parts[-4:-1]) if len(parts) >= 4 else str(vectors.parent)
                out.append({"label": label, "vectors": str(vectors.resolve()),
                            "meta": str(meta.resolve()), "chunks": _npy_rows(vectors)})
                if len(out) >= 50:
                    return out
        except (OSError, PermissionError):
            continue
    return out


# ── spend + usage ───────────────────────────────────────────────────────────────
def _engagement_spend_total() -> dict:
    roots = [config.ENGAGEMENTS_ROOT, config.ENGAGEMENT_DIR]
    engagements: list[dict] = []
    total = 0.0
    for root in roots:
        try:
            if not root.exists():
                continue
            dbs = ([root / "spend.db"] if (root / "spend.db").exists()
                   else list(root.glob("*/spend.db")))
            for db in dbs:
                try:
                    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
                    try:
                        row = conn.execute(
                            "SELECT COALESCE(SUM(actual),0) FROM reservations WHERE status='settled'"
                        ).fetchone()
                        spent = float(row[0] or 0.0)
                    finally:
                        conn.close()
                except (sqlite3.Error, OSError):
                    continue
                eid = db.parent.name
                engagements.append({"id": eid, "spent": spent})
                total += spent
        except (OSError, PermissionError):
            continue
    return {"total": total, "engagements": engagements}


def _openrouter_usage() -> dict:
    key = _read_openrouter_key()
    if not key:
        return {"ok": False, "error": "no OpenRouter key set"}
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/key",
            headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read()).get("data", {})
        limit = data.get("limit")
        usage = data.get("usage")
        remaining = data.get("limit_remaining")
        if remaining is None and limit is not None and usage is not None:
            remaining = limit - usage
        return {"ok": True, "limit": limit, "usage": usage, "remaining": remaining}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"OpenRouter HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ── child-process supervisor ─────────────────────────────────────────────────────
class Supervisor:
    """Owns the dev_server and embedding-server child processes. One instance, guarded by a lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.dev: subprocess.Popen | None = None
        self.dev_params: dict = {}
        self.embed: subprocess.Popen | None = None
        self.embed_port: int | None = None

    # -- validation (reject anything that is not a known-good value) --
    @staticmethod
    def _validate_dev(params: dict) -> dict:
        provider = str(params.get("provider") or "openrouter")
        if provider not in _PROVIDERS:
            raise ValueError(f"unknown provider {provider!r}")
        model = str(params.get("model") or "").strip()
        if provider == "openrouter" and not _MODEL_RE.match(model):
            raise ValueError("model id is required and must match [A-Za-z0-9._:/-]")
        host = str(params.get("host") or "0.0.0.0")
        if host not in _HOSTS:
            raise ValueError(f"host must be one of {_HOSTS}")
        try:
            port = int(params.get("port") or 7777)
        except (TypeError, ValueError):
            raise ValueError("port must be an integer")
        if not (1024 <= port <= 65535):
            raise ValueError("port must be 1024..65535")
        clean = {"provider": provider, "model": model, "host": host, "port": port}
        for k in ("vector_index", "vector_meta"):
            v = params.get(k)
            if v:
                p = Path(str(v))
                if not p.is_file():
                    raise ValueError(f"{k} does not exist: {v}")
                clean[k] = str(p.resolve())
        embed_url = params.get("embed_url")
        if embed_url:
            if not re.match(r"^https?://[A-Za-z0-9.\-]+:\d+/?$", str(embed_url)):
                raise ValueError("embed_url must look like http://host:port")
            clean["embed_url"] = str(embed_url)
        return clean

    @staticmethod
    def _alive(proc: subprocess.Popen | None) -> bool:
        return proc is not None and proc.poll() is None

    def _kill(self, proc: subprocess.Popen | None) -> None:
        if not self._alive(proc):
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:  # noqa: BLE001
            pass

    # -- dev_server --
    def start_dev(self, params: dict) -> dict:
        clean = self._validate_dev(params)
        with self._lock:
            if self._alive(self.dev):
                raise RuntimeError("dev_server is already running — stop it first")
            argv = [sys.executable, "-m", "agent.web.dev_server",
                    "--provider", clean["provider"], "--host", clean["host"],
                    "--port", str(clean["port"])]
            if clean["provider"] == "openrouter":
                argv += ["--model", clean["model"]]
            if clean.get("vector_index"):
                argv += ["--vector-index", clean["vector_index"]]
            if clean.get("vector_meta"):
                argv += ["--vector-meta", clean["vector_meta"]]
            if clean.get("embed_url"):
                argv += ["--embed-url", clean["embed_url"]]
            env = dict(os.environ)
            key = _read_openrouter_key()
            if key:
                env["OPENROUTER_API_KEY"] = key  # env, never argv (not visible in `ps`)
            cwd = str(Path(__file__).resolve().parents[2])  # app/ (the package root's parent)
            self.dev = subprocess.Popen(argv, cwd=cwd, env=env)
            self.dev_params = clean
            _save_panel_config(clean)
            return {"ok": True}

    def stop_dev(self) -> dict:
        with self._lock:
            self._kill(self.dev)
            self.dev = None
            return {"ok": True}

    def restart_dev(self, params: dict | None) -> dict:
        use = params if (params and params.get("model")) else (self.dev_params or _load_panel_config())
        if not use:
            raise RuntimeError("no saved server settings to restart from — start it once first")
        self.stop_dev()
        time.sleep(0.5)
        return self.start_dev(use)

    # -- embedding server --
    def start_embed(self, model_path: str, port: int) -> dict:
        mp = Path(str(model_path))
        if mp.suffix != ".gguf" or not mp.is_file():
            raise ValueError("model_path must be an existing .gguf file")
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise ValueError("port must be an integer")
        if not (1024 <= port <= 65535):
            raise ValueError("port must be 1024..65535")
        binary = _find_llama_server()
        if not binary:
            raise RuntimeError("llama-server binary not found (set OXPECKER_LLAMA_SERVER)")
        with self._lock:
            if self._alive(self.embed):
                raise RuntimeError("embedding server is already running — stop it first")
            argv = [binary, "-m", str(mp.resolve()), "--embedding", "--pooling", "mean",
                    "-ngl", "0",  # CPU only — the panel never touches the GPU
                    "-c", "2048", "-b", "2048", "-ub", "2048",
                    "--host", "127.0.0.1", "--port", str(port)]
            self.embed = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.embed_port = port
            return {"ok": True}

    def stop_embed(self) -> dict:
        with self._lock:
            self._kill(self.embed)
            self.embed = None
            self.embed_port = None
            return {"ok": True}

    def status(self) -> dict:
        dev_alive = self._alive(self.dev)
        health = None
        if dev_alive:
            port = self.dev_params.get("port", 7777)
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
                    health = json.loads(r.read())
            except Exception:  # noqa: BLE001 — just starting up, or busy
                health = None
        embed_alive = self._alive(self.embed)
        embed_ok = False
        if embed_alive and self.embed_port:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.embed_port}/health", timeout=2) as r:
                    embed_ok = r.status == 200
            except Exception:  # noqa: BLE001
                embed_ok = False
        return {
            "dev_server": {"running": dev_alive,
                           "pid": self.dev.pid if dev_alive else None,
                           "params": self.dev_params, "health": health},
            "embed": {"running": embed_alive,
                      "pid": self.embed.pid if embed_alive else None,
                      "port": self.embed_port, "health_ok": embed_ok},
            "openrouter_key_set": _read_openrouter_key() is not None,
            "openrouter_key_last4": _openrouter_key_last4(),
            "web_ui_key_set": _web_ui_key_set(),
        }

    def shutdown(self) -> None:
        self._kill(self.dev)
        self._kill(self.embed)


def _find_llama_server() -> str | None:
    env = os.environ.get("OXPECKER_LLAMA_SERVER")
    if env and Path(env).is_file():
        return env
    import shutil
    found = shutil.which("llama-server")
    if found:
        return found
    for cand in [Path.home() / "llama.cpp" / "build" / "bin" / "llama-server"]:
        if cand.is_file():
            return str(cand)
    return None


def _save_panel_config(params: dict) -> None:
    try:
        PANEL_CONFIG_FILE.write_text(json.dumps(params), encoding="utf-8")
    except OSError:
        pass


def _load_panel_config() -> dict:
    try:
        return json.loads(PANEL_CONFIG_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}


# ── HTTP app ──────────────────────────────────────────────────────────────────
_SUPERVISOR = Supervisor()
_TOKEN = ""  # set in main(); tests set it directly

app = FastAPI(title="Oxpecker Control Panel")


class StartServerRequest(BaseModel):
    provider: str = "openrouter"
    model: str = ""
    host: str = "0.0.0.0"
    port: int = 7777
    vector_index: str | None = None
    vector_meta: str | None = None
    embed_url: str | None = None


class StartEmbedRequest(BaseModel):
    model_path: str
    port: int = 8091


class OpenRouterKeyRequest(BaseModel):
    key: str


@app.middleware("http")
async def require_admin_token(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)
    if not _TOKEN or not _token_ok(request, _TOKEN):
        return JSONResponse({"detail": "missing or invalid admin token"}, status_code=401)
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
def index():
    try:
        return (STATIC_DIR / "control_panel.html").read_text(encoding="utf-8")
    except FileNotFoundError:
        return HTMLResponse("<h1>control_panel.html not built</h1>", status_code=503)


@app.get("/api/status")
def api_status():
    return _SUPERVISOR.status()


@app.post("/api/server/start")
def api_server_start(req: StartServerRequest):
    try:
        return _SUPERVISOR.start_dev(req.model_dump())
    except (ValueError, RuntimeError) as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/server/stop")
def api_server_stop():
    return _SUPERVISOR.stop_dev()


@app.post("/api/server/restart")
def api_server_restart(req: StartServerRequest | None = None):
    try:
        return _SUPERVISOR.restart_dev(req.model_dump() if req else None)
    except (ValueError, RuntimeError) as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/embed/start")
def api_embed_start(req: StartEmbedRequest):
    try:
        return _SUPERVISOR.start_embed(req.model_path, req.port)
    except (ValueError, RuntimeError) as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/embed/stop")
def api_embed_stop():
    return _SUPERVISOR.stop_embed()


@app.get("/api/rag/indexes")
def api_rag_indexes():
    return {"indexes": discover_rag_indexes()}


@app.get("/api/keys/openrouter")
def api_get_openrouter_key():
    return {"set": _read_openrouter_key() is not None, "last4": _openrouter_key_last4()}


@app.post("/api/keys/openrouter")
def api_set_openrouter_key(req: OpenRouterKeyRequest):
    key = (req.key or "").strip()
    if not key:
        return {"ok": False, "error": "empty key"}
    _write_private(OPENROUTER_KEY_FILE, key + "\n")
    return {"ok": True}


@app.post("/api/keys/webui/rotate")
def api_rotate_web_key():
    return {"ok": True, "key": _rotate_web_ui_key()}


@app.get("/api/usage/openrouter")
def api_usage_openrouter():
    return _openrouter_usage()


@app.get("/api/usage/spend")
def api_usage_spend():
    return _engagement_spend_total()


def main() -> None:
    global _TOKEN
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Oxpecker Control Panel")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default 0.0.0.0 for LAN)")
    parser.add_argument("--port", type=int, default=7788, help="Port (default 7788)")
    parser.add_argument("--no-tls", action="store_true",
                        help="Serve plain HTTP (NOT recommended — the admin token would cross the "
                             "LAN in the clear). Default is HTTPS with a self-signed cert.")
    args = parser.parse_args()

    _TOKEN = ensure_token()
    import uvicorn
    ssl_kwargs = {}
    scheme = "http"
    if not args.no_tls:
        cert, keyf = ensure_cert()
        ssl_kwargs = {"ssl_certfile": str(cert), "ssl_keyfile": str(keyf)}
        scheme = "https"

    lan = _primary_lan_ip() or args.host
    log.info("=" * 68)
    log.info("Oxpecker Control Panel")
    log.info("  URL:   %s://%s:%d/", scheme, lan, args.port)
    log.info("  Token: %s", _TOKEN)
    log.info("  (paste the token on the page; it is also in %s)", TOKEN_FILE)
    log.info("=" * 68)

    try:
        uvicorn.run(app, host=args.host, port=args.port, **ssl_kwargs)
    finally:
        _SUPERVISOR.shutdown()


if __name__ == "__main__":
    main()
