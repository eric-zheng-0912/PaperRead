const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
function fixture(){
  const fields={'#doclingPython':{value:''},'#doclingEnabled':{checked:true}};
  let calls=0;
  const PR={settingsTabs:{},esc:s=>String(s).replaceAll('<','&lt;').replaceAll('"','&quot;'),$:k=>fields[k],toast:()=>{},
    api:async()=>{calls++;return {status:'basic_import',candidates:[{python:'Z:/Conda/envs/science/python.exe',status:'basic_import',docling:'2.126.0'}]};}};
  const window={PR,easyreadDesktop:{pickPython:async()=> 'Y:/Other/env/python.exe'}};
  vm.runInNewContext(fs.readFileSync('easyread/web/js/common/settings-docling.js','utf8'),{window});
  return {tab:PR.settingsTabs.docling,fields,s:{cfg:{}},calls:()=>calls};
}
const event=act=>({target:{closest:()=>({dataset:{docling:act,index:'0'}})}});
test('opening settings only renders; no inference or discovery side effect',()=>{
  const f=fixture();assert.match(f.tab.render(f.s),/尚未驗證/);assert.equal(f.calls(),0);
});
test('manual Python picker changes local settings; save boundary is separate',async()=>{
  const f=fixture();await f.tab.click(event('pick'),f.s);assert.equal(f.s.cfg.docling_python,'Y:/Other/env/python.exe');assert.equal(f.calls(),0);
});
test('redetect exposes candidates and explicit selection survives sync',async()=>{
  const f=fixture();await f.tab.click(event('detect'),f.s);assert.equal(f.calls(),1);
  assert.match(f.tab.render(f.s),/基本匯入成功/);assert.match(f.tab.render(f.s),/未驗證推論/);
  await f.tab.click(event('use'),f.s);assert.equal(f.s.cfg.docling_python,'Z:/Conda/envs/science/python.exe');
});
test('disable fallback and escaped path use existing config state',()=>{
  const f=fixture();f.fields['#doclingPython'].value='X:/a/python.exe';f.fields['#doclingEnabled'].checked=false;
  f.tab.sync(f.s);assert.equal(f.s.cfg.docling_enabled,false);assert.equal(f.s.cfg.docling_python,'X:/a/python.exe');
  f.s.cfg.docling_python='<bad>';assert.match(f.tab.render(f.s),/&lt;bad>/);
});
