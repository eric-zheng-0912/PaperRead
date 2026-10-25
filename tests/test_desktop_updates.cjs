const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { registerUpdates } = require("../electron/desktop-updates.cjs");

function fixture() {
  const handlers = {};
  const updater = new EventEmitter();
  const calls = [];
  const feeds = [];
  updater.setFeedURL = value => feeds.push(value);
  updater.checkForUpdates = async () => ({ isUpdateAvailable: true, updateInfo: { version: "1.3.2" } });
  updater.downloadUpdate = async () => { calls.push("download"); updater.emit("update-downloaded", { version: "1.3.2" }); };
  updater.quitAndInstall = (...args) => calls.push(["install", ...args]);
  registerUpdates({
    app: { isPackaged: true }, updater,
    ipcMain: { handle: (name, fn) => { handlers[name] = fn; } },
    trustedWindow: event => { if (event !== "trusted") throw Error("Untrusted"); },
    repository: "example/research-os",
    getWindow: () => null,
    prepareInstall: async () => { calls.push("shutdown"); },
    recover: async () => { calls.push("recover"); },
  });
  return { updater, calls, feeds, run: (name, event = "trusted") => handlers[`easyread:update-${name}`](event) };
}

test("all repository selections stay manual-only with no feed or install", async () => {
  const f = fixture();
  f.updater.checkForUpdates = () => assert.fail('Network check forbidden');
  assert.equal(f.updater.autoDownload, false);
  assert.equal(f.updater.autoInstallOnAppQuit, false);
  assert.equal((await f.run('state')).supported, false);
  await f.run('download'); await f.run('install');
  assert.deepEqual(f.calls, []); assert.deepEqual(f.feeds, []);
});

test("untrusted renderer cannot invoke update operations", async () => {
  const f = fixture();
  assert.throws(() => f.run("state", "foreign"), /Untrusted/);
  await assert.rejects(f.run("download", "foreign"), /Untrusted/);
  await assert.rejects(f.run("install", "foreign"), /Untrusted/);
});

test("concurrent download attempts remain disabled", { skip: process.platform !== "win32" }, async () => {
  const f = fixture();
  await Promise.all([f.run("download"), f.run("download")]);
  assert.deepEqual(f.calls, []);
});
