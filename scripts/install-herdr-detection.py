#!/usr/bin/env python3
"""Install a transparent launcher for a user-local Herdr, preserving its binary."""
import os
from pathlib import Path
import shutil
import sys

prefix=Path(sys.argv[1])
command=shutil.which('herdr')
if not command:
    print('Herdr not installed; skipping automatic terminal registration.')
    raise SystemExit(0)
entry=Path(command)
marker='# PhoneMic automatic terminal registration'
with entry.open('rb') as f:
    if marker.encode() in f.read(256):
        raise SystemExit(0)
if entry.is_symlink() or entry.parent != Path.home()/'.local/bin' or entry.stat().st_uid != os.getuid():
    print('Automatic Herdr registration requires a user-local ~/.local/bin/herdr installation.')
    raise SystemExit(0)
original=prefix/'herdr-bin/herdr'
original.parent.mkdir(parents=True,exist_ok=True)
# Keep the original binary intact. Herdr updates continue to update this binary.
shutil.copy2(entry,original)
launcher='#!/usr/bin/python3\n'+marker+'\nimport runpy\nrunpy.run_path('+repr(str(prefix/'lib/herdr_detect.py'))+',run_name="__main__")\n'
temp=entry.with_name('.herdr-phonemic-launcher')
temp.write_text(launcher);temp.chmod(0o755);temp.replace(entry)
print('Automatic Herdr launch detection installed; original binary preserved.')
