"""Conservative, local PDF table extraction. No model is used to invent structure.

Only single-page rectangular tables with a nearby Table caption are accepted.
Every visible glyph must belong to exactly one cell; merged cells and uncertain
headers are rejected. Footnotes and sources are kept as ordered source strings.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from statistics import median
from types import SimpleNamespace

from . import tables, table_policy

VERSION = 1
_CAPTION = re.compile(r"^Table\s+([A-Za-z]?\d+(?:[.\-]\d+)*)(?=$|\s|[:.\u2013\u2014])", re.I)
_NOTE = re.compile(r"^(?:(?:Notes?|Sources?)\s*[:.]|Standard errors?\b|\*|†|‡)", re.I)
_CONTINUED = re.compile(r"\bcontinu(?:ed|ation)\b", re.I)


class UnreliableTable(ValueError):
    pass


@dataclass
class PageTables:
    sources: list[dict]
    issues: list[str]
    unresolved: list[dict] = field(default_factory=list)


def caption_number(block):
    caption = block.get("caption_en")
    match = _CAPTION.match(caption) if isinstance(caption, str) else None
    return match.group(1) if match else ""


def _inside(char, box):
    x, y = (char["x0"] + char["x1"]) / 2, (char["top"] + char["bottom"]) / 2
    return box[0] <= x < box[2] and box[1] <= y < box[3]


def _lines(page):
    lines = []
    for word in sorted(page.extract_words(x_tolerance=2, y_tolerance=2), key=lambda w: (w["top"], w["x0"])):
        line = next((ln for ln in reversed(lines) if abs(ln["top"] - word["top"]) < 3), None)
        if line is None:
            line = {"top": word["top"], "bottom": word["bottom"], "words": []}
            lines.append(line)
        line["bottom"] = max(line["bottom"], word["bottom"])
        line["words"].append(word)
    for line in lines:
        line["words"].sort(key=lambda w: w["x0"])
        line["text"] = " ".join(w["text"] for w in line["words"])
    return sorted(lines, key=lambda ln: ln["top"])


def _matrix(page, table, method):
    if not table_policy.enabled():
        raise UnreliableTable(table_policy.DISABLED)
    rows = table.rows
    if len(rows) < 2 or len(rows[0].cells) < 2:
        raise UnreliableTable("Insufficient rows or columns")
    width = len(rows[0].cells)
    if any(len(row.cells) != width or any(cell is None for cell in row.cells) for row in rows):
        raise UnreliableTable("Merged or irregular cells")
    # A larger cell spanning other rows/columns must never be flattened.
    for row in rows:
        if any(abs(cell[1] - row.bbox[1]) > 1 or abs(cell[3] - row.bbox[3]) > 1 for cell in row.cells):
            raise UnreliableTable("Row spans")
        if any(abs(cell[0] - rows[0].cells[i][0]) > 1 or abs(cell[2] - rows[0].cells[i][2]) > 1
               for i, cell in enumerate(row.cells)):
            raise UnreliableTable("Column spans")
    chars = [c for c in page.chars if c["text"].strip() and _inside(c, table.bbox)]
    if method == "text" and any(c["text"].strip() and
                              _inside(c, (page.bbox[0], table.bbox[1], page.bbox[2], table.bbox[3])) and
                              not _inside(c, table.bbox) for c in page.chars):
        raise UnreliableTable("Borderless extraction may omit an outside column")
    cells = [cell for row in rows for cell in row.cells]
    if not chars or any(sum(_inside(c, cell) for cell in cells) != 1 for c in chars):
        raise UnreliableTable("Glyph coverage mismatch")
    if method == "text":
        # Implied grid lines must not turn intact source words into fragments.
        # Statistical digits and a caption alone also occur in ordinary prose.
        for word in page.extract_words(x_tolerance=2, y_tolerance=2):
            box = (word["x0"], word["top"], word["x1"], word["bottom"])
            owners = {i for c in chars if _inside(c, box)
                      for i, cell in enumerate(cells) if _inside(c, cell)}
            if len(owners) > 1:
                raise UnreliableTable("Borderless grid splits a source word")
    if any("\ufffd" in c["text"] or any(ord(ch) < 32 for ch in c["text"]) for c in chars):
        raise UnreliableTable("Unmapped PDF glyphs")
    extracted = table.extract(x_tolerance=2, y_tolerance=2)
    if len(extracted) != len(rows) or any(not isinstance(cell, str) for row in extracted for cell in row):
        raise UnreliableTable("Invalid extracted cells")
    physical_rows = [row for row, values in zip(rows, extracted) if any(cell and cell.strip() for cell in values)]
    matrix = extracted
    # text-strategy creates physical whitespace rows; only truly empty rows go.
    matrix = [row for row in matrix if any(cell.strip() for cell in row)]
    if len(matrix) < 2 or any(not isinstance(cell, str) for row in matrix for cell in row):
        raise UnreliableTable("Empty table")
    # Extraction must neither drop nor duplicate any non-whitespace glyph.
    from collections import Counter
    # Expand only Latin presentation ligatures for comparison, never cell data.
    # General compatibility normalization could also change numbers or units.
    ligatures = str.maketrans({"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl",
                               "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st"})
    glyphs = Counter(ch for c in chars for ch in c["text"].translate(ligatures) if not ch.isspace())
    text = Counter(ch for row in matrix for cell in row for ch in cell.translate(ligatures) if not ch.isspace())
    if glyphs != text:
        raise UnreliableTable("Extracted text does not match source glyphs")
    if any(re.search(r"[\u3400-\u9fff]", c["text"]) for c in chars):
        raise UnreliableTable("Source is not an English table")
    # Superscripts and mixed math layout require visual reconstruction, not a
    # linear text heuristic. A single ambiguous cell rejects the entire table.
    for cell in cells:
        sizes = [c["size"] for c in chars if _inside(c, cell)]
        if sizes and max(sizes) > min(sizes) * 1.25:
            raise UnreliableTable("Superscript or mixed-size mathematical layout")
    bold = []
    for row in rows:
        rc = [c for c in chars if any(_inside(c, cell) for cell in row.cells)]
        if rc:
            bold.append(all(re.search(r"bold|black|demi", c.get("fontname", ""), re.I) for c in rc))
    header_count = 0
    for flag in bold:
        if not flag:
            break
        header_count += 1
    if not header_count:
        # Non-bold headers are accepted only when there is a clear transition
        # from textual headings to data containing numbers in value columns.
        for i, row in enumerate(matrix):
            if any(re.search(r"\d", c) for c in row[1:]):
                header_count = i
                break
    if not 0 < header_count < len(matrix):
        raise UnreliableTable("Header boundary is ambiguous")
    if method == "text" and (len(matrix) - header_count < 2 or any("\n" in c for row in matrix for c in row)):
        raise UnreliableTable("Insufficient borderless alignment evidence")
    if method == "text" and any(not any(re.search(r"\d", c) for c in row) for row in matrix[header_count:]):
        raise UnreliableTable("Borderless rows lack statistical alignment evidence")
    return matrix[:header_count], matrix[header_count:], physical_rows


def _word_gutter_table(page, caption, end):
    """Infer a grid only from repeated, intact-word gutters and regular rows.

    A bold header, independent full rows and an explicit closing note are
    required. Sparse rows use the same established gutters, never new columns.
    The adapter must still pass every existing text-mode _matrix safety check.
    """
    lines = [ln for ln in _lines(page) if ln['top'] > caption['bottom'] + 2 and ln['bottom'] < end]
    if len(lines) < 4 or lines[0]['top'] - caption['bottom'] >= 40:
        raise UnreliableTable('Insufficient independent borderless rows')
    if not any(abs(ln['top'] - end) < 1 and _NOTE.match(ln['text']) for ln in _lines(page)):
        raise UnreliableTable('Borderless table has no verified closing boundary')
    height = median(w['bottom'] - w['top'] for ln in lines for w in ln['words'])
    gaps = [b['top'] - a['top'] for a, b in zip(lines, lines[1:])]
    if height <= 0 or min(gaps) < height * 1.15 or max(gaps) > min(gaps) * 1.35:
        raise UnreliableTable('Wrapped or irregular borderless row spacing')
    runs = []
    for ln in lines:
        grouped = []
        for word in ln['words']:
            if not grouped or word['x0'] - grouped[-1][-1]['x1'] >= height * 1.5:
                grouped.append([])
            grouped[-1].append(word)
        runs.append(grouped)
    columns = len(runs[0])
    if columns < 2 or any(len(row) > columns for row in runs):
        raise UnreliableTable('Ambiguous borderless column count')
    header = [c for c in page.chars if c['text'].strip() and lines[0]['top'] - 1 <= c['top'] and c['bottom'] <= lines[0]['bottom'] + 1]
    if not header or not all(re.search(r'bold|black|demi', c.get('fontname', ''), re.I) for c in header):
        raise UnreliableTable('Borderless header lacks independent font evidence')
    complete = [row for row in runs if len(row) == columns]
    if len(complete) < 3:
        raise UnreliableTable('Insufficient full-row column evidence')
    separators = []
    for col in range(columns - 1):
        left = max(row[col][-1]['x1'] for row in complete)
        right = min(row[col + 1][0]['x0'] for row in complete)
        if right - left < height * 1.5:
            raise UnreliableTable('No shared word-safe column gutter')
        separators.append((left + right) / 2)
    x0 = max(page.bbox[0], min(w['x0'] for ln in lines for w in ln['words']) - 1)
    x1 = min(page.bbox[2], max(w['x1'] for ln in lines for w in ln['words']) + 1)
    edges = [x0, *separators, x1]
    ys = [lines[0]['top'] - 1, *[(a['bottom'] + b['top']) / 2 for a, b in zip(lines, lines[1:])], lines[-1]['bottom'] + 1]
    matrix, rows = [], []
    for index, ln in enumerate(lines):
        values = [[] for _ in range(columns)]
        for word in ln['words']:
            owners = [j for j in range(columns) if edges[j] <= word['x0'] and word['x1'] <= edges[j + 1]]
            if len(owners) != 1:
                raise UnreliableTable('Borderless gutter intersects a source word')
            values[owners[0]].append(word['text'])
        matrix.append([' '.join(v) for v in values])
        cells = [(edges[j], ys[index], edges[j + 1], ys[index + 1]) for j in range(columns)]
        rows.append(SimpleNamespace(cells=cells, bbox=(x0, ys[index], x1, ys[index + 1])))
    return SimpleNamespace(rows=rows, bbox=(x0, ys[0], x1, ys[-1]), extract=lambda **kw: matrix)


def _bounded_box(page, box):
    """Intersect in PDF coordinates; never round or assume a zero origin."""
    if (len(box) != 4 or not all(math.isfinite(n) for n in box) or
            box[0] >= box[2] or box[1] >= box[3]):
        raise UnreliableTable("Invalid PDF crop region")
    bounds = page.bbox
    box = (max(bounds[0], box[0]), max(bounds[1], box[1]),
           min(bounds[2], box[2]), min(bounds[3], box[3]))
    if box[0] >= box[2] or box[1] >= box[3]:
        raise UnreliableTable("Empty PDF crop region")
    return box


def _crop(page, box):
    box = _bounded_box(page, box)
    return page if box == tuple(page.bbox) else page.crop(box)


def _visible_page(page):
    # pdfplumber otherwise includes hidden text outside CropBox, unlike the
    # page image shown to the reader. Partial glyphs need visual verification.
    if tuple(page.bbox) != tuple(page.cropbox):
        box = _bounded_box(page, page.cropbox)
        if any(_inside(c, box) and (c["x0"] < box[0] or c["x1"] > box[2] or c["top"] < box[1] or c["bottom"] > box[3])
               for c in page.chars):
            raise UnreliableTable("Partially cropped table glyphs require PDF review")
        page = _crop(page, box)
    return page


def _extract_page(page, number: int, *, borderless: str) -> PageTables:
    """Use pdfplumber's actual cell geometry, never model-supplied rows."""
    if not table_policy.enabled():
        return PageTables([], [table_policy.DISABLED])
    try:
        page = _visible_page(page)
    except ValueError as exc:
        return PageTables([], [str(exc)])
    lines = _lines(page)
    # "Table 1 contains ..." in body prose is a reference, not a caption.
    # Without punctuation or bold label evidence, leave it to PDF review.
    def caption_line(line):
        match = _CAPTION.match(line["text"])
        if not match:
            return False
        if line["text"][match.end():].lstrip().startswith((".", ":", "\u2013", "\u2014")):
            return True
        label_chars = [c for c in page.chars if line["top"] - 1 <= c["top"] <= line["bottom"] and c["x0"] <= line["words"][1]["x1"]]
        return bool(label_chars) and all(re.search(r"bold|black|demi", c.get("fontname", ""), re.I) for c in label_chars)
    captions = []
    for index, line in enumerate(lines):
        match = _CAPTION.match(line["text"])
        if match and not line["text"][match.end():].strip():
            # A bare label needs a nearby, aligned title, even if it is bold.
            # Do not let wrapped body references become caption candidates.
            if index + 1 >= len(lines):
                continue
            title = lines[index + 1]
            height = line["bottom"] - line["top"]
            left, right = line["words"][0]["x0"], line["words"][-1]["x1"]
            tl, tr = title["words"][0]["x0"], title["words"][-1]["x1"]
            text = title["text"]
            if (not 0 <= title["top"] - line["bottom"] <= min(12, height) or
                    not (abs(left - tl) <= 6 or abs((left + right) - (tl + tr)) <= 12) or
                    len(text.split()) < 2 or not re.match(r"[A-Z]", text) or
                    text.endswith((".", ":", ";", "?", "!")) or
                    _CAPTION.match(text) or _NOTE.match(text) or
                    re.match(r"(?:shows?|provides?|presents?|contains?|reports?|summarizes?|"
                             r"illustrates?|lists?|compares?|is|are|was|were|and|for|in|of|the)\b", text, re.I)):
                continue
            line = {**line, "text": line["text"] + " " + text,
                    "bottom": title["bottom"], "words": line["words"] + title["words"]}
        elif not caption_line(line):
            continue
        captions.append((line, match))
    found, issues, unresolved = [], [], []
    ruled = page.find_tables()
    for caption, match in captions:
        num = match.group(1)
        try:
            if _CONTINUED.search(caption["text"]):
                raise UnreliableTable("Continued table requires cross-page evidence")
            # Search only below the caption, stopping before notes / next
            # caption. Caption-below tables intentionally retain the PDF.
            end = min([ln["top"] for ln in lines if ln["top"] > caption["bottom"] + 2 and
                       (_NOTE.match(ln["text"]) or _CAPTION.match(ln["text"]))] + [page.bbox[3] - 18])
            candidates = [t for t in ruled if 0 <= t.bbox[1] - caption["bottom"] < 40 and t.bbox[3] <= end + 2]
            method = "lines"
            if not candidates:
                if any(0 <= t.bbox[1] - caption["bottom"] < 40 for t in ruled):
                    raise UnreliableTable("Table and footnote boundaries overlap")
                method = borderless
                if end - caption["bottom"] < 20:
                    raise UnreliableTable("No table below caption")
                if borderless == 'word-gutters':
                    candidates = [_word_gutter_table(page, caption, end)]
                else:
                    area = _crop(page, (page.bbox[0], caption["bottom"] + 2, page.bbox[2], end))
                    candidates = [t for t in area.find_tables({"vertical_strategy": "text", "horizontal_strategy": "text",
                                                             "min_words_vertical": 3, "text_x_tolerance": 2,
                                                             "text_y_tolerance": 2})
                                  if 0 <= t.bbox[1] - caption["bottom"] < 40]
            if len(candidates) != 1:
                raise UnreliableTable("Missing or ambiguous table geometry")
            table = candidates[0]
            head, rows, physical_rows = _matrix(page, table, 'text' if method == 'word-gutters' else method)
            if table.bbox[3] >= page.bbox[3] - 30:
                raise UnreliableTable("Table touches page boundary")
            # A caption may wrap above the table. Reject prose between the
            # caption and table instead of silently discarding it.
            extra = [ln for ln in lines if caption["bottom"] < ln["top"] < table.bbox[1] - 2]
            if len(extra) > 2:
                raise UnreliableTable("Caption boundary is ambiguous")
            cap = " ".join([caption["text"], *(ln["text"] for ln in extra)])
            notes, note_boxes, last, note_size = [], [], table.bbox[3], None
            below = [ln for ln in lines if ln["top"] >= table.bbox[3] - 1]
            for ln in below:
                if ln["top"] - last > 18 or _CAPTION.match(ln["text"]):
                    break
                note_chars = [c for c in page.chars if c["text"].strip() and ln["top"] - 1 <= c["top"] <= ln["bottom"]]
                size = min((c["size"] for c in note_chars), default=0)
                if _NOTE.match(ln["text"]):
                    notes.append(ln["text"])
                    note_boxes.append([min(w["x0"] for w in ln["words"]), ln["top"], max(w["x1"] for w in ln["words"]), ln["bottom"]])
                    note_size = size
                elif notes and ln["top"] - last < 7 and note_size and abs(size - note_size) <= note_size * 0.05:
                    notes[-1] += " " + ln["text"]
                    note_boxes[-1] = [min(note_boxes[-1][0], min(w["x0"] for w in ln["words"])), note_boxes[-1][1],
                                      max(note_boxes[-1][2], max(w["x1"] for w in ln["words"])), ln["bottom"]]
                elif not notes:
                    raise UnreliableTable("Text below the table may contain unmarked footnotes")
                else:
                    break
                if note_chars and max(c["size"] for c in note_chars) > min(c["size"] for c in note_chars) * 1.25:
                    raise UnreliableTable("Footnotes contain mixed-size mathematical layout")
                last = ln["bottom"]
            block = {"type": "table", "page": number, "num": num, "table_schema": 1,
                     "caption_en": cap, "caption_pos": "above", "head_en": head, "rows_en": rows,
                     "footnotes_en": notes, "table_status": "ready",
                     "table_source_box": [round((table.bbox[i] - page.bbox[i % 2]) /
                                          (page.width if i % 2 == 0 else page.height), 6) for i in range(4)],
                     "table_evidence": {"extractor": VERSION, "method": method, "glyphs_verified": True}}
            def location(box):
                return {"page": number, "box": [round((box[i] - page.bbox[i % 2]) /
                                                     (page.width if i % 2 == 0 else page.height), 6) for i in range(4)]}
            block["table_cell_boxes"] = {}
            for i, row in enumerate(physical_rows):
                section, index = ("head", i) if i < len(head) else ("rows", i - len(head))
                for col, box in enumerate(row.cells):
                    block["table_cell_boxes"][f"{section}:{index}:{col}"] = location(box)
            for i, box in enumerate(note_boxes):
                block["table_cell_boxes"][f"note:{i}"] = location(box)
            block["table_geometry_source"] = tables.source_token(block)
            block['table_structure'] = {'version': 1, 'columns': len(rows[0]), 'header_rows': len(head),
                                        'body_rows': len(rows), 'cell_ids': [key for key in block['table_cell_boxes'] if not key.startswith('note:')]}
            if tables.shape_problem(block):
                raise UnreliableTable("Invalid rectangular structure")
            found.append(block)
        except (UnreliableTable, ValueError) as exc:
            issues.append(f"Table {num}: {exc}")
            unresolved.append({"type": "table", "page": number, "num": num, "caption_en": caption["text"],
                               "caption_pos": "above", "table_status": "fallback", "table_issue": str(exc)})
    return PageTables(found, issues, unresolved)


def _combine_tables(legacy: PageTables, words: PageTables) -> PageTables:
    """Prefer a fully validated legacy result, but never choose between conflicts.

    Both inputs have already passed the complete extraction/footnote checks.
    A different geometric box alone is not a matrix conflict; a different cell,
    row, header, caption or note is. Never stitch cells from different parsers.
    """
    def identity(source):
        return source['page'], source['num'].casefold()
    keys = list(dict.fromkeys(identity(s) for s in legacy.sources + legacy.unresolved + words.sources + words.unresolved))
    sources, issues, unresolved = [], [], []
    for key in keys:
        old = [s for s in legacy.sources if identity(s) == key]
        new = [s for s in words.sources if identity(s) == key]
        candidates = old + new
        base = next(s for s in legacy.sources + legacy.unresolved + words.sources + words.unresolved if identity(s) == key)
        conflict = len(old) > 1 or len(new) > 1
        if len(old) == len(new) == 1:
            conflict = any(old[0].get(k) != new[0].get(k) for k in ('head_en', 'rows_en', 'footnotes_en', 'caption_en'))
        if conflict:
            reason = 'Validated table reconstructions conflict; confirm the source manually'
        elif candidates:
            sources.append(old[0] if old else new[0])
            continue
        else:
            reasons = [s.get('table_issue', '') for s in legacy.unresolved + words.unresolved if identity(s) == key]
            reason = '; '.join(dict.fromkeys(r for r in reasons if r)) or 'No reliable table reconstruction'
        issues.append(f"Table {base['num']}: {reason}")
        unresolved.append({'type': 'table', 'page': base['page'], 'num': base['num'],
                           'caption_en': base['caption_en'], 'caption_pos': 'above',
                           'table_status': 'fallback', 'table_issue': reason})
    if not keys:
        issues = list(dict.fromkeys(legacy.issues + words.issues))
    return PageTables(sources, issues, unresolved)


def _continuation_problem(page, source):
    """Require independent header AND column geometry for unlabelled continuity.

    A table elsewhere on the next page is not by itself continuation evidence.
    Closed ruling is not proof of completion: publishers repeat closed grids.
    """
    if page.page_number >= len(page.pdf.pages):
        return None
    following = _visible_page(page.pdf.pages[page.page_number])
    lines = _lines(following)
    explicit = re.compile(r'Table\s+'+re.escape(source['num'])+r'\b[^\n]{0,100}\bcontinued\b',re.I)
    if any(explicit.search(line['text']) for line in lines):
        return 'Table explicitly continues on following page'
    normalize = lambda rows: [[re.sub(r'\s+', '', c) for c in row] for row in rows]
    boxes = source.get('table_cell_boxes', {})
    original = [boxes.get(f'head:0:{i}',{}).get('box') for i in range(len(source['head_en'][0]))]
    if not all(original):
        return None
    first_top = min((ln['top'] for ln in lines),default=following.height)
    height = median([w['bottom']-w['top'] for ln in lines for w in ln['words']] or [10])
    for table in following.find_tables():
        if table.bbox[1] > first_top+height*6:
            continue  # Not a leading continuation region.
        if any(_CAPTION.match(ln['text']) and ln['bottom'] <= table.bbox[1] for ln in lines):
            continue  # Independently labelled next table.
        try:
            head, _, physical = _matrix(following, table, 'lines')
        except ValueError:
            continue
        if normalize(head) != normalize(source['head_en']):
            continue
        cells = physical[0].cells
        aligned = len(cells)==len(original) and all(
            abs((cell[edge]-following.bbox[0])/following.width-box[edge]) <= max(2,height*.3)/following.width
            for cell,box in zip(cells,original) for edge in (0,2))
        near_boundary = source['table_source_box'][3] >= .9
        if aligned and (near_boundary or not source.get('footnotes_en')):
            return 'Matching unlabelled header and columns on following page; completeness is uncertain'
    return None


def _safe_sources(page, result):
    accepted = []
    for source in result.sources:
        problem = None
        if source.get('table_evidence',{}).get('method') in ('text','word-gutters'):
            # Alignment plus digits also describes numeric prose. Require an
            # independently typed value column, not digits embedded in sentences.
            rows = source['rows_en']
            def data_cell(value):
                return bool(re.search(r'\d',value)) and not re.search(r'[A-Za-z]{2,}', tables._PROTECTED.sub('',value))
            columns = [[row[i] for row in rows if row[i].strip()] for i in range(1,len(rows[0]))]
            if not any(len(col)>=2 and all(data_cell(v) for v in col) for col in columns):
                problem = 'Text alignment has no independently typed value column; possible numeric prose'
        problem = problem or _continuation_problem(page,source)
        if problem:
            result.issues.append(f"Table {source['num']}: {problem}")
            result.unresolved.append({'type':'table','page':source['page'],'num':source['num'],
                'caption_en':source['caption_en'],'table_status':'fallback','table_issue':problem})
        else:
            accepted.append(source)
    result.sources = accepted
    return result


def extract_page(page, number: int) -> PageTables:
    """Run existing validation first; independently cross-check or fill gaps."""
    legacy = _extract_page(page, number, borderless='text')
    words = _extract_page(page, number, borderless='word-gutters')
    result = _combine_tables(legacy, words)
    if not table_policy.enabled():
        return result
    if not page.find_tables():
        return _safe_sources(page,result)
    from . import table_regions, pdfwork
    try:
        regional = table_regions.extract(_visible_page(page), number)
    except ValueError as exc:
        regional = PageTables([], [str(exc)])
    if regional.sources:
        stream = page.pdf.stream
        offset = stream.tell()
        try:
            stream.seek(0)
            # Public callers may label an isolated page with its original PDF
            # page number; native indexing must use the actual input document.
            characters = pdfwork.table_characters(stream.read(), page.page_number)
        finally:
            stream.seek(offset)
        verified = []
        for source in regional.sources:
            try:
                table_regions.crosscheck(page, source, characters)
                source['table_evidence']['pdfium_cells_verified'] = True
                verified.append(source)
            except ValueError as exc:
                regional.unresolved.append({**source, 'table_issue':str(exc)})
                regional.issues.append(f"Table {source['num']}: {exc}")
        regional.sources = verified
    # A title boundary difference cannot replace a valid legacy result. Cell
    # or note disagreements still go through the existing conflict rejection.
    for new in regional.sources:
        for old in result.sources:
            if old['num'].casefold()==new['num'].casefold() and all(old.get(k)==new.get(k) for k in ('head_en','rows_en','footnotes_en')):
                new['caption_en']=old['caption_en']
    return _safe_sources(page,_combine_tables(result, regional))


def recognize(ws, block: dict) -> dict:
    """Match one existing table by its original caption number, without using
    legacy matrices as English evidence. Raises on zero or multiple matches.
    """
    if not table_policy.enabled():
        raise UnreliableTable(table_policy.DISABLED)
    import pdfplumber
    page = block.get("page")
    if not isinstance(block.get("caption_en", ""), str):
        raise UnreliableTable("Invalid source caption")
    num = str(block.get("num") or caption_number(block))
    if block.get("num") and caption_number(block) and str(block["num"]).casefold() != caption_number(block).casefold():
        raise UnreliableTable("Stored table number conflicts with its source caption")
    if type(page) is not int or page < 1 or block.get("table_spans") or block.get("table_segments"):
        raise UnreliableTable("Merged, cross-page or missing source page")
    with pdfplumber.open(ws.source_pdf) as pdf:
        if page > len(pdf.pages):
            raise UnreliableTable("Source page does not exist")
        result = extract_page(pdf.pages[page - 1], page)
        if page < len(pdf.pages) and re.search(r"Table\s+" + re.escape(num) + r"\b[^\n]{0,100}\bcontinued\b",
                                              pdf.pages[page].extract_text() or "", re.I):
            raise UnreliableTable("Table continues on the following page")
    candidates = [b for b in result.sources if b["num"].casefold() == num.casefold()]
    if len(candidates) != 1:
        raise UnreliableTable("; ".join(result.issues) or "Caption does not uniquely identify a source table")
    return candidates[0]
