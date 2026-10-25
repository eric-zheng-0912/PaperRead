"""Group review evidence by physical PDF source, not caption/name alone.

Read-only reconciliation: original reviews, edits and verification records stay
intact. Geometry-only aliases never create an insertion anchor or new cells.
"""
import copy
from . import table_translate,table_extract
from .table_regions import LABEL

def _uid(pdf,page,box):return table_translate._hash({'pdf':pdf,'page':page,'box':box})

def groups(ws,items,value,digest):
    native={}
    sources=[r for r in value['reviews'].values() if r.get('pdf')==digest]
    sources.extend(d['table'] for d in value['drafts'].values() if d.get('pdf')==digest)
    for source in sources:
        if not source.get('table_native') or not source.get('table_source_box'):continue
        page=source['page'];box=source['table_source_box'];key=_uid(digest,page,box)
        native[key]={'page':page,'box':box,'source':source}
    if not native:return items
    # Resolve missing region references against source caption geometry. A match
    # needs unique reciprocal nearest neighbours and no intervening PDF text.
    import pdfplumber
    aliases={}
    with pdfplumber.open(ws.source_pdf) as pdf:
        for number in sorted({s['page'] for s in native.values()}):
            page=pdf.pages[number-1];lines=table_extract._lines(page)
            captions=[l for l in lines if LABEL.match(l['text'])]
            candidates={k:s for k,s in native.items() if s['page']==number}
            links=[]
            for index,cap in enumerate(captions):
                x0=min(w['x0'] for w in cap['words']);x1=max(w['x1'] for w in cap['words'])
                for key,source in candidates.items():
                    box=[v*(page.width if i%2==0 else page.height) for i,v in enumerate(source['box'])]
                    a,b=box[1],box[3]
                    for note in source['source']['table_native'].get('footnotes',[]):
                        for p in note.get('provenance',[]):
                            if p['page_no']==number and p['bbox'].get('coord_origin')=='TOPLEFT':
                                a=min(a,p['bbox']['t']);b=max(b,p['bbox']['b'])
                    left,right=max(x0,box[0]),min(x1,box[2])
                    if left>=right:continue
                    if cap['bottom']<=a:lo,hi=cap['bottom'],a
                    elif cap['top']>=b:lo,hi=b,cap['top']
                    else:continue
                    if any(l is not cap and lo<(l['top']+l['bottom'])/2<hi and
                           any(w['x0']<right and w['x1']>left for w in l['words']) for l in lines):continue
                    links.append((hi-lo,index,key))
            for distance,index,key in links:
                by_cap=[v for v in links if v[1]==index];by_source=[v for v in links if v[2]==key]
                if (distance!=min(v[0] for v in by_cap) or sum(v[0]==distance for v in by_cap)!=1 or
                    distance!=min(v[0] for v in by_source) or sum(v[0]==distance for v in by_source)!=1):continue
                # Exact source line identity is required; ambiguous repeated
                # captions remain separate even if one is spatially closer.
                text=''.join(captions[index]['text'].split())
                if sum(''.join(c['text'].split())==text for c in captions)==1:
                    aliases[(number,text)]=key
    def source_id(item):
        if item.get('pdf',digest)!=digest:return None
        if item.get('draft_id') and value['drafts'].get(item['draft_id'],{}).get('pdf')!=digest:return None
        region=(item.get('regions') or [{}])[0].get('box')
        if region:
            key=_uid(digest,item.get('page'),region)
            return key if key in native else None
        # A reader block's caption is not necessarily source text. Use saved
        # recognizer evidence, never a display title, to establish this alias.
        refs=[r for r in value['reviews'].values() if r.get('page')==item.get('page') and
              r.get('num') and r.get('num')==item.get('num') and r.get('caption_en')]
        ids={aliases[(r['page'],''.join(r['caption_en'].split()))] for r in refs
             if (r['page'],''.join(r['caption_en'].split())) in aliases}
        return next(iter(ids)) if len(ids)==1 else None
    buckets={};unmatched=[]
    for item in items:
        key=source_id(item)
        if key:buckets.setdefault(key,[]).append(item)
        else:unmatched.append(item)
    for key,members in buckets.items():
        primary=max(members,key=lambda i:(bool(i.get('protected')),bool(i.get('editor_id')),bool(i.get('partial_preview')),bool(i.get('cache_valid'))))
        merged=copy.deepcopy(primary);merged['source_id']=key
        merged['evidence_members']=[{'id':i['id'],'reason':i.get('reason',''),'state':i['state']} for i in members]
        merged['candidate_block_ids']=sorted({b for i in members for b in ([i['block_id']] if i.get('block_id') else i.get('candidate_block_ids',[]))})
        unmatched.append(merged)
    return unmatched
