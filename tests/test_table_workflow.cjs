const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const {test}=require('node:test');
const script=fs.readFileSync(path.join(__dirname,'../easyread/web/js/reader/table-workflow.js'),'utf8');
class Node {
  constructor(tag,text=''){this.tagName=tag;this._text=text;this.children=[];this.dataset={};this.listeners={};this.disabled=false;this.value='';}
  set textContent(v){this._text=String(v);} get textContent(){return this._text+this.children.map(n=>n.textContent||'').join('');}
  append(...nodes){for(const node of nodes){node.parent=this;this.children.push(node);}}
  addEventListener(name,fn){this.listeners[name]=fn;}
  showModal(){this.open=true;}close(){this.open=false;this.listeners.close?.();}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(n=>n!==this);}
}
function descendants(node,tag){return node.children.flatMap(n=>[...(n.tagName===tag?[n]:[]),...descendants(n,tag)]);}
function setup(result,confirm=true){
  const body=new Node('body'),events={},calls=[];
  const document={body,createElement:t=>new Node(t),addEventListener:(k,f)=>events[k]=f,getElementById:()=>({scrollIntoView(){}})};
  const PR={state:{job:{},reader:{edits:{}}},blockById:{anchor:{id:'anchor',type:'table',page:9}},pid:'synthetic',t:s=>s,
    esc:s=>String(s).replaceAll('<','&lt;').replaceAll('"','&quot;'),
    api:async(url,options)=>{calls.push({url,body:options.body});if(options.body.action==='list'){if(result instanceof Error)throw result;return result;}return {ok:true};},
    poll(){calls.push({poll:true});},toast(message){calls.push({toast:message});},
    openPage:async(...args)=>calls.push({page:args}),openSimpleTables:async(...args)=>calls.push({manual:args}),setCurrent(){}};
  vm.runInNewContext(script,{window:{PR,confirm:()=>confirm},document,console});
  return {PR,body,calls,events};
}
const empty={items:[],drafts:{},reviews:{},targets:[],revision:0};
function findButton(parent,label){return descendants(parent,'button').find(b=>b.textContent===label);}
function assertNoModel(calls){assert.ok(calls.every(c=>!c.url||c.body?.action==='list'),'Opening triggered a write/model action');}

test('every workflow state has visible status and a working page-aware manual fallback',async()=>{
  for(const state of ['unprocessed','recognized','cached','translated_unplaced','ambiguous','recognition_failed','manual_protected','completed','cancelled','failed','processing']){
    const ctx=setup({...empty,items:[{id:state,state,block_id:'anchor',page:9,reason:'Synthetic reason'}]});
    await ctx.PR.tableWorkflow.open('anchor');const dialog=ctx.body.children[0];
    assert.ok(dialog.textContent.includes(ctx.PR.tableWorkflow.labels[state]));assertNoModel(ctx.calls);
    const section=descendants(dialog,'section')[0];await findButton(section,'手動框選').onclick();
    assert.equal(ctx.calls.find(c=>c.manual).manual[0].page,9);assert.equal(ctx.calls.find(c=>c.manual).manual[0].pick,true);
    assertNoModel(ctx.calls);
  }
});

test('recognized table starts only after explicit confirmation and limits request to selected table',async()=>{
  const ctx=setup(empty);await ctx.PR.tableWorkflow.start('anchor');
  const write=ctx.calls.find(c=>c.url);assert.equal(write.body.tables_only,true);
  assert.equal(write.body.table_id,'anchor');assert.equal(write.body.pages,'9');
  const declined=setup(empty,false);await declined.PR.tableWorkflow.start('anchor');assert.equal(declined.calls.length,0);
});

test('cached unplaced or ambiguous drafts confirm without translation, and manual-linked targets cannot be selected',async()=>{
  for(const state of ['translated_unplaced','ambiguous']){
    const ctx=setup({...empty,revision:4,items:[{id:'d',state,page:9,draft_id:'draft',cache_valid:true}],
      drafts:{draft:{table:{page:9,head_zh:[['標題']],rows_zh:[['1.20***']]}}},
      targets:[{id:'anchor',page:9,hash:'current',type:'table'},{id:'protected',page:9,hash:'x',manual_linked:true}]});
    await ctx.PR.tableWorkflow.open();const dialog=ctx.body.children[0];assertNoModel(ctx.calls);
    assert.ok(dialog.textContent.includes('已有有效快取'));
    const select=descendants(dialog,'select')[0],confirm=findButton(dialog,'確認加入此位置');
    assert.equal(confirm.disabled,true);assert.equal(select.children.find(n=>n.value==='protected').disabled,true);
    select.value='anchor';select.onchange();assert.equal(confirm.disabled,false);await confirm.onclick();
    const write=ctx.calls.filter(c=>c.url).at(-1);assert.equal(write.body.action,'confirm');assert.equal(write.body.revision,4);
    assert.ok(!ctx.calls.some(c=>c.url?.endsWith('/translate')));
  }
});

test('status endpoint failure still leaves a usable manual selection button',async()=>{
  const ctx=setup(new Error('synthetic status failure'));await ctx.PR.tableWorkflow.open('anchor');
  const dialog=ctx.body.children[0];assert.match(dialog.textContent,/無法載入表格狀態/);
  await findButton(dialog,'手動框選').onclick();assert.ok(ctx.calls.some(c=>c.manual));assertNoModel(ctx.calls);
});

test('original region is forwarded into PDF navigation and manual panel without translating',async()=>{
  const ctx=setup(empty),region={page:9,box:[.1,.2,.8,.7]};
  await ctx.PR.tableWorkflow.manual({block_id:'anchor',page:9,regions:[region]});
  assert.equal(ctx.calls.find(c=>c.page).page[2],region);assert.equal(ctx.calls.find(c=>c.manual).manual[0].location,region);
  assertNoModel(ctx.calls);
});

test('inline states always expose review and manual actions, without relying on model availability',()=>{
  const ctx=setup(empty);const b={id:'anchor',page:9,type:'table'};
  for(const block of [b,{...b,manual_table_issue:'Unreliable'},
    {...b,table_schema:1,head_en:[['Label']],rows_en:[['Value']]},
    {...b,manual_table_view:{}},
    {...b,table_schema:1,head_en:[['Label']],rows_en:[['Value']],head_zh:[['標題']],rows_zh:[['譯文']]}]){
    const html=ctx.PR.tableWorkflow.inline(block);assert.match(html,/確認表格草稿/);assert.match(html,/手動框選/);
    assert.match(html,/data-table-block="anchor"/);
  }
});

test('delegated inline actions route to the existing shared panel',async()=>{
  const ctx=setup(empty);let routed;
  ctx.PR.openSimpleTables=async value=>{routed=value;};
  const button={dataset:{tableAction:'manual',tableBlock:'anchor'}};
  ctx.events.click({target:{closest:()=>button},preventDefault(){}});
  await new Promise(resolve=>setImmediate(resolve));assert.equal(routed.page,9);assert.equal(routed.pick,true);
});

test('reader renders global controls even without model access, and per-block recovery controls',()=>{
  const testRoot=__dirname;
  const oldHarness=fs.readFileSync(path.join(testRoot,'test_table_render.cjs'),'utf8');
  let prefix=oldHarness.slice(0,oldHarness.search(/\ntest\(/));
  prefix=prefix.replace('store: { mode: "file" }','store: { mode: "server" }');
  prefix=prefix.replace('documentElement: { dataset: {} },','documentElement: { dataset: {} }, addEventListener(){},');
  prefix=prefix.replace('  PR.renderPaper();','  PR.pageCapHtml=()=>""; PR.renderPaper();');
  const customFs={...fs,readFileSync:(file,...args)=>{
    const text=fs.readFileSync(file,...args);
    return String(file).replace(/\\/g,'/').endsWith('reader/render.js')?script+'\n'+text:text;
  }};
  const context={require:name=>name==='node:fs'?customFs:require(name),__dirname:testRoot,console,URL};
  vm.runInNewContext(prefix+'\nglobalThis.renderWorkflow=render;',context);
  for(const block of [{id:'t',type:'table',page:9,manual_table_issue:'Synthetic structural failure'},
    {id:'t',type:'table',page:9,table_schema:1,head_en:[['Label']],rows_en:[['Value']]}]){
    const html=context.renderWorkflow([block],{canAsk:false});
    assert.match(html,/data-table-action="start"/);assert.match(html,/確認表格草稿/);
    assert.match(html,/data-table-action="manual" data-table-block="t"/);
    assert.equal(html.split('id="b-t"').length-1,1);
  }
});
