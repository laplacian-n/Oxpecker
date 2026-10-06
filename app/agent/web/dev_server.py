"""Oxpecker standalone dev server — runs on any machine with llama-server (llama.cpp).
Designed for: Windows laptop, RTX 4050 6GB, Qwen3.5-4B-Q6_K.

Implements the FULL API surface that agent/web/static/index.html expects, with:
  - LLM inference via llama-server's OpenAI-compatible API (streaming)
  - RAG retrieval via TF-IDF (no embedding server needed)
  - In-memory session, engagement, approval, hypothesis graph, notebook, findings stores
  - SSE streaming for real-time token delivery
  - WebSocket support for bidirectional comms

Prerequisites:
  pip install fastapi uvicorn scikit-learn numpy

Usage:
  Step 1 — Start llama-server (in a separate terminal):
    llama-server.exe -m Qwen3.5-4B-Q6_K.gguf --port 8080 -ngl 99 --cors "*"

  Step 2 — Start Oxpecker dev server:
    python dev_server.py --port 7777

  Then open http://127.0.0.1:7777 in your browser.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import pathlib
import platform
import shlex
import queue
import re
import textwrap
import threading
import time
import urllib.request
import urllib.error
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect

from .. import audit_log as _audit_log
from ..broker import broker as _broker_mod
from ..broker.contracts import ActionRequest as _ActionRequest
from .. import injection_guard as _injection_guard
from ..engagement import intake as _intake
from ..sandbox import availability as _isolation
from ..security_tools import port_discovery as _port_discovery
from ..tools import run_command as _run_command
from . import debug_trace as _debug
from . import scope as _scope
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ═══════════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════════

log = logging.getLogger("oxpecker.dev")

STATIC_DIR = Path(__file__).resolve().parent / "static"
DATA_DIR = Path(__file__).resolve().parent / "dev_data"
RAG_CORPUS_DIR = DATA_DIR / "rag_corpus"
API_VERSION = "1.0.0-dev"

def _platform_facts() -> str:
    """Tell the model what the run_command host actually is, so a small model
    stops reaching for Linux-only tools on Windows (and vice versa)."""
    if platform.system() == "Windows":
        return textwrap.dedent("""\
        HOST ENVIRONMENT: run_command executes on Windows via cmd.exe. Linux-only tools
        (nmap, ss, lsof, dig, nc, curl) are usually NOT installed. Use Windows equivalents:
          - TCP port / connectivity:  powershell -NoProfile -Command "Test-NetConnection <host> -Port <port>"
          - HTTP GET (headers+body):  powershell -NoProfile -Command "(Invoke-WebRequest -UseBasicParsing <url>).Content"
          - HTTP headers only:        powershell -NoProfile -Command "(Invoke-WebRequest -UseBasicParsing -Method Head <url>).Headers"
          - DNS lookup:               nslookup <host>
          - Listening ports:          netstat -ano
          - Ping / traceroute:        ping -n 4 <host>   |   tracert <host>
        Prefer a single `powershell -NoProfile -Command "..."` for anything non-trivial.
        Each run_command runs in a fresh shell: no cwd or environment persists between calls.""")
    return textwrap.dedent("""\
        HOST ENVIRONMENT: run_command executes on a POSIX shell (Linux/macOS). Common tools
        (curl, dig, ss, nc, and nmap if installed) are available. Prefer non-interactive flags.""")


DEFAULT_SYSTEM_PROMPT = textwrap.dedent("""\
You are Oxpecker, a self-hosted AI penetration testing assistant. You help security \
professionals with reconnaissance, vulnerability analysis, exploitation, and reporting.

You have access to the following tools:
- http_request: Send an HTTP request to an in-scope target (status + headers + body). PREFER
  this for ALL web testing — send payloads as structured fields instead of shell-quoting.
- run_command: Execute a shell command on the host (see HOST ENVIRONMENT below)
- read_file: Read file contents from the workspace
- write_file: Write or create files in the workspace
- knowledge_search: Search the local security knowledge base
- record_hypothesis / update_hypothesis_status: track theories in the hypothesis graph
- record_note: keep a running notebook (technique / dead-end / todo / observation)
- record_finding: log a confirmed vulnerability with evidence

WEB TESTING RULE: Use http_request — NOT run_command/PowerShell — to probe URLs and send
SQLi/XSS/auth payloads. PowerShell quote-escaping wastes turns and corrupts payloads.
- FIRST MOVE on a web target: call http_request GET on the operator's EXACT url (the
  http://host:port/ you were given) before anything else. Do NOT warm up with Test-NetConnection,
  Invoke-WebRequest, ping, or netstat — they tell you nothing useful about a remote web app.
- TARGET LOCK: work the operator's target and the "In-scope targets" list below. NEVER switch to
  localhost / 127.0.0.1 and never wander to the local machine. If a request fails, retry
  http_request against the SAME host (try http vs https, or a different path) rather than
  changing the host.
- SCOPE IS ENFORCED, not advisory. A tool result saying "out of scope" means the request was
  never sent. Do not retry it, do not try a variation of the host to get around it, and never
  describe a response you did not receive. Report the refusal to the operator and say which host
  needs authorising — only the operator can add a target to the engagement.
- Do NOT use netstat / Test-NetConnection / Get-NetTCPConnection / Get-Process to investigate a
  web target — those show YOUR machine, not the remote server, and are a dead-end loop.
For OWASP Juice Shop the real login API is POST /rest/user/login with JSON
{"email":"<payload>","password":"<payload>"}; the SQLi bypass is email "' OR 1=1--".
Angular routes like /#/login are client-side only — never POST to them.

When given a target and scope, plan your approach systematically:
1. Enumerate services and open ports
2. Identify potential vulnerabilities
3. Attempt exploitation with appropriate caution
4. Document findings with severity ratings

Tool-calling rules:
- Call a tool by emitting its function call — NEVER describe what you are about to do and then
  end the turn. If you write "let me…", "now I'll…", "next I will…", "let me try…", you MUST
  emit that tool call in the SAME turn. An announcement with no tool call is a FAILED turn.
- Chain freely: keep calling tools, step after step, until the objective is met. Do NOT stop
  after one tool to ask "should I continue?" — continue on your own. A normal turn runs several
  tools: probe, read the result, decide, probe again.
- Do NOT repeat a tool call you already made this session with the same arguments. The earlier
  results are in the conversation — read them and take the NEXT action instead of re-sending
  identical requests.
- Only end your turn when (a) the task is done and you are giving the final report with real
  evidence, or (b) you are truly blocked and must ask the operator one specific question.
- When you say a vulnerability is likely, immediately test it (send the payload) in the same
  turn rather than announcing the plan — confirm or refute it with a tool, then move on.
- Use the exact target host/URL given by the operator. Never substitute localhost/127.0.0.1
  unless the operator's target is actually local.

EVIDENCE RULE (do not violate):
- Report ONLY what a tool actually returned this session. Never invent ports, services,
  versions, CVEs, or findings, and never copy an example into a result as if it were real.
- If you have not verified something with a tool, say "not verified" — do not guess.
- netstat / Get-NetTCPConnection / Get-Process show the LOCAL machine you run on, NOT the
  target. Never attribute local ports or processes to the target.
- Quote the real evidence (the command and a snippet of its output) when you state a finding.

TRACK YOUR WORK (keep the hypothesis graph and notebook alive — do this continuously, not just at the end):
- When you form a theory about a weakness, call record_hypothesis (title, what you'll test, phase)
  BEFORE you test it. Link it to a parent with parent_ordinal when it follows from an earlier one.
- When a tool confirms or refutes that theory, call update_hypothesis_status (status=completed,
  verdict=confirmed or refuted, evidence=the real proof you just saw). NEVER mark a hypothesis
  confirmed or refuted until you have ACTUALLY run the test with a tool — if you recorded a
  hypothesis to test the login SQLi, you must send that POST payload before judging it. No verdict
  without evidence from a tool call this turn.
- Jot record_note as you work: a technique that worked, a dead-end to avoid, a todo, an observation.
- Call record_finding for every CONFIRMED vulnerability, with evidence.
A real red-teamer leaves a trail — the operator watches the hypothesis tree and notebook fill up
as you go, so keep them current round by round.

Always respect the Rules of Engagement. Never scan or attack targets outside the defined scope.

{platform_facts}

Be direct and autonomous: when you state an intention, act on it in the SAME turn — never end a
turn on "let me…" or "next I'll…". Drive the engagement forward yourself (recon → analysis →
exploitation → evidence) without waiting to be nudged step by step. Do not pad with long \
preamble — the operator wants commands run and real results, not a plan recited back.
""").replace("{platform_facts}", _platform_facts())


# ═══════════════════════════════════════════════════════════════════════════════
# LLM Client (llama-server OpenAI-compatible API)
# ═══════════════════════════════════════════════════════════════════════════════

class LLMClient:
    """Talks to llama-server via its OpenAI-compatible /v1/chat/completions endpoint."""

    def __init__(self, server_url: str = "http://127.0.0.1:8080"):
        self.base_url = server_url.rstrip("/")
        log.info("Connecting to llama-server at %s", self.base_url)
        self._check_health()

    def _check_health(self):
        try:
            req = urllib.request.Request(f"{self.base_url}/health")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            status = data.get("status", "unknown")
            if status == "ok":
                log.info("llama-server is ready")
            else:
                log.warning("llama-server status: %s (may still be loading)", status)
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Cannot connect to llama-server at {self.base_url}.\n"
                f"Start it first:\n"
                f"  llama-server -m your-model.gguf --port 8080 -ngl 99\n\n{e}"
            ) from e

    def chat(self, messages: list[dict], *, max_tokens: int = 3072,
             temperature: float = 0.6, stream: bool = False,
             tools: list[dict] | None = None, enable_thinking: bool = False) -> Any:
        # Qwen3-recommended sampling (temp 0.6 / top_p 0.95 / top_k 20); a bigger
        # max_tokens so a <think> block can't eat the whole budget and leave empty content.
        payload = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": 0.95,
            "top_k": 20,
            "repeat_penalty": 1.1,
            "stream": stream,
            # Qwen3 thinking: off by default — a 4B otherwise spends the whole token budget
            # inside <think> and often returns EMPTY content. When on, llama.cpp surfaces the
            # reasoning as delta.reasoning_content (handled by the agent loop) so the UI can
            # show it without the answer being swallowed.
            "chat_template_kwargs": {"enable_thinking": enable_thinking},
        }
        if stream:
            # Ask llama-server for a trailing usage chunk so the UI can show the REAL
            # prompt-token count (drives the context monitor + makes compaction visible).
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = tools
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions", data=data,
            headers={"Content-Type": "application/json"},
        )

        if not stream:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())

        return self._stream_response(req)

    def _stream_response(self, req):
        """Read SSE stream from llama-server (OpenAI format: 'data: {...}' lines)."""
        resp = urllib.request.urlopen(req, timeout=300)
        try:
            buffer = b""
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    if line == b"data: [DONE]":
                        return
                    if line.startswith(b"data: "):
                        try:
                            obj = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue
                        yield obj
        finally:
            resp.close()

    def token_count(self, text: str) -> int:
        return len(text) // 4


# ═══════════════════════════════════════════════════════════════════════════════
# RAG: TF-IDF vector store (CPU-only, no embedding server)
# ═══════════════════════════════════════════════════════════════════════════════

class TFIDFStore:
    """Simple TF-IDF-based RAG store. Loads text files from a corpus directory,
    builds a TF-IDF matrix in memory, and does cosine-similarity search."""

    def __init__(self):
        self.documents: list[dict] = []
        self.vectorizer = None
        self.tfidf_matrix = None

    def index_directory(self, corpus_dir: Path) -> int:
        if not corpus_dir.exists():
            return 0
        texts, metas = [], []
        for fp in sorted(corpus_dir.rglob("*")):
            if fp.suffix not in (".txt", ".md", ".json", ".jsonl"):
                continue
            try:
                content = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if fp.suffix == ".jsonl":
                for line in content.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        text = obj.get("text", "") or obj.get("content", "")
                        if not text:
                            continue
                        meta = {
                            "title": obj.get("title", fp.stem),
                            "source": obj.get("source", fp.parent.name),
                            "url": obj.get("url", ""),
                            "tags": obj.get("tags", []),
                            "text": text[:2000],
                        }
                        texts.append(text[:2000])
                        metas.append(meta)
                    except json.JSONDecodeError:
                        continue
            else:
                chunks = self._chunk_text(content, max_chars=1500)
                for i, chunk in enumerate(chunks):
                    meta = {
                        "title": fp.stem + (f" (part {i+1})" if len(chunks) > 1 else ""),
                        "source": fp.parent.name,
                        "url": "",
                        "tags": [],
                        "text": chunk,
                    }
                    texts.append(chunk)
                    metas.append(meta)

        if not texts:
            return 0

        from sklearn.feature_extraction.text import TfidfVectorizer
        self.vectorizer = TfidfVectorizer(
            max_features=50000,
            stop_words="english",
            ngram_range=(1, 2),
            sublinear_tf=True,
        )
        self.tfidf_matrix = self.vectorizer.fit_transform(texts)
        self.documents = metas
        return len(texts)

    def index_from_existing_meta(self, meta_path: Path) -> int:
        """Load from the project's existing meta.jsonl (the same format knowledge_rag uses)."""
        if not meta_path.exists():
            return 0
        texts, metas = [], []
        with open(meta_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    text = obj.get("text", "")
                    if not text:
                        continue
                    texts.append(text[:2000])
                    metas.append(obj)
                except json.JSONDecodeError:
                    continue
        if not texts:
            return 0
        from sklearn.feature_extraction.text import TfidfVectorizer
        self.vectorizer = TfidfVectorizer(
            max_features=50000,
            stop_words="english",
            ngram_range=(1, 2),
            sublinear_tf=True,
        )
        self.tfidf_matrix = self.vectorizer.fit_transform(texts)
        self.documents = metas
        return len(texts)

    def search(self, query: str, top_k: int = 5, source: str | None = None) -> list[dict]:
        if self.vectorizer is None or self.tfidf_matrix is None:
            return []
        q_vec = self.vectorizer.transform([query])
        from sklearn.metrics.pairwise import cosine_similarity
        scores = cosine_similarity(q_vec, self.tfidf_matrix).flatten()
        order = scores.argsort()[::-1]
        results = []
        for i in order:
            if scores[i] < 0.01:
                break
            doc = self.documents[i]
            if source and doc.get("source") != source:
                continue
            results.append({
                "title": doc.get("title", ""),
                "text": doc.get("text", ""),
                "source": doc.get("source", ""),
                "url": doc.get("url", ""),
                "score": round(float(scores[i]), 3),
                "tags": doc.get("tags", []),
            })
            if len(results) >= top_k:
                break
        return results

    def count(self) -> int:
        return len(self.documents)

    @staticmethod
    def _chunk_text(text: str, max_chars: int = 1500) -> list[str]:
        if len(text) <= max_chars:
            return [text] if text.strip() else []
        chunks = []
        lines = text.split("\n")
        current = []
        current_len = 0
        for line in lines:
            if current_len + len(line) + 1 > max_chars and current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            current.append(line)
            current_len += len(line) + 1
        if current:
            chunks.append("\n".join(current))
        return [c for c in chunks if c.strip()]


class VectorRAG:
    """Dense RAG over a large prebuilt index, kept memory-safe.

    A 547k-chunk index is ~1.7GB of vectors + ~1GB of JSONL metadata; loading both into
    RAM (np.load + [json.loads(l) for l in f]) costs ~5GB and OOMs a 24GB box that is
    already full. Instead: vectors are memory-MAPPED (paged by the OS, ~0 resident), and
    metadata is read on demand via a cached byte-offset index — so steady-state RAM is a
    few MB. Query embedding goes to a small nomic-embed llama-server (/embedding).
    """

    def __init__(self, vectors_path: Path, meta_path: Path, embed_url: str,
                 query_prefix: str = "search_query: "):
        import numpy as np
        self._np = np
        self.vectors = np.load(str(vectors_path), mmap_mode="r")  # (n, dim), L2-normalized rows
        self.meta_path = Path(meta_path)
        self.embed_url = embed_url.rstrip("/")
        self.query_prefix = query_prefix
        self.offsets = self._load_or_build_offsets()
        self._n = int(min(len(self.offsets), self.vectors.shape[0]))
        self._dim = int(self.vectors.shape[1])

    def _load_or_build_offsets(self):
        np = self._np
        off_path = self.meta_path.with_suffix(".offsets.npy")
        try:
            if off_path.exists() and off_path.stat().st_mtime >= self.meta_path.stat().st_mtime:
                return np.load(str(off_path))
        except OSError:
            pass
        offs = []
        with open(self.meta_path, "rb") as f:
            pos = f.tell()
            line = f.readline()
            while line:
                if line.strip():
                    offs.append(pos)
                pos = f.tell()
                line = f.readline()
        arr = np.array(offs, dtype=np.int64)
        try:
            np.save(str(off_path), arr)
        except OSError:
            pass
        return arr

    def embed_query(self, text: str):
        payload = json.dumps({"content": [self.query_prefix + text]}).encode()
        req = urllib.request.Request(
            f"{self.embed_url}/embedding", data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        # llama.cpp shapes vary: [{"embedding":[[...]]}] or [{"embedding":[...]}] or {"embedding":[...]}
        row = data[0] if isinstance(data, list) else data
        emb = row.get("embedding", row) if isinstance(row, dict) else row
        if isinstance(emb, list) and emb and isinstance(emb[0], list):
            emb = emb[0]
        return self._np.asarray(emb, dtype=self._np.float32)

    def search(self, query: str, top_k: int = 5, source: str | None = None) -> list[dict]:
        query = (query or "").strip()
        if not query or self._n == 0:
            return []
        np = self._np
        try:
            q = self.embed_query(query)
        except Exception as e:  # degrade silently — RAG is reference, never load-bearing
            log.warning("embed query failed: %s", e)
            return []
        if q.shape[0] != self._dim:
            return []
        qn = np.linalg.norm(q)
        if qn > 0:
            q = q / qn
        scores = np.asarray(self.vectors[:self._n] @ q)  # memmap matmul, streams from page cache
        pool = int(min(max(top_k * 8, 40), self._n))
        cand = np.argpartition(-scores, pool - 1)[:pool]
        cand = cand[np.argsort(-scores[cand])]
        qtok = set(re.findall(r"[a-z0-9]{2,}", query.lower()))
        results, seen = [], set()
        with open(self.meta_path, "rb") as f:
            for i in cand:
                f.seek(int(self.offsets[i]))
                try:
                    m = json.loads(f.readline())
                except json.JSONDecodeError:
                    continue
                if source and m.get("source") != source:
                    continue
                key = (m.get("source", ""), (m.get("title", "") or "")[:40])
                if key in seen:
                    continue
                seen.add(key)
                tags = {str(t).lower() for t in (m.get("tags") or [])}
                ttok = set(re.findall(r"[a-z0-9]{2,}", (m.get("title", "") or "").lower()))
                bonus = 0.08 * len(qtok & tags) + 0.05 * len(qtok & ttok)
                results.append({
                    "title": m.get("title", ""), "text": (m.get("text", "") or "")[:2000],
                    "source": m.get("source", ""), "url": m.get("url", ""),
                    "score": round(float(scores[i]) + bonus, 3), "tags": list(tags),
                })
        results.sort(key=lambda r: -r["score"])
        return results[:top_k]

    def count(self) -> int:
        return self._n


# ═══════════════════════════════════════════════════════════════════════════════
# In-memory stores
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Session:
    session_id: str
    messages: list[dict] = field(default_factory=list)
    events: queue.Queue = field(default_factory=queue.Queue)
    running: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    created_at: float = field(default_factory=time.time)
    use_security_tools: bool = False
    engagement_id: str = "lab-default"
    isolation_tier: str = "bubblewrap"
    # What the last run_command actually executed under. `isolation_tier` above is the
    # operator's REQUEST; this is the outcome. They can differ, and this is None when a command
    # was refused because the requested tier was unavailable. Reported separately so the API
    # never implies isolation that did not happen.
    effective_isolation_tier: str | None = None
    profile: str = "safe"
    autonomous_active: bool = False
    autonomous_mode: str = ""
    stop_requested: bool = False
    thinking: bool = False     # expose the model's <think> reasoning to the UI when True
    last_prompt_tokens: int = 0  # real prompt-token count from llama-server's last turn
    summary: str = ""          # running condensed summary of folded-away older turns
    summary_upto: int = 0      # messages[:summary_upto] are represented by `summary`
    stage: str = "RECON"       # current pentest stage (operator-driven, not auto-advanced)
    guided: bool = False       # follow the staged pentest protocol (vs free chat)
    pending_steers: list[str] = field(default_factory=list)  # operator notes to inject mid-run

    def push(self, event: dict):
        self.events.put(event)

    def append(self, role: str, content: str, extra: dict | None = None) -> dict:
        record = {
            "role": role,
            "content": content,
            "message_id": str(uuid.uuid4())[:8],
            "timestamp": time.time(),
        }
        if extra:
            record.update(extra)
        self.messages.append(record)
        self.push({"type": "message", "record": record})
        return record


@dataclass
class Engagement:
    engagement_id: str
    description: str = ""
    allow_targets: list[str] = field(default_factory=list)
    # Hosts the operator has mentioned but not authorised. Never consulted by the scope check —
    # see _scope_urls_into_engagement.
    proposed_targets: list[str] = field(default_factory=list)
    allowed_action_classes: list[str] = field(default_factory=list)
    authorized_by: str = ""
    valid_until: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "engagement_id": self.engagement_id,
            "description": self.description,
            "allow_targets": self.allow_targets,
            "proposed_targets": self.proposed_targets,
            "allowed_action_classes": self.allowed_action_classes,
            "valid_until": self.valid_until,
        }


@dataclass
class ApprovalRequest:
    request_id: str
    session_id: str
    tool: str = ""
    description: str = ""
    command: str = ""
    detail: str = ""
    prompt: str = ""
    status: str = "pending"
    resolved_by: str = ""
    event: threading.Event = field(default_factory=threading.Event)
    approved: bool = False


@dataclass
class ConsultRequest:
    request_id: str
    session_id: str
    question: str = ""
    prompt: str = ""
    status: str = "pending"
    resolved_by: str = ""
    event: threading.Event = field(default_factory=threading.Event)
    answer: str = ""


@dataclass
class HypothesisNode:
    ordinal: int
    title: str
    claim: str = ""
    description: str = ""
    phase: str = "RECON"
    status: str = "open"
    verdict: str = ""
    parent_ordinal: int | None = None
    why: str = ""
    attempts: list[dict] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_summary(self) -> dict:
        return {
            "ordinal": self.ordinal, "title": self.title, "claim": self.claim,
            "phase": self.phase, "status": self.status, "verdict": self.verdict,
            "parent_ordinal": self.parent_ordinal,
        }

    def to_detail(self) -> dict:
        return {**self.to_summary(), "description": self.description, "why": self.why,
                "attempts": self.attempts, "observations": self.observations, "notes": self.notes}


@dataclass
class NotebookNote:
    ordinal: int
    text: str
    category: str = "observation"
    refs: list[str] = field(default_factory=list)
    resolved: bool = False
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"ordinal": self.ordinal, "text": self.text, "category": self.category,
                "refs": self.refs, "resolved": self.resolved, "created_at": self.created_at}


@dataclass
class Finding:
    finding_id: str
    title: str
    severity: str = "medium"
    description: str = ""
    target: str = ""
    text: str = ""
    reviewed_by: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"finding_id": self.finding_id, "title": self.title, "severity": self.severity,
                "description": self.description, "target": self.target, "text": self.text,
                "reviewed_by": self.reviewed_by, "created_at": self.created_at}


# Global stores
_sessions: dict[str, Session] = {}
_engagements: dict[str, Engagement] = {"lab-default": Engagement(
    engagement_id="lab-default", description="Local lab environment",
    allow_targets=["127.0.0.1", "localhost"],
    # These are enforced now, and must be names the broker knows (broker.TOOL_ACTION_CLASS).
    # The previous value included "active_recon", which is not one — it was free text that
    # nothing validated and nothing checked, so it neither permitted nor denied anything.
    allowed_action_classes=["passive_recon", "active_web_request", "active_scan_light",
                            "knowledge_search"],
    authorized_by="operator",
)}
_approvals: dict[str, ApprovalRequest] = {}
_consults: dict[str, ConsultRequest] = {}
_graphs: dict[str, list[HypothesisNode]] = {}
_graph_edges: dict[str, list[dict]] = {}
_notebooks: dict[str, list[NotebookNote]] = {}
_findings: dict[str, list[Finding]] = {}
_technique_kb: list[dict] = []

# Serializes access to the single-slot llama-server: concurrent generations (e.g. two
# MCP-driven turns at once) otherwise pile onto `-np 1` and wedge the slot.
_LLM_LOCK = threading.Lock()

# LLM and RAG globals (set during startup)
_llm: LLMClient | None = None
_rag: TFIDFStore = TFIDFStore()


# ═══════════════════════════════════════════════════════════════════════════════
# Persistence — best-effort JSON snapshot so a dev_server restart keeps sessions,
# engagements, findings, hypotheses and notebooks (all otherwise in-memory only).
# ═══════════════════════════════════════════════════════════════════════════════

STATE_FILE = DATA_DIR / "state.json"
_persist_lock = threading.Lock()


def _mk(cls, d: dict):
    """Build a dataclass from a dict, ignoring unknown keys."""
    return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _persist():
    try:
        data = {
            "sessions": [
                {"session_id": s.session_id, "messages": s.messages, "created_at": s.created_at,
                 "use_security_tools": s.use_security_tools, "engagement_id": s.engagement_id,
                 "isolation_tier": s.isolation_tier,
                 "effective_isolation_tier": s.effective_isolation_tier, "profile": s.profile,
                 "autonomous_mode": s.autonomous_mode, "thinking": s.thinking,
                 "stage": s.stage, "guided": s.guided,
                 "summary": s.summary, "summary_upto": s.summary_upto}
                for s in list(_sessions.values())
            ],
            "engagements": [
                {"engagement_id": e.engagement_id, "description": e.description,
                 "allow_targets": e.allow_targets, "proposed_targets": e.proposed_targets,
                 "allowed_action_classes": e.allowed_action_classes,
                 "authorized_by": e.authorized_by, "valid_until": e.valid_until, "created_at": e.created_at}
                for e in list(_engagements.values())
            ],
            "findings": {eid: [f.to_dict() for f in fs] for eid, fs in _findings.items()},
            "graphs": {eid: [n.to_detail() for n in ns] for eid, ns in _graphs.items()},
            "graph_edges": _graph_edges,
            "notebooks": {eid: [n.to_dict() for n in ns] for eid, ns in _notebooks.items()},
        }
        with _persist_lock:
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(STATE_FILE)
    except Exception as e:
        log.warning("persist failed: %s", e)


def _load_state():
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("load state failed: %s", e)
        return
    for e in data.get("engagements", []):
        _engagements[e["engagement_id"]] = _mk(Engagement, e)
    for eid, fs in data.get("findings", {}).items():
        _findings[eid] = [_mk(Finding, f) for f in fs]
    for eid, ns in data.get("graphs", {}).items():
        _graphs[eid] = [_mk(HypothesisNode, n) for n in ns]
    _graph_edges.update(data.get("graph_edges", {}))
    for eid, ns in data.get("notebooks", {}).items():
        _notebooks[eid] = [_mk(NotebookNote, n) for n in ns]
    for sd in data.get("sessions", []):
        s = Session(
            session_id=sd["session_id"], use_security_tools=sd.get("use_security_tools", False),
            engagement_id=sd.get("engagement_id", "lab-default"),
            isolation_tier=sd.get("isolation_tier", "bubblewrap"),
            effective_isolation_tier=sd.get("effective_isolation_tier"),
            profile=sd.get("profile", "safe"),
            created_at=sd.get("created_at", time.time()),
        )
        s.messages = sd.get("messages", [])
        s.autonomous_mode = sd.get("autonomous_mode", "")
        s.thinking = sd.get("thinking", False)
        s.stage = sd.get("stage", "RECON")
        s.guided = sd.get("guided", False)
        s.summary = sd.get("summary", "")
        s.summary_upto = sd.get("summary_upto", 0)
        _sessions[s.session_id] = s
    log.info("Restored state: %d sessions, %d engagements, %d finding-sets",
             len(_sessions), len(_engagements), len(_findings))


# ═══════════════════════════════════════════════════════════════════════════════
# Tool execution (sandboxed)
# ═══════════════════════════════════════════════════════════════════════════════

# ── Safety net: refuse clearly destructive host commands (no approval prompt, just a block).
# This is a seatbelt, not a scope wall — it stops a 4B from wiping the dev box by accident.
_DESTRUCTIVE_PATTERNS = [
    r"\bformat\s+[a-zA-Z]:",             # format C:
    r"\bformat-volume\b", r"\bdiskpart\b", r"\bmkfs(\.\w+)?\b",
    r"\bdel\s+/[a-zA-Z]*[fsq]",          # del /f /s /q
    r"\berase\s+/[a-zA-Z]*[fsq]",
    r"\b(?:rd|rmdir)\s+/s\b",            # rd /s
    r"\brm\s+-\w*[rf]\w*[rf]", r"\brm\s+-r\b",   # rm -rf / -fr / -r
    r"\bremove-item\b.*-recurse",        # Remove-Item ... -Recurse
    r"\bshutdown\b", r"\b(?:stop|restart)-computer\b",
    r"\breg\s+delete\b", r"\bcipher\s+/w",
    r"\bdd\s+if=.*of=/dev/", r">\s*/dev/sd[a-z]",
    r":\(\)\s*\{.*\|.*&.*\}\s*;",         # fork bomb
]
_DESTRUCTIVE_RE = re.compile("|".join(_DESTRUCTIVE_PATTERNS), re.IGNORECASE | re.DOTALL)

# An HTTP fetch via the shell (Invoke-WebRequest / Invoke-RestMethod / curl / wget against an
# http(s) URL) → redirect to the http_request tool. Plain TCP probes (Test-NetConnection,
# netstat, nslookup) are left alone.
# Port/host scanning via the shell is not refused because scanning is wrong — it is the job —
# but because a shell scan answers to nothing: no scope check, no blast-radius cap, no kill
# switch, no record of what was probed. port_discovery enforces all four. Steering the model
# there is also cheaper for it than fighting nmap's output format.
_SCAN_IN_CMD_RE = re.compile(
    r"\b(?:nmap|masscan|rustscan|zmap|unicornscan|hping3?|nc|ncat|netcat)\b"
    r"|Test-NetConnection|New-Object\s+System\.Net\.Sockets",
    re.IGNORECASE,
)

_HTTP_IN_CMD_RE = re.compile(
    r"(?:invoke-webrequest|invoke-restmethod|\biwr\b|\bcurl\b|\bwget\b).*?https?://|https?://\S+.*?(?:invoke-webrequest|invoke-restmethod)",
    re.IGNORECASE | re.DOTALL,
)

# ── PowerShell routing: run PS via list-args (shell=False) so pipes/quotes inside a
# `powershell -Command "... | Select-String ..."` don't get mangled by cmd.exe.
_PS_WRAPPER_RE = re.compile(
    r"^\s*(?:powershell|pwsh)(?:\.exe)?\b(?:\s+-[^\s]+)*?\s+-(?:Command|c)\s+(?P<body>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_PS_CMDLET_RE = re.compile(
    r"\b(?:Invoke|Get|Set|Test|Select|New|Remove|Start|Stop|ConvertTo|ConvertFrom|"
    r"Out|Where|ForEach|Measure|Resolve|Format|Add|Clear|Export|Import)-\w+",
    re.IGNORECASE,
)


# Shell constructs the sandbox cannot honour. There is no shell inside it — the executor runs an
# argv vector directly — so these would reach the program as literal text and the model would be
# left reading a result that silently did not do what it asked. Refusing with the reason costs
# one turn; a silently wrong result costs the whole line of reasoning.
_SHELL_FEATURE_RE = re.compile(r"\|\||&&|[|;&<>]|\$\(|`")


class _ShellFeatureError(ValueError):
    pass


class _PathEscape(ValueError):
    pass


def _resolve_in_data_dir(path: str) -> pathlib.Path:
    """Resolve `path` under DATA_DIR, or refuse.

    Both file tools used `str(fp).startswith(str(DATA_DIR))`, which is a string-prefix test, not
    a containment test: a sibling directory whose name merely begins with the same characters
    passes it. `dev_data_evil` sat outside `dev_data` and read as inside, so `read_file` returned
    its contents and `write_file` created files there. Verified before the fix; a test now holds
    it.

    `is_relative_to` tests actual containment. Resolution happens first, so ordinary `../`
    traversal and symlinks pointing outside both resolve to a path that fails the check.
    """
    root = DATA_DIR.resolve()
    fp = (root / path).resolve()
    if fp != root and not fp.is_relative_to(root):
        raise _PathEscape(
            f"path escapes the workspace: {path!r} resolves outside {root.name}/"
        )
    return fp


def _parse_argv(args: dict) -> list[str]:
    """Accept either `argv` (a list, preferred) or `command` (a string, split with shlex).

    The sandboxed executor takes an argv vector and never invokes a shell, which is what keeps
    `preflight()`'s check on argv[0] meaningful: with `sh -c "<string>"` the real binary hides
    inside the string and the blocked-binary list stops applying.
    """
    argv = args.get("argv")
    if isinstance(argv, list) and argv:
        return [str(a) for a in argv]
    if isinstance(argv, str) and argv.strip():
        # A model that reaches for argv and then passes a string meant the command line; saying
        # "empty command" at that point is both wrong and unhelpful. Treat it as `command`.
        args = {**args, "command": argv}

    cmd = str(args.get("command") or args.get("cmd") or "").strip()
    if not cmd:
        raise _ShellFeatureError('empty command — pass argv:["ls","-la"] or command:"ls -la"')

    if match := _SHELL_FEATURE_RE.search(cmd):
        raise _ShellFeatureError(
            f"{match.group(0)!r} is a shell construct and there is no shell in the sandbox, so "
            "this was NOT run. Run one program per call and combine the results yourself: "
            "instead of 'grep x f | wc -l', call grep and count what comes back. For a pipeline "
            'you genuinely need, pass it to python3 as one argument: '
            'argv:["python3","-c","<script>"].'
        )
    try:
        parsed = shlex.split(cmd)
    except ValueError as e:
        raise _ShellFeatureError(f"could not parse the command ({e}) — check the quoting") from e
    if not parsed:
        raise _ShellFeatureError("the command parsed to nothing")
    return parsed


def _powershell_body(cmd: str) -> str | None:
    """If cmd wraps a PowerShell script (`powershell -Command "..."`), return the inner
    script; if it's a bare PowerShell cmdlet pipeline, return it as-is; else None (use cmd.exe)."""
    m = _PS_WRAPPER_RE.match(cmd)
    if m:
        body = m.group("body").strip()
        if len(body) >= 2 and body[0] in "\"'" and body[-1] == body[0]:
            body = body[1:-1]
        return body
    if _PS_CMDLET_RE.search(cmd):   # bare cmdlet pipeline, no wrapper
        return cmd.strip()
    return None


# A small model often picks a near-miss tool name — accept the obvious synonyms.
_TOOL_NAME_ALIASES = {
    "run": "run_command", "bash": "run_command", "sh": "run_command", "shell": "run_command",
    "exec": "run_command", "execute": "run_command", "command": "run_command", "run_cmd": "run_command",
    "read": "read_file", "cat": "read_file", "open_file": "read_file", "readfile": "read_file",
    "write": "write_file", "create_file": "write_file", "writefile": "write_file", "save_file": "write_file",
    "search": "knowledge_search", "kb_search": "knowledge_search", "search_knowledge": "knowledge_search",
    "knowledge": "knowledge_search", "knowledgebase": "knowledge_search",
    "nmap": "port_discovery", "scan": "port_discovery", "port_scan": "port_discovery",
    "portscan": "port_discovery", "scan_ports": "port_discovery",
    "http": "http_request", "request": "http_request", "fetch": "http_request",
    "curl": "http_request", "http_get": "http_request", "http_post": "http_request",
    "web_request": "http_request", "httprequest": "http_request",
}


def _run_tool(name: str, args: dict, session: Session) -> dict:
    """Execute a tool call. Returns the tool result dict."""
    name = _TOOL_NAME_ALIASES.get(name, name)
    if name == "run_command":
        # Parse first, then screen the RESOLVED argv. The screening regexes below used to read
        # `args["command"]` directly; once `argv` became an accepted input form that left a
        # bypass, because argv:["rm","-rf","/"] never passed through the string they inspect.
        # Joining the parsed argv means both input forms are screened identically.
        try:
            argv = _parse_argv(args)
        except _ShellFeatureError as e:
            return {"ok": False, "error": str(e)}
        cmd = " ".join(argv)

        if _DESTRUCTIVE_RE.search(cmd):
            return {"ok": False, "error": "blocked: this looks like a destructive host command "
                                          "(format/shutdown/recursive-delete/etc.) and was NOT run. "
                                          "Pentest the target over the network instead."}
        # Deterministically steer HTTP fetches to http_request: a small model keeps reaching
        # for Invoke-WebRequest/curl and then either loses to quote-escaping or hallucinates a
        # response from echoed output. http_request returns the REAL status+body instead.
        if _SCAN_IN_CMD_RE.search(cmd):
            return {"ok": False, "error": (
                "Do not scan with run_command. Use the port_discovery tool instead: "
                "{\"host\":\"<host>\", \"ports\":[22,80,443,3000]}. It checks the host "
                "against the engagement scope, connects to the validated IP (so the target "
                "cannot be swapped by DNS mid-scan), caps the ports per call, and honours the "
                "kill switch — none of which a shell scan does."
            )}
        if _HTTP_IN_CMD_RE.search(cmd):
            return {"ok": False, "error": "Do not fetch HTTP with run_command. Use the http_request "
                                          "tool instead: {\"url\":\"<full url incl. path>\", "
                                          "\"method\":\"GET|POST\", \"headers\":{...}, \"body\":{...}}. "
                                          "It returns the real status, headers, and body — no shell quoting."}
        # Executed through agent/tools/run_command.py — the same path the CLI uses — so this
        # inherits its blocked-binary preflight, credential-path refusal, workspace confinement
        # and output capping, and runs inside the isolation tier the operator selected. It used
        # to be `subprocess.run(cmd, shell=True)` on the host with only the destructive-command
        # regex in front of it, while `session.isolation_tier` reported "bubblewrap" to the API
        # and was read by nothing.
        if platform.system() != "Linux":
            return {"ok": False, "error": (
                f"run_command is unavailable on {platform.system() or 'this platform'}: the "
                "isolation tiers are Linux-only (bubblewrap namespaces + seccomp), and the "
                "command runner resolves binaries against a Unix PATH. Running commands "
                "unsandboxed on the host is not offered as a fallback. The network tools "
                "(http_request, port_discovery) work on every platform and cover web and "
                "service testing; local command execution needs a Linux host or WSL2."
            )}
        requested_tier = getattr(session, "isolation_tier", "bubblewrap") or "bubblewrap"
        try:
            # Selecting "direct" IS the operator's explicit opt-out, so no fallback flag is
            # needed: resolve_tier returns direct when direct was asked for, and raises when a
            # stronger tier was asked for and cannot run. It never silently downgrades.
            tier, tier_reason = _isolation.resolve_tier(requested_tier)
        except _isolation.IsolationUnavailableError as e:
            session.effective_isolation_tier = None
            return {"ok": False, "error": (
                f"{e} This command was NOT run. The operator can install bubblewrap, or select "
                f"the 'direct' isolation tier for this session to accept running without "
                f"kernel isolation."
            )}
        except ValueError as e:
            return {"ok": False, "error": f"invalid isolation tier {requested_tier!r}: {e}"}

        session.effective_isolation_tier = tier
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            result = _run_command.run(
                argv, workspace_root=DATA_DIR, isolation_tier=tier, dangerous_local=False,
            )
        except FileNotFoundError as e:
            # The availability probe is static; a tier can still fail at exec time.
            session.effective_isolation_tier = None
            return {"ok": False, "error": (
                f"the {tier!r} isolation tier failed at execution ({e}), so the command was NOT "
                "run. This host reported the tier as available but could not use it."
            )}
        except Exception as e:
            # Nothing completed, so no tier describes what ran — same reason a preflight-blocked
            # command reports none.
            session.effective_isolation_tier = None
            return {"ok": False, "error": f"execution failed: {e}"}

        # Flatten to the shape this tool has always returned, and state the tier that actually
        # ran rather than the one the session advertises. A command stopped by preflight never
        # reached the executor, so no tier applies to it — reporting the requested tier there
        # would claim an isolated execution that did not happen, which is the same confusion
        # `effective_isolation_tier` exists to remove.
        blocked = bool(result.get("blocked"))
        ran_under = result.get("isolation_tier") or (None if blocked else tier)
        session.effective_isolation_tier = ran_under
        out = ((result.get("stdout") or "") + (result.get("stderr") or ""))[:1500]
        return {
            "ok": bool(result.get("ok")) and not result.get("blocked"),
            "exit_code": result.get("exit_code"),
            "output": out,
            "error": result.get("error"),
            "blocked": blocked,
            "timed_out": result.get("timed_out", False),
            "killed_reason": result.get("killed_reason"),
            "output_capped": result.get("output_capped", False),
            "duration_ms": result.get("duration_ms"),
            "isolation_tier": ran_under,
            "isolation_detail": tier_reason,
            "sandbox_profile_digest": result.get("sandbox_profile_digest"),
        }

    elif name == "read_file":
        path = args.get("path", "")
        if not path:
            return {"ok": False, "error": "empty path"}
        try:
            try:
                fp = _resolve_in_data_dir(path)
            except _PathEscape as e:
                return {"ok": False, "error": str(e)}
            content = fp.read_text(encoding="utf-8", errors="replace")[:8000]
            return {"ok": True, "content": content}
        except FileNotFoundError:
            return {"ok": False, "error": f"file not found: {path}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    elif name == "write_file":
        path = args.get("path", "")
        content = args.get("content", "")
        if not path:
            return {"ok": False, "error": "empty path"}
        try:
            try:
                fp = _resolve_in_data_dir(path)
            except _PathEscape as e:
                return {"ok": False, "error": str(e)}
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")
            return {"ok": True, "path": str(fp)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    elif name == "http_request":
        # Structured HTTP — the web-pentest workhorse. Lets the model send payloads
        # (SQLi/XSS/auth) as JSON fields instead of fighting PowerShell quote-escaping,
        # which was the #1 cause of wasted tool rounds. Enforces engagement scope.
        url = (args.get("url") or "").strip()
        if not url:
            return {"ok": False, "error": "empty url"}
        if not re.match(r"^https?://", url, re.IGNORECASE):
            url = "http://" + url
        method = str(args.get("method") or "GET").upper()
        headers = args.get("headers") or {}
        if not isinstance(headers, dict):
            headers = {}
        # NOTE: _normalize_tool_args aliases body/data → "content" (for write_file), so the
        # request body can arrive under any of these keys. Check all of them.
        body = args.get("body")
        if body is None:
            body = args.get("data")
        if body is None:
            body = args.get("content")
        # Scope is decided by agent/web/scope.py, which delegates to the broker's own matcher
        # (broker/scope_check.validate_target). The check that used to live inline here did
        # `host in str(t).lower()` — substring matching in the permissive direction, so
        # `evil.example.com` passed whenever `notevil.example.com` was in scope — and skipped
        # itself entirely when allow_targets was empty, which `create_engagement` allowed by
        # default. See scope.py's docstring for the full list of what was wrong with it.
        decision = _scope.check_url(url, _engagements.get(session.engagement_id))
        if not decision.allowed:
            return decision.as_tool_error()
        host = decision.host
        data = None
        if body is not None:
            if isinstance(body, (dict, list)):
                data = json.dumps(body).encode()
                headers.setdefault("Content-Type", "application/json")
            else:
                data = str(body).encode()
        hdrs = {str(k): str(v) for k, v in headers.items()}
        hdrs.setdefault("User-Agent", "Oxpecker/1.0")
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read(200000)
                return {"ok": True, "status": resp.status,
                        "headers": dict(resp.headers),
                        "body": raw.decode("utf-8", "replace")[:4000]}
        except urllib.error.HTTPError as e:
            raw = e.read(200000)
            return {"ok": True, "status": e.code, "headers": dict(e.headers),
                    "body": raw.decode("utf-8", "replace")[:4000]}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    elif name == "knowledge_search":
        query = args.get("query", "")
        if not query:
            return {"ok": False, "error": "empty query"}
        results = _rag.search(query, top_k=args.get("top_k", 5))
        return {"ok": True, "results": results, "count": len(results)}

    elif name == "record_hypothesis":
        eng_id = session.session_id  # graph is per-session (each chat keeps its own tree)
        if eng_id not in _graphs:
            _graphs[eng_id] = []
            _graph_edges[eng_id] = []
        nodes = _graphs[eng_id]
        title = (args.get("title") or "").strip()
        if not title:
            return {"ok": False, "error": "title is required for a hypothesis"}
        # Dedup: a 4B often re-records the same hypothesis several times in one turn.
        for n in nodes:
            if (n.title or "").strip().lower() == title.lower():
                return {"ok": True, "hypothesis_id": f"h-{n.ordinal}", "ordinal": n.ordinal, "deduped": True}
        ordinal = len(nodes) + 1
        node = HypothesisNode(
            ordinal=ordinal,
            title=args.get("title", ""),
            claim=args.get("description", args.get("claim", "")),
            description=args.get("description", ""),
            phase=args.get("phase", "RECON"),
            parent_ordinal=args.get("parent_ordinal"),
        )
        nodes.append(node)
        if node.parent_ordinal:
            _graph_edges[eng_id].append({
                "from_ordinal": node.parent_ordinal,
                "to_ordinal": ordinal,
                "edge_type": "derives",
            })
        return {"ok": True, "hypothesis_id": f"h-{ordinal}", "ordinal": ordinal}

    elif name == "update_hypothesis_status":
        eng_id = session.session_id
        nodes = _graphs.get(eng_id, [])
        h_id = args.get("hypothesis_id", "")
        ordinal = int(re.sub(r"[^0-9]", "", h_id)) if h_id else args.get("ordinal", 0)
        for n in nodes:
            if n.ordinal == ordinal:
                n.status = args.get("status", n.status)
                n.verdict = args.get("verdict", n.verdict)
                if args.get("evidence"):
                    n.attempts.append({"method": "evidence", "result": args["evidence"], "status": "done"})
                return {"ok": True, "hypothesis_id": h_id}
        return {"ok": False, "error": f"hypothesis {h_id} not found"}

    elif name == "record_finding":
        eng_id = session.session_id
        title = (args.get("title") or "").strip()
        desc = (args.get("description") or "").strip()
        # Reject malformed/empty findings (a 4B sometimes emits a junk call with only a stray
        # "raw" blob and no real title/description — that used to create an empty finding).
        if not title or not desc:
            return {"ok": False, "error": "a finding needs both a title and a description"}
        if eng_id not in _findings:
            _findings[eng_id] = []
        # Dedup by title so repeated calls in one turn don't pile up duplicates.
        for f in _findings[eng_id]:
            if (f.title or "").strip().lower() == title.lower():
                return {"ok": True, "finding_id": f.finding_id, "deduped": True}
        # Monotonic id from the max existing ordinal (not len) — len-based ids collide
        # after a finding is deleted (e.g. f-1,f-3 → len 2 → a second "f-3").
        max_ord = 0
        for f in _findings[eng_id]:
            m = re.match(r"f-(\d+)$", f.finding_id or "")
            if m:
                max_ord = max(max_ord, int(m.group(1)))
        fid = f"f-{max_ord + 1}"
        finding = Finding(
            finding_id=fid,
            title=args.get("title", ""),
            severity=args.get("severity", "medium"),
            description=args.get("description", ""),
            target=args.get("target", ""),
        )
        _findings[eng_id].append(finding)
        return {"ok": True, "finding_id": fid}

    elif name == "record_note":
        eng_id = session.session_id
        if eng_id not in _notebooks:
            _notebooks[eng_id] = []
        notes = _notebooks[eng_id]
        ordinal = len(notes) + 1
        cat = args.get("category", "observation")
        if cat not in ("technique", "dead-end", "todo", "observation"):
            cat = "observation"
        # _normalize_tool_args aliases text → content globally (for write_file), so a note's
        # body can arrive under either key.
        note_text = (args.get("text") or args.get("content") or "").strip()
        if not note_text:
            return {"ok": False, "error": "note text is required"}
        for n in notes:
            if (n.text or "").strip() == note_text:
                return {"ok": True, "note_ordinal": n.ordinal, "deduped": True}
        notes.append(NotebookNote(ordinal=ordinal, text=note_text, category=cat,
                                  refs=args.get("refs", []) or []))
        return {"ok": True, "note_ordinal": ordinal}

    elif name == "port_discovery":
        # Delegates to the same security_tools implementation the CLI uses, with a Policy built
        # from this session's engagement. That brings four things the web runtime had no way to
        # do: the scope check, a cap on ports per call, probing the validated IP rather than
        # re-resolving the hostname mid-scan, and the kill switch.
        host = str(args.get("host") or "").strip()
        ports = args.get("ports") or []
        if not host:
            return {"ok": False, "error": "host is required"}
        if isinstance(ports, (str, int)):
            ports = [ports]
        try:
            ports = [int(p) for p in ports]
        except (TypeError, ValueError):
            return {"ok": False, "error": f"ports must be integers, got {args.get('ports')!r}"}
        if not ports:
            return {"ok": False, "error": "at least one port is required"}

        eng = _engagements.get(session.engagement_id)
        # Reuse the same gate http_request goes through, so a missing, empty or expired
        # engagement denies here for the same reason and with the same wording.
        pre = _scope.check_url(f"http://{host}/", eng)
        if not pre.allowed:
            return pre.as_tool_error()
        try:
            return _port_discovery.run(host, ports, _scope.policy_from_engagement(eng))
        except PermissionError as e:
            return {"ok": False, "error": f"out of scope: {e}. This scan was NOT run.",
                    "scope_rule": "not_in_scope"}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except OSError as e:
            return {"ok": False, "error": f"scan failed: {e}"}

    return {"ok": False, "error": f"unknown tool: {name}"}


# Tools that touch anything outside this process go through the broker. run_command does not:
# local execution is gated by the sandbox and its preflight (see the isolation work), and
# run_command is deliberately absent from broker.TOOL_ACTION_CLASS. read/write_file and the
# record_* tools stay local and in-memory.
_BROKER_MEDIATED = {"http_request", "port_discovery", "knowledge_search"}


class _AlreadyAudited(Exception):
    """Control-flow marker: the broker already wrote this action's audit entry."""

    def __init__(self, digest):
        super().__init__(digest)
        self.digest = digest

_brokers: dict[str, _broker_mod.Broker] = {}


def _deny_approval(prompt: str) -> bool:
    """Approval is refused rather than awaited.

    Broker's default confirm_fn reads stdin, which would hang a FastAPI worker, and
    use_approval_queue=True would block it waiting for an out-of-band resolution that this
    runtime has no endpoint to provide yet. Denying is the fail-closed answer and cannot hang
    the server; the denial reaches the operator with the broker's own reason attached.

    Nothing among the three mediated tools requires approval today (REQUIRES_APPROVAL is empty
    and none is the insecure-TLS case), so this is a guard against a future change rather than a
    live path. Wiring real approval means a web endpoint plus session taint marking, which
    belong together — marking taint without somewhere to approve would leave the agent limited
    to passive recon for the 5-minute taint window with no operator recourse.
    """
    log.warning("broker requested approval and this runtime cannot ask: denying — %s", prompt)
    return False


def _broker_for(engagement_id: str) -> _broker_mod.Broker:
    broker = _brokers.get(engagement_id)
    if broker is None:
        broker = _brokers[engagement_id] = _broker_mod.Broker(
            # Read through on every dispatch, so an engagement edited mid-session takes effect
            # on the next call rather than being captured once at construction.
            policy_loader=lambda eid=engagement_id: _scope.policy_from_engagement(
                _engagements.get(eid)
            ),
            confirm_fn=_deny_approval,
            use_approval_queue=False,
        )
    return broker


def _dispatch_via_broker(canonical: str, args: dict, session: Session, *, turn_index: int,
                         action_rationale: str, audit_args: dict) -> tuple[dict, dict]:
    """Run a tool through the broker. Returns (tool-result dict, broker metadata).

    The broker adds, on top of the scope check the tool already does: the RoE action-class
    gate, the kill switch, the per-action-class rate limit, taint escalation, the encrypted
    evidence store holding the full untruncated output, an audit entry whose content digest
    cross-references that evidence, and idempotent replay.
    """
    request = _ActionRequest(
        tool=canonical,
        # audit_args carries the alias annotation; the executor strips it before the tool sees
        # it, so the trail keeps "the model asked for nmap" without the tool receiving an
        # argument it never declared.
        arguments=audit_args,
        session_id=session.session_id,
        device_id="web-ui",
        engagement_id=session.engagement_id,
        turn_index=turn_index,
        action_rationale=action_rationale[:300],
    )
    def execute(policy, arguments):
        tool_args = {k: v for k, v in arguments.items() if not k.startswith("_requested_")}
        out = _run_tool(canonical, tool_args, session)
        # port_discovery raises PermissionError on an out-of-scope target, which the broker
        # records as a denial. dev_server's http_request returns a refusal dict instead, and the
        # broker would then record status "succeeded" with policy_rule "n/a" for a request that
        # was refused — the audit trail would say the call went through. Translate it so a
        # refusal is recorded as one however the tool chose to report it.
        if isinstance(out, dict) and out.get("ok") is False and out.get("scope_rule"):
            raise PermissionError(out.get("error") or out["scope_rule"])
        return out

    response = _broker_for(session.engagement_id).dispatch(request, execute)
    meta = {
        "broker_status": response.status,
        "policy_rule": response.policy_rule,
        "audit_record_digest": response.audit_record_digest,
        "audit_entry_id": response.audit_entry_id,
        "evidence_digest": response.evidence_digest,
        "duration_ms": response.duration_ms,
    }
    if response.status == "succeeded":
        # The broker strips _policy_rule and _exit_metadata off the raw result into the response,
        # so put the rule back where the tool's own callers expect it.
        out = dict(response.output) if isinstance(response.output, dict) else {
            "ok": True, "output": response.output}
        out.setdefault("_policy_rule", response.policy_rule)
        return out, meta

    # Not every non-success is a policy block, and conflating them would tell the model the
    # wrong thing: "denied"/"needs_approval" mean the call never happened, while
    # "timed_out"/"failed" mean it was attempted and did not complete. A model told its request
    # was blocked when it actually timed out will go looking for permission instead of retrying.
    if response.status in ("denied", "needs_approval", "budget_exhausted", "cancelled"):
        return {
            "ok": False,
            "error": f"blocked by the execution broker ({response.policy_rule}): "
                     f"{response.detail or response.status}. This call was NOT made.",
            "scope_rule": response.policy_rule,
        }, meta
    return {
        "ok": False,
        "error": f"the call was attempted and did not complete ({response.status}): "
                 f"{response.detail or 'no detail'}",
        "broker_status": response.status,
    }, meta


_audit_logs: dict[str, _audit_log.AuditLog] = {}


# Per-session handles are cached so each session keeps one hash chain. Bounded because a
# long-lived server would otherwise hold one object per session seen since start; the entries are
# only caches — a dropped one is rebuilt on next use, and AuditLog re-reads the chain head from
# disk on every write, so rebuilding cannot fork the chain.
_AUDIT_LOG_CACHE_MAX = 200


def _audit_for(session_id: str) -> _audit_log.AuditLog:
    """One AuditLog per session, matching Broker._audit_for. Writes land in config.AUDIT_DIR
    beside the CLI's, so `python3 -m agent.main --verify-audit <session_id>` verifies a web
    session's chain too — the web runtime having no audit trail at all was the gap this closes,
    and a trail only the web UI can read would be half a fix."""
    log_obj = _audit_logs.get(session_id)
    if log_obj is None:
        if len(_audit_logs) >= _AUDIT_LOG_CACHE_MAX:
            for stale in list(_audit_logs)[: len(_audit_logs) - _AUDIT_LOG_CACHE_MAX + 1]:
                _audit_logs.pop(stale, None)
        log_obj = _audit_logs[session_id] = _audit_log.AuditLog(session_id)
    return log_obj


def _scope_decision_of(result: dict) -> str:
    """What the gate decided, in the one string the audit schema has for it."""
    if not isinstance(result, dict):
        return "n/a"
    if rule := result.get("scope_rule"):
        return f"denied:{rule}"
    if result.get("blocked"):
        return "denied:preflight"
    for key in ("validated_ip", "host"):
        if result.get(key):
            return f"allowed:{key}={result[key]}"
    return "allowed" if result.get("ok") else "n/a"


def _audited_run_tool(name: str, args: dict, session: Session, *, turn_index: int,
                      action_rationale: str) -> tuple[dict, str]:
    """Execute a tool and record it. Returns (result, text ready to enter context).

    Every tool call goes through here because _run_tool has exactly one call site — so the audit
    trail cannot be bypassed by a tool forgetting to write one, and adding a tool cannot
    accidentally opt out of it.

    Two things happen here that did not happen anywhere in the web runtime before: the call is
    recorded in the hash-chained audit log, and the output is screened for prompt injection and
    wrapped as data before the model sees it. injection_guard was already in this package,
    unused on this path.
    """
    canonical = _TOOL_NAME_ALIASES.get(name, name)
    audit_args = dict(args)
    if canonical != name:
        # Keep the spelling the model used: 27 aliases reach these tools, and "the model asked
        # for nmap" is a different fact from "port_discovery ran".
        audit_args["_requested_tool_name"] = name
    started = time.monotonic()
    broker_meta: dict = {}
    try:
        if canonical in _BROKER_MEDIATED:
            # The broker writes its own audit entry and evidence record in _finalize, so the
            # local write below is skipped for these — one action, one entry.
            result, broker_meta = _dispatch_via_broker(
                canonical, args, session, turn_index=turn_index,
                action_rationale=action_rationale or "", audit_args=audit_args,
            )
        else:
            result = _run_tool(name, args, session)
    except Exception as e:  # a tool raising must still be recorded, not swallowed
        log.exception("tool %r raised", canonical)
        result = {"ok": False, "error": f"tool raised {type(e).__name__}: {e}"}
    latency_ms = (time.monotonic() - started) * 1000

    raw_text = _jsonify_result(result)
    wrapped, scan = _injection_guard.wrap_and_flag(raw_text)

    audit_entry_id = None
    try:
        if broker_meta:
            # Already recorded by Broker._finalize, with the evidence digest cross-referenced
            # against the audit entry's content digest. Writing a second entry here would make
            # the trail say an action happened twice.
            #
            # Correlate on the entry id, not the chain hash. audit_record_digest is the chain
            # link; entry_id is what the entry is keyed by, and so what another record has to
            # point at. Storing the hash here left the trace reader joining nothing.
            raise _AlreadyAudited(broker_meta.get("audit_entry_id"))
        entry = _audit_for(session.session_id).record(
            turn_index=turn_index,
            tool_name=canonical,
            action_rationale=(action_rationale or "")[:300],
            arguments=audit_args,
            raw_output=raw_text,
            sanitized_output=wrapped,
            scope_decision=_scope_decision_of(result),
            approval_identity=None,  # the approval queue is not wired into this runtime yet
            prompt_tokens=session.last_prompt_tokens or None,
            completion_tokens=None,  # not reported per tool call by this runtime
            latency_ms=latency_ms,
            exit_code=result.get("exit_code") if isinstance(result, dict) else None,
            injection_flagged=scan.matched,
            isolation_tier=(result.get("isolation_tier") if isinstance(result, dict) else None),
        )
        audit_entry_id = entry.get("entry_id")
    except _AlreadyAudited as already:
        audit_entry_id = already.digest
    except Exception as e:
        # Never fail the turn on an audit error, but never let one pass unnoticed either: a
        # silently missing entry is indistinguishable from an action that never happened.
        log.error("AUDIT WRITE FAILED for %s/%s: %s", session.session_id, canonical, e)
        session.push({"type": "audit_error", "tool": canonical, "error": str(e)})

    # Chunk identity and score per hit, recorded here rather than inside the knowledge_search
    # branch so it gets the turn index — and so every form of tracing lives at this one
    # chokepoint, like the audit write.  The model's own account of what it read is not
    # evidence of what was retrieved.
    if canonical == "knowledge_search" and isinstance(result, dict):
        hits = result.get("results")
        if isinstance(hits, dict):
            hits = hits.get("results")
        if isinstance(hits, list):
            _debug.for_session(session.session_id).retrieval(
                turn_index=turn_index, query=str(args.get("query", "")), results=hits,
            )

    # The audit entry is redacted and capped at 2000 characters; this keeps the whole result,
    # carrying the audit entry_id so the two records line up.
    _debug.for_session(session.session_id).tool(
        turn_index=turn_index, tool_name=canonical, requested_name=name,
        arguments=args, result=result, audit_entry_id=audit_entry_id,
        latency_ms=latency_ms, injection_verdict=scan.verdict,
    )
    if broker_meta:
        _debug.for_session(session.session_id).record(
            "broker", turn_index=turn_index, tool_name=canonical, **broker_meta,
        )

    if scan.matched:
        session.push({"type": "injection_flagged", "tool": canonical,
                      "verdict": scan.verdict, "reasons": scan.reasons})
    return result, wrapped


# Tool schemas exposed to the model
TOOL_SCHEMAS = [
    _port_discovery.SCHEMA,
    {"type": "function", "function": {"name": "run_command", "description": "Run ONE local program inside the isolation sandbox. There is no shell: pipes, redirects, &&, ; and $( ) are refused — run one program per call and combine results yourself, or pass a pipeline to python3 -c. The sandbox has NO network, so use http_request and port_discovery for anything involving a target. Linux only.", "parameters": {"type": "object", "properties": {"argv": {"type": "array", "items": {"type": "string"}, "description": "Program and arguments, e.g. [\"grep\",\"-rn\",\"password\",\"notes.txt\"]. Preferred — no quoting to get wrong."}, "command": {"type": "string", "description": "Alternative to argv: a single command line, split on whitespace honouring quotes. Shell constructs are refused."}}}}},
    {"type": "function", "function": {"name": "http_request", "description": "Send an HTTP request to an in-scope target and get back status, headers, and body. PREFER this over run_command for any web testing — send SQLi/XSS/auth payloads as structured fields (no shell quote-escaping). For Juice Shop login use POST http://HOST:3000/rest/user/login with a JSON body {\"email\":\"...\",\"password\":\"...\"}.", "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "Full URL incl. path and query string"}, "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "HEAD", "PATCH", "OPTIONS"], "default": "GET"}, "headers": {"type": "object", "description": "Request headers as key/value pairs"}, "body": {"description": "Request body — a JSON object (sent as application/json) or a raw string"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a file from the workspace.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Relative path to read"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Write content to a file in the workspace.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Relative path to write"}, "content": {"type": "string", "description": "File content"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "knowledge_search", "description": "Search the security knowledge base (GTFOBins, HackTricks, exploit-db, etc.).", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Search query"}, "top_k": {"type": "integer", "description": "Number of results", "default": 5}}, "required": ["query"]}}},
]

SECURITY_TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": "record_hypothesis", "description": "Record a new hypothesis in the hypothesis graph.", "parameters": {"type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string"}, "phase": {"type": "string"}, "parent_ordinal": {"type": "integer"}}, "required": ["title", "description"]}}},
    {"type": "function", "function": {"name": "update_hypothesis_status", "description": "Update hypothesis status and verdict.", "parameters": {"type": "object", "properties": {"hypothesis_id": {"type": "string"}, "status": {"type": "string"}, "verdict": {"type": "string"}, "evidence": {"type": "string"}}, "required": ["hypothesis_id", "status"]}}},
    {"type": "function", "function": {"name": "record_finding", "description": "Record a security finding.", "parameters": {"type": "object", "properties": {"title": {"type": "string"}, "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", "info"]}, "target": {"type": "string"}, "description": {"type": "string"}, "remediation": {"type": "string"}}, "required": ["title", "severity", "description"]}}},
    {"type": "function", "function": {"name": "record_note", "description": "Jot a note in the engagement notebook: a technique that worked, a dead-end to avoid, a todo, or an observation. Keep a running lab notebook like a real red-teamer does.", "parameters": {"type": "object", "properties": {"text": {"type": "string", "description": "The note"}, "category": {"type": "string", "enum": ["technique", "dead-end", "todo", "observation"], "default": "observation"}}, "required": ["text"]}}},
]


# ═══════════════════════════════════════════════════════════════════════════════
# Agent loop (runs in a background thread per session)
# ═══════════════════════════════════════════════════════════════════════════════

# Keep the request well under llama-server's context (now 32768). With max_tokens≤3072 for
# generation, ~90k chars (~22k tokens) of input leaves a safe margin and avoids HTTP 400
# "exceeds context size" — the error that was killing long autonomous runs.
# Sized for llama-server -c 32768 (KV-quantized q8_0, fits 6GB: ~4.1GB VRAM). Input budget
# leaves ~3072 tok for generation: 90000 chars ≈ 22k tok conversation + ~1k system/tools.
MAX_INPUT_CHARS = 90000
PER_MSG_CAP = 8000
CTX_WINDOW = 32768


def _fit_context(messages: list[dict]) -> list[dict]:
    """Trim to fit the context window: hard-cap any single message, then drop the oldest
    non-system messages until the total is under budget. The system message and the most
    recent messages (including the current question/tool results) are always kept."""
    for m in messages:
        c = m.get("content")
        if isinstance(c, str) and len(c) > PER_MSG_CAP:
            m["content"] = c[:PER_MSG_CAP] + "\n…[truncated]"
    if not messages:
        return messages
    system, rest = messages[0], messages[1:]
    base = len(system.get("content", "") or "")
    total = base + sum(len(m.get("content", "") or "") for m in rest)
    while rest and total > MAX_INPUT_CHARS:
        dropped = rest.pop(0)
        total -= len(dropped.get("content", "") or "")
    return [system] + rest


COMPACT_WHEN = 24   # fold once this many messages sit beyond the last summary point
KEEP_RECENT = 10    # always keep this many most-recent messages verbatim


def _maybe_compact(session: Session):
    """Auto-compact: when a session grows long, summarize the older turns into one
    condensed note so the context window stays small. Only this session's own messages
    are ever folded — nothing from other chats is mixed in."""
    if _llm is None:
        return
    pending = len(session.messages) - session.summary_upto
    if pending <= COMPACT_WHEN:
        return
    fold_end = len(session.messages) - KEEP_RECENT
    to_fold = session.messages[session.summary_upto:fold_end]
    if not to_fold:
        return
    parts = []
    if session.summary:
        parts.append("Existing summary:\n" + session.summary)
    for m in to_fold:
        tn = (m.get("extra") or {}).get("tool_name") or m.get("tool_name")
        label = m.get("role", "") + (f"/{tn}" if tn else "")
        parts.append(f"[{label}] {(m.get('content') or '')[:800]}")
    convo = "\n".join(parts)[:12000]
    prompt = [
        {"role": "system", "content":
            "Compress this penetration-testing session transcript into a concise, FACTUAL summary. "
            "Keep: target(s), what was tried, concrete tool results/evidence, confirmed vs "
            "unconfirmed findings, and open next steps. Invent nothing. Bullet points, under 250 words."},
        {"role": "user", "content": convo},
    ]
    try:
        with _LLM_LOCK:
            resp = _llm.chat(prompt, stream=False, max_tokens=500, temperature=0.3)
        summary = resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    except Exception as e:
        log.warning("compact failed: %s", e)
        return
    if summary:
        # Record what is about to be removed BEFORE removing it. This logged only a count,
        # so "why did the agent forget what it found at step 5" could not be answered — the
        # folded messages are gone from context and the summary that replaced them was
        # overwritten by the next compaction.
        _debug.for_session(session.session_id).compaction(
            scope="session",
            folded_messages=[{**m, "_index": session.summary_upto + i}
                             for i, m in enumerate(to_fold)],
            summary=summary,
            summary_upto=fold_end,
            kept=KEEP_RECENT,
            prompt_tokens=session.last_prompt_tokens or None,
        )
        session.summary = summary
        session.summary_upto = fold_end
        session.push({"type": "compacted", "folded": len(to_fold)})
        log.info("compacted %d msgs for session %s", len(to_fold), session.session_id)


# Mid-turn compaction: a single turn is now uncapped, so a long tool chain can balloon the
# working message list beyond the context window. _fit_context would silently DROP the oldest
# messages (losing evidence); instead, once the real prompt-token count crosses this fraction
# of the window, summarize the middle of the in-flight turn and keep going — preserving facts.
INTURN_COMPACT_FRACTION = 0.65
INTURN_KEEP_TAIL = 6   # keep this many most-recent working messages verbatim


def _compact_working_messages(messages: list[dict], session: Session) -> list[dict]:
    """Fold the middle of an in-progress turn's working messages into a running summary.
    Keeps messages[0] (system) and the last INTURN_KEEP_TAIL messages verbatim."""
    if _llm is None or len(messages) <= INTURN_KEEP_TAIL + 2:
        return messages
    system = messages[0]
    head = messages[1:len(messages) - INTURN_KEEP_TAIL]
    tail = messages[len(messages) - INTURN_KEEP_TAIL:]
    if not head:
        return messages
    convo = "\n".join(f"[{m.get('role', '')}] {(m.get('content') or '')[:800]}" for m in head)[:12000]
    prompt = [
        {"role": "system", "content":
            "Compress this in-progress penetration-testing turn into a concise, FACTUAL summary. "
            "Keep: target(s), requests sent, concrete responses/evidence, confirmed vs unconfirmed, "
            "and the open next step. Invent nothing. Bullet points, under 180 words."},
        {"role": "user", "content": convo},
    ]
    try:
        with _LLM_LOCK:
            resp = _llm.chat(prompt, stream=False, max_tokens=360, temperature=0.3)
        summ = resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    except Exception as e:
        log.warning("mid-turn compact failed: %s", e)
        return messages
    if not summ:
        return messages
    _debug.for_session(session.session_id).compaction(
        scope="in_turn",
        folded_messages=[{**m, "_index": i + 1} for i, m in enumerate(head)],
        summary=summ,
        kept=INTURN_KEEP_TAIL,
        prompt_tokens=session.last_prompt_tokens or None,
    )
    session.push({"type": "compacted", "folded": len(head), "scope": "in-turn"})
    log.info("mid-turn compacted %d working msgs for %s", len(head), session.session_id)
    return [system, {"role": "user",
                     "content": "Progress so far this turn (condensed to save context):\n" + summ}] + tail


# ── Guided stage protocol ─────────────────────────────────────────────────────
# Stages are operator-driven progress labels, NOT an auto-advancing pipeline. The agent
# works within the current stage, then stops and reports; the OPERATOR decides what's next
# (advance, redo, go back to an earlier stage, or request the full report).
STAGES = ["RECON", "ANALYSIS", "VALIDATION", "REPORT"]
_STAGE_OBJECTIVE = {
    "RECON": "Enumerate the target: reachable endpoints, HTTP responses, headers, tech stack, "
             "interesting paths/APIs. Map the attack surface — don't exploit yet.",
    "ANALYSIS": "From what RECON actually observed, list candidate vulnerabilities, each tied to a "
                "specific endpoint you saw. Use knowledge_search for the tech/endpoints found.",
    "VALIDATION": "Prove or disprove the top candidates with real http_request calls. A candidate is "
                  "CONFIRMED only if the response proves it. Call record_finding for each confirmed issue.",
    "REPORT": "Write the full report from ONLY findings confirmed by tool evidence this engagement: "
              "per finding — endpoint, request+response evidence, severity, remediation.",
}


def _stage_protocol(session: Session) -> str:
    stage = session.stage if session.stage in STAGES else "RECON"
    obj = _STAGE_OBJECTIVE[stage]
    base = (
        f"\n\nGUIDED PENTEST PROTOCOL — current stage: {stage}\n"
        f"Objective of this stage: {obj}\n"
        "Rules:\n"
        "- Work toward THIS stage's objective using tools. Go as deep as the stage needs.\n"
        "- When you have enough for this stage, STOP: give a concise summary of what you verified "
        "(with evidence) and suggest concrete next steps, then WAIT for the operator.\n"
        "- Do NOT advance to another stage on your own — the operator drives stage changes by chatting.\n"
        "- The operator may send you back to an earlier stage (e.g. more recon, research a vuln). Follow that.\n"
    )
    if stage != "REPORT":
        base += ("- Do NOT write a full report now. The full REPORT stage happens ONLY when the operator "
                 "explicitly asks for it.\n")
    return base


# Operator phrases that move the stage (English + Thai), checked against a chat message.
_STAGE_INTENT = [
    (re.compile(r"\b(recon|reconnaiss\w*|enumerat\w*)\b|รีคอน|สำรวจ", re.I), "RECON"),
    (re.compile(r"\banalys\w*\b|วิเคราะห์", re.I), "ANALYSIS"),
    (re.compile(r"\b(validat\w*|exploit\w*|verif\w*|confirm\w*)\b|ยืนยัน|เจาะ", re.I), "VALIDATION"),
    (re.compile(r"\b(report|write.?up|รายงาน)\b|เขียนรายงาน", re.I), "REPORT"),
]


def _detect_stage_intent(message: str) -> str | None:
    """If the operator's message clearly names a stage to move to, return it; else None.
    Only triggers on an explicit move cue so a passing mention doesn't hijack the stage."""
    if not re.search(r"\b(go|move|back|next|start|now|do|let'?s|switch)\b|ไป|กลับ|ต่อ|เริ่ม|ขอ", message, re.I):
        return None
    for rx, stage in _STAGE_INTENT:
        if rx.search(message):
            return stage
    return None


def _run_agent_turn(session: Session, user_content: str, *, emit_done: bool = True):
    """Single agent turn: build messages, call LLM (streaming), handle tool calls, loop.

    emit_done=False lets the autonomous driver run many turns without flipping the
    UI to "not running" between phases (it emits its own autonomous_* events instead).
    """
    if _llm is None:
        session.push({"type": "task_error", "error": "No model loaded"})
        return

    # System message is kept byte-stable (prompt-cache friendly): engagement scope rarely
    # changes within a session, and volatile RAG context is appended as the LAST message.
    eng = _engagements.get(session.engagement_id)
    # Tool output is wrapped in quarantine markers before it enters context (see
    # _audited_run_tool). The addendum is what makes those markers mean something to the model
    # instead of being unexplained noise in the middle of a result.
    system_content = DEFAULT_SYSTEM_PROMPT + "\n\n" + _injection_guard.SYSTEM_INSTRUCTION_ADDENDUM
    if eng:
        system_content += f"\n\nEngagement: {eng.engagement_id}\nIn-scope targets: {', '.join(eng.allow_targets)}"
    if session.guided:
        system_content += _stage_protocol(session)

    messages = [{"role": "system", "content": system_content}]

    # Auto-compact long sessions, then send the summary + only the recent turns.
    _maybe_compact(session)
    if session.summary:
        messages.append({"role": "user",
                         "content": "Session summary so far (condensed prior context):\n" + session.summary})
    history = session.messages[session.summary_upto:]
    if len(history) > 20:
        history = history[-20:]
    for m in history:
        role = m["role"] if m["role"] in ("user", "assistant", "system") else "user"
        messages.append({"role": role, "content": m["content"]})

    # RAG augmentation goes LAST so the stable prefix above stays cacheable.
    rag_results = _rag.search(user_content, top_k=2)
    if rag_results:
        rag_context = "Relevant knowledge-base excerpts (cite only if actually relevant):\n"
        for r in rag_results:
            rag_context += f"\n[{r['source']}] {r['title']} (relevance {r['score']})\n{r['text'][:400]}\n"
        messages.append({"role": "user", "content": rag_context})

    tools = TOOL_SCHEMAS[:]
    if session.use_security_tools:
        tools.extend(SECURITY_TOOL_SCHEMAS)

    # No hard tool-round cap — the turn runs until the model stops calling tools (its natural
    # finish) or hits a safety guard. Guards (not a task limit): an absolute runaway ceiling,
    # and loop-detection that stops if the model repeats the exact same call 3× in a row.
    RUNAWAY_CEILING = 60
    tool_round = 0
    nudges_used = 0
    recent_sigs: list[str] = []
    while True:
        if session.stop_requested:
            if emit_done:
                session.push({"type": "task_done", "status": "stopped", "message": "Stopped by user"})
            return
        if tool_round >= RUNAWAY_CEILING:
            session.append("system", f"[stopped: hit the {RUNAWAY_CEILING}-tool-call safety ceiling for "
                                     f"one turn — send another message to continue]")
            break

        # Live steer: inject any operator notes queued since the last round so a running
        # turn can be redirected without being stopped (non-blocking).
        if session.pending_steers:
            with session.lock:
                steers, session.pending_steers = session.pending_steers, []
            for s in steers:
                messages.append({"role": "user", "content": f"[OPERATOR STEER] {s}"})
                session.push({"type": "steer_applied", "text": s})

        _LLM_LOCK.acquire()  # one generation at a time on the single-slot llama-server
        stream = None
        try:
            stream = _llm.chat(_fit_context(messages), stream=True, max_tokens=3072,
                               tools=tools, enable_thinking=session.thinking)
        except Exception as e:
            _LLM_LOCK.release()
            session.push({"type": "task_error", "error": f"LLM error: {e}"})
            return

        collected_text = ""
        collected_reasoning = ""
        tool_call_buffer = ""
        in_tool_call = False
        in_think = False
        stopped_mid_stream = False
        stream_error = None
        oai_tool_calls = {}  # index -> {name, arguments_str}

        def _safe_iter(gen):
            # The HTTP call runs while iterating (not at creation), so a 400/connection
            # error surfaces HERE. Catch it so one bad turn can't crash the whole run.
            nonlocal stream_error
            try:
                for c in gen:
                    yield c
            except Exception as e:
                stream_error = e

        for chunk in _safe_iter(stream):
            if session.stop_requested:
                stopped_mid_stream = True
                break  # closing the generator closes the upstream HTTP connection

            # Trailing usage chunk (stream_options.include_usage): choices is empty, but
            # usage.prompt_tokens is the REAL context size llama-server saw this round.
            usage = chunk.get("usage")
            if usage and usage.get("prompt_tokens"):
                session.last_prompt_tokens = int(usage["prompt_tokens"])
                session.push({"type": "context", "prompt_tokens": session.last_prompt_tokens,
                              "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                              "max": CTX_WINDOW})

            choices = chunk.get("choices") or [{}]
            delta = choices[0].get("delta", {})

            # Thinking: llama.cpp surfaces <think> content as a separate reasoning_content
            # field (reasoning-format deepseek) rather than inline in content.
            rc = delta.get("reasoning_content")
            if rc:
                collected_reasoning += rc
                session.push({"type": "reasoning_delta", "text": rc})

            # OpenAI-format tool calls (delta.tool_calls)
            for tc in delta.get("tool_calls", []):
                idx = tc.get("index", 0)
                if idx not in oai_tool_calls:
                    oai_tool_calls[idx] = {"name": "", "arguments": ""}
                fn = tc.get("function", {})
                if fn.get("name"):
                    oai_tool_calls[idx]["name"] = fn["name"]
                if fn.get("arguments"):
                    oai_tool_calls[idx]["arguments"] += fn["arguments"]

            content = delta.get("content", "")
            if content:
                if content.strip().startswith("<tool_call>") or in_tool_call:
                    in_tool_call = True
                    tool_call_buffer += content
                    if "</tool_call>" in tool_call_buffer:
                        in_tool_call = False
                else:
                    # Handle <think>...</think> blocks as reasoning
                    buf = content
                    while buf:
                        if in_think:
                            end_idx = buf.find("</think>")
                            if end_idx != -1:
                                collected_reasoning += buf[:end_idx]
                                session.push({"type": "reasoning_delta", "text": buf[:end_idx]})
                                buf = buf[end_idx + 8:]
                                in_think = False
                            else:
                                collected_reasoning += buf
                                session.push({"type": "reasoning_delta", "text": buf})
                                buf = ""
                        else:
                            start_idx = buf.find("<think>")
                            if start_idx != -1:
                                before = buf[:start_idx]
                                if before:
                                    collected_text += before
                                    session.push({"type": "assistant_delta", "text": before})
                                buf = buf[start_idx + 7:]
                                in_think = True
                            else:
                                collected_text += buf
                                session.push({"type": "assistant_delta", "text": buf})
                                buf = ""

            # Don't break on finish_reason: the trailing usage chunk (include_usage) arrives
            # AFTER it, so keep draining until the generator ends on [DONE].

        # Generation done (or stopped) — free the llama slot before running tools / next round.
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass
        _LLM_LOCK.release()

        if stream_error is not None:
            # Surface what we have, then end this turn cleanly (autonomous moves to next phase).
            log.warning("LLM stream error: %s", stream_error)
            if collected_text.strip():
                session.append("assistant", collected_text.strip())
            if emit_done:
                session.push({"type": "task_error", "error": f"LLM stream error: {stream_error}"})
            else:
                session.append("system", f"[phase turn ended: {stream_error}]")
            return

        if stopped_mid_stream or session.stop_requested:
            if collected_text.strip():
                session.append("assistant", collected_text.strip())
            if emit_done:
                session.push({"type": "task_done", "status": "stopped", "message": "Stopped by user"})
            return

        # Parse tool calls from collected text (some models use JSON blocks)
        tool_calls = _extract_tool_calls(collected_text + tool_call_buffer)

        # Also include OpenAI-format tool calls from delta.tool_calls
        for idx in sorted(oai_tool_calls.keys()):
            tc = oai_tool_calls[idx]
            if tc["name"]:
                try:
                    args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except json.JSONDecodeError:
                    args = {"raw": tc["arguments"]}
                tool_calls.append((tc["name"], args))
        oai_tool_calls.clear()

        if tool_calls:
            # One assistant message carrying the tool calls, then ONE user message with all
            # results — never an assistant/user pair per tool (that corrupts the chat template).
            extra = {"tool_calls": [{"name": tc[0], "arguments": tc[1]} for tc in tool_calls]}
            if collected_reasoning.strip():
                extra["reasoning_content"] = collected_reasoning.strip()
            session.append("assistant", collected_text.strip() or "(calling tools…)", extra=extra)
            messages.append({"role": "assistant",
                             "content": collected_text.strip() or "(calling tools…)"})

            result_blocks = []
            for tool_name, tool_args in tool_calls:
                tool_args = _normalize_tool_args(tool_name, tool_args)
                result, result_text = _audited_run_tool(
                    tool_name, tool_args, session,
                    turn_index=tool_round,
                    action_rationale=collected_text.strip(),
                )
                session.append("tool", result_text, extra={"tool_name": tool_name})
                result_blocks.append(f"[{tool_name}] →\n{result_text}")
            messages.append({"role": "user",
                             "content": "Tool results:\n\n" + "\n\n".join(result_blocks)})
            tool_round += 1
            # Loop-detection: if the model fires the identical call 3× running, it's stuck
            # (a 4B sometimes re-sends the same request forever) — stop and let it summarize.
            sig = json.dumps(extra["tool_calls"], sort_keys=True, ensure_ascii=False)
            recent_sigs.append(sig)
            if len(recent_sigs) >= 3 and recent_sigs[-1] == recent_sigs[-2] == recent_sigs[-3]:
                messages.append({"role": "user",
                                 "content": "You have repeated the same tool call three times. "
                                            "Stop repeating — summarize what you found and either try a "
                                            "DIFFERENT request or give your final answer."})
                recent_sigs.clear()
            # Mid-turn compaction: fold the middle of this turn once the real prompt size
            # crosses the threshold, so a long tool chain preserves evidence instead of
            # having _fit_context silently drop the oldest messages.
            if session.last_prompt_tokens > INTURN_COMPACT_FRACTION * CTX_WINDOW:
                messages = _compact_working_messages(messages, session)
            continue

        # No tool calls — final response (or an empty answer to recover from).
        if collected_text.strip():
            extra = {}
            if collected_reasoning.strip():
                extra["reasoning_content"] = collected_reasoning.strip()
            session.append("assistant", collected_text.strip(), extra=extra if extra else None)
            break

        # Model produced only <think> (common failure on small models): nudge for the answer.
        # Nudges don't consume the round budget — an empty thinking round shouldn't end the turn.
        if nudges_used < 3:
            nudges_used += 1
            messages.append({"role": "user",
                             "content": "Now give your actual answer or call a tool. "
                                        "Do not output only reasoning."})
            continue
        # Still no text after nudges. If the model actually did work this turn, force ONE
        # non-streaming summary (no tools) so the turn is never a silent wall of tool calls
        # with no message — the #1 "did a lot but said nothing" complaint.
        if tool_round > 0:
            try:
                with _LLM_LOCK:
                    resp = _llm.chat(
                        _fit_context(messages + [{"role": "user",
                            "content": "Summarize for the operator, in plain text, what you did and "
                                       "found this turn — targets, payloads, status codes, tokens, and "
                                       "which hypotheses/findings you confirmed. Do NOT call any tool."}]),
                        stream=False, max_tokens=700, temperature=0.3)
                txt = (resp.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
            except Exception as e:
                log.warning("final-summary generation failed: %s", e)
                txt = ""
            if txt:
                session.append("assistant", txt)
                break
        # Give up gracefully: surface the reasoning so the turn is never silent.
        if collected_reasoning.strip():
            session.append("assistant", collected_reasoning.strip())
        break

    if emit_done:
        session.push({"type": "task_done", "status": "ok", "message": "Turn complete"})


# Canonical argument names, so a near-miss key still lands on the right field.
_TOOL_ARG_ALIASES = {
    "cmd": "command", "cmdline": "command", "shell": "command", "script": "command",
    "file": "path", "filename": "path", "filepath": "path", "file_path": "path", "name": "path",
    "text": "content", "data": "content", "body": "content", "file_content": "content",
    "q": "query", "search": "query", "search_query": "query", "question": "query",
}


def _jsonify_result(result: dict, field_limit: int = 1200) -> str:
    """Serialize a tool result to JSON that is ALWAYS valid.

    Large output is capped at the FIELD level before dumping — never by slicing the
    serialized string (that cut JSON mid-token and crashed the UI's JSON.parse)."""
    capped = dict(result)
    for k in ("output", "content", "error"):
        v = capped.get(k)
        if isinstance(v, str) and len(v) > field_limit:
            capped[k] = v[:field_limit] + f"\n…[truncated {len(v) - field_limit} chars]"
    return json.dumps(capped, indent=2, ensure_ascii=False)


def _normalize_tool_args(name: str, args: Any) -> dict:
    """Make a small model's nearly-right arguments usable (lesson: normalize, never guess).

    Handles: stringified-JSON args, a bare scalar where a dict was expected, aliased keys,
    stringified arrays/objects as values, top_k as a string, and list-valued string fields.
    Only single-meaning fixes — ambiguous input is left for the tool to reject clearly.
    """
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
            args = parsed if isinstance(parsed, dict) else {"_value": parsed}
        except (json.JSONDecodeError, TypeError):
            # A lone string is almost always the command (run_command) or the query.
            canonical = _TOOL_NAME_ALIASES.get(name, name)
            key = "query" if canonical == "knowledge_search" else "command"
            args = {key: args}
    if not isinstance(args, dict):
        return {}
    # Recover from an upstream JSON parse that failed and stored the raw string.
    if set(args.keys()) == {"raw"} and isinstance(args["raw"], str):
        try:
            recovered = json.loads(args["raw"])
            if isinstance(recovered, dict):
                args = recovered
        except json.JSONDecodeError:
            pass

    out: dict = {}
    for k, v in args.items():
        key = _TOOL_ARG_ALIASES.get(str(k).lower(), k)
        if isinstance(v, str) and v[:1] in ("[", "{"):  # stringified array/object → real one
            try:
                v = json.loads(v)
            except json.JSONDecodeError:
                pass
        out[key] = v

    if "top_k" in out:
        try:
            out["top_k"] = int(out["top_k"])
        except (ValueError, TypeError):
            out.pop("top_k", None)
    # A string field that arrived as a list → join it back together.
    for sk in ("command", "content"):
        if isinstance(out.get(sk), list):
            out[sk] = "\n".join(str(x) for x in out[sk])
    for sk in ("path", "query", "title", "description"):
        if isinstance(out.get(sk), list):
            out[sk] = " ".join(str(x) for x in out[sk])
    return out


def _extract_tool_calls(text: str) -> list[tuple[str, dict]]:
    """Extract tool calls from model output. Supports multiple formats:
    - <tool_call>{"name": ..., "arguments": ...}</tool_call>
    - ```json\n{"tool": ..., "args": ...}\n```
    - Direct JSON blocks with function-like signatures
    """
    calls = []

    # Format 1: <tool_call> tags
    for m in re.finditer(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", text, re.DOTALL):
        try:
            obj = json.loads(m.group(1))
            name = obj.get("name", obj.get("function", ""))
            args = obj.get("arguments", obj.get("args", obj.get("parameters", {})))
            if isinstance(args, str):
                args = json.loads(args)
            if name:
                calls.append((name, args))
        except (json.JSONDecodeError, TypeError):
            continue

    if calls:
        return calls

    # Format 2: Qwen/Llama tool-use format
    for m in re.finditer(r'✿FUNCTION✿:\s*(\w+)\n✿ARGS✿:\s*(\{.*?\})', text, re.DOTALL):
        try:
            calls.append((m.group(1), json.loads(m.group(2))))
        except json.JSONDecodeError:
            continue

    if calls:
        return calls

    # Format 3: JSON code blocks
    for m in re.finditer(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL):
        try:
            obj = json.loads(m.group(1))
            name = obj.get("tool", obj.get("name", obj.get("function", "")))
            args = obj.get("args", obj.get("arguments", obj.get("parameters", {})))
            if isinstance(args, str):
                args = json.loads(args)
            if name and isinstance(args, dict):
                calls.append((name, args))
        except (json.JSONDecodeError, TypeError):
            continue

    return calls


# ═══════════════════════════════════════════════════════════════════════════════
# Guided mode — a single operator-gated turn (replaces the old 6-phase auto-pipeline).
# The agent works within the current stage, reports, and stops; the operator drives what's
# next by chatting. No auto-advance, no auto full-report. See _stage_protocol().
# ═══════════════════════════════════════════════════════════════════════════════

def _scope_urls_into_engagement(engagement_id: str, message: str):
    """Record hosts named in an operator message as *proposed*, never as authorised.

    This used to append them straight into `allow_targets`, which made mentioning a URL in
    conversation equivalent to authorising it — and an operator pastes URLs as context all the
    time, out of a report, a ticket or a log excerpt. There is no way to tell those apart from
    "please test this", so the grant has to be a deliberate act in the engagement, not a
    side-effect of text.

    The names are still worth keeping: the UI can offer them as one-click additions, and a scope
    refusal can point at the host the operator just mentioned. They grant nothing on their own.
    """
    eng = _engagements.get(engagement_id)
    if not eng:
        return
    from urllib.parse import urlparse
    for u in re.findall(r'https?://[^\s<>"\']+', message or ""):
        host = (urlparse(u).hostname or "").lower()
        if host and host not in eng.allow_targets and host not in eng.proposed_targets:
            eng.proposed_targets.append(host)


def _run_guided(session: Session, engagement_id: str, user_message: str = ""):
    """Run ONE guided turn: the agent works the current stage, then stops and waits for the
    operator. Stage is operator-driven — this never auto-advances through stages."""
    session.engagement_id = engagement_id or session.engagement_id
    session.guided = True
    _scope_urls_into_engagement(session.engagement_id, user_message)
    moved = _detect_stage_intent(user_message)
    if moved:
        session.stage = moved
    session.push({"type": "stage", "stage": session.stage, "guided": True})
    _run_agent_turn(session, user_message)


# ═══════════════════════════════════════════════════════════════════════════════
# Pydantic request models
# ═══════════════════════════════════════════════════════════════════════════════

class CreateSessionRequest(BaseModel):
    profile: str = "safe"
    use_security_tools: bool = False
    engagement_id: str = "lab-default"
    dangerous_local: bool = False
    isolation_tier: str = "bubblewrap"
    thinking: bool = False

class MessageRequest(BaseModel):
    content: str
    guided: bool | None = None   # follow the staged pentest protocol for this turn
    stage: str | None = None     # operator-set stage for this turn (overrides detection)

class StageRequest(BaseModel):
    stage: str

class SteerRequest(BaseModel):
    message: str

class ApprovalResolveRequest(BaseModel):
    approved: bool
    resolved_by: str = "web-ui"

class ConsultResolveRequest(BaseModel):
    answer: str
    resolved_by: str = "web-ui"

class CreateEngagementRequest(BaseModel):
    engagement_id: str
    description: str = ""
    allow_targets: list[str] = []
    allowed_action_classes: list[str] = []
    authorized_by: str = ""
    valid_hours: float = 24.0

class OperatorGraphActionRequest(BaseModel):
    action: str
    reason: str = ""
    text: str = ""

class AutonomousStartRequest(BaseModel):
    engagement_id: str
    profile: str = "web_api"
    mode: str
    user_message: str = ""

class ReviewFindingRequest(BaseModel):
    reviewed_by: str

class NotebookResolveRequest(BaseModel):
    action: str = "resolve"
    reason: str = ""


# ═══════════════════════════════════════════════════════════════════════════════
# FastAPI app + all routes
# ═══════════════════════════════════════════════════════════════════════════════

app = FastAPI(title="Oxpecker Dev Server", version=API_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Health ──
@app.get("/api/health")
@app.get("/health")  # /health alias for the Electron shell's startup probe
def health_check():
    return {
        "status": "ok",
        "version": API_VERSION,
        "sessions_active": len(_sessions),
        "model_loaded": _llm is not None,
        "rag_documents": _rag.count(),
        "timestamp": time.time(),
    }

# ── Static / index ──
@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

# ── Sessions ──
@app.post("/api/sessions")
def create_session(req: CreateSessionRequest):
    sid = str(uuid.uuid4())[:12]
    session = Session(
        session_id=sid,
        use_security_tools=req.use_security_tools,
        engagement_id=req.engagement_id,
        isolation_tier=req.isolation_tier,
        profile=req.profile,
        thinking=req.thinking,
    )
    _sessions[sid] = session
    _persist()
    return {
        "session_id": sid, "workspace_root": str(DATA_DIR),
        "profile": req.profile, "use_security_tools": req.use_security_tools,
        "engagement_id": req.engagement_id,
    }

@app.get("/api/sessions")
def list_sessions():
    out = []
    for sid, s in sorted(_sessions.items(), key=lambda x: x[1].created_at, reverse=True):
        title = ""
        for m in s.messages:
            if m["role"] == "user":
                title = m["content"][:80]
                break
        out.append({
            "session_id": sid, "known_live": True,
            "last_modified": s.messages[-1]["timestamp"] if s.messages else s.created_at,
            "title": title, "message_count": len(s.messages),
        })
    return out

@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str):
    s = _sessions.pop(session_id, None)
    _persist()
    return {"deleted": s is not None}

@app.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    s = _sessions.get(session_id)
    if not s:
        raise HTTPException(404, f"session {session_id!r} not found")
    return {
        "session_id": session_id,
        "thinking": s.thinking,
        "stage": s.stage,
        "guided": s.guided,
        "last_prompt_tokens": s.last_prompt_tokens,
        # `isolation_tier` is what the session ASKED for; `effective_isolation_tier` is what the
        # last run_command actually executed under (None if none has run, or if one was refused
        # because the requested tier was unavailable). `isolation` describes what this host can
        # really do. Reported separately and from the live probe so the UI cannot show a tier
        # nothing enforced — which is exactly what `isolation_tier` alone did while no code read
        # it at execution time.
        "isolation_tier": s.isolation_tier,
        "effective_isolation_tier": s.effective_isolation_tier,
        "isolation": _isolation.describe_host(),
        "messages": [{"role": m["role"], "content": m["content"], "message_id": m.get("message_id"),
                      "reasoning_content": m.get("reasoning_content"),
                      "tool_calls": m.get("tool_calls"), "tool_name": m.get("tool_name")}
                     for m in s.messages],
    }

class ThinkingRequest(BaseModel):
    thinking: bool

@app.post("/api/sessions/{session_id}/thinking")
def set_thinking(session_id: str, req: ThinkingRequest):
    s = _sessions.get(session_id)
    if not s:
        raise HTTPException(404, f"session {session_id!r} not found")
    s.thinking = req.thinking
    _persist()
    return {"thinking": s.thinking}

# ── Messages ──
@app.post("/api/sessions/{session_id}/messages")
async def send_message(session_id: str, req: MessageRequest):
    s = _sessions.get(session_id)
    if not s:
        raise HTTPException(404, f"session {session_id!r} not found")
    with s.lock:
        if s.running:
            raise HTTPException(409, "a task is already running")
        s.running = True
        s.stop_requested = False

    # Guided mode: follow the staged protocol. The operator drives stages — an explicit
    # stage wins, else detect a move cue in the message (e.g. "go to validation" / "เจาะ").
    if req.guided is not None:
        s.guided = req.guided
    if s.guided:
        _scope_urls_into_engagement(s.engagement_id, req.content)
        if req.stage and req.stage.upper() in STAGES:
            s.stage = req.stage.upper()
        else:
            moved = _detect_stage_intent(req.content)
            if moved:
                s.stage = moved
        s.push({"type": "stage", "stage": s.stage, "guided": True})

    s.append("user", req.content)

    def run():
        try:
            _run_agent_turn(s, req.content)
        except Exception as e:
            s.push({"type": "task_error", "error": f"{type(e).__name__}: {e}"})
        finally:
            with s.lock:
                s.running = False
            _persist()

    threading.Thread(target=run, daemon=True).start()
    return {"status": "started"}

# ── Stage (operator-driven) ──
@app.post("/api/sessions/{session_id}/stage")
def set_stage(session_id: str, req: StageRequest):
    s = _sessions.get(session_id)
    if not s:
        raise HTTPException(404, f"session {session_id!r} not found")
    st = (req.stage or "").upper()
    if st not in STAGES:
        raise HTTPException(400, f"stage must be one of {STAGES}")
    s.stage = st
    s.guided = True
    s.push({"type": "stage", "stage": s.stage, "guided": True})
    _persist()
    return {"stage": s.stage}

# ── Steer ──
@app.post("/api/sessions/{session_id}/steer")
def steer_session(session_id: str, req: SteerRequest):
    s = _sessions.get(session_id)
    if not s:
        raise HTTPException(404, f"session {session_id!r} not found")
    s.append("system", f"[OPERATOR STEER] {req.message}")
    # Queue for mid-run injection if a turn is in flight; if idle, start a turn so the
    # steer is acted on immediately rather than sitting until the next message.
    if s.running:
        with s.lock:
            s.pending_steers.append(req.message)
    else:
        with s.lock:
            if not s.running:
                s.running = True
                s.stop_requested = False
                started = True
            else:
                s.pending_steers.append(req.message)
                started = False
        if started:
            def run(msg=req.message):
                try:
                    _run_agent_turn(s, msg)
                except Exception as e:
                    s.push({"type": "task_error", "error": f"{type(e).__name__}: {e}"})
                finally:
                    with s.lock:
                        s.running = False
                    _persist()
            threading.Thread(target=run, daemon=True).start()
    return {"status": "sent"}

# ── SSE Events ──
@app.get("/api/sessions/{session_id}/events")
async def stream_events(session_id: str):
    s = _sessions.get(session_id)
    if not s:
        raise HTTPException(404, f"session {session_id!r} not found")

    async def event_stream():
        while True:
            try:
                event = s.events.get_nowait()
                yield f"data: {json.dumps(event)}\n\n"
            except queue.Empty:
                await asyncio.sleep(0.06)

    return StreamingResponse(event_stream(), media_type="text/event-stream")

# ── WebSocket ──
@app.websocket("/api/sessions/{session_id}/ws")
async def websocket_events(websocket: WebSocket, session_id: str):
    s = _sessions.get(session_id)
    if not s:
        await websocket.close(code=4004, reason="session not found")
        return
    await websocket.accept()

    async def send_events():
        try:
            while True:
                try:
                    event = s.events.get_nowait()
                    await websocket.send_json(event)
                except queue.Empty:
                    await asyncio.sleep(0.06)
        except Exception:
            pass

    task = asyncio.create_task(send_events())
    try:
        while True:
            data = await websocket.receive_json()
            t = data.get("type")
            if t == "message":
                content = data.get("content", "").strip()
                if content:
                    with s.lock:
                        if s.running:
                            await websocket.send_json({"type": "error", "error": "task already running"})
                            continue
                        s.running = True
                    s.append("user", content)
                    def run(c=content):
                        try:
                            _run_agent_turn(s, c)
                        except Exception as e:
                            s.push({"type": "task_error", "error": str(e)})
                        finally:
                            with s.lock:
                                s.running = False
                    threading.Thread(target=run, daemon=True).start()
                    await websocket.send_json({"type": "ack", "action": "message"})
            elif t == "steer":
                msg = data.get("message", "").strip()
                if msg:
                    s.append("system", f"[OPERATOR STEER] {msg}")
                    if s.running:
                        with s.lock:
                            s.pending_steers.append(msg)
                    await websocket.send_json({"type": "ack", "action": "steer"})
            elif t == "ping":
                await websocket.send_json({"type": "pong", "timestamp": time.time()})
    except WebSocketDisconnect:
        pass
    finally:
        task.cancel()

# ── Approvals ──
@app.get("/api/approvals")
def list_approvals(session_id: str | None = None):
    return [
        {"request_id": a.request_id, "session_id": a.session_id, "tool": a.tool,
         "description": a.description, "command": a.command, "detail": a.detail, "prompt": a.prompt}
        for a in _approvals.values()
        if a.status == "pending" and (session_id is None or a.session_id == session_id)
    ]

@app.post("/api/approvals/{request_id}/resolve")
def resolve_approval(request_id: str, req: ApprovalResolveRequest):
    ar = _approvals.get(request_id)
    if not ar:
        raise HTTPException(404, "approval not found")
    ar.approved = req.approved
    ar.status = "approved" if req.approved else "declined"
    ar.resolved_by = req.resolved_by
    ar.event.set()
    return {"status": ar.status, "request_id": request_id}

# ── Consults ──
@app.get("/api/consults")
def list_consults(session_id: str | None = None):
    return [
        {"request_id": c.request_id, "session_id": c.session_id,
         "question": c.question, "prompt": c.prompt}
        for c in _consults.values()
        if c.status == "pending" and (session_id is None or c.session_id == session_id)
    ]

@app.post("/api/consults/{request_id}/resolve")
def resolve_consult(request_id: str, req: ConsultResolveRequest):
    cr = _consults.get(request_id)
    if not cr:
        raise HTTPException(404, "consult not found")
    cr.answer = req.answer
    cr.status = "resolved"
    cr.resolved_by = req.resolved_by
    cr.event.set()
    return {"status": "resolved", "request_id": request_id}

# ── Engagements ──
@app.post("/api/engagements")
def create_engagement(req: CreateEngagementRequest):
    # Validated server-side against the same rules agent/engagement/intake.py applies on the CLI
    # side. This endpoint previously accepted anything, and `allow_targets` defaults to `[]`, so
    # an engagement with no targets at all could be created through the API — which, before the
    # scope fix, meant an engagement that permitted every host. The UI already assumed this was
    # rejected (see web/test_frontend.py's validation test, "intake.py rejects an engagement with
    # none"); only the server did not.
    errors: list[str] = []
    if not req.allow_targets:
        errors.append(
            "allow_targets must have at least one entry — an engagement with no targets "
            "authorises nothing and cannot be used"
        )
    for target in req.allow_targets:
        _intake._validate_scope_line(target, errors, "allow_targets")
    if not req.authorized_by.strip():
        errors.append("authorized_by is required — record who authorised this engagement")
    if req.valid_hours <= 0:
        errors.append("valid_hours must be greater than zero")
    known = set(_broker_mod.TOOL_ACTION_CLASS.values()) | {"http_recon_insecure"}
    for cls in req.allowed_action_classes:
        if cls not in known:
            errors.append(
                f"allowed_action_classes entry {cls!r} is not a known action class "
                f"(choose from {sorted(known)}) — an unknown name permits nothing, and before "
                "the RoE check was enforced it silently did nothing at all"
            )
    if errors:
        raise HTTPException(400, "; ".join(errors))

    now = time.time()
    eng = Engagement(
        engagement_id=req.engagement_id,
        description=req.description,
        allow_targets=req.allow_targets,
        allowed_action_classes=req.allowed_action_classes,
        authorized_by=req.authorized_by,
        valid_until=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + req.valid_hours * 3600)),
    )
    _engagements[req.engagement_id] = eng
    _persist()
    return {"engagement_id": req.engagement_id, "engagement_dir": str(DATA_DIR / req.engagement_id)}

@app.get("/api/engagements")
def list_engagements():
    return [e.to_dict() for e in _engagements.values()]

# ── Hypothesis Graph ──
def _scope_keys(path_id: str) -> list[str]:
    """Graph/notebook/findings are stored per-SESSION. A path id is either a session_id
    (that one session's view) or an engagement_id (aggregate across its sessions, plus any
    legacy data that was stored under the engagement id before the per-session split)."""
    if path_id in _sessions:
        return [path_id]
    keys = [sid for sid, s in _sessions.items() if s.engagement_id == path_id]
    if path_id in _graphs or path_id in _notebooks or path_id in _findings:
        keys.append(path_id)
    return keys

@app.get("/api/engagements/{engagement_id}/hypothesis-graph")
def hypothesis_graph_overview(engagement_id: str, phase: str | None = None):
    nodes, edges = [], []
    for k in _scope_keys(engagement_id):
        nodes += _graphs.get(k, [])
        edges += _graph_edges.get(k, [])
    if not nodes:
        return {"exists": False, "nodes": [], "edges": [], "graph_state": None}
    filtered = nodes if not phase else [n for n in nodes if n.phase == phase]
    return {
        "exists": True,
        "nodes": [n.to_summary() for n in filtered],
        "edges": edges,
        "graph_state": {"current_phase": phase or ""},
    }

@app.get("/api/engagements/{engagement_id}/hypothesis-graph/nodes/{ordinal}")
def hypothesis_graph_node(engagement_id: str, ordinal: int):
    for k in _scope_keys(engagement_id):
        for n in _graphs.get(k, []):
            if n.ordinal == ordinal:
                return n.to_detail()
    raise HTTPException(404, f"node {ordinal} not found")

@app.post("/api/engagements/{engagement_id}/hypothesis-graph/nodes/{ordinal}/operator-action")
def hypothesis_graph_action(engagement_id: str, ordinal: int, req: OperatorGraphActionRequest):
    node = None
    for k in _scope_keys(engagement_id):
        for n in _graphs.get(k, []):
            if n.ordinal == ordinal:
                node = n
                break
        if node:
            break
    if not node:
        raise HTTPException(404, f"node {ordinal} not found")
    if req.action == "park":
        if not req.reason.strip():
            raise HTTPException(400, "parking requires a reason")
        node.status = "parked"
        return {"ok": True, "status": "parked"}
    elif req.action == "reopen":
        node.status = "open"
        return {"ok": True, "status": "open"}
    elif req.action == "note":
        if not req.text.strip():
            raise HTTPException(400, "note needs text")
        node.notes.append(req.text.strip())
        return {"ok": True, "notes_count": len(node.notes)}
    raise HTTPException(400, f"unknown action {req.action!r}")

# ── Notebook ──
@app.get("/api/engagements/{engagement_id}/notebook")
def notebook_overview(engagement_id: str):
    notes = []
    for k in _scope_keys(engagement_id):
        notes += _notebooks.get(k, [])
    if not notes:
        return {"exists": False, "notes": [], "counts": {}, "version": 0}
    counts = Counter(n.category for n in notes)
    return {
        "exists": True,
        "notes": [n.to_dict() for n in notes],
        "counts": dict(counts),
        "version": len(notes),
    }

@app.post("/api/engagements/{engagement_id}/notebook/notes/{ordinal}/resolve")
def notebook_resolve(engagement_id: str, ordinal: int, req: NotebookResolveRequest):
    notes = []
    for k in _scope_keys(engagement_id):
        notes += _notebooks.get(k, [])
    for n in notes:
        if n.ordinal == ordinal:
            if req.action == "resolve":
                n.resolved = True
            elif req.action == "reopen":
                n.resolved = False
            return {"ok": True, "resolved": n.resolved}
    raise HTTPException(404, f"note {ordinal} not found")

# ── Technique KB ──
@app.get("/api/technique-kb")
def technique_kb():
    return {"techniques": _technique_kb, "count": len(_technique_kb)}

@app.delete("/api/technique-kb/{ordinal}")
def technique_kb_forget(ordinal: int):
    if 0 <= ordinal < len(_technique_kb):
        _technique_kb.pop(ordinal)
        return {"deleted": True}
    return {"deleted": False}

# ── Findings ──
@app.get("/api/engagements/{engagement_id}/findings")
def list_findings(engagement_id: str):
    findings = []
    for k in _scope_keys(engagement_id):
        findings += _findings.get(k, [])
    reviewed_count = sum(1 for f in findings if f.reviewed_by)
    return {
        "findings": [f.to_dict() for f in findings],
        "count": len(findings),
        "reviewed_count": reviewed_count,
    }

@app.delete("/api/engagements/{engagement_id}/findings/{finding_id}")
def delete_finding(engagement_id: str, finding_id: str):
    for k in _scope_keys(engagement_id):
        fs = _findings.get(k, [])
        if any(f.finding_id == finding_id for f in fs):
            _findings[k] = [f for f in fs if f.finding_id != finding_id]
            _persist()
            return {"deleted": True}
    return {"deleted": False}

@app.post("/api/engagements/{engagement_id}/findings/{finding_id}/review")
def review_finding(engagement_id: str, finding_id: str, req: ReviewFindingRequest):
    for k in _scope_keys(engagement_id):
        for f in _findings.get(k, []):
            if f.finding_id == finding_id:
                f.reviewed_by = req.reviewed_by
                _persist()
                return f.to_dict()
    raise HTTPException(404, f"finding {finding_id!r} not found")

# ── Guided run (back-compat path for the old "autonomous/start" endpoint) ──
# No longer a 6-phase auto-pipeline: it runs ONE guided turn and stops for the operator.
# The UI now drives guided mode through the normal /messages endpoint (guided=True).
@app.post("/api/sessions/{session_id}/autonomous/start")
def start_autonomous(session_id: str, req: AutonomousStartRequest):
    s = _sessions.get(session_id)
    if not s:
        raise HTTPException(404, f"session {session_id!r} not found")
    with s.lock:
        if s.running:
            raise HTTPException(409, "a task is already running")
        s.running = True
        s.autonomous_mode = req.mode
        s.stop_requested = False

    if req.user_message:
        s.append("user", req.user_message)

    def run():
        try:
            _run_guided(s, req.engagement_id, req.user_message)
        except Exception as e:
            s.push({"type": "task_error", "error": str(e)})
        finally:
            with s.lock:
                s.running = False
            _persist()

    threading.Thread(target=run, daemon=True).start()
    return {"status": "started", "mode": req.mode, "engagement_id": req.engagement_id}

@app.get("/api/sessions/{session_id}/autonomous/status")
def autonomous_status(session_id: str):
    s = _sessions.get(session_id)
    if not s:
        raise HTTPException(404, f"session {session_id!r} not found")
    return {"active": s.autonomous_active, "mode": s.autonomous_mode}

@app.post("/api/sessions/{session_id}/autonomous/stop")
def stop_autonomous(session_id: str):
    s = _sessions.get(session_id)
    if not s:
        raise HTTPException(404, f"session {session_id!r} not found")
    with s.lock:
        s.autonomous_active = False
        s.stop_requested = True
    return {"status": "stop_requested"}

# ── Static files ──
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ═══════════════════════════════════════════════════════════════════════════════
# Startup
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    global _llm, _rag

    parser = argparse.ArgumentParser(description="Oxpecker Dev Server")
    parser.add_argument("--llama-url", type=str, default="http://127.0.0.1:8080",
                        help="llama-server URL (default: http://127.0.0.1:8080)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", type=int, default=7777, help="Port")
    parser.add_argument("--rag-dir", type=str, default=None,
                        help="Directory with RAG corpus files (.txt/.md/.jsonl) — TF-IDF, small corpora only")
    parser.add_argument("--rag-meta", type=str, default=None,
                        help="Path to a SMALL meta.jsonl to TF-IDF (large indexes use --vector-index instead)")
    parser.add_argument("--vector-index", type=str, default=None,
                        help="Path to vectors.npy (dense RAG, memory-mapped). Defaults to knowledge_rag/index.")
    parser.add_argument("--vector-meta", type=str, default=None,
                        help="Path to meta.jsonl paired with --vector-index")
    parser.add_argument("--embed-url", type=str, default="http://127.0.0.1:8091",
                        help="nomic-embed llama-server URL for query embedding (default :8091)")
    parser.add_argument("--no-vector-rag", action="store_true",
                        help="Disable dense RAG even if an index is present")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    # Create data directory
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Restore persisted sessions/engagements/findings from a previous run
    _load_state()

    # Connect to llama-server
    _llm = LLMClient(server_url=args.llama_url)

    # ── RAG ──
    # Prefer the dense vector index (memory-mapped) when present. The TF-IDF path stays
    # for small corpora only — never TF-IDF the 1GB knowledge_rag meta.jsonl (it OOMs).
    kr_dir = Path(__file__).resolve().parent.parent / "knowledge_rag" / "index"
    vec_path = Path(args.vector_index) if args.vector_index else kr_dir / "vectors.npy"
    vmeta_path = Path(args.vector_meta) if args.vector_meta else kr_dir / "meta.jsonl"

    loaded = False
    if not args.no_vector_rag and vec_path.exists() and vmeta_path.exists():
        embed_ok = False
        try:
            with urllib.request.urlopen(f"{args.embed_url.rstrip('/')}/health", timeout=3) as r:
                embed_ok = r.status == 200
        except Exception as e:
            log.warning("Embedding server %s not reachable: %s", args.embed_url, e)
        if embed_ok:
            try:
                log.info("Loading dense RAG (mmap): %s", vec_path)
                _rag = VectorRAG(vec_path, vmeta_path, args.embed_url)
                log.info("RAG: %d chunks (dense, memory-mapped)", _rag.count())
                loaded = True
            except Exception as e:
                log.warning("Dense RAG load failed (%s) — falling back", e)
        else:
            log.warning("Dense index present but embedding server down — start it with:\n"
                        "  llama-server -m knowledge_rag/models/nomic-embed-text-v1.5.f16.gguf "
                        "--embedding --port 8091 -ngl 0")

    if not loaded:
        rag_count = 0
        if args.rag_meta:
            meta_path = Path(args.rag_meta)
            size_mb = meta_path.stat().st_size / 1e6 if meta_path.exists() else 0
            if size_mb > 20:
                log.warning("--rag-meta is %.0fMB — too large for in-memory TF-IDF; "
                            "use --vector-index instead. Skipping.", size_mb)
            else:
                log.info("Loading RAG (TF-IDF) from meta.jsonl: %s", meta_path)
                rag_count = _rag.index_from_existing_meta(meta_path)
        elif args.rag_dir:
            log.info("Indexing RAG corpus (TF-IDF) from: %s", args.rag_dir)
            rag_count = _rag.index_directory(Path(args.rag_dir))
        elif RAG_CORPUS_DIR.exists():
            log.info("Indexing RAG corpus (TF-IDF) from default: %s", RAG_CORPUS_DIR)
            rag_count = _rag.index_directory(RAG_CORPUS_DIR)
        log.info("RAG: %d documents indexed (TF-IDF)", rag_count)

    # Start server
    import uvicorn
    log.info("Starting Oxpecker dev server on http://%s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
