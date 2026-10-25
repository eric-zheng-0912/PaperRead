(function (PR) {
  "use strict";
  const labels = {unverified:"尚未驗證", incomplete:"套件／模型不完整", basic_import:"基本匯入成功（未驗證推論）", offline_worker_verified:"本次啟動已通過離線 worker 推論", unavailable:"不可用"};
  PR.settingsTabs.docling = {
    render(s) {
      const r=s.doclingResult;
      return '<h4>離線表格辨識 · Docling</h4><p>使用本機外部 Python，CPU、OCR 關閉；不下載模型。找不到時保留原有辨識器。</p>'+
        '<label><input type="checkbox" id="doclingEnabled"'+(s.cfg.docling_enabled!==false?' checked':'')+'>啟用外部 Docling（停用時使用原有辨識器）</label><br>'+
        '<label>Python 執行檔<input class="input" id="doclingPython" value="'+PR.esc(s.cfg.docling_python||'')+'" placeholder="自動偵測已登錄環境"></label>'+
        '<button class="btn" data-docling="pick">選擇 Python</button> <button class="btn" data-docling="detect">檢查／重新偵測</button>'+
        '<p>路徑需按「保存」才會儲存；不會寫入安裝目錄。開啟本頁不執行模型。</p>'+
        (r ? '<p role="status">'+PR.esc(labels[r.status]||r.status)+' — '+PR.esc(r.reason||'')+'</p>'+
          (r.candidates||[]).map((c,i)=>'<p><code>'+PR.esc(c.python)+'</code><br>'+PR.esc(labels[c.status]||c.status)+' · Docling '+PR.esc(c.docling||'未知')+'<br>'+PR.esc(c.reason||'')+
            (c.status==='basic_import'?' <button class="btn sm" data-docling="use" data-index="'+i+'">使用此環境</button>':'')+'</p>').join('') : '<p>尚未驗證。請按「檢查／重新偵測」。</p>');
    },
    sync(s) { const input=PR.$('#doclingPython'); if(input)s.cfg.docling_python=input.value.trim(); const enabled=PR.$('#doclingEnabled');if(enabled)s.cfg.docling_enabled=enabled.checked; },
    async click(e,s) {
      const b=e.target.closest('[data-docling]'); if(!b)return false;
      this.sync(s);
      try {
        if(b.dataset.docling==='pick') {
          if(!window.easyreadDesktop?.pickPython) {PR.toast('請輸入 Python 執行檔的完整路徑');return false;}
          const p=await window.easyreadDesktop.pickPython();if(p)s.cfg.docling_python=p;
        } else if(b.dataset.docling==='use') {
          s.cfg.docling_python=s.doclingResult.candidates[Number(b.dataset.index)].python;
        } else {
          b.disabled=true;b.textContent='離線檢查中…';
          s.doclingResult=await PR.api('/api/docling/environment',{method:'POST',body:{python:s.cfg.docling_python,refresh:true}});
        }
      } catch(err) {PR.toast(err.message);b.disabled=false;}
      return true;
    }
  };
})(window.PR);
