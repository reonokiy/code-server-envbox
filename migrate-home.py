#!/usr/bin/python3
"""Preserve the legacy PVC root and atomically prepare the shifted workspace."""
import os
import shutil
import stat
import subprocess
from pathlib import Path

root = Path('/volume')
layout = root / '.envbox'
if layout.is_symlink() or (layout.exists() and (not layout.is_dir() or layout.stat().st_uid != 0)):
    raise SystemExit('Refusing an untrusted workspace layout directory')
layout.mkdir(mode=0o700, exist_ok=True)
marker = layout / 'home.completed'
if not marker.exists():
    staging = layout / 'home.staging'
    if staging.exists():
        if staging.is_symlink():
            raise SystemExit('Refusing a symlink at the migration staging path')
        shutil.rmtree(staging)
    # Refuse a partial layout rather than merging it into retained user data.
    if (layout / 'home').exists():
        raise SystemExit('Workspace home exists without a migration marker')
    total = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        if Path(directory) == root:
            dirs[:] = [d for d in dirs if d != '.envbox']
        for name in files:
            s = (Path(directory) / name).lstat()
            if stat.S_ISREG(s.st_mode):
                total += s.st_size
    available = shutil.disk_usage(root).free
    if available < total + 2 * 1024**3:
        raise SystemExit('Insufficient space for a recoverable home copy')
    def ignore(directory, names):
        # Stopped processes' UNIX sockets contain no persistent file data.
        ignored = ['.envbox'] if Path(directory) == root else []
        return ignored + [name for name in names
                          if stat.S_ISSOCK((Path(directory) / name).lstat().st_mode)]
    shutil.copytree(root, staging, symlinks=True, ignore=ignore)
    for directory, dirs, files in os.walk(staging, followlinks=False):
        for path in [Path(directory), *(Path(directory) / name for name in dirs + files)]:
            s = path.lstat()
            uid = s.st_uid + 100000 if s.st_uid < 65536 else s.st_uid
            gid = s.st_gid + 100000 if s.st_gid < 65536 else s.st_gid
            os.chown(path, uid, gid, follow_symlinks=False)
    os.chown(staging, 101000, 101000)
    staging.rename(layout / 'home')
    marker.write_text('v1\n')
for name in ['outer-docker', 'inner', 'sysbox']:
    (layout / name).mkdir(mode=0o700, exist_ok=True)

# Ephemeral relay credentials are generated and consumed only inside the Pod.
relay = Path('/run/relay')
relay.mkdir(exist_ok=True)
private, public = relay / 'private', relay / 'public'
private.mkdir(mode=0o700, exist_ok=True)
public.mkdir(mode=0o755, exist_ok=True)
key = private / 'client'
if not key.exists():
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
shutil.copyfile(str(key) + '.pub', public / 'authorized_key')
os.chown(private, 101000, 101000)
os.chown(key, 101000, 101000)
os.chmod(key, 0o600)
os.chmod(public / 'authorized_key', 0o644)
runtime = Path('/var/run/tailscale')
if runtime.is_dir():
    os.chown(runtime, 101000, 101000)
    os.chmod(runtime, 0o700)
print('Workspace copy prepared; legacy home retained')
