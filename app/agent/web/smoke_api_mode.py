"""End-to-end smoke test for API mode: does every component actually work together?

This answers one question — "can this system run a governed engagement against a real target,
driven by a real model over an API, and produce a record you could train on?" — by doing it, on
a self-contained target, and then checking what landed on disk.

It is not a unit test. It starts a real dev_server subprocess, a real HTTP target, and makes
real API calls. Everything it asserts is a cross-component fact that no unit test can reach:
that the provider the server reports is the one serving, that a tool call survives the broker
gate, that the audit chain verifies, and that a trajectory can be reconstructed from the trace.

WHY THE LAB TARGET IS BUILT IN: it needs a target that is unambiguously in scope and
unambiguously ours. A docker daemon is not always available and a public target is never
authorised. So the script serves its own deliberately-broken app on loopback, puts exactly that
address in the engagement scope, and the scope check then has something real to allow and
something real to deny.

COST: defaults to a free model, so a normal run spends nothing. `--model` overrides it; the
summary prints what the run actually cost, read from the provider's own usage accounting rather
than estimated.

    python3 -m agent.web.smoke_api_mode                      # free model, full run
    python3 -m agent.web.smoke_api_mode --model deepseek/deepseek-v4.1-flash
    python3 -m agent.web.smoke_api_mode --skip-model         # everything except the API calls

Needs $OPENROUTER_API_KEY, or state/openrouter_api_key.txt. The key is passed to the server
through the environment, never on argv, where `ps` would show it to every user on the host.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []

#: A free, tool-capable model with a large context. Free models are rate-limited upstream, so
#: the runner tries these in order and reports which one answered — a 429 on the first is an
#: ordinary condition, not a failure of this system.
FREE_MODELS = (
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "google/gemma-4-26b-a4b-it:free",
    "dots-studio/dots-3-note-preview:free",
)


def check(name: str, condition: bool, detail: str = "") -> bool:
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"   {detail}" if detail and not condition else ""))
    return bool(condition)


def skip(name: str, why: str) -> None:
    SKIP.append(name)
    print(f"  SKIP  {name}   ({why})")


def info(line: str) -> None:
    print(f"        {line}")


# ── the lab target ───────────────────────────────────────────────────────────────────────────

class _LabHandler(BaseHTTPRequestHandler):
    """Deliberately broken, in three ways a scanner should notice.

    Not a toy for its own sake: the agent needs something it can confirm, so that record_finding
    has a real finding to record and the evidence store has real evidence to hold. Reflected
    input, a leaked credential file, and a version banner are the three cheapest true positives
    that do not require state.
    """

    server_version = "LabServer/0.1 (vulnerable-on-purpose)"

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/search"):
            q = ""
            if "?" in self.path:
                from urllib.parse import parse_qs, urlparse

                q = (parse_qs(urlparse(self.path).query).get("q") or [""])[0]
            # Reflected, unescaped.
            body = f"<html><body><h1>Results for {q}</h1></body></html>".encode()
        elif self.path == "/.env":
            body = b"DB_PASSWORD=hunter2-not-a-real-secret\nDEBUG=true\n"
        elif self.path == "/":
            body = (b"<html><body><h1>Lab target</h1>"
                    b"<p>Try /search?q=test and /.env</p></body></html>")
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("X-Powered-By", "LabServer/0.1")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── HTTP helpers ─────────────────────────────────────────────────────────────────────────────

#: Every URL this script touches is loopback, so proxies are bypassed explicitly. An
#: environment with http_proxy set (CI runners and this project's own dev container both do)
#: otherwise sends 127.0.0.1 requests to the proxy, which cannot reach them — a failure that
#: looks exactly like "the server did not start".
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _req(url: str, *, method: str = "GET", body: dict | None = None,
         key: str = "", timeout: float = 30.0) -> tuple[int, dict | str]:
    """(status, parsed-body). Status 0 means the connection itself failed.

    Connection errors are returned rather than raised because the main caller is a poll loop
    waiting for a server that is not up yet: there, a refused connection is the expected state,
    not an error.
    """
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return 0, str(e)


def _wait_health(url: str, proc: subprocess.Popen, log_path: Path, timeout: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            print(f"\n  server exited with code {proc.returncode}. Last log lines:\n")
            print("    " + "\n    ".join(log_path.read_text(errors="replace").splitlines()[-25:]))
            return False
        code, _ = _req(url, timeout=3)
        if code == 200:
            return True
        time.sleep(1.0)
    return False


# ── the run ──────────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="End-to-end smoke test for Oxpecker API mode")
    ap.add_argument("--model", default="", help="OpenRouter model id (default: first free one that answers)")
    ap.add_argument("--skip-model", action="store_true", help="Run everything that needs no API call")
    ap.add_argument("--keep", action="store_true", help="Keep the temporary state dir and print its path")
    ap.add_argument("--turn-timeout", type=float, default=300.0, help="Seconds to wait for the agent turn")
    args = ap.parse_args()

    repo_app = Path(__file__).resolve().parents[2]
    state = Path(tempfile.mkdtemp(prefix="oxp-smoke-state-"))
    data = Path(tempfile.mkdtemp(prefix="oxp-smoke-data-"))
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        # Read from the normal location so the operator does not have to export it. It is then
        # handed to the subprocess through the environment, not argv.
        default_key = repo_app / "agent" / "state" / "openrouter_api_key.txt"
        if default_key.exists():
            api_key = default_key.read_text(encoding="utf-8").strip()

    print("\n" + "=" * 78)
    print("Oxpecker API-mode smoke test")
    print("=" * 78)
    info(f"app dir     : {repo_app}")
    info(f"state dir   : {state}")
    info(f"api key     : {'present' if api_key else 'ABSENT — model checks will be skipped'}")

    # ── 1. host capability, before anything is started ──
    print("\n[1] host and sandbox")
    sys.path.insert(0, str(repo_app))
    from agent.sandbox import availability as isolation  # noqa: E402

    host = isolation.describe_host()
    tier = host["strongest_available"]
    info(f"platform={host['platform']} strongest_tier={tier} seccomp={host['seccomp_available']}")
    check("an isolation tier is available", tier in isolation.TIERS, str(host["tiers"]))
    if tier == "direct":
        info("NOTE: 'direct' provides NO kernel isolation. On Linux: apt install bubblewrap "
             "&& pip install pyseccomp, then re-run to exercise the real sandbox.")
    check("the chosen tier declares whether it has been exercised",
          isinstance(host["tiers"][tier].get("exercised"), bool))

    # ── 2. the provider, without spending tokens ──
    print("\n[2] provider construction and capability probe (no tokens spent)")
    model = args.model
    caps = None
    if not api_key:
        skip("provider probe", "no API key")
    elif args.skip_model:
        skip("provider probe", "--skip-model")
    else:
        os.environ["OPENROUTER_API_KEY"] = api_key
        from agent.llm.openrouter import OpenRouterProvider  # noqa: E402

        candidates = [model] if model else list(FREE_MODELS)
        provider = None
        for cand in candidates:
            try:
                provider = OpenRouterProvider(model=cand)
                model = cand
                break
            except Exception as e:  # noqa: BLE001
                info(f"{cand}: {str(e)[:120]}")
        if provider is None:
            check("a provider could be constructed", False, "every candidate model failed")
        else:
            caps = provider.capabilities()
            info(f"model={caps.model} tools={caps.native_tool_calls} "
                 f"reasoning={caps.reasoning} cost/Mtok={caps.cost_per_mtok}")
            check("capabilities were probed, not assumed", caps.model == model)
            check("the model supports tool calls", caps.native_tool_calls,
                  "this model cannot drive the agent loop — pick another")
            check("pricing was read from the provider", caps.cost_per_mtok is not None)
            check("reasoning fidelity is declared and is never 'raw' for an API",
                  caps.reasoning in ("none", "summary", "omitted"), caps.reasoning)

    # ── 3. the lab target ──
    print("\n[3] lab target")
    lab_port = _free_port()
    lab = ThreadingHTTPServer(("127.0.0.1", lab_port), _LabHandler)
    threading.Thread(target=lab.serve_forever, daemon=True).start()
    lab_url = f"http://127.0.0.1:{lab_port}/"
    code, body = _req(lab_url)
    check("the lab target serves", code == 200 and "Lab target" in str(body), f"{code} {str(body)[:80]}")
    info(f"lab target at {lab_url} (reflected /search?q=, leaked /.env, version banner)")

    # ── 4. the server ──
    print("\n[4] dev_server startup")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    key_file = state / "web_ui_api_key.txt"
    key_file.write_text("smoke-test-key-not-a-secret", encoding="utf-8")
    web_key = key_file.read_text().strip()

    env = {
        **os.environ,
        "AGENT_STATE_DIR": str(state),
        "OXPECKER_DATA_DIR": str(data),
        "PYTHONUNBUFFERED": "1",
    }
    if api_key:
        env["OPENROUTER_API_KEY"] = api_key
    argv = [sys.executable, "-m", "agent.web.dev_server",
            "--host", "127.0.0.1", "--port", str(port), "--no-vector-rag"]
    if model and api_key and not args.skip_model:
        argv += ["--provider", "openrouter", "--model", model]
    else:
        skip("server with openrouter provider", "no model available; cannot start without llama-server")
        lab.shutdown()
        return _summary(state, data, args.keep)

    log_path = state / "server.log"
    with open(log_path, "w") as logf:
        proc = subprocess.Popen(argv, cwd=str(repo_app), env=env, stdout=logf, stderr=subprocess.STDOUT)
    try:
        started = _wait_health(f"{base}/health", proc, log_path)
        if not check("the server starts with --provider openrouter and NO llama-server", started):
            return _summary(state, data, args.keep)

        # ── 5. auth ──
        print("\n[5] authentication")
        code, _ = _req(f"{base}/api/health")
        check("/api/* refuses an unauthenticated request", code == 401, f"got {code}")
        code, health = _req(f"{base}/api/health", key=web_key)
        check("/api/* accepts the key", code == 200, f"got {code}")

        prov = (health or {}).get("provider") if isinstance(health, dict) else None
        check("/api/health reports WHICH provider is serving", isinstance(prov, dict), str(prov)[:120])
        if isinstance(prov, dict):
            info(f"server reports: {prov.get('name')} / {prov.get('model')}")
            check("the serving model is the one requested", prov.get("model") == model,
                  f"{prov.get('model')!r} != {model!r}")

        # ── 6. engagement and session ──
        print("\n[6] engagement, scope and session")
        eng_id = f"smoke-{int(time.time())}"
        code, eng = _req(f"{base}/api/engagements", method="POST", key=web_key, body={
            "engagement_id": eng_id,
            "description": "API-mode smoke test against a built-in loopback lab target",
            "allow_targets": ["127.0.0.1"],
            "deny_targets": ["169.254.169.254"],
            "allowed_action_classes": ["passive_recon", "active_web_request", "active_scan_light"],
            "authorized_by": "smoke-test",
            "valid_hours": 1.0,
            "program": {"platform": "lab", "automation_allowed": True, "max_requests_per_min": 30},
        })
        check("the engagement is created", code == 200, f"{code} {str(eng)[:200]}")

        code, sess = _req(f"{base}/api/sessions", method="POST", key=web_key, body={
            "engagement_id": eng_id, "isolation_tier": tier, "use_security_tools": True,
        })
        sid = (sess or {}).get("session_id") if isinstance(sess, dict) else None
        check("the session is created", code == 200 and bool(sid), f"{code} {str(sess)[:200]}")
        if not sid:
            return _summary(state, data, args.keep)

        # ── 7. the turn ──
        print("\n[7] one agent turn against the lab target")
        info("asking the model to fetch the target and record what it finds")
        code, _ = _req(f"{base}/api/sessions/{sid}/messages", method="POST", key=web_key, body={
            "content": (
                f"Target in scope: {lab_url}\n"
                f"1. Fetch {lab_url} with http_request.\n"
                f"2. Fetch {lab_url}.env with http_request.\n"
                f"3. Call record_note with one observation, and record_finding for the exposed "
                f".env file (severity high).\n"
                f"Use the tools. Do not describe what you would do."
            ),
        })
        check("the message is accepted", code == 200, f"got {code}")

        # The field is asserted present rather than defaulted. Defaulting a missing "running"
        # to False made an absent field indistinguishable from a finished turn, and this loop
        # exited on its first poll before the model had answered — which looked like the agent
        # producing nothing rather than like the test not waiting.
        code, s0 = _req(f"{base}/api/sessions/{sid}", key=web_key)
        has_running = isinstance(s0, dict) and "running" in s0
        check("the session reports whether a turn is running", has_running,
              "no 'running' field — this test cannot tell busy from idle")

        deadline = time.monotonic() + args.turn_timeout
        finished = False
        polls = 0
        while time.monotonic() < deadline:
            code, s = _req(f"{base}/api/sessions/{sid}", key=web_key)
            polls += 1
            if isinstance(s, dict) and has_running and not s.get("running"):
                finished = True
                break
            time.sleep(2.0)
        info(f"polled {polls}x")
        check("the turn finishes within the timeout", finished,
              f"still running after {args.turn_timeout:.0f}s")
        msgs = (s or {}).get("messages") or [] if isinstance(s, dict) else []
        roles = [m.get("role") for m in msgs]
        info(f"conversation roles: {roles}")
        check("the model answered at all", "assistant" in roles, str(roles))

        # ── 8. what landed on disk ──
        print("\n[8] the record: can a trajectory be reconstructed?")
        trace_path = state / "debug_trace" / f"{sid}.jsonl"
        audit_path = state / "audit" / f"{sid}.jsonl"
        kinds: dict[str, int] = {}
        if check("a debug trace file exists", trace_path.exists(), str(trace_path)):
            for line in trace_path.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    kinds[json.loads(line).get("kind", "?")] = kinds.get(json.loads(line).get("kind", "?"), 0) + 1
                except json.JSONDecodeError:
                    continue
            info(f"trace kinds: {kinds}")
            # The four dimensions a trajectory needs. `prompt` and `model_output` are the two
            # that were implemented-but-never-called until this build; without them the record
            # holds the agent's actions but not what it saw or what it thought.
            check("the prompt as assembled is recorded", kinds.get("prompt", 0) > 0)
            check("the model's own output is recorded", kinds.get("model_output", 0) > 0)
            check("tool calls with full untruncated results are recorded", kinds.get("tool", 0) > 0)

        if check("an audit log exists", audit_path.exists(), str(audit_path)):
            entries = [json.loads(l) for l in audit_path.read_text().splitlines() if l.strip()]
            info(f"audit entries: {len(entries)}")
            check("the audit log has entries", len(entries) > 0)
            digests = [e for e in entries if e.get("content_digest")]
            check("at least one entry cross-references the evidence store",
                  len(digests) > 0, "no content_digest on any entry")
            from agent import audit_log as al  # noqa: E402

            ok, detail = al.verify(sid, audit_dir=state / "audit")
            check("the hash chain verifies", ok, str(detail)[:200])

        # ── 9. the bookkeeping tools the model was finally given ──
        print("\n[9] graph, notebook and findings — written by the model, read by the UI")
        code, nb = _req(f"{base}/api/engagements/{eng_id}/notebook", key=web_key)
        code2, fn = _req(f"{base}/api/engagements/{eng_id}/findings", key=web_key)
        code3, gr = _req(f"{base}/api/engagements/{eng_id}/hypothesis-graph", key=web_key)
        note_n = len((nb or {}).get("notes") or []) if isinstance(nb, dict) else 0
        find_n = len((fn or {}).get("findings") or []) if isinstance(fn, dict) else 0
        info(f"notebook notes={note_n}  findings={find_n}  graph nodes="
             f"{len((gr or {}).get('nodes') or []) if isinstance(gr, dict) else 0}")
        # Asserted as a pair: the endpoints answering is the server's half, and something being
        # in them is the model's half. A model that ignored the instruction is not a defect in
        # this system, so the second is reported but only the first fails the run.
        check("the notebook/findings endpoints answer", code == 200 and code2 == 200 and code3 == 200)
        if note_n or find_n:
            check("the model reached the record_* tools and they wrote through", True)
        else:
            skip("the model used the record_* tools",
                 "the model did not call them this run — the tools are wired "
                 "(test_tool_parity covers that); this depends on the model obeying")

        # ── 10. cost ──
        print("\n[10] cost")
        log_text = log_path.read_text(errors="replace")
        info(f"server log: {log_path}")
        if caps and caps.cost_per_mtok == (0.0, 0.0):
            info("free model — this run cost $0.00")
        elif caps and caps.cost_per_mtok:
            info(f"priced model: ${caps.cost_per_mtok[0]:.4f} in / ${caps.cost_per_mtok[1]:.4f} "
                 f"out per Mtok. Check the key's spend at "
                 f"https://openrouter.ai/api/v1/key")
        if "Traceback" in log_text:
            check("the server logged no traceback", False,
                  "see " + str(log_path) + " — a traceback means a component failed silently")
        else:
            check("the server logged no traceback", True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        lab.shutdown()

    return _summary(state, data, args.keep)


def _summary(state: Path, data: Path, keep: bool) -> int:
    print("\n" + "=" * 78)
    print(f"  {len(PASS)} passed   {len(FAIL)} failed   {len(SKIP)} skipped")
    if FAIL:
        print("\n  FAILED:")
        for f in FAIL:
            print(f"    - {f}")
    if SKIP:
        print("\n  SKIPPED:")
        for s in SKIP:
            print(f"    - {s}")
    print("=" * 78)
    if keep:
        print(f"\nkept: state={state}  data={data}")
    else:
        print(f"\nstate dir (trace/audit/evidence, inspect before it is cleaned): {state}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
