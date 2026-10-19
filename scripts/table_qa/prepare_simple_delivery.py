"""Fresh real-PDF desktop fixture; never modifies an existing review profile."""
import hashlib
import json
import os
from pathlib import Path
import tempfile


def main():
    repo = Path(__file__).resolve().parents[2]
    parent = repo/'.paperread-local-test'
    parent.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='simple-delivery-',dir=parent)).resolve()
    os.environ.update(PAPERREAD_TEST_PROFILE=str(root), PAPERREAD_REVIEW_MODE='Mock',
        PAPERREAD_OFFLINE_TEST='1', PAPERREAD_DISABLE_TABLE_EXTRACTION='1',
        EASYREAD_HOME=str(root/'home'), EASYREAD_LIBRARY=str(root/'library'))
    from easyread import config, pdfwork, simple_tables
    from easyread.library import Library
    from easyread.store import write_json_atomic
    (root/'home').mkdir()
    (root/'.simple-table-mock').write_text('Mock-only',encoding='utf8')
    write_json_atomic(config.CONFIG_PATH,{'engine':'openai','auto_translate':False,'check_updates':False,
        'library_dir':str(root/'library'),'target':'zh-TW','page_check':False,'concurrency':1})
    lib = Library(root/'library')
    fixture = repo/'tests/fixtures/tables/nist-tn-2113-pages-17-19.pdf'
    ws, _ = lib.create_from_pdf(fixture.read_bytes(),fixture.name)
    pages = pdfwork.prepare(ws.root)
    ws.update('paper',lambda p:p.update(meta={**p['meta'],'target':'zh-TW','title_en':'NIST TN 2113 · Desktop Mock delivery',
        'pages':pages,'page_count':len(pages)}, blocks=[
        {'id':'old-body','type':'para','page':1,'en':'Existing English body.','zh':'既有正文譯文。'},
        {'id':'legacy-table','type':'table','page':1,'num':'1','caption_en':'Table 1. Legacy table',
         'caption_zh':'既有表格標題','head':[['Legacy heading']],'rows':[['Original value']]},
        {'id':'delivery-body','type':'para','page':2,
         'en':'The results of the regression in Equation 2 are provided in Table 3.'}],
        translation={'done_pages':[1,2,3],'en_pages':[2]}))
    ws.apply_reader_ops([{'op':'note','note':{'id':'delivery-note','anchor':'old-body','body':'保留既有筆記'}}])
    write_json_atomic(ws.root/'layout.json',{})
    selections = []
    for number in (2,1):
        accepted = []
        for candidate in simple_tables.candidates(ws,number):
            try:
                simple_tables.extract(ws,[candidate])
                accepted.append(candidate)
            except ValueError:
                pass
        if not accepted:
            raise RuntimeError(f'Real PDF page {number} has no safe fixture selection')
        selections.append(accepted[0])
    for name in ('paper','reader','layout'):
        (root/f'initial-{name}.json').write_bytes((ws.root/f'{name}.json').read_bytes())
    info={'profile':str(root),'pid':ws.id,'paper':str(ws.paper_path),'regions':selections,
          'source_sha256':hashlib.sha256(ws.source_pdf.read_bytes()).hexdigest(),
          'config_sha256':hashlib.sha256(config.CONFIG_PATH.read_bytes()).hexdigest()}
    (repo/'tmp/simple-delivery-fixture.json').write_text(json.dumps(info,indent=2),encoding='utf8')
    print(json.dumps(info),flush=True)


if __name__=='__main__':
    main()
