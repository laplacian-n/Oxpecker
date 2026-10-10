'use strict';
/*
 * Oxpecker desktop shell.
 *
 * On launch it brings up (only the ones not already running), in order:
 *   1. chat llama-server      (the decision model)
 *   2. embed llama-server     (nomic, for RAG)      — optional
 *   3. dev_server.py          (the Oxpecker backend)
 * then opens a BrowserWindow on the dev server once its health check passes.
 * Every process it spawns is killed on quit. Servers already listening are reused,
 * never double-started.
 *
 * Paths/ports live in electron/config.json so this works on another machine without
 * editing code. Nothing heavy is bundled — Python, llama-server and the model are the
 * user's own (see doc/handoff.md). This is the MVP "Option C + B" shell.
 */
const { app, BrowserWindow, dialog, shell } = require('electron');
const { spawn } = require('child_process');
const http = require('http');
const path = require('path');
const fs = require('fs');

// Auto-update via GitHub Releases (no-ops in dev / when unpackaged).
let autoUpdater = null;
try { autoUpdater = require('electron-updater').autoUpdater; } catch (_) { /* dev */ }

function initAutoUpdate() {
  if (!autoUpdater || !app.isPackaged) return;
  autoUpdater.autoDownload = true;
  autoUpdater.on('update-downloaded', (info) => {
    dialog.showMessageBox({
      type: 'info', buttons: ['Restart now', 'Later'], defaultId: 0,
      title: 'Update ready',
      message: `Oxpecker ${info.version} downloaded. Restart to update?`,
    }).then((r) => { if (r.response === 0) autoUpdater.quitAndInstall(); });
  });
  autoUpdater.on('error', (e) => console.error('[update]', e && e.message));
  autoUpdater.checkForUpdatesAndNotify().catch(() => {});
}

const PROJECT_ROOT = path.resolve(__dirname, '..');
// Prefer a private, machine-specific override (config.local.json) that is gitignored and never
// bundled into the installer, so a distributed build never carries anyone's personal paths.
// Falls back to the committed template config.json.
const _LOCAL_CONFIG = path.join(__dirname, 'config.local.json');
const CONFIG_PATH = fs.existsSync(_LOCAL_CONFIG) ? _LOCAL_CONFIG : path.join(__dirname, 'config.json');

function loadConfig() {
  const cfg = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf-8'));
  // resolve relative paths against the project root
  const abs = (p) => (p && !path.isAbsolute(p) ? path.join(PROJECT_ROOT, p) : p);
  for (const key of ['chatLlama', 'embedLlama']) {
    if (cfg[key]) { cfg[key].exe = abs(cfg[key].exe); cfg[key].model = abs(cfg[key].model); }
  }
  if (cfg.devServer) cfg.devServer.script = abs(cfg.devServer.script);
  return cfg;
}

const children = [];
let mainWindow = null;
let cfg = null;

/* ── process + health helpers ───────────────────────────────────────────── */
function httpOk(url, timeoutMs = 2500) {
  return new Promise((resolve) => {
    const req = http.get(url, { timeout: timeoutMs }, (res) => {
      res.resume();
      resolve(res.statusCode >= 200 && res.statusCode < 500);
    });
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
  });
}

async function waitForHealth(url, label, totalMs = 180000) {
  const start = Date.now();
  while (Date.now() - start < totalMs) {
    if (await httpOk(url)) return true;
    setStatus(`Waiting for ${label}…`);
    await new Promise((r) => setTimeout(r, 1000));
  }
  return false;
}

function spawnProc(label, command, args, opts = {}) {
  setStatus(`Starting ${label}…`);
  const child = spawn(command, args, { cwd: PROJECT_ROOT, windowsHide: true, ...opts });
  child._label = label;
  child.on('error', (e) => console.error(`[${label}] spawn error:`, e.message));
  child.stdout && child.stdout.on('data', (d) => process.stdout.write(`[${label}] ${d}`));
  child.stderr && child.stderr.on('data', (d) => process.stderr.write(`[${label}] ${d}`));
  children.push(child);
  return child;
}

function killAll() {
  for (const c of children) {
    if (!c || c.killed || c.pid == null) continue;
    try {
      if (process.platform === 'win32') spawn('taskkill', ['/pid', String(c.pid), '/T', '/F']);
      else c.kill('SIGTERM');
    } catch (_) { /* ignore */ }
  }
}

/* ── loading window ─────────────────────────────────────────────────────── */
function setStatus(msg) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.executeJavaScript(
      `window.__setStatus && window.__setStatus(${JSON.stringify(msg)})`
    ).catch(() => {});
  }
  console.log('[oxpecker]', msg);
}

function loadingHtml() {
  return 'data:text/html;charset=utf-8,' + encodeURIComponent(`
<!doctype html><html><head><meta charset="utf-8"><title>Oxpecker</title>
<style>
  html,body{margin:0;height:100%;background:#000;color:#ededed;font:400 14px system-ui,sans-serif}
  .wrap{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px}
  .logo{font:600 22px 'JetBrains Mono',monospace;color:oklch(62% 0.21 25)}
  .spin{width:26px;height:26px;border:3px solid #222;border-top-color:oklch(62% 0.21 25);border-radius:50%;animation:s .8s linear infinite}
  @keyframes s{to{transform:rotate(360deg)}}
  #status{color:#8a8a8a;font-size:13px}
</style></head><body><div class="wrap">
  <div class="logo">Oxpecker</div><div class="spin"></div><div id="status">Starting…</div>
</div><script>window.__setStatus=function(m){document.getElementById('status').textContent=m}</script>
</body></html>`);
}

/* ── startup orchestration ──────────────────────────────────────────────── */
async function ensureServer(c, healthUrl, label) {
  if (await httpOk(healthUrl)) { console.log(`[oxpecker] ${label} already running — reusing`); return true; }
  if (!c || !c.exe || !fs.existsSync(c.exe)) {
    console.warn(`[oxpecker] ${label}: exe not found (${c && c.exe}) — skipping spawn`);
    return await httpOk(healthUrl);
  }
  if (!fs.existsSync(c.model)) {
    console.warn(`[oxpecker] ${label}: model not found (${c.model}) — skipping spawn`);
    return false;
  }
  const args = ['-m', c.model, '--host', '127.0.0.1', '--port', String(c.port), ...(c.args || [])];
  spawnProc(label, c.exe, args);
  return await waitForHealth(healthUrl, label);
}

async function startEverything() {
  cfg = loadConfig();

  // Remote mode: the server runs on another host (the Ubuntu box) — spawn nothing locally, just
  // load its UI. Set "remoteUrl" in config.local.json (e.g. "http://192.168.1.50:7777"); once the
  // page loads, paste the API key into the UI's key field. /health is the unauthenticated probe
  // the server keeps open for exactly this. See docs/DEPLOY_UBUNTU.md.
  if (cfg.remoteUrl) {
    const base = String(cfg.remoteUrl).replace(/\/$/, '');
    console.log(`[oxpecker] remote mode — loading ${base}, spawning nothing locally`);
    const ok = await waitForHealth(`${base}/health`, 'remote Oxpecker server', 20000);
    if (!ok) return fail(`Could not reach the remote Oxpecker server at ${base}. Is it running and reachable on the LAN (and bound with --host 0.0.0.0)?`);
    mainWindow.loadURL(base);
    return;
  }

  const chatUrl = `http://127.0.0.1:${cfg.chatLlama.port}/health`;
  const embedUrl = cfg.embedLlama ? `http://127.0.0.1:${cfg.embedLlama.port}/health` : null;
  const devUrl = `http://127.0.0.1:${cfg.devServer.port}/api/health`;
  const appUrl = `http://127.0.0.1:${cfg.devServer.port}`;

  if (cfg.startLlama !== false) {
    const okChat = await ensureServer(cfg.chatLlama, chatUrl, 'chat llama-server');
    if (!okChat) return fail('The chat model (llama-server) did not start. Check electron/config.json paths.');
    if (cfg.embedLlama) await ensureServer(cfg.embedLlama, embedUrl, 'embed llama-server'); // optional (RAG)
  }

  if (!(await httpOk(devUrl))) {
    const py = cfg.pythonCmd || 'py';
    // Launched as a module, not as a script path. dev_server.py uses package-relative imports
    // (`from .. import config`), and `python agent/web/dev_server.py` gives those no parent
    // package: it died with "attempted relative import with no known parent package" before
    // binding a port, every time. The failure then surfaced as the generic "did not start —
    // is Python installed?" message below, which sends you after dependencies that are fine.
    // The module path is derived from cfg.devServer.script so config.json stays the one place
    // that names the entry point.
    const devModule = String(cfg.devServer.script)
      .replace(/\.py$/, '').replace(/[\\/]/g, '.');
    const devArgs = ['-m', devModule, '--port', String(cfg.devServer.port)];
    if (cfg.devServer.ragIndex) devArgs.push('--vector-index', cfg.devServer.ragIndex);
    if (cfg.devServer.ragMeta) devArgs.push('--vector-meta', cfg.devServer.ragMeta);
    if (cfg.devServer.embedUrl) devArgs.push('--embed-url', cfg.devServer.embedUrl);
    spawnProc('dev_server', py, devArgs);
  }
  const okDev = await waitForHealth(devUrl, 'Oxpecker backend');
  // Kept in sync with dev_server.py's own Prerequisites docstring and docs/DEPLOY_UBUNTU.md.
  // This line named `sse-starlette` and `python-multipart` (never imported anywhere in this
  // repo's history — grep found nothing, `git log -S` found nothing) and omitted `cryptography`
  // (a real, load-bearing import since agent/evidence/store.py landed) and `uvicorn[standard]`
  // (the bare package has no WebSocket implementation). A user who hit this dialog and followed
  // it verbatim would still not have a working venv.
  if (!okDev) return fail('The Oxpecker backend (dev_server.py) did not start. Is Python installed? Try: py -m pip install fastapi "uvicorn[standard]" scikit-learn numpy cryptography pydantic requests jinja2 PyYAML fpdf2 "mcp<2" playwright');

  setStatus('Ready');
  mainWindow.loadURL(appUrl);
}

function fail(message) {
  console.error('[oxpecker] startup failed:', message);
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(`
<!doctype html><meta charset="utf-8"><body style="background:#000;color:#ededed;font:14px system-ui;padding:40px">
<h2 style="color:oklch(62% 0.21 25)">Oxpecker could not start</h2>
<p style="color:#b5b5b5;max-width:640px;line-height:1.6">${message}</p>
<p style="color:#7d7d7d">Edit <code>electron/config.json</code> and relaunch.</p></body>`));
  }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280, height: 860, backgroundColor: '#000000',
    title: 'Oxpecker', autoHideMenuBar: true,
    icon: path.join(PROJECT_ROOT, 'build', 'icon.png'),
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  // open external links in the system browser, not inside the app
  mainWindow.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: 'deny' }; });
  mainWindow.loadURL(loadingHtml());
  mainWindow.webContents.once('did-finish-load', () => { startEverything().catch((e) => fail(String(e))); });
}

app.whenReady().then(() => { createWindow(); initAutoUpdate(); });
app.on('window-all-closed', () => { killAll(); if (process.platform !== 'darwin') app.quit(); });
app.on('before-quit', killAll);
process.on('exit', killAll);
