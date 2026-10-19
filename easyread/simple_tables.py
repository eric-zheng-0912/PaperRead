"""Selected PDF text translation, independent of rectangular table recognition.

Regions are page-local evidence, never a claim that a logical table is complete.
Only this module's separate paper field is written; legacy blocks stay intact.
"""
from __future__ import annotations

import copy
import hashlib
import io
from collections import Counter

import pdfplumber

from . import pdfwork, table_reading, table_translate
from .i18n import tr
from .table_extract import _CAPTION, _NOTE, _lines, _visible_page

VERSION = 1
FIELD = "simple_table_translations"


def extract(ws, regions):
    if not isinstance(regions, list) or not 1 <= len(regions) <= 12:
        raise ValueError(tr("請選擇 1 至 12 個 PDF 區域。"))
    data = ws.source_pdf.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    selected, segments = [], []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for index, region in enumerate(regions):
            if (not isinstance(region, dict) or set(region) != {"page", "box"} or
                    type(region["page"]) is not int or not 1 <= region["page"] <= len(pdf.pages) or
                    not table_reading._box(region["box"])):
                raise ValueError(tr("無效的 PDF 選取區域。"))
            page = _visible_page(pdf.pages[region["page"] - 1])
            box = region["box"]
            absolute = (page.bbox[0]+box[0]*page.width, page.bbox[1]+box[1]*page.height,
                        page.bbox[0]+box[2]*page.width, page.bbox[1]+box[3]*page.height)
            for previous in selected:
                pb = previous["box"]
                if (previous["page"] == region["page"] and
                        max(pb[0], box[0]) < min(pb[2], box[2]) and max(pb[1], box[1]) < min(pb[3], box[3])):
                    raise ValueError(tr("選取範圍重疊，請調整以避免重複翻譯。"))
            chars = [c for c in page.chars if c["text"].strip() and
                     c["x0"] < absolute[2] and c["x1"] > absolute[0] and
                     c["top"] < absolute[3] and c["bottom"] > absolute[1]]
            if not chars:
                raise ValueError(tr("此區域沒有可擷取文字；掃描影像請查看原始 PDF。"))
            if any(not (absolute[0] <= c["x0"] < c["x1"] <= absolute[2] and
                        absolute[1] <= c["top"] < c["bottom"] <= absolute[3]) for c in chars):
                raise ValueError(tr("選框切到文字，請擴大或調整範圍。"))
            if any(not c.get("upright", True) or "\ufffd" in c["text"] or
                   "(cid:" in c["text"] or any(ord(ch) < 32 for ch in c["text"]) for c in chars):
                raise ValueError(tr("文字層無法可靠解碼或包含旋轉文字，請查看原始 PDF。"))
            lines = _lines(page.crop(absolute))
            text = [line["text"] for line in lines if line["text"].strip()]
            def canonical(value):
                ligatures = str.maketrans({"\ufb00":"ff", "\ufb01":"fi", "\ufb02":"fl",
                    "\ufb03":"ffi", "\ufb04":"ffl", "\ufb05":"st", "\ufb06":"st"})
                return Counter(ch for ch in value.translate(ligatures) if not ch.isspace())
            if canonical("".join(text)) != canonical("".join(c["text"] for c in chars)):
                raise ValueError(tr("擷取文字與來源不一致，未送出翻譯。"))
            selected.append(copy.deepcopy(region))
            for line_index, en in enumerate(text):
                segments.append({"id": f"r{index}-l{line_index}", "en": en,
                                 "region": index, "page": region["page"]})
    if sum(len(s["en"]) for s in segments) > 60000:
        raise ValueError(tr("選取文字過多，請分批翻譯。"))
    source = {"version": VERSION, "pdf_sha256": digest, "regions": selected,
              "segments": segments, "coverage": "selected_regions_only"}
    return {"source": source, "source_hash": table_translate._hash(source)}


def candidates(ws, page_number):
    """Unconfirmed suggestions only. No matrix, numeric-row or cell checks."""
    with pdfplumber.open(ws.source_pdf) as pdf:
        if type(page_number) is not int or not 1 <= page_number <= len(pdf.pages):
            raise ValueError(tr("PDF 頁碼無效。"))
        page = _visible_page(pdf.pages[page_number - 1])
        lines = _lines(page)
        boxes = []
        # Existing vector geometry helps locate regions, not validate cells.
        for table in page.find_tables():
            box = list(table.bbox)
            nearby = [ln for ln in lines if _CAPTION.match(ln["text"]) and
                      0 <= box[1] - ln["bottom"] < 40]
            if nearby:
                cap = nearby[-1]
                box[0] = min(box[0], cap["words"][0]["x0"])
                box[1] = cap["top"]
                box[2] = max(box[2], cap["words"][-1]["x1"])
            notes = False
            for line in lines:
                if line["top"] < box[3]:
                    continue
                if line["top"] - box[3] > 18 or _CAPTION.match(line["text"]):
                    break
                if not notes and not _NOTE.match(line["text"]):
                    break
                notes = True
                box[0] = min(box[0], line["words"][0]["x0"])
                box[2] = max(box[2], line["words"][-1]["x1"])
                box[3] = line["bottom"]
            boxes.append(box)
        for i, cap in enumerate(lines):
            if not _CAPTION.match(cap["text"]):
                continue
            if any(b[1] <= cap["top"] <= b[3] for b in boxes):
                continue
            # A caption anchors a text-group suggestion; user reviews the bounds.
            group = [cap]
            for line in lines[i+1:]:
                if _CAPTION.match(line["text"]):
                    break
                height = max(1, group[-1]["bottom"] - group[-1]["top"])
                if line["top"] - group[-1]["bottom"] > 3 * height:
                    break
                group.append(line)
                if _NOTE.match(line["text"]):
                    break
            if len(group) > 1:
                boxes.append([min(w["x0"] for ln in group for w in ln["words"]), cap["top"],
                              max(w["x1"] for ln in group for w in ln["words"]), group[-1]["bottom"]])
        result = []
        for box in boxes[:30]:
            # Small padding keeps border glyphs wholly inside the suggestion.
            norm = [max(0, (box[0]-page.bbox[0]-1)/page.width),
                    max(0, (box[1]-page.bbox[1]-1)/page.height),
                    min(1, (box[2]-page.bbox[0]+1)/page.width),
                    min(1, (box[3]-page.bbox[1]+1)/page.height)]
            result.append({"page": page_number, "box": norm})
        return result


def preview(ws, regions):
    evidence = extract(ws, regions)
    data = ws.source_pdf.read_bytes()
    if hashlib.sha256(data).hexdigest() != evidence["source"]["pdf_sha256"]:
        raise ValueError(tr("來源 PDF 已變更，請重新預覽。"))
    return {**evidence, "previews": [pdfwork.reading_preview(data, r["page"], r["box"])
                                    for r in regions]}


def translate(ws, cfg, regions, source_hash, cancel=None, run=None):
    if cfg.get("engine") == "none":
        raise ValueError(tr("請先設定翻譯模型。"))
    with table_translate._job_lock(ws, cancel):
        evidence = extract(ws, regions)
        if evidence["source_hash"] != source_hash:
            raise ValueError(tr("來源或選框已變更，請重新預覽英文。"))
        paper = ws.load("paper")
        previous = next((v for v in paper.get(FIELD, []) if v.get("id") == source_hash), {})
        old = copy.deepcopy(previous)
        values, key, cached = table_translate.translate_segments(ws, cfg, evidence, previous, cancel, run=run)
        table_translate._cancel(cancel)
        if extract(ws, regions) != evidence:
            raise ValueError(tr("來源已變更，譯文未儲存。"))
        record = {"id": source_hash, **evidence, "table_reading_zh": values,
                  "table_reading_cache": {"version": table_translate.READING_VERSION,
                      "source_hash": key, "translation_hash": table_translate._hash(values), "target": "zh-TW"}}
        def apply(current):
            table_translate._cancel(cancel)
            if table_translate.pdf_hash(ws) != evidence["source"]["pdf_sha256"]:
                raise ValueError(tr("來源 PDF 已變更，譯文未儲存。"))
            records = current.setdefault(FIELD, [])
            existing = next((v for v in records if v.get("id") == source_hash), {})
            if existing != old:
                raise ValueError(tr("譯文已由其他操作更新，請重新載入。"))
            if existing:
                records[records.index(existing)] = record
            else:
                records.append(record)
        ws.update("paper", apply)
        return {"id": source_hash, "cached": cached}


def saved(ws):
    """Never expose a stale/tampered source or translation as verified content."""
    valid = []
    for record in ws.load("paper").get(FIELD, []):
        try:
            evidence = extract(ws, record["source"]["regions"])
            if record["id"] != evidence["source_hash"] or any(record[k] != v for k, v in evidence.items()):
                continue
            values = record["table_reading_zh"]
            table_translate._reading_values(evidence, values)
            cache = record["table_reading_cache"]
            if (cache["version"] != table_translate.READING_VERSION or cache["target"] != "zh-TW" or
                    cache["translation_hash"] != table_translate._hash(values)):
                continue
            valid.append(copy.deepcopy(record))
        except (ValueError, KeyError, TypeError, OSError):
            continue
    digest = table_translate.pdf_hash(ws)
    return [record for record in valid if record["source"]["pdf_sha256"] == digest]


def handle(jobs, ws, body):
    action = body.get("action")
    if action == "candidates":
        return {"candidates": candidates(ws, body.get("page")), "saved": saved(ws)}
    if action == "preview":
        return preview(ws, body.get("regions"))
    if action == "translate":
        evidence = extract(ws, body.get("regions"))
        if evidence["source_hash"] != body.get("source_hash"):
            raise ValueError(tr("請重新預覽英文。"))
        return jobs.submit_small("simple-table", ws.id, regions=evidence["source"]["regions"],
                                 source_hash=evidence["source_hash"])
    if action == "status":
        return {"jobs": [j for j in jobs.small_status(ws.id) if j["kind"] == "simple-table"], "saved": saved(ws)}
    if action == "cancel":
        own = {j["id"] for j in jobs.small_status(ws.id) if j["kind"] == "simple-table"}
        return {"cancelled": body.get("job") in own and jobs.cancel_small(body["job"])}
    raise ValueError(tr("未知的簡易表格操作。"))
