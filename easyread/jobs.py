"""后台任务：导入后的渲染与整篇翻译（一条队列，状态落在每篇的 job.json，重启后接着做），
以及“让模型回答我的问题 / 重译这段”这类小任务（另一条队列，不用等整篇翻译）。"""
from __future__ import annotations

import queue
import threading
import time
from uuid import uuid4

from . import background, chat_models, config, engines, langs, review_policy, translate, usage
from .engines import Cancelled, EngineError
from .i18n import tr
from .library import Library
from .log import log
from .store import DataReadError, Workspace, now_iso, dir_lock, read_json, write_json_atomic


def engine_for(cfg: dict, model: str | None) -> dict:
    """导入时选了“问 AI”名单里的模型就用它，否则用设置里的翻译引擎。名单里已经没有这个模型了，也用翻译引擎。"""
    m = next((x for x in chat_models.models(cfg) if x.get("id") == model), None) if model else None
    if not m:
        return cfg
    out, _ = chat_models.engine_cfg(cfg, model)
    o = cfg.get("openai") or {}
    if m["engine"] == "openai" and m.get("preset") == o.get("preset") and (m.get("model") or o.get("model")) == o.get("model"):
        out["openai"]["vision"] = o.get("vision", False)  # 和翻译引擎是同一个模型：沿用“模型能看图”
    return out


def _read_job(ws):
    # Validate core saved parts before changing a queued job or resuming work.
    for part in ("paper", "reader", "item", "discussion"):
        ws.load(part)
    return ws.load("job") or {}


class Jobs:
    def __init__(self, lib: Library):
        self.lib = lib
        self.bulk: queue.Queue[str] = queue.Queue()
        self.small: queue.Queue[dict] = queue.Queue()
        self.cancels: dict[str, threading.Event] = {}
        self.small_cancels: dict[str, threading.Event] = {}
        self.recent: list[dict] = []  # 小任务的状态，给页面轮询
        self.lock = threading.Lock()
        self._small_error = None
        self._session = uuid4().hex
        self._load_small()
        self._resume()
        for target in (self._bulk_loop, self._small_loop):
            threading.Thread(target=target, daemon=True).start()

    def _small_path(self):
        return self.lib.root / ".background-small-jobs.json"

    def _read_small(self):
        try:
            data = read_json(self._small_path(), {"version":1,"jobs":{}})
            if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict) or any(
                not isinstance(v, dict) or not all(k in v for k in ("id","pid","kind","state"))
                for v in data["jobs"].values()):
                raise ValueError("Invalid job status")
            return data
        except (OSError, ValueError, UnicodeError):
            self._small_error = "Unreadable background status; original preserved; repair and restart before submitting small jobs"
            log.exception(self._small_error)
            return None

    def _persist_small(self):
        with dir_lock(self.lib.root, ".background-small.lock"):
            data = self._read_small()
            if data is None:
                return
            for job in self.recent:
                old = data["jobs"].get(job["id"],{})
                if old and old.get("session") != self._session:
                    continue
                data["jobs"][job["id"]] = {k:v for k,v in job.items() if k in
                    ("id","kind","pid","state","message","at","library_scope","session","expires")}
            write_json_atomic(self._small_path(), data)

    def _load_small(self):
        with dir_lock(self.lib.root, ".background-small.lock"):
            data = self._read_small()
            if data is None:
                return
            self.recent = list(data["jobs"].values())[:50]
            for job in self.recent:
                if job.get("state") in ("queued","running","cancelling"):
                    job.update(state="cancelled", session=self._session, message="Interrupted by restart; submit explicitly to retry")
            if self.recent:
                write_json_atomic(self._small_path(), {"version":1,"jobs":{j["id"]:j for j in self.recent}})

    def _owns_small(self, job):
        current = read_json(self._small_path(), {"jobs":{}}).get("jobs",{}).get(job["id"],{})
        return current.get("session") == self._session and current.get("state") in ("queued","running")

    def _claim(self, ws, queued):
        def claim(current):
            if current.get('state') != 'queued' or current.get('run_id') != queued.get('run_id'):
                raise background.StaleWork('Job was claimed by another worker')
            current.update(run_id=uuid4().hex, state='running', cancel_requested=False, updated=now_iso())
            return dict(current)
        return ws.update('job', claim)

    def _owns_bulk(self, ws, job):
        current = ws.load('job') or {}
        if current.get('run_id') != job.get('run_id'):
            return False
        if current.get('cancel_requested') or current.get('state') == 'cancelled':
            raise Cancelled()
        return True

    # ---------- 整篇 ----------
    def _write(self, ws: Workspace, expected_run=None, **fields):
        def apply(job):
            if expected_run is not None and job.get("run_id") != expected_run:
                return
            job.update(fields)
            job["updated"] = now_iso()
        ws.update("job", apply)

    def enqueue(self, ws: Workspace, pages: list[int] | None = None, translate_after: bool = True, scope: str | None = None,
                read: bool = False, model: str = "", confirmed: bool = False, target: str = "", cap_check: bool = False,
                tables_only: bool = False, include_tables: bool = False, table_id: str | None = None):
        """pages=None：按 scope（all / body / range:A-B / first:N）翻译还没译的页。
        read：只读原文，用模型把页整理成段落、公式、表格，不翻译；之后再翻译时就地补中文。
        model：用“问 AI”名单里的哪个模型（导入时选的）；空着用设置里的翻译引擎。
        confirmed：只接受真正的 True；target：续跑时保留原任务的译文语言。
        cap_check：pages 来自系统计划而非用户指定，仍检查页数上限并跳过已完成页。"""
        if ws.root.parent != self.lib.root:
            raise ValueError("Workspace belongs to another library")
        baseline = background.snapshot(ws)
        target = langs.valid(target) if target else langs.of_paper((ws.load("paper") or {}).get("meta"))
        def apply(job):
            if job.get("state") in ("queued", "running"):
                raise ValueError(tr("这篇论文已有任务在排队或运行，请等它结束，或先取消再重试。"))
            job.update(expires=time.time()+21600, cancel_requested=False, run_id=uuid4().hex, library_scope=str(self.lib.root), _baseline=baseline, type="tables" if tables_only else "read" if read and translate_after else "translate" if translate_after else "prepare",
                       state="queued", message=tr("排队中"), pages=pages, scope=scope or "all", translate=translate_after, read=read, model=model or "",
                       confirmed=confirmed is True, target=target, cap_check=cap_check is True, tables_only=tables_only is True, include_tables=include_tables is True, table_id=table_id,
                       done=0, total=0, error="", failed={}, updated=now_iso(), usage={})
            job.pop("page_cap", None)
            job.pop("table_result", None)
        with self.lock:
            ws.update("job", apply)
            self.bulk.put(ws.id)

    def cancel(self, pid: str):
        with self.lock:
            ev = self.cancels.get(pid)
            if ev:
                ev.set()
            ws = self.lib.ws(pid)
            job = (ws.load("job") or {}) if ws else {}
            if job.get('state') == 'running':
                self._write(ws, expected_run=job.get('run_id'), cancel_requested=True)
            if job.get("state") == "queued":
                self._write(ws, state="cancelled", message=tr("已取消"))
            elif job.get("state") == "confirm":
                self._write(ws, state="done", message=tr("已导入，未整理") if job.get("read") else tr("已导入，未翻译"))

    def _resume(self):
        for ws in self.lib.all():
            try:
                job = _read_job(ws)
                if job.get("state") in ("queued", "running"):
                    if (review_policy.isolated() or job.get("state") == "running"
                            or not job.get('run_id') or not job.get('_baseline')):
                        # Preserve the existing isolated-profile restart policy.
                        self._write(ws, run_id=uuid4().hex, cancel_requested=True, state="cancelled",
                                    message="Interrupted or unverifiable job; completed data retained; retry explicitly")
                        continue
                    self._write(ws, run_id=uuid4().hex, _baseline=job.get("_baseline") or background.snapshot(ws), state="queued", message=tr("排队中（服务重启后继续）"))
                    self.bulk.put(ws.id)
            except (ValueError, OSError):
                log.exception("Skipping unreadable workspace %s", ws.id)
                continue

    def _bulk_loop(self):
        while True:
            pid = self.bulk.get()
            with self.lock:
                ws = self.lib.ws(pid)
                if not ws:
                    continue
                try:
                    job = _read_job(ws)
                    if job.get("state") != "queued":
                        continue
                    job = self._claim(ws, job)
                except DataReadError:
                    log.exception("Skipping unreadable queued workspace %s", pid)
                    continue
                except background.StaleWork:
                    continue
                cancel = self.cancels[pid] = background.CancelEvent()
            try:
                fence = background.Fence(ws, cancel, job.get("_baseline"),
                    owner=lambda: self._owns_bulk(ws, job), expires=job.get("expires"))
                with background.bind(fence):
                    self._run_bulk(ws, job, cancel)
            except Cancelled:
                try:
                    self._write(ws, expected_run=job.get("run_id"), state="cancelled", message=tr("已取消，已译的部分保留"))
                except DataReadError:
                    log.exception("Preserving unreadable cancelled job %s", pid)
            except DataReadError:
                # In particular, never try writing an error over a broken job.
                log.exception("Preserving unreadable running workspace %s", pid)
            except Exception as e:  # noqa: BLE001
                msg = str(e) if isinstance(e, (EngineError, KeyError, ValueError)) else f"{type(e).__name__}: {e}"
                try:
                    self._write(ws, expected_run=job.get("run_id"), state="error", message=tr("出错了"), error=msg[:800])
                except DataReadError:
                    log.exception("Preserving unreadable failed job %s", pid)
                log.exception("后台任务出错 %s", pid)
                try:
                    translate.journal(ws, tr("出错停止：{msg}", msg=msg[:500]))
                except OSError:
                    pass
            finally:
                with self.lock:
                    self.cancels.pop(pid, None)

    def _run_bulk(self, ws: Workspace, job: dict, cancel: threading.Event):
        cfg = engine_for(config.load(), job.get("model"))
        if job.get("target"):
            cfg = {**cfg, "target": langs.valid(job["target"])}
        if cancel.is_set():
            raise Cancelled()
        if job.get("tables_only"):
            from . import auto_tables
            if cfg.get("engine") == "none":
                raise ValueError(tr("表格補譯需要可用的翻譯模型。"))
            paper = ws.load("paper")
            wanted = job.get("pages")
            if wanted is None:
                wanted = [p["n"] for p in paper.get("meta", {}).get("pages", [])]
            cap = cfg.get("page_cap", 60)
            if type(cap) is not int or cap < 0:
                cap = 60
            if cap and len(wanted) > cap and (job.get("pages") is None or job.get("cap_check")) and job.get("confirmed") is not True:
                with self.lock:
                    if cancel.is_set():
                        raise Cancelled()
                    self._write(ws, state="confirm", pages=wanted, total=len(wanted), page_cap=cap, cap_check=True,
                                message=tr("這次要檢查 {n} 頁的表格，超過設定的 {cap} 頁，確認後再開始。", n=len(wanted), cap=cap))
                return
            meter = usage.Meter(cfg.get("engine") or "")
            def table_report(done, total, message):
                self._write(ws, done=done, total=total, message=message, usage=meter.snapshot())
            try:
                result = auto_tables.process(ws, cfg, wanted, cancel, table_report, meter, table_id=job.get('table_id'))
            finally:
                run = meter.snapshot()
                if run["calls"]:
                    ws.update("job", lambda j: j.update(usage=run, usage_total=usage.merge(j.get("usage_total"), run)))
            self._write(ws, state="partial" if result["failed"] or result["issues"] or result.get('pending') else "done", table_result=result,
                        failed={}, error="; ".join(list(result["failed"].values()) + result["issues"])[:800],
                        message=tr("表格補譯完成；請開啟「確認表格草稿」處理待確認位置，辨識失敗可使用手動框選。"))
            return
        if not ws.load("paper").get("meta", {}).get("pages"):
            self._write(ws, state="running", message=tr("正在渲染原页、抽取文字"))
            translate.prepare(ws)
        if cancel.is_set():
            raise Cancelled()
        read = bool(job.get("read"))
        if not job.get("translate") or cfg.get("engine") == "none":
            self._write(ws, state="done", message=tr("已导入（没有可用的模型，先放原页）") if read else tr("已导入（未开启自动翻译）") if job.get("translate") else tr("已导入"))
            return
        paper = ws.load("paper")
        all_pages = [p["n"] for p in paper["meta"]["pages"]]
        tl = paper.get("translation", {})
        # 只读原文：跳过已经整理过（或已经译过）的页；翻译：跳过已经有译文的页，只有原文的页会补译文
        skip = set(tl.get("done_pages", [])) if read else set(tl.get("done_pages", [])) - set(tl.get("en_pages", []))
        planned = job.get("pages") is not None
        wanted = job["pages"] if planned else translate.scope_pages(ws, job.get("scope")) or all_pages
        pages = wanted if planned and not job.get("cap_check") else [n for n in wanted if n not in skip]
        if not pages:
            if job.get('include_tables') and not read:
                # Use the same page-cap, metering and confirmation path as the table-only button.
                self._run_bulk(ws, {**job, 'tables_only': True, 'pages': wanted, 'cap_check': True}, cancel)
                return
            self._write(ws, state="done", message=tr("选定范围已整理完") if read else tr("选定范围已译完"))
            return

        cap = cfg.get("page_cap", 60)
        if type(cap) is not int or cap < 0:
            cap = 60  # 手改配置写错时仍按默认上限保护，不把错误值当作不限。
        check_cap = job.get("cap_check") or (not planned and (job.get("scope") or "all") in ("all", "body"))
        if cap > 0 and len(pages) > cap and check_cap and job.get("confirmed") is not True:
            with self.lock:
                if cancel.is_set():
                    raise Cancelled()
                self._write(ws, state="confirm", total=len(pages), page_cap=cap, pages=pages, cap_check=True,
                            message=tr("这篇要整理 {n} 页，超过了你设的 {cap} 页，确认后再开始", n=len(pages), cap=cap) if read
                            else tr("这篇要译 {n} 页，超过了你设的 {cap} 页，确认后再开始", n=len(pages), cap=cap))
            return

        meter = usage.Meter(cfg.get("engine") or "")

        def report(done, total, message):
            if cancel.is_set():
                raise Cancelled()
            self._write(ws, state="running", done=done, total=total, message=message, usage=meter.snapshot())

        started = time.time()
        try:
            failed = translate.translate_pages(ws, cfg, pages, cancel, report, meter, read)
            if job.get('include_tables') and not read and langs.of_paper(ws.load("paper").get("meta")) == "zh-TW" and ws.source_pdf.exists():
                from . import auto_tables
                table_result = auto_tables.process(ws, cfg, [p for p in pages if p not in failed], cancel,
                                                                 lambda d, t, m: report(len(pages), len(pages), m), meter)
                self._write(ws, table_result=table_result)
        finally:  # 取消、出错也把已经花掉的记上
            run = meter.snapshot()
            if run["calls"]:
                ws.update("job", lambda j: j.update(usage=run, usage_total=usage.merge(j.get("usage_total"), run)))
        minutes = max(1, round((time.time() - started) / 60))
        if failed:
            first = next(iter(failed.values()))
            self._write(ws, state="partial", failed={str(k): v for k, v in failed.items()}, error=first,
                        message=(tr("{ok} 页好了，", ok=len(pages) - len(failed)) if len(failed) < len(pages) else "")
                        + (tr("{n} 页没整理成功", n=len(failed)) if read else tr("{n} 页没译成功", n=len(failed))))
        else:
            self._write(ws, state="done", failed={}, error="",
                        message=tr("原文整理完成（{n} 页，用时约 {m} 分钟）", n=len(pages), m=minutes) if read
                        else tr("翻译完成（{n} 页，用时约 {m} 分钟）", n=len(pages), m=minutes))
        table_result = (ws.load("job") or {}).get("table_result")
        if not failed and table_result and (table_result.get("failed") or table_result.get("issues") or table_result.get('pending')):
            self._write(ws, state='partial', message=tr("正文已完成；表格待確認位置請開啟「確認表格草稿」，辨識失敗可使用手動框選。"))

    # ---------- 小任务 ----------
    def submit_small(self, kind: str, pid: str, **kw) -> dict:
        if self._small_error:
            raise ValueError(self._small_error)
        ws = self.lib.ws(pid)
        if ws is None:
            raise ValueError("Workspace belongs to another library")
        job = {"expires":time.time()+21600, "session":self._session, "_baseline":background.snapshot(ws), "library_scope":str(self.lib.root), "id": "j" + uuid4().hex, "kind": kind, "pid": pid, "state": "queued", "message": tr("排队中"),
               "at": now_iso(), **kw}
        with self.lock:
            self.recent = ([job] + self.recent)[:50]
            self._persist_small()
        self.small.put(job)
        return job

    def cancel_small(self, job_id: str) -> bool:
        """取消仍在執行的小任務；已寫入的候選會保留。"""
        with self.lock:
            job = next((x for x in self.recent if x.get("id") == job_id), None)
            if not job or job.get("state") not in ("queued", "running"):
                return False
            self.small_cancels.setdefault(job_id, background.CancelEvent()).set()
            job["message"] = tr("正在取消")
            return True

    def small_status(self, pid: str | None = None) -> list[dict]:
        with self.lock:
            return [{k:v for k,v in j.items() if not k.startswith("_")} for j in self.recent if not pid or j["pid"] == pid]

    def busy(self) -> bool:
        """还有整篇翻译或小任务没做完（关页自动退出时要等它们）。"""
        if self.cancels or not self.bulk.empty() or not self.small.empty():
            return True
        with self.lock:
            return any(j["state"] in ("queued", "running") for j in self.recent)

    def _small_loop(self):
        while True:
            job = self.small.get()
            ws = self.lib.ws(job["pid"])
            if not ws:
                job["state"], job["message"] = "error", tr("文献已移除，无法执行任务")
                continue
            cancel = self.small_cancels.setdefault(job["id"], background.CancelEvent())
            job["state"], job["message"] = "running", tr("模型思考中")
            self._persist_small()
            try:
                if cancel.is_set():
                    raise Cancelled()
                fence = background.Fence(ws, cancel, job.get("_baseline"), owner=lambda:self._owns_small(job), expires=job.get("expires"), registry=self.lib.root)
                with background.bind(fence):
                    cfg = config.load()
                    if job["kind"] == "answer":
                        translate.answer(ws, cfg, job["note"], cancel)
                    elif job["kind"] == "retranslate":
                        translate.retranslate(ws, cfg, job["key"], job.get("hint", ""), cancel)
                    elif job["kind"] == "smart-annotate":
                        from . import smart_annotations
                        result = smart_annotations.generate(
                            ws, scope=job.get("scope", "all"), section_id=job.get("section_id", ""),
                            mode=job.get("mode", "auto"), purpose=job.get("purpose", ""),
                            model_id=job.get("model", ""), cancel=cancel,
                        )
                        job["result"] = result
                    elif job["kind"] == "simple-table":
                        from . import simple_tables
                        job["result"] = simple_tables.translate(
                            ws, engines.for_translation(cfg), job["regions"], job["source_hash"], cancel,
                            refresh=job.get("refresh") is True,
                        )
                    if cancel.is_set():
                        raise Cancelled()
                    with self.lock:
                        job["state"], job["message"] = "done", tr("完成")
                        self._persist_small()
            except Cancelled:
                with self.lock:
                    job["state"], job["message"] = "cancelled", tr("已取消")
                    self._persist_small()
            except Exception as e:  # noqa: BLE001
                with self.lock:
                    job["state"], job["message"] = "error", str(e)[:500]
                    self._persist_small()
                log.exception("小任务出错 %s", job.get("kind"))
            finally:
                with self.lock:
                    self.small_cancels.pop(job["id"], None)
                    self._persist_small()
