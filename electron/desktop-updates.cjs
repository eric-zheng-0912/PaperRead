// Windows NSIS updates. The renderer never supplies a feed URL or installer path.
function registerUpdates({ app, ipcMain, updater, trustedWindow, getWindow, prepareInstall, recover, repository = "" }) {
  // Custom/private distribution: no feed, auth token, network download or installer execution.
  // Runtime environment variables must not re-enable upstream or private auto-updates.
  const supported = false;
  updater.autoDownload = false;
  updater.autoInstallOnAppQuit = false;
  let state = { supported, phase: "idle", version: "", percent: 0, error: "" };
  ipcMain.handle("easyread:update-state", event => { trustedWindow(event); return state; });
  for (const operation of ['download','install']) {
    ipcMain.handle('easyread:update-'+operation, async event => { trustedWindow(event); return state; });
  }
}

module.exports = { registerUpdates };
