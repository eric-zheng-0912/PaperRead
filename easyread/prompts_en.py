"""“只读原文”的两段提示词：
- structure：不翻译，只把原页整理成和译文一样的块（段落、标题、公式、表格），文字都放 en；
- fill：之后想看中文了，给已经整理好的块就地补上 zh，块 id 不变，笔记、划线都还挂得住。"""
from __future__ import annotations

import json

from . import langs, links
from .prompts import _context, _page_texts, peek_note, rules, TEXT_RULES
from .store import Workspace

RULES_EN = """整理要求（不翻译）：
- 只把原文整理好，不翻译、不解释、不总结，不加原文没有的内容。原文的笔误照录。
- 保留材料中的 Markdown 链接 [文字](URL)，文字和 URL 原样保留，不猜网址，不把链接移到段落外。
- 保留原文的阅读顺序、章节编号、引用号 [n]。双栏论文按栏读，断在行尾的连字符单词拼回去。
- 行内数学一律写成 $TeX$（KaTeX 能渲染的 LaTeX），变量、下标、上标都要用 TeX，不要用 Unicode 拼。行间公式单独成 math 块，照原页重排，原编号放 tag。
- 表格重排成 table 块，数字与公式原样。图使用 figure 块，只保留 page、num 与图注（caption_en / caption_zh）；不要识别、抄写或翻译图内文字，不输出 image_en、image_zh 或裁切框，不截取图片，src 留空。读者将在原 PDF 核对图片。
- 参考文献列表：输出一个 references 块，条目放进 references 数组（id 是编号，text 是原文）。
- 看不清的地方写“[unclear, see page N]”，不要猜。
- 页眉、页脚、页码、arXiv 侧边水印不要输出。"""

RULES_EN += TEXT_RULES

SCHEMA_EN = """输出格式：只输出一个 JSON 对象，不要任何别的文字。
{
  "meta": {"title_en": "", "authors": "作者, 用逗号分隔", "affiliation": "", "date": "", "venue": ""},   // 只有包含第 1 页时才写
  "references": [{"id": "1", "text": "原文条目"}],             // 本批出现参考文献列表时才写
  "blocks": [ ... ]
}
块（每块都要 id、type、page；page 是这块在原 PDF 中开始的页码；文字只写 en，不写 zh）：
- {"id":"p3-2","type":"para","page":3,"en":"原文（行内数学也写成 $TeX$）"}   摘要段落加 "role":"abstract"；紧接在公式后的半句（如 where …）加 "cont": true
- {"id":"s2-1","type":"heading","page":2,"level":1或2,"num":"2.1","en":"Independent questions"}   附录标题加 "appendix": true，摘要标题 num 留空
- {"id":"p2-5","type":"list","page":2,"ordered":true,"items":[{"en":"…"}]}
- {"id":"eq1","type":"math","page":3,"tex":"…","tag":"1"}   没有编号不写 tag；多行用 \\begin{aligned}…\\end{aligned}
- {"id":"tab2","type":"table","page":3,"num":"2","head":[["","Questions","…"]],"rows":[["MATH","5,000","65.5%\\n(0.7%)"]],"align":"lrr","caption_en":"Table 2: …"}
- {"id":"fig1","type":"figure","page":4,"num":"1","src":"","caption_en":"Figure 1: …","caption_zh":"图 1：…"}
- {"id":"refs","type":"references","page":10,"en":"References"}
id 规则：段落 p{页}-{序号}，标题 s{编号，点换成横线}，公式 eq{编号} 或 eq-p{页}-{序号}，表 tab{编号}，图 fig{编号}。
注意 JSON 里 TeX 的反斜杠要写两个（\\\\frac、\\\\text、\\\\bar）。字符串里的英文双引号要转义成 \\"。表格和图放在正文第一次提到它的段落之后。"""


def structure(ws: Workspace, pages: list[int], engine: str, next_head: str, skip_head: bool = False, peek=()) -> str:
    see = ""
    if engine == "claude":
        imgs = "、".join(f"extract/page-{n:03d}.jpg" for n in pages)
        see = f"\n先用 Read 工具看原页图 {imgs}，以原页为准核对公式、表格、上下标和阅读顺序（双栏论文按栏读）。抽取的文字只作参考。"
    elif engine == "attached":
        see = "\n附上了这几页的原页图，以原页为准核对公式、表格和阅读顺序。"
    see += peek_note(engine, pages, list(peek))
    look = ("\n===== 下一页开头（只用来把本批最后一段补完整，其余不要输出）=====\n" + next_head) if next_head else ""
    return (f"你在把一篇学术论文的 PDF 整理成便于阅读的结构化原文（读者要直接读英文，不要翻译），这次只处理第 {', '.join(map(str, pages))} 页。{see}\n\n"
            f"{_context(ws, pages, skip_head=skip_head)}\n\n{RULES_EN}\n\n{SCHEMA_EN}\n\n" + _page_texts(ws, pages) + look)


def todo(blocks: list[dict]) -> dict[str, object]:
    """这些块里还没有中文的地方：{键: 英文}。键同页面：块 id、id#caption、id#image（图内文字）、id#序号、id#head（表头）。"""
    out: dict[str, object] = {}
    for b in blocks:
        t = b.get("type")
        if t in ("para", "heading") and b.get("en") and not b.get("zh"):
            out[b["id"]] = b["en"]
        elif t == "list":
            for i, it in enumerate(b.get("items", [])):
                if it.get("en") and not it.get("zh"):
                    out[f"{b['id']}#{i}"] = it["en"]
        elif t in ("table", "figure") and b.get("caption_en") and not b.get("caption_zh"):
            out[f"{b['id']}#caption"] = b["caption_en"]
            if t == "table" and b.get("head"):
                out[f"{b['id']}#head"] = b["head"]
    return out


def fill(ws: Workspace, pages: list[int], items: dict[str, object]) -> str:
    """给已经整理好的原文块补译文。"""
    urls = links.ensure(ws)
    records = [link for page in pages for link in urls.get(str(page), [])]
    items = {key: links.insert(value, records) if isinstance(value, str) else value for key, value in items.items()}
    first = 1 in pages
    target = langs.of_paper(ws.load("paper").get("meta"))
    name = langs.prompt_name(target)
    meta = ('  "meta": {"title_zh": "", "short_zh": "' + ("不超过 12 字的短标题" if target in ("zh", "zh-TW") else "不超过 6 个词的短标题") + '"},   // 论文英文标题：'
            + json.dumps(ws.load("paper").get("meta", {}).get("title_en", ""), ensure_ascii=False) + "\n") if first else ""
    return (f"你在把一篇学术论文译成{name}。原文已经整理成块，这次只翻译第 {', '.join(map(str, pages))} 页上下面这些键对应的文字。\n\n"
            f"{_context(ws, pages, new_blocks=False)}\n\n{rules(target)}\n"
            f"- 表头（键以 #head 结尾）是二维数组：保持行列数不变，把文字译成{name}，数字和符号原样。\n"
            + (f"- 英文里的 ‖ 是句子分界：译文在对应的句子交界处也插 ‖，个数和这条英文的一样。两句英文在{name}里要合成一句时，也在合并后最接近的位置插上。\n"
               if any(isinstance(v, str) and "‖" in v for v in items.values()) else "") + "\n"
            "输出格式：只输出一个 JSON 对象，不要任何别的文字。\n{\n" + meta +
            '  "glossary": [{"en": "standard error", "zh": "' + ("標準誤差" if target == "zh-TW" else "标准误差" if target == "zh" else name + "译名") + '"}],   // 本批新出现的核心术语\n'
            '  "checks": [{"anchor": "块 id", "quote": "译文里相关的几个字（可空）", "title": "一句话：哪里不对", "body": "具体说明和依据"}],   // 原文有问题时才写\n'
            '  "zh": {"键": "' + name + '译文", …}   // 下面每个键都要有，一个不漏\n}\n'
            "注意 JSON 里 TeX 的反斜杠要写两个（\\\\frac、\\\\text、\\\\bar）。" + ("字串內的中文引號請用「」或『』。" if target == "zh-TW" else "字符串里的中文引号用“”或「”。" if target == "zh" else "字符串里的英文双引号要转义。") + "\n\n"
            "要翻译的内容（键 → 英文）：\n" + json.dumps(items, ensure_ascii=False, indent=1))
