"""Custom distribution paths. No discovery writes or automatic data migration."""
import os
import sys
from pathlib import Path


def data_root(platform=None, env=None, home=None):
    platform, env, home = platform or sys.platform, os.environ if env is None else env, Path(home or Path.home())
    if platform == 'win32':
        return Path(env.get('LOCALAPPDATA') or home/'AppData'/'Local')/'PaperRead-Custom'
    if platform == 'darwin':
        return home/'Library'/'Application Support'/'PaperRead-Custom'
    return Path(env.get('XDG_DATA_HOME') or home/'.local'/'share')/'paperread-custom'


def outside(path, installation):
    path, installation = Path(path).resolve(), Path(installation).resolve()
    if path == installation or path.is_relative_to(installation) or installation.is_relative_to(path):
        raise ValueError('User data and installation directories must not overlap')
    return path
