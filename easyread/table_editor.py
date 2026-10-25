"""Sparse source, cached machine text and revisioned human views are separate.

This is not an extractor: missing slots remain None and no inferred cell text is
sent to translation. Structural human operations never mutate native evidence.
"""
import copy
from . import table_translate, tables, engines
from .store import read_json,write_json_atomic,dir_lock,now_iso
from .background import writing

FILE='table_editor.json'

def state(ws):
    value=read_json(ws.root/FILE,{'version':1,'records':{}})
    if not isinstance(value,dict) or value.get('version')!=1 or not isinstance(value.get('records'),dict):
        raise ValueError('Invalid table editor store; retained')
    for record in value['records'].values():
        if record.get('machine') and not record.get('human') and not record.get('verified'):
            record['machine']=table_translate.normalize_machine(record['machine'],sparse=True)
    return value

def identity(pdf,page,box):return table_translate._hash({'pdf':pdf,'page':page,'box':box})

def locate(ws,key):
    from . import auto_tables
    with dir_lock(ws.root),writing(ws,'table-editor'):
        value=state(ws);record=value['records'][key]
        if record['pdf']!=table_translate.pdf_hash(ws) or record.get('source_changed'):return
        matches=auto_tables.targets(ws.load('paper'),record)
        if len(matches)==1 and not auto_tables.protected(ws,matches[0]):
            record.update(anchor=matches[0]['id'],anchor_hash=table_translate._hash(matches[0]))
        else:record.update(anchor=None,anchor_hash=None)
        write_json_atomic(ws.root/FILE,value)

def view(record):return copy.deepcopy(record.get('human') or record.get('machine') or record['initial'])

def capture(ws,review,digest,cfg=None,cancel=None,meter=None,run=None):
    preview=review.get('partial_preview')
    if not preview:return None
    key=identity(digest,review['page'],review.get('table_source_box'))
    fingerprint=table_translate._hash({'pdf':digest,'preview':preview})
    old=state(ws)['records'].get(key)
    if old and old['source_hash']==fingerprint and (old.get('human') or old.get('verified') or cfg is None):
        locate(ws,key);return key
    initial={'rows':preview['rows'],'columns':preview['columns'],'caption':'','footnotes':[],
        'cells':[{'id':f'{s["row"]}:{s["column"]}','row':s['row'],'column':s['column'],'row_span':1,'col_span':1,
                  'text':None,'source_id':f'{s["row"]}:{s["column"]}','status':s['status']}
                 for row in preview['slots'] for s in row]}
    machine=None;cache_key=None
    if cfg is not None and preview.get('translation_allowed') and not (old and (old.get('human') or old.get('verified'))):
        pairs=[s for row in preview['slots'] for s in row if s['status']=='source_checked']
        # A one-column transport list, never a reconstructed PDF matrix. IDs map
        # each returned value back to its original sparse source slot.
        carrier={'table_schema':1,'head_en':[],'rows_en':[[s['text']] for s in pairs],
                 'caption_en':' '.join(x['text'] for x in preview.get('caption',[])),
                 'footnotes_en':[x['text'] for x in preview.get('footnotes',[])]}
        cache_key=table_translate._hash({'source':carrier,'cfg':cfg,'glossary':ws.load('paper').get('glossary')})
        if old and old.get('machine_key')==cache_key:machine=copy.deepcopy(old.get('machine'))
        elif pairs:
            translated=table_translate._translate(ws,cfg,carrier,{},cancel,meter,run)
            machine=copy.deepcopy(initial);lookup={c['source_id']:c for c in machine['cells']}
            for source,row in zip(pairs,translated['rows_zh']):lookup[f'{source["row"]}:{source["column"]}']['text']=row[0]
            machine.update(caption=translated['caption_zh'],footnotes=translated['footnotes_zh'])
    with dir_lock(ws.root),writing(ws,'table-editor'):
        table_translate._cancel(cancel)
        if table_translate.pdf_hash(ws)!=digest:raise ValueError('Source changed; sparse result discarded')
        value=state(ws);current=value['records'].get(key)
        if current and (current.get('human') or current.get('verified')):
            if current['source_hash']!=fingerprint:
                current['history'].append({'at':now_iso(),'action':'source_changed','previous_incoming':current.get('incoming_source')})
                current.update(verified=False,source_changed=True,incoming_source=copy.deepcopy(preview),incoming_hash=fingerprint,
                               incoming_recognition_hash=table_translate._hash(review.get('table_native')))
                write_json_atomic(ws.root/FILE,value)
            return key
        if current and current.get('source_hash')==fingerprint:
            if machine:
                if current.get('machine')!=machine:current['revision']+=1
                current.update(machine=machine,machine_key=cache_key,verified=False)
        else:
            record={'pdf':digest,'page':review['page'],'box':review.get('table_source_box'),'num':review.get('num',''),
                'recognition_hash':table_translate._hash(review.get('table_native')),
                'source_hash':fingerprint,'source':copy.deepcopy(preview),'initial':initial,'machine':machine,'machine_key':cache_key,
                'human':None,'revision':0,'verified':False,'history':[]}
            if current:record['history'].append({'at':now_iso(),'previous':current})
            value['records'][key]=record
        write_json_atomic(ws.root/FILE,value)
    locate(ws,key)
    return key

def protect_authored(ws,source,digest):
    """Recognition can invalidate a check, never replace a human view."""
    with dir_lock(ws.root),writing(ws,'table-editor'):
        value=state(ws);protected=False;changed=False
        for record in value['records'].values():
            if record['pdf']!=digest or record['page']!=source['page'] or not (record.get('human') or record.get('verified')):continue
            if record['box']!=source.get('table_source_box') and (not source.get('num') or record.get('num')!=source['num']):continue
            protected=True
            if record.get('recognition_hash')!=table_translate._hash(source.get('table_native')):
                record.update(verified=False,source_changed=True,incoming_native=copy.deepcopy(source.get('table_native')),
                              incoming_hash=table_translate._hash(source.get('table_native')),
                              incoming_recognition_hash=table_translate._hash(source.get('table_native')));changed=True
        if changed:write_json_atomic(ws.root/FILE,value)
        return protected

def public(record):
    return {'view':view(record),'revision':record['revision'],'verified':record.get('verified',False),
            'source':record.get('incoming_source') or record['source'],'source_hash':record['source_hash'],'has_human':bool(record.get('human')),
            'latest_source_hash':record.get('incoming_hash') or record['source_hash'],
            'translation_available':bool(record.get('machine') or record.get('human')),'source_changed':record.get('source_changed',False)}

def prepare_complete(ws,draft):
    table=draft['table'];digest=draft['pdf'];rows=table['head_en']+table['rows_en']
    translated=table.get('head_zh',[])+table.get('rows_zh',[])
    preview={'rows':len(rows),'columns':len(rows[0]),'slots':[], 'checks':table.get('table_checks',{}),
             'translation_allowed':False,'read_only':True,'complete':False}
    for r,row in enumerate(rows):
        preview['slots'].append([{'row':r,'column':c,'text':text,'status':'source_checked','engine':table.get('table_evidence',{}).get('method','existing'),
            'location':table.get('table_cell_boxes',{}).get(f'{"head" if r<len(table["head_en"]) else "rows"}:{r if r<len(table["head_en"]) else r-len(table["head_en"])}:{c}')}
            for c,text in enumerate(row)])
    review={**table,'partial_preview':preview}
    key=capture(ws,review,digest)
    with dir_lock(ws.root),writing(ws,'table-editor'):
        value=state(ws);record=value['records'][key]
        if not record.get('human') and not record.get('machine'):
            machine=copy.deepcopy(record['initial'])
            for cell in machine['cells']:cell['text']=translated[cell['row']][cell['column']]
            machine.update(caption=table.get('caption_zh',''),footnotes=table.get('footnotes_zh',[]))
            record['machine']=machine;write_json_atomic(ws.root/FILE,value)
    return key

def _validate(v):
    rows,cols=v.get('rows'),v.get('columns')
    if type(rows)!=int or type(cols)!=int or not 1<=rows<=1000 or not 1<=cols<=1000 or rows*cols>100000:raise ValueError('Invalid editor dimensions')
    occupied=set();ids=set()
    for c in v['cells']:
        if c['id'] in ids:raise ValueError('Duplicate editor cell')
        ids.add(c['id']);r,k,rs,cs=[c[n] for n in ('row','column','row_span','col_span')]
        if any(type(n)!=int for n in (r,k,rs,cs)) or min(r,k)<0 or min(rs,cs)<1 or r+rs>rows or k+cs>cols:raise ValueError('Invalid editor span')
        if c['text'] is not None and not isinstance(c['text'],str):raise ValueError('Invalid editor text')
        for y in range(r,r+rs):
            for x in range(k,k+cs):
                if (y,x) in occupied:raise ValueError('Overlapping editor cells')
                occupied.add((y,x))
    if len(occupied)!=rows*cols:raise ValueError('Unrepresented editor slots')
    if not isinstance(v.get('caption'),str) or not isinstance(v.get('footnotes'),list) or any(not isinstance(n,str) for n in v['footnotes']):raise ValueError('Invalid editor metadata')

def handle(ws,body):
    if body['action']=='editor_prepare':
        from . import auto_tables
        draft=auto_tables.state(ws)['drafts'][body['draft_id']]
        if draft['pdf']!=table_translate.pdf_hash(ws) or tables.translation_problem(draft['table']):raise ValueError('Stale table draft')
        key=prepare_complete(ws,draft)
        return {'editor_id':key,**public(state(ws)['records'][key])}
    key=body['editor_id']
    with dir_lock(ws.root),writing(ws,'table-editor'):
        value=state(ws);record=value['records'][key]
        if record['pdf']!=table_translate.pdf_hash(ws):raise ValueError('Source changed; prior edits retained')
        if body['action']=='editor_open':return public(record)
        if body.get('revision')!=record['revision']:raise ValueError('Editor changed; reload before saving')
        if body['action']=='editor_verify':
            if record.get('source_changed') and body.get('source_hash')!=record.get('incoming_hash'):
                raise ValueError('Recognition source changed; explicitly review current source before verification')
            if body.get('confirmed') is not True:raise ValueError('Explicit human verification required')
            v=view(record);_validate(v)
            if any(c['text'] is None for c in v['cells']):raise ValueError('Resolve missing cells before verification')
            record['verified']=True
            if record.get('source_changed'):
                record.update(source_changed=False,source_hash=record['incoming_hash'],recognition_hash=record['incoming_recognition_hash'])
            record['history'].append({'at':now_iso(),'actor':'user','action':'verify','source_hash':record['source_hash']})
        else:
            if body['action']!='editor_save':raise ValueError('Unknown editor action')
            v=copy.deepcopy(body['view']);_validate(v)
            record['history'].append({'at':now_iso(),'actor':'user','previous_view':view(record)})
            record.update(human=v,verified=False)
        record['revision']+=1;write_json_atomic(ws.root/FILE,value)
        return public(record)
