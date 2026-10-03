#!/usr/bin/python3
"""Relay a native Tailscale-authenticated shell into the inner coder account."""
import ipaddress
import os
import sys
from pathlib import Path

target = str(ipaddress.ip_address(Path('/run/relay/target').read_text().strip()))
args = ['ssh', '-F', '/dev/null', '-i', '/run/relay/private/client',
        '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=yes', '-o', 'HostKeyAlias=workspace-inner',
        '-o', 'UserKnownHostsFile=/run/relay/known_hosts', '-o', 'ConnectTimeout=15',
        '-p', '2222']
if os.isatty(0):
    args.append('-tt')
args.append('coder@' + target)
if '-c' in sys.argv[1:]:
    index = sys.argv.index('-c')
    if index + 1 != len(sys.argv) - 1:
        raise SystemExit('Unsupported login-shell arguments')
    args.append(sys.argv[index + 1])
os.execvp(args[0], args)
