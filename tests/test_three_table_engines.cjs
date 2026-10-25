const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const {test}=require('node:test');
const workflow=fs.readFileSync(path.join(__dirname,'test_table_workflow.cjs'),'utf8');
const context={require,__dirname,console,URL};
vm.runInNewContext(workflow.slice(0,workflow.search(/\ntest\(/))+'\nglobalThis.fixtureSetup=setup;globalThis.fixtureFindButton=findButton;',context);

test('pending check shows source risks and requires explicit user verification without translating',async()=>{
  const result={revision:7,items:[{id:'anchor',block_id:'anchor',page:9,state:'pending_check',draft_id:'d',cache_valid:true,
    source_fingerprint:'source-hash',risks:[{level:'warning',kind:'punctuation_or_whitespace',source:'source',engine:'original'}]}],
    drafts:{d:{table:{head_zh:[['標題']],rows_zh:[['1.20%']]}}},targets:[]};
  for(const confirmed of [false,true]){
    const ctx=context.fixtureSetup(result,confirmed);await ctx.PR.tableWorkflow.open('anchor');
    const dialog=ctx.body.children[0];assert.match(dialog.textContent,/待確認/);assert.match(dialog.textContent,/來源差異與風險/);
    assert.equal(ctx.calls.filter(c=>c.body?.action==='verify').length,0);
    const button=context.fixtureFindButton(dialog,'我已核對原文、欄列與譯文');assert.ok(button);await button.onclick();
    assert.equal(ctx.calls.filter(c=>c.body?.action==='verify').length,confirmed?1:0);
    if(confirmed){const call=ctx.calls.find(c=>c.body?.action==='verify');assert.equal(call.body.source_fingerprint,'source-hash');assert.equal(call.body.revision,7);}
    assert.ok(!ctx.calls.some(c=>c.url?.endsWith('/translate')));
  }
});

test('verified state has no automatic verify write and retains source PDF/manual controls',async()=>{
  const ctx=context.fixtureSetup({items:[{id:'anchor',state:'verified',block_id:'anchor',page:9,draft_id:'d',cache_valid:true}],drafts:{d:{table:{}}},targets:[]});
  await ctx.PR.tableWorkflow.open('anchor');const dialog=ctx.body.children[0];assert.match(dialog.textContent,/已核對/);
  assert.equal(context.fixtureFindButton(dialog,'我已核對原文、欄列與譯文'),undefined);
  assert.ok(context.fixtureFindButton(dialog,'查看原文 PDF'));assert.ok(context.fixtureFindButton(dialog,'手動框選'));
  assert.equal(ctx.calls.filter(c=>c.url&&!c.body?.action?.includes('list')).length,0);
});

const renderSource=fs.readFileSync(path.join(__dirname,'test_table_render.cjs'),'utf8');
test('partial slots render missing as unresolved and never offer confirmation as a full draft',async()=>{
  const preview={read_only:true,translation_allowed:true,slots:[[{row:0,column:0,text:null,status:'missing'},{row:0,column:1,text:'Value 1.2%',status:'source_checked'}]]};
  const ctx=context.fixtureSetup({items:[{id:'review:x',page:9,state:'recognition_failed',partial_preview:preview,table_checks:{structure:{status:'incomplete'}},regions:[{page:9,box:[0,0,1,1]}]}],drafts:{},targets:[]});
  await ctx.PR.tableWorkflow.open();const dialog=ctx.body.children[0];
  assert.match(dialog.textContent,/需確認（缺少儲存格）/);assert.match(dialog.textContent,/Value 1.2%/);
  assert.equal(context.fixtureFindButton(dialog,'確認加入此位置'),undefined);
  assert.equal(ctx.calls.filter(c=>c.body?.action!=='list'&&c.url).length,0);
});

test('human row column merge split operations retain missing values without invented content',()=>{
  const ctx=context.fixtureSetup({items:[]});const op=ctx.PR.tableWorkflow.structure;
  const initial={rows:2,columns:2,caption:'',footnotes:[],cells:[{id:'a',row:0,column:0,row_span:1,col_span:1,text:'甲'},{id:'b',row:0,column:1,row_span:1,col_span:1,text:'乙'},{id:'c',row:1,column:0,row_span:1,col_span:1,text:null},{id:'d',row:1,column:1,row_span:1,col_span:1,text:'1%'}]};
  const merged=op(initial,'merge',0,0,0,1);assert.equal(merged.cells.find(c=>c.id==='a').col_span,2);
  const split=op(merged,'split',0,0);assert.equal(split.cells.find(c=>c.row===0&&c.column===1).text,null);
  const added=op(initial,'add_row',1,0);assert.equal(added.rows,3);assert.equal(added.cells.filter(c=>c.row===1&&c.text===null).length,2);
  const column=op(added,'add_column',0,1);assert.equal(column.columns,3);
  const removed=op(op(column,'delete_column',0,1),'delete_row',1,0);assert.equal(removed.rows,2);assert.equal(removed.columns,2);
  assert.equal(initial.cells.length,4);
});

test('conflicting partial source exposes PDF warning instead of an unreliable automatic grid',async()=>{
  const ctx=context.fixtureSetup({items:[{id:'review:bad',page:9,state:'recognition_failed',partial_preview:{read_only:true,translation_allowed:false,slots:[[{text:'999',status:'conflict'}]]}}],drafts:{},targets:[]});
  await ctx.PR.tableWorkflow.open();const dialog=ctx.body.children[0],nodes=n=>[n,...n.children.flatMap(nodes)];
  assert.match(dialog.textContent,/不自動翻譯/);assert.equal(nodes(dialog).filter(n=>n.tagName==='table').length,0);
  assert.ok(context.fixtureFindButton(dialog,'查看原文 PDF'));assert.equal(ctx.calls.filter(c=>c.url&&c.body?.action!=='list').length,0);
});

test('partial editor opens and saves only explicit user changes without a translate request',async()=>{
  const result={items:[{id:'review:x',page:9,state:'pending_check',editor_id:'x'}],drafts:{},targets:[]};
  const ctx=context.fixtureSetup(result);let record={revision:0,verified:true,latest_source_hash:'f',view:{rows:1,columns:1,caption:'原表題',footnotes:[],cells:[{id:'a',row:0,column:0,row_span:1,col_span:1,text:null}]}};
  ctx.PR.api=async(url,options)=>{ctx.calls.push({url,body:options.body});const b=options.body;
    if(b.action==='list')return result;
    if(b.action==='editor_save')record={...record,revision:record.revision+1,view:b.view,verified:false};
    return record;
  };
  await ctx.PR.tableWorkflow.open();await context.fixtureFindButton(ctx.body.children[0],'編輯表格譯文與結構').onclick();
  const dialog=ctx.body.children[0],all=n=>[n,...n.children.flatMap(all)];
  const inputs=all(dialog).filter(n=>n.tagName==='textarea');inputs[0].value='人工表題';inputs[0].oninput();
  assert.match(dialog.textContent,/待確認（尚未儲存）/);
  await context.fixtureFindButton(dialog,'儲存人工修改').onclick();
  assert.equal(record.view.caption,'人工表題');assert.equal(record.verified,false);
  assert.ok(!ctx.calls.some(c=>c.url?.endsWith('/translate')));
});

test('whole-document UI action supplies the actual document page range',async()=>{
  const ctx=context.fixtureSetup({items:[]});ctx.PR.state.paper={meta:{page_count:6}};
  await ctx.PR.tableWorkflow.start();assert.equal(ctx.calls.find(c=>c.url).body.pages,'1-6');
});
const renderer={require,__dirname,console,URL};
vm.runInNewContext(renderSource.slice(0,renderSource.search(/\ntest\(/))+'\nglobalThis.renderFixture=render;',renderer);

test('partial human view renders spans and unresolved slots without pretending to be a complete grid',()=>{
  const view={rows:1,columns:3,caption:'人工表題',footnotes:['人工註腳'],cells:[{row:0,column:0,row_span:1,col_span:2,text:'人工合併'},{row:0,column:2,row_span:1,col_span:1,text:null}]};
  const html=renderer.renderFixture([{id:'p',type:'table',page:1,table_editor_view:{verified:false,view,source:{rows:1,slots:[[{row:0,column:0,text:'Original'},{row:0,column:1,text:null},{row:0,column:2,text:'Data'}]]}}}]);
  assert.match(html,/待確認/);assert.match(html,/需確認/);assert.match(html,/colspan="2"/);assert.match(html,/人工註腳/);assert.ok(!html.includes('已核對'));
});
function block(extra={}){return {id:'table',type:'table',page:1,table_schema:1,caption_en:'Table: original',caption_zh:'譯文表題',
  head_en:[['Label','Value']],rows_en:[['Original','1.20%']],head_zh:[['標籤','數值']],rows_zh:[['譯文','1.20%']],...extra};}
test('structured pending/verified tables remain selectable native HTML cells',()=>{
  for(const state of ['pending','verified']){
    const html=renderer.renderFixture([block({auto_table_verification:state})]);
    assert.ok(html.includes('data-table-verification="'+state+'"'));
    assert.ok(html.includes(state==='pending'?'待確認':'已核對'));
    assert.ok(html.includes('<table'));assert.ok(html.includes('<td'));assert.ok(html.includes('1.20%'));
  }
});
test('risky source renders warning and normal-flow source preview without a translated HTML grid',()=>{
  const html=renderer.renderFixture([block({auto_table_risk_preview:'data:image/webp;base64,synthetic',manual_table_issue:'數值歸屬有疑義'})]);
  assert.ok(html.includes('role="alert"'));assert.ok(html.includes('table-source-preview'));
  assert.ok(html.includes('data:image/webp'));assert.ok(!html.includes('<table'));
  assert.equal((html.match(/譯文表題/g)||[]).length,1);
});
