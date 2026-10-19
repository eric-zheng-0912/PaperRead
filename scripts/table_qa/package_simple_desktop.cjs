// Build-only adapter: use electron-builder's bundled filesystem collector when
// npm is absent from this isolated runtime. No node_modules files are patched.
const { build, Platform, Arch, Packager } = require('electron-builder');
const { PM } = require('app-builder-lib/out/node-module-collector');
Packager.prototype.getPackageManager = async function () { return PM.TRAVERSAL; };
build({config:process.argv[2],targets:Platform.WINDOWS.createTarget(['dir'],Arch.x64),publish:'never'})
  .catch(error=>{console.error(error);process.exitCode=1;});
