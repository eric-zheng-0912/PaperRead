/* Shared table status/actions. Opening or locating a panel never starts a model. */
(function (PR) {
  'use strict';
  const labels = {
    unprocessed:'尚未處理', recognized:'已辨識，尚未翻譯', cached:'已有有效快取',
    translated_unplaced:'已翻譯，待定位', ambiguous:'位置不唯一，請確認', recognition_failed:'結構辨識失敗',
    manual_protected:'已有人工關聯或修改，保留原資料', completed:'已完成', processing:'工作進行中',
    cancelled:'工作已取消，舊結果保留', failed:'工作失敗或資料待確認，舊結果保留',
    pending_check:'待確認', verified:'已核對'
  };
  const safe = text => PR.esc(String(text || ''));
  function blockState(b) {
    if (b.manual_table_view) return 'manual_protected';
    if (b.manual_table_issue && Number.isInteger(b.manual_table_link_revision)) return 'failed';
    if (b.manual_table_issue || b.table_issue) return 'recognition_failed';
    if (Object.keys(PR.state.reader?.edits || {}).some(k=>k===b.id || k.startsWith(b.id+'#'))) return 'manual_protected';
    const matrix = side => {
      const rows=b['rows_'+side],head=b['head_'+side];
      return Array.isArray(rows)&&rows.length&&Array.isArray(head)&&[...head,...rows].every(r=>Array.isArray(r)&&r.length===rows[0]?.length&&r.length&&r.every(c=>typeof c==='string'));
    };
    if (b.table_schema===1 && matrix('en') && matrix('zh') && b.rows_en.length===b.rows_zh.length && b.rows_en[0].length===b.rows_zh[0].length) return b.auto_table_verification ? (b.auto_table_verification==='verified'?'verified':'pending_check') : 'completed';
    if (b.table_schema===1 && matrix('en')) return b.rows_zh ? 'failed' : 'recognized';
    const job = PR.state.job || {};
    if ((job.tables_only || job.include_tables) && (!job.table_id || job.table_id === b.id) && (!job.pages || job.pages.includes(b.page))) {
      if (job.state === 'cancelled') return 'cancelled';
      if (job.state === 'error') return 'failed';
      if (['queued','running'].includes(job.state)) return 'processing';
    }
    return 'unprocessed';
  }
  function inline(b) {
    const state = blockState(b), id = safe(b.id);
    const button = (action,text) => '<button type="button" class="btn sm line" data-table-action="'+action+'" data-table-block="'+id+'">'+safe(PR.t(text))+'</button>';
    return '<div class="table-workflow-actions" data-table-state="'+state+'"><span role="status">'+safe(PR.t(labels[state]))+'</span>'+
      (['unprocessed','recognized','cancelled','failed'].includes(state) ? button('start',state==='recognized'?'翻譯此表格':'補齊／更新表格') : '')+
      button('review','確認表格草稿')+button('manual','手動框選')+'</div>';
  }
  async function manual(item={}) {
    const b=PR.blockById?.[item.block_id] || item;
    const page=item.page || b.page, region=item.regions?.[0];
    if (Number.isInteger(page) && page>0) await PR.openPage(page,item.block_id || b.id,region);
    await PR.openSimpleTables({page,pick:true,block_id:item.block_id || b.id,location:region});
  }
  async function start(blockId) {
    if (!window.confirm(PR.t('補齊／更新表格會優先重用有效快取；缺少譯文時會呼叫目前模型。是否開始？'))) return;
    const body={tables_only:true};if(blockId){body.table_id=blockId;const page=PR.blockById?.[blockId]?.page;if(Number.isInteger(page))body.pages=String(page);}
    else { const pages=PR.state.paper?.meta?.page_count;if(Number.isInteger(pages)&&pages>0)body.pages='1-'+pages; }
    try {
      await PR.api('/api/p/'+PR.pid+'/translate',{method:'POST',body});PR.poll();
      PR.toast(PR.t('已啟動表格工作；待確認結果請按「確認表格草稿」。'));
    } catch(error) { PR.toast(PR.esc(error.message)); }
  }
  function element(tag,text,parent) {
    const n=document.createElement(tag);if(text)n.textContent=text;if(parent)parent.append(n);return n;
  }
  function button(parent,text,action) {
    const n=element('button',PR.t(text),parent);n.type='button';n.className='btn sm line';
    n.onclick=async()=>{try{await action();}catch(error){PR.toast(PR.esc(error.message));}};return n;
  }
  function structure(view,op,row,column,endRow=row,endColumn=column){
    const v=JSON.parse(JSON.stringify(view)),axis=op.includes('column')?'column':'row',size=axis==='row'?'rows':'columns',span=axis==='row'?'row_span':'col_span',at=axis==='row'?row:column;
    if(op.startsWith('add_')||op.startsWith('delete_')){
      const add=op.startsWith('add_');if(!add&&v[size]===1)throw Error('至少保留一列及一欄');
      v.cells=v.cells.filter(c=>{if(add){if(c[axis]>=at)c[axis]++;else if(c[axis]+c[span]>at)c[span]++;return true;}
        if(c[axis]>at)c[axis]--;else if(c[axis]<=at&&c[axis]+c[span]>at){if(c[span]===1)return false;c[span]--;}return true;});
      v[size]+=add?1:-1;
    }else if(op==='merge'){
      if(endRow<row||endColumn<column||endRow>=v.rows||endColumn>=v.columns)throw Error('無效合併範圍');
      const chosen=v.cells.filter(c=>c.row<=endRow&&c.row+c.row_span>row&&c.column<=endColumn&&c.column+c.col_span>column);
      if(chosen.some(c=>c.row<row||c.column<column||c.row+c.row_span>endRow+1||c.column+c.col_span>endColumn+1))throw Error('範圍切過既有合併格');
      const first=chosen.find(c=>c.row===row&&c.column===column);if(!first)throw Error('無效起點');
      const text=chosen.some(c=>c.text===null)?null:chosen.map(c=>c.text).join('\n');
      const source_ids=chosen.flatMap(c=>c.source_ids||(c.source_id?[c.source_id]:[]));
      v.cells=v.cells.filter(c=>!chosen.includes(c));v.cells.push({...first,row_span:endRow-row+1,col_span:endColumn-column+1,text,source_ids});
    }else if(op==='split'){
      const c=v.cells.find(c=>c.row===row&&c.column===column);if(!c)throw Error('請選擇儲存格起點');c.row_span=1;c.col_span=1;
    }
    const occupied=new Set();for(const c of v.cells)for(let r=c.row;r<c.row+c.row_span;r++)for(let k=c.column;k<c.column+c.col_span;k++)occupied.add(r+':'+k);
    for(let r=0;r<v.rows;r++)for(let c=0;c<v.columns;c++)if(!occupied.has(r+':'+c))v.cells.push({id:'user:'+r+':'+c+':'+v.cells.length,row:r,column:c,row_span:1,col_span:1,text:null,source_id:null,status:'unresolved'});
    return v;
  }
  async function edit(item){
    const endpoint='/api/p/'+PR.pid+'/auto-table';
    let saved=await PR.api(endpoint,{method:'POST',body:item.editor_id?{action:'editor_open',editor_id:item.editor_id}:{action:'editor_prepare',draft_id:item.draft_id}});
    const id=item.editor_id||saved.editor_id;let value=saved.view;
    const dialog=element('dialog');dialog.className='table-workflow-dialog';document.body.append(dialog);dialog.addEventListener('close',()=>dialog.remove());dialog.showModal();
    function draw(){
      dialog.textContent='';button(dialog,'關閉',()=>dialog.close());element('h2',PR.t('編輯表格譯文（原始辨識資料保留）'),dialog);
      const status=element('p',PR.t(saved.verified?'已核對':'待確認'),dialog);
      const dirty=()=>{saved.verified=false;status.textContent=PR.t('待確認（尚未儲存）');};
      if(item.page)button(dialog,'查看原文 PDF',()=>PR.openPage(item.page,item.block_id,item.regions?.[0]));
      if(!saved.source?.translation_allowed)element('p',PR.t('來源或結構尚未通過自動翻譯檢查；此處只保存人工編輯，不代表辨識可靠。'),dialog);
      if(saved.source_changed){element('p',PR.t('來源辨識已更新，人工內容保留；請重新核對 PDF 及最新來源後確認。'),dialog);element('pre',JSON.stringify(saved.source,null,2),dialog);}
      element('label',PR.t('表題'),dialog);const caption=element('textarea',null,dialog);caption.value=value.caption;caption.oninput=()=>{value.caption=caption.value;dirty();};
      element('label',PR.t('註腳（每行一則）'),dialog);const notes=element('textarea',null,dialog);notes.value=value.footnotes.join('\n');notes.oninput=()=>{value.footnotes=notes.value?notes.value.split('\n'):[];dirty();};
      const table=element('table',null,dialog);table.className='tbl';
      for(let r=0;r<value.rows;r++){const tr=element('tr',null,table);for(const c of value.cells.filter(c=>c.row===r).sort((a,b)=>a.column-b.column)){
        const td=element('td',null,tr);td.rowSpan=c.row_span;td.colSpan=c.col_span;
        element('small',`${c.row+1}, ${c.column+1}`,td);const input=element('textarea',null,td);input.value=c.text??'';input.placeholder=PR.t('需確認');input.oninput=()=>{c.text=input.value;dirty();};
      }}
      const fields=['列','欄','合併至列','合併至欄'].map((label,i)=>{element('label',PR.t(label),dialog);const n=element('input',null,dialog);n.type='number';n.min='1';n.value='1';return n;});
      for(const [op,label]of [['add_row','新增列'],['delete_row','刪除列'],['add_column','新增欄'],['delete_column','刪除欄'],['merge','合併儲存格'],['split','拆分儲存格']])button(dialog,label,()=>{
        const [r,c,er,ec]=fields.map(f=>Number(f.value)-1);if(![r,c,er,ec].every(Number.isInteger)||r<0||c<0||r>=value.rows||c>=value.columns)throw Error('無效列欄位置');
        value=structure(value,op,r,c,er,ec);saved.verified=false;draw();
      });
      button(dialog,'儲存人工修改',async()=>{saved=await PR.api(endpoint,{method:'POST',body:{action:'editor_save',editor_id:id,revision:saved.revision,view:value}});value=saved.view;PR.poll();draw();});
      button(dialog,'我已核對原文、欄列與譯文',async()=>{
        if(!window.confirm(PR.t('請先儲存修改；確認已逐格核對原文及譯文？')))return;
        saved=await PR.api(endpoint,{method:'POST',body:{action:'editor_save',editor_id:id,revision:saved.revision,view:value}});
        saved=await PR.api(endpoint,{method:'POST',body:{action:'editor_verify',editor_id:id,revision:saved.revision,confirmed:true,source_hash:saved.latest_source_hash}});PR.poll();draw();
      });
    }draw();
  }
  async function open(blockId) {
    const b=PR.blockById?.[blockId];
    if(b?.page)await PR.openPage(b.page,blockId);
    const dialog=element('dialog');dialog.className='table-workflow-dialog';
    button(dialog,'關閉',()=>dialog.close());element('h2',PR.t('確認表格草稿'),dialog);
    // Recovery remains available even if the read-only status endpoint fails.
    button(dialog,'手動框選',async()=>{dialog.close();await manual({block_id:blockId,page:b?.page});});
    dialog.addEventListener('close',()=>dialog.remove());document.body.append(dialog);dialog.showModal();
    try {
      const endpoint='/api/p/'+PR.pid+'/auto-table';
      const result=await PR.api(endpoint,{method:'POST',body:{action:'list'}});
      let items=result.items || [];
      if(blockId){const matched=items.filter(i=>i.block_id===blockId || i.candidate_block_ids?.includes(blockId) || (i.page===b?.page && !i.block_id));if(matched.length)items=matched;}
      if(!items.length){element('p',PR.t('尚無表格結果；可按「補齊／更新表格」或「手動框選」。'),dialog);button(dialog,'補齊／更新表格',()=>start(blockId));}
      for(const item of items){
        const section=element('section',null,dialog);section.dataset.tableState=item.state;
        element('h3',`${item.page ? 'PDF p.'+item.page+' · ' : ''}${item.caption || item.num || item.id}`,section);
        element('p',PR.t(labels[item.state] || labels.failed)+(item.cache_valid?' · '+PR.t(labels.cached):''),section);
        if(item.reason)element('p',item.reason,section);
        if(item.evidence_members?.length>1){const details=element('details',null,section);element('summary',PR.t('同一來源的辨識證據'),details);for(const e of item.evidence_members)if(e.reason)element('p',e.reason,details);}
        if(item.table_checks){const details=element('details',null,section);element('summary',PR.t('結構、來源、表題註解與定位判定'),details);element('pre',JSON.stringify(item.table_checks,null,2),details);}
        if(item.table_checks?.metadata?.caption==='missing_or_unknown')element('p',PR.t('表題未取得或未知，未自動補寫。'),section);
        if(item.table_checks?.metadata?.footnotes)element('p',PR.t(item.table_checks.metadata.footnotes==='missing_or_unknown'?'註腳未取得或未知，未自動補寫。':'已取得部分註腳，完整性仍需人工確認。'),section);
        if(item.partial_preview?.read_only&&!item.partial_preview.translation_allowed)element('p',PR.t('來源或結構有疑義，不自動翻譯；請查看原文 PDF 與警示。'),section);
        if(item.partial_preview?.read_only&&item.partial_preview.translation_allowed){
          element('p',PR.t(item.editor?.translation_available?'部分表格譯文：待確認，缺格仍需補齊。':'部分原文結構預覽：尚未完成，不是譯表，也未經人工核對。'),section);
          const wrap=element('div',null,section);wrap.className='tbl-wrap';
          const preview=element('table',null,wrap);preview.className='tbl';
          for(const row of item.partial_preview.slots){const tr=element('tr',null,preview);for(const slot of row){
            const cell=element('td',null,tr);cell.dataset.cellStatus=slot.status;
            element('span',slot.text===null?PR.t('需確認（缺少儲存格）'):slot.text,cell);
            if(slot.status!=='source_checked')element('small',PR.t('需確認'),cell);
          }}
          if(item.editor?.translation_available){
            element('h4',PR.t(item.editor.verified?'人工已核對版本':'譯文（待確認）'),section);
            const translated=element('table',null,section);translated.className='tbl';const v=item.editor.view;
            for(let r=0;r<v.rows;r++){const tr=element('tr',null,translated);for(const c of v.cells.filter(c=>c.row===r).sort((a,b)=>a.column-b.column)){
              const td=element('td',c.text===null?PR.t('需確認'):c.text,tr);td.rowSpan=c.row_span;td.colSpan=c.col_span;
            }}
            element('p',v.caption,section);for(const note of v.footnotes)element('p',note,section);
          }
        }
        if(item.risks?.length){const details=element('details',null,section);element('summary',PR.t('來源差異與風險'),details);element('pre',JSON.stringify(item.risks,null,2),details);}
        if(item.protected)element('p',PR.t('既有譯文及人工資料受到保護，不會自動覆寫。'),section);
        if(item.editor_id || item.draft_id&&item.cache_valid)button(section,'編輯表格譯文與結構',async()=>{dialog.close();await edit(item);});
        if(['cancelled','error'].includes(item.job_state))element('p',PR.t(item.job_state==='cancelled'?labels.cancelled:labels.failed),section);
        button(section,'手動框選',async()=>{dialog.close();await manual(item);});
        if(item.page)button(section,'查看原文 PDF',async()=>{dialog.close();await PR.openPage(item.page,item.block_id,item.regions?.[0]);});
        if(['unprocessed','recognized','cancelled','failed'].includes(item.state) && item.block_id)
          button(section,item.state==='recognized'?'翻譯此表格':'補齊／更新表格',()=>start(item.block_id));
        const draft=result.drafts?.[item.draft_id];
        if(!draft || !item.cache_valid)continue;
        if(['completed','manual_protected','pending_check','verified'].includes(item.state)) {
          if(item.block_id)button(section,'查看正文位置',()=>{dialog.close();PR.setCurrent(item.block_id,{bar:false});document.getElementById('b-'+item.block_id)?.scrollIntoView({block:'center'});});
          if(item.state==='pending_check')button(section,'我已核對原文、欄列與譯文',async()=>{
            if(!window.confirm(PR.t('已逐格核對原文、譯文、數值與表格位置？此操作只標記人工核對，不會重新翻譯。')))return;
            await PR.api(endpoint,{method:'POST',body:{action:'verify',confirmed:true,revision:result.revision,draft_id:item.draft_id,source_fingerprint:item.source_fingerprint}});
            dialog.close();PR.poll();
          });
          if(item.state!=='manual_protected')continue;
        }
        const table=element('table',null,section);table.className='tbl';
        for(const [tag,rows]of [['thead',draft.table.head_zh||[]],['tbody',draft.table.rows_zh||[]]]){
          const group=element(tag,null,table);for(const row of rows){const tr=element('tr',null,group);for(const cell of row)element(tag==='thead'?'th':'td',cell,tr);}
        }
        const select=element('select',null,section);const blank=element('option',PR.t('請選擇同頁正文位置'),select);blank.value='';
        const targets=(result.targets||[]).filter(t=>t.page===draft.table.page);
        for(const target of targets){const option=element('option',`${target.id} · ${target.caption || target.type}${target.manual_linked?' · '+PR.t('已有人工關聯'):''}`,select);option.value=target.id;option.disabled=!!target.manual_linked;}
        if(!targets.length)element('p',PR.t('同頁沒有可定位的正文區塊；譯文已保存，可使用「手動框選」檢視既有紀錄。'),section);
        const confirm=button(section,'確認加入此位置',async()=>{
          const target=targets.find(t=>t.id===select.value);
          if(!target || target.manual_linked)return;
          if(!window.confirm(PR.t('確認將已保存譯文顯示於此位置？原始內容、筆記及人工修改保留。')))return;
          await PR.api(endpoint,{method:'POST',body:{action:'confirm',confirmed:true,revision:result.revision,
            draft_id:item.draft_id,block_id:target.id,block_hash:target.hash}});
          dialog.close();PR.poll();
        });confirm.disabled=true;select.onchange=()=>{confirm.disabled=!targets.some(t=>t.id===select.value&&!t.manual_linked);};
      }
    }catch(error){element('p',PR.t('無法載入表格狀態；既有資料未變更，可使用「手動框選」。')+' '+error.message,dialog);}
  }
  PR.tableWorkflow={labels,blockState,inline,open,start,manual,structure,edit};
  document.addEventListener('click',event=>{
    const node=event.target.closest('[data-table-action]');if(!node)return;
    event.preventDefault();const id=node.dataset.tableBlock;
    const action=node.dataset.tableAction;
    Promise.resolve(action==='start'?start(id):action==='review'?open(id):manual({block_id:id})).catch(e=>PR.toast(PR.esc(e.message)));
  });
})(window.PR);
