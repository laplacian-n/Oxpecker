'use strict';
/*
 * Preload bridge for "this computer" isolation.
 *
 * The UI (index.html) runs in an isolated world with no Node access. When the operator selects a
 * session whose isolation is "this computer", the server delegates that session's tool I/O to this
 * desktop app to run from the operator's own machine. The UI cannot spawn processes or open raw
 * sockets, so it asks the main process to, through this narrow bridge:
 *
 *   window.oxpecker.startClientExecutor(serverBase, apiKey)  — begin pulling + running jobs
 *   window.oxpecker.stopClientExecutor()                     — stop (e.g. switched to cloud)
 *
 * Only these two calls cross the boundary; no Node primitives are exposed to the page, and the page
 * cannot choose what runs — the server decides that and has already enforced scope before a job is
 * ever offered here.
 */
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('oxpecker', {
  startClientExecutor: (serverBase, apiKey) =>
    ipcRenderer.invoke('oxpecker:start-executor', { serverBase, apiKey }),
  stopClientExecutor: () => ipcRenderer.invoke('oxpecker:stop-executor'),
});
