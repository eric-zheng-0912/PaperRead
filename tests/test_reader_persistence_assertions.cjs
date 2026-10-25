const {test} = require('node:test');
const assert = require('node:assert/strict');
const {assertReaderPersistence: check} = require('./reader_persistence_assertions.cjs');
const base = () => ({schema:2, rev:1, notes:{n:{body:'keep'}}, edits:{b:{zh:'keep'}}, ai_annotations:{a:{status:'accepted'}}, paper_note:{body:'keep'}, progress:{}, future:{keep:true}});
test('valid progress and revision preserve all human and unknown fields', () => {
  const a=base(),b=structuredClone(a);b.rev++;b.progress={block:'b',at:'2026-10-23T00:00:00Z',ratio:.3,off:.4};check(a,b,['b']);
});
test('no change remains valid',()=>check(base(),base(),['b']));
test('does not silently ignore human or future fields',()=>{
  for(const k of ['notes','edits','ai_annotations','paper_note','future','schema']){const a=base(),b=structuredClone(a);delete b[k];assert.throws(()=>check(a,b,['b']));}
});
test('rejects invalid revision, location, timestamp and fractions',()=>{
  for(const patch of [{rev:0},{progress:{block:'wrong',at:'2026-01-01'}},{progress:{block:'b',at:'bad'}},{progress:{block:'b',at:'2026-01-01',ratio:2}},{progress:{block:'b',at:'2026-01-01',off:-1}},{progress:{surprise:1}}])assert.throws(()=>check(base(),{...base(),...patch},['b']));
});
