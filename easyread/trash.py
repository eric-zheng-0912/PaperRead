"""回收站：删掉的论文整个文件夹挪到 文献库/.trash/<id>-<时间>，可以恢复、彻底删除、清空。"""
from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

from .i18n import tr
from .store import read_json

_NAME = re.compile(r"^([0-9a-f]{12})-(\d{14})$")


def _dir(root: Path) -> Path:
    return root / ".trash"


def _entry(root: Path, name: str) -> Path:
    if not _NAME.match(name or ""):
        raise ValueError(tr("回收站里没有这一项"))
    p = _dir(root) / name
    if not p.is_dir():
        raise ValueError(tr("回收站里没有这一项"))
    return p


def items(root: Path) -> list[dict]:
    out = []
    for p in _dir(root).glob("*") if _dir(root).is_dir() else []:
        m = _NAME.match(p.name)
        if not m or not p.is_dir():
            continue
        error = ""
        try:
            paper = read_json(p / "paper.json", {})
            if not isinstance(paper, dict) or not isinstance(paper.get("meta", {}), dict):
                raise ValueError("Invalid paper metadata")
            meta = paper.get("meta", {})
        except (UnicodeError, ValueError, OSError):
            # Only a display fallback: do not save or repair the damaged file.
            meta = {"title_en": m.group(1)}
            error = f"Cannot read data file: {p / 'paper.json'}; original preserved"
        out.append({"name": p.name, "id": m.group(1), "title_zh": meta.get("title_zh", ""), "title_en": meta.get("title_en", ""),
                    "authors": meta.get("authors", ""), "deleted": datetime.strptime(m.group(2), "%Y%m%d%H%M%S").isoformat(timespec="seconds")})
        if error:
            out[-1]["load_error"] = error
    return sorted(out, key=lambda x: x["deleted"], reverse=True)


def restore(root: Path, name: str) -> str:
    src = _entry(root, name)
    pid = _NAME.match(name).group(1)
    if (root / pid).exists():
        raise ValueError(tr("文献库里已经有这篇论文了（可能后来又导入过一次）"))
    shutil.move(str(src), root / pid)
    return pid


def purge(root: Path, name: str) -> int:
    """彻底删掉回收站里的一篇。"""
    shutil.rmtree(_entry(root, name))
    return 1


def empty(root: Path) -> int:
    """清空回收站，返回删掉了几篇。"""
    targets = [p for p in _dir(root).glob("*") if p.is_dir() and _NAME.match(p.name)] if _dir(root).is_dir() else []
    for p in targets:
        shutil.rmtree(p)
    return len(targets)
