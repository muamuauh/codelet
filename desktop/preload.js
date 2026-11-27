"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("codeletDesktop", {
  isDesktop: true,
  platform: process.platform,
  pickWorkspaceDir: (defaultPath) =>
    ipcRenderer.invoke("desktop:pick-workspace", defaultPath || ""),
  saveMarkdown: (filename, content) =>
    ipcRenderer.invoke("desktop:save-markdown", {
      filename: filename || "transcript.md",
      content: String(content ?? ""),
    }),
  openExternal: (url) => ipcRenderer.invoke("desktop:open-external", String(url || "")),
  revealPath: (targetPath) => ipcRenderer.invoke("desktop:reveal-path", String(targetPath || "")),
  setTitle: (title) => ipcRenderer.send("desktop:set-title", String(title || "codelet")),
  reportWorkspace: (cwd) => ipcRenderer.send("desktop:workspace-changed", String(cwd || "")),
  quitApp: () => ipcRenderer.invoke("desktop:quit"),
  getVersion: () => ipcRenderer.invoke("desktop:version"),
  checkForUpdates: () => ipcRenderer.invoke("desktop:check-updates"),
  getVaultInfo: () => ipcRenderer.invoke("desktop:vault-info"),
  saveSecret: (name, value) =>
    ipcRenderer.invoke("desktop:save-secret", {
      name: String(name || ""),
      value: String(value ?? ""),
    }),
  deleteSecret: (name) => ipcRenderer.invoke("desktop:delete-secret", String(name || "")),
  onAction: (callback) => {
    if (typeof callback !== "function") return () => {};
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("desktop:action", listener);
    return () => ipcRenderer.removeListener("desktop:action", listener);
  },
});
