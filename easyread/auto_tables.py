"""Persist automatic links at verified anchors; never rewrite paper/reader."""
import copy
import re

from . import engines, manual_table_links, table_extract, table_policy, table_translate, tables, table_engines
from .store import dir_lock, read_json, write_json_atomic
from .background import writing

FILE = 'auto_table_drafts.json'


def state(ws):
    value = read_json(ws.root / FILE, {'version': 1, 'revision': 0, 'drafts': {}})
    if (not isinstance(value, dict) or value.get('version') != 1 or
            not isinstance(value.get('drafts'), dict) or type(value.get('revision')) is not int):
        raise ValueError('Invalid automatic table drafts; saved data retained')
    value.setdefault('reviews', {})
    if not isinstance(value['reviews'], dict):
        raise ValueError('Invalid table review records; saved data retained')
    for draft in value['drafts'].values():
        if (draft.get('verification',{}).get('status')!='verified' and
            draft.get('translation_hash')==table_translate._hash(draft.get('table')) and
            not tables.translation_problem(draft['table'])):
            draft['table']=table_translate.normalize_machine(draft['table'])
            draft['translation_hash']=table_translate._hash(draft['table'])
    return value


def _review(ws, source, digest, reason, cancel):
    """Do not lose recognizer rejections or pretend a translation exists."""
    key = table_translate._hash({'pdf': digest, 'page': source['page'], 'num': source['num']})
    if not source['num']:
        key = table_translate._hash({'pdf':digest,'page':source['page'],'region':source.get('table_source_box')})
    with dir_lock(ws.root), writing(ws, 'auto-tables'):
        table_translate._cancel(cancel)
        if table_translate.pdf_hash(ws) != digest:
            raise ValueError('PDF changed; review discarded')
        value = state(ws)
        value['reviews'][key] = {'pdf': digest, 'page': source['page'], 'num': source['num'],
                                 'stage': 'recognition', 'reason': reason,
                                 'translation_available': False,
                                 **{k:source[k] for k in ('caption_en','table_source_box','source_audit','table_native','partial_preview','table_checks') if k in source}}
        review=value['reviews'][key]
        if review.get('table_checks'):
            matches=targets(ws.load('paper'),source)
            review['table_checks']['location']={'status':'unique' if len(matches)==1 else 'ambiguous' if matches else 'unresolved'}
        value['revision'] += 1
        write_json_atomic(ws.root / FILE, value)


def targets(paper, source):
    """An existing table on the same PDF page and with the same number is an anchor."""
    if not source.get('num'):
        return []  # No invented identifier or page-only placement.
    return [b for b in paper.get('blocks', []) if b.get('type') == 'table' and
            b.get('page') == source['page'] and
            str(b.get('num') or table_extract.caption_number(b)).casefold() == source['num'].casefold() and
            (not table_extract.caption_number(b) or table_extract.caption_number(b).casefold() == source['num'].casefold())]


def protected(ws, block):
    reader = ws.load('reader') or {}
    bid = block['id']
    return (bool(block.get('rows_zh') or block.get('table_reading_zh')) or
            bid in manual_table_links.state(ws)['links'] or
            any(k == bid or k.startswith(bid + '#') for k in reader.get('edits', {})))


def process(ws, cfg, pages=None, cancel=None, report=lambda *a: None, meter=None, *, run=None, table_id=None):
    import pdfplumber
    result = {'translated': 0, 'cached': 0, 'failed': {}, 'issues': [], 'pending': 0}
    with table_translate._job_lock(ws, cancel):
        digest = table_translate.pdf_hash(ws)
        selected = None
        if table_id is not None:
            selected = [b for b in ws.load('paper').get('blocks', []) if b.get('id') == table_id and b.get('type') == 'table']
            if len(selected) != 1:
                raise ValueError('Select exactly one table block')
            selected = selected[0]
            if not (selected.get('num') or table_extract.caption_number(selected)):
                raise ValueError('Table number is unavailable; use manual selection')
            pages = [selected['page']]
        def requested(source):
            return selected is None or any(b.get('id') == table_id for b in targets({'blocks': [selected]}, source))
        recognized = table_engines.recognize_document(ws,cfg,cancel)
        try:
            primary = table_engines.prepare_pages(ws,recognized)
        except (ValueError,OSError,KeyError,TypeError) as exc:
            primary={};recognized={'status':'unavailable','reason':'Invalid recognition response: '+str(exc)}
        result['recognition'] = {k:v for k,v in recognized.items() if k not in ('tables','pages')}
        if table_translate.pdf_hash(ws)!=digest:
            raise ValueError('Source changed during recognition')
        with pdfplumber.open(ws.source_pdf) as pdf, table_policy.explicit_job():
            wanted = list(range(1, len(pdf.pages) + 1)) if pages is None else sorted(set(pages))
            for index, number in enumerate(wanted):
                table_translate._cancel(cancel)
                if type(number) is not int or not 1 <= number <= len(pdf.pages):
                    raise ValueError('Table page outside PDF')
                report(index, len(wanted), '正在辨識／補齊表格')
                page = pdf.pages[number - 1]
                auxiliary = table_extract.extract_page(page, number)
                found = table_engines.arbitrate(primary.get(number),auxiliary)
                scoped_unresolved = [r for r in found.unresolved if requested(r)]
                page_issues = found.issues if selected is None else [f"Table {r['num']}: {r.get('table_issue', '')}" for r in scoped_unresolved]
                result['issues'].extend(f'PDF p.{number}: {issue}' for issue in page_issues)
                for unresolved in found.unresolved:
                    if requested(unresolved):
                        _review(ws, unresolved, digest, unresolved.get('table_issue') or 'Unreliable table structure', cancel)
                        from . import table_editor
                        existing=ws.load('paper')
                        owners=targets(existing,unresolved) if unresolved.get('num') else [b for b in existing.get('blocks',[]) if b.get('page')==number and b.get('type')=='table']
                        if not any(protected(ws,b) for b in owners):
                            table_editor.capture(ws,unresolved,digest,cfg,cancel,meter,run)
                missing = [b for b in ws.load('paper').get('blocks', []) if b.get('type') == 'table' and b.get('page') == number and
                           (selected is None or b['id'] == table_id) and not any(b in targets({'blocks':[b]}, s) for s in found.sources + found.unresolved)]
                if missing:
                    result['issues'].append(f'PDF p.{number}: 正文有表格佔位區，但 PDF 結構無法可靠辨識，請使用手動框選')
                    for block in missing:
                        _review(ws, {**block, 'num':str(block.get('num') or table_extract.caption_number(block))}, digest,
                                '找不到可靠的表格來源，請使用「手動框選」。', cancel)
                for source in found.sources:
                    if not requested(source):
                        continue
                    from . import table_editor
                    if table_editor.protect_authored(ws,source,digest):continue
                    # Keep uncertain borderless/cross-page structures in the manual workflow.
                    following = pdf.pages[number].extract_text() or '' if number < len(pdf.pages) else ''
                    if (source.get('table_evidence', {}).get('method') not in ('lines', 'text', 'word-gutters','docling') or
                            sum(s['num'].casefold() == source['num'].casefold() for s in found.sources) != 1 or
                            re.search(r'Table\s+' + re.escape(source['num']) + r'\b[^\n]{0,100}\bcontinued\b', following, re.I)):
                        result['issues'].append(f"PDF p.{number} Table {source['num']}: 請使用手動框選確認跨頁或無框線表格")
                        _review(ws, source, digest, 'Cross-page, borderless or ambiguous structure requires selection', cancel)
                        continue
                    # Additive structure descriptors do not invalidate an existing
                    # cache whose source cells/evidence have not changed.
                    cache_source = {k: v for k, v in source.items() if k != 'table_structure'}
                    key = table_translate._hash({'pdf': digest, 'source': cache_source, 'target': 'zh-TW'})
                    try:
                        paper = ws.load('paper')
                        matches = targets(paper, source)
                        if source.get('table_checks'):
                            source['table_checks']['location']={'status':'unique' if len(matches)==1 else 'ambiguous' if matches else 'unresolved'}
                        saved = state(ws)
                        old = saved['drafts'].get(key)
                        if old is None:
                            # A parser/geometry upgrade may change a cache key
                            # without changing any source cell. Reuse only exact
                            # cell/caption/note matches on the same PDF and page.
                            matches_cache = [(k, d) for k, d in saved['drafts'].items()
                                if d.get('pdf') == digest and d.get('table', {}).get('page') == source['page'] and
                                d['table'].get('num') == source['num'] and
                                all(d['table'].get(f) == source.get(f) for f in table_translate._SOURCE) and
                                not tables.translation_problem(d['table']) and
                                d.get('translation_hash') == table_translate._hash(d['table'])]
                            if len(matches_cache) > 1:
                                raise ValueError('多份既有快取符合來源，請開啟「確認表格草稿」選擇；未呼叫模型。')
                            if matches_cache:
                                key, old = matches_cache[0]
                        reused = bool(old and not tables.translation_problem(old['table']) and old.get('translation_hash') == table_translate._hash(old['table']))
                        if reused:
                            result['cached'] += 1
                            translated = copy.deepcopy(old['table'])
                        else:
                            # Reuse existing validated bilingual cells as well as sidecar drafts.
                            previous = matches[0] if len(matches) == 1 else {}
                            translated = table_translate._translate(ws, cfg, source, previous, cancel, meter, run)
                        for field in ('source_audit','table_evidence','table_native','table_cell_boxes','table_source_box','table_geometry_source','table_structure','table_checks'):
                            if field in source:translated[field]=copy.deepcopy(source[field])
                        table_translate._cancel(cancel)
                        if table_translate.pdf_hash(ws) != digest or ws.load('paper') != paper:
                            raise ValueError('Source changed; draft discarded')
                        anchor = matches[0] if len(matches) == 1 and sum(b.get('id') == matches[0]['id'] for b in paper.get('blocks', [])) == 1 else None
                        automatic = anchor is not None and not protected(ws, anchor)
                        source_fingerprint=table_translate._hash({'pdf':digest,'source':source})
                        draft = {'pdf': digest, 'table': translated, 'translation_hash': table_translate._hash(translated),
                                 'anchor': anchor['id'] if automatic else None,
                                 'anchor_hash': table_translate._hash(anchor) if automatic else None,
                                 'placement': 'replace', 'confirmed': False,'source_fingerprint':source_fingerprint,
                                 'verification':{'status':'pending','source_fingerprint':source_fingerprint}}
                        if (old and old.get('source_fingerprint')==source_fingerprint and
                                old.get('translation_hash')==draft['translation_hash']):
                            draft['verification']=copy.deepcopy(old.get('verification',draft['verification']))
                        if old and old.get('confirmed') and any(b.get('id') == old.get('anchor') and table_translate._hash(b) == old.get('anchor_hash') for b in paper.get('blocks', [])):
                            draft.update({k: old[k] for k in ('anchor', 'anchor_hash', 'placement', 'confirmed')})
                        with dir_lock(ws.root), writing(ws, 'auto-tables'):
                            table_translate._cancel(cancel)
                            if table_translate.pdf_hash(ws) != digest or ws.load('paper') != paper:
                                raise ValueError('Source changed; draft discarded')
                            value = state(ws)
                            if value['revision'] != saved['revision']:
                                raise ValueError('Table placement changed; draft retained without overwrite')
                            # Recheck authored data under the write lock: it may
                            # have changed while the model was returning.
                            if draft.get('anchor') and not draft.get('confirmed'):
                                if protected(ws, anchor):
                                    draft.update(anchor=None, anchor_hash=None)
                                elif any(k != key and d.get('pdf') == digest and
                                         d.get('anchor') == draft['anchor'] and
                                         not tables.translation_problem(d['table']) and
                                         d.get('translation_hash') == table_translate._hash(d['table'])
                                         for k, d in value['drafts'].items()):
                                    raise ValueError('Conflicting table links; manual confirmation required')
                            if value['drafts'].get(key) == draft:
                                continue  # Already persisted; do not insert or rewrite again.
                            value['drafts'][key] = draft
                            review_key = table_translate._hash({'pdf': digest, 'page': source['page'], 'num': source['num']})
                            value['reviews'].pop(review_key, None)
                            value['revision'] += 1
                            write_json_atomic(ws.root / FILE, value)
                        if not reused:
                            result['translated'] += 1
                    except engines.Cancelled:
                        raise
                    except (ValueError, OSError, engines.EngineError) as exc:
                        result['failed'][key] = str(exc)[:300]
            value = state(ws)
            result['pending'] = (sum(not d.get('anchor') for d in value['drafts'].values() if d.get('pdf') == digest and requested(d['table'])) +
                                 sum(r.get('pdf') == digest and requested(r) for r in value['reviews'].values()))
            result['awaiting_review']=sum(d.get('pdf')==digest and d.get('verification',{}).get('status')=='pending' for d in value['drafts'].values())
    return result


def project(ws, paper):
    """Display copies only. Revoked anchors never hide or overwrite original data."""
    if not (ws.root / FILE).exists():
        return
    try:
        value = state(ws)
        drafts = value['drafts']
        digest = table_translate.pdf_hash(ws)
        raw = ws.load('paper')
        for review in value['reviews'].values():
            if review.get('pdf') != digest:
                continue
            for block in targets(paper, review):
                if not protected(ws,block):
                    block['manual_table_issue'] = ('表格結構辨識未完成，尚無譯表。請開啟「確認表格草稿」框選此表格。'
                                                   + ' (' + str(review.get('reason', ''))[:200] + ')')
                    if review.get('table_source_box'):
                        from . import pdfwork
                        block['auto_table_risk_preview']=pdfwork.reading_preview(ws.source_pdf.read_bytes(),review['page'],review['table_source_box'])
                        block['auto_table_risks']=review.get('source_audit',{}).get('risks',[])
        for key, d in drafts.items():
            if d.get('pdf') != digest or not d.get('anchor') or tables.translation_problem(d['table']) or d.get('translation_hash') != table_translate._hash(d['table']):
                continue
            if d['table'].get('source_audit',{}).get('blocking'):
                continue
            if any(k != key and other.get('pdf') == digest and other.get('anchor') == d['anchor'] and
                   not tables.translation_problem(other['table']) and
                   other.get('translation_hash') == table_translate._hash(other['table'])
                   for k, other in drafts.items()):
                continue  # Never let dictionary order choose a conflicting table.
            originals = [b for b in raw.get('blocks', []) if b.get('id') == d['anchor']]
            if len(originals) != 1 or table_translate._hash(originals[0]) != d['anchor_hash']:
                continue
            if protected(ws, originals[0]):
                continue
            visible = [b for b in paper['blocks'] if b.get('id') == d['anchor']]
            if len(visible) != 1:
                continue
            block = visible[0]
            if block.get('manual_table_view'):
                continue
            table = copy.deepcopy(d['table'])
            table['id'] = block['id'] if d['placement'] == 'replace' else 'auto-table-' + key[:16]
            table['auto_table_draft'] = key
            verification=d.get('verification',{})
            table['auto_table_verification']='verified' if (verification.get('status')=='verified' and
                verification.get('source_fingerprint')==d.get('source_fingerprint') and
                verification.get('translation_hash')==d.get('translation_hash')) else 'pending'
            if d['placement'] == 'replace':
                # Existing translated captions remain authoritative.
                if originals[0].get('caption_zh'):
                    table['caption_zh'] = originals[0]['caption_zh']
                # Old optional geometry/evidence must not invalidate a new verified matrix.
                # This touches the display copy only, never the stored block.
                for field in ('table_spans', 'table_segments', 'table_reading', 'table_reading_zh',
                              'table_reading_cache', 'table_reading_preview', 'manual_table_issue'):
                    block.pop(field, None)
                block.update(table)
            else:
                paper['blocks'].insert(paper['blocks'].index(block) + 1, table)
        from . import table_editor
        for record in table_editor.state(ws)['records'].values():
            if record.get('source_changed'):continue
            if record['pdf']!=digest or not record['source'].get('translation_allowed') and not record.get('human'):continue
            if not (record.get('machine') or record.get('human')):continue
            matches=targets(raw,record)
            if len(matches)!=1 or protected(ws,matches[0]):continue
            if record.get('anchor')!=matches[0]['id'] or record.get('anchor_hash')!=table_translate._hash(matches[0]):continue
            visible=[b for b in paper['blocks'] if b.get('id')==matches[0]['id']]
            if len(visible)==1:visible[0]['table_editor_view']=table_editor.public(record)
        if table_translate.pdf_hash(ws) != digest:
            paper['blocks'] = copy.deepcopy(raw.get('blocks', []))
    except (ValueError, OSError, KeyError, TypeError):
        return


def _workflow_items(ws, raw, value, digest, job):
    """Read-only status projection over existing blocks, drafts, links and jobs."""
    blocks = raw.get('blocks', [])
    links = manual_table_links.state(ws)['links']
    items, represented = [], set()
    for block in (b for b in blocks if b.get('type') == 'table'):
        bid = block['id']
        item = {'id': bid, 'block_id': bid, 'page': block.get('page'), 'num': block.get('num') or table_extract.caption_number(block),
                'caption': block.get('caption_en', ''), 'state': 'unprocessed', 'reason': '', 'cache_valid': False,
                'protected': bool(protected(ws, block)), 'job_state': None}
        if block.get('rows_en') and not tables.shape_problem(block):
            item['state'] = 'completed' if block.get('rows_zh') and not tables.translation_problem(block) else 'recognized'
        if item['protected'] and item['state'] != 'completed':
            item['state'] = 'manual_protected'
        for key, draft in value['drafts'].items():
            if not any(b.get('id') == bid for b in targets(raw, draft['table'])) and draft.get('anchor') != bid:
                continue
            represented.add(key)
            valid = (draft.get('pdf') == digest and not tables.translation_problem(draft['table']) and
                     draft.get('translation_hash') == table_translate._hash(draft['table']) and not draft['table'].get('source_audit',{}).get('blocking'))
            anchored = draft.get('anchor') == bid and draft.get('anchor_hash') == table_translate._hash(block)
            conflict = anchored and any(k != key and d.get('pdf') == digest and d.get('anchor') == bid and
                                        not tables.translation_problem(d['table']) and
                                        d.get('translation_hash') == table_translate._hash(d['table'])
                                        for k, d in value['drafts'].items())
            candidates = targets(raw, draft['table'])
            item.update(draft_id=key, cache_valid=valid, candidate_block_ids=[b['id'] for b in candidates])
            item['source_fingerprint']=draft.get('source_fingerprint')
            item['risks']=draft['table'].get('source_audit',{}).get('risks',[])
            if draft['table'].get('table_source_box'):item['regions']=[{'page':block.get('page'),'box':draft['table']['table_source_box']}]
            if conflict:
                item.update(state='ambiguous', reason='Conflicting table links; manual confirmation required')
            elif not valid:
                item.update(state='failed', reason='表格草稿來源或內容已變更，舊資料保留。')
            elif item['protected']:
                item.update(state='manual_protected', reason='已有人工關聯或修改；不會自動覆寫。')
            else:
                item['state'] = 'completed' if anchored else 'ambiguous' if len(candidates) > 1 else 'translated_unplaced'
                if anchored and draft.get('verification'):
                    verification=draft['verification']
                    item['state']='verified' if (verification.get('status')=='verified' and
                        verification.get('source_fingerprint')==draft.get('source_fingerprint') and
                        verification.get('translation_hash')==draft.get('translation_hash')) else 'pending_check'
        for review in value['reviews'].values():
            if review.get('pdf') == digest and any(b.get('id') == bid for b in targets(raw, review)) and not item['cache_valid'] and not item.get('protected') and bid not in links:
                item.update(state='recognition_failed', reason=review.get('reason', ''))
                if review.get('table_source_box'):item['regions']=[{'page':review['page'],'box':review['table_source_box']}]
                item['risks']=review.get('source_audit',{}).get('risks',[])
                item['partial_preview']=review.get('partial_preview')
                item['table_checks']=review.get('table_checks')
        if bid in links:
            from . import simple_tables
            record = links[bid].get('record', {})
            if len(simple_tables.saved(ws, [record])) != 1 or links[bid].get('block_hash') != manual_table_links.identity(block):
                item.update(state='failed', reason='既有人工關聯需要重新確認；原譯文保留。')
            else:
                item.update(state='manual_protected', reason='已保存選區譯文；是否有完整欄列須另行確認。', regions=record['source']['regions'])
        relevant = (job.get('tables_only') or job.get('include_tables')) and (not job.get('pages') or block.get('page') in job['pages']) and (not job.get('table_id') or job['table_id'] == bid)
        if relevant:
            item['job_state'] = job.get('state')
            if item['state'] in ('unprocessed', 'recognized'):
                if job.get('state') in ('queued', 'running'):
                    item['state'] = 'processing'
                elif job.get('state') in ('cancelled', 'error'):
                    item.update(state='cancelled' if job['state'] == 'cancelled' else 'failed', reason=job.get('error') or job.get('message', ''))
        if not (item.get('draft_id') and any(i.get('draft_id') == item['draft_id'] for i in items)):
            items.append(item)
    for key, draft in value['drafts'].items():
        if key in represented:
            continue
        valid = draft.get('pdf') == digest and not tables.translation_problem(draft['table']) and draft.get('translation_hash') == table_translate._hash(draft['table']) and not draft['table'].get('source_audit',{}).get('blocking')
        anchors = [b for b in blocks if b.get('id') == draft.get('anchor')]
        anchored = len(anchors) == 1 and draft.get('anchor_hash') == table_translate._hash(anchors[0])
        items.append({'id': 'draft:'+key, 'draft_id': key, 'page': draft['table']['page'], 'num': draft['table']['num'],
                      'block_id': draft.get('anchor') if anchored else None,
                      'caption': draft['table'].get('caption_en', ''), 'state': ('completed' if anchored else 'translated_unplaced') if valid else 'failed', 'cache_valid': valid,
                      'reason': '' if valid else '來源或草稿已變更，請重新確認。',
                      'risks':draft['table'].get('source_audit',{}).get('risks',[]),
                      'regions':[{'page':draft['table']['page'],'box':draft['table']['table_source_box']}] if draft['table'].get('table_source_box') else []})
        if anchored and draft.get('verification') and valid:
            v=draft['verification'];items[-1].update(state='verified' if v.get('status')=='verified' and v.get('translation_hash')==draft.get('translation_hash') and v.get('source_fingerprint')==draft.get('source_fingerprint') else 'pending_check',source_fingerprint=draft.get('source_fingerprint'))
    for key, review in value['reviews'].items():
        if review.get('pdf') == digest and not targets(raw, review):
            items.append({'id':'review:'+key, **review, 'state':'recognition_failed',
                'risks':review.get('source_audit',{}).get('risks',[]),
                'regions':[{'page':review['page'],'box':review['table_source_box']}] if review.get('table_source_box') else []})
    return items


def handle(ws, body):
    if str(body.get('action','')).startswith('editor_'):
        from . import table_editor
        return table_editor.handle(ws,body)
    value = state(ws)
    if body.get('action') == 'list':
        raw = ws.load('paper')
        digest = table_translate.pdf_hash(ws)
        value['reviews'] = {k: r for k, r in value['reviews'].items() if r.get('pdf') == digest}
        # Existing custom.2 jobs saved rejection details only in job.json. Surface
        # these read-only as review items immediately, without another model job.
        job = ws.load('job') or {}
        issues = (job.get('table_result') or {}).get('issues', []) if job.get('_baseline', {}).get('pdf') == digest else []
        for issue in issues:
            match = re.match(r'PDF p\.(\d+): Table ([^:]+): (.+)', issue)
            if match:
                page, num, reason = int(match[1]), match[2], match[3]
                key = table_translate._hash({'pdf': digest, 'page': page, 'num': num})
                if not any(d.get('pdf') == digest and d.get('table', {}).get('page') == page and d.get('table', {}).get('num') == num for d in value['drafts'].values()):
                    value['reviews'].setdefault(key, {'pdf': digest, 'page': page, 'num': num,
                                                     'stage': 'recognition', 'reason': reason, 'translation_available': False})
        items=_workflow_items(ws, raw, value, digest, job)
        from . import table_editor
        records=table_editor.state(ws)['records']
        for item in items:
            box=(item.get('regions') or [{}])[0].get('box')
            key=table_editor.identity(digest,item.get('page'),box)
            if key in records and not item.get('protected'):
                item.update(editor_id=key,editor=table_editor.public(records[key]))
                if item['editor']['translation_available']:
                    item['state']='verified' if item['editor']['verified'] else 'pending_check'
        from . import table_review_identity
        items=table_review_identity.groups(ws,items,value,digest)
        return {**value, 'items': items,
                'targets': [{'id': b['id'], 'page': b.get('page'), 'caption': b.get('caption_en') or str(b.get('en', ''))[:100],
                             'hash': table_translate._hash(b), 'type': b.get('type'),
                             'manual_linked': b['id'] in manual_table_links.state(ws)['links']}
                            for b in raw.get('blocks', []) if b.get('id')]}
    if body.get('action') == 'verify':
        from .store import now_iso
        if body.get('confirmed') is not True:
            raise ValueError('Explicit human verification required')
        with dir_lock(ws.root),writing(ws,'auto-tables'):
            value=state(ws)
            if body.get('revision')!=value['revision']:raise ValueError('Draft changed; reload before verification')
            draft=value['drafts'][body['draft_id']]
            anchors=[b for b in ws.load('paper').get('blocks',[]) if b.get('id')==draft.get('anchor')]
            if (draft['pdf']!=table_translate.pdf_hash(ws) or not draft.get('source_fingerprint') or
                body.get('source_fingerprint')!=draft['source_fingerprint'] or
                draft['translation_hash']!=table_translate._hash(draft['table']) or
                draft['table'].get('source_audit',{}).get('blocking') or
                len(anchors)!=1 or table_translate._hash(anchors[0])!=draft['anchor_hash'] or protected(ws,anchors[0])):
                raise ValueError('Source, translation, placement or authored data changed; cannot verify')
            draft['verification']={'status':'verified','actor':'user','at':now_iso(),
                'source_fingerprint':draft['source_fingerprint'],'translation_hash':draft['translation_hash']}
            value['revision']+=1;write_json_atomic(ws.root/FILE,value)
        return {'ok':True}
    if body.get('action') != 'confirm' or body.get('confirmed') is not True:
        raise ValueError('Explicit table placement confirmation required')
    with dir_lock(ws.root):
        value = state(ws)
        if body.get('revision') != value['revision']:
            raise ValueError('Draft changed; reload before confirming')
        draft = value['drafts'][body['draft_id']]
        blocks = [b for b in ws.load('paper').get('blocks', []) if b.get('id') == body.get('block_id')]
        if (len(blocks) != 1 or table_translate._hash(blocks[0]) != body.get('block_hash') or
                draft['pdf'] != table_translate.pdf_hash(ws) or tables.translation_problem(draft['table'])):
            raise ValueError('Source or anchor changed; nothing written')
        if blocks[0].get('page') != draft['table']['page']:
            raise ValueError('Select an anchor on the source PDF page')
        if blocks[0]['id'] in manual_table_links.state(ws)['links']:
            raise ValueError('Existing manual link retained; use manual table translation to manage it')
        if protected(ws,blocks[0]) or draft['table'].get('source_audit',{}).get('blocking'):
            raise ValueError('Authored data or risky source retained; automatic placement prohibited')
        draft.update(anchor=blocks[0]['id'], anchor_hash=body['block_hash'], confirmed=True,
                     placement='replace' if blocks[0].get('type') == 'table' else 'after')
        value['revision'] += 1
        write_json_atomic(ws.root / FILE, value)
    return {'ok': True}
