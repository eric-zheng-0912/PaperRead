"""Offline process bridge, immutable recognition cache, and engine arbitration.

This module has no Docling/torch dependency. Native cells are never blended or
repaired; PDFium differences are evidence, never replacement text.
"""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata

from . import engines, pdfwork, table_extract, table_translate, tables
from .background import writing
from .store import dir_lock, read_json, write_json_atomic

CACHE = 'docling_recognition.json'
SCHEMA = 1


class WorkerUnavailable(ValueError):
    pass


def _environment(cfg):
    from . import docling_env
    found = docling_env.discover(cfg)
    if not found['selected']:
        details = '; '.join(x.get('reason', x['status']) for x in found['candidates'])
        raise WorkerUnavailable(found['reason'] + ': ' + details)
    return Path(found['selected']['python']), Path(found['hub']), found['models']


def _invoke(executable, request, timeout, cancel):
    worker = Path(__file__).with_name('docling_worker.py')
    if not worker.is_file():
        raise WorkerUnavailable('Standalone Docling worker resource is missing')
    env = {k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME','NODE_OPTIONS') and
           not re.search(r'(?:API_KEY|ACCESS_TOKEN|HF_TOKEN)$',k)}
    env.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',PYTHONDONTWRITEBYTECODE='1')
    log_path = Path(request['runtime'])/'worker.log'
    start = time.monotonic()
    with log_path.open('wb') as log:
        process = subprocess.Popen([str(executable),'-I','-B',str(worker)],stdin=subprocess.PIPE,
            stdout=log,stderr=log,env=env,cwd=request['runtime'],
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        try:
            process.stdin.write(json.dumps(request).encode('utf8'));process.stdin.close()
            while process.poll() is None:
                table_translate._cancel(cancel)
                if time.monotonic()-start>timeout:
                    raise WorkerUnavailable('Docling timed out; using existing extractor')
                time.sleep(.1)
            table_translate._cancel(cancel)
            if process.returncode:
                raise WorkerUnavailable('Docling failed: '+log_path.read_text(encoding='utf8',errors='replace')[-1200:])
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()  # Reap on cancellation, timeout and exceptions. No retries.
    output = Path(request['output'])
    if not output.is_file() or output.stat().st_size>64*1024*1024:
        raise WorkerUnavailable('Invalid Docling response size')
    return json.loads(output.read_text(encoding='utf8'))


def recognize_document(ws, cfg, cancel=None):
    """One whole-PDF worker per hash/config; OS lock also deduplicates processes."""
    if cfg.get('docling_enabled') is False:
        return {'status':'unavailable','reason':'Docling disabled; existing extractor retained'}
    try:
        executable,hub,models = _environment(cfg)
        worker = Path(__file__).with_name('docling_worker.py')
        identity = table_translate._hash({'protocol':SCHEMA,'worker':hashlib.sha256(worker.read_bytes()).hexdigest(),
            'python':str(executable),'models':models,'mode':'accurate','cpu':4,'ocr':False})
        digest = table_translate.pdf_hash(ws)
        with dir_lock(ws.root,'.docling-recognition.lock',timeout=180):
            value = read_json(ws.root/CACHE,{})
            if not isinstance(value,dict):
                raise WorkerUnavailable('Unreadable recognition cache retained; using existing extractor')
            if (value.get('identity')==identity and value.get('pdf')==digest and
                    value.get('content_hash')==table_translate._hash(value.get('result'))):
                return {**value['result'],'status':'ok','cache_reused':True}
            table_translate._cancel(cancel)
            # A shared library-level lock prevents simultaneous torch workers.
            with dir_lock(ws.root.parent,'.docling-worker.lock',timeout=180):
                shared_dir=ws.root.parent/'.table-recognition-cache'
                shared_file=shared_dir/(table_translate._hash({'identity':identity,'pdf':digest})+'.json')
                shared=read_json(shared_file,{})
                if (isinstance(shared,dict) and shared.get('identity')==identity and shared.get('pdf')==digest and
                        shared.get('content_hash')==table_translate._hash(shared.get('result'))):
                    with dir_lock(ws.root),writing(ws,'docling-recognition'):
                        table_translate._cancel(cancel)
                        if table_translate.pdf_hash(ws)!=digest:raise WorkerUnavailable('Source changed; cached result not used')
                        write_json_atomic(ws.root/CACHE,shared)
                    return {**shared['result'],'status':'ok','cache_reused':True}
                with tempfile.TemporaryDirectory(prefix='.docling-',dir=ws.root) as temp:
                    runtime = Path(temp)
                    snapshot = runtime/'source.pdf';shutil.copyfile(ws.source_pdf,snapshot)
                    if table_translate.pdf_hash(ws)!=digest or hashlib.sha256(snapshot.read_bytes()).hexdigest()!=digest:
                        raise WorkerUnavailable('Source changed before Docling recognition')
                    request = {'pdf':str(snapshot),'pdf_sha256':digest,'runtime':str(runtime),
                               'hub':str(hub),'output':str(runtime/'result.json')}
                    try:
                        result = _invoke(executable,request,float(cfg.get('docling_timeout',180)),cancel)
                    finally:
                        if (runtime/'worker.log').exists():
                            shutil.copyfile(runtime/'worker.log',ws.root/'docling-last.log')
                if result.get('protocol')!=SCHEMA or result.get('pdf')!=digest or not isinstance(result.get('tables'),list):
                    raise WorkerUnavailable('Docling response identity mismatch')
                from . import docling_env
                docling_env.mark_verified(executable, models)
                table_translate._cancel(cancel)
                with dir_lock(ws.root),writing(ws,'docling-recognition'):
                    table_translate._cancel(cancel)
                    if table_translate.pdf_hash(ws)!=digest:
                        raise WorkerUnavailable('Source changed; Docling response discarded')
                    entry={'identity':identity,'pdf':digest,'result':result,'content_hash':table_translate._hash(result)}
                    shared_dir.mkdir(exist_ok=True)
                    write_json_atomic(shared_file,entry)
                    write_json_atomic(ws.root/CACHE,entry)
            return {**result,'status':'ok','cache_reused':False}
    except engines.Cancelled:
        raise
    except (OSError,ValueError,KeyError,TypeError) as exc:
        return {'status':'unavailable','reason':str(exc)[:1500]}


def _box(value):
    if not isinstance(value,dict) or value.get('coord_origin')!='TOPLEFT':
        raise ValueError('Unsupported source coordinate origin')
    box = [value[k] for k in ('l','t','r','b')]
    if not all(isinstance(x,(int,float)) and math.isfinite(x) for x in box) or box[0]>=box[2] or box[1]>=box[3]:
        raise ValueError('Invalid source box')
    return box


def _plain(text):
    return ''.join(c for c in text if not c.isspace())  # Comparison only; never saved as source text.


def _difference(observed, source):
    if observed==source:return None
    import difflib
    differences = []
    for tag,a,b,c,d in difflib.SequenceMatcher(None,source,observed,autojunk=False).get_opcodes():
        if tag!='equal':differences.append({'operation':tag,'source':source[a:b],'engine':observed[c:d]})
    # Preserve ordered numeric/formula/statistical invariants. Unicode symbols
    # (including nonnumeric check/cross values) are independent invariants too.
    critical = tables.protected(_plain(observed))!=tables.protected(_plain(source)) or (
        [c for c in observed if unicodedata.category(c).startswith('S')] !=
        [c for c in source if unicodedata.category(c).startswith('S')])
    cosmetic = all(all(x.isspace() or unicodedata.category(x).startswith('P') for x in v['source']+v['engine']) for v in differences)
    return {'level':'blocking' if critical or not cosmetic else 'warning',
            'kind':'value_or_text_conflict' if critical or not cosmetic else 'punctuation_or_whitespace',
            'differences':differences,'source':source,'engine':observed}


def _partial(native, page, tp):
    """Read-only native slots. Absence is unresolved, never an empty cell."""
    rows, cols = native['rows'], native['columns']
    if type(rows)!=int or type(cols)!=int or not (2<=rows<=1000 and 2<=cols<=1000 and rows*cols<=100000):
        raise ValueError('Invalid native table dimensions')
    width,height=page.get_size();number=native['provenance'][0]['page_no']
    slots=[[{'status':'missing','text':None,'row':r,'column':c} for c in range(cols)] for r in range(rows)]
    invalid=len(native.get('provenance',[]))!=1;conflict=False;warnings=False
    region=_box(native['provenance'][0]['bbox'])
    for cell in native['cells']:
        r,c=cell['start_row_offset_idx'],cell['start_col_offset_idx']
        if type(r)!=int or type(c)!=int or not (0<=r<rows and 0<=c<cols):
            invalid=True;continue
        slot=slots[r][c]
        if slot['status']!='missing':
            slot['status']='conflict';invalid=True;continue
        slot.update(text=cell.get('text'),engine='docling',status='unresolved')
        if (cell.get('row_span')!=1 or cell.get('col_span')!=1 or
            cell.get('end_row_offset_idx')!=r+1 or cell.get('end_col_offset_idx')!=c+1):
            invalid=True;continue
        try:
            box=_box(cell['bbox']);slot['box']=box;slot['page']=number
            if (not isinstance(slot['text'],str) or box[0]<region[0] or box[1]<region[1] or box[2]>region[2] or box[3]>region[3] or
                box[0]<0 or box[1]<0 or box[2]>width or box[3]>height or
                page.get_rotation() or any(abs(x)>1e-6 for x in page.get_bbox()[:2])):continue
            raw_text=tp.get_text_bounded(left=box[0],bottom=height-box[3],right=box[2],top=height-box[1])
            difference=_difference(slot['text'],raw_text)
            slot['difference']=difference
            slot['status']='conflict' if difference and difference['level']=='blocking' else 'source_checked'
            conflict|=slot['status']=='conflict';warnings|=bool(difference)
        except (ValueError,KeyError,TypeError):pass
    # Independently indexed cells must agree with spatial ordering. Matching
    # text inside boxes alone cannot detect a simultaneous text/box reassignment.
    for group,axis in ([(row,0) for row in slots]+[([slots[r][c] for r in range(rows)],1) for c in range(cols)]):
        centers=[(s['box'][axis]+s['box'][axis+2])/2 for s in group if s.get('box')]
        if any(a>=b for a,b in zip(centers,centers[1:])):invalid=True
    missing=sum(s['status']=='missing' for row in slots for s in row)
    unresolved=any(s['status']=='unresolved' for row in slots for s in row)
    from pypdfium2 import raw
    for i in range(tp.count_chars()):
        code=raw.FPDFText_GetUnicode(tp,i)
        if not code or code>0x10ffff or 0xd800<=code<=0xdfff:continue
        char=chr(code)
        l,b,r,t=tp.get_charbox(i,loose=False);x,y=(l+r)/2,height-(b+t)/2
        if not(region[0]<=x<=region[2] and region[1]<=y<=region[3]):continue
        owners=[s for row in slots for s in row if s.get('box') and s['box'][0]<=x<s['box'][2] and s['box'][1]<=y<s['box'][3]]
        if ((unicodedata.category(char)[0] in ('N','S') and len(owners)!=1) or
            raw.FPDFText_HasUnicodeMapError(tp,i) or abs(raw.FPDFText_GetCharAngle(tp,i))>1e-6):conflict=True
    checks={'structure':{'status':'invalid' if invalid else 'incomplete' if missing else 'complete','missing_cells':missing},
        'source':{'status':'conflict' if conflict else 'unresolved' if unresolved else 'warning' if warnings else 'checked'},
        'metadata':{'caption':'available' if native.get('captions') else 'missing_or_unknown',
                    'footnotes':'available_completeness_unknown' if native.get('footnotes') else 'missing_or_unknown'},
        'location':{'status':'unresolved'}}
    for field in ('captions','footnotes'):
        for item in native.get(field,[]):
            try:
                prov=item['provenance']
                if len(prov)!=1 or prov[0]['page_no']!=number:raise ValueError('Metadata source unresolved')
                box=_box(prov[0]['bbox'])
                difference=_difference(item['text'],tp.get_text_bounded(left=box[0],bottom=height-box[3],right=box[2],top=height-box[1]))
                if difference and difference['level']=='blocking':conflict=True
                warnings|=bool(difference)
            except (ValueError,KeyError,TypeError):unresolved=True
    if conflict:checks['source']['status']='conflict'
    elif unresolved:checks['source']['status']='unresolved'
    elif warnings:checks['source']['status']='warning'
    return {'version':1,'read_only':True,'complete':False,'rows':rows,'columns':cols,'slots':slots,'checks':checks,
            'translation_allowed':not invalid and not conflict and not unresolved,
            'caption':native.get('captions',[]),'footnotes':native.get('footnotes',[])}


def _adapt(native, page, tp):
    """Do not repair cells. Unsupported spans/coverage go to preview/review."""
    from .table_regions import LABEL
    prov = native['provenance']
    if len(prov)!=1:raise ValueError('Cross-page provenance requires manual review')
    number = prov[0]['page_no'];width,height = page.get_size()
    if page.get_rotation() or any(abs(x)>1e-6 for x in page.get_bbox()[:2]):
        raise ValueError('Rotated or nonzero-origin source needs coordinate review')
    region = _box(prov[0]['bbox'])
    if region[0]<0 or region[1]<0 or region[2]>width or region[3]>height:
        raise ValueError('Native table region is outside the source page')
    rows,cols = native['rows'],native['columns']
    if type(rows)!=int or type(cols)!=int or not (2<=rows<=1000 and 2<=cols<=1000 and rows*cols<=100000):
        raise ValueError('Invalid native table dimensions')
    captions = native.get('captions',[])
    caption = ' '.join(c['text'] for c in captions)
    match = LABEL.match(caption)
    source = {'type':'table','page':number,'num':match.group(1) if match else '', 'table_schema':1,
        'caption_en':caption,'caption_pos':'above','footnotes_en':[n['text'] for n in native.get('footnotes',[])],
        'table_status':'ready','table_source_box':[region[i]/(width if i%2==0 else height) for i in range(4)],
        'table_evidence':{'method':'docling','engine':'docling-2.126.0','native_id':native['id']},
        'source_audit':{'version':1,'risks':[],'blocking':False,'automatic_backfill':False},
        'table_native':copy.deepcopy(native)}
    risks = source['source_audit']['risks']
    matrix = [[None for _ in range(cols)] for _ in range(rows)]
    occupied = set();header_rows=set();geometry={}
    native_boxes=[]
    for cell in native['cells']:
        r,c=cell['start_row_offset_idx'],cell['start_col_offset_idx']
        if (cell['row_span']!=1 or cell['col_span']!=1 or cell['end_row_offset_idx']!=r+1 or cell['end_col_offset_idx']!=c+1):
            raise ValueError('Merged native cells require manual review')
        if not (0<=r<rows and 0<=c<cols) or (r,c) in occupied or not isinstance(cell['text'],str):
            raise ValueError('Overlapping or invalid native cell indices')
        occupied.add((r,c));matrix[r][c]=cell['text']
        if cell.get('column_header'):header_rows.add(r)
        box=_box(cell['bbox'])
        if box[0]<region[0] or box[1]<region[1] or box[2]>region[2] or box[3]>region[3]:
            raise ValueError('Native cell geometry leaves the table region')
        native_boxes.append(((r,c),box))
        geometry[(r,c)]={'page':number,'box':[box[i]/(width if i%2==0 else height) for i in range(4)]}
    if len(occupied)!=rows*cols:raise ValueError('Native grid has unrepresented cells')
    nhead=0
    while nhead in header_rows:nhead+=1
    if header_rows!=set(range(nhead)) or nhead>=rows:raise ValueError('Ambiguous native header roles')
    source.update(head_en=matrix[:nhead],rows_en=matrix[nhead:],table_cell_boxes={})
    for (r,c),loc in geometry.items():
        source['table_cell_boxes'][f'{"head" if r<nhead else "rows"}:{r if r<nhead else r-nhead}:{c}']=loc
    for (r,c),box in native_boxes:
        raw_text=tp.get_text_bounded(left=box[0],bottom=height-box[3],right=box[2],top=height-box[1])
        difference=_difference(matrix[r][c],raw_text)
        if difference:risks.append({'cell':[r,c],**difference})
    # Coordinate exclusivity is checked for data/symbol glyphs. Ambiguous source
    # ownership blocks display; no missing glyph is guessed or backfilled.
    from pypdfium2 import raw
    for i in range(tp.count_chars()):
        code=raw.FPDFText_GetUnicode(tp,i)
        if not code or code>0x10ffff or 0xd800<=code<=0xdfff:continue
        char=chr(code)
        if char.isspace():continue
        l,b,r,t=tp.get_charbox(i,loose=False);cx,cy=(l+r)/2,height-(b+t)/2
        if not(region[0]<=cx<=region[2] and region[1]<=cy<=region[3]):continue
        owners=[key for key,box in native_boxes if box[0]<=cx<box[2] and box[1]<=cy<box[3]]
        critical=unicodedata.category(char)[0] in ('N','S')
        if len(owners)!=1:
            risks.append({'level':'blocking' if critical else 'warning','kind':'ambiguous_glyph_ownership',
                          'glyph':i,'codepoint':code,'owners':[list(k) for k in owners]})
        if raw.FPDFText_HasUnicodeMapError(tp,i) or abs(raw.FPDFText_GetCharAngle(tp,i))>1e-6:
            risks.append({'level':'blocking','kind':'unicode_mapping_or_rotation','glyph':i})
    for kind,values in [('caption',captions),('note',native.get('footnotes',[]))]:
        for index,item in enumerate(values):
            if len(item.get('provenance',[]))!=1 or item['provenance'][0]['page_no']!=number:
                risks.append({'level':'blocking','kind':'ambiguous_'+kind+'_provenance'});continue
            box=_box(item['provenance'][0]['bbox'])
            evidence=tp.get_text_bounded(left=box[0],bottom=height-box[3],right=box[2],top=height-box[1])
            difference=_difference(item['text'],evidence)
            if difference:risks.append({'field':kind,'index':index,**difference})
            if kind=='caption':source['caption_pos']='below' if box[1]>=region[3] else 'above'
            else:source['table_cell_boxes'][f'note:{index}']={'page':number,'box':[box[i]/(width if i%2==0 else height) for i in range(4)]}
    source['source_audit']['blocking']=any(r['level']=='blocking' for r in risks)
    source['table_geometry_source']=tables.source_token(source)
    source['table_structure']={'version':1,'columns':cols,'header_rows':nhead,'body_rows':rows-nhead,
                               'cell_ids':[k for k in source['table_cell_boxes'] if not k.startswith('note:')]}
    return source


def prepare_pages(ws, recognized):
    """Map a native whole-document response using PDFium, without model calls."""
    result={}
    if recognized.get('status')!='ok':return result
    with pdfwork.open_pdf(ws.source_pdf) as pdf:
        for native in recognized['tables']:
            prov=native.get('provenance',[])
            number=prov[0].get('page_no') if prov else None
            if type(number)!=int or not 1<=number<=len(pdf):continue
            group=result.setdefault(number,table_extract.PageTables([],[],[]))
            page=pdf[number-1];tp=page.get_textpage()
            preview=None
            try:
                preview=_partial(native,page,tp)
                source=_adapt(native,page,tp)
                if preview['checks']['structure']['status']=='invalid':
                    source['source_audit']['blocking']=True
                    source['source_audit']['risks'].append({'level':'blocking','kind':'native_index_geometry_conflict'})
                source['table_checks']=preview['checks']
                source['table_checks']['source']['status']='conflict' if source['source_audit']['blocking'] else 'warning' if source['source_audit']['risks'] else 'checked'
                if source['source_audit']['blocking']:
                    source['partial_preview']=preview
                    source['table_issue']='來源文字、數值或儲存格歸屬有疑義；僅顯示來源預覽。'
                    group.unresolved.append(source)
                else:group.sources.append(source)
            except (ValueError,KeyError,TypeError) as exc:
                from .table_regions import LABEL
                caption=' '.join(c.get('text','') for c in native.get('captions',[]));match=LABEL.match(caption)
                item={'page':number,'num':match.group(1) if match else '', 'caption_en':caption,'table_issue':str(exc),
                      'source_audit':{'blocking':True,'risks':[{'level':'blocking','kind':str(exc)}]},'table_native':native}
                if preview:
                    item.update(partial_preview=preview,table_checks=preview['checks'])
                try:
                    box=_box(prov[0]['bbox']);w,h=page.get_size();item['table_source_box']=[box[i]/(w if i%2==0 else h) for i in range(4)]
                except (ValueError,KeyError,TypeError):pass
                group.unresolved.append(item)
            finally:tp.close();page.close()
    return result


def arbitrate(primary, auxiliary):
    """Choose whole sources, never splice cells. Any matrix conflict is blocked."""
    if primary is None:return auxiliary
    sources=[];unresolved=list(primary.unresolved);issues=list(primary.issues)
    represented={s.get('num','').casefold() for s in primary.sources+primary.unresolved}
    for source in primary.sources:
        others=[s for s in auxiliary.sources if s['num'].casefold()==source['num'].casefold()]
        fields=('head_en','rows_en')
        conflict = len(others)>1
        if len(others)==1:
            for field in fields:
                a,b=others[0].get(field,[]),source.get(field,[])
                if [len(r) for r in a]!=[len(r) for r in b]:
                    conflict=True;continue
                if any((d:=_difference(x,y)) and d['level']=='blocking' for ar,br in zip(a,b) for x,y in zip(ar,br)):
                    conflict=True
            for field in ('caption_en','footnotes_en'):
                a,b=others[0].get(field,'' if field=='caption_en' else []),source.get(field,'' if field=='caption_en' else [])
                if field=='footnotes_en' and len(a)!=len(b):conflict=True;continue
                pairs=[(a,b)] if field=='caption_en' else zip(a,b)
                if any((d:=_difference(x,y)) and d['level']=='blocking' for x,y in pairs):conflict=True
        if conflict:
            blocked=copy.deepcopy(source);blocked['source_audit']['blocking']=True
            blocked['source_audit']['risks'].append({'level':'blocking','kind':'extractor_matrix_conflict'})
            blocked['table_issue']='辨識器欄列或儲存格結果衝突；請人工核對。';unresolved.append(blocked)
            blocked.setdefault('table_checks',{})['structure']={'status':'conflict'}
        else:
            selected=copy.deepcopy(source)
            selected['table_evidence']['auxiliary_agrees']=bool(others)
            sources.append(selected)
    sources.extend(s for s in auxiliary.sources if s['num'].casefold() not in represented)
    unresolved.extend(s for s in auxiliary.unresolved if s['num'].casefold() not in represented)
    return table_extract.PageTables(sources,issues+auxiliary.issues,unresolved)
