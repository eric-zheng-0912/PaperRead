// Explicit coexistence test channel. Never reuse this identity for custom-upgrade releases.
const base = require('../package.json').build;
module.exports = {
  ...base,
  appId: 'io.github.eric-zheng-0912.paperread.custom.test',
  productName: 'PaperRead Custom Test',
  executableName: 'PaperRead-Custom-Test',
  extraMetadata: { ...base.extraMetadata, name: 'paperread-custom-test', paperreadChannel: 'isolated-test' },
  protocols: [],
  publish: null,
  directories: { ...base.directories, output: 'dist/electron-test' },
  nsis: { ...base.nsis, include: 'electron/installer-test.nsh', shortcutName: 'PaperRead Custom Test',
    artifactName: 'PaperRead-Test-Setup-${version}.exe' },
};
