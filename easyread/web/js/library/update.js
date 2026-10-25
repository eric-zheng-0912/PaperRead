/* 新版本提示：打开文献库时问一次服务（服务一天最多问一次 GitHub），有新版本就在顶栏放一个“新版本 x.y.z”，
   第一次看到这个版本时再弹一条提示。点开看这次更新了什么，Windows 安装版直接点“更新”。
   帮助里有“检查更新”和“自动检查新版本”开关。 */
(function (PR) {
  "use strict";
  const SEEN = "easyread-seen-update";
  const desktop = /Electron/i.test(navigator.userAgent);
  const bridge = window.easyreadDesktop;
  let nativeUpdate = { supported: false, phase: "idle" };
  /* 弹窗下半部分。Windows 安装版：只有“稍后”和“更新”，按钮自己显示进度；其他平台没有应用内更新，给下载链接 */
  function renderNativeUpdate() {
    const status = document.querySelector("#upStatus"), foot = document.querySelector("#upFoot");
    if (!status || !foot) return;
    const u = PR.update, n = nativeUpdate, phase = n.phase;
    const pct = Math.floor(n.percent || 0);
    status.hidden = !n.supported || phase !== "downloading";
    status.innerHTML = '<div class="up-bar"><i style="width:' + pct + '%"></i></div>';
    if (!n.supported) {
      foot.innerHTML = '<p class="hint up-how">' + howTo() + '</p><button class="btn" data-close>' + PR.t("关闭") + '</button><a class="btn accent" href="' + PR.esc(u.url) + '" target="_blank" rel="noopener">' + PR.t("去下载") + "</a>";
      return;
    }
    const labels = { checking: PR.t("正在检查…"), downloading: PR.t("正在下载 {p}%", { p: pct }), downloaded: PR.t("重启并更新"), installing: PR.t("正在重启…"), error: PR.t("重试"), current: PR.t("已经是最新版") };
    const off = ["checking", "downloading", "installing", "current"].includes(phase);
    foot.innerHTML = (n.error ? '<p class="hint up-err">' + PR.esc(n.error) + "</p>" : "") +
      '<button class="btn" data-close>' + PR.t("稍后") + '</button><button class="btn accent" data-native-update' + (off ? " disabled" : "") + ">" + (labels[phase] || PR.t("更新")) + "</button>";
  }
  function acceptNativeUpdate(state) { nativeUpdate = state; renderNativeUpdate(); }
  /* 点一次“更新”：下载完直接重启安装（等下载那次调用返回再装，主进程那时才空出来）；
     后台正在翻译等原因装不了，会带着原因停在“重启并更新” */
  async function runUpdate() {
    try {
      if (nativeUpdate.phase !== "downloaded") acceptNativeUpdate(await bridge.downloadUpdate());
      if (nativeUpdate.phase === "downloaded") acceptNativeUpdate(await bridge.installUpdate());
    } catch (error) { acceptNativeUpdate({ ...nativeUpdate, phase: "error", error: error.message }); }
  }
  if (bridge && bridge.updateState) {
    bridge.onUpdateState(acceptNativeUpdate);
    bridge.updateState().then(acceptNativeUpdate).catch(() => {});
  }
  document.addEventListener("click", async event => {
    if (!event.target.closest("[data-native-update]")) return;
    runUpdate();
  });
  PR.update = null;

  const chip = PR.el("button", { class: "engine-chip update-chip", id: "updateChip", hidden: "" });
  PR.$("#engineChip").before(chip);
  chip.onclick = () => PR.openUpdate();

  /* Release 说明是 Markdown：只认段落、“- ”列表、**粗体**、[链接](地址)、`代码`，够用了 */
  function inline(s) {
    return PR.esc(s)
      .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
      .replace(/(^|[\s（(])(https?:\/\/[^\s<）)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
  }
  function notesHtml(md) {
    let h = "", list = false;
    for (const raw of (md || "").split(/\r?\n/)) {
      const line = raw.trim();
      const li = line.match(/^[-*] (.*)/);
      if (li) { if (!list) { h += "<ul>"; list = true; } h += "<li>" + inline(li[1]) + "</li>"; continue; }
      if (list) { h += "</ul>"; list = false; }
      if (!line) continue;
      const head = line.match(/^#{1,4} (.*)/);
      h += head ? "<h4>" + inline(head[1]) + "</h4>" : "<p>" + inline(line) + "</p>";
    }
    return h + (list ? "</ul>" : "");
  }

  function howTo() {
    return desktop ? PR.t("下载对应系统的安装包，装上就会覆盖旧版本，论文和设置都还在。")
      : PR.t("从源码运行的：下载新版 zip 解压后双击 start.cmd（macOS / Linux 运行 ./start.sh），或者在项目目录里 git pull；用 pip 装的：pip install -U easyread。论文和设置在数据目录里，不受影响。");
  }

  function show(u) {
    PR.update = u;
    const on = !!(u && u.newer);
    chip.hidden = !on;
    if (!on) return;
    chip.innerHTML = '<span class="dot"></span><span>' + PR.t("新版本 {v}", { v: PR.esc(u.latest) }) + "</span>";
    chip.title = PR.t("PaperRead {v} 已发布，点开看更新了什么", { v: u.latest });
    if (PR.ls.get(SEEN, "") !== u.latest) {  // 每个新版本只弹一次
      PR.ls.set(SEEN, u.latest);
      PR.toast(PR.t("PaperRead {v} 发布了", { v: PR.esc(u.latest) }), { label: PR.t("看看更新了什么"), fn: PR.openUpdate }, 9000);
    }
  }

  PR.openUpdate = function () {
    const u = PR.update;
    if (!u || !u.latest) return;
    const dlg = PR.$("#textDlg");
    dlg.querySelector(".dialog").innerHTML =
      '<div class="help-head">' + PR.logo("hero sm") + "<div><h2>PaperRead " + PR.esc(u.latest) + (u.newer ? PR.t(" 可以更新了") : "") + '</h2><div class="hint">' + PR.t("你现在用的是 {v}", { v: PR.esc(u.current) }) +
      (u.published ? PR.t(" · {date} 发布", { date: PR.esc(u.published.slice(0, 10)) }) : "") + "</div></div></div>" +
      '<div class="update-notes">' + (notesHtml(u.notes) || '<p class="hint">' + PR.t("这次没写更新说明。") + "</p>") + "</div>" +
      (u.newer ? '<div class="up-status" id="upStatus" hidden></div><div class="actions up-foot" id="upFoot"></div>'
        : '<div class="actions"><button class="btn" data-close>' + PR.t("关闭") + '</button><a class="btn accent" href="' + PR.esc(u.url) + '" target="_blank" rel="noopener">' + PR.t("在 GitHub 上看") + "</a></div>");
    dlg.classList.add("open");
    renderNativeUpdate();
  };

  /* 帮助里用：force 为真时马上问 GitHub */
  PR.checkUpdate = async function (force) {
    const u = await PR.api("/api/update" + (force ? "?force=1" : ""));
    show(u);
    return u;
  };
  PR.setAutoUpdate = async function (on) {
    show(await PR.api("/api/update", { method: "POST", body: { enabled: on } }));
  };

  /* 设置 → 阅读最下面：版本、检查更新、自动检查开关 */
  function updateLine() {
    const u = PR.update;
    if (!u) return "";
    if (u.manual_only) return PR.t("客製版僅支援手動更新；請登入私人下載倉庫，先備份再升級。");
    if (u.newer) return PR.t("有新版本 {v}", { v: '<a href="#" data-help="open">' + PR.esc(u.latest) + "</a>" });
    return u.latest ? PR.t("已经是最新版") : "";
  }
  PR.updateSection = () => '<h4 class="set-h">' + PR.t("版本") + '</h4><div class="help-update"><span>' + PR.esc(PR.lib.version || "") + '</span><p>' +
    PR.t("客製版僅支援手動更新；請登入私人下載倉庫，先備份再升級。") + '</p><a class="btn sm line" href="https://github.com/eric-zheng-0912/PaperRead-Private-Downloads/releases" target="_blank" rel="noopener">' + PR.t("在 GitHub 上看") + '</a></div>';
  document.addEventListener("click", async (e) => {
    const b = e.target.closest('[data-help="check"], [data-help="open"]');
    if (!b) return;
    e.preventDefault();
    if (b.dataset.help === "open") return PR.openUpdate();
    const msg = PR.$("#helpUpdateMsg");
    b.disabled = true; msg.textContent = PR.t("正在检查…");
    try {
      const u = await PR.checkUpdate(true);
      msg.innerHTML = u.manual_only || u.latest ? updateLine() : PR.t("没连上 GitHub，稍后再试");
    } catch (err) { msg.textContent = PR.t("检查失败：") + err.message; }
    b.disabled = false;
  });
  document.addEventListener("change", (e) => {
    if (e.target.dataset.help === "auto") PR.setAutoUpdate(e.target.checked).catch((err) => PR.toast(PR.t("保存失败：") + PR.esc(err.message)));
  });

  setTimeout(() => PR.checkUpdate(false).catch(() => {}), 1500);  // 等文献库先显示出来
})(window.PR);
