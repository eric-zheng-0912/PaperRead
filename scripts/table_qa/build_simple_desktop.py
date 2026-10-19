"""Build a fresh unpacked Mock-only desktop app without touching old artifacts."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

from scripts import build_backend as backend


def main():
    repo = backend.ROOT
    (repo/'build').mkdir(exist_ok=True)
    build = Path(tempfile.mkdtemp(prefix='simple-delivery-',dir=repo/'build'))
    (repo/'dist/table-review').mkdir(parents=True,exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix='simple-delivery-',dir=repo/'dist/table-review'))
    python = backend.python_with_pyinstaller()
    backend.check_dependencies(python)
    command = python + ['-m','PyInstaller','--noconfirm','--onedir','--name','easyread-backend',
        '--distpath',str(build/'backend'),'--workpath',str(build/'work'),'--specpath',str(build),
        '--runtime-hook',str(repo/'scripts/table_qa/desktop_mock_hook.py'),
        *[arg for module in backend.REQUIRED for arg in ('--collect-all',module)],
        *backend.openssl_binary_args(python),str(repo/'scripts/backend_entry.py')]
    with (build/'backend-build.log').open('w',encoding='utf8') as log:
        subprocess.run(command,cwd=repo,stdout=log,stderr=subprocess.STDOUT,check=True)
    settings = json.loads((repo/'package.json').read_text(encoding='utf8'))['build']
    settings.update(electronDist=str(repo/'node_modules/electron/dist'),
        directories={'output':str(output)}, extraResources=[
        {'from':str(build/'backend/easyread-backend'),'to':'backend','filter':['**/*']}])
    settings['win']['signAndEditExecutable'] = False
    settings['npmRebuild'] = False
    settings['extends'] = None
    spec = build/'electron-review.json'
    spec.write_text(json.dumps(settings),encoding='utf8')
    node = Path('C:/Users/00818/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe')
    with (build/'electron-build.log').open('w',encoding='utf8') as log:
        subprocess.run([str(node),str(repo/'scripts/table_qa/package_simple_desktop.cjs'),str(spec)],cwd=repo,
            stdout=log,stderr=subprocess.STDOUT,check=True,env={**os.environ,'CSC_IDENTITY_AUTO_DISCOVERY':'false'})
    result = {'build':str(build),'output':str(output),'executable':str(output/'win-unpacked/PaperRead.exe')}
    (repo/'tmp/simple-delivery-build.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps(result),flush=True)


if __name__=='__main__':
    main()
