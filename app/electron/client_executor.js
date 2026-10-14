'use strict';
/*
 * "This computer" isolation — the executor that runs the server's delegated tool I/O on the
 * operator's own machine.
 *
 * The server authorises a tool (scope, broker, deny list all applied THERE) and parks its I/O. We
 * long-poll /api/client-jobs/next, run the job locally from this machine's network position, and
 * POST the result to /api/client-jobs/{id}/result. Three job kinds, mirroring the server tools:
 *   - run_command    : a single program (no shell) via child_process.execFile
 *   - http_request   : an HTTP(S) request via node http/https
 *   - port_discovery : TCP connect probes via node net
 *
 * We do NOT decide what may run. By the time a job arrives it has already passed the server's
 * scope and gate checks for its target; this file is only the hands, not the judgment.
 */
const http = require('http');
const https = require('https');
const net = require('net');
const { execFile } = require('child_process');
const { URL } = require('url');

let state = { running: false, base: null, key: null, loop: 0 };

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

// Minimal JSON-over-HTTP for talking to the Oxpecker server (the control channel, not the target).
function serverRequest(method, urlStr, key, bodyObj, timeoutMs) {
  return new Promise((resolve, reject) => {
    let u;
    try { u = new URL(urlStr); } catch (e) { return reject(e); }
    const lib = u.protocol === 'https:' ? https : http;
    const payload = bodyObj == null ? null : Buffer.from(JSON.stringify(bodyObj));
    const headers = { Accept: 'application/json' };
    if (key) headers['X-API-Key'] = key;
    if (payload) { headers['Content-Type'] = 'application/json'; headers['Content-Length'] = payload.length; }
    const req = lib.request(u, { method, headers, timeout: timeoutMs || 35000 }, (res) => {
      const chunks = [];
      res.on('data', (d) => chunks.push(d));
      res.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf-8');
        try { resolve({ status: res.statusCode, json: text ? JSON.parse(text) : null }); }
        catch (e) { resolve({ status: res.statusCode, json: null, text }); }
      });
    });
    req.on('error', reject);
    req.on('timeout', () => { req.destroy(new Error('server request timed out')); });
    if (payload) req.write(payload);
    req.end();
  });
}

/* ── job runners ─────────────────────────────────────────────────────────── */
function runCommand(payload) {
  return new Promise((resolve) => {
    const argv = Array.isArray(payload.argv) ? payload.argv.map(String) : [];
    if (!argv.length) return resolve({ ok: false, error: 'empty argv' });
    const started = Date.now();
    execFile(argv[0], argv.slice(1), { timeout: 110000, maxBuffer: 2 * 1024 * 1024, windowsHide: true },
      (err, stdout, stderr) => {
        const out = String((stdout || '') + (stderr || '')).slice(0, 1500);
        const duration_ms = Date.now() - started;
        if (err && err.killed) {
          return resolve({ ok: false, output: out, timed_out: true, duration_ms,
                           error: 'command timed out on this computer' });
        }
        const code = err && typeof err.code === 'number' ? err.code : (err ? 1 : 0);
        if (err && err.code === 'ENOENT') {
          return resolve({ ok: false, output: out, exit_code: null, duration_ms,
                           error: `program not found on this computer: ${argv[0]}` });
        }
        resolve({ ok: code === 0, exit_code: code, output: out, timed_out: false, duration_ms });
      });
  });
}

function httpRequest(payload) {
  return new Promise((resolve) => {
    let u;
    try { u = new URL(payload.url); } catch (e) { return resolve({ ok: false, error: 'bad url' }); }
    const lib = u.protocol === 'https:' ? https : http;
    const headers = Object.assign({}, payload.headers || {});
    const body = payload.body != null ? Buffer.from(String(payload.body)) : null;
    if (body) headers['Content-Length'] = body.length;
    const req = lib.request(u, { method: payload.method || 'GET', headers, timeout: 15000 }, (res) => {
      const chunks = [];
      let size = 0;
      res.on('data', (d) => { if (size < 200000) { chunks.push(d); size += d.length; } });
      res.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf-8').slice(0, 4000);
        resolve({ ok: true, status: res.statusCode, headers: res.headers, body: text });
      });
    });
    req.on('error', (e) => resolve({ ok: false, error: `${e.code || 'error'}: ${e.message}` }));
    req.on('timeout', () => { req.destroy(new Error('timeout')); });
    if (body) req.write(body);
    req.end();
  });
}

function probePort(host, port, timeoutMs) {
  return new Promise((resolve) => {
    const sock = new net.Socket();
    let settled = false;
    const done = (open) => { if (settled) return; settled = true; sock.destroy(); resolve(open); };
    sock.setTimeout(timeoutMs);
    sock.once('connect', () => done(true));
    sock.once('timeout', () => done(false));
    sock.once('error', () => done(false));
    sock.connect(port, host);
  });
}

async function portDiscovery(payload) {
  const host = String(payload.host || '');
  const ports = Array.isArray(payload.ports) ? payload.ports.map(Number).filter((n) => n > 0) : [];
  if (!host || !ports.length) return { ok: false, error: 'host and ports are required' };
  const results = [];
  for (const port of ports) {
    const open = await probePort(host, port, 2000);
    results.push({ port, open });
  }
  return { ok: true, host, open_ports: results.filter((r) => r.open).map((r) => r.port), results };
}

async function runJob(job) {
  const kind = job.kind;
  const payload = job.payload || {};
  if (kind === 'run_command') return runCommand(payload);
  if (kind === 'http_request') return httpRequest(payload);
  if (kind === 'port_discovery') return portDiscovery(payload);
  return { ok: false, error: `unknown job kind on this computer: ${kind}` };
}

/* ── poll loop ───────────────────────────────────────────────────────────── */
async function loop(myLoopId) {
  while (state.running && state.loop === myLoopId) {
    let job = null;
    try {
      const r = await serverRequest('GET', state.base + '/api/client-jobs/next?wait=25', state.key, null, 35000);
      job = r.json && r.json.job;
    } catch (e) {
      await sleep(2000);   // server unreachable — back off, keep trying
      continue;
    }
    if (!job) continue;    // long-poll expired with nothing to do
    let result;
    try { result = await runJob(job); }
    catch (e) { result = { ok: false, error: String((e && e.message) || e) }; }
    try {
      await serverRequest('POST', state.base + '/api/client-jobs/' + encodeURIComponent(job.id) + '/result',
        state.key, { result }, 15000);
    } catch (e) { /* submitter likely timed out; nothing to do */ }
  }
}

function start(serverBase, apiKey) {
  const base = String(serverBase || '').replace(/\/+$/, '');
  if (!base) return { ok: false, error: 'no server base' };
  // Restart cleanly if base/key changed; bump loop id so any old loop exits.
  state = { running: true, base, key: apiKey || '', loop: state.loop + 1 };
  loop(state.loop);
  return { ok: true };
}

function stop() {
  state.running = false;
  return { ok: true };
}

module.exports = { start, stop, _runJob: runJob };
