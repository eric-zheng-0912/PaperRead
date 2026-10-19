// Real Chromium + real localhost reader/API/jobs. Inference is a fixture-only Mock.
// NODE_PATH may point at an existing Playwright installation; no downloads.
const { chromium } = require('playwright');
const { spawn } = require('node:child_process');
const { createInterface } = require('node:readline');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const repo = path.resolve(__dirname, '..');
(async () => {
  const child = spawn(path.join(repo,'.venv/Scripts/python.exe'),['-m','scripts.table_qa.simple_review'],{cwd:repo,stdio:['ignore','pipe','pipe'],windowsHide:true});
  let stderr=''; child.stderr.on('data', d => { stderr+=d; });
  let browser;
  try {
    const fixture = await new Promise((resolve,reject) => {
      const lines=createInterface({input:child.stdout});
      const timer=setTimeout(()=>reject(new Error('Mock server timeout '+stderr)),30000);
      lines.once('line',line=>{clearTimeout(timer);try{resolve(JSON.parse(line));}catch(e){reject(new Error(line+stderr));}});
      child.once('exit',code=>{clearTimeout(timer);reject(new Error('Mock server exit '+code+stderr));});
    });
    browser=await chromium.launch({channel:'chrome',headless:true});
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[];
    page.on('pageerror', e=>errors.push(e.message));
    // Browser cannot contact external services either.
    await page.route('**/*', route => new URL(route.request().url()).origin===fixture.origin ? route.continue() : route.abort());
    await page.goto(fixture.origin+'/read/'+fixture.pid);
    await page.waitForFunction(()=>window.PR?.state.paper?.blocks?.length);
    const denied=await page.request.post(fixture.origin+'/api/p/'+fixture.pid+'/simple-table',{
      data:{action:'translate',regions:[]},headers:{'X-Token':'invalid'}});
    assert.equal(denied.status(),403,'existing API CSRF token is enforced');
    await page.evaluate(()=>PR.openPage(1,'tab1'));
    await page.locator('#simple-table-open').click();
    const panel=page.locator('#simple-table-panel');
    await panel.getByRole('button',{name:'追加候選 1',exact:true}).click();
    await panel.getByRole('button',{name:'預覽英文',exact:true}).click();
    await panel.getByRole('heading',{name:'英文原文',exact:true}).waitFor();
    assert.ok((await panel.innerText()).includes('Alpha -1.25 0.04'));
    // Append page two by an actual pointer drag over the displayed PDF.
    await page.evaluate(()=>PR.openPage(2));
    const pdf=page.locator('.pv-page[data-n="2"]');
    await pdf.scrollIntoViewIfNeeded();
    await page.waitForFunction(()=>{const n=document.querySelector('.pv-page[data-n="2"] img');return n?.complete&&n.naturalWidth>0;});
    await panel.getByRole('button',{name:'手動框選／追加',exact:true}).click();
    const box=await pdf.boundingBox();
    await page.mouse.move(box.x+box.width*.05,box.y+box.height*.10);
    await page.mouse.down();
    await page.mouse.move(box.x+box.width*.95,box.y+box.height*.32,{steps:10});
    await page.mouse.up();
    await panel.getByText('範圍 2 · PDF p.2',{exact:false}).waitFor();
    await panel.getByRole('button',{name:'手動框選／追加',exact:true}).click();
    await panel.getByRole('button',{name:'預覽英文',exact:true}).click();
    await panel.getByText('Epsilon 4.50 0.25',{exact:true}).waitFor();
    await panel.getByRole('button',{name:'翻譯為繁體中文',exact:true}).click();
    await panel.getByText('譯文已儲存；僅涵蓋所選範圍。',{exact:true}).waitFor({timeout:20000});
    assert.ok((await panel.innerText()).includes('艾普西隆 4.50 0.25'));
    assert.ok((await panel.innerText()).includes('阿爾法 -1.25 0.04'));
    await page.screenshot({path:path.join(fixture.profile,'reader-result.png'),fullPage:true});
    await page.screenshot({path:path.join(repo,'tmp','simple-table-reader-result.png'),fullPage:true});
    // Repeated request must hit the independent cache, without another Mock call.
    await panel.getByRole('button',{name:'翻譯為繁體中文',exact:true}).click();
    await panel.getByText('譯文已儲存；僅涵蓋所選範圍。',{exact:true}).waitFor({timeout:20000});
    assert.equal(JSON.parse(fs.readFileSync(path.join(fixture.profile,'mock-calls.json'),'utf8')),1);
    // Reopen reader, retrieve saved translation, and verify existing note/body.
    await page.reload();
    await page.waitForFunction(()=>window.PR?.state.paper?.blocks?.length);
    await page.evaluate(()=>PR.openPage(1));
    await page.locator('#simple-table-open').click();
    await panel.getByRole('button',{name:/^譯文 1/}).click();
    await panel.getByText('艾普西隆 4.50 0.25',{exact:true}).waitFor();
    const initial=JSON.parse(fs.readFileSync(path.join(fixture.profile,'initial-paper.json'),'utf8'));
    const current=JSON.parse(fs.readFileSync(fixture.paper,'utf8'));
    delete current.simple_table_translations;
    assert.deepEqual(current,initial);
    const reader=JSON.parse(fs.readFileSync(path.join(path.dirname(fixture.paper),'reader.json'),'utf8'));
    const oldReader=JSON.parse(fs.readFileSync(path.join(fixture.profile,'initial-reader.json'),'utf8'));
    // Ordinary reader navigation saves progress; notes/edits must stay intact.
    for(const key of ['progress','rev']) { delete reader[key]; delete oldReader[key]; }
    assert.deepEqual(reader,oldReader);
    assert.deepEqual(errors,[]);
    const report={passed:true,checks:['automatic candidate','English preview','manual pointer selection','cross-page append',
      'Traditional Chinese plain text','independent cache','reopen saved translation','existing paper and notes unchanged','no renderer errors'],profile:fixture.profile};
    fs.writeFileSync(path.join(fixture.profile,'browser-report.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify(report));
  } finally {
    if(browser) await browser.close();
    child.kill();
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
