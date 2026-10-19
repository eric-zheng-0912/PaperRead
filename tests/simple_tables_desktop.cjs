// Unpacked Electron + packaged Python service + real NIST PDF, Mock-only build.
const { _electron } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const repo = path.resolve(__dirname,'..');
const hash = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const read = file => JSON.parse(fs.readFileSync(file,'utf8'));
const wait = ms => new Promise(r=>setTimeout(r,ms));
(async()=>{
  const build=read(path.join(repo,'tmp/simple-delivery-build.json'));
  const fixture=read(path.join(repo,'tmp/simple-delivery-fixture.json'));
  assert.ok(path.resolve(build.executable).startsWith(path.join(repo,'dist/table-review')+path.sep));
  assert.ok(path.resolve(fixture.profile).startsWith(path.join(repo,'.paperread-local-test')+path.sep));
  let app;
  const errors=[];
  const checks=[];
  try {
    const environment={...process.env,PAPERREAD_TEST_PROFILE:fixture.profile,PAPERREAD_REVIEW_MODE:'Mock',
      PAPERREAD_DISABLE_TABLE_EXTRACTION:'1',PAPERREAD_OFFLINE_TEST:'1'};
    delete environment.ELECTRON_RUN_AS_NODE;
    const launch=()=>_electron.launch({executablePath:build.executable,timeout:60000,
      args:['--disable-gpu','--no-sandbox'],env:environment});
    app=await launch();
    let page=await app.firstWindow();
    await page.waitForURL(/http:\/\/127\.0\.0\.1:\d+\//,{timeout:60000});
    let origin=new URL(page.url()).origin;
    await page.setViewportSize({width:1440,height:1000});
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(origin+'/read/'+fixture.pid);
    await page.waitForFunction(()=>window.PR?.state.paper?.blocks?.length);
    const policies=await page.evaluate(()=>PR.api('/api/p/'+PR.pid+'/simple-table',{method:'POST',body:{action:'status'}}));
    assert.equal(policies.saved.length,0);
    // The frozen test runtime hook enforces exact parsing off. The API must refuse it.
    await page.evaluate(async()=>{
      const result=await PR.api('/api/p/'+PR.pid+'/translate',{method:'POST',body:{tables_only:true,pages:'1'}});
      return result;
    });
    for(let n=0;n<80;n++) {
      const state=await page.evaluate(()=>PR.api('/api/p/'+PR.pid+'/state'));
      if(['error','done','partial'].includes(state.job?.state)) {
        assert.equal(state.job.state,'error'); assert.match(state.job.error||state.job.message||'',/disabled/); break;
      }
      if(n===79) throw new Error('Exact-disabled check timed out');
      await wait(200);
    }
    checks.push('exact parser disabled; simple entry available');
    await page.evaluate(()=>PR.openPage(2));
    await page.locator('#simple-table-open').click();
    let panel=page.locator('#simple-table-panel');
    await panel.getByRole('button',{name:'追加候選 1',exact:true}).click();
    await panel.getByRole('button',{name:'預覽英文',exact:true}).click();
    await panel.getByRole('heading',{name:'英文原文',exact:true}).waitFor();
    assert.match(await panel.innerText(),/2015/);
    checks.push('real NIST automatic candidate and English preview');
    await page.evaluate(()=>PR.openPage(1));
    const pdf=page.locator('.pv-page[data-n="1"]');
    await pdf.scrollIntoViewIfNeeded();
    await page.waitForFunction(()=>{const p=document.querySelector('.pv-page[data-n="1"] img');return p?.complete&&p.naturalWidth>0;});
    await panel.getByRole('button',{name:'手動框選／追加',exact:true}).click();
    const area=await pdf.boundingBox(), r=fixture.regions[1].box;
    await page.mouse.move(area.x+area.width*(r[0]-.002),area.y+area.height*(r[1]-.002));
    await page.mouse.down();
    await page.mouse.move(area.x+area.width*(r[2]+.002),area.y+area.height*(r[3]+.002),{steps:12});
    await page.mouse.up();
    await panel.getByText('範圍 2 · PDF p.1',{exact:false}).waitFor();
    await panel.getByRole('button',{name:'手動框選／追加',exact:true}).click();
    await panel.getByRole('button',{name:'預覽英文',exact:true}).click();
    await panel.getByRole('heading',{name:'英文原文',exact:true}).nth(1).waitFor();
    checks.push('manual pointer selection and cross-page append');
    await panel.getByRole('button',{name:'翻譯為繁體中文',exact:true}).click();
    await panel.getByText('譯文已儲存；僅涵蓋所選範圍。',{exact:true}).waitFor({timeout:30000});
    assert.match(await panel.innerText(),/模擬繁體中文譯文/);
    await panel.getByRole('button',{name:'翻譯為繁體中文',exact:true}).click();
    await panel.getByText('譯文已儲存；僅涵蓋所選範圍。',{exact:true}).waitFor({timeout:30000});
    let calls=fs.readFileSync(path.join(fixture.profile,'model-calls.jsonl'),'utf8').trim().split('\n').map(JSON.parse);
    assert.equal(calls.filter(c=>c.kind==='simple').length,1);
    checks.push('Traditional Chinese plain text and independent cache');
    await page.screenshot({path:path.join(fixture.profile,'desktop-translation.png')});
    await panel.getByRole('button',{name:'關閉',exact:true}).click();
    for(const [view,order] of [['original'],['translation'],['both','original'],['both','translation']]) {
      const text=await page.evaluate(([v,o])=>{
        if(o) PR.setBiOrder(o); else PR.setView(v);
        return document.getElementById('b-old-body').innerText;
      },[view,order]);
      const en=text.indexOf('Existing English body.'), zh=text.indexOf('既有正文譯文。');
      if(view==='original') assert.ok(en>=0&&zh<0,text);
      else if(view==='translation') assert.ok(zh>=0&&en<0,text);
      else assert.ok(en>=0&&zh>=0&&(order==='original'?en<zh:zh<en),text);
    }
    checks.push('all four reading modes on legacy body');
    await page.evaluate(()=>PR.startNote({anchor:'old-body'}));
    await page.locator('.card textarea').last().fill('桌面隔離驗收筆記');
    await page.evaluate(async()=>{PR.closeNoteEditor();await PR.flushPending();});
    checks.push('create and persist a note while retaining existing note');
    await page.evaluate(()=>PR.api('/api/p/'+PR.pid+'/translate',{method:'POST',body:{en:true,pages:'2'}}));
    for(let n=0;n<100;n++) {
      const state=await page.evaluate(()=>PR.api('/api/p/'+PR.pid+'/state'));
      if(state.job?.state==='done') break;
      if(['error','partial'].includes(state.job?.state)) throw new Error(JSON.stringify(state.job));
      if(n===99) throw new Error('Body translation timed out');
      await wait(200);
    }
    assert.ok(read(fixture.paper).blocks.find(b=>b.id==='delivery-body').zh);
    checks.push('ordinary full-text translation job completes with exact parsing off');
    await app.close(); app=await launch();
    page=await app.firstWindow();
    await page.waitForURL(/http:\/\/127\.0\.0\.1:\d+\//,{timeout:60000});
    origin=new URL(page.url()).origin;
    await page.goto(origin+'/read/'+fixture.pid);
    await page.waitForFunction(()=>window.PR?.state.paper?.blocks?.length);
    await page.evaluate(()=>PR.openPage(2));
    await page.locator('#simple-table-open').click();
    panel=page.locator('#simple-table-panel');
    await panel.getByRole('button',{name:/^譯文 1/}).click();
    await panel.getByRole('heading',{name:'繁體中文譯文',exact:true}).nth(1).waitFor();
    checks.push('actual desktop close/relaunch restores saved translation');
    const old=read(path.join(fixture.profile,'initial-paper.json')), current=read(fixture.paper);
    for(const block of old.blocks.filter(b=>b.id!=='delivery-body')) {
      assert.deepEqual(current.blocks.find(b=>b.id===block.id),block);
    }
    const reader=read(path.join(path.dirname(fixture.paper),'reader.json'));
    const originalReader=read(path.join(fixture.profile,'initial-reader.json'));
    assert.deepEqual(reader.notes['delivery-note'],originalReader.notes['delivery-note']);
    assert.ok(Object.values(reader.notes).some(n=>n.body==='桌面隔離驗收筆記'));
    assert.equal(hash(path.join(path.dirname(fixture.paper),'source.pdf')),fixture.source_sha256);
    assert.equal(hash(path.join(fixture.profile,'home/config.json')),fixture.config_sha256);
    calls=fs.readFileSync(path.join(fixture.profile,'model-calls.jsonl'),'utf8').trim().split('\n').map(JSON.parse);
    assert.equal(calls.filter(c=>c.kind==='simple').length,1);
    assert.equal(calls.filter(c=>c.kind==='body').length,1);
    assert.deepEqual(errors,[]);
    checks.push('PDF, configuration, legacy blocks and notes retained; no real inference');
    const report={passed:true,executable:build.executable,profile:fixture.profile,checks};
    fs.writeFileSync(path.join(fixture.profile,'desktop-report.json'),JSON.stringify(report,null,2));
    fs.writeFileSync(path.join(repo,'tmp/simple-delivery-desktop-report.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify(report));
  } finally { if(app) await app.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
