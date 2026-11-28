"use strict";

const { dialog, shell } = require("electron");

const RELEASES_URL = "https://github.com/muamuauh/codelet/releases";

let updater = null;
let initialized = false;
let manualCheckPending = false;
let isDownloading = false;
let downloadedVersion = "";
let targetVersion = "";
let getParentWindow = () => null;
let getAppTray = () => null;
let emitStatus = () => {};
let requestAppQuitForInstall = null;

function _loadUpdater() {
  if (updater !== null) return updater;
  try {
    ({ autoUpdater: updater } = require("electron-updater"));
  } catch (_) {
    updater = false;
  }
  return updater;
}

function _setProgress(fraction, tooltipText) {
  const win = getParentWindow();
  if (win && !win.isDestroyed() && typeof win.setProgressBar === "function") {
    try { win.setProgressBar(fraction); } catch (_) {}
  }
  const tr = getAppTray();
  if (tr && !tr.isDestroyed() && typeof tr.setToolTip === "function") {
    try {
      tr.setToolTip(tooltipText || "codelet — Local AI Coding Workbench");
    } catch (_) {}
  }
}

async function installDownloadedUpdate() {
  const au = _loadUpdater();
  if (!au || !downloadedVersion) return { ok: false, reason: "not-downloaded" };
  _setProgress(-1);
  if (typeof requestAppQuitForInstall === "function") {
    await requestAppQuitForInstall();
  }
  au.quitAndInstall(false, true);
  return { ok: true };
}

function setupAutoUpdater({ app, getMainWindow, getTray, onStatus, beforeQuitAndInstall }) {
  getParentWindow = getMainWindow;
  getAppTray = typeof getTray === "function" ? getTray : () => null;
  emitStatus = typeof onStatus === "function" ? onStatus : () => {};
  requestAppQuitForInstall = beforeQuitAndInstall;

  if (!app.isPackaged) return;
  const au = _loadUpdater();
  if (!au || initialized) return;
  initialized = true;

  au.autoDownload = false;
  au.autoInstallOnAppQuit = true;
  au.allowPrerelease = false;

  au.on("update-available", async (info) => {
    manualCheckPending = false;
    const ver = info && info.version ? `v${info.version}` : "新版本";
    targetVersion = ver;
    const win = getParentWindow();
    if (isDownloading) {
      if (win && !win.isDestroyed()) win.show();
      return;
    }
    const res = await dialog.showMessageBox(win || undefined, {
      type: "info",
      title: "发现 codelet 新版本",
      message: `发现新版本 ${ver}（当前 v${app.getVersion()}）`,
      detail: "是否立即下载更新包？下载进度将在应用右下角和任务栏实时显示。",
      buttons: ["立即下载", "打开发布页", "稍后"],
      defaultId: 0,
      cancelId: 2,
    });
    if (res.response === 0) {
      isDownloading = true;
      downloadedVersion = "";
      _setProgress(2, `codelet — 正在下载更新 ${ver}…`);
      emitStatus({
        state: "downloading",
        version: ver,
        percent: 0,
        transferred: 0,
        total: 0,
        bytesPerSecond: 0,
      });
      au.downloadUpdate().catch(async (err) => {
        isDownloading = false;
        const msg = err && err.message ? err.message : String(err);
        _setProgress(-1);
        emitStatus({ state: "error", version: ver, message: msg });
        const r2 = await dialog.showMessageBox(win || undefined, {
          type: "warning",
          title: "自动下载失败",
          message: `更新包下载失败：${msg}`,
          detail: "你可以直接前往 GitHub Releases 页面手动下载最新安装包。",
          buttons: ["打开发布页", "关闭"],
          defaultId: 0,
          cancelId: 1,
        });
        if (r2.response === 0) shell.openExternal(RELEASES_URL);
      });
    } else if (res.response === 1) {
      shell.openExternal(RELEASES_URL);
    }
  });

  au.on("download-progress", (p) => {
    const pct = Math.max(0, Math.min(100, Number(p && p.percent ? p.percent : 0)));
    const ver = targetVersion || "新版本";
    _setProgress(pct / 100, `codelet — 正在下载更新 ${ver} (${Math.round(pct)}%)`);
    emitStatus({
      state: "downloading",
      version: ver,
      percent: pct,
      transferred: Number((p && p.transferred) || 0),
      total: Number((p && p.total) || 0),
      bytesPerSecond: Number((p && p.bytesPerSecond) || 0),
    });
  });

  au.on("update-not-available", async () => {
    if (!manualCheckPending) return;
    manualCheckPending = false;
    const win = getParentWindow();
    await dialog.showMessageBox(win || undefined, {
      type: "info",
      title: "检查更新",
      message: `当前已是最新版本 (v${app.getVersion()})`,
      buttons: ["确定"],
    });
  });

  au.on("update-downloaded", async (info) => {
    isDownloading = false;
    const ver = info && info.version ? `v${info.version}` : (targetVersion || "新版本");
    downloadedVersion = ver;
    _setProgress(-1, `codelet — ${ver} 已下载完成，等待重启安装`);
    emitStatus({ state: "downloaded", version: ver, percent: 100 });
    const win = getParentWindow();
    const res = await dialog.showMessageBox(win || undefined, {
      type: "info",
      title: "更新已就绪",
      message: `codelet ${ver} 已下载完成`,
      detail: "是否立即退出并安装更新？你也可以稍后点击右下角提示或在退出应用时自动完成安装。",
      buttons: ["立即重启安装", "稍后"],
      defaultId: 0,
      cancelId: 1,
    });
    if (res.response === 0) {
      await installDownloadedUpdate();
    }
  });

  au.on("error", async (err) => {
    const wasDownloading = isDownloading;
    isDownloading = false;
    _setProgress(-1);
    const msg = err && err.message ? err.message : String(err);
    if (wasDownloading) {
      emitStatus({ state: "error", version: targetVersion, message: msg });
    }
    if (!manualCheckPending) return;
    manualCheckPending = false;
    const win = getParentWindow();
    const res = await dialog.showMessageBox(win || undefined, {
      type: "warning",
      title: "检查更新失败",
      message: "暂时无法连接到 GitHub Releases 检查更新。",
      detail: msg,
      buttons: ["打开 GitHub Releases", "关闭"],
      defaultId: 0,
      cancelId: 1,
    });
    if (res.response === 0) shell.openExternal(RELEASES_URL);
  });

  // Silent background check shortly after launch so cold startup stays fast.
  setTimeout(() => {
    au.checkForUpdates().catch(() => {});
  }, 8000);
}

async function checkForUpdates(app, { manual = true } = {}) {
  const win = getParentWindow();
  if (!app.isPackaged) {
    if (manual) {
      const res = await dialog.showMessageBox(win || undefined, {
        type: "info",
        title: "检查更新",
        message: "当前处于开发模式，自动更新仅在打包安装版生效。",
        buttons: ["打开 GitHub Releases", "关闭"],
        defaultId: 1,
        cancelId: 1,
      });
      if (res.response === 0) shell.openExternal(RELEASES_URL);
    }
    return { ok: false, reason: "dev" };
  }
  const au = _loadUpdater();
  if (!au) {
    if (manual) shell.openExternal(RELEASES_URL);
    return { ok: false, reason: "unavailable" };
  }
  manualCheckPending = Boolean(manual);
  try {
    await au.checkForUpdates();
    return { ok: true };
  } catch (err) {
    if (manualCheckPending) {
      manualCheckPending = false;
      const res = await dialog.showMessageBox(win || undefined, {
        type: "warning",
        title: "检查更新失败",
        message: err && err.message ? err.message : String(err),
        buttons: ["打开 GitHub Releases", "关闭"],
        defaultId: 0,
        cancelId: 1,
      });
      if (res.response === 0) shell.openExternal(RELEASES_URL);
    }
    return { ok: false, error: err && err.message ? err.message : String(err) };
  }
}

module.exports = { setupAutoUpdater, checkForUpdates, installDownloadedUpdate };
