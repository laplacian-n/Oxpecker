"""Config: constants only, no I/O at import time (paths are computed, not read/created here).

Phase 1 was loopback-only, unauthenticated. Phase 2 (§6) turns on llama-server's own
--api-key auth and adds a separate memory-service credential — never reuse one as the other
(research doc's "Simple LAN auth" section is explicit about this).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = AGENT_DIR.parent
# Every path below is env-overridable (unset -> exactly the prior hardcoded path, so every
# normal run — CLI, web server, in-process tests — is untouched). This exists so a test that
# spawns a *real subprocess* of the web server (agent/web/test_frontend.py's Playwright suite —
# an in-process TestClient can patch these module attributes directly, a subprocess can't) can
# give that subprocess its own throwaway state/engagements tree instead of writing into the
# real ones on every run.
STATE_DIR = Path(os.environ.get("AGENT_STATE_DIR", AGENT_DIR / "state"))
SESSIONS_DIR = STATE_DIR / "sessions"
AUDIT_DIR = STATE_DIR / "audit"
MEMORY_SERVICE_DIR = STATE_DIR / "memory_service"

# --- Inference endpoint ---
LLAMA_SERVER_URL = "http://127.0.0.1:8080"
LLAMA_API_KEY_FILE = STATE_DIR / "llama_api_key.txt"
# 300s, not 120s: hit a real ReadTimeout during M4.4 eval harness runs on a slower-than-usual
# generation (server confirmed healthy immediately after — not a hang, just this hardware's
# worst-case generation time for a longer response exceeding the old margin).
REQUEST_TIMEOUT_S = 300

# --- Phase 2: memory service (the "shared mind") ---
MEMORY_SERVICE_HOST = "0.0.0.0"
MEMORY_SERVICE_PORT = 8443
MEMORY_SERVICE_URL = "https://127.0.0.1:8443"  # client default; override per remote client
MEMORY_SERVICE_DB_PATH = MEMORY_SERVICE_DIR / "memory.db"
MEMORY_SERVICE_DEVICES_PATH = MEMORY_SERVICE_DIR / "devices.json"
MEMORY_SERVICE_CERT_PATH = MEMORY_SERVICE_DIR / "certs" / "memory-service.crt"
MEMORY_SERVICE_KEY_PATH = MEMORY_SERVICE_DIR / "certs" / "memory-service.key"
DEVICE_TOKEN_DEFAULT_TTL_S = 90 * 24 * 3600  # 90 days

# --- Phase 2: MCP tool server (runs on the client machine per Topology B, §6) ---
MCP_SERVER_MODULE = "agent.mcp_tools_server"

# --- Phase 3: broker, RoE/scope, kill switch (§8c, §11) ---
ENGAGEMENT_DIR = Path(os.environ.get("AGENT_ENGAGEMENT_DIR", PROJECT_DIR / "engagement"))  # the single legacy/default engagement (Phase 3-4)
ROE_PATH = ENGAGEMENT_DIR / "roe.json"
SCOPE_PATH = ENGAGEMENT_DIR / "scope.txt"
DENY_PATH = ENGAGEMENT_DIR / "deny.txt"
# Phase 5 M5.1: multiple named engagements, created via agent/engagement/intake.py. Additive —
# ENGAGEMENT_DIR above is untouched, so every Phase 3-4 broker/policy test and default CLI
# behavior keeps working exactly as before. A new engagement lives at
# ENGAGEMENTS_ROOT/<engagement_id>/{roe.json,scope.txt,deny.txt} and is used by passing that
# path as Broker(engagement_dir=...)/load_policy(engagement_dir=...) — both already accepted
# an explicit engagement_dir parameter before this pass, so no broker/policy code changed.
ENGAGEMENTS_ROOT = Path(os.environ.get("AGENT_ENGAGEMENTS_ROOT", PROJECT_DIR / "engagements"))
# M5.5: knowledge_search/knowledge_fetch enabled live per the owner's explicit choice (asked
# directly since real internet egress is a first for this project — see ADR-0007). SearXNG runs
# as a local Docker container (`docker run ... -p 127.0.0.1:8888:8080 searxng/searxng`, same
# loopback-only pattern as the Juice Shop/DVWA lab targets) so this project queries one
# self-hosted metasearch instance rather than sending every query to a single third party.
KNOWLEDGE_SEARCH_SEARXNG_URL = "http://127.0.0.1:8888"
KNOWLEDGE_SEARCH_ALLOWED_PROVIDERS = frozenset({"searxng-local"})
KNOWLEDGE_SEARCH_MAX_RESULTS = 10
KNOWLEDGE_FETCH_TIMEOUT_S = 10
KNOWLEDGE_FETCH_MAX_BYTES = 500_000
KILL_SWITCH_PATH = STATE_DIR / "KILL_SWITCH_ENGAGED"
APPROVAL_QUEUE_TIMEOUT_S = 300  # how long an async (use_approval_queue=True) dispatch waits for
                                 # an out-of-band resolution before denying — matches the kind
                                 # of "operator steps away" window REQUEST_TIMEOUT_S's comment
                                 # already reasons about for generation latency, applied here to
                                 # human response latency instead.
IDEMPOTENCY_CACHE_PATH = STATE_DIR / "broker" / "idempotency_cache.json"
SCOPE_CHECK_TIMEOUT_S = 5  # DNS resolution budget; APTS-SE-006 wants <100ms for the *check*
                            # itself once resolved — DNS lookup latency is a separate, network-
                            # bound cost this timeout caps rather than pretends away.
PORT_DISCOVERY_MAX_PORTS = 100  # blast-radius limit per single action
PORT_DISCOVERY_CONNECT_TIMEOUT_S = 1.5
HTTP_RECON_TIMEOUT_S = 8
HTTP_RECON_MAX_REDIRECTS = 5
HTTP_RECON_MAX_BODY_BYTES = 20_000
ACTION_CLASS_COOLDOWN_S = {
    "passive_recon": 1.0,
    "active_scan_light": 2.0,
    "knowledge_search": 2.0,
    "knowledge_fetch": 2.0,
    "browser_recon": 3.0,  # browser startup/render time alone is >1s; a tighter cooldown
                            # would just queue calls behind each other anyway
}
SECURITY_MCP_SERVER_MODULE = "agent.security_mcp_server"

# --- Phase 4: sandboxing, evidence store, findings, reporting, eval ---
EVIDENCE_DIR = STATE_DIR / "evidence"
EVIDENCE_KEY_PATH = EVIDENCE_DIR / "evidence_key.bin"
EVIDENCE_INDEX_PATH = EVIDENCE_DIR / "index.jsonl"
EVIDENCE_ACCESS_LOG_PATH = EVIDENCE_DIR / "access_log.jsonl"
FINDINGS_DIR = STATE_DIR / "findings"
EVAL_RESULTS_DIR = STATE_DIR / "eval_results"
# The Working Notebook is per-engagement; `technique` notes also overflow into this ONE global
# store so a reusable trick learned on engagement A is recallable on engagement B
# (docs/working-notebook-spec.md §7). Not engagement-scoped on purpose.
TECHNIQUE_KB_PATH = STATE_DIR / "technique_kb.db"

# --- General-knowledge RAG (agent/knowledge_rag/) — GTFOBins/LOLBAS/PayloadsAllTheThings,
# embedded offline, served by a dedicated CPU-only llama-server instance so it never competes
# with the main GPU-loaded decision model for VRAM (see the module docstring for why).
KNOWLEDGE_RAG_EMBED_SERVER_URL = "http://127.0.0.1:8091"
KNOWLEDGE_RAG_DIR = AGENT_DIR / "knowledge_rag"
KNOWLEDGE_RAG_INDEX_PATH = KNOWLEDGE_RAG_DIR / "index" / "vectors.npy"
KNOWLEDGE_RAG_META_PATH = KNOWLEDGE_RAG_DIR / "index" / "meta.jsonl"

# --- Resource-budget manager (research doc, Phase 1 §"Resource management") ---
RESERVED_OUTPUT_TOKENS = 2048
SAFETY_MARGIN = 0.10
# 4000 was too high for the full production tool surface: measured live (2026-09-09,
# use_security_tools + use_hypothesis_graph, 25 tool schemas), fixed overhead (system prompt +
# tool schemas + graph/notebook digests) alone is ~8.4k tokens against a 12.7k budget at
# n_ctx=16384, leaving ~4.3k for the whole turn. A single tool result truncated to 4000 ate
# nearly all of that, leaving no room for the user task + tool-call args + final response —
# reproduced as context_budget_exhausted on a real http_recon call against Juice Shop with
# BOTH the base model and a LoRA-equipped one (same failure either way, confirming this is a
# margin bug, not model-specific).
# 2800 (2026-09-09 fix) was STILL too high once a real task made 2+ tool calls before
# converging: measured live (2026-09-10) against BOTH Qwen3-32B-abliterated (~7.8k fixed
# overhead) and Qwen3.6-35B-A3B-abliterated (~8.0k fixed overhead, near-identical despite a
# different tokenizer/tool-call format) — remaining headroom per turn is only ~4.7-4.9k tokens.
# Two results truncated to 2800 (2527 + 2328 actual) alone hit context_budget_exhausted at
# 14434 > 12697, even after eviction and summarization, with zero tool-call reasoning yet to
# blame. 1300 leaves room for at least 3 substantial tool calls in one turn before the fixed
# overhead + results exhaust the budget, for either model.
# 1300 was STILL too high for a harsher task requiring a full test-to-verdict cycle (recon +
# attempt_start + attempt_complete, each returning real content) — measured live (2026-09-10):
# 3 truncated results (1079+1091+1092) alone hit 13070 > 12697. Tried raising n_ctx instead of
# shrinking the cap further: Qwen3-32B-abliterated at -ngl 44 cannot fit even 20480 (let alone
# 24576) — cudaMalloc OOM on the KV-cache buffer both times, confirming the 32B dense model is
# already at its VRAM ceiling at 16384 with full GPU offload. Qwen3.6's CPU-MoE-offloaded config
# has VRAM headroom to spare, but keeping n_ctx identical across models matters more for a
# controlled comparison than exploiting that asymmetry. 1000 leaves room for 4+ substantial tool
# calls per turn for either model under the current n_ctx=16384 ceiling.
MAX_SINGLE_RESULT_TOKENS = 1000
MIN_KEEP_TURNS = 4
SUMMARY_INPUT_BUDGET = 6000
SUMMARY_MAX_TOKENS = 512

# --- Loop / safety guards (§8b) ---
MAX_ITERATIONS = 25
TOOL_CALL_TIMEOUT_S = 30
TASK_WALL_CLOCK_S = 600
REPEAT_THRESHOLD = 3

# --- Inference profiles ---
NO_THINK_SUFFIX = "/no_think"
PROFILE_SAFE_DEFAULT = "safe-default"
PROFILE_DIAGNOSTIC_THINKING = "diagnostic-thinking"
DEFAULT_PROFILE = PROFILE_SAFE_DEFAULT
DIAGNOSTIC_MAX_TOKENS = 4096
SAFE_DEFAULT_MAX_TOKENS = 1024

# A real 900s A/B experiment (2026-08-31) tried enabling thinking for every safe-default turn,
# tool calls included, and it never finished a single task: this hardware decodes at a fixed
# ~3.1-4.0 tok/s regardless of thinking, but a thinking turn runs 250-320+ tokens vs. ~34-90 for
# a plain /no_think turn — a 3-8x latency multiplier on *every* tool-calling turn compounds into
# tens of minutes per task. ANALYSIS is the one phase that pays a bounded, one-time cost instead:
# it's tool-call-free by design (phases/analysis.md — reasons over existing observations, makes
# no new tool calls), so a deeper-reasoning pass there costs latency once per phase transition,
# not once per tool call. Every other phase stays on the fast /no_think path.
THINKING_ENABLED_PHASES = {"ANALYSIS"}
# Bounded well under TASK_WALL_CLOCK_S (600s) even at the slowest observed decode rate (~3.1
# tok/s: 1536 / 3.1 ≈ 495s), leaving margin for prompt-eval time — not the same as
# DIAGNOSTIC_MAX_TOKENS (4096), which has no wall-clock budget to fit inside.
ANALYSIS_THINKING_MAX_TOKENS = 1536
# REQUEST_TIMEOUT_S (300s) is deliberately tight for ordinary /no_think completions (real hang
# detection, see its own comment) and is too short for a 1536-token thinking budget at this
# hardware's worst-case ~3.1 tok/s (found live: a first verification run hit exactly this
# ReadTimeout at 300s while still decoding). This is that call's own, correspondingly larger,
# still-bounded timeout — comfortably fits ANALYSIS_THINKING_MAX_TOKENS at the worst-case rate
# (1536/3.1 ≈ 495s decode + prompt-eval overhead) while staying under TASK_WALL_CLOCK_S (600s).
ANALYSIS_THINKING_REQUEST_TIMEOUT_S = 560

# --- Tool execution: subprocess-level safety caps ---
RUN_COMMAND_MAX_OUTPUT_BYTES = 100_000
READ_FILE_MAX_BYTES = 200_000
WRITE_FILE_MAX_BYTES = 200_000

# Binaries blocked by default (Phase-1 safety boundary: "no network-capable commands,
# no privilege escalation"). --dangerous-local can relax this after explicit confirmation.
NETWORK_BINARIES = {
    "curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp", "ftp", "tftp",
    "telnet", "nmap", "ping", "ping6", "traceroute", "dig", "nslookup", "host",
    "rsync", "socat", "openssl" ,  # openssl s_client can reach the network
}
PRIVESC_BINARIES = {"sudo", "su", "doas", "pkexec", "setuid", "runas"}
BLOCKED_BINARIES = NETWORK_BINARIES | PRIVESC_BINARIES

# Commands that run without a confirmation prompt (conservative, read-only-ish).
# Anything else still runs (if not blocked) but asks the human first.
COMMAND_ALLOWLIST = {
    "ls", "cat", "pwd", "echo", "head", "tail", "wc", "grep", "find", "sort",
    "uniq", "diff", "mkdir", "touch", "file", "stat", "tree", "cut", "sed", "awk",
    "python3", "date", "env",
}

# Path fragments that always block a run_command argv element (credential dirs),
# regardless of --dangerous-local — Phase-1 safety boundary treats these as out of scope
# to relax, not just a default.
CREDENTIAL_PATH_MARKERS = (
    ".ssh", ".gnupg", ".gpg", ".aws", ".config/gcloud", ".kube", ".docker/config.json",
    "id_rsa", "id_ed25519", "shadow", ".netrc", ".git-credentials",
)

# Environment variable name fragments never forwarded into a tool subprocess.
SECRET_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH")

WORKSPACE_PREFIX = "agent-workspace-"

# --- Phase 6: web UI backend (agent/web/server.py) ---
# Loopback-only by default — matches Phase 1's own default-safe posture for a single-operator
# local tool. If WEB_UI_API_KEY_FILE exists, every /api/* route requires it (Bearer header, or
# a ?key= query param for the one browser API that can't set headers — EventSource); if it
# doesn't exist, auth is off, exactly the old unauthenticated behavior (every existing test
# relies on this). A separate credential from LLAMA_API_KEY_FILE and the memory service's device
# tokens — "never reuse one as the other" (see LLAMA_API_KEY_FILE's own comment) applies here
# too. This is deliberately lighter than the memory service's multi-device DeviceStore (no
# rotation, no per-device identity) — appropriate for "one operator, maybe one other LAN device",
# not a shared multi-user deployment; reach for the heavier pattern if that need ever shows up.
WEB_UI_HOST = "127.0.0.1"
WEB_UI_PORT = 8765
WEB_UI_API_KEY_FILE = STATE_DIR / "web_ui_api_key.txt"


def default_workspace_root() -> Path:
    """A fresh temp dir per process — Phase-1 default per the safety boundary."""
    return Path(tempfile.mkdtemp(prefix=WORKSPACE_PREFIX))
