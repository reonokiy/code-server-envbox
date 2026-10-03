#!/usr/bin/python3
"""Forward Web traffic and publish only SSH target/public-key metadata."""
import ipaddress
import os
import signal
import subprocess
import time
from pathlib import Path

relay = Path('/run/relay')
process = subprocess.Popen(['/envbox', 'docker'])
forward = None
previous = None
stopping = False

def stop(signum, frame):
    global stopping
    stopping = True

def publish(name, value):
    temporary = relay / (name + '.new')
    temporary.write_text(value)
    temporary.chmod(0o644)
    temporary.replace(relay / name)

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
try:
    while not stopping and process.poll() is None:
        info = subprocess.run(['docker', 'inspect', '--format',
                               '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}',
                               'workspace_cvm'], capture_output=True, text=True)
        if info.returncode == 0 and info.stdout.strip():
            target = str(ipaddress.ip_address(info.stdout.strip()))
            key = subprocess.run(['docker', 'exec', 'workspace_cvm', 'cat',
                                  '/etc/ssh/ssh_host_ed25519_key.pub'], capture_output=True, text=True)
            if key.returncode == 0 and key.stdout.startswith('ssh-ed25519 '):
                publish('known_hosts', 'workspace-inner ' + key.stdout.strip() + '\n')
                publish('target', target + '\n')
                if target != previous or forward is None or forward.poll() is not None:
                    if forward is not None and forward.poll() is None:
                        forward.terminate()
                        forward.wait(timeout=10)
                    forward = subprocess.Popen(['socat', 'TCP-LISTEN:8080,fork,reuseaddr',
                                                'TCP:' + target + ':8080'])
                    previous = target
        time.sleep(2)
    if not stopping:
        raise RuntimeError('Envbox exited')
finally:
    if forward is not None and forward.poll() is None:
        forward.terminate()
        forward.wait(timeout=10)
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=45)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
