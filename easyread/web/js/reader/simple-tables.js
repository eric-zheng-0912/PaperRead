/* Source-region translation. Model text is always inserted with textContent. */
(function (PR) {
  "use strict";
  const make = (tag, text, parent) => {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    if (parent) parent.appendChild(node);
    return node;
  };
  const panel = make("section");
  panel.id = "simple-table-panel"; panel.hidden = true;
  panel.setAttribute("aria-label", PR.t("簡易表格翻譯"));
  document.body.appendChild(panel);
  const head = make("div", "", panel); head.className = "simple-table-tools";
  make("strong", PR.t("簡易表格翻譯"), head);
  const button = (label, parent, fn) => {
    const b = make("button", label, parent); b.className = "btn sm";
    b.type = "button"; b.addEventListener("click", () => Promise.resolve().then(fn).catch(fail));
    return b;
  };
  button(PR.t("關閉"), head, () => { panel.hidden = true; stopPicking(); });
  make("p", PR.t("僅翻譯所選範圍；整表完整性未確認。可跨頁追加，系統不推斷欄位或補造內容。"), panel);
  const tools = make("div", "", panel); tools.className = "simple-table-tools";
  button(PR.t("本頁自動候選"), tools, loadCandidates);
  const pick = button(PR.t("手動框選／追加"), tools, () => {
    picking = !picking; document.body.classList.toggle("simple-table-picking", picking);
    message.textContent = picking ? PR.t("請在右側 PDF 拖曳框選；可換頁再次追加。") : PR.t("已結束框選。");
  });
  button(PR.t("清空選取"), tools, () => { regions = []; invalidate(); renderRegions(); });
  button(PR.t("預覽英文"), tools, showPreview);
  const translateButton = button(PR.t("翻譯為繁體中文"), tools, startTranslation);
  translateButton.disabled = true;
  const cancelButton = button(PR.t("取消翻譯"), tools, async () => {
    if (activeJob) await api({ action: "cancel", job: activeJob });
  });
  cancelButton.disabled = true;
  const message = make("p", "", panel); message.setAttribute("role", "status");
  const choices = make("div", "", panel);
  const selections = make("div", "", panel);
  const content = make("div", "", panel); content.setAttribute("data-no-i18n", "");
  make("h3", PR.t("已儲存的簡易譯文"), panel);
  const history = make("div", "", panel);
  let regions = [], evidence = null, picking = false, activeJob = null, submitting = false, revision = 0;
  const api = (body) => PR.api("/api/p/" + PR.pid + "/simple-table", { method: "POST", body });
  function fail(error) { message.textContent = error.message || String(error); }
  function stopPicking() { picking = false; document.body.classList.remove("simple-table-picking"); }
  function invalidate() {
    revision++; evidence = null; translateButton.disabled = true; content.replaceChildren();
  }
  function addRegion(region) {
    regions.push({ page: region.page, box: [...region.box] }); invalidate(); renderRegions();
    message.textContent = PR.t("已追加範圍，請預覽英文。");
  }
  function renderRegions() {
    selections.replaceChildren();
    regions.forEach((r, i) => {
      const row = make("div", PR.t("範圍 {n} · PDF p.{page}", { n: i + 1, page: r.page }) + " ", selections);
      button(PR.t("查看"), row, () => PR.openPage(r.page, null, { page: r.page, box: r.box }));
      button(PR.t("移除"), row, () => { regions.splice(i, 1); invalidate(); renderRegions(); });
    });
  }
  function renderHistory(records) {
    history.replaceChildren();
    records.forEach((record, i) => button(PR.t("譯文 {n} · p.{pages}", { n: i + 1, pages: record.source.regions.map(r => r.page).join(",") }), history, async () => {
      regions = record.source.regions.map(r => ({ page: r.page, box: [...r.box] }));
      invalidate(); renderRegions(); await showPreview();
      // Bind the displayed translation to the exact preview, including PDF bytes.
      if (evidence?.source_hash === record.id) renderText(record);
    }));
  }
  async function loadCandidates() {
    const result = await api({ action: "candidates", page: PR.currentPdfPage() });
    choices.replaceChildren();
    result.candidates.forEach((r, i) => button(PR.t("追加候選 {n}", { n: i + 1 }), choices, () => addRegion(r)));
    renderHistory(result.saved);
    message.textContent = result.candidates.length ? PR.t("候選範圍尚未確認，請預覽並與 PDF 核對。") : PR.t("找不到可靠候選，請手動框選。");
  }
  function renderText(record) {
    content.replaceChildren();
    const values = record.table_reading_zh;
    record.source.regions.forEach((region, i) => {
      make("h3", PR.t("範圍 {n} · PDF p.{page}", { n: i + 1, page: region.page }), content);
      if (evidence?.previews?.[i]) {
        const image = make("img", "", content); image.src = evidence.previews[i]; image.alt = PR.t("選取的原始 PDF 範圍");
      }
      make("h4", PR.t("英文原文"), content);
      record.source.segments.filter(s => s.region === i).forEach(s => make("p", s.en, content));
      if (values) {
        make("h4", PR.t("繁體中文譯文"), content);
        record.source.segments.forEach((s, j) => { if (s.region === i) make("p", values[j].zh, content); });
      }
    });
  }
  async function showPreview() {
    if (!regions.length) throw new Error(PR.t("請先追加候選或框選 PDF 區域。"));
    const generation = revision;
    const result = await api({ action: "preview", regions });
    if (generation !== revision) return;
    evidence = result; renderText(result); translateButton.disabled = !!activeJob;
    message.textContent = PR.t("請核對英文與原始 PDF。翻譯涵蓋選框內的可擷取文字，未確認整張邏輯表完整。");
  }
  async function startTranslation() {
    if (!evidence || activeJob || submitting) return;
    submitting = true; translateButton.disabled = true;
    let job;
    try { job = await api({ action: "translate", regions: evidence.source.regions, source_hash: evidence.source_hash }); }
    finally { submitting = false; translateButton.disabled = !evidence || !!activeJob; }
    activeJob = job.id; translateButton.disabled = true; cancelButton.disabled = false;
    message.textContent = PR.t("正在翻譯；原始 PDF 與既有論文內容保留。");
    await poll();
  }
  async function poll() {
    try {
      const result = await api({ action: "status" }); renderHistory(result.saved);
      const job = result.jobs.find(j => j.id === activeJob);
      if (job && ["done", "error", "cancelled"].includes(job.state)) {
        activeJob = null; cancelButton.disabled = true; translateButton.disabled = !evidence;
        message.textContent = job.state === "done" ? PR.t("譯文已儲存；僅涵蓋所選範圍。") : job.message;
        const record = result.saved.find(r => r.id === evidence?.source_hash);
        if (record) renderText(record);
      }
    } catch (error) { fail(error); }
    if (activeJob) setTimeout(poll, 1000);
  }
  PR.openSimpleTables = async function () {
    if (PR.store.mode !== "server") { PR.toast(PR.t("簡易表格翻譯需要本機服務。")); return; }
    panel.hidden = false; PR.openSide("pages");
    try {
      await loadCandidates();
      if (!activeJob) {
        const status = await api({ action: "status" });
        const pending = status.jobs.find(j => ["queued", "running"].includes(j.state));
        if (pending) { activeJob = pending.id; translateButton.disabled = true; cancelButton.disabled = false; await poll(); }
      }
    } catch (error) { fail(error); }
  };
  document.getElementById("simple-table-open")?.addEventListener("click", PR.openSimpleTables);
  // Capture gestures before PDF's normal paragraph navigation handles them.
  let drag = null, overlay = null;
  const clamp = n => Math.max(0, Math.min(1, n));
  const point = (e, node) => { const r = node.getBoundingClientRect(); return [clamp((e.clientX-r.left)/r.width), clamp((e.clientY-r.top)/r.height)]; };
  function draw(to) {
    const [x, y] = drag.start;
    Object.assign(overlay.style, { left: Math.min(x,to[0])*100+"%", top: Math.min(y,to[1])*100+"%",
      width: Math.abs(x-to[0])*100+"%", height: Math.abs(y-to[1])*100+"%" });
  }
  document.addEventListener("pointerdown", e => {
    const node = e.target.closest(".pv-page[data-n]");
    if (!picking || !node || e.button !== 0) return;
    e.preventDefault(); e.stopImmediatePropagation();
    drag = { node, page: Number(node.dataset.n), start: point(e,node), pointer: e.pointerId };
    node.setPointerCapture(e.pointerId);
    overlay = make("div", "", node); overlay.className = "simple-table-selection"; draw(drag.start);
  }, true);
  document.addEventListener("pointermove", e => {
    if (!drag) return; e.preventDefault(); e.stopImmediatePropagation(); draw(point(e,drag.node));
  }, true);
  document.addEventListener("pointerup", e => {
    if (!drag) return; e.preventDefault(); e.stopImmediatePropagation();
    const to = point(e,drag.node), start = drag.start;
    const region = { page: drag.page, box: [Math.min(start[0],to[0]),Math.min(start[1],to[1]),Math.max(start[0],to[0]),Math.max(start[1],to[1])] };
    drag.node.releasePointerCapture(drag.pointer); overlay.remove(); drag = null;
    if (region.box[2]-region.box[0] > .003 && region.box[3]-region.box[1] > .003) addRegion(region);
  }, true);
  document.addEventListener("click", e => {
    if (picking && e.target.closest(".pv-page")) { e.preventDefault(); e.stopImmediatePropagation(); }
  }, true);
  document.addEventListener("pointercancel", () => { if (overlay) overlay.remove(); drag = null; });
})(window.PR);
