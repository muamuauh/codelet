"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { app, BrowserWindow, dialog, ipcMain, screen, shell } = require("electron");
const { BackendManager } = require("./backend");
const { SecretVault } = require("./secrets");
const { createAppTray } = require("./tray");
const { setupAutoUpdater, checkForUpdates, installDownloadedUpdate } = require("./updater");

const STATE_FILE = () => path.join(app.getPath("userData"), "desktop-state.json");
const REPO_ROOT = path.resolve(__dirname, "..");

let mainWindow = null;
let tray = null;
let backend = null;
let vault = null;
let backendOrigin = "";

function extractWorkspaceArg(argv) {
  if (!Array.isArray(argv)) return "";
  const startIdx = app.isPackaged ? 1 : 2;
  for (let i = startIdx; i < argv.length; i++) {
    const raw = String(argv[i] || "").trim().replace(/^["']|["']$/g, "");
    if (!raw || raw.startsWith("-") || raw === ".") continue;
    try {
      const resolved = path.resolve(raw);
      if (fs.existsSync(resolved) && fs.statSync(resolved).isDirectory()) {
        return resolved;
      }
    } catch (_) {}
  }
  return "";
}
let isQuitting = false;
let trayTipShown = false;
let currentWorkspace = "";

function loadState() {
  try {
    const raw = fs.readFileSync(STATE_FILE(), "utf8");
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch (_) {
    return {};
  }
}

function saveState(patch) {
  const prev = loadState();
  const next = { ...prev, ...patch };
  try {
    fs.mkdirSync(path.dirname(STATE_FILE()), { recursive: true });
    fs.writeFileSync(STATE_FILE(), JSON.stringify(next, null, 2), "utf8");
  } catch (_) {}
}

function clampBounds(bounds) {
  const fallback = { width: 1360, height: 860 };
  if (!bounds || typeof bounds.width !== "number" || typeof bounds.height !== "number") {
    return fallback;
  }
  const width = Math.max(960, Math.min(bounds.width, 3840));
  const height = Math.max(620, Math.min(bounds.height, 2160));
  if (typeof bounds.x !== "number" || typeof bounds.y !== "number") {
    return { width, height };
  }
  const visible = screen.getAllDisplays().some((d) => {
    const area = d.workArea;
    return (
      bounds.x + 80 >= area.x &&
      bounds.x <= area.x + area.width - 80 &&
      bounds.y >= area.y &&
      bounds.y <= area.y + area.height - 80
    );
  });
  return visible ? { x: bounds.x, y: bounds.y, width, height } : { width, height };
}

function isSafeHttpUrl(url) {
  return /^https?:\/\//i.test(String(url || ""));
}

function resolveAsset(filename) {
  return path.join(__dirname, "assets", filename);
}

function showMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

function toggleMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (mainWindow.isVisible() && !mainWindow.isMinimized()) {
    mainWindow.hide();
  } else {
    showMainWindow();
  }
}

function dispatchAction(type, data = {}) {
  showMainWindow();
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("desktop:action", { type, ...data });
  }
}

async function quitApp() {
  if (isQuitting) return;
  isQuitting = true;
  if (mainWindow && !mainWindow.isDestroyed()) {
    const maximized = mainWindow.isMaximized();
    const bounds = maximized ? mainWindow.getNormalBounds() : mainWindow.getBounds();
    saveState({ bounds: { ...bounds, maximized }, lastWorkspace: currentWorkspace });
  }
  if (tray) {
    try {
      tray.destroy();
    } catch (_) {}
    tray = null;
  }
  if (backend) {
    try {
      await backend.stop();
    } catch (_) {}
  }
  app.quit();
}

function registerIpc() {
  ipcMain.handle("desktop:pick-workspace", async (_event, defaultPath) => {
    const startDir =
      defaultPath && fs.existsSync(defaultPath)
        ? defaultPath
        : currentWorkspace && fs.existsSync(currentWorkspace)
          ? currentWorkspace
          : os.homedir();
    const res = await dialog.showOpenDialog(mainWindow, {
      title: "选择工作区目录",
      defaultPath: startDir,
      properties: ["openDirectory", "createDirectory"],
    });
    if (res.canceled || !res.filePaths || !res.filePaths.length) {
      return null;
    }
    const chosen = res.filePaths[0];
    currentWorkspace = chosen;
    saveState({ lastWorkspace: chosen });
    return chosen;
  });

  ipcMain.handle("desktop:save-markdown", async (_event, { filename, content }) => {
    const baseDir = currentWorkspace && fs.existsSync(currentWorkspace)
      ? currentWorkspace
      : app.getPath("documents");
    const res = await dialog.showSaveDialog(mainWindow, {
      title: "导出对话记录",
      defaultPath: path.join(baseDir, filename || "transcript.md"),
      filters: [
        { name: "Markdown", extensions: ["md"] },
        { name: "All Files", extensions: ["*"] },
      ],
    });
    if (res.canceled || !res.filePath) {
      return { ok: false, canceled: true };
    }
    try {
      fs.writeFileSync(res.filePath, String(content ?? ""), "utf8");
      return { ok: true, canceled: false, path: res.filePath };
    } catch (err) {
      return { ok: false, canceled: false, error: err.message };
    }
  });

  ipcMain.handle("desktop:open-external", async (_event, url) => {
    if (!isSafeHttpUrl(url)) return false;
    await shell.openExternal(url);
    return true;
  });

  ipcMain.handle("desktop:reveal-path", async (_event, targetPath) => {
    if (!targetPath || !fs.existsSync(targetPath)) return false;
    const stat = fs.statSync(targetPath);
    if (stat.isDirectory()) {
      await shell.openPath(targetPath);
    } else {
      shell.showItemInFolder(targetPath);
    }
    return true;
  });

  ipcMain.on("desktop:set-title", (_event, title) => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.setTitle(String(title || "codelet"));
    }
  });

  ipcMain.on("desktop:workspace-changed", (_event, cwd) => {
    if (cwd && typeof cwd === "string") {
      currentWorkspace = cwd;
      saveState({ lastWorkspace: cwd });
    }
  });

  ipcMain.handle("desktop:quit", async () => {
    await quitApp();
    return true;
  });

  ipcMain.handle("desktop:version", () => app.getVersion());
  ipcMain.handle("desktop:check-updates", () => checkForUpdates(app, { manual: true }));
  ipcMain.handle("desktop:install-update", () => installDownloadedUpdate());

  ipcMain.handle("desktop:vault-info", () => {
    const available = vault ? vault.isAvailable() : false;
    return {
      available,
      storage: available ? (process.platform === "win32" ? "dpapi" : "os-keychain") : "unavailable",
      keys: vault ? vault.listSecretNames() : [],
    };
  });

  ipcMain.handle("desktop:save-secret", (_event, payload) => {
    if (!vault) return { ok: false, error: "Secret vault not initialized." };
    return vault.setSecret(payload?.name, payload?.value);
  });

  ipcMain.handle("desktop:delete-secret", (_event, name) => {
    if (!vault) return { ok: false, error: "Secret vault not initialized." };
    return vault.deleteSecret(name);
  });
}

function createWindow() {
  const saved = loadState();
  const bounds = clampBounds(saved.bounds);
  const iconFile = process.platform === "win32" ? "icon.ico" : "icon.png";

  mainWindow = new BrowserWindow({
    ...bounds,
    minWidth: 960,
    minHeight: 620,
    show: false,
    title: "codelet",
    icon: resolveAsset(iconFile),
    backgroundColor: "#0f172a",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  if (saved.bounds && saved.bounds.maximized) {
    mainWindow.maximize();
  }

  mainWindow.once("ready-to-show", () => {
    mainWindow.show();
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (isSafeHttpUrl(url)) shell.openExternal(url);
    return { action: "deny" };
  });

  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (backendOrigin && url.startsWith(backendOrigin)) return;
    if (url.startsWith("file://")) return;
    event.preventDefault();
    if (isSafeHttpUrl(url)) shell.openExternal(url);
  });

  mainWindow.on("close", (event) => {
    if (isQuitting) return;
    event.preventDefault();
    const maximized = mainWindow.isMaximized();
    const b = maximized ? mainWindow.getNormalBounds() : mainWindow.getBounds();
    saveState({ bounds: { ...b, maximized }, lastWorkspace: currentWorkspace });
    mainWindow.hide();
    if (!trayTipShown && tray && process.platform === "win32") {
      trayTipShown = true;
      try {
        tray.displayBalloon({
          iconType: "info",
          title: "codelet 仍在后台运行",
          content: "窗口已最小化到系统托盘。点击托盘图标可重新打开，右键可退出。",
        });
      } catch (_) {}
    }
  });

  mainWindow.loadFile(path.join(__dirname, "splash.html"));
}

async function bootBackendAndLoad() {
  const saved = loadState();
  const cliWorkspace = extractWorkspaceArg(process.argv);
  const initialWorkspace = cliWorkspace
    ? cliWorkspace
    : saved.lastWorkspace && fs.existsSync(saved.lastWorkspace)
      ? saved.lastWorkspace
      : app.isPackaged
        ? os.homedir()
        : REPO_ROOT;
  currentWorkspace = initialWorkspace;
  if (cliWorkspace) saveState({ lastWorkspace: cliWorkspace });

  backend = new BackendManager({
    isPackaged: app.isPackaged,
    repoRoot: REPO_ROOT,
    resourcesPath: process.resourcesPath,
  });

  try {
    const secretEnv = vault ? vault.getDecryptedEnv() : {};
    const info = await backend.start({
      workspace: initialWorkspace,
      secretEnv,
      onUnexpectedExit: (code, logs) => {
        if (isQuitting) return;
        dialog.showErrorBox(
          "codelet 后端意外退出",
          `后端进程已退出 (code=${code})。\n\n${logs || "(无日志)"}`
        );
      },
    });
    backendOrigin = info.origin;
    if (mainWindow && !mainWindow.isDestroyed()) {
      await mainWindow.loadURL(info.entryUrl);
    }
  } catch (err) {
    const msg = err && err.message ? err.message : String(err);
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents
        .executeJavaScript(`window.__setSplashError && window.__setSplashError(${JSON.stringify(msg)});`)
        .catch(() => {});
    }
  }
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", (_event, commandLine) => {
    showMainWindow();
    const targetDir = extractWorkspaceArg(commandLine);
    if (targetDir) {
      currentWorkspace = targetDir;
      saveState({ lastWorkspace: targetDir });
      dispatchAction("open-workspace", { path: targetDir });
    }
  });

  app.whenReady().then(async () => {
    vault = new SecretVault(app.getPath("userData"));
    registerIpc();
    createWindow();

    const trayIcon = resolveAsset(process.platform === "win32" ? "icon.ico" : "tray.png");
    tray = createAppTray({
      iconPath: trayIcon,
      onToggleWindow: () => toggleMainWindow(),
      onNewChat: () => dispatchAction("new-chat"),
      onPickWorkspace: () => dispatchAction("pick-workspace"),
      onOpenWorkspaceFolder: () => {
        const target = currentWorkspace && fs.existsSync(currentWorkspace) ? currentWorkspace : os.homedir();
        shell.openPath(target);
      },
      onOpenSettings: () => dispatchAction("open-settings"),
      onCheckUpdates: () => checkForUpdates(app, { manual: true }),
      onQuit: () => quitApp(),
    });

    setupAutoUpdater({
      app,
      getMainWindow: () => mainWindow,
      getTray: () => tray,
      onStatus: (status) => {
        if (mainWindow && !mainWindow.isDestroyed()) {
          mainWindow.webContents.send("desktop:action", { type: "update-status", ...status });
        }
      },
      beforeQuitAndInstall: async () => {
        isQuitting = true;
        if (tray) {
          try { tray.destroy(); } catch (_) {}
          tray = null;
        }
        if (backend) {
          try { await backend.stop(); } catch (_) {}
        }
      },
    });

    await bootBackendAndLoad();
  });

  app.on("before-quit", (event) => {
    if (!isQuitting) {
      event.preventDefault();
      quitApp();
    }
  });

  app.on("window-all-closed", (event) => {
    // Keep alive in system tray until explicit quit.
    if (!isQuitting) {
      event.preventDefault();
    }
  });
}
