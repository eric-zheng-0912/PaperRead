"""PyInstaller runtime hook for the isolated delivery build ONLY.

Never referenced by the production builder. No environment flag in the normal
application enables this hook. Frozen review builds refuse unmarked profiles.
"""
import json
import os
from pathlib import Path
import re
import socket
import sys
import types

root = Path(os.environ.get('PAPERREAD_TEST_PROFILE', '')).resolve()
if (not getattr(sys, 'frozen', False) or os.environ.get('PAPERREAD_REVIEW_MODE') != 'Mock' or
        not root.is_absolute() or (root/'.simple-table-mock').read_text(encoding='utf8') != 'Mock-only'):
    raise RuntimeError('This delivery executable requires its dedicated Mock profile')
os.environ['PAPERREAD_DISABLE_TABLE_EXTRACTION'] = '1'
from easyread import engines, netcheck


def blocked(*args, **kwargs):
    raise AssertionError('Real model transport forbidden in delivery build')


def response(cfg, prompt, cwd, images=None, cancel=None, meter=None):
    if not Path(cwd).resolve().is_relative_to(root/'library'):
        raise AssertionError('Mock request outside isolated library')
    kind = 'simple'
    if prompt.startswith('Translate only these source text segments'):
        data = json.loads(prompt.split('\n', 1)[1])
        translated = []
        for segment in data['segments']:
            pieces = re.split(r'(\[\[READING\d+\]\])',segment['en'])
            for i in range(0,len(pieces),2):
                pieces[i] = re.sub(r'[A-Za-z]{2,}', '模擬繁體中文譯文', pieces[i])
            translated.append({'id':segment['id'],'zh':''.join(pieces)})
        result = {'segments':translated}
    elif 'delivery-body' in prompt:
        kind = 'body'
        result = {'zh': {'delivery-body': '迴歸結果列於表 3，方程式 2 提供相關估計。'}}
    else:
        raise AssertionError('Unexpected Mock prompt; no live fallback')
    with (root/'model-calls.jsonl').open('a',encoding='utf8') as log:
        log.write(json.dumps({'kind':kind})+'\n')
    return json.dumps(result,ensure_ascii=False)


engines.run = response
engines._run = blocked
engines._popen = blocked
netcheck.problem = lambda cfg: None
original_connect = socket.socket.connect


def local_only(self, address):
    if isinstance(address,tuple) and address[0] not in ('127.0.0.1','::1','localhost'):
        raise AssertionError('External network forbidden in delivery build')
    return original_connect(self,address)


socket.socket.connect = local_only

# translate_pages performs a preflight before calling engines.run. Give only
# this fixture entry an engines dependency that checks the Mock transport.
# The real engines.require_model_access and review_policy remain unchanged.
from easyread import translate, review_policy


def require_mock_transport():
    if (engines.run is not response or engines._run is not blocked or
            engines._popen is not blocked or socket.socket.connect is not local_only or
            review_policy.model_error() is None):
        raise AssertionError('Mock transport isolation changed')


_original_translate_pages = translate.translate_pages
_mock_engines = types.SimpleNamespace(**vars(engines))
_mock_engines.require_model_access = require_mock_transport
_mock_globals = dict(_original_translate_pages.__globals__, engines=_mock_engines)
_mock_translate_pages = types.FunctionType(_original_translate_pages.__code__, _mock_globals,
    _original_translate_pages.__name__, _original_translate_pages.__defaults__,
    _original_translate_pages.__closure__)


def translate_fixture_pages(ws, cfg, pages, cancel, report, meter=None, read=False):
    if (not Path(ws.root).resolve().is_relative_to(root/'library') or pages != [2] or read or
            not any(b.get('id') == 'delivery-body' for b in ws.load('paper').get('blocks', []))):
        raise AssertionError('Unexpected whole-text Mock fixture')
    require_mock_transport()
    return _mock_translate_pages(ws, cfg, pages, cancel, report, meter, read)


translate.translate_pages = translate_fixture_pages
