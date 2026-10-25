"""Disposable localhost reader for real-browser Mock acceptance. Keeps evidence.

No production configuration is loaded: set the new profile before app imports.
Run only from the repository venv; stdout's first line describes the fixture.
"""
import json
import os
from pathlib import Path
import re
import socket
import tempfile
from unittest import mock


def main():
    repo = Path(__file__).resolve().parents[2]
    parent = repo / '.paperread-local-test'
    parent.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='simple-table-browser-', dir=parent)).resolve()
    os.environ.update(PAPERREAD_TEST_PROFILE=str(root), PAPERREAD_REVIEW_MODE='Mock',
                      PAPERREAD_OFFLINE_TEST='1', EASYREAD_HOME=str(root/'home'),
                      EASYREAD_LIBRARY=str(root/'library'))
    from http.server import ThreadingHTTPServer
    from easyread import config, engines, pdfwork, server, table_extract
    (root/'home').mkdir(exist_ok=True)
    config.CONFIG_PATH.write_text(json.dumps({'engine':'openai','auto_translate':False,'target':'en',
                                             'library_dir':str(root/'library')}),encoding='utf8')
    app = server.App({'library_dir': str(root/'library')})
    data = (repo/'tests/fixtures/tables/benchmark-v1/regression/continued_unlabelled.pdf').read_bytes()
    ws, _ = app.lib.create_from_pdf(data, 'simple-table-mock.pdf')
    pages = pdfwork.render_pages(ws.source_pdf, ws.root/'pages', scale=1)
    ws.update('paper', lambda p: p.update(meta={**p['meta'],'title_en':'Simple table Mock acceptance',
        'pages':pages,'page_count':2}, blocks=[
        {'id':'body1','type':'para','page':1,'en':'Existing English body.','zh':'既有正文譯文。'},
        {'id':'tab1','type':'table','page':1,'num':'1','caption_en':'Table 1: Estimates',
         'caption_zh':'既有標題','head':[['Legacy']], 'rows':[['Keep']], 'table_status':'fallback'}],
        translation={'done_pages':[1,2]}))
    ws.apply_reader_ops([{'op':'note','note':{'id':'keep-note','anchor':'body1','body':'既有筆記'}}])
    if os.environ.get('PAPERREAD_MANUAL_LINKS_FIXTURE') == '1':
        ws.update('paper', lambda p: p['blocks'].extend([
            {'id':'body-after','type':'para','page':1,'en':'Text after the original table.', 'zh':'表格後的既有正文。'},
            {'id':'tab2','type':'table','page':1,'num':'2','caption_en':'Table 2: Other placeholder',
             'caption_zh':'同頁另一個佔位區','table_status':'fallback'}]))
    (root/'initial-paper.json').write_bytes(ws.paper_path.read_bytes())
    (root/'initial-reader.json').write_bytes(ws.reader_path.read_bytes())
    calls = []
    words = {'Table':'表','Estimates':'估計結果','Measure':'測量項目','Estimate':'估計值','Error':'誤差',
             'Alpha':'阿爾法','Beta':'貝塔','Gamma':'伽瑪','Delta':'德爾塔','Epsilon':'艾普西隆',
             'Zeta':'澤塔','Further':'後續','results':'結果'}
    def response(cfg, prompt, cwd, images, cancel, meter):
        calls.append(1)
        (root/'mock-calls.json').write_text(json.dumps(len(calls)),encoding='utf8')
        content = json.loads(prompt.split('\n',1)[1])
        translated=[]
        for segment in content['segments']:
            pieces=re.split(r'(\[\[READING\d+\]\])',segment['en'])
            for i in range(0,len(pieces),2):
                pieces[i]=re.sub(r'[A-Za-z]+',lambda m: words[m.group()],pieces[i])
            translated.append({'id':segment['id'],'zh':''.join(pieces)})
        return json.dumps({'segments':translated},ensure_ascii=False)
    original_connect = socket.socket.connect
    def local_only(self, address):
        if isinstance(address,tuple) and address[0] not in ('127.0.0.1','::1','localhost'):
            raise AssertionError('External network forbidden')
        return original_connect(self,address)
    class Handler(server.Handler):
        pass
    Handler.app=app
    http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    print(json.dumps({'origin':f'http://127.0.0.1:{http.server_port}', 'pid':ws.id,
                      'profile':str(root),'paper':str(ws.paper_path),'token':app.token}),flush=True)
    with mock.patch.object(engines,'run',side_effect=response), \
         mock.patch.object(engines,'_popen',side_effect=AssertionError('No model CLI')), \
         mock.patch.object(table_extract,'_matrix',side_effect=AssertionError('No matrix in simple translation')), \
         mock.patch.object(socket.socket,'connect',local_only):
        http.serve_forever(poll_interval=.1)


if __name__ == '__main__':
    main()
