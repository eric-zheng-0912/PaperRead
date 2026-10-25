/* Incremental v1.5.1 research UI. Uses the local scoped task registry unchanged. */
(function (PR) {
  "use strict";
  const API = "/api/research/v2/ui";
  let scope = "", bridge = null, path = null, selected = "", generation = 0, busy = false;
  let view = { scale: 1, panX: 0, panY: 0 }, latestTasks = [], focusBefore = null, observedCompletion = "", invalidated = false;
  const active = (task) => ["queued", "running", "cancelling"].includes(task.state);
  const safeUrl = (value) => { try { const u = new URL(String(value)); return ["http:", "https:"].includes(u.protocol) ? u.href : ""; } catch (_) { return ""; } };
  const el = (tag, text, cls) => { const node = document.createElement(tag); if (text != null) node.textContent = String(text); if (cls) node.className = cls; return node; };
  const button = (text, handler) => { const node = el("button", text, "btn sm line"); node.type = "button"; node.onclick = handler; return node; };
  const scoped = async (endpoint, body) => {
    const expected = scope, epoch = generation;
    if (!expected) throw Error(PR.t("請重新載入文獻庫"));
    const result = await PR.api(API + endpoint + (body ? "" : "?scope=" + encodeURIComponent(expected)),
      body ? { method: "POST", body: { ...body, scope: expected } } : undefined);
    if (epoch !== generation || expected !== scope || result.scope !== expected) throw Error(PR.t("文獻庫已切換，請重新載入"));
    return result;
  };
  function invalidate(message) {
    generation++; scope = ""; path = null; latestTasks = []; invalidated = true; closePeek();
    document.getElementById("pr-research-notifications")?.replaceChildren();
    const host = document.querySelector(".rw-shell");
    if (host) { host.replaceChildren(el("p", message)); host.append(button(PR.t("重新載入"), () => location.reload())); }
  }
  async function checked() {
    const epoch = generation;
    const data = await PR.api(API + "/monitor" + (scope ? "?scope=" + encodeURIComponent(scope) : ""));
    if (epoch !== generation) return null;
    if (scope && scope !== data.scope) { invalidate(PR.t("文獻庫已切換")); return null; }
    scope = data.scope;
    latestTasks = (data.tasks || []).filter((task) => task.scope === scope);
    renderNotifications();
    return data;
  }
  function ackKey() { return "paperread:research-ui:ack:" + scope; }
  function acknowledged() { try { return JSON.parse(localStorage.getItem(ackKey()) || "[]"); } catch (_) { return []; } }
  function acknowledge(id) { try { localStorage.setItem(ackKey(), JSON.stringify([...new Set([...acknowledged(), id])].slice(-100))); } catch (_) {} renderNotifications(); }
  function renderNotifications() {
    let host = document.getElementById("pr-research-notifications");
    if (!host) { host = el("aside", null, "pr-research-notifications"); host.id = "pr-research-notifications"; host.setAttribute("aria-label", PR.t("背景研究工作")); document.body.append(host); }
    host.replaceChildren();
    const seen = acknowledged();
    for (const task of latestTasks.filter((t) => ["comparison", "reading-path"].includes(t.kind) && (active(t) || !seen.includes(t.id))).slice(0, 4)) {
      const row = el("div", null, "pr-research-notification"); row.setAttribute("role", "status");
      const label = task.kind === "reading-path" ? PR.t("閱讀路徑") : PR.t("跨論文比較");
      row.append(el("strong", label + " · " + task.state), el("p", task.message || task.phase));
      if (task.total > 0) { const progress = el("progress"); progress.max = task.total; progress.value = task.done || 0; row.append(progress); }
      if (active(task)) row.append(button(PR.t("取消"), async () => {
        try { await scoped("/cancel", { id: task.id }); await checked(); } catch (e) { PR.toast(PR.esc(String(e.message))); }
      }));
      else {
        const link = el("a", task.state === "completed" ? PR.t("查看結果") : PR.t("返回重試"), "btn sm line");
        link.href = "/research?mode=" + (task.kind === "reading-path" ? "path" : "comparisons"); row.append(link, button(PR.t("關閉通知"), () => acknowledge(task.id)));
      }
      host.append(row);
    }
  }
  async function loadPath() {
    const result = await scoped("/path");
    path = result;
    if (bridge) { bridge.state.pathResults = result.results || []; bridge.renderPath(); }
  }
  async function poll() {
    if (busy || invalidated) return;
    busy = true;
    try {
      const data = await checked();
      if (data && bridge) {
        const task = latestTasks.find((t) => t.kind === "reading-path");
        if (task?.state === "completed" && task.id !== observedCompletion) {
          observedCompletion = task.id;
          if (task.result?.revision !== path?.revision) await loadPath();
        }
      }
    } catch (_) { invalidate(PR.t("無法確認文獻庫作用域，已隱藏研究結果；請重新載入。")); }
    finally { busy = false; }
  }
  function closePeek() { document.getElementById("pr-quick-peek")?.remove(); if (focusBefore?.isConnected) focusBefore.focus(); focusBefore = null; }
  async function peek(id) {
    closePeek(); focusBefore = document.activeElement;
    const epoch = ++peek.epoch;
    const dialog = el("dialog"); dialog.id = "pr-quick-peek"; dialog.setAttribute("aria-label", "Quick Peek");
    dialog.append(button(PR.t("關閉"), closePeek), el("p", PR.t("載入中…"))); document.body.append(dialog); dialog.showModal();
    dialog.addEventListener("cancel", (e) => { e.preventDefault(); closePeek(); });
    try {
      const task = bridge.tasks().find((t) => t.id === id);
      if (!task) throw Error(PR.t("找不到文獻"));
      let data;
      if (task.saved) data = { meta: task.metadata || {}, item: task, records: [], questions: [] };
      else data = await scoped("/peek/" + encodeURIComponent(id));
      if (epoch !== peek.epoch || !dialog.isConnected) return;
      dialog.replaceChildren(button(PR.t("關閉"), closePeek));
      const meta = data.meta;
      dialog.append(el("h2", task.title_zh || task.title_en || task.title || meta.title_zh || meta.title_en || meta.title),
        el("h3", PR.t("概覽")), el("p", [task.authors, task.year, data.item.status, ...(data.item.tags || [])].filter(Boolean).join(" · ")));
      const summary = bridge.state.summaries.find((s) => [task.paper_ref, task.saved_key].includes(s.paper_key));
      dialog.append(el("h3", PR.t("摘要")), el("p", data.summary || summary?.summary_markdown || meta.abstract_zh || meta.abstract || PR.t("尚無摘要；不自動呼叫 AI")));
      for (const [title, records] of [[PR.t("閱讀紀錄"), data.records], [PR.t("最近問題"), data.questions]]) {
        dialog.append(el("h3", title));
        if (!records.length) dialog.append(el("p", PR.t("尚無紀錄")));
        for (const record of records) {
          const node = el(record.anchor ? "a" : "p", record.body || record.quote || PR.t("無文字"));
          if (record.anchor) node.href = "/read/" + encodeURIComponent(id) + "#b-" + encodeURIComponent(record.anchor);
          dialog.append(node);
        }
      }
      if (!task.saved) { const link = el("a", PR.t("開啟原文定位"), "btn line"); link.href = "/read/" + encodeURIComponent(id); dialog.append(link); }
    } catch (e) { if (dialog.isConnected) dialog.replaceChildren(button(PR.t("關閉"), closePeek), el("p", e.message)); }
  }
  peek.epoch = 0;
  function renderPath() {
    if (!bridge || !window.PRPathMap) return;
    const host = document.querySelector("[data-view='path']");
    if (!host || !path?.results?.length) return;
    host.querySelector(".pr-path-interactive")?.remove();
    const panel = el("section", null, "rw-panel pr-path-interactive");
    const toolbar = el("div", null, "pr-path-toolbar"); panel.append(toolbar);
    let showRelations = true, showSequence = true;
    const map = el("div"); panel.append(map);
    const detail = el("div"); panel.append(detail);
    const draw = () => {
      map.innerHTML = window.PRPathMap.render({ steps: path.results, relations: path.relations, esc: PR.esc, selectedKey: selected,
        showSequence, showRelations, view, labels: { mapLabel: PR.t("閱讀路徑地圖"), mapHelp: PR.t("拖曳平移，方向鍵移動；使用縮放按鈕"), paper: PR.t("文獻") } });
      window.PRPathMap.bind(map, view, (v) => { view = v; });
      map.querySelectorAll("[data-path-key]").forEach((node) => { node.onclick = () => { selected = node.dataset.pathKey; draw(); }; });
      detail.replaceChildren();
      const index = path.results.findIndex((s) => s.paper_key === selected), step = path.results[index];
      if (!step) return;
      detail.append(el("h3", step.paper.title), el("p", step.reason));
      const url = safeUrl(step.paper.url); if (url) { const a = el("a", PR.t("論文來源")); a.href = url; a.target = "_blank"; a.rel = "noopener"; detail.append(a); }
      for (const [label, delta] of [[PR.t("往前閱讀"), -1], [PR.t("往後閱讀"), 1]]) {
        const b = button(label, async () => {
          const keys = path.results.map((s) => s.paper_key), at = index + delta;
          [keys[index], keys[at]] = [keys[at], keys[index]]; b.disabled = true;
          try { path = await scoped("/path/reorder", { keys, revision: path.revision }); bridge.state.pathResults = path.results; bridge.renderPath(); }
          catch (e) { PR.toast(PR.esc(String(e.message))); await loadPath(); }
        }); b.disabled = index + delta < 0 || index + delta >= path.results.length; detail.append(b);
      }
    };
    toolbar.append(el("span", PR.t("實線：閱讀順序；虛線：來源引用。缺乏引用資料時不推測關係。")));
    for (const [label, factor] of [[PR.t("放大"), 1.2], [PR.t("縮小"), 1 / 1.2]]) toolbar.append(button(label, () => { view.scale = window.PRPathMap.clamp(view.scale * factor, .35, 2); draw(); }));
    toolbar.append(button(PR.t("重設視角"), () => { view = { scale: 1, panX: 0, panY: 0 }; draw(); }), button(PR.t("閱讀順序"), () => { showSequence = !showSequence; draw(); }), button(PR.t("引用關係"), () => { showRelations = !showRelations; draw(); }));
    host.querySelector(".rw-path-list")?.before(panel); draw();
  }
  PR.researchUI = { safeUrl, renderPath, async attach(value) { bridge = value; await checked(); await loadPath(); },
    async startPath(request) { await scoped("/path/start", { request }); await checked(); } };
  document.addEventListener("click", (event) => { const b = event.target.closest("[data-research-peek]"); if (b) peek(b.dataset.researchPeek); });
  if (location.protocol === "http:") {
    PR.api("/api/library").then((lib) => { PR.token = lib.token; return poll(); }).catch(() => {});
    setInterval(poll, 2500);
  }
})(window.PR);
