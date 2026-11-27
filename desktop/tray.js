"use strict";

const { Menu, Tray, nativeImage } = require("electron");

function createAppTray({
  iconPath,
  onToggleWindow,
  onNewChat,
  onPickWorkspace,
  onOpenWorkspaceFolder,
  onOpenSettings,
  onCheckUpdates,
  onQuit,
}) {
  let img = nativeImage.createFromPath(iconPath);
  if (!img.isEmpty() && process.platform === "win32") {
    img = img.resize({ width: 16, height: 16, quality: "best" });
  }
  const tray = new Tray(img);
  tray.setToolTip("codelet — Local AI Coding Workbench");

  const menu = Menu.buildFromTemplate([
    {
      label: "显示 / 隐藏 codelet",
      click: () => onToggleWindow(),
    },
    { type: "separator" },
    {
      label: "新建对话",
      click: () => onNewChat(),
    },
    {
      label: "切换工作区…",
      click: () => onPickWorkspace(),
    },
    {
      label: "打开工作区目录",
      click: () => onOpenWorkspaceFolder(),
    },
    {
      label: "API 与模型设置…",
      click: () => onOpenSettings && onOpenSettings(),
    },
    {
      label: "检查更新…",
      click: () => onCheckUpdates && onCheckUpdates(),
    },
    { type: "separator" },
    {
      label: "退出 codelet",
      click: () => onQuit(),
    },
  ]);

  tray.setContextMenu(menu);
  tray.on("click", () => onToggleWindow());
  tray.on("double-click", () => onToggleWindow());

  return tray;
}

module.exports = { createAppTray };
