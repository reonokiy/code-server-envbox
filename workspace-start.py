#!/usr/bin/python3
"""Start services inside the Sysbox workspace, with no cluster credentials."""
import os
import signal
import subprocess
import time
from pathlib import Path

if os.geteuid() != 0:
    raise SystemExit('Workspace startup requires inner root')
Path('/run/sshd').mkdir(exist_ok=True)
subprocess.run(['ssh-keygen', '-A'], check=True, stdout=subprocess.DEVNULL)
# System changes can be reproduced by a user-owned startup definition.
startup = Path('/home/coder/.config/workspace/startup.sh')
if startup.is_file():
    with open('/tmp/workspace-startup.log', 'wb') as log:
        subprocess.run(['/bin/bash', str(startup)], check=True, stdout=log, stderr=log)
commands = [
    ['dockerd'],
    ['/usr/sbin/sshd', '-D', '-e', '-f', '/etc/ssh/sshd_workspace_config'],
    ['runuser', '-u', 'coder', '--', '/usr/bin/code-server', '--bind-addr', '0.0.0.0:8080',
     '--auth', 'none', '--disable-telemetry', '/home/coder'],
]
processes = []
logs = []
stopping = False

def stop(signum, frame):
    global stopping
    stopping = True

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
try:
    for name, command in zip(['docker', 'ssh', 'code-server'], commands):
        log = open('/tmp/workspace-' + name + '.log', 'ab')
        logs.append(log)
        processes.append(subprocess.Popen(command, stdout=log, stderr=log,
                                          env={**os.environ, 'HOME': '/home/coder'}))
    while not stopping and all(p.poll() is None for p in processes):
        time.sleep(1)
    if not stopping:
        raise RuntimeError('A workspace service exited')
finally:
    for p in processes:
        if p.poll() is None:
            p.terminate()
    for p in processes:
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
    for log in logs:
        log.close()
