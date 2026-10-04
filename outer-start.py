#!/usr/bin/python3
"""Publish safe listener metadata and relay Web/UDP into the workspace."""
import ipaddress
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from tailnet_outbound import TailnetOutbound

relay = Path('/run/relay')
process = subprocess.Popen(['/envbox', 'docker'])
forward = None
previous = None
stopping = False
udp_forwards = {}
outbound = TailnetOutbound()

def stop(signum, frame):
    global stopping
    stopping = True

def publish(name, value):
    temporary = relay / (name + '.new')
    temporary.write_text(value)
    temporary.chmod(0o644)
    temporary.replace(relay / name)

def inventory(target):
    # Docker's iptables publishing may not appear in the listening sockets.
    sockets = subprocess.run(['docker', 'exec', 'workspace_cvm', 'ss', '-H', '-lntu'],
                             capture_output=True, text=True, timeout=10)
    if sockets.returncode:
        return
    ports = {'tcp': set(), 'udp': set()}
    for line in sockets.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0] not in ports:
            continue
        address, port = fields[4].rsplit(':', 1)
        address = address.strip('[]')
        if address in ('*', '0.0.0.0', '::', target) and port.isdigit():
            ports[fields[0]].add(int(port))
    ids = subprocess.run(['docker', 'exec', 'workspace_cvm', 'docker', 'ps', '-q'],
                         capture_output=True, text=True, timeout=10)
    if ids.returncode == 0 and ids.stdout.strip():
        mappings = subprocess.run(['docker', 'exec', 'workspace_cvm', 'docker', 'inspect',
                                   '--format', '{{json .NetworkSettings.Ports}}',
                                   *ids.stdout.split()], capture_output=True, text=True, timeout=10)
        if mappings.returncode == 0:
            for line in mappings.stdout.splitlines():
                for container_port, bindings in (json.loads(line) or {}).items():
                    protocol = container_port.rsplit('/', 1)[-1]
                    if protocol in ports:
                        for binding in bindings or []:
                            if binding['HostIp'] in ('', '0.0.0.0', '::', target):
                                ports[protocol].add(int(binding['HostPort']))
    ports['tcp'].difference_update((22, 443, 8080, 2222))
    # Tailscale's existing WireGuard transport owns this UDP port.
    ports['udp'].discard(41641)
    wanted = {(target, port) for port in ports['udp']}
    for key, child in list(udp_forwards.items()):
        if key not in wanted or child.poll() is not None:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=10)
            del udp_forwards[key]
    for key in wanted - udp_forwards.keys():
        host, port = key
        udp_forwards[key] = subprocess.Popen([
            'socat', '-T', '60', f'UDP4-LISTEN:{port},bind=127.0.0.1,fork,reuseaddr',
            f'UDP4:{host}:{port}'], start_new_session=True)
    publish('ports.json', json.dumps({'target': target, **{
        protocol: sorted(values) for protocol, values in ports.items()}}) + '\n')

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
try:
    while not stopping and process.poll() is None:
        info = subprocess.run(['docker', 'inspect', '--format',
                               '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}',
                               'workspace_cvm'], capture_output=True, text=True)
        if info.returncode == 0 and info.stdout.strip():
            target = str(ipaddress.ip_address(info.stdout.strip()))
            outbound.update(target)
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
                try:
                    inventory(target)
                except (subprocess.TimeoutExpired, ValueError):
                    # Workspace/Docker restarts must not kill Envbox itself.
                    pass
        time.sleep(2)
    if not stopping:
        raise RuntimeError('Envbox exited')
finally:
    outbound.close()
    for child in udp_forwards.values():
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=10)
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
