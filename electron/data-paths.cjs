const path = require('node:path');
function desktopPaths({platform, env, home, appData, installation, fs, testChannel = false}) {
  const name = testChannel ? 'PaperRead-Custom-Test' : 'PaperRead-Custom';
  const root = platform === 'win32' ? path.join(env.LOCALAPPDATA || path.join(home,'AppData','Local'),name)
    : platform === 'darwin' ? path.join(home,'Library','Application Support',name)
    : path.join(env.XDG_DATA_HOME || path.join(home,'.local','share'),testChannel ? 'paperread-custom-test' : 'paperread-custom');
  const dataHome = testChannel ? path.join(root,'home') : env.EASYREAD_HOME || path.join(root,'home');
  const userData = path.join(root,'desktop');
  function canonical(value) {
    let parent=path.resolve(value); const suffix=[];
    while (!fs.existsSync(parent) && path.dirname(parent)!==parent) { suffix.unshift(path.basename(parent)); parent=path.dirname(parent); }
    return path.resolve(fs.realpathSync ? fs.realpathSync(parent) : parent,...suffix);
  }
  function outside(value) {
    if (!installation) return;
    const a=canonical(value), b=canonical(installation);
    const inside=(x,y)=> { const rel=path.relative(x,y); return !rel || (!rel.startsWith('..'+path.sep) && rel!=='..' && !path.isAbsolute(rel)); };
    if (inside(a,b) || inside(b,a)) throw Error('User data and installation directories must not overlap');
  }
  outside(dataHome); outside(userData);
  const receipt=path.join(path.dirname(dataHome),'migration.json');
  const phase=fs.existsSync(receipt) ? JSON.parse(fs.readFileSync(receipt,'utf8')).phase : null;
  if (phase && phase!=='complete') throw Error('Data migration incomplete; resume before opening the destination profile');
  if (!testChannel && !env.EASYREAD_HOME && phase!=='complete') {
    const old=[path.join(home,'PaperRead'),path.join(home,'EasyRead')];
    const profiles=appData ? [path.join(appData,'PaperRead'),path.join(appData,'EasyRead')] : [];
    if (old.some(p=>fs.existsSync(path.join(p,'config.json')) || fs.existsSync(path.join(p,'library'))) ||
        profiles.some(p=>['Preferences','window-state.json','Local State'].some(name=>fs.existsSync(path.join(p,name)))))
      throw Error('Legacy data found. Close PaperRead and run migrate-data with a verified backup before switching. The old version and source data are unchanged.');
  }
  return {userData, env:{EASYREAD_HOME:dataHome,
    ...(testChannel ? {EASYREAD_LIBRARY:path.join(root,'library'), COREAD_LIBRARY:path.join(root,'library')} : {}),
    ...(installation?{PAPERREAD_INSTALL_DIR:installation}: {})}};
}
module.exports={desktopPaths};
