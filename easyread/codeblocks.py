"""Keep model-generated listings in the existing para/Markdown format."""
from __future__ import annotations

import re

# Match the whole delimiter run: a shorter run inside a longer fence is literal.
CODE = re.compile(r"(?<![\\`])(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)")


def parts(text: str) -> list[str]:
    """Alternate prose/code spans, starting and ending with prose."""
    out, start = [], 0
    for match in CODE.finditer(text):
        out.extend((text[start:match.start()], match.group()))
        start = match.end()
    out.append(text[start:])
    return out


def prose(text: str) -> str:
    return " ".join(parts(text)[::2])


def has_fence(text: str) -> bool:
    return any(len(m.group(1)) >= 3 and "\n" in m.group() for m in CODE.finditer(text))


def _fence(text: str, language: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and CODE.fullmatch(stripped):
        return text
    width = max(3, 1 + max((len(m.group()) for m in re.finditer(r"`+", text)), default=0))
    fence = "`" * width
    return f"{fence}{language}\n{text}" + ("" if text.endswith("\n") else "\n") + fence


def normalize(block: dict) -> None:
    """Accept code/listing aliases without adding a new persisted block type.

    Leave malformed listings untouched so normal validation rejects them rather
    than silently marking an empty page as translated.
    """
    if block.get("type") not in ("code", "listing"):
        return
    source = next((block[k] for k in ("en", "code") if isinstance(block.get(k), str) and block[k].strip()), "")
    lines = block.get("lines")
    if not source and isinstance(lines, list) and all(isinstance(line, str) for line in lines):
        source = "\n".join(lines)
    if not source.strip():
        return
    language = block.get("lang") or block.get("language") or ""
    language = language if isinstance(language, str) and re.fullmatch(r"[\w+-]+", language) else ""
    translated = block.get("zh")
    for side, body in (("en", source), ("zh", translated)):
        if isinstance(body, str) and body:
            text = _fence(body, language)
            caption = block.get(f"caption_{side}")
            block[side] = text + (f"\n\n{caption}" if isinstance(caption, str) and caption else "")
    block["type"] = "para"
    block.pop("sents", None)


# Noncapturing alternatives: consumers may wrap this pattern in one split group.
MATH = re.compile(
    r"(?<!\\)\$\$[\s\S]+?(?<!\\)\$\$"
    r"|(?<![\\$])\$(?!\$)(?:\\\$|[^$])+?(?<!\\)\$(?!\$)"
    r"|(?<!\\)\\\[[\s\S]+?(?<!\\)\\\]"
    r"|(?<!\\)\\\([\s\S]+?(?<!\\)\\\)"
)
