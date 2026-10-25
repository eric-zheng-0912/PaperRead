"""Bounded discovery of registered local Python environments; never installs.

No machine-specific default. Probe is our fixed standalone worker, with network
blocked before imports. A successful import is not an inference certification.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

_cache = {}
_verified = {}


def mark_verified(executable, signature):
    """Only called after a successful real worker response, never by import probe."""
    import hashlib
    _verified[str(Path(executable).resolve()).casefold()] = (
        signature, hashlib.sha256(Path(__file__).with_name('docling_worker.py').read_bytes()).hexdigest())


def registered(home=None):
    home = Path(home or Path.home())
    found = []
    def add(p, origin):
        if p:
            p = Path(p).expanduser()
            if p.is_absolute() and p.is_file():
                key = str(p.resolve()).casefold()
                if not any(str(x['python']).casefold() == key for x in found):
                    found.append({'python': str(p.resolve()), 'origin': origin})
    def conda_list(file):
        try:
            for line in file.read_text(encoding='utf8').splitlines()[:128]:
                if line.strip(): add(Path(line.strip())/'python.exe', 'conda-registration')
        except OSError:
            pass
    conda_list(home/'.conda/environments.txt')
    roots = []
    if os.name == 'nt':
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                def children(key):
                    names = []
                    for i in range(4096):
                        try: names.append(winreg.EnumKey(key, i))
                        except OSError: break
                    return names
                try:
                    with winreg.OpenKey(hive, 'Software\\Python', 0, winreg.KEY_READ|view) as base:
                        for company in children(base):
                            with winreg.OpenKey(base, company) as ck:
                                for tag in children(ck):
                                    try:
                                        with winreg.OpenKey(ck, tag+'\\InstallPath') as k:
                                            root = Path(winreg.QueryValueEx(k, '')[0]); roots.append(root)
                                            try: exe = winreg.QueryValueEx(k, 'ExecutablePath')[0]
                                            except OSError: exe = root/'python.exe'
                                            add(exe, 'windows-registration')
                                    except OSError: pass
                except OSError: pass
                try:
                    with winreg.OpenKey(hive, 'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall', 0, winreg.KEY_READ|view) as base:
                        for name in children(base):
                            with winreg.OpenKey(base, name) as k:
                                try:
                                    label = winreg.QueryValueEx(k, 'DisplayName')[0].lower()
                                    if any(x in label for x in ('anaconda', 'miniconda', 'miniforge')):
                                        roots.append(Path(winreg.QueryValueEx(k, 'InstallLocation')[0]))
                                except OSError: pass
                except OSError: pass
    conda = shutil.which('conda.exe') or shutil.which('conda')
    if conda: roots.append(Path(conda).parent.parent)
    for root in sorted(set(roots), key=lambda p:str(p).casefold()):
        if not root.is_absolute(): continue
        add(root/'python.exe', 'conda-registration')
        conda_list(root/'.conda/environments.txt')
        # Only the registered installation's envs, never recursive disk search.
        envs = root/'envs'
        if envs.is_dir():
            for env in sorted(envs.iterdir(), key=lambda p:str(p).casefold())[:128]:
                if (env/'conda-meta').is_dir(): add(env/'python.exe', 'conda-registration')
    return sorted(found, key=lambda x:(0 if x['origin'].startswith('conda') else 1, x['python'].casefold()))


def models(cfg):
    import re
    hub = Path(cfg.get('docling_model_cache') or os.environ.get('HF_HUB_CACHE') or
               Path(os.environ.get('HF_HOME', str(Path.home()/'.cache/huggingface')))/'hub').expanduser()
    required = [('docling-project--docling-models','v2.3.0',
        ['model_artifacts/tableformer/accurate/tm_config.json','model_artifacts/tableformer/accurate/tableformer_accurate.safetensors']),
        ('docling-project--docling-layout-heron','main',['config.json','preprocessor_config.json','model.safetensors'])]
    signature=[]
    for repo, ref, files in required:
        folder=hub/('models--'+repo); revision=(folder/'refs'/ref).read_text(encoding='utf8').strip()
        if not re.fullmatch('[0-9a-f]{40,64}',revision): raise ValueError('Invalid local model revision')
        for name in files:
            p=folder/'snapshots'/revision/name
            if not p.is_file() or not p.stat().st_size: raise ValueError('Missing local model: '+repo+'/'+name)
            signature.append([repo,revision,name,p.stat().st_size,p.stat().st_mtime_ns])
    return hub.resolve(), signature


def probe(executable, hub, timeout=120):
    worker=Path(__file__).with_name('docling_worker.py')
    env={k:v for k,v in os.environ.items() if k not in ('PYTHONHOME','PYTHONPATH','NODE_OPTIONS') and
         not any(s in k.upper() for s in ('API_KEY','ACCESS_TOKEN','HF_TOKEN'))}
    env.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',PYTHONDONTWRITEBYTECODE='1')
    with tempfile.TemporaryDirectory(prefix='paperread-docling-probe-') as temp:
        request={'probe':True,'hub':str(hub),'runtime':temp,'output':str(Path(temp)/'result.json')}
        # No shell, -I ignores cwd/user-site/PYTHONPATH; no user supplied script.
        try:
            p=subprocess.run([str(executable),'-I','-B',str(worker)],input=json.dumps(request),
                capture_output=True,text=True,encoding='utf8',errors='replace',env=env,cwd=temp,
                timeout=timeout,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        except subprocess.TimeoutExpired as e:
            trace=e.stdout or ''
            if isinstance(trace,bytes): trace=trace.decode('utf8',errors='replace')
            return {'status':'unavailable','reason':'Environment check timed out; process killed and reaped. '+trace[-1000:]}
        out=Path(request['output'])
        if p.returncode or not out.is_file():
            return {'status':'unavailable','reason':p.stderr[-1200:] or 'Environment check failed'}
        return json.loads(out.read_text(encoding='utf8'))


def discover(cfg, refresh=False):
    explicit=str(cfg.get('docling_python') or os.environ.get('PAPERREAD_DOCLING_PYTHON') or '').strip()
    candidates=registered()
    if explicit:
        candidates=[{'python':explicit,'origin':'configured'}]+[x for x in candidates if x['python'].casefold()!=explicit.casefold()]
    results=[]; selected=None; started=time.monotonic()
    try: hub, signature=models(cfg); model_error=None
    except (OSError,ValueError) as e:
        hub=Path(cfg.get('docling_model_cache') or os.environ.get('HF_HUB_CACHE') or Path.home()/'.cache/huggingface/hub')
        signature=[]; model_error='Local models incomplete: '+str(e)
    for item in candidates[:32]:
        p=Path(item['python']); result={**item,'status':'unverified'}
        if not p.is_absolute() or not p.is_file():
            result.update(status='unavailable',reason='Python path is invalid; choose Python or redetect')
        else:
            key=(str(p.resolve()),p.stat().st_mtime_ns,str(hub))
            cached=_cache.get(key)
            if not refresh and cached and time.monotonic()-cached[0]<300: info=cached[1]
            elif time.monotonic()-started>180:
                info={'status':'unverified','reason':'Discovery time budget reached; select this Python to check separately'}
            else:
                try: info=probe(p,hub,timeout=min(120,max(1,180-(time.monotonic()-started))))
                except (OSError,ValueError) as e: info={'status':'unavailable','reason':str(e)}
                _cache[key]=(time.monotonic(),info)
            result.update(info)
            if model_error and result['status']=='basic_import': result.update(status='incomplete',reason=model_error)
        results.append(result)
        if selected is None and result['status']=='basic_import': selected=result
    if selected:
        signature = signature + [['runtime',selected.get('docling'),selected.get('python_version')]]
        import hashlib
        verified = _verified.get(str(Path(selected['python']).resolve()).casefold()) == (
            signature,hashlib.sha256(Path(__file__).with_name('docling_worker.py').read_bytes()).hexdigest())
    else: verified = False
    return {'selected':selected,'candidates':results,'hub':str(hub),'models':signature,
        'selection_rule':'configured, then registered Conda, then registered Windows; stable path order',
        'status': 'offline_worker_verified' if verified else 'basic_import' if selected else 'unavailable',
        'reason': 'Offline worker inference succeeded during this application session' if verified else
            'Basic offline API check only; model inference not certified' if selected else 'No qualified local Docling; existing extractor remains available'}
