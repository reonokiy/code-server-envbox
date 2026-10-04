"""Transparent workspace TCP to the existing userspace Tailscale SOCKS daemon."""
import ipaddress
import subprocess
from pathlib import Path


class TailnetOutbound:
    chain = 'WORKSPACE_TAILNET'

    def __init__(self):
        self.process = None
        self.target = None
        self.jumps = []

    @staticmethod
    def iptables(*args, required=True):
        result = subprocess.run(['iptables', '-w', '5', '-t', 'nat', *args],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if required and result.returncode:
            raise RuntimeError('Workspace Tailnet routing operation failed')
        return result.returncode == 0

    def update(self, target):
        if self.target == target and self.process is not None and self.process.poll() is None:
            return
        self.close()
        result = subprocess.run(['docker', 'inspect', '--format',
                                 '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}',
                                 'workspace_cvm'], capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise RuntimeError('Workspace bridge unavailable')
        gateway = str(ipaddress.IPv4Address(result.stdout.strip()))
        target = str(ipaddress.IPv4Address(target))
        # Listen only on the private Docker bridge, never the Pod/public address.
        config = Path('/run/workspace-tailnet.conf')
        config.write_text('base { log_debug = off; log_info = off; log = stderr; '
                          'daemon = off; redirector = iptables; }\n'
                          'redsocks { local_ip = ' + gateway + '; local_port = 12345; '
                          'ip = 127.0.0.1; port = 1055; type = socks5; }\n')
        self.process = subprocess.Popen(['redsocks', '-c', str(config)],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.iptables('-N', self.chain, required=False)
        self.iptables('-F', self.chain)
        self.iptables('-A', self.chain, '-p', 'tcp', '-j', 'DNAT', '--to-destination', gateway + ':12345')
        # Inner Docker workloads are already masqueraded to the workspace IP.
        self.jumps = [
            ['PREROUTING', '-s', target + '/32', '-d', '100.64.0.0/10', '-p', 'tcp', '-j', self.chain],
            ['OUTPUT', '-d', '100.64.0.0/10', '-p', 'tcp', '-j', self.chain],
        ]
        for jump in self.jumps:
            if not self.iptables('-C', *jump, required=False):
                self.iptables('-I', jump[0], '1', *jump[1:])
        self.target = target

    def close(self):
        for jump in self.jumps:
            self.iptables('-D', *jump, required=False)
        self.jumps = []
        self.iptables('-F', self.chain, required=False)
        self.iptables('-X', self.chain, required=False)
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)
        self.process = None
        self.target = None
