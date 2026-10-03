# Handoff: Oxpecker — Self-Hosted AI Pentest Agent

Updated 2026-10-03. For the next Claude session on the **Windows laptop** (192.168.1.104).

## What is this

Oxpecker is a self-hosted AI penetration testing agent with a web UI. The user runs a local LLM
via llama-server (llama.cpp, Vulkan GPU) and this project provides the backend + frontend.

**This is a private repo** — `laplacian-n/localAI` on GitHub.

## Machine setup

| Machine | Role | OS | IP |
|---------|------|----|----|
| Windows laptop | Dev server + llama-server + UI | Windows, RTX 4050 6GB | 192.168.1.104 |
| Linux server | Targets (Juice Shop on Docker), full agent suite | Ubuntu | 192.168.1.118 |

**Windows specifics:**
- Python 3.14, use `py` command (not `python`)
- llama-server with Vulkan backend
- Model: Qwen3.5-4B-Q6_K.gguf (or similar)
- Dev server runs on port 7777
- Juice Shop target at `http://192.168.1.118:3000/` (Docker on Linux machine)

## Two backends — which one matters

There are two backend files. **Only `dev_server.py` matters for now:**

| File | Status | What it is |
|------|--------|-----------|
| `agent/web/dev_server.py` | **Active** | Standalone FastAPI backend. All-in-one, no dependencies on agent modules. This is what runs on Windows. |
| `agent/web/server.py` | Legacy | Full-featured backend using all agent modules. Only runs on Linux where the full agent tree is installed. Not actively developed. |

## How to run

```bash
# 1. Start llama-server (separate terminal)
llama-server -m path/to/model.gguf -ngl 99 -fa -c 8192 --port 8080

# 2. Start dev_server
py agent/web/dev_server.py

# 3. Open browser
# http://localhost:7777
```

Dependencies: `py -m pip install fastapi uvicorn httpx sse-starlette python-multipart`

## Architecture

```
Browser (index.html)
    │
    │ HTTP REST + SSE (port 7777)
    │
dev_server.py (FastAPI)
    │
    │ HTTP /v1/chat/completions (port 8080)
    │
llama-server (llama.cpp, Vulkan)
```

- **SSE streaming**: `GET /api/sessions/{id}/events` pushes `message`, `assistant_delta`,
  `reasoning_delta`, `task_done`, `task_error`, `autonomous_event`, `autonomous_done` events.
- **Tool execution**: LLM returns tool_calls → dev_server executes them → results fed back
  in the next LLM turn. Up to 5 tool rounds per agent turn.
- **Autonomous mode**: Runs phases sequentially (INTAKE → RECON → ANALYSIS → VALIDATION →
  REPORT → CLOSEOUT), each phase is an agent turn with a phase-specific prompt.

## UI design source of truth

`ai-web-platform-mockups_v2/Oxpecker UI.dc.html` — the full Claude Design mockup.
`ai-web-platform-mockups_v2/Oxpecker Chat Animation Demo.dc.html` — how tool chains should animate.

Design system: pure black `#000` background, `#050505` sidebar, red accent `oklch(62% 0.21 25)`,
fonts: `Instrument Sans` (UI) + `JetBrains Mono` (code). Both loaded from Google Fonts CDN.

## Known bugs to fix

### 1. Stop button doesn't reliably stop execution
The stop mechanism was just added but hasn't been tested yet. The stop button now appears in the
chat input area (replaces send button during running), and the server checks `stop_requested`
between tool rounds. But:
- The LLM streaming call itself can't be interrupted mid-stream (it blocks until the current
  chunk finishes).
- If the model is generating a very long response, stop won't take effect until the current
  stream completes and control returns to the tool loop.
- Need to test: does `stop_requested` flag actually propagate correctly in all code paths?

### 2. Agent runs wrong commands on Windows
The LLM tries Linux commands (`nmap`, `ss`, `netstat`, `lsof`) on Windows where they don't exist.
The system prompt and tools need to:
- Detect the OS and tell the model what's available
- Provide Windows-compatible alternatives (PowerShell: `Test-NetConnection`, `Invoke-WebRequest`,
  `nslookup`, etc.)
- Or: route tool execution to the Linux machine via SSH/API

### 3. Target resolution
When user types "เจาะระบบ http://192.168.1.118:3000/", the autonomous pipeline was using hardcoded
engagement targets (`127.0.0.1`, `localhost`) instead of the URL from the user's message. A fix was
added to extract URLs from the user message and add them to `allow_targets`, but needs testing.

### 4. Tool display edge cases
The tool chain display was rewritten to show clean labels (Run/Read/Edit/Knowledge base) with
terminal blocks for command output. But:
- No expand/collapse animation yet (demo has CSS grid `0fr/1fr` transitions)
- No diff display for `write_file` results
- Per-line streaming of terminal output not implemented
- Long command output may not be truncated properly

### 5. Auto-refresh still occasionally disrupts
The debounce + background render guard mostly fixed the 3-5s refresh issue, but full innerHTML
rebuild still happens. Consider switching to incremental DOM updates for the streaming parts
(streaming text, streaming reasoning, run elapsed timer).

### 6. Polling during idle
Approval/consult polling is guarded (`if(!S.activeSession||!S.running)return`) but the
`setInterval` itself still runs. Could be cleaned up to only start/stop with session state.

## What's already working

- SSE streaming with live text + reasoning display
- `<think>` tag parsing for Qwen3.5 thinking mode
- Tool calls sent to LLM API (OpenAI format `delta.tool_calls`)
- Tool chain display: clean labels, terminal blocks, status badges
- Session management (create, list, delete, search)
- Hypothesis tree panel
- Working notebook tab
- Findings tracker with review gate
- Context window monitor popup (opens upward)
- Steer bar (redirect running task)
- 3 autonomy modes (assistant / autonomous / consult)
- Approval mechanism disabled (commands run without approval)
- Duplicate message deduplication
- Scroll preservation during streaming (softScroll)
- Background render guard (no disruption during typing/dropdown)

## Electron packaging plan

The goal is to package Oxpecker as a standalone `.exe` desktop app.

### Architecture
```
Electron main process
    ├── Spawns llama-server (bundled or user-provided)
    ├── Spawns dev_server.py (bundled Python or pyinstaller'd)
    └── BrowserWindow loads http://localhost:7777
```

### What's already Electron-ready
- `dev_server.py` has CORS headers (`Access-Control-Allow-Origin: *`)
- `dev_server.py` has a health check endpoint (`GET /health`)
- `index.html` is a single static file — no build step
- All API calls use relative URLs (no hardcoded host)

### Steps to package
1. **Create Electron shell** (`electron/main.js`):
   - On startup: spawn `dev_server.py` as a child process
   - Wait for health check to return 200
   - Open BrowserWindow pointing to `http://localhost:7777`
   - On close: kill the child process

2. **Bundle Python runtime**:
   - Option A: PyInstaller — bundle `dev_server.py` into a single `.exe`
   - Option B: Embedded Python — ship a portable Python distribution
   - Option C: Require user to have Python installed (simplest for now)

3. **Bundle llama-server**:
   - Option A: Ship `llama-server.exe` with Vulkan support
   - Option B: Let user point to their own llama-server
   - Option C: Model download + auto-setup on first run

4. **Build with electron-builder**:
   ```bash
   npm init
   npm install electron electron-builder --save-dev
   npx electron-builder --win
   ```

### Recommended approach for MVP
Start with Option C (require Python) + Option B (user provides llama-server):
- Minimal packaging — just the Electron shell + `dev_server.py` + `index.html`
- Settings UI to configure: model path, llama-server path, port
- First-run wizard that checks prerequisites

## File inventory

### Essential files (the only ones that matter for dev)
```
agent/web/dev_server.py          # Backend — edit this
agent/web/static/index.html      # Frontend — edit this
agent/web/static/oxpecker-logo.png
```

### Design references (read-only)
```
ai-web-platform-mockups_v2/Oxpecker UI.dc.html
ai-web-platform-mockups_v2/Oxpecker Chat Animation Demo.dc.html
```

### Full agent suite (Linux only, not needed for Windows dev)
```
agent/broker/          # RoE/scope enforcement
agent/sandbox/         # Bubblewrap isolation
agent/pipeline/        # Phase state machine
agent/security_tools/  # http_recon, port_discovery
agent/knowledge_rag/   # Knowledge base
agent/hypothesis_graph/# Hypothesis tracking
agent/findings/        # Findings model
agent/evidence/        # Evidence store
agent/engagement/      # Engagement lifecycle
agent/tools/           # Tool schemas
agent/prompts/         # System prompts
agent/loop.py          # Agent loop
agent/web/server.py    # Full backend (uses all above)
```

### Can be cleaned from git
```
ai-web-platform-mockups/     # Old v1 mockups (superseded by v2)
agent/dev_mcp_server.py      # MCP server prototype (unused)
doc/fromGPTandHackerAI/      # Research notes (not source)
```

## Key code patterns

### dev_server.py
- `LlamaClient.chat()` — HTTP call to llama-server with streaming
- `_run_agent_turn()` — single LLM turn: build messages → stream response → parse tool calls → execute → loop
- `_run_autonomous()` — phase loop calling `_run_agent_turn()` per phase
- `_run_tool()` — tool dispatch (run_command uses subprocess)
- `Session.push()` — push SSE event to client
- `Session.append()` — add message to history + push SSE

### index.html
- `S` object — all UI state
- `render()` — debounced full re-render via `requestAnimationFrame`
- `connectSSE()` — EventSource connection to `/api/sessions/{id}/events`
- `sendMessage()` — send user message or start autonomous run
- `stopRun()` — stop running task
- `renderToolCallStep()` — tool chain display with `_toolMeta()` helper
- `softScroll()` — only auto-scroll if user is at bottom
- `afterRender()` — post-render hooks (textarea auto-resize, elapsed timer, scroll tracking)

## User preferences (from memory)

- Never store or reference user's real name — use "user"
- User speaks Thai, comments/UI text in English
- User values visual polish — the mockup is the source of truth
- User dislikes: raw JSON in UI, unnecessary approval prompts, auto-refresh that disrupts typing
- Python command on Windows: `py` (not `python`)
