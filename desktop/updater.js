"use strict";

const { dialog, shell } = require("electron");

const RELEASES_URL = "https://github.com/muamuauh/codelet/releases";

let updater = null;
let initialized = false;
let manualCheckPending = false;
let getParentWindow = () => null;
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

function setupAutoUpdater({ app, getMainWindow, beforeQuitAndInstall }) {
  getParentWindow = getMainWindow;
  requestAppQuitForInstall = beforeQuitAndInstall;

  if (!app.isPackaged) return;
  const au = _loadUpdater();
  if (!au || initialized) return;
  initialized = true;

  au.autoDownload = false;
  au.autoInstallOnAppQuit = true;
  au.allowPrerelease = false;

  au.on("update-available", async (info) => {
    const isManual = manualCheckPending;
    manualCheckPending = false;
    const ver = info && info.version ? `v${info.version}` : "新版本";
    const win = getParentWindow();
    const res = await dialog.showMessageBox(win || undefined, {
      type: "info",
      title: "发现 codelet 新版本",
      message: `发现新版本 ${ver}（当前 v${app.getVersion()}）`,
      detail: "是否立即下载更新包？下载完成后可一键重启安装。",
      buttons: ["立即下载", "打开发布页", "稍后"],
      defaultId: 0,
      cancelId: 2,
    });
    if (res.response === 0) {
      au.downloadUpdate().catch(async (err) => {
        if (isManual || true) {
          const r2 = await dialog.showMessageBox(win || undefined, {
            type: "warning",
            title: "自动下载失败",
            message: `更新包下载失败：${err && err.message ? err.message : String(err)}`,
            detail: "你可以直接前往 GitHub Releases 页面手动下载最新安装包。",
            buttons: ["打开发布页", "关闭"],
            defaultId: 0,
            cancelId: 1,
          });
          if (r2.response === 0) shell.openExternal(RELEASES_URL);
        }
      });
    } else if (res.response === 1) {
      shell.openExternal(RELEASES_URL);
    }
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
    const ver = info && info.version ? `v${info.version}` : "新版本";
    const win = getParentWindow();
    const res = await dialog.showMessageBox(win || undefined, {
      type: "info",
      title: "更新已就绪",
      message: `codelet ${ver} 已下载完成`,
      detail: "是否立即退出并安装更新？你也可以稍后在退出应用时自动完成安装。",
      buttons: ["立即重启安装", "稍后"],
      defaultId: 0,
      cancelId: 1,
    });
    if (res.response === 0) {
      if (typeof requestAppQuitForInstall === "function") {
        await requestAppQuitForInstall();
      }
      au.quitAndInstall(false, true);
    }
  });

  au.on("error", async (err) => {
    if (!manualCheckPending) return;
    manualCheckPending = false;
    const win = getParentWindow();
    const res = await dialog.showMessageBox(win || undefined, {
      type: "warning",
      title: "检查更新失败",
      message: "暂时无法连接到 GitHub Releases 检查更新。",
      detail: err && err.message ? err.message : String(err),
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

module.exports = { setupAutoUpdater, checkForUpdates };
