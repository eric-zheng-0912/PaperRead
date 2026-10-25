// Targeted renderer tests only; no browser, service, inference or user-data access.
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assert = require('node:assert/strict'), {test} = require('node:test');
const file = fs.readFileSync(path.join(__dirname, 'test_table_render.cjs'), 'utf8');
const harness = {require: name => name === 'node:fs' ? {
  ...fs, readFileSync: (name, ...args) => {
    const data = fs.readFileSync(name, ...args);
    return String(name).replace(/\\/g, '/').endsWith('reader/render.js') ? data.replace('  // Input comes exclusively',
      '  PR.__mountManualTablesForTest = mountManualTables;\n  // Input comes exclusively') : data;
  }
} : require(name), __dirname, console, URL};
let helper = file.slice(0, file.search(/\ntest\(/));
helper = helper.replace('store: { mode: "file" }', 'store: { mode: options.storeMode || "file" }');
helper = helper.replace('  PR.renderPaper();', '  PR.pageCapHtml = () => ""; PR.readingView = () => options.view || "translation"; PR.originalFirst = () => options.order !== "translation"; PR.renderPaper();');
vm.runInNewContext(helper + '\nglobalThis.renderLinked = render;', harness);
const fixture = () => ({id:'tab1', type:'table', page:7, caption_en:'Table 1: Synthetic summary', caption_zh:'表 1：合成摘要',
  manual_table_view:{record_id:'mock-record',revision:3,
    source:{regions:[{page:7,box:[.1,.2,.9,.8]}],segments:[
      {id:'s0',region:0,en:'Table 1: Synthetic summary'},
      {id:'s1',region:0,en:'Treatment 1.20***'},
      {id:'s2',region:0,en:'Note: p < 0.01'}]},
    values:[{id:'s0',zh:'表 1：合成摘要'},{id:'s1',zh:'處置組 1.20***'},{id:'s2',zh:'註：p < 0.01'}],
    previews:['data:image/webp;base64,AAAA']}});
const render = (block, options={}) => harness.renderLinked([block],{...options,storeMode:'server',inspect:true});

test('unstructured translation is explicitly labelled and retained in collapsed details', () => {
  const b=fixture(), before=JSON.stringify(b), {html}=render(b);
  assert.match(html,/class="manual-table-original" hidden/);
  assert.match(html,/class="manual-table-translation" lang="zh-Hant" data-no-i18n="">/);
  assert.match(html,/處置組 1.20\*\*\*/);
  assert.match(html,/註：p &lt; 0.01/);
  assert.match(html,/尚未完成結構化表格翻譯/);
  assert.match(html,/<details class="manual-table-saved-text">/);
  assert.doesNotMatch(html,/<details[^>]* open|<table/);
  assert.equal(html.split('合成摘要').length-1,1);
  assert.equal(html.split('class="caption"').length-1,1);
  assert.doesNotMatch(html,/fig-placeholder|table-placeholder|表格保留於原文/);
  assert.match(html,/解除關聯/); assert.match(html,/重新選框／翻譯/);assert.match(html,/查看原文 PDF/);
  assert.equal(JSON.stringify(b),before);
});

test('both orders contain one source preview and one translation without placeholder', () => {
  for(const order of ['original','translation']) {
    const {html}=render(fixture(),{view:'both',order});
    assert.doesNotMatch(html,/class="manual-table-(?:original|translation)"[^>]* hidden/);
    const a=html.indexOf('class="manual-table-original"'),b=html.indexOf('class="manual-table-translation"');
    assert.equal(a<b,order==='original');
    assert.equal(html.split('<img ').length-1,1);
    assert.equal(html.split('處置組 1.20***').length-1,1);
    assert.doesNotMatch(html,/table-placeholder/);
  }
});

test('original mode has the actual crop and optional translation entry', () => {
  const {html}=render(fixture(),{view:'original'});
  assert.match(html,/class="manual-table-translation"[^>]* hidden/);
  assert.match(html,/manual-table-view">檢視已保存譯文/);
  assert.match(html,/<img src="data:image\/webp;base64,AAAA"/);
});

test('reloading the persisted Mock projection restores the same translated content', () => {
  const saved=JSON.stringify(fixture());
  const first=render(JSON.parse(saved)).html, second=render(JSON.parse(saved)).html;
  assert.equal(first,second);assert.match(second,/處置組 1.20/);
});

test('invalid linked translation or preview produces an explicit error, never fake success', () => {
  for(const corrupt of [b=>b.manual_table_view.values.pop(),b=>b.manual_table_view.values[1].id='wrong',
    b=>b.manual_table_view.previews[0]='https://invalid.example/image']) {
    const b=fixture();corrupt(b);const {html}=render(b);
    assert.match(html,/role="alert"/);assert.match(html,/未顯示譯表/);
    assert.doesNotMatch(html,/處置組|manual-table-translation|table-placeholder/);
  }
});

test('backend rejected link displays its explicit issue and retains unlink control', () => {
  const b=fixture();delete b.manual_table_view;
  b.manual_table_issue='已儲存的表格關聯需要重新確認';b.manual_table_link_revision=3;
  const {html}=render(b);assert.match(html,/role="alert"/);assert.match(html,/需要重新確認/);
  assert.match(html,/解除關聯/);assert.doesNotMatch(html,/table-placeholder/);
});

test('model-shaped HTML remains escaped text', () => {
  const b=fixture();b.manual_table_view.values[1].zh='<img src=x onerror=alert(1)>譯文';
  const {html}=render(b);assert.match(html,/&lt;img src=x onerror=alert\(1\)&gt;譯文/);
  assert.doesNotMatch(html,/<img src=x/);
});

test('mode changes and retained actions work without refetching or translating', () => {
  const {PR}=render(fixture()); let view='translation', first=true;
  PR.readingView=()=>view;PR.originalFirst=()=>first;
  const make=()=>({hidden:false,dataset:{},addEventListener(name,fn){this[name]=fn;}});
  const original=make(),zh=make(),toggle=make(),unlink=make(),reselect=make(),source=make();
  source.dataset.manualRegion='0';
  const sides={children:[original,zh],prepend(n){this.children=this.children.filter(x=>x!==n);this.children.unshift(n);}};
  const classes=new Set();
  const node={dataset:{manualBlock:'tab1'},classList:{contains:c=>classes.has(c),toggle:c=>classes.has(c)?classes.delete(c):classes.add(c)},
    querySelector:s=>({'.manual-table-original':original,'.manual-table-translation':zh,'.manual-table-sides':sides,
      '.manual-table-view':toggle,'.manual-table-unlink':unlink,'.manual-table-reselect':reselect})[s]};
  PR.$$=s=>s==='.manual-table-source'?[source]:[node];PR.layoutMargin=()=>{};
  const calls=[];PR.openPage=(...a)=>calls.push(['pdf',...a]);PR.unlinkManualTable=(...a)=>calls.push(['unlink',...a]);PR.editManualTable=()=>calls.push(['edit']);
  PR.__mountManualTablesForTest({});
  assert.equal(original.hidden,true);assert.equal(zh.hidden,false);
  view='both';first=false;PR.applyManualTableLanguage();assert.equal(sides.children[0],zh);
  assert.equal(original.hidden,false);assert.equal(zh.hidden,false);
  view='original';PR.applyManualTableLanguage();assert.equal(zh.hidden,true);toggle.click();assert.equal(zh.hidden,false);
  unlink.click();reselect.click();source.click();
  assert.equal(calls[0][0],'unlink');assert.equal(calls[0][1],'tab1');assert.equal(calls[0][2],3);
  assert.equal(calls[1][0],'edit');assert.equal(calls[2][0],'pdf');assert.equal(calls[2][1],7);
});

test('trusted bilingual cell matrices use semantic table elements without duplicate captions', () => {
  const b=fixture();Object.assign(b,{table_schema:1,table_status:'ready',
    head_en:[['Group','Estimate']],head_zh:[['組別','估計值']],
    rows_en:[['Control','1.20***']],rows_zh:[['控制組','1.20***']],
    footnotes_en:['p < 0.01'],footnotes_zh:['p < 0.01']});
  const before=JSON.stringify(b),{html}=render(b);
  assert.equal(html.split('<table ').length-1,1);
  assert.match(html,/<thead>.*<th scope="col"/);
  assert.match(html,/<tbody><tr><td/);
  assert.match(html,/1.20\*\*\*/);
  assert.doesNotMatch(html,/尚未完成結構化|manual-table-saved-text/);
  assert.equal(html.split('class="caption"').length-1,1);
  assert.equal(JSON.stringify(b),before);
});

test('invalid cell dimensions never turn segment text into invented rows', () => {
  const b=fixture();Object.assign(b,{table_schema:1,table_status:'ready',head_en:[['A','B']],head_zh:[['甲']],
    rows_en:[['C','1.20']],rows_zh:[['乙']],footnotes_en:[],footnotes_zh:[]});
  const {html}=render(b);
  assert.match(html,/尚未完成結構化表格翻譯/);assert.doesNotMatch(html,/<table/);
});

test('source crop is outside the fixed-height generic button style', () => {
  const {html}=render(fixture(),{view:'both'});
  assert.match(html,/class="manual-table-image-scroll"/);
  assert.match(html,/class="manual-table-image manual-table-source"/);
  assert.doesNotMatch(html,/class="btn[^\"]*manual-table-image/);
});
