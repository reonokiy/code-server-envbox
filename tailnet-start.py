#!/usr/bin/python3
"""Keep native Tailscale Serve in sync with public workspace TCP listeners."""
import ipaddress
import json
import os
import signal
import subprocess
import time
from pathlib import Path

base_path = Path(os.environ['TS_SERVE_CONFIG'])
effective = Path('/var/run/tailscale/serve-workspace.json')
metadata = Path('/run/relay/ports.json')
stopping = False
previous = None


def stop(signum, frame):
    global stopping
    stopping = True


def update():
    global previous
    config = json.loads(base_path.read_text())
    try:
        ports = json.loads(metadata.read_text())
        target = str(ipaddress.IPv4Address(ports['target']))
        tcp = config.setdefault('TCP', {})
        for port in ports['tcp']:
            if type(port) is int and 1 <= port <= 65535 and port not in (22, 2222, 443, 8080):
                tcp[str(port)] = {'TCPForward': f'{target}:{port}'}
    except (FileNotFoundError, ValueError, KeyError):
        pass
    value = json.dumps(config, sort_keys=True) + '\n'
    if value != previous:
        temporary = effective.with_suffix('.new')
        temporary.write_text(value)
        temporary.replace(effective)
        previous = value


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
update()
env = dict(os.environ, TS_SERVE_CONFIG=str(effective))
process = subprocess.Popen(['/usr/local/bin/containerboot'], env=env)
try:
    while not stopping and process.poll() is None:
        update()
        time.sleep(1)
finally:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
raise SystemExit(process.returncode)
