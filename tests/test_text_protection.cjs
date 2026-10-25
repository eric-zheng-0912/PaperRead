const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const {test} = require('node:test');
function markup() {
  const PR = {t:s=>s, esc:s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../easyread/web/js/common/markup.js'),'utf8'),
    {window:{PR}, URL, katex:require('../easyread/web/vendor/katex/katex.min.js')});
  return PR;
}
test('title math is rendered while HTML and Markdown remain literal',()=>{
  const PR=markup(), html=PR.titleHtml('**Title** $x_{i}$ <img onerror=bad> [a](https://x.test)');
  assert.match(html,/class="katex/); assert.match(html,/\*\*Title\*\*/);
  assert.match(html,/&lt;img/); assert.doesNotMatch(html,/<img|<a /);
});
test('footnotes link only to the unique matching page and do not replace citations',()=>{
  const PR=markup(); PR.refById={'1':{id:'1'}};
  PR.buildFootnoteIndex([{id:'f1',type:'para',role:'footnote',page:1,num:'1'}, {id:'f2',type:'para',role:'footnote',page:2,num:'1'}]);
  const html=PR.md('Note [^1] and reference [1]',{footnotePage:2});
  assert.match(html,/data-footnote="b-f2"/); assert.match(html,/class="cite"/);
  assert.doesNotMatch(PR.md('[^1]',{footnotePage:3}),/data-footnote/);
  assert.match(PR.md('[^1]',{footnotePage:3}),/\[\^1\]/);
});
test('ambiguous, unsafe and duplicate-ID footnotes preserve original markers',()=>{
  const PR=markup();
  PR.buildFootnoteIndex([{id:'a',type:'para',role:'footnote',page:1,num:'1'}, {id:'b',type:'para',role:'footnote',page:1,num:'1'}]);
  assert.equal(PR.md('[^1]',{footnotePage:1}),'[^1]');
  PR.buildFootnoteIndex([{id:'bad"id',type:'para',role:'footnote',page:1,num:'1'}]);
  assert.equal(PR.md('[^1]',{footnotePage:1}),'[^1]');
  PR.buildFootnoteIndex([{id:'same',type:'para',role:'footnote',page:1,num:'1'}, {id:'same',type:'para',page:2}]);
  assert.equal(PR.md('[^1]',{footnotePage:1}),'[^1]');
});
test('code and math footnote markers are not linked',()=>{
  const PR=markup(); PR.buildFootnoteIndex([{id:'f',type:'para',role:'footnote',page:1,num:'1'}]);
  const html=PR.md('`[^1]` $x^{[1]}$ \\(x\\)',{footnotePage:1});
  assert.doesNotMatch(html,/data-footnote|class="cite"/);
});
test('long code fences protect literal shorter fences, math and HTML',()=>{
  const PR=markup(); const html=PR.mdBlocks('````py\n```\n$x$ <img> [^1]\n````');
  assert.match(html,/<pre><code>/); assert.match(html,/```/);
  assert.match(html,/\$x\$/); assert.match(html,/&lt;img&gt;/);
  assert.doesNotMatch(html,/class="katex|data-footnote/);
});

function aligned(PR, block) {
  Object.assign(PR, {state:{reader:{edits:{},notes:{}},paper:{blocks:[block]}},
    on(){}, emit(){}, $:()=>null, $$:()=>[], imageUrl:s=>s, hashText:s=>s});
  const ctx={window:{PR}, document:{getElementById:()=>null,addEventListener(){}},
    CSS:{escape:String},getSelection:()=>({toString:()=>''})};
  for(const file of ['render.js','sentences.js'])
    vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../easyread/web/js/reader',file),'utf8'),ctx);
  PR.blockById[block.id]=block;
  return PR;
}
test('legacy sentence offsets never split inline code or bracket math',()=>{
  for(const text of ['`a. b.` Done.', '\\(a. b.\\) Done.']){
    const b={id:'p',type:'para',page:1,en:text,zh:text,sents:[[4,4],[text.length,text.length]]};
    const PR=aligned(markup(),b), before=JSON.stringify(b);
    const html=PR.sentMd('p','en',text);
    assert.doesNotMatch(html,/class="snt"/); assert.equal(JSON.stringify(b),before);
  }
});
test('aligned footnote markers retain page context on both language sides',()=>{
  const text='See [^1]. Next.', b={id:'p',type:'para',page:2,en:text,zh:text,sents:[[9,9],[text.length,text.length]]};
  const PR=aligned(markup(),b);
  PR.buildFootnoteIndex([{id:'f',type:'para',role:'footnote',page:2,num:'1'}]);
  for(const side of ['en','zh']) assert.match(PR.sentMd('p',side,text),/data-footnote="b-f"/);
});
