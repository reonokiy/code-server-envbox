#!/usr/bin/python3
"""Prepare systemd services and request graceful inner shutdown on Pod exit."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

if os.geteuid() != 0:
    raise SystemExit('Workspace startup requires inner root')

if sys.argv[1:] == ['--prepare']:
    Path('/run/sshd').mkdir(exist_ok=True)
    subprocess.run(['ssh-keygen', '-A'], check=True, stdout=subprocess.DEVNULL)
elif sys.argv[1:] == ['--startup']:
    startup = Path('/home/coder/.config/workspace/startup.sh')
    if startup.is_file():
        subprocess.run(['/bin/bash', str(startup)], check=True)
elif not sys.argv[1:]:
    if Path('/proc/1/comm').read_text().strip() != 'systemd':
        raise SystemExit('Workspace requires systemd as PID 1')
    for _ in range(90):
        result = subprocess.run(['systemctl', 'start', 'workspace.target'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            break
        time.sleep(1)
    else:
        raise SystemExit('Workspace systemd target did not start')
    stopping = False
    def stop(signum, frame):
        global stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        time.sleep(1)
    subprocess.run(['systemctl', 'poweroff', '--no-block'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
else:
    raise SystemExit('Unsupported workspace startup operation')
