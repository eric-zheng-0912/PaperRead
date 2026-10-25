"""Explicit user-confirmed links; never write paper, reader, PDF or caches."""
import copy
import hashlib

from . import pdfwork, simple_tables, table_translate, tables
from .i18n import tr
from .store import dir_lock, read_json, write_json_atomic

FILE = "manual_table_links.json"
VIEW = "manual_table_view"


def identity(block):
    return table_translate._hash({k: block.get(k) for k in
        ("id", "type", "page", "num", "caption_en", "en", "table_spans", "table_segments")})


def eligible(block):
    return (isinstance(block, dict) and block.get("type") == "table" and
            isinstance(block.get("id"), str) and bool(block["id"]) and
            (block.get("table_schema") != 1 or block.get("table_status") == "fallback" or
             bool(tables.shape_problem(block))))


def state(ws):
    value = read_json(ws.root / FILE, {"version": 1, "revision": 0, "links": {}})
    if (not isinstance(value, dict) or value.get("version") != 1 or
            type(value.get("revision")) is not int or value["revision"] < 0 or
            not isinstance(value.get("links"), dict)):
        raise ValueError(tr("表格關聯資料無效，未變更儲存內容。"))
    return value


def selected(ws, body):
    matches = [r for r in simple_tables.saved(ws) if r["id"] == body.get("record_id")]
    if (len(matches) != 1 or
            matches[0]["table_reading_cache"]["translation_hash"] != body.get("translation_hash")):
        raise ValueError(tr("譯文或來源已變更，請重新選擇並確認。"))
    return matches[0]


def handle(ws, body):
    action = body.get("action")
    if action == "link-targets":
        selected(ws, body)
        current = state(ws)
        blocks = [b for b in (ws.load("paper") or {}).get("blocks", []) if isinstance(b, dict)]
        return {"revision": current["revision"], "targets": [
            {"id": b["id"], "block_hash": identity(b), "page": b.get("page"),
             "caption": b.get("caption_en") or b.get("num") or b["id"],
             "linked": b["id"] in current["links"]}
            for b in blocks if eligible(b) and sum(x.get("id") == b["id"] for x in blocks) == 1]}
    if body.get("confirmed") is not True or type(body.get("revision")) is not int:
        raise ValueError(tr("請明確確認表格關聯操作。"))
    with dir_lock(ws.root):
        current = state(ws)
        if current["revision"] != body["revision"]:
            raise ValueError(tr("表格關聯已由其他操作更新，請重新載入並確認。"))
        bid = body.get("block_id")
        if not isinstance(bid, str):
            raise ValueError(tr("請選擇正文中的表格佔位區。"))
        if action == "unlink":
            current["links"].pop(bid, None)
        elif action == "link":
            blocks = [b for b in (ws.load("paper") or {}).get("blocks", [])
                      if isinstance(b, dict) and b.get("id") == bid]
            if len(blocks) != 1 or not eligible(blocks[0]) or identity(blocks[0]) != body.get("block_hash"):
                raise ValueError(tr("正文表格已變更，請重新選擇佔位區。"))
            record = selected(ws, body)
            current["links"][bid] = {"block_hash": identity(blocks[0]), "record": copy.deepcopy(record)}
        else:
            raise ValueError(tr("未知的表格關聯操作。"))
        current["revision"] += 1
        write_json_atomic(ws.root / FILE, current)
        return {"revision": current["revision"]}


def project(ws, paper):
    """Attach verified display-only snapshots at their existing block positions."""
    blocks = [b for b in paper.get("blocks", []) if isinstance(b, dict)]
    for b in blocks:
        b.pop(VIEW, None)
        b.pop("manual_table_issue", None)
        b.pop("manual_table_link_revision", None)
    try:
        current = state(ws)
    except (ValueError, OSError):
        return
    if not current["links"]:
        return
    for b in blocks:
        if not isinstance(b.get("id"), str):
            continue
        link = current["links"].get(b.get("id"))
        if link is None:
            continue
        b["manual_table_link_revision"] = current["revision"]
        try:
            if (not eligible(b) or sum(x.get("id") == b["id"] for x in blocks) != 1 or
                    link["block_hash"] != identity(b)):
                raise ValueError("Block identity changed")
            record = link["record"]
            if len(simple_tables.saved(ws, [record])) != 1:
                raise ValueError("Snapshot validation failed")
            data = ws.source_pdf.read_bytes()
            if hashlib.sha256(data).hexdigest() != record["source"]["pdf_sha256"]:
                raise ValueError("PDF changed")
            previews = [pdfwork.reading_preview(data, r["page"], r["box"])
                        for r in record["source"]["regions"]]
            b[VIEW] = {"record_id": record["id"], "revision": current["revision"],
                       "source": copy.deepcopy(record["source"]),
                       "values": copy.deepcopy(record["table_reading_zh"]), "previews": previews}
        except Exception:
            b["manual_table_issue"] = tr("已儲存的表格關聯需要重新確認，請開啟簡易表格翻譯。")
    try:
        digest = table_translate.pdf_hash(ws)
    except (OSError, ValueError):
        digest = None
    for b in blocks:
        if VIEW in b and b[VIEW]["source"]["pdf_sha256"] != digest:
            b.pop(VIEW)
            b["manual_table_issue"] = tr("來源 PDF 已變更，請重新確認表格關聯。")
