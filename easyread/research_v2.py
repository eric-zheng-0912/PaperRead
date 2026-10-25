"""PaperRead Research workspace API: OpenAlex discovery + local reading plans."""
from __future__ import annotations

import json
import re
import threading
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timezone
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, planning_store, sources
from .research import obsidian_export
from .i18n import tr

PREFIX = "/api/research/v2"
OPENALEX = "https://api.openalex.org"
_request_lock = threading.Lock()
_last_request = 0.0
_summary_worker_lock = threading.Lock()
_summary_workers: set[str] = set()
_summary_recovered_roots: set[str] = set()


def _root(app) -> Path:
    return app.lib.root


def _now() -> datetime:
    return datetime.now().astimezone()


def _fresh_cache(root: Path, key: str, max_age_seconds: int = 21600) -> dict | None:
    value, updated = planning_store.cache_get(root, key)
    if not value or not updated:
        return None
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(updated).astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        return None
    return {**value, "cached": True} if 0 <= age <= max_age_seconds else None


def _summary_worker(app):
    """Drain persisted candidate-summary jobs in small batches off the request thread."""
    root = _root(app)
    worker_key = str(root.resolve())
    try:
        while True:
            _time.sleep(0.8)  # brief coalescing window so rapid additions share one model request
            queued = planning_store.candidate_summary_queue(root, 5)
            if not queued:
                return
            claimed = planning_store.candidate_summary_claim(root, [x["paper_key"] for x in queued])
            if not claimed:
                continue
            valid = [x for x in claimed if str(x["paper"].get("abstract") or "").strip()]
            for item in claimed:
                if item not in valid:
                    planning_store.candidate_summary_status(root, item["paper_key"], "failed", "來源沒有提供摘要，無法自動整理。")  # i18n-dom: 保持資料庫錯誤內容固定繁中，前端翻譯呈現。
            if not valid:
                continue
            try:
                from . import chat_models, engines, usage
                cfg, model = chat_models.engine_cfg(config.load(), None)
                meter = usage.Meter(cfg.get("engine", ""))
                papers = [{"paper_key": x["paper_key"], "title": str(x["paper"].get("title") or "")[:400],
                           "authors": str(x["paper"].get("authors") or "")[:500], "year": x["paper"].get("year"),
                           "venue": str(x["paper"].get("venue") or "")[:300], "abstract": str(x["paper"].get("abstract") or "")[:5000]} for x in valid]
                prompt = "請使用繁體中文（臺灣用語）整理下列候選論文的來源摘要。每篇只根據該篇提供的資料，不補造結果、引用或出版資訊；缺少資訊請標示未提供。輸出 JSON 陣列，每個元素含 paper_key 與 summary_markdown，摘要以 Markdown 呈現研究問題、方法、結果與限制。\n" + json.dumps(papers, ensure_ascii=False)  # i18n-ok: 固定要求模型產生繁體中文摘要。
                answer = engines.run(engines.for_translation(cfg), prompt, app.lib.root, [], None, meter)
                output = engines.parse_json(answer)
                unwrapped = _unwrap_json_list(output)
                by_key = {str(x.get("paper_key") or ""): str(x.get("summary_markdown") or "").strip() for x in unwrapped if isinstance(x, dict)} if isinstance(unwrapped, list) else {}
                for item in valid:
                    markdown = by_key.get(item["paper_key"], "")
                    if markdown:
                        planning_store.candidate_save_summary(root, item["paper_key"], markdown, model.get("name", model.get("id", "")))
                    else:
                        planning_store.candidate_summary_status(root, item["paper_key"], "failed", "模型未回傳這篇論文的摘要，請重試整理。")  # i18n-dom: 保持資料庫錯誤內容固定繁中，前端翻譯呈現。
            except Exception:
                for item in valid:
                    planning_store.candidate_summary_status(root, item["paper_key"], "failed", "摘要整理失敗，請確認 AI 模型設定或連線後重試。")  # i18n-dom: 保持資料庫錯誤內容固定繁中，前端翻譯呈現。
    finally:
        with _summary_worker_lock:
            _summary_workers.discard(worker_key)
        # Catch a job enqueued as the worker was exiting.
        if planning_store.candidate_summary_queue(root, 1):
            _ensure_summary_worker(app)


def _ensure_summary_worker(app):
    worker_key = str(_root(app).resolve())
    with _summary_worker_lock:
        if worker_key in _summary_workers:
            return
        _summary_workers.add(worker_key)
    threading.Thread(target=_summary_worker, args=(app,), daemon=True, name="paperread-summary-queue").start()


def _recover_summary_jobs(app):
    worker_key = str(_root(app).resolve())
    with _summary_worker_lock:
        if worker_key in _summary_recovered_roots:
            return
        _summary_recovered_roots.add(worker_key)
    planning_store.recover_candidate_summaries(_root(app))


def _fast_model_id(cfg: dict) -> str | None:
    """Prefer a configured low-latency model for short search-term expansion."""
    from . import chat_models
    models = chat_models.models(cfg)
    patterns = (r"haiku", r"flash", r"mini", r"small", r"fast", r"sonnet")
    for pattern in patterns:
        match = next((m for m in models if re.search(pattern, " ".join(str(m.get(k) or "") for k in ("id", "name", "model")), re.I)), None)
        if match:
            return match.get("id")
    return (cfg.get("chat") or {}).get("default")


def _key(work: dict) -> str:
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(work.get("doi") or "").strip(), flags=re.I).lower()
    if doi:
        return "doi:" + doi
    arxiv = re.sub(r"v\d+$", "", str(work.get("arxiv") or "").strip(), flags=re.I)
    if arxiv:
        return "arxiv:" + arxiv.lower()
    ident = str(work.get("openalex_id") or work.get("id") or "").strip()
    openalex_id = re.search(r"(?:openalex\.org/)?(W\d+)$", ident, flags=re.I)
    if openalex_id:
        return "openalex:" + openalex_id.group(1).upper()
    ident = " ".join(str(work.get("title") or "").lower().split())
    return "title:" + sha256(ident.encode("utf-8")).hexdigest()[:24]


def _abstract(index: dict | None) -> str:
    if not isinstance(index, dict):
        return ""
    words = {}
    for word, positions in index.items():
        for pos in positions:
            words[int(pos)] = word
    return " ".join(words[k] for k in sorted(words))


def _work(raw: dict) -> dict:
    authors = [((a.get("author") or {}).get("display_name") or "") for a in raw.get("authorships", [])]
    locations = raw.get("locations") or []
    preferred = raw.get("best_oa_location") or raw.get("primary_location") or {}
    pdf_url = preferred.get("pdf_url") or ""
    if not pdf_url:
        pdf_url = next((loc.get("pdf_url") for loc in locations if loc.get("pdf_url")), "")
    # Publication venue and the repository hosting the public PDF are distinct.
    source = (((raw.get("primary_location") or {}).get("source") or {}).get("display_name") or
              (preferred.get("source") or {}).get("display_name") or "")
    doi = raw.get("doi") or ""
    if doi.startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/"):]
    landing = raw.get("id") or ""
    arxiv = ""
    for loc in locations:
        landing_url = ((loc.get("landing_page_url") or "") + " " + (loc.get("pdf_url") or "")).lower()
        match = re.search(r"arxiv\.org/(?:abs|pdf)/([0-9.]+(?:v\d+)?)", landing_url)
        if match:
            arxiv = match.group(1)
            break
    referenced = [str(x).rstrip("/").split("/")[-1] for x in (raw.get("referenced_works") or []) if x]
    related = [str(x).rstrip("/").split("/")[-1] for x in (raw.get("related_works") or []) if x]
    keywords = []
    for k in (raw.get("keywords") or []):
        name = k.get("display_name") if isinstance(k, dict) else str(k)
        if name and name not in keywords:
            keywords.append(name)
    topics = []
    pt = raw.get("primary_topic")
    if isinstance(pt, dict) and pt.get("display_name"):
        topics.append(pt["display_name"])
    for t in (raw.get("topics") or []):
        name = t.get("display_name") if isinstance(t, dict) else str(t)
        if name and name not in topics:
            topics.append(name)
    return {
        "id": landing, "openalex_id": landing, "doi": doi, "arxiv": arxiv,
        "title": raw.get("title") or "（未提供標題）", "authors": ", ".join(a for a in authors if a),  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        "year": raw.get("publication_year") or "", "venue": source,
        "abstract": _abstract(raw.get("abstract_inverted_index")),
        "cited_by_count": raw.get("cited_by_count"), "is_oa": bool((raw.get("open_access") or {}).get("is_oa")),
        "pdf_url": pdf_url, "url": landing, "source": "OpenAlex",
        "referenced_works": referenced, "related_works": related,
        "keywords": keywords[:8], "topics": topics[:5],
    }


def _openalex(path: str, params: dict | None = None, timeout=15) -> dict:
    global _last_request
    query = urllib.parse.urlencode(params or {}, doseq=True)
    url = OPENALEX + path + ("?" + query if query else "")
    request = urllib.request.Request(url, headers={"User-Agent": "PaperRead/1.4 (local research reading planner)", "Accept": "application/json"})
    for attempt in range(2):
        # One bounded retry; permanent failures such as unknown IDs are not retried.
        with _request_lock:
            delay = .15 - (_time.monotonic() - _last_request)
            if delay > 0:
                _time.sleep(delay)
            _last_request = _time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if attempt == 0 and exc.code in (429, 500, 502, 503, 504):
                try:
                    retry_after = float(exc.headers.get("Retry-After", ".5")) if exc.headers else .5
                except (TypeError, ValueError):
                    retry_after = .5
                _time.sleep(max(.5, min(2, retry_after)))
                continue
            if exc.code == 429:
                raise ValueError("OpenAlex 已達到查詢限制，請稍後再試；已保留先前搜尋結果。") from exc  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
            raise ValueError(f"OpenAlex 查詢失敗（HTTP {exc.code}），請稍後重試。") from exc  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt == 0:
                _time.sleep(.5)
                continue
            raise ValueError("目前無法連線到 OpenAlex；請檢查網路後重試。") from exc  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。


def _search_works(root: Path, query: str, *, sort="relevance_score:desc", year_from="", year_to="", open_access=False, page=1, filter_expr="", source_id="", source_ids=None) -> dict:
    query = query.strip()[:300]
    values = source_ids or []
    if isinstance(values, str):
        values = re.split(r"[|,]", values)
    if not isinstance(values, (list, tuple)):
        raise ValueError("出版來源識別資料不正確。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
    values = list(values) + ([source_id] if source_id else [])
    source_ids = []
    for value in values:
        ident = str(value).strip().rstrip("/").split("/")[-1]
        if not re.fullmatch(r"S\d+", ident):
            raise ValueError("出版來源識別資料不正確。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        if ident not in source_ids:
            source_ids.append(ident)
    if len(source_ids) > 100:
        raise ValueError("一次最多可選擇 100 個出版來源。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
    source_ids.sort()
    if not query and not filter_expr and not source_ids:
        raise ValueError("請輸入主題、論文標題或作者名稱。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
    if not query and sort == "relevance_score:desc":
        sort = "publication_date:desc"
    years = []
    for value in (year_from, year_to):
        value = str(value or "").strip()
        if value and (not re.fullmatch(r"\d{4}", value) or not 1800 <= int(value) <= 2100):
            raise ValueError("年份請輸入 1800 到 2100 之間的四位數。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        years.append(value)
    year_from, year_to = years
    if year_from and year_to and int(year_from) > int(year_to):
        raise ValueError("起始年份不能晚於結束年份。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
    page = max(1, min(100, int(page or 1)))
    cache_key = "works:" + sha256(json.dumps([query, sort, year_from, year_to, open_access, page, filter_expr, source_ids], ensure_ascii=False).encode()).hexdigest()
    cached = _fresh_cache(root, cache_key)
    if cached:
        return cached
    try:
        params = {"per-page": 20, "page": page, "select": "id,doi,title,authorships,publication_year,primary_location,best_oa_location,locations,open_access,cited_by_count,abstract_inverted_index,referenced_works,related_works"}
        if query:
            params["search"] = query
        if sort in ("relevance_score:desc", "publication_date:desc", "cited_by_count:desc"):
            params["sort"] = sort
        filters = []
        if year_from and str(year_from).isdigit():
            filters.append("from_publication_date:" + str(year_from) + "-01-01")
        if year_to and str(year_to).isdigit():
            filters.append("to_publication_date:" + str(year_to) + "-12-31")
        if open_access:
            filters.append("has_content.pdf:true")
        if source_ids:
            filters.append("primary_location.source.id:" + "|".join(source_ids))
        if filter_expr:
            filters.append(filter_expr)
        if filters:
            params["filter"] = ",".join(filters)
        raw = _openalex("/works", params)
        result = {"results": [_work(item) for item in raw.get("results", [])], "count": raw.get("meta", {}).get("count", 0), "page": page, "source": "OpenAlex", "checked_at": _now().isoformat(), "stale": False, "source_ids": source_ids, "sort": sort}
        planning_store.cache_put(root, cache_key, result)
        return result
    except Exception as exc:
        cached, checked = planning_store.cache_get(root, cache_key)
        if cached:
            return {**cached, "stale": True, "error": str(exc), "checked_at": checked}
        raise


def _external_json(url: str, source: str, timeout: int = 12) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "PaperRead/1.4 (local research reading tool)", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise ValueError(f"{source} 查詢達到服務限制，請稍後重試。") from exc  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        raise ValueError(f"{source} 查詢失敗（HTTP {exc.code}）。") from exc  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ValueError(f"目前無法連線到 {source}，請檢查網路後重試。") from exc  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。


def _dblp_search(root: Path, query: str, venues: list[str], page=1, year_from="", year_to="") -> dict:
    query = query.strip()[:250]
    venue_terms = [x.strip()[:80] for x in (venues or []) if str(x).strip()]
    if not venue_terms:
        return {"results": [], "count": 0, "source": "DBLP", "checked_at": _now().isoformat(), "warning": "請先選擇一個會議或期刊來源。"}  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
    page = max(1, min(10, int(page or 1)))
    # DBLP's public search grammar does not provide a `venue:` field operator.
    # Search topic terms first, then strictly filter returned venue metadata;
    # without a topic, search the selected venue names/aliases themselves.
    aliases = []
    for term in venue_terms:
        aliases.extend([term, SOURCE_ALIASES.get(term.upper(), "")])
    aliases = list(dict.fromkeys(x.casefold() for x in aliases if x))
    venue_query = " | ".join(term for term in venue_terms)
    full_query = query or venue_query
    cache_key = "dblp:" + sha256(json.dumps([full_query, venue_terms, page, year_from, year_to], ensure_ascii=False).encode()).hexdigest()
    cached = _fresh_cache(root, cache_key)
    if cached:
        return cached
    try:
        params = {"q": full_query, "format": "json", "h": 100, "f": (page - 1) * 100}
        raw = _external_json("https://dblp.org/search/publ/api?" + urllib.parse.urlencode(params), "DBLP")
        hits = ((raw.get("result") or {}).get("hits") or {})
        values = hits.get("hit", [])
        if isinstance(values, dict): values = [values]
        results = []
        for entry in values:
            info = entry.get("info") or {}
            venue = str(info.get("venue") or "")
            normalized_venue = " ".join(re.findall(r"[\w]+", venue.casefold()))
            if not any(alias and (alias == normalized_venue or normalized_venue.endswith(" " + " ".join(re.findall(r"[\w]+", alias)))) for alias in aliases):
                continue
            year = str(info.get("year") or "")
            if year and ((year_from and year < year_from) or (year_to and year > year_to)):
                continue
            authors = info.get("authors") or {}
            names = authors.get("author", []) if isinstance(authors, dict) else []
            if isinstance(names, dict): names = [names]
            results.append({"id": info.get("url") or entry.get("@id", ""), "openalex_id": "", "doi": info.get("doi") or "",
                            "arxiv": "", "title": info.get("title") or tr("（未提供標題）"), "authors": ", ".join(str(a.get("text") or a) for a in names),
                            "year": year, "venue": venue, "abstract": "",
                            "cited_by_count": None, "is_oa": False, "pdf_url": "", "url": info.get("ee") or info.get("url") or "", "source": "DBLP",
                            "publication_type": info.get("type") or ""})
        result = {"results": results, "count": int(hits.get("@total") or len(results)), "page": page, "has_more": (page * 20 < int(hits.get("@total") or 0)), "source": "DBLP", "checked_at": _now().isoformat(), "stale": False}
        planning_store.cache_put(root, cache_key, result)
        return result
    except Exception as exc:
        cached, checked = planning_store.cache_get(root, cache_key)
        if cached:
            return {**cached, "stale": True, "error": str(exc), "checked_at": checked}
        raise


def _identifier_search(root: Path, query: str) -> dict | None:
    value = query.strip()
    match = re.search(r"(?:openalex\.org/)?(W\d+)$", value, flags=re.I)
    exact_doi = ""
    if match:
        endpoint = "/works/" + match.group(1).upper()
    else:
        exact_doi = re.sub(r"^doi:\s*", "", value, flags=re.I)
        exact_doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", exact_doi, flags=re.I)
        if not re.fullmatch(r"10\.\d{4,9}/[^\s]+", exact_doi, flags=re.I):
            return None
        endpoint = "/works/https://doi.org/" + urllib.parse.quote(exact_doi, safe="")
    cache_key = "identifier:" + sha256(endpoint.casefold().encode()).hexdigest()
    try:
        select = "id,doi,title,authorships,publication_year,primary_location,best_oa_location,locations,open_access,cited_by_count,abstract_inverted_index,referenced_works,related_works"
        try:
            raw = _openalex(endpoint, {"select": select})
        except ValueError as exc:
            if "HTTP 404" not in str(exc):
                raise
            if exact_doi:
                matches = _openalex("/works", {"filter": "doi:" + exact_doi, "per-page": 1, "select": select}).get("results", [])
                if matches:
                    raw = matches[0]
                else:
                    result = {"results": [], "count": 0, "page": 1, "source": "OpenAlex", "checked_at": _now().isoformat(), "stale": False, "reason": "not-found"}
                    planning_store.cache_put(root, cache_key, result)
                    return result
            else:
                result = {"results": [], "count": 0, "page": 1, "source": "OpenAlex", "checked_at": _now().isoformat(), "stale": False, "reason": "not-found"}
                planning_store.cache_put(root, cache_key, result)
                return result
        result = {"results": [_work(raw)], "count": 1, "page": 1, "source": "OpenAlex", "checked_at": _now().isoformat(), "stale": False}
        planning_store.cache_put(root, cache_key, result)
        return result
    except Exception as exc:
        cached, checked = planning_store.cache_get(root, cache_key)
        if cached:
            return {**cached, "stale": True, "error": str(exc), "checked_at": checked}
        raise


SOURCE_ALIASES = {
    "ICCV": "International Conference on Computer Vision",
    "ECCV": "European Conference on Computer Vision",
    "CVPR": "Computer Vision and Pattern Recognition",
    "MICCAI": "Medical Image Computing and Computer Assisted Intervention",
    "WACV": "Winter Conference on Applications of Computer Vision",
    "BMVC": "British Machine Vision Conference",
    "ACCV": "Asian Conference on Computer Vision",
    "3DV": "International Conference on 3D Vision",
    "TPAMI": "IEEE Transactions on Pattern Analysis and Machine Intelligence",
    "IJCV": "International Journal of Computer Vision",
    "TMI": "IEEE Transactions on Medical Imaging",
    "TIP": "IEEE Transactions on Image Processing",
    "TNNLS": "IEEE Transactions on Neural Networks and Learning Systems",
    "NEURIPS": "Advances in Neural Information Processing Systems",
    "ICML": "International Conference on Machine Learning",
    "ICLR": "International Conference on Learning Representations",
    "AAAI": "AAAI Conference on Artificial Intelligence",
    "IJCAI": "International Joint Conference on Artificial Intelligence",
}


def _source_terms(query: str) -> list[str]:
    # Spaces separate a list of acronyms; full journal/conference names retain
    # their spaces and use commas, semicolons or newlines to separate names.
    parts = re.split(r"[,，、;；|\n]+", query.strip())  # i18n-ok: 內部比對或資料格式，不會直接顯示。  # i18n-ok: 搜尋字詞的分隔符號正規表示式。
    terms, seen = [], set()
    for part in parts:
        words = part.strip().split()
        if len(words) > 1 and all(w.upper() in SOURCE_ALIASES or re.fullmatch(r"[A-Z][A-Z0-9-]{1,11}", w) for w in words):
            names = words
        else:
            names = [" ".join(words)] if words else []
        for name in names:
            if name.casefold() not in seen:
                seen.add(name.casefold())
                terms.append(name.upper() if name.upper() in SOURCE_ALIASES else name)
    if len(terms) > 10:
        raise ValueError("一次最多查找 10 組出版來源名稱，請分批查找。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
    return terms


def _source_search(root: Path, query: str) -> dict:
    query = query.strip()[:500]
    if not query:
        raise ValueError("請輸入出版來源名稱。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
    terms = _source_terms(query)
    if not terms:
        raise ValueError("請輸入出版來源名稱。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
    key = "sources:v2:" + sha256(json.dumps(terms, ensure_ascii=False).casefold().encode()).hexdigest()
    try:
        merged, unmatched, errors = {}, [], []
        for term in terms:
            alias = SOURCE_ALIASES.get(term.upper(), "")
            try:
                params = {"search": term, "per-page": 12, "select": "id,display_name,type,works_count,host_organization_name"}
                matches = _openalex("/sources", params).get("results", [])
                if not matches and alias:
                    matches = _openalex("/sources", {**params, "search": alias}).get("results", [])
                if not matches:
                    unmatched.append(term)
                    # OpenAlex does not consistently model conference proceedings
                    # and journals as selectable sources. Preserve the user's
                    # venue as an explicit filter choice so DBLP/Crossref can
                    # search its bibliographic records instead.
                    merged.setdefault("venue:" + term.casefold(), {
                        "id": "venue:" + term.casefold(), "name": alias or term,
                        "type": "manual", "works_count": None, "publisher": None,
                        "matched_queries": [term], "suggested": True,
                    })
                for x in matches:
                    ident, name = x.get("id"), x.get("display_name") or ""
                    if not ident:
                        continue
                    suggested = name.casefold() == term.casefold() or bool(alias and x.get("type") == "conference" and alias.casefold() in name.casefold() and "workshop" not in name.casefold())
                    entry = merged.setdefault(ident, {"id": ident, "name": name, "type": x.get("type"), "works_count": x.get("works_count"), "publisher": x.get("host_organization_name"), "matched_queries": [], "suggested": False})
                    entry["matched_queries"].append(term)
                    entry["suggested"] = entry["suggested"] or suggested
            except Exception as exc:
                errors.append((term, exc))
                merged.setdefault("venue:" + term.casefold(), {
                    "id": "venue:" + term.casefold(), "name": alias or term,
                    "type": "manual", "works_count": None, "publisher": None,
                    "matched_queries": [term], "suggested": True,
                })
        if len(errors) == len(terms):
            raise errors[0][1]
        results = list(merged.values())
        warnings = []
        if unmatched:
            warnings.append("部分來源未列於 OpenAlex 的出版來源清單，已保留為手動來源，改由書目資料搜尋；結果可能不完整：" + "、".join(unmatched) + "。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        if errors:
            warnings.append("以下來源暫時查詢失敗，請重試：" + "、".join(t for t, _ in errors) + "。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        if "ECCV" in terms:
            warnings.append("OpenAlex 的 ECCV 會議來源收錄可能不完整；部分論文登錄於書籍系列，這裡不會把整個書籍系列當成 ECCV。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        result = {"results": results, "count": len(results), "queries": terms, "unmatched": unmatched, "failed_queries": [t for t, _ in errors], "warning": " ".join(warnings),
                  "source": "OpenAlex", "checked_at": _now().isoformat(), "stale": False}
        planning_store.cache_put(root, key, result)
        return result
    except Exception as exc:
        cached, checked = planning_store.cache_get(root, key)
        if cached:
            return {**cached, "stale": True, "error": str(exc), "checked_at": checked}
        raise


def _crossref_venue_search(root: Path, query: str, venues: list[str], page=1, year_from="", year_to="") -> dict:
    """Search Crossref by venue metadata; keep only exact/alias title matches."""
    venue_terms = [str(x).strip()[:120] for x in venues if str(x).strip()]
    if not venue_terms:
        return {"results": [], "count": 0, "source": "Crossref", "checked_at": _now().isoformat(), "warning": tr("請先選擇會議或期刊來源。")}
    page = max(1, min(10, int(page or 1)))
    cache_key = "crossref-venue:v2:" + sha256(json.dumps([query, venue_terms, page, year_from, year_to], ensure_ascii=False).encode()).hexdigest()
    cached = _fresh_cache(root, cache_key)
    if cached:
        return cached
    results, errors = {}, []
    for term in venue_terms[:5]:
        alias = SOURCE_ALIASES.get(term.upper(), "")
        searches = [alias or term]
        if alias and term.casefold() != alias.casefold():
            searches.append(term)
        for venue_query in searches:
            try:
                params = {"query.container-title": venue_query, "rows": 100, "offset": (page - 1) * 100,
                          "select": "DOI,title,author,published-print,published-online,issued,container-title,URL,type,abstract"}
                if query.strip():
                    params["query"] = query.strip()[:200]
                raw = _external_json("https://api.crossref.org/works?" + urllib.parse.urlencode(params), "Crossref")
                message = raw.get("message") or {}
                matched_before = len(results)
                for item in message.get("items", []):
                    containers = item.get("container-title") or []
                    venue = str(containers[0] if containers else "")
                    normalize = lambda value: " ".join(re.findall(r"[\w]+", str(value).casefold()))
                    names = [term, alias]
                    normalized_venue = normalize(venue)
                    if not venue or not any(
                        normalize(name) and (normalized_venue == normalize(name) or normalized_venue.startswith(normalize(name) + " "))
                        for name in names
                    ):
                        continue
                    published = item.get("published-print") or item.get("published-online") or item.get("issued") or {}
                    date_parts = published.get("date-parts") or [[]]
                    year = str(date_parts[0][0]) if date_parts and date_parts[0] else ""
                    if year and ((year_from and year < str(year_from)) or (year_to and year > str(year_to))):
                        continue
                    authors = [" ".join(filter(None, (a.get("given"), a.get("family")))) for a in item.get("author", [])]
                    doi = str(item.get("DOI") or "")
                    titles = item.get("title") or []
                    key = doi.casefold() or str(item.get("URL") or "").casefold()
                    if not key or not titles:
                        continue
                    results[key] = {"id": item.get("URL") or "", "openalex_id": "", "doi": doi, "arxiv": "",
                                    "title": titles[0], "authors": ", ".join(authors), "year": year, "venue": venue,
                                    "abstract": re.sub(r"<[^>]+>", "", str(item.get("abstract") or ""))[:4000],
                                    "cited_by_count": None, "is_oa": False, "pdf_url": "", "url": item.get("URL") or "",
                                    "source": "Crossref", "publication_type": item.get("type") or ""}
                if len(results) > matched_before:
                    break
            except Exception as exc:
                errors.append(exc)
    if not results and errors and len(errors) >= len(venue_terms):
        raise errors[0]
    ordered = list(results.values())
    result = {"results": ordered, "count": len(ordered), "page": page, "has_more": False, "source": "Crossref",
              "checked_at": _now().isoformat(), "stale": False}
    if errors:
        result["warning"] = tr("Crossref 部分查詢未完成，已顯示成功取得的書目資料。")
    planning_store.cache_put(root, cache_key, result)
    return result


def _existing_key_set(items):
    keys = set()
    for i in items:
        if i.get("doi"):
            keys.add("doi:" + re.sub(r"^https?://(?:dx\.)?doi\.org/", "", i["doi"], flags=re.I).lower())
        if i.get("arxiv"):
            keys.add("arxiv:" + re.sub(r"v\d+$", "", i["arxiv"], flags=re.I).lower())
        if i.get("openalex_id") or i.get("id"):
            ident = str(i.get("openalex_id") or i["id"]).rstrip("/").split("/")[-1].upper()
            if re.fullmatch(r"W\d+", ident):
                keys.add("openalex:" + ident)
    return keys


def _resolve_seed(seed: dict) -> dict | None:
    """Resolve a library seed to its OpenAlex work without accepting a fuzzy title match."""
    select = "id,doi,title,authorships,publication_year,primary_location,best_oa_location,locations,open_access,cited_by_count,abstract_inverted_index,related_works,keywords,topics,primary_topic"
    openalex = str(seed.get("openalex_id") or "").rstrip("/").split("/")[-1].upper()
    if re.fullmatch(r"W\d+", openalex):
        return _openalex("/works/" + openalex, {"select": select})
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(seed.get("doi") or "").strip(), flags=re.I)
    if re.fullmatch(r"10\.\d{4,9}/[^\s]+", doi, flags=re.I):
        try:
            return _openalex("/works/https://doi.org/" + urllib.parse.quote(doi, safe=""), {"select": select})
        except ValueError as exc:
            if "HTTP 404" not in str(exc):
                raise
    arxiv = re.sub(r"v\d+$", "", str(seed.get("arxiv") or "").strip(), flags=re.I)
    title = " ".join(str(seed.get("title_en") or seed.get("title_zh") or seed.get("title") or "").casefold().split())
    for query, expected, field in ((arxiv, arxiv.casefold(), "arxiv"), (title, title, "title")):
        if not query:
            continue
        raw = _openalex("/works", {"search": query[:250], "per-page": 5, "select": select})
        for candidate in raw.get("results", []):
            resolved = _work(candidate)
            actual = (resolved.get("arxiv") or "").casefold() if field == "arxiv" else " ".join((resolved.get("title") or "").casefold().split())
            if actual == expected:
                return candidate
    return None


def _similarity_tokens(text: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9]{2,}", str(text).casefold()))
    return words - {"and", "for", "from", "into", "with", "the", "this", "that", "using", "based", "via", "are", "was", "were", "our", "their", "its"}


def _text_similarity(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _find_existing(app, work):
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(work.get("doi") or "").strip(), flags=re.I).lower()
    arxiv = re.sub(r"v\d+$", "", str(work.get("arxiv") or "").strip(), flags=re.I).lower()
    openalex_id = str(work.get("openalex_id") or work.get("id") or "").rstrip("/").lower()
    for item in app.lib.list():
        if doi and re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(item.get("doi") or "").strip(), flags=re.I).lower() == doi:
            return item
        if arxiv and re.sub(r"v\d+$", "", str(item.get("arxiv") or "").strip(), flags=re.I).lower() == arxiv:
            return item
        if openalex_id and str(item.get("openalex_id") or "").rstrip("/").lower() == openalex_id:
            return item
    return None


def get(app, path_query: str):
    path = urlparse(path_query)
    if not (path.path == PREFIX or path.path.startswith(PREFIX + "/")):
        return None
    endpoint = path.path[len(PREFIX):].strip("/")
    q = {k: v[0] for k, v in parse_qs(path.query).items()}
    if endpoint.startswith("ui/"):
        from . import research_ui
        return research_ui.get(app, endpoint, q)
    if endpoint in ("", "init"):
        _recover_summary_jobs(app)
        if planning_store.candidate_summary_queue(_root(app), 1):
            _ensure_summary_worker(app)
        state = planning_store.init(_root(app))
        items = app.lib.list()
        for item in items:
            item["interested"] = bool(item.get("starred", item.get("interested", False)))
        tags = sorted({tag for i in items for tag in (i.get("tags") or [])})
        return {**state, "items": items, "candidates": planning_store.candidates(_root(app)),
                "summaries": planning_store.summaries_list(_root(app)), "categories": tags,
                "provider": "OpenAlex", "api_key_required": False}
    if endpoint.startswith("paper/"):
        tail = endpoint.split("/", 1)[1]
        pid, _, action = tail.partition("/")
        ws = app.lib.ws(pid)
        if not ws:
            raise ValueError("找不到這篇論文。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        item = ws.load("item") or {}
        paper = ws.load("paper") or {}
        meta = paper.get("meta") or {}
        if action == "mindmap":
            evidence = []
            for block in paper.get("blocks") or []:
                if not isinstance(block, dict):
                    continue
                parts = [str(block.get(k) or "").strip() for k in ("en", "zh", "text", "caption_en", "caption_zh", "alt_en", "alt_zh")]
                text = " | ".join(dict.fromkeys(x for x in parts if x))
                if text:
                    evidence.append({"id": str(block.get("id") or ""), "type": block.get("type"), "page": block.get("page"), "text": text[:1800]})
            source_hash = str(meta.get("source_sha256") or sha256(json.dumps(evidence, ensure_ascii=False).encode()).hexdigest())
            cache_key = "mindmap:" + sha256(source_hash.encode()).hexdigest()
            cached, checked = planning_store.cache_get(_root(app), cache_key)
            return {**(cached or {}), "cached": bool(cached), "checked_at": checked}
        return {"id": pid, "item": item, "title": meta.get("title_zh") or meta.get("title_en") or "未命名論文",  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
                "title_en": meta.get("title_en", ""), "doi": meta.get("doi", ""), "arxiv": meta.get("arxiv", ""),
                "openalex_id": meta.get("openalex_id", ""), "statuses": planning_store.statuses(_root(app))}
    if endpoint == "search":
        kind = q.get("kind", "works")
        if kind == "dblp":
            return _dblp_search(_root(app), q.get("q", ""), q.get("venues", "").split("|"), q.get("page", 1), q.get("year_from", ""), q.get("year_to", ""))
        if kind == "crossref":
            return _crossref_venue_search(_root(app), q.get("q", ""), q.get("venues", "").split("|"), q.get("page", 1), q.get("year_from", ""), q.get("year_to", ""))
        if kind == "sources":
            return _source_search(_root(app), q.get("q", ""))
        if kind != "works":
            raise ValueError("不支援的搜尋類型。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        exact = _identifier_search(_root(app), q.get("q", ""))
        if exact:
            return exact
        return _search_works(_root(app), q.get("q", ""), sort=q.get("sort", "relevance_score:desc"), year_from=q.get("year_from", ""), year_to=q.get("year_to", ""), open_access=q.get("open_access") == "1", page=q.get("page", 1), source_id=q.get("source_id", ""), source_ids=q.get("source_ids", ""))
    if endpoint == "publication-check":
        doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", q.get("doi", "").strip(), flags=re.I)
        if not re.fullmatch(r"10\.\d{4,9}/[^\s]+", doi, re.I):
            raise ValueError("這篇論文沒有可核對的 DOI。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        work = _external_json("https://api.crossref.org/works/" + urllib.parse.quote(doi, safe=""), "Crossref")
        item = work.get("message") or {}
        title = (item.get("title") or [""])[0]
        published = item.get("published-print") or item.get("published-online") or item.get("issued") or {}
        date_parts = published.get("date-parts") or [[]]
        year = date_parts[0][0] if date_parts and date_parts[0] else None
        return {"doi": item.get("DOI") or doi, "title": title, "container_title": (item.get("container-title") or [""])[0],
                "type": item.get("type") or "", "publisher": item.get("publisher") or "", "year": year,
                "url": item.get("URL") or "", "source": "Crossref", "checked_at": _now().isoformat()}
    if endpoint.startswith("work/"):
        raw_id = urllib.parse.unquote(endpoint.split("/", 1)[1].strip())
        kind = q.get("relation", "related")
        if kind not in ("references", "related", "cited-by"):
            raise ValueError("不支援的論文關聯類型。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        wid = raw_id.rstrip("/").split("/")[-1]
        seed_title = q.get("start_title", "").strip()
        seed_doi = q.get("start_doi", "").strip()
        resolved = None
        if re.fullmatch(r"W\d+", wid, flags=re.I):
            wid = wid.upper()
        else:
            seed_dict = {}
            if raw_id.lower().startswith("doi:"):
                seed_dict["doi"] = raw_id[4:].strip()
            elif re.match(r"^10\.\d{4,9}/", raw_id):
                seed_dict["doi"] = raw_id.strip()
            elif raw_id.lower().startswith("arxiv:"):
                seed_dict["arxiv"] = raw_id[6:].strip()
            elif raw_id.lower().startswith("openalex:") or raw_id.lower().startswith("openalex_id:"):
                seed_dict["openalex_id"] = raw_id.split(":", 1)[1].strip()
            elif raw_id and raw_id != "_":
                seed_dict["title"] = raw_id

            if q.get("start_doi"):
                seed_dict["doi"] = q["start_doi"]
            if q.get("start_arxiv"):
                seed_dict["arxiv"] = q["start_arxiv"]
            if q.get("start_title"):
                seed_dict["title"] = q["start_title"]
            if q.get("openalex_id"):
                seed_dict["openalex_id"] = q["openalex_id"]

            resolved = _resolve_seed(seed_dict)
            if not resolved or not resolved.get("id"):
                raise ValueError("找不到對應的 OpenAlex 論文識別資料，無法展開學術關聯。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
            wid = resolved["id"].rstrip("/").split("/")[-1].upper()
            if not seed_title:
                seed_title = resolved.get("title") or ""
            if not seed_doi:
                seed_doi = resolved.get("doi") or ""

        seed_keywords = []
        seed_topics = []
        raw_seed = resolved
        if kind == "cited-by" and not raw_seed:
            try:
                raw_seed = _openalex("/works/" + wid, {"select": "id,doi,title,keywords,topics,primary_topic"})
            except Exception:
                raw_seed = None
        if raw_seed:
            if not seed_title and raw_seed.get("title"):
                seed_title = raw_seed.get("title") or ""
            if not seed_doi and raw_seed.get("doi"):
                seed_doi = raw_seed.get("doi") or ""
            for k in (raw_seed.get("keywords") or []):
                name = k.get("display_name") if isinstance(k, dict) else str(k)
                if name and name not in seed_keywords:
                    seed_keywords.append(name)
            pt = raw_seed.get("primary_topic")
            if isinstance(pt, dict) and pt.get("display_name") and pt["display_name"] not in seed_topics:
                seed_topics.append(pt["display_name"])
            for t in (raw_seed.get("topics") or []):
                name = t.get("display_name") if isinstance(t, dict) else str(t)
                if name and name not in seed_topics:
                    seed_topics.append(name)

        if kind == "cited-by":
            result = _search_works(_root(app), "", sort="publication_date:desc", page=q.get("page", 1), filter_expr=f"cites:{wid}")
            return {**result, "relation": kind, "seed_id": wid, "seed_title": seed_title, "seed_doi": seed_doi, "seed_keywords": seed_keywords[:8], "seed_topics": seed_topics[:5]}
        page = max(1, min(100, int(q.get("page", 1) or 1)))
        cache_key = f"relation:{wid}:{kind}:{page}"
        try:
            raw = _openalex("/works/" + wid, {"select": "id,doi,title,referenced_works,related_works,keywords,topics,primary_topic"})
            if not seed_title and raw.get("title"):
                seed_title = raw.get("title") or ""
            if not seed_doi and raw.get("doi"):
                seed_doi = raw.get("doi") or ""
            for k in (raw.get("keywords") or []):
                name = k.get("display_name") if isinstance(k, dict) else str(k)
                if name and name not in seed_keywords:
                    seed_keywords.append(name)
            pt = raw.get("primary_topic")
            if isinstance(pt, dict) and pt.get("display_name") and pt["display_name"] not in seed_topics:
                seed_topics.append(pt["display_name"])
            for t in (raw.get("topics") or []):
                name = t.get("display_name") if isinstance(t, dict) else str(t)
                if name and name not in seed_topics:
                    seed_topics.append(name)
            ids = raw.get("referenced_works", []) if kind == "references" else raw.get("related_works", [])
            batch_ids = [ident.rstrip("/").split("/")[-1] for ident in ids[(page - 1) * 20:page * 20]]
            results = []
            if batch_ids:
                batch = _openalex("/works", {"filter": "openalex_id:" + "|".join(batch_ids), "per-page": 20,
                                             "select": "id,doi,title,authorships,publication_year,primary_location,best_oa_location,locations,open_access,cited_by_count,abstract_inverted_index"})
                by_id = {x["id"].rstrip("/").split("/")[-1]: x for x in batch.get("results", [])}
                results = [_work(by_id[ident]) for ident in batch_ids if ident in by_id]
            result = {"results": results, "count": len(ids), "page": page, "has_more": page * 20 < len(ids), "source": "OpenAlex", "checked_at": _now().isoformat(), "relation": kind, "seed_id": wid, "seed_title": seed_title, "seed_doi": seed_doi, "seed_keywords": seed_keywords[:8], "seed_topics": seed_topics[:5]}
            missing = len(batch_ids) - len(results)
            if missing:
                result["warning"] = f"OpenAlex 未提供本頁 {missing} 篇論文的詳細資料；清單僅顯示可取得的來源。"  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
            planning_store.cache_put(_root(app), cache_key, result)
            return result
        except Exception as exc:
            cached, checked = planning_store.cache_get(_root(app), cache_key)
            if cached:
                return {**cached, "stale": True, "error": str(exc), "checked_at": checked}
            raise
    if endpoint == "category-recommendations":
        category = q.get("category", "").strip()
        all_items = app.lib.list()
        items = [i for i in all_items if category and category in (i.get("tags") or [])]
        selected = [x for x in q.get("seeds", "").split(",") if x]
        seeds = [i for i in items if i["id"] in selected][:10] if selected else sorted(items, key=lambda i: (bool(i.get("interested")), i.get("status") == "done", i.get("last_opened", "")), reverse=True)[:10]
        if not seeds:
            return {"results": [], "count": 0, "source": "OpenAlex", "checked_at": _now().isoformat(), "reason": "empty"}
        cache_key = "category:" + sha256(json.dumps([category, [x["id"] for x in seeds]], ensure_ascii=False).encode()).hexdigest()
        aggregate, seed_texts, resolved, resolution_errors = {}, [], 0, []
        for seed in seeds:
            try:
                raw = _resolve_seed(seed)
                if not raw:
                    continue
                resolved += 1
                source_work = _work(raw)
                seed_texts.append(_similarity_tokens((source_work.get("title") or "") + " " + (source_work.get("abstract") or "")))
                for rid in raw.get("related_works", [])[:10]:
                    key = rid.rstrip("/").split("/")[-1]
                    aggregate.setdefault(key, set()).add(seed["id"])
            except Exception as exc:
                resolution_errors.append(exc)
                continue
        try:
            if not resolved:
                transient = next((error for error in resolution_errors if "HTTP 404" not in str(error)), None)
                if transient:
                    raise transient
                result = {"results": [], "count": 0, "source": "OpenAlex", "checked_at": _now().isoformat(), "reason": "unresolved", "seeds": [i["id"] for i in seeds]}
            else:
                ranked = sorted(aggregate.items(), key=lambda pair: (-len(pair[1]), pair[0]))[:40]
                results, candidate_errors = [], []
                for wid, related in ranked:
                    try:
                        work = _work(_openalex("/works/" + wid, {"select": "id,doi,title,authorships,publication_year,primary_location,best_oa_location,locations,open_access,cited_by_count,abstract_inverted_index"}, timeout=8))
                        tokens = _similarity_tokens((work.get("title") or "") + " " + (work.get("abstract") or ""))
                        similarity = max((_text_similarity(tokens, seed_tokens) for seed_tokens in seed_texts), default=0.0)
                        work["recommendation_reason"] = f"與此分類中的 {len(related)} 篇種子論文相關"  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
                        work["seed_ids"] = sorted(related)
                        work["_similarity"] = similarity
                        work["_seed_count"] = len(related)
                        results.append(work)
                    except Exception as exc:
                        candidate_errors.append(exc)
                        continue
                if ranked and not results and len(candidate_errors) == len(ranked):
                    raise candidate_errors[0]
                existing = _existing_key_set(all_items + [s["metadata"] for s in planning_store.get_saved(_root(app))])
                excluded = planning_store.skipped(_root(app))
                results = [w for w in results if not (_existing_key_set([w]) & existing) and _key(w) not in excluded]
                results.sort(key=lambda w: (-w["_seed_count"], -w["_similarity"], -(w.get("cited_by_count") or 0), w.get("openalex_id") or ""))
                for work in results:
                    work.pop("_similarity", None); work.pop("_seed_count", None)
                reason = "no-related" if not aggregate else "no-new-results" if not results else ""
                result = {"results": results[:20], "count": len(results[:20]), "source": "OpenAlex", "checked_at": _now().isoformat(), "seeds": [i["id"] for i in seeds], "reason": reason}
                partial_count = max(0, len(seeds) - resolved) + len(candidate_errors)
                if partial_count and results:
                    result["warning"] = f"有 {partial_count} 項種子或論文資料未能完整取得；目前只依成功取得的來源排序。"  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
            planning_store.cache_put(_root(app), cache_key, result)
            return result
        except Exception as exc:
            cached, checked = planning_store.cache_get(_root(app), cache_key)
            if cached:
                existing = _existing_key_set(all_items + [s["metadata"] for s in planning_store.get_saved(_root(app))])
                excluded = planning_store.skipped(_root(app))
                remaining = [w for w in cached.get("results", []) if not (_existing_key_set([w]) & existing) and _key(w) not in excluded]
                return {**cached, "results": remaining, "count": len(remaining), "stale": True, "error": str(exc), "checked_at": checked}
            raise
    return None


def _background_download(app, paper_key: str):
    record = planning_store.saved(_root(app), paper_key)
    if not record or not planning_store.claim_pdf_download(_root(app), paper_key):
        return
    metadata, plan = record["metadata"], record["plan"]
    refs = [record.get("pdf_url"), metadata.get("arxiv"), metadata.get("doi"), metadata.get("url")]
    errors = []
    for ref in dict.fromkeys(x for x in refs if x):
        try:
            data, filename, fetched_meta = app.lib.fetch(ref)
            if not data.startswith(b"%PDF"):
                errors.append("來源回應不是 PDF")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
                continue
            merged = {**metadata, **(fetched_meta or {}), "title_en": metadata.get("title") or fetched_meta.get("title_en", ""),
                      "authors": metadata.get("authors") or fetched_meta.get("authors", ""), "year": metadata.get("year") or fetched_meta.get("year", ""),
                      "doi": metadata.get("doi") or fetched_meta.get("doi", ""), "arxiv": metadata.get("arxiv") or fetched_meta.get("arxiv", ""),
                      "venue": metadata.get("venue") or fetched_meta.get("venue", ""), "url": metadata.get("url") or fetched_meta.get("url", "")}
            ws, fresh = app.lib.create_from_pdf(data, filename, merged)
            latest = planning_store.saved(_root(app), paper_key) or record
            current_item = ws.load("item") or {}
            tags = list(dict.fromkeys(list(current_item.get("tags") or []) + list(latest.get("categories") or [])))
            interested = bool(current_item.get("starred", current_item.get("interested")) or latest.get("interested"))
            fields = {"status": (latest.get("status") or "unread") if fresh else (current_item.get("status") or latest.get("status") or "unread"),
                      "reading_plan": latest.get("plan") or current_item.get("reading_plan") or {},
                      "interested": interested, "starred": interested,
                      "manual_order": latest.get("manual_order", 0) if fresh else current_item.get("manual_order", 0), "tags": tags, "paper_ref": paper_key}
            ws.patch_item(fields)
            # Prepare source pages and extracted text through the existing pipeline;
            # translation remains an explicit user action.
            planning_store.update_saved(_root(app), paper_key, pdf_state="imported", library_id=ws.id, pdf_error="")
            if fresh or not (ws.load("paper") or {}).get("meta", {}).get("pages"):
                try:
                    app.jobs.enqueue(ws, translate_after=False, read=False)
                except Exception:
                    pass  # PDF and reading plan are linked; preparation can be retried from the library.
            return
        except Exception as exc:
            errors.append(str(exc)[:180])
    planning_store.update_saved(_root(app), paper_key, pdf_state="failed", pdf_error="；".join(errors[-3:]) or "找不到可下載的公開 PDF")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。


def attach_pdf(app, paper_key: str, data: bytes, filename: str) -> dict:
    record = planning_store.saved(_root(app), paper_key)
    if not record:
        raise ValueError("找不到待補 PDF 的論文。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
    if not data.startswith(b"%PDF"):
        raise ValueError("選取的檔案不是有效的 PDF。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
    if record.get("pdf_state") == "running":
        raise ValueError("公開 PDF 正在下載；完成或失敗後再附加自己的檔案。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
    if record.get("pdf_state") == "imported":
        raise ValueError("這篇論文已在文獻庫；請從文獻庫開啟原始檔。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
    if not planning_store.claim_pdf_download(_root(app), paper_key):
        raise ValueError("PDF 作業剛開始，請等候完成或失敗後再重試。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
    record = planning_store.saved(_root(app), paper_key) or record
    metadata = record["metadata"]
    meta = {"title_en": metadata.get("title") or "", "authors": metadata.get("authors") or "", "year": metadata.get("year") or "",
            "venue": metadata.get("venue") or "", "doi": metadata.get("doi") or "", "arxiv": metadata.get("arxiv") or "",
            "openalex_id": metadata.get("openalex_id") or "", "url": metadata.get("url") or ""}
    try:
        ws, fresh = app.lib.create_from_pdf(data, filename, meta)
        tags = list(dict.fromkeys(list((ws.load("item") or {}).get("tags") or []) + list(record.get("categories") or [])))
        current_item = ws.load("item") or {}
        interested = bool(current_item.get("starred", current_item.get("interested")) or record.get("interested"))
        ws.patch_item({"status": (record.get("status") or "unread") if fresh else (current_item.get("status") or record.get("status") or "unread"),
                       "reading_plan": current_item.get("reading_plan") or record.get("plan") or {},
                       "interested": interested, "starred": interested,
                       "manual_order": record.get("manual_order", 0) if fresh else current_item.get("manual_order", 0), "tags": tags, "paper_ref": paper_key})
        job = (ws.load("job") or {}).get("state")
        planning_store.update_saved(_root(app), paper_key, pdf_state="imported", library_id=ws.id, pdf_error="")
        queued = False
        if fresh or (not (ws.load("paper") or {}).get("meta", {}).get("pages") and job not in ("queued", "running")):
            try:
                app.jobs.enqueue(ws, translate_after=False, read=False)
                queued = True
            except Exception:
                queued = False
        return {"id": ws.id, "new": fresh, "queued": queued}
    except Exception as exc:
        planning_store.update_saved(_root(app), paper_key, pdf_state="failed", pdf_error=str(exc)[:180])
        raise


def _normalize_plan(plan: dict | None) -> dict:
    plan = plan if isinstance(plan, dict) else {}
    output = {}
    for key in ("start_date", "due_date"):
        value = str(plan.get(key) or "")
        if value:
            try:
                output[key] = date.fromisoformat(value).isoformat()
            except ValueError as exc:
                raise ValueError("閱讀日期格式不正確。") from exc  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
    if output.get("start_date") and output.get("due_date") and output["start_date"] > output["due_date"]:
        raise ValueError("閱讀開始日不能晚於截止日期。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
    reminders = plan.get("reminders") or {}
    if not isinstance(reminders, dict):
        raise ValueError("提醒設定格式不正確。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
    for key in ("start_time", "deadline_time"):
        try:
            time.fromisoformat(str(reminders.get(key) or "09:00")[:5])
        except ValueError as exc:
            raise ValueError("提醒時間格式不正確。") from exc  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
    output["reminders"] = {"start_enabled": bool(reminders.get("start_enabled")), "due_enabled": bool(reminders.get("due_enabled")),
                           "start_time": str(reminders.get("start_time") or "09:00")[:5], "deadline_time": str(reminders.get("deadline_time") or "09:00")[:5]}
    output["milestone_id"] = str(plan.get("milestone_id") or "")[:80]
    return output


def _unwrap_json_list(raw_parsed: any) -> list | None:
    """Robustly extract a list from parsed JSON, tolerating wrapper objects from weak models."""
    if isinstance(raw_parsed, list):
        return raw_parsed
    if isinstance(raw_parsed, dict):
        for key in ("reading_path", "path", "papers", "steps", "results", "items", "suggestions", "queries", "keywords", "list", "data"):
            val = raw_parsed.get(key)
            if isinstance(val, list):
                return val
        for val in raw_parsed.values():
            if isinstance(val, list) and val and isinstance(val[0], (dict, str)):
                return val
        if raw_parsed.get("id") or raw_parsed.get("title") or raw_parsed.get("role") or raw_parsed.get("paper_key"):
            return [raw_parsed]
    return None


def _normalize_title_key(title: str) -> str:
    return " ".join(re.findall(r"[\w]+", str(title or "").casefold()))


def _build_paper_index(papers: list[dict]) -> tuple[dict[str, dict], dict[str, dict], dict[str, dict]]:
    """Build exact, alias/stripped ID, and title indexes for robust paper resolution."""
    by_exact = {}
    by_alias = {}
    by_title = {}
    for w in papers:
        k = _key(w)
        by_exact[k] = w
        for raw_id in (w.get("openalex_id"), w.get("id"), w.get("doi"), w.get("arxiv"), k):
            if not raw_id:
                continue
            s = str(raw_id).strip()
            by_alias[s.lower()] = w
            stripped = re.sub(r"^(?:https?://(?:openalex\.org/|(?:dx\.)?doi\.org/|arxiv\.org/(?:abs|pdf)/)|openalex:|doi:|arxiv:)", "", s, flags=re.I).rstrip("/").lower()
            if stripped:
                by_alias[stripped] = w
        for t in (w.get("title"), w.get("title_en"), w.get("title_zh")):
            norm_t = _normalize_title_key(t)
            if len(norm_t) >= 4:
                by_title[norm_t] = w
    return by_exact, by_alias, by_title


def _resolve_step_paper(step: dict, by_exact: dict, by_alias: dict, by_title: dict, papers: list[dict]) -> dict | None:
    raw_id = str(step.get("id") or "").strip()
    if raw_id in by_exact:
        return by_exact[raw_id]
    if raw_id.lower() in by_alias:
        return by_alias[raw_id.lower()]
    stripped = re.sub(r"^(?:https?://(?:openalex\.org/|(?:dx\.)?doi\.org/|arxiv\.org/(?:abs|pdf)/)|openalex:|doi:|arxiv:)", "", raw_id, flags=re.I).rstrip("/").lower()
    if stripped and stripped in by_alias:
        return by_alias[stripped]
    for t_key in ("title", "paper_title", "name"):
        t = str(step.get(t_key) or "").strip()
        norm_t = _normalize_title_key(t)
        if norm_t in by_title:
            return by_title[norm_t]
    norm_id = _normalize_title_key(raw_id)
    if norm_id in by_title:
        return by_title[norm_id]
    if raw_id.isdigit():
        idx = int(raw_id)
        if 1 <= idx <= len(papers):
            return papers[idx - 1]
        elif 0 <= idx < len(papers):
            return papers[idx]
    return None


def post(app, endpoint: str, body: dict, *, _cancel=None):
    # Accept both server-normalized paths and callers that provide a bare or
    # trailing-slash endpoint (for example, /candidate/).
    endpoint = "/" + str(endpoint or "").strip("/")
    root = _root(app)
    body = body if isinstance(body, dict) else {}
    if endpoint == "/export/obsidian":
        return obsidian_export.export_to_vault(body.get("documents"))
    if endpoint == "/export/zip":
        return obsidian_export.export_zip(body.get("documents"))
    if endpoint.startswith("/paper/") and endpoint.endswith("/mindmap"):
        pid = endpoint.strip("/").split("/")[1]
        ws = app.lib.ws(pid)
        if not ws:
            raise ValueError("找不到這篇論文。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        paper = ws.load("paper") or {}
        meta = paper.get("meta") or {}
        title = str(meta.get("title_zh") or meta.get("title_en") or tr("未命名論文"))[:300]
        blocks = paper.get("blocks") or []
        evidence = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            parts = [str(block.get(k) or "").strip() for k in ("en", "zh", "text", "caption_en", "caption_zh", "alt_en", "alt_zh")]
            text = " | ".join(dict.fromkeys(x for x in parts if x))
            if text:
                evidence.append({"id": str(block.get("id") or ""), "type": block.get("type"), "page": block.get("page"), "text": text[:1800]})
        if not evidence:
            raise ValueError("這篇論文目前沒有可供整理的內文文字。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        source_hash = str(meta.get("source_sha256") or sha256(json.dumps(evidence, ensure_ascii=False).encode()).hexdigest())
        cache_key = "mindmap:" + sha256(source_hash.encode()).hexdigest()
        # POST is the explicit "產生／更新" action.  It must always invoke
        # the model; cache lookup belongs to the GET path above.  Relying on
        # an optional body flag made older clients silently display the old
        # mind map when they omitted ``force``.
        from . import chat_models, engines, usage
        cfg, model = chat_models.engine_cfg(config.load(), body.get("model"))
        meter = usage.Meter(cfg.get("engine", ""))
        source_text = json.dumps(evidence, ensure_ascii=False)[:24000]
        prompt = "請依據下方實際提供的論文標題與文字，產生 Mermaid mindmap。根節點是論文標題；主要分支請依研究邏輯組織研究問題、背景/理論、方法設計、核心發現、研究貢獻、限制。下層列具體要點、變數、數據；節點精簡，盡可能不超過20個中文字，總數以內容足以支持為準，禁止虛構。用繁體中文，保留必要英文術語。只輸出 mindmap 程式碼，不輸出 HTML、Markdown code fence 或腳本。\n論文標題：" + title + "\n來源文字：" + source_text  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        answer = engines.run(engines.for_translation(cfg), prompt, app.lib.root, [], None, meter).strip()
        answer = re.sub(r"^```(?:mermaid)?\s*|\s*```$", "", answer, flags=re.I).strip()
        if not answer.lower().startswith("mindmap") or len(answer) > 20000 or re.search(r"<\s*/?\s*(?:script|iframe|foreignObject)|javascript:", answer, re.I):
            raise ValueError("模型未回傳安全且可辨識的 Mermaid mindmap，請重試。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        result = {"mermaid": answer, "title": title, "source_sha256": source_hash, "model": model.get("name", model.get("id", "")), "usage": meter.snapshot(), "generated_at": _now().isoformat(), "cached": False}
        planning_store.cache_put(root, cache_key, result)
        return result
    if endpoint == "/candidate":
        work = body.get("paper") or {}
        if not isinstance(work, dict) or not str(work.get("title") or "").strip():
            raise ValueError("論文資料不完整，請重新搜尋後再加入候選清單。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        key = _key(work)
        if body.get("action") == "save-summary":
            markdown = str(body.get("summary_markdown") or "").strip()
            if not markdown:
                raise ValueError("摘要內容是空的，沒有可儲存的內容。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
            record = planning_store.candidate_save_summary(root, key, markdown, str(body.get("model") or ""))
            if not record:
                raise ValueError("這篇論文已不在候選清單，摘要尚未儲存。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
            return {"candidate": record}
        if body.get("action") == "retry-summary":
            if not planning_store.candidate_summary_status(root, key, "queued"):
                raise ValueError(tr("這篇論文已不在候選清單中。"))
            _ensure_summary_worker(app)
            return {"paper_key": key, "summary_status": "queued"}
        if body.get("action", "add") == "remove":
            return {"removed": planning_store.candidate_remove(root, key), "paper_key": key}
        record = planning_store.candidate_add(root, key, work)
        if not record.get("summary_markdown") and record.get("summary_status") in ("", "queued"):
            planning_store.candidate_summary_status(root, key, "queued")
            _ensure_summary_worker(app)
        return {"candidate": next((x for x in planning_store.candidates(root) if x["paper_key"] == key), record)}
    if endpoint == "/statuses":
        values = body.get("statuses")
        moves = body.get("moves") or {}
        planning_store.validate_statuses(values)
        current = {s["id"] for s in planning_store.statuses(root)}
        new_ids = {str(s.get("id")) for s in values or []}
        removed = (current - new_ids) - {"paused"}
        if removed:
            if not isinstance(moves, dict) or any(not isinstance(moves.get(s), str) or moves[s] not in new_ids for s in removed):
                raise ValueError("刪除狀態前，請指定論文要移到哪一欄。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
            item_moves, saved_moves = [], []
            try:
                for ws in app.lib.all():
                    item = ws.load("item") or {}
                    if item.get("status") in removed:
                        item_moves.append((ws, item["status"]))
                        ws.patch_item({"status": moves[item["status"]]})
                for item in planning_store.get_saved(root):
                    if item.get("status") in removed:
                        saved_moves.append((item["paper_key"], item["status"]))
                        planning_store.move_saved_status(root, item["status"], moves[item["status"]])
                planning_store.save_statuses(root, values, moves)
            except Exception:
                for ws, old in item_moves:
                    ws.patch_item({"status": old})
                for key, old in saved_moves:
                    planning_store.update_saved(root, key, status=old)
                raise
            return {"statuses": planning_store.statuses(root)}
        return {"statuses": planning_store.save_statuses(root, values, moves)}
    if endpoint == "/item":
        pid = str(body.get("id") or "")
        saved_key = str(body.get("saved_key") or "")
        if saved_key:
            record = planning_store.saved(root, saved_key)
            if not record:
                raise ValueError("找不到待補 PDF 的論文。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
            fields = {}
            if "status" in body:
                if not isinstance(body["status"], str) or body["status"] not in {x["id"] for x in planning_store.statuses(root)}:
                    raise ValueError("這個閱讀狀態不存在。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
                fields["status"] = body["status"]
            if "reading_plan" in body:
                fields["plan_json"] = json.dumps(_normalize_plan(body["reading_plan"]), ensure_ascii=False)
            if "interested" in body:
                fields["interested"] = int(bool(body["interested"]))
            if "manual_order" in body:
                fields["manual_order"] = max(0, min(10**9, int(body["manual_order"])))
            if "archived" in body:
                fields["archived"] = int(bool(body["archived"]))
            if "tags" in body:
                if not isinstance(body["tags"], list):
                    raise ValueError("分類格式不正確。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
                fields["category"] = json.dumps(list(dict.fromkeys(str(x)[:80] for x in body["tags"] if str(x).strip())), ensure_ascii=False)
            planning_store.update_saved(root, saved_key, **fields)
            return {"ok": True}
        ws = app.lib.ws(pid)
        if not ws:
            raise ValueError("找不到這篇論文。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        fields = {}
        if "status" in body:
            allowed = {x["id"] for x in planning_store.statuses(root)}
            if not isinstance(body["status"], str) or body["status"] not in allowed:
                raise ValueError("這個閱讀狀態不存在。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
            fields["status"] = body["status"]
        if "reading_plan" in body:
            fields["reading_plan"] = _normalize_plan(body["reading_plan"])
        if "interested" in body:
            interested = bool(body.get("starred", body["interested"]))
            fields["interested"] = interested
            fields["starred"] = interested
        elif "starred" in body:
            fields["interested"] = bool(body["starred"])
            fields["starred"] = bool(body["starred"])
        if "manual_order" in body:
            fields["manual_order"] = max(0, min(10**9, int(body["manual_order"])))
        if "archived" in body:
            fields["archived"] = bool(body["archived"])
        if "tags" in body:
            if not isinstance(body["tags"], list):
                raise ValueError("分類格式不正確。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
            fields["tags"] = list(dict.fromkeys(str(x)[:80] for x in body["tags"] if str(x).strip()))[:30]
        return ws.patch_item(fields)
    if endpoint == "/save-paper":
        work = body.get("paper") or {}
        if not isinstance(work, dict) or not work.get("title"):
            raise ValueError("論文資料不完整，請重新搜尋後再加入。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        existing = _find_existing(app, work)
        if existing:
            interested = bool(body.get("interested"))
            fields = {"reading_plan": _normalize_plan(body.get("plan")), "interested": interested, "starred": interested}
            tags = list(existing.get("tags") or [])
            for category in body.get("categories") if isinstance(body.get("categories"), list) else [body.get("category")]:
                if category and category not in tags:
                    tags.append(str(category)[:80])
            fields["tags"] = tags
            app.lib.ws(existing["id"]).patch_item(fields)
            return {"existing": True, "library_id": existing["id"], "paper_key": _key(work)}
        key = _key(work)
        categories = body.get("categories") if isinstance(body.get("categories"), list) else ([body.get("category")] if body.get("category") else [])
        record = planning_store.upsert_saved(root, key, work, categories, _normalize_plan(body.get("plan")), bool(body.get("interested")), str(work.get("pdf_url") or ""))
        if record.get("pdf_state") not in ("running", "imported"):
            threading.Thread(target=_background_download, args=(app, key), daemon=True, name="paperread-pdf-" + key[-8:]).start()
        return {"existing": False, "saved": record}
    if endpoint == "/retry-pdf":
        key = str(body.get("paper_key") or "")
        saved = planning_store.saved(root, key)
        if not saved:
            raise ValueError("找不到待補 PDF 的論文。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        if saved.get("pdf_state") == "running":
            return {"state": "running"}
        if saved.get("pdf_state") == "imported":
            return {"state": "imported", "library_id": saved.get("library_id")}
        planning_store.update_saved(root, key, pdf_state="pending", pdf_error="")
        threading.Thread(target=_background_download, args=(app, key), daemon=True).start()
        return {"state": "running"}
    if endpoint == "/linked":
        key, pid = str(body.get("paper_key") or ""), str(body.get("library_id") or "")
        if not planning_store.saved(root, key) or not app.lib.ws(pid):
            raise ValueError("論文與文獻庫項目無法配對。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        planning_store.update_saved(root, key, pdf_state="imported", library_id=pid, pdf_error="")
        return {"library_id": pid}
    if endpoint == "/skip":
        key = str(body.get("paper_key") or "")
        if not key:
            raise ValueError("論文識別資料不正確。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        planning_store.skip(root, key)
        return {"ok": True}
    if endpoint == "/ai-summary":
        # Optional, user-triggered abstract organization. Never fetch a PDF or
        # read personal notes here; use exactly the selected metadata provided.
        works = body.get("papers") or []
        if not isinstance(works, list) or not works or len(works) > 1:
            raise ValueError("請選擇一篇候選論文再整理摘要。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        selected = [{"title": str(w.get("title") or "")[:400], "authors": str(w.get("authors") or "")[:500], "year": w.get("year"), "venue": str(w.get("venue") or "")[:300], "abstract": str(w.get("abstract") or "")[:5000]} for w in works]
        if not any(w["abstract"] for w in selected):
            raise ValueError("所選論文沒有可供整理的摘要。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        from . import chat_models, engines, usage
        cfg, model = chat_models.engine_cfg(config.load(), body.get("model"))
        prompt = "請使用繁體中文（臺灣用語）整理下列已取得的論文摘要。只能整理提供的資料；不得補造研究結果、引用或論文資訊。缺少資料請標示「未提供」，推論請明確標示。使用 Markdown，包含研究問題、方法、結果與限制。\n\n" + json.dumps(selected, ensure_ascii=False)  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        meter = usage.Meter(cfg.get("engine", ""))
        answer = engines.run(engines.for_translation(cfg), prompt, app.lib.root, [], None, meter)
        return {"text": answer, "model": model.get("name", model.get("id", "")), "source_count": len(selected), "usage": meter.snapshot()}
    if endpoint == "/ai-rewrite":
        selected = str(body.get("selected_text") or "").strip()[:2000]
        context = str(body.get("context") or "")[:2500]
        if not selected:
            raise ValueError("請先在草稿中選取要改寫的文字。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        from . import chat_models, engines, usage
        cfg, model = chat_models.engine_cfg(config.load(), body.get("model"))
        meter = usage.Meter(cfg.get("engine", ""))
        prompt = "請針對使用者選取的英文內容，提供 2 個意思相近、自然且符合學術寫作的英文改寫版本。保持原文主張強度，不新增資訊。只輸出 JSON 字串陣列。\n上下文（只用於理解）：" + context + "\n選取文字：" + selected  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        answer = engines.run(engines.for_translation(cfg), prompt, app.lib.root, [], None, meter)
        suggestions = _unwrap_json_list(engines.parse_json(answer))
        if not isinstance(suggestions, list) or not suggestions:
            raise ValueError("模型未回傳可用的改寫建議，請重試或自行修改。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        return {"suggestions": [str(x).strip()[:2000] for x in suggestions if str(x).strip()][:2], "model": model.get("name", model.get("id", "")), "usage": meter.snapshot()}
    if endpoint.startswith("/ui/"):
        from . import research_ui
        return research_ui.post(app, endpoint, body)
    if endpoint == "/reading-path":
        topic = str(body.get("topic") or "").strip()[:400]
        goal = str(body.get("goal") or "").strip()[:500]
        start_paper = body.get("start_paper") if isinstance(body.get("start_paper"), dict) else {}
        supplied = body.get("papers") if isinstance(body.get("papers"), list) else []
        if len(supplied) > 30:
            raise ValueError("一次最多可依據 30 篇論文規劃閱讀路徑。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        papers = []
        seen = set()
        def include(work):
            if not isinstance(work, dict) or not work.get("title"):
                return
            key = _key(work)
            if key not in seen:
                seen.add(key)
                papers.append(dict(work))
            elif isinstance(work.get("referenced_works"), list):
                # Legacy candidates omitted citation metadata. Enrich only the
                # matching retrieved source, never AI-generated plan fields.
                next(p for p in papers if _key(p) == key)["referenced_works"] = list(work["referenced_works"])
        for work in supplied:
            include(work)
        if topic:
            for work in _search_works(root, topic, sort="relevance_score:desc").get("results", []):
                include(work)
        if start_paper.get("title"):
            exact = None
            identifier = start_paper.get("openalex_id") or start_paper.get("doi")
            if identifier:
                lookup = _identifier_search(root, str(identifier))
                exact = next(iter(lookup.get("results", [])), None) if lookup else None
            if not exact:
                title_result = _search_works(root, str(start_paper.get("title")), sort="relevance_score:desc")
                normalized_title = " ".join(str(start_paper.get("title")).casefold().split())
                exact = next((work for work in title_result.get("results", []) if " ".join(str(work.get("title") or "").casefold().split()) == normalized_title), None)
            start_work = exact or start_paper
            start_work["is_start_paper"] = True
            include(start_work)

            # Auto-expand key foundational references and related follow-up works for the start paper
            ref_ids = [str(x).rstrip("/").split("/")[-1] for x in (start_work.get("referenced_works") or []) if x][:8]
            rel_ids = [str(x).rstrip("/").split("/")[-1] for x in (start_work.get("related_works") or []) if x][:8]
            expand_ids = [ident for ident in (ref_ids + rel_ids) if ident and ("openalex:" + ident.upper()) not in seen]
            if expand_ids:
                try:
                    batch = _openalex("/works", {
                        "filter": "openalex_id:" + "|".join(expand_ids[:15]),
                        "per-page": 15,
                        "select": "id,doi,title,authorships,publication_year,primary_location,best_oa_location,locations,open_access,cited_by_count,abstract_inverted_index"
                    })
                    for item in batch.get("results", []):
                        w = _work(item)
                        ident = (w.get("openalex_id") or "").rstrip("/").split("/")[-1]
                        if ident in ref_ids:
                            w["seed_relation"] = "reference"
                        elif ident in rel_ids:
                            w["seed_relation"] = "related"
                        include(w)
                except Exception:
                    pass
        if not papers:
            raise ValueError("找不到可用的論文資料，請先搜尋主題或選擇候選論文。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        if not topic and not start_paper.get("title"):
            raise ValueError("請輸入研究主題，或指定一篇起點論文。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        from . import chat_models, engines, usage
        cfg, model = chat_models.engine_cfg(config.load(), body.get("model"))
        meter = usage.Meter(cfg.get("engine", ""))

        # Prioritize candidates: start paper first, then user candidates/references, capped at 24 for weak-model stability
        capped_papers = []
        seen_capped = set()
        for w in papers:
            k = _key(w)
            if k not in seen_capped:
                seen_capped.add(k)
                capped_papers.append(w)
            if len(capped_papers) >= 24:
                break

        by_exact, by_alias, by_title = _build_paper_index(capped_papers)
        source = []
        for w in capped_papers:
            entry = {
                "id": _key(w),
                "title": str(w.get("title") or "")[:350],
                "year": w.get("year"),
                "venue": w.get("venue"),
                "citations": w.get("cited_by_count"),
                "abstract": str(w.get("abstract") or "")[:1200],
            }
            if w.get("is_start_paper"):
                entry["note"] = "起點論文（目前閱讀中）"  # i18n-ok: prompt note
            elif w.get("seed_relation") == "reference":
                entry["note"] = "起點論文之重要參考文獻（基礎背景）"  # i18n-ok: prompt note
            elif w.get("seed_relation") == "related":
                entry["note"] = "起點論文之相關研究（主流方法／衍生延伸）"  # i18n-ok: prompt note
            source.append(entry)

        prompt = (
            "你是一位學術研究導師。請根據下方已檢索到的候選論文清單，依研究目標規劃一份具備學習進階順序的閱讀路徑（由淺入深、由基礎到前沿，約 5 至 10 篇）。\n"  # i18n-ok: prompt
            "規則：\n"  # i18n-ok: prompt
            "1. 只能推薦清單中存在的論文 id，不得虛構論文或作者。\n"  # i18n-ok: prompt
            "2. 角色（role）請依閱讀順序精準標註：如「基礎入門」、「先備背景」、「主流方法」、「起點核心」、「代表性研究」、「延伸探索」、「前沿突破」等。\n"  # i18n-ok: prompt
            "3. 理由（reason）請具體說明「為什麼要先讀這篇、它為後續論文建立了什麼概念或提供了什麼方法」。\n"  # i18n-ok: prompt
            "4. priority 請標記閱讀順序（1 為最先讀，數字依序遞增）。\n"  # i18n-ok: prompt
            "5. 請使用繁體中文（臺灣用語）。輸出標準 JSON 陣列，每項包含 id、role、reason、priority。\n\n"  # i18n-ok: prompt
            f"研究目標：{goal or '建立主題脈絡，由淺入深掌握核心研究'}\n"  # i18n-ok: prompt
            f"主題：{topic or str(start_paper.get('title') or '')}\n"  # i18n-ok: prompt
            f"候選論文：{json.dumps(source, ensure_ascii=False)}"  # i18n-ok: prompt
        )

        answer = engines.run(engines.for_translation(cfg), prompt, app.lib.root, [], _cancel, meter)
        if _cancel is not None and _cancel.is_set():
            from .engines import Cancelled
            raise Cancelled()
        plan = _unwrap_json_list(engines.parse_json(answer))
        if not isinstance(plan, list):
            raise ValueError("模型回傳的閱讀路徑格式不正確，請重試。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。

        output = []
        seen_keys = set()
        for step in plan:
            if not isinstance(step, dict):
                continue
            work = _resolve_step_paper(step, by_exact, by_alias, by_title, capped_papers)
            if not work:
                continue
            wk = _key(work)
            if wk in seen_keys:
                continue
            seen_keys.add(wk)
            try:
                priority = int(step.get("priority") or len(output) + 1)
            except (TypeError, ValueError):
                priority = len(output) + 1
            output.append({
                "paper": work,
                "role": str(step.get("role") or tr("延伸探索"))[:80],
                "reason": str(step.get("reason") or "")[:800],
                "priority": max(1, min(30, priority)),
            })
        output.sort(key=lambda x: x["priority"])
        return {"results": output, "model": model.get("name", model.get("id", "")), "source_count": len(capped_papers), "usage": meter.snapshot()}
    if endpoint == "/ai-query":
        query = str(body.get("query") or "").strip()[:500]
        title = str(body.get("title") or "").strip()[:400]
        abstract = str(body.get("abstract") or "").strip()[:2000]
        if not query and not title and not abstract:
            raise ValueError("請先輸入研究主題或提供論文標題。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        from . import chat_models, engines, usage
        full_cfg = config.load()
        cfg, model = chat_models.engine_cfg(full_cfg, _fast_model_id(full_cfg))
        meter = usage.Meter(cfg.get("engine", ""))
        if title or abstract:
            prompt = (
                "你是一位學術文獻檢索專家。請依據下方論文的標題與摘要，提煉出 3 到 5 組精簡、標準的英文學術檢索關鍵詞或主題短語（Task、Method、Domain）。\n"  # i18n-ok: prompt
                "規則：\n"  # i18n-ok: prompt
                "1. 請去除自創的模型代號、專有名稱前綴或過於狹隘的專案簡稱（例如 Mask2Former、LLaVA、BERT-XYZ），提煉出通用學術方法與任務名詞（例如 Masked Attention Image Segmentation、Visual Instruction Tuning）。\n"  # i18n-ok: prompt
                "2. 關鍵詞長度宜為 2 至 5 個英文單詞，適合直接在 OpenAlex 或 Google Scholar 檢索。\n"  # i18n-ok: prompt
                "3. 保持純英文術語，每組短語務必精準聚焦在核心研究主題與方法。\n"  # i18n-ok: prompt
                "4. 只輸出標準 JSON 陣列字串（例如 [\"Keyword 1\", \"Keyword 2\", ...]），不要有其他說明文字。\n\n"  # i18n-ok: prompt
                f"論文標題：{title}\n"  # i18n-ok: prompt
                f"摘要：{abstract}"  # i18n-ok: prompt
            )
        else:
            prompt = "將使用者的繁體中文研究主題轉為 3 到 5 組精簡、可編輯的英文 OpenAlex 搜尋詞。保留標準學術術語；不擴張成未提及的研究方向。只輸出 JSON 陣列字串。\n主題：" + query  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        answer = engines.run(engines.for_translation(cfg), prompt, app.lib.root, [], None, meter)
        suggestions = _unwrap_json_list(engines.parse_json(answer))
        if not isinstance(suggestions, list):
            raise ValueError("模型回傳的關鍵字格式不正確，請直接使用原始關鍵字搜尋。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        return {"suggestions": [str(x).strip()[:160] for x in suggestions if str(x).strip()][:5], "model": model.get("name", model.get("id", "")), "usage": meter.snapshot()}
    if endpoint == "/ai-category-explain":
        category = str(body.get("category") or "")[:100]
        work = body.get("paper") or {}
        if not category or not work.get("title"):
            raise ValueError("請提供分類與論文資料。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        from . import chat_models, engines, usage
        cfg, model = chat_models.engine_cfg(config.load(), body.get("model"))
        meter = usage.Meter(cfg.get("engine", ""))
        source = {"title": str(work.get("title"))[:400], "abstract": str(work.get("abstract") or "")[:5000]}
        prompt = "請用繁體中文說明這篇論文與使用者選擇的分類可能有何關聯。只能根據提供的標題與摘要；若資料不足，直接說明不足，不要替論文補造方法或成果。這是待審閱的 AI 解讀，不是資料來源證明。\n分類：" + category + "\n論文資料：" + json.dumps(source, ensure_ascii=False)  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
        answer = engines.run(engines.for_translation(cfg), prompt, app.lib.root, [], None, meter)
        return {"text": answer, "model": model.get("name", model.get("id", "")), "usage": meter.snapshot()}
    raise ValueError("不支援的研究工作區操作。")  # i18n-ok: Research 工作區訊息依產品規格固定使用繁體中文。
