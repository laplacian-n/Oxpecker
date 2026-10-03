# Oxpecker

Self-hosted AI penetration testing agent. Runs a local LLM (llama.cpp) with a web UI for interactive and autonomous security assessments.

## Architecture

```
┌─────────────┐     HTTP /v1/chat/completions     ┌──────────────┐
│ llama-server │◄─────────────────────────────────►│  dev_server  │
│ (llama.cpp)  │     (OpenAI-compatible API)       │  (FastAPI)   │
│  Vulkan GPU  │                                   │  port 7777   │
└─────────────┘                                    └──────┬───────┘
                                                          │ HTTP / SSE
                                                   ┌──────▼───────┐
                                                   │   Browser    │
                                                   │  index.html  │
                                                   └──────────────┘
```

- **llama-server** — serves the model via OpenAI-compatible API. Any GGUF model works.
- **dev_server.py** — FastAPI backend handling sessions, tool execution, RAG, autonomous pipeline, SSE streaming.
- **index.html** — Single-file vanilla JS SPA. No build step, no framework.

## Quick Start (Windows)

### Prerequisites
- Python 3.12+ (`py` launcher)
- [llama.cpp](https://github.com/ggerganov/llama.cpp) with Vulkan support
- A GGUF model (tested with Qwen3.5-4B-Q6_K)

### 1. Start llama-server
```bash
llama-server -m path/to/model.gguf -ngl 99 -fa -c 8192 --port 8080
```

### 2. Install dependencies
```bash
py -m pip install fastapi uvicorn httpx sse-starlette python-multipart
```

### 3. Start dev_server
```bash
py agent/web/dev_server.py
```

Open `http://localhost:7777` in your browser.

## Quick Start (Linux)

```bash
python3 -m pip install fastapi uvicorn httpx sse-starlette python-multipart
python3 agent/web/dev_server.py
```

## Project Structure

```
agent/
├── web/
│   ├── dev_server.py          # Simplified FastAPI backend (standalone, all-in-one)
│   ├── server.py              # Full-featured backend (uses all agent modules)
│   └── static/
│       ├── index.html         # Oxpecker UI (vanilla JS SPA)
│       └── oxpecker-logo.png  # Logo
├── broker/                    # RoE/scope enforcement, approval/consult queues
├── sandbox/                   # Bubblewrap isolation, seccomp profiles
├── pipeline/                  # Phase state machine, autonomous driver
├── security_tools/            # http_recon, port_discovery
├── knowledge_rag/             # Knowledge base search (SearXNG + local corpus)
├── hypothesis_graph/          # Typed-DAG hypothesis tracking
├── findings/                  # Findings model, SARIF export
├── evidence/                  # HMAC-addressed evidence store
├── engagement/                # Engagement lifecycle, intake
├── tools/                     # Tool schemas and execution
├── prompts/                   # System prompt registry
└── loop.py                    # Agent loop (tool-calling loop)

ai-web-platform-mockups_v2/    # Claude Design mockups (design source of truth)
doc/                           # Architecture docs, phase plans, handoffs
```

## Design References

The UI design source of truth is in `ai-web-platform-mockups_v2/`:
- `Oxpecker UI.dc.html` — Full UI mockup (Claude Design)
- `Oxpecker Chat Animation Demo.dc.html` — Tool chain animation reference
- `assets/oxpecker-logo.png` — Logo source

## Two Backends

| | `dev_server.py` | `server.py` |
|---|---|---|
| **Purpose** | Standalone dev/demo | Full production |
| **Dependencies** | FastAPI + httpx only | All agent modules |
| **Features** | Sessions, tools, RAG, autonomous mode, SSE | Everything + broker, sandbox, evidence, audit |
| **Use when** | Developing UI, quick testing | Full security assessment |

`dev_server.py` is the active backend for development. It's self-contained and doesn't require the full agent module tree.

## Autonomy Modes

- **Assistant** — Operator drives every turn via chat
- **Autonomous** — Pipeline runs all phases unattended (INTAKE → RECON → ANALYSIS → VALIDATION → REPORT → CLOSEOUT)
- **Consult** — Pauses at each phase boundary for operator confirmation

## UI Features

- Real-time SSE streaming with thinking/reasoning display
- Tool chain visualization (terminal blocks, file operations, knowledge search)
- Hypothesis tree panel
- Working notebook
- Findings tracker
- Context window monitor
- Session management with search
- Dark theme (pure black `#000`, red accent)

## Electron (Desktop App)

See [doc/handoff.md](doc/handoff.md) for the Electron packaging plan. The architecture is ready:
- `dev_server.py` has CORS headers and health check endpoint
- `index.html` is a single static file — no build step needed
- The Electron app wraps the FastAPI server + loads the UI in a BrowserWindow

## License

Private repository.
