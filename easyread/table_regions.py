"""Local, vector-bounded complement to the existing table recognizers.

No OCR or invented cell boundaries. Unruled candidates retain the original
recognizers; this complement requires independently detected vector cells.
"""
from collections import Counter
from statistics import median
import re

from . import tables

LABEL = re.compile(r'^(?:Table\s+|Tab\.\s*|表\s*)([A-Za-z]?\d+(?:[.\-]\d+)*)(?=$|\s|[:.：\u2013\u2014])', re.I)


def crosscheck(page, source, characters):
    """Check every cell independently against a second native PDF text engine.

    A whole-table character bag would miss numbers exchanged between cells.
    """
    def bag(text):
        text = text.translate(str.maketrans({'ﬀ':'ff','ﬁ':'fi','ﬂ':'fl','ﬃ':'ffi','ﬄ':'ffl'}))
        return Counter(c for c in text if not c.isspace())
    for key, location in source['table_cell_boxes'].items():
        box = location['box']
        actual = ''.join(c for c, b in characters if box[0] <= (b[0]+b[2])/2 < box[2] and box[1] <= (b[1]+b[3])/2 < box[3])
        section, *indices = key.split(':')
        expected = source['footnotes_en'][int(indices[0])] if section == 'note' else source[section+'_en'][int(indices[0])][int(indices[1])]
        if bag(actual) != bag(expected):
            raise ValueError('PDFium per-cell text/number coverage disagrees: '+key)
        # Multisets alone cannot detect a changed 12 -> 21 inside one cell.
        tokens = lambda text: re.findall(r'\d+(?:[.,]\d+)*', re.sub(r'\s+', '', text))
        if tokens(actual) != tokens(expected):
            raise ValueError('PDFium per-cell numeric order disagrees: '+key)


def extract(page, number):
    from . import table_extract as old
    found, issues, unresolved = [], [], []
    ruled = page.find_tables()
    for table in ruled:
        x0, top, x1, bottom = table.bbox
        # Geometry establishes the horizontal region before text lines are made.
        local = old._crop(page, (x0, page.bbox[1], x1, page.bbox[3]))
        lines = old._lines(local)
        height = median([w['bottom']-w['top'] for ln in lines for w in ln['words']] or [10])
        labels = []
        for ln in lines:
            match = LABEL.match(ln['text'])
            if not match:
                continue
            above = ln['bottom'] <= top
            distance = top-ln['bottom'] if above else ln['top']-bottom
            if distance < 0 or distance > height*8:
                continue
            between = [l for l in lines if (ln['bottom'] < l['top'] < top if above else bottom < l['top'] < ln['top'])]
            if not ln['text'][match.end():].strip():
                following = [l for l in lines if l['top']>ln['bottom']]
                if not following:
                    continue
                title = following[0]
                left = ln['words'][0]['x0']
                tl = title['words'][0]['x0']
                if (title['top']-ln['bottom']>min(12,height) or abs(left-tl)>6 or
                        len(title['words'])<2 or not re.match(r'[A-Z]',title['text']) or
                        title['text'].endswith(('.',':',';','?','!')) or LABEL.match(title['text']) or
                        re.match(r'(?:shows?|provides?|presents?|contains?|reports?|summarizes?|illustrates?|lists?|compares?|is|are|was|were|and|for|in|of|the)\b',title['text'],re.I)):
                    continue
            # Wrapped titles can occupy short adjacent lines; ordinary prose
            # and intervening tables are not silently absorbed into a caption.
            if len(between)>2 or any(len(l['words'])>14 or l['text'].endswith(('.', ';')) for l in between):
                continue
            if above and any(t is not table and top > t.bbox[3] > ln['bottom'] for t in ruled):
                continue
            labels.append((ln,match,above,between))
        if len(labels)!=1:
            continue
        cap, match, above, between = labels[0]
        num = match.group(1)
        try:
            if sum(bool(m and m.group(1).casefold()==num.casefold()) for m in (LABEL.match(l['text']) for l in lines)) != 1:
                raise ValueError('Repeated caption identity in local column; manual review required')
            if old._CONTINUED.search(cap['text']) or bottom >= page.bbox[3]-30:
                raise ValueError('Continued or page-edge table requires review')
            head, rows, physical = old._matrix(local,table,'lines')
            # Explicit per-cell words must not be split by an inferred adapter.
            cells=[c for row in physical for c in row.cells]
            for word in local.extract_words(x_tolerance=2,y_tolerance=2):
                chars=[c for c in local.chars if old._inside(c,table.bbox) and old._inside(c,(word['x0'],word['top'],word['x1'],word['bottom']))]
                if len({i for c in chars for i,cell in enumerate(cells) if old._inside(c,cell)})>1:
                    raise ValueError('Local grid splits a source word')
            title=[cap['text']]
            if above:
                title += [ln['text'] for ln in between]
            notes, note_boxes = [], []
            last=bottom
            for ln in [l for l in lines if l['top']>=bottom-1]:
                if not above and ln['top']>=cap['top']:
                    break
                gap=ln['top']-last
                if gap>height*2 or LABEL.match(ln['text']):
                    break
                if old._NOTE.match(ln['text']):
                    notes.append(ln['text'])
                    note_boxes.append((x0,ln['top'],x1,ln['bottom']))
                elif notes:
                    # Do not guess where an unlabelled continuation ends.
                    raise ValueError('Local footnote continuation requires review')
                elif gap>=height and len(ln['words'])>=12 and not re.match(r'^[\d*†‡]',ln['text']):
                    break  # Closed vector grid followed by separated narrative.
                else:
                    raise ValueError('Unmarked text near local table boundary')
                last=ln['bottom']
            def location(box):
                return {'page':number,'box':[round((box[i]-page.bbox[i%2])/(page.width if i%2==0 else page.height),6) for i in range(4)]}
            boxes={}
            for i,row in enumerate(physical):
                section,index=('head',i) if i<len(head) else ('rows',i-len(head))
                for j,box in enumerate(row.cells):boxes[f'{section}:{index}:{j}']=location(box)
            for i,box in enumerate(note_boxes):boxes[f'note:{i}']=location(box)
            source={'type':'table','page':number,'num':num,'table_schema':1,'table_status':'ready',
                    'caption_en':' '.join(title),'caption_pos':'above' if above else 'below',
                    'head_en':head,'rows_en':rows,'footnotes_en':notes,
                    'table_source_box':location(table.bbox)['box'],'table_cell_boxes':boxes,
                    'table_evidence':{'extractor':old.VERSION,'method':'lines','region_local':True,'glyphs_verified':True},
                    'table_structure':{'version':1,'columns':len(rows[0]),'header_rows':len(head),'body_rows':len(rows),
                                       'cell_ids':[k for k in boxes if not k.startswith('note:')]}}
            source['table_geometry_source']=tables.source_token(source)
            if tables.shape_problem(source):raise ValueError('Invalid local matrix')
            found.append(source)
        except ValueError as exc:
            issues.append(f'Table {num}: {exc}')
            unresolved.append({'type':'table','page':number,'num':num,'caption_en':cap['text'],'table_issue':str(exc)})
    return old.PageTables(found,issues,unresolved)
