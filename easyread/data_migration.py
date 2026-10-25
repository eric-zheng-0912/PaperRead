"""Explicit copy/verify migration. Run only with all source applications closed.

No config import, credential output, source deletes, or automatic conflict merge.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid
import sys

from .store import dir_lock, read_json, write_json_atomic

HOME_ITEMS = ('config.json', 'prefs.json', 'limits.json', '.models-seen.json', '.research-os')


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''): h.update(block)
    return h.hexdigest()


def plain(path):
    path = Path(path).absolute()
    for p in (path, *path.parents):
        if p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()):
            raise ValueError('Links/junctions are not allowed in migration paths')
    return path.resolve()


def inventory(source_home, library=None, profile=None):
    home = plain(source_home)
    known = set(HOME_ITEMS) | {'library','resources','locales','.server.json','easyread.log','update.json',
                              'package.json','LICENSE','LICENSE.electron.txt','LICENSES.chromium.html'}
    for entry in home.iterdir():
        # Unknown folders/documents might be user data: require review rather than silently omit.
        program = entry.is_file() and entry.suffix.lower() in ('.exe','.dll','.pak','.bin','.dat')
        runtime = entry.is_file() and (entry.name.endswith('.lock') or entry.name.startswith('easyread.log.'))
        if entry.name not in known and not program and not runtime:
            raise ValueError('Unknown top-level source data; review selection before migration')
    selected = [(home/name, Path('home')/name) for name in HOME_ITEMS if (home/name).exists()]
    cfg = read_json(home/'config.json', {})
    configured = Path(cfg.get('library_dir') or home/'library')
    if not configured.is_absolute(): configured = home/configured
    lib = plain(library or home/'library')
    if cfg.get('library_dir') and plain(configured) != lib:
        raise ValueError('Configured external library requires an explicit matching --library')
    if lib.exists(): selected.append((lib, Path('library')))
    if profile: selected.append((plain(profile), Path('desktop')))
    records = {}
    for source, relative in selected:
        plain(source)
        paths = [source] if source.is_file() else sorted(source.rglob('*'))
        for file in paths:
            plain(file)
            if not file.is_file(): continue
            # OS lock files are process state, not user content. Never unlink a source lock.
            if file.name.endswith('.lock') or file.name in ('SingletonLock','SingletonCookie','SingletonSocket'): continue
            if file.suffix.lower() in ('.exe','.dll','.pyd','.asar'):
                raise ValueError('Executable/application resources found in selected user data')
            rel = relative if source.is_file() else relative/file.relative_to(source)
            records[rel.as_posix()] = {'source':str(file), 'sha256':digest(file), 'size':file.stat().st_size}
    if not records: raise ValueError('No persistent source data found')
    return records


def copy_verified(source, target, expected):
    plain(target)
    if target.exists():
        if not target.is_file() or digest(target) != expected:
            raise ValueError('Conflicting destination; no overwrite performed')
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    # Partial temp copies can be replaced on retry, never final user files.
    temp = target.with_name(target.name+'.migration-partial-'+uuid.uuid4().hex)
    plain(temp)
    if temp.exists() and (not temp.is_file() or digest(temp) != expected):
        raise ValueError('Conflicting partial copy; preserve it for manual review')
    if not temp.exists():
        with source.open('rb') as src, temp.open('xb') as dst:
            shutil.copyfileobj(src, dst); dst.flush(); os.fsync(dst.fileno())
    if digest(temp) != expected or digest(source) != expected:
        raise ValueError('Source changed or copy verification failed')
    # Atomic exclusive publication (NTFS/POSIX); never replace a final user file.
    os.link(temp, target)
    temp.unlink()
    if digest(target) != expected: raise ValueError('Destination verification failed')


def migrate(source_home, destination, backup, *, library=None, profile=None, confirm_closed=False, checkpoint=None):
    if not confirm_closed: raise ValueError('Close all source applications and explicitly confirm first')
    source_home, destination, backup = map(plain, (source_home,destination,backup))
    installation = os.environ.get('PAPERREAD_INSTALL_DIR') or (str(Path(sys.executable).resolve().parents[2]) if getattr(sys,'frozen',False) else None)
    if installation:
        from .data_paths import outside
        outside(destination,installation); outside(backup,installation)
    roots = [source_home] + ([plain(library)] if library else []) + ([plain(profile)] if profile else [])
    for a in (destination, backup):
        for b in roots + ([backup] if a == destination else [destination]):
            if a == b or a.is_relative_to(b) or b.is_relative_to(a):
                raise ValueError('Source, backup and destination must be disjoint')
    destination.mkdir(parents=True,exist_ok=True)
    with dir_lock(destination, '.migration.lock'):
        plain(destination/'migration.json'); plain(backup/'manifest.json')
        records = inventory(source_home, library, profile)
        plan = {'schema':1, 'source_home':str(source_home), 'backup':str(backup), 'files':records}
        old = read_json(destination/'migration.json', None)
        if old and old.get('plan') != plan: raise ValueError('Migration plan changed; manual review required')
        if backup.exists() and any(backup.iterdir()) and not old:
            raise ValueError('Use a new backup directory; existing backups must remain unchanged')
        if old and old.get('phase') == 'complete':
            for rel, entry in records.items():
                expected = old['destination_hashes'][rel]
                if digest(destination/rel) != expected or digest(backup/rel) != entry['sha256']:
                    raise ValueError('Completed migration changed; do not overwrite live data')
            return {'phase':'complete','files':len(records),'resumed':True}
        # Derive only the library path/update policy; preserve every other config field/key.
        cfg = read_json(source_home/'config.json', {})
        cfg.update(library_dir=str(destination/'library'), check_updates=False)
        encoded = (json.dumps(cfg,ensure_ascii=False,indent=2)+'\n').encode('utf8')
        cfg_hash = hashlib.sha256(encoded).hexdigest()
        # Detect conflicts before copying anything to the final home/profile.
        for rel, entry in records.items():
            target = destination/rel
            plain(target)
            if target.exists() and digest(target) != (cfg_hash if rel=='home/config.json' else entry['sha256']):
                raise ValueError('Conflicting user data; no automatic merge or overwrite')
        if 'home/config.json' not in records and (destination/'home/config.json').exists():
            raise ValueError('Destination already has a configuration')
        write_json_atomic(destination/'migration.json', {'phase':'backup','plan':plan})
        for rel, entry in records.items(): copy_verified(Path(entry['source']), backup/rel, entry['sha256'])
        # Verify the entire backup before any destination user data is copied.
        for rel, entry in records.items():
            if digest(backup/rel) != entry['sha256']: raise ValueError('Backup verification failed')
        manifest = backup/'manifest.json'
        if manifest.exists() and read_json(manifest) != plan: raise ValueError('Conflicting backup manifest')
        write_json_atomic(manifest, plan)
        write_json_atomic(destination/'migration.json', {'phase':'copy','plan':plan})
        for rel, entry in records.items():
            if rel == 'home/config.json': continue
            copy_verified(backup/rel, destination/rel, entry['sha256'])
            if checkpoint: checkpoint(rel)
        if inventory(source_home,library,profile) != records: raise ValueError('Source changed; switch refused')
        hashes = {rel:digest(destination/rel) for rel in records if rel!='home/config.json'}
        for rel, value in hashes.items():
            if value != records[rel]['sha256']: raise ValueError('Final verification failed')
        target = destination/'home/config.json'; target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists():
            temp = target.with_name('config.migration-partial-'+uuid.uuid4().hex)
            with temp.open('xb') as stream: stream.write(encoded); stream.flush(); os.fsync(stream.fileno())
            os.link(temp,target)
            temp.unlink()
        if digest(target) != cfg_hash: raise ValueError('Configuration conflict; switch refused')
        hashes['home/config.json'] = cfg_hash
        write_json_atomic(destination/'migration.json', {'phase':'complete','plan':plan,'destination_hashes':hashes})
        return {'phase':'complete','files':len(records),'resumed':False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('home','destination','backup'): parser.add_argument('--'+arg,required=True)
    for arg in ('library','profile'): parser.add_argument('--'+arg)
    parser.add_argument('--confirm-closed',action='store_true')
    args=parser.parse_args(argv)
    print(json.dumps(migrate(args.home,args.destination,args.backup,library=args.library,profile=args.profile,confirm_closed=args.confirm_closed)))


if __name__ == '__main__': main()
