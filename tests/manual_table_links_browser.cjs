// Real local reader and Chromium. Synthetic PDF, fixture-only mock inference.
const { chromium } = require('playwright');
const { spawn } = require('node:child_process');
const { createInterface } = require('node:readline');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const repo = path.resolve(__dirname, '..');
(async () => {
  const child = spawn(path.join(repo,'.venv/Scripts/python.exe'),['-m','scripts.table_qa.simple_review'],
    {cwd:repo,stdio:['ignore','pipe','pipe'],windowsHide:true,
      env:{...process.env,PAPERREAD_MANUAL_LINKS_FIXTURE:'1'}});
  let browser, stderr=''; child.stderr.on('data',d=>stderr+=d);
  const checks=[];
  try {
    const fixture=await new Promise((resolve,reject)=>{
      const lines=createInterface({input:child.stdout});
      const timer=setTimeout(()=>reject(new Error('Mock fixture timeout '+stderr)),30000);
      lines.once('line',line=>{clearTimeout(timer);try{resolve(JSON.parse(line));}catch(e){reject(e);}});
      child.once('exit',code=>{clearTimeout(timer);reject(new Error('Fixture exited '+code+stderr));});
    });
    browser=await chromium.launch({channel:'chrome',headless:true});
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[]; page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/*',r=>new URL(r.request().url()).origin===fixture.origin?r.continue():r.abort());
    await page.goto(fixture.origin+'/read/'+fixture.pid);
    await page.waitForFunction(()=>window.PR?.state.paper?.blocks?.length===4);
    await page.evaluate(async()=>{
      const api=body=>PR.api('/api/p/'+PR.pid+'/simple-table',{method:'POST',body});
      const {candidates}=await api({action:'candidates',page:1});
      const preview=await api({action:'preview',regions:[candidates[0]]});
      await api({action:'translate',regions:preview.source.regions,source_hash:preview.source_hash});
    });
    await page.waitForFunction(async()=>{
      const s=await PR.api('/api/p/'+PR.pid+'/simple-table',{method:'POST',body:{action:'status'}});
      return s.saved.length===1 && s.jobs.every(j=>j.state==='done');
    });
    await page.evaluate(()=>PR.openPage(1,'tab1'));
    await page.locator('#simple-table-open').click();
    const panel=page.locator('#simple-table-panel');
    await panel.getByRole('button',{name:/^譯文 1/}).click();
    await panel.getByRole('button',{name:'加入譯文',exact:true}).click();
    assert.equal(await page.locator('#paper .manual-table').count(),0);
    assert.equal(await panel.getByRole('button',{name:'確認加入此位置',exact:true}).count(),0);
    await panel.getByRole('button',{name:/^佔位區 1/}).click();
    assert.equal(await page.locator('#paper .manual-table').count(),0);
    await panel.getByRole('button',{name:'確認加入此位置',exact:true}).click();
    await page.evaluate(()=>PR.setView('translation'));
    await page.locator('#b-tab1 .manual-table-translation img').waitFor();
    assert.equal(await page.locator('#b-tab2 .manual-table').count(),0);
    assert.equal(await page.evaluate(()=>document.querySelector('#b-tab1').nextElementSibling.id),'b-body-after');
    checks.push('explicit two-step confirmation; no same-page auto match; rendered inside original block');
    await panel.getByRole('button',{name:'關閉',exact:true}).click();
    const original=page.locator('#b-tab1 .manual-table-original');
    const translated=page.locator('#b-tab1 .manual-table-translation');
    await page.evaluate(()=>PR.setView('translation'));
    await translated.waitFor({state:'visible'}); assert.equal(await original.isVisible(),false);
    assert.match(await translated.innerText(),/阿爾法 -1.25 0.04/);
    assert.match(await translated.innerText(),/來源 PDF p.1/);
    await page.screenshot({path:path.join(fixture.profile,'manual-linked-translation.png'),fullPage:true});
    await page.evaluate(()=>PR.setView('original'));
    await original.waitFor({state:'visible'}); assert.equal(await translated.isVisible(),false);
    try { await original.locator('.manual-table-view').click({timeout:5000}); }
    catch(error) {
      fs.writeFileSync(path.join(fixture.profile,'manual-debug.json'),JSON.stringify(await page.evaluate(()=>({
        block:document.querySelector('#b-tab1')?.outerHTML,
        buttons:[...document.querySelectorAll('#b-tab1 button')].map(n=>({text:n.textContent,hidden:n.hidden,display:getComputedStyle(n).display}))
      })),null,2));
      await page.screenshot({path:path.join(fixture.profile,'manual-debug.png')}); throw error;
    }
    assert.equal(await translated.isVisible(),true);
    for (const order of ['original','translation']) {
      await page.evaluate(order=>{PR.setView('both');PR.setBiOrder(order);},order);
      assert.equal(await original.isVisible(),true);assert.equal(await translated.isVisible(),true);
      assert.equal(await page.locator('#b-tab1 .manual-table > div').first().getAttribute('class'),
        order==='original'?'manual-table-original':'manual-table-translation');
    }
    checks.push('translation/source image and page; original preserved with optional entry; both bilingual orders');
    await page.reload();
    await page.locator('#b-tab1 .manual-table-translation img').waitFor({state:'attached'});
    assert.equal(JSON.parse(fs.readFileSync(path.join(fixture.profile,'mock-calls.json'),'utf8')),1);
    checks.push('reopen persisted snapshot without inference');
    // Model-shaped HTML stays a literal string even in the inline linked view.
    await page.evaluate(()=>{
      PR.blockById.tab1.manual_table_view.values[0].zh='<img src=x onerror=alert(1)>翻譯';
      PR.renderBlock('tab1'); PR.applyReadingLanguage();
    });
    assert.match(await translated.textContent(),/<img src=x onerror=alert\(1\)>翻譯/);
    assert.equal(await translated.locator('[onerror]').count(),0);
    checks.push('model text rendered with textContent, not HTML');
    await page.reload();
    await page.locator('#b-tab1 .manual-table').waitFor();
    await page.evaluate(()=>PR.setView('translation'));
    await translated.getByRole('button',{name:'重新選框／翻譯',exact:true}).click();
    await panel.getByRole('button',{name:'預覽英文',exact:true}).click();
    await panel.getByRole('button',{name:'翻譯為繁體中文',exact:true}).click();
    await panel.getByText('譯文已儲存；僅涵蓋所選範圍。',{exact:true}).waitFor();
    await panel.getByRole('button',{name:'重新翻譯（重新呼叫模型）',exact:true}).click();
    await page.waitForFunction(async()=>{
      const s=await PR.api('/api/p/'+PR.pid+'/simple-table',{method:'POST',body:{action:'status'}});
      return s.jobs.filter(j=>j.refresh).some(j=>j.state==='done');
    });
    const onDisk=JSON.parse(fs.readFileSync(path.join(path.dirname(fixture.paper),'manual_table_links.json'),'utf8'));
    assert.equal(onDisk.revision,1,'retranslation alone does not replace confirmed link');
    await panel.getByRole('button',{name:'關閉',exact:true}).click();
    page.once('dialog',d=>d.accept());
    await translated.getByRole('button',{name:'解除關聯',exact:true}).click();
    await page.locator('#b-tab1 .manual-table').waitFor({state:'detached'});
    const initial=JSON.parse(fs.readFileSync(path.join(fixture.profile,'initial-paper.json'),'utf8'));
    const current=JSON.parse(fs.readFileSync(fixture.paper,'utf8'));
    assert.equal(current.simple_table_translations.length,1);
    delete current.simple_table_translations;assert.deepEqual(current,initial);
    const reader=JSON.parse(fs.readFileSync(path.join(path.dirname(fixture.paper),'reader.json'),'utf8'));
    const before=JSON.parse(fs.readFileSync(path.join(fixture.profile,'initial-reader.json'),'utf8'));
    assert.deepEqual(reader.notes,before.notes);assert.deepEqual(reader.edits,before.edits);
    assert.deepEqual(errors,[]);
    checks.push('reselect/retranslate retains confirmed snapshot; unlink retains saved translation, original paper and notes');
    await page.screenshot({path:path.join(fixture.profile,'manual-links-result.png')});
    fs.writeFileSync(path.join(fixture.profile,'manual-links-report.json'),JSON.stringify({passed:true,checks},null,2));
    console.log(JSON.stringify({passed:true,checks,profile:fixture.profile}));
  } finally { if(browser)await browser.close(); child.kill(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
