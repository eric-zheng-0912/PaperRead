// Reuse only the existing renderer harness; do not execute the old test suite.
const fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const assert = require('node:assert/strict');
const {test} = require('node:test');
const source = fs.readFileSync(path.join(__dirname, 'test_table_render.cjs'), 'utf8');
const end = source.search(/\ntest\(/);
assert.ok(end > 0);
const harness = {require, __dirname, console, URL};
vm.runInNewContext(source.slice(0, end) + '\nglobalThis.renderDraft = render;', harness);

test('automatic draft matrix and notes render at its original position', () => {
  const blocks = [
    {id:'before', type:'para', en:'Before', zh:'前文'},
    {id:'tab1', type:'table', page:1, table_schema:1, table_status:'ready', auto_table_draft:'verified',
      caption_en:'Table 1: Estimates',caption_zh:'表 1：估計', head_en:[['Variable','Mean']],head_zh:[['變數','平均值']],
      rows_en:[['Control','-0.123***']],rows_zh:[['控制組','-0.123***']],
      footnotes_en:['Note: p < 0.01'],footnotes_zh:['註：p < 0.01']},
    {id:'after', type:'para', en:'After', zh:'後文'}
  ];
  const html = harness.renderDraft(blocks, {canAsk:true});
  assert.ok(html.includes('控制組'));
  assert.ok(html.includes('-0.123***'));
  assert.ok(html.includes('p &lt; 0.01'));
  assert.ok(html.indexOf('id="b-before"') < html.indexOf('id="b-tab1"'));
  assert.ok(html.indexOf('id="b-tab1"') < html.indexOf('id="b-after"'));
  // File-mode rendering needs neither server controls nor placement confirmation.
  assert.ok(html.includes('<table'));
  assert.ok(html.includes('Control'));
  assert.ok(!html.includes('表格保留於原文'));
});
