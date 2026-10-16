#!/usr/bin/env python3
"""Install the skill as a symlink without overwriting an existing skill."""
import argparse
import os
from pathlib import Path

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--destination', default=str(Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex')))/'skills'))
a = p.parse_args()
source = Path(__file__).resolve().parent/'skills/codex-chat-organizer'
target = Path(a.destination).expanduser()/'codex-chat-organizer'
if target.is_symlink() and target.resolve() == source:
    print('Already installed: '+str(target))
elif target.exists() or target.is_symlink():
    p.error('Existing skill preserved; choose another destination: '+str(target))
else:
    target.parent.mkdir(parents=True,exist_ok=True)
    target.symlink_to(source,target_is_directory=True)
    print('Installed: '+str(target))
