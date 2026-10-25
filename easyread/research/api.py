from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

from .. import chat_models, config, prefs
from ..i18n import tr
from . import comparison, proposals, tasks
from .store import normalize_folders, set_vault, settings, vault_path

PREFIX = "/api/research/v1"


def get(app, request_path: str):
    parsed = urlparse(request_path)
    path = unquote(parsed.path)
    if not path.startswith(PREFIX):
        return None
    endpoint = path[len(PREFIX):] or "/status"
    query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
    if endpoint == "/status":
        vault = vault_path()
        cfg = config.load()
        models = [{"id": item.get("id"), "name": item.get("name"), "engine": item.get("engine")}
                  for item in chat_models.models(cfg)]
        return {"vault": str(vault) if vault else "", "configured": bool(vault), "model": settings().get("model") or (cfg.get("chat") or {}).get("default", ""),
                "models": models, "papers": len(app.lib.all()), "tasks": tasks.list_tasks(scope=tasks.scope_id(app.lib.root), storage=tasks.storage_for(app.lib.root)),
                "pending_comparison": proposals.latest_pending_comparison(),
                "latest_comparison": proposals.latest_comparison()}
    if endpoint == "/comparisons":
        return {"comparisons": proposals.list_comparisons()}
    if endpoint.startswith("/tasks/"):
        task = tasks.get(endpoint.rsplit("/", 1)[1], scope=tasks.scope_id(app.lib.root), storage=tasks.storage_for(app.lib.root))
        if task is None:
            raise ValueError(tr("找不到這個研究工作。"))
        return task
    if endpoint.startswith("/proposals/") and not endpoint.endswith("/review") and not endpoint.endswith("/title"):
        return proposals.get(endpoint.rsplit("/", 1)[1])
    raise ValueError(tr("找不到這個 Research API 路徑。"))


def post(app, endpoint: str, body: dict):
    if endpoint == "/setup":
        path = set_vault(str(body.get("vault") or "").strip(), body.get("folders"))
        return {"vault": str(path), "configured": True, "notes": proposals.list_notes()}
    if endpoint == "/settings":
        patch = {}
        if "model" in body:
            model_id = str(body["model"] or "")
            if model_id:
                chat_models.find(config.load(), model_id)
            patch["model"] = model_id
        if "folders" in body:
            patch["folders"] = normalize_folders(body["folders"])
        if patch:
            prefs.save({"research": patch})
        return {"model": settings().get("model", "")}
    if endpoint.startswith("/tasks/") and endpoint.endswith("/cancel"):
        task = tasks.cancel(endpoint.split("/")[-2], scope=tasks.scope_id(app.lib.root), storage=tasks.storage_for(app.lib.root))
        if task is None:
            raise ValueError(tr("找不到這個研究工作。"))
        return task
    if endpoint == "/comparison":
        return comparison.start(app.lib, body.get("paper_ids"), body.get("dimensions"),
                                str(body.get("focus") or ""), str(body.get("model") or ""))
    if endpoint.startswith("/proposals/") and endpoint.endswith("/title"):
        identity = endpoint.split("/")[-2]
        return proposals.rename(identity, str(body.get("title") or ""))
    if endpoint.startswith("/proposals/") and endpoint.endswith("/review"):
        identity = endpoint.split("/")[-2]
        return proposals.decide(identity, str(body.get("decision") or "") == "accept")
    raise ValueError(tr("找不到這個 Research API 路徑。"))
