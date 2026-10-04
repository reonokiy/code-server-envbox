"""Disposable Envbox + native Headscale/Tailscale SSH/SFTP integration."""
import io
import json
import tarfile
import time
from pathlib import Path
import docker

client = docker.from_env()
fixtures = Path(__file__).resolve().parent
network = client.networks.create('code-envbox-integration')
containers, volumes = [], []

def run(name, image, **kwargs):
    if 'network_mode' not in kwargs:
        kwargs['network'] = network.name
    c = client.containers.run(image, name=name, detach=True, **kwargs)
    containers.append(c)
    return c

def execute(c, args, **kwargs):
    r = c.exec_run(args, **kwargs)
    if r.exit_code:
        raise RuntimeError('Synthetic fixture operation failed: ' + args[0])
    return r.output

def put(c, directory, name, data, mode=0o644):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as t:
        item = tarfile.TarInfo(name)
        item.size, item.mode = len(data), mode
        t.addfile(item, io.BytesIO(data))
    assert c.put_archive(directory, buf.getvalue())

def subpath(volume, target, path, readonly=False):
    m = docker.types.Mount(target, volume.name, type='volume', read_only=readonly)
    m['VolumeOptions'] = {'Subpath': path}
    return m

def wait_state(c, socket='/tmp/tailscaled.sock'):
    for _ in range(90):
        r = c.exec_run(['tailscale', '--socket=' + socket, 'status', '--json'])
        if r.exit_code == 0:
            state = json.loads(r.output)
            if state.get('BackendState') == 'Running':
                return state['TailscaleIPs'][0]
        time.sleep(1)
    raise RuntimeError('Synthetic tailnet did not enroll')

try:
    registry = run('code-test-registry', 'registry:3', ports={'5000/tcp': ('127.0.0.1', 15002)})
    time.sleep(2)
    client.images.get('code-server-workspace:test').tag('localhost:15002/workspace', 'test')
    for event in client.images.push('localhost:15002/workspace', tag='test', stream=True, decode=True):
        if 'error' in event:
            raise RuntimeError('Synthetic registry push failed')
    home, relay, data, runtime = [client.volumes.create(name='code-envbox-test-' + n)
                                 for n in ['home', 'relay', 'data', 'runtime']]
    volumes.extend([home, relay, data, runtime])
    seed = run('code-envbox-seed', 'code-server-tailnet:test',
               entrypoint='sh', command=['-c', 'printf retained > /home/coder/legacy-proof; sleep infinity'],
               volumes={home.name: {'bind': '/home/coder', 'mode': 'rw'}})
    time.sleep(1)
    assert execute(seed, ['cat', '/home/coder/legacy-proof']) == b'retained'
    execute(seed, ['sh', '-c', 'mkdir -p /home/coder/.local /home/coder/.cache; '
             'chown -R 0:0 /home/coder/.local /home/coder/.cache; '
             'chmod 700 /home/coder/.local /home/coder/.cache'], user='0:0')
    seed.stop()
    init = run('code-envbox-init', 'code-server-envbox:test', command=['/usr/local/bin/migrate-home'],
               user='0:0', cap_drop=['ALL'], cap_add=['CHOWN', 'DAC_OVERRIDE', 'FOWNER'],
               security_opt=['no-new-privileges:true'],
               volumes={home.name: {'bind': '/volume', 'mode': 'rw'},
                        relay.name: {'bind': '/run/relay', 'mode': 'rw'},
                        runtime.name: {'bind': '/var/run/tailscale', 'mode': 'rw'}})
    assert init.wait(timeout=60)['StatusCode'] == 0, 'Recoverable migration failed'
    mounts = [subpath(home, '/home/coder', '.envbox/home'),
              subpath(home, '/var/lib/docker', '.envbox/outer-docker'),
              subpath(home, '/var/lib/coder', '.envbox/inner'),
              subpath(home, '/var/lib/sysbox', '.envbox/sysbox'),
              docker.types.Mount('/run/relay', relay.name, type='volume'),
              docker.types.Mount('/data', data.name, type='volume')]
    outer = run('code-envbox-outer', 'code-server-envbox:test', privileged=True,
                command=['sh', '-c', 'sleep infinity'], mounts=mounts,
                environment={'CODER_INNER_IMAGE': 'code-test-registry:5000/workspace:test',
                             'CODER_INNER_USERNAME': 'coder',
                             'CODER_INNER_HOSTNAME': 'code.nokiy.net',
                             'CODER_MOUNTS': '/home/coder:/home/coder,/data:/data:managed,/run/relay/public:/run/relay/public:managed-ro',
                             'CODER_BOOTSTRAP_SCRIPT': 'exec sudo -n /usr/local/bin/workspace-start'})
    # Anonymous HTTP registry is confined to this disposable synthetic fixture.
    execute(outer, ['python3', '-c', 'import json; p="/etc/docker/daemon.json"; d=json.load(open(p)); d["insecure-registries"]=["code-test-registry:5000"]; json.dump(d,open(p,"w"))'])
    execute(outer, ['sh', '-c', 'chown 100000:100000 /data; chmod 770 /data; /usr/local/bin/outer-start >/tmp/outer.log 2>&1 &'])
    for _ in range(180):
        r = outer.exec_run(['curl', '-fsS', 'http://127.0.0.1:8080/healthz'])
        if r.exit_code == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError('Integrated workspace did not become ready')
    assert execute(outer, ['docker', 'exec', 'workspace_cvm', 'cat', '/proc/1/comm']).strip() == b'systemd'
    assert execute(outer, ['docker', 'exec', 'workspace_cvm', 'hostname']).strip() == b'code.nokiy.net'
    execute(outer, ['docker', 'exec', 'workspace_cvm', 'systemctl', 'is-active', '--quiet',
                    'docker.service', 'workspace-ssh.service', 'code-server.service'])
    execute(outer, ['docker', 'exec', 'workspace_cvm', 'systemd-run', '--quiet', '--wait',
                    '--unit=workspace-systemd-proof', '/usr/bin/touch', '/tmp/systemd-proof'])
    execute(outer, ['docker', 'exec', 'workspace_cvm', 'test', '-f', '/tmp/systemd-proof'])
    execute(outer, ['docker', 'exec', 'workspace_cvm', 'systemctl', 'restart', 'workspace-ssh.service'])
    failed_units = execute(outer, ['docker', 'exec', 'workspace_cvm', 'systemctl', '--failed',
                                   '--no-legend', '--no-pager']).strip()
    assert not failed_units, failed_units.decode()
    print('PASS: real systemd PID 1, fixed FQDN, service control, transient service and no failed units', flush=True)
    control = run('code-test-control', 'ghcr.io/juanfont/headscale:v0.29.4@sha256:8833f828b414c0907b7e5c71da76473216fe17cce0818a166b536ec552c0903f', command=['serve'], volumes={
        str(fixtures / 'headscale.yaml'): {'bind': '/etc/headscale/config.yaml', 'mode': 'ro'},
        str(fixtures / 'policy.hujson'): {'bind': '/etc/headscale/policy.hujson', 'mode': 'ro'}})
    time.sleep(3)
    users = {name: json.loads(execute(control, ['/ko-app/headscale', 'users', 'create', name, '-o', 'json']))['id']
             for name in ['operator', 'denied']}
    def node(name, user, server=False, kernel=False, tag=None):
        args = ['/ko-app/headscale', 'preauthkeys', 'create', '--user', str(users[user]), '--expiration', '5m', '-o', 'json']
        if server or tag:
            args += ['--tags', tag or 'tag:code-server']
        key = json.loads(execute(control, args))['key']
        env = {'TS_AUTHKEY': key, 'TS_HOSTNAME': name, 'TS_KUBE_SECRET': '', 'TS_USERSPACE': 'true',
               'TS_STATE_DIR': '/home/coder/.local/state/tailscale' if server else '/tmp/tailscale-state',
               'TS_SOCKET': '/tmp/tailscaled.sock', 'TS_ACCEPT_DNS': 'false', 'TS_AUTH_ONCE': 'true',
               'TS_EXTRA_ARGS': '--login-server=http://code-test-control:8080' + (' --ssh' if server else '')}
        env['TS_SOCKS5_SERVER'] = '127.0.0.1:1055'
        options = {'user': '1000:1000', 'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true']}
        image = 'code-server-tailnet:test'
        if server:
            image = 'code-server-relay:test'
            options.update(user='101000:101000', group_add=['100000'], network_mode='container:' + outer.id,
                           mounts=[subpath(home, '/home/coder', '.envbox/home'),
                                   docker.types.Mount('/run/relay', relay.name, type='volume', read_only=True),
                                   docker.types.Mount('/var/run/tailscale', runtime.name, type='volume'),
                                   docker.types.Mount('/data', data.name, type='volume')])
            options['volumes'] = {str(fixtures / 'serve.json'): {'bind': '/etc/tailscale/serve.json', 'mode': 'ro'}}
            env['TS_SERVE_CONFIG'] = '/etc/tailscale/serve.json'
            env['TS_SOCKET'] = '/var/run/tailscale/tailscaled.sock'
        entrypoint = '/usr/local/bin/tailnet-start' if server else '/usr/local/bin/containerboot'
        if kernel:
            env['TS_USERSPACE'] = 'false'
            options = {'user': '0:0', 'privileged': True,
                       'command': ['-c', 'mkdir -p /dev/net; test -e /dev/net/tun || mknod /dev/net/tun c 10 200; exec /usr/local/bin/containerboot']}
            entrypoint = 'sh'
        c = run(name, image, entrypoint=entrypoint, environment=env, **options)
        del key
        return c, wait_state(c, env['TS_SOCKET'])
    server, address = node('code-test-workspace', 'operator', True)
    operator, _ = node('code-test-operator', 'operator')
    denied, _ = node('code-test-denied', 'denied')
    kernel, _ = node('code-test-kernel', 'operator', kernel=True)
    peer, peer_address = node('code-test-private-api', 'operator', tag='tag:api-internal')
    execute(peer, ['python3', '-c', 'import subprocess; subprocess.Popen(["python3","-m","http.server","8090","--bind","127.0.0.1"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)'])
    for port in ('443', '444'):
        execute(peer, ['tailscale', '--socket=/tmp/tailscaled.sock', 'serve', '--bg',
                       '--tcp=' + port, 'tcp://127.0.0.1:8090'])
    for _ in range(30):
        r = outer.exec_run(['docker', 'exec', 'workspace_cvm', 'runuser', '-u', 'coder', '--',
                           'curl', '-fsS', '--connect-timeout', '3', '--max-time', '5',
                           'http://' + peer_address + ':443/'])
        if r.exit_code == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError('Transparent workspace Tailnet TCP did not connect')
    assert b'Directory listing' in r.output
    denied_outbound = outer.exec_run(['docker', 'exec', 'workspace_cvm', 'curl', '-fsS',
                                     '--connect-timeout', '3', '--max-time', '5',
                                     'http://' + peer_address + ':444/'])
    assert denied_outbound.exit_code != 0, 'Unauthorized Tailnet port accepted'
    bridge = execute(outer, ['docker', 'inspect', '--format',
                             '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}',
                             'workspace_cvm']).decode().strip()
    execute(outer, ['python3', '-c', 'import subprocess; subprocess.Popen(["python3","-m","http.server","8091","--bind","' + bridge + '"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)'])
    time.sleep(1)
    direct = outer.exec_run(['docker', 'exec', 'workspace_cvm', 'curl', '-fsS',
                             '--max-time', '5', 'http://' + bridge + ':8091/'])
    assert direct.exit_code == 0, 'Non-Tailnet traffic was redirected'
    proxy = execute(outer, ['ss', '-H', '-lnt', 'sport = :12345'])
    assert b'0.0.0.0:12345' not in proxy and b'127.0.0.1:12345' not in proxy
    print('PASS: transparent inner Tailnet TCP, unauthorized port denied, direct traffic retained, proxy private bridge only', flush=True)
    # Start services after enrollment: no manual Serve/ACL changes per port.
    execute(outer, ['docker', 'exec', 'workspace_cvm', 'systemd-run', '--quiet',
                    '--unit=workspace-http-proof', '/usr/bin/python3', '-m', 'http.server',
                    '3000', '--bind', '0.0.0.0', '--directory', '/tmp'])
    udp_program = ('import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); '
                   's.bind(("0.0.0.0",53000));\nwhile True:\n d,a=s.recvfrom(4096); s.sendto(d,a)')
    execute(outer, ['docker', 'exec', 'workspace_cvm', 'systemd-run', '--quiet',
                    '--unit=workspace-udp-proof', '/usr/bin/python3', '-c', udp_program])
    for _ in range(30):
        r = operator.exec_run(['curl', '--max-time', '3', '-fsS', '--socks5-hostname',
                               '127.0.0.1:1055', f'http://{address}:3000/systemd-proof'])
        if r.exit_code == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError('Dynamic workspace TCP forwarding failed')
    udp_probe = ('import socket;\nfor n in range(2):\n '
                 's=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(5); '
                 f's.sendto(b"udp-proof",("{address}",53000)); '
                 'assert s.recv(4096)==b"udp-proof"; s.close()')
    execute(kernel, ['python3', '-c', udp_probe])
    r = denied.exec_run(['curl', '--max-time', '3', '-fsS', '--socks5-hostname',
                         '127.0.0.1:1055', f'http://{address}:3000/systemd-proof'])
    assert r.exit_code != 0, 'Other identity unexpectedly reached application port'
    execute(outer, ['docker', 'exec', 'workspace_cvm', 'systemctl', 'stop',
                    'workspace-http-proof', 'workspace-udp-proof'])
    time.sleep(5)
    config = json.loads(execute(server, ['cat', '/var/run/tailscale/serve-workspace.json']))
    assert '3000' not in config['TCP']
    print('PASS: automatic TCP/UDP forwarding, multiple UDP clients, identity deny and removal', flush=True)
    ssh = ['ssh', '-o', 'ProxyCommand=tailscale --socket=/tmp/tailscaled.sock nc %h %p',
           '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null']
    result = execute(operator, ['timeout', '30', *ssh, 'coder@' + address,
                               'id -u; command -v docker; sudo -n docker info --format "{{.ServerVersion}}"; printf shell > /home/coder/shell-proof'])
    assert b'1000' in result.splitlines() and b'/usr/bin/docker' in result
    execute(server, ['sh', '-c', 'test -w /home/coder/.local && test -w /home/coder/.cache'])
    assert execute(outer, ['docker', 'exec', 'workspace_cvm', 'cat', '/home/coder/shell-proof']) == b'shell'
    r = operator.exec_run(['timeout', '30', *ssh, 'coder@' + address, 'exit 37'])
    assert r.exit_code == 37
    r = operator.exec_run(['timeout', '30', *ssh, '-tt', 'coder@' + address, 'test -t 0 && printf tty'])
    assert r.exit_code == 0 and b'tty' in r.output
    print('PASS: native Tailnet ACL -> inner coder UID 1000, Docker, shared home, exact exit status and TTY', flush=True)
    execute(outer, ['docker', 'exec', 'workspace_cvm', 'pkg-config', '--exists',
                    'openssl', 'libffi', 'zlib', 'sqlite3', 'libpq', 'libpng', 'libxml-2.0'])
    print('PASS: system development libraries available for user-managed tools', flush=True)
    nested = execute(outer, ['docker', 'exec', 'workspace_cvm', 'runuser', '-u', 'coder', '--',
                            'sh', '-c', '''set -e
mkdir -p /tmp/docker-proof
printf 'int main(void) { return 0; }' | cc -static -x c -o /tmp/docker-proof/probe -
printf 'FROM scratch\nCOPY probe /probe\nENTRYPOINT ["/probe"]\n' > /tmp/docker-proof/Dockerfile
docker build -t workspace-proof /tmp/docker-proof >/tmp/docker-proof/build.log 2>&1
docker run --rm workspace-proof
docker compose version
'''])
    assert b'Docker Compose version' in nested
    print('PASS: inner coder builds and runs an actual Docker container', flush=True)
    put(outer, '/tmp', 'port-server.c', (fixtures / 'port-server.c').read_bytes())
    execute(outer, ['docker', 'cp', '/tmp/port-server.c', 'workspace_cvm:/tmp/docker-proof/port-server.c'])
    execute(outer, ['docker', 'exec', 'workspace_cvm', 'sh', '-c',
                    'cc -static /tmp/docker-proof/port-server.c -o /tmp/docker-proof/port-server; '
                    'printf \'FROM scratch\\nCOPY port-server /server\\nENTRYPOINT ["/server"]\\n\' > /tmp/docker-proof/Dockerfile; '
                    'docker build -t workspace-port-proof /tmp/docker-proof >/tmp/docker-proof/ports-build.log 2>&1; '
                    'docker run -d --name port-proof -p 13001:3001 -p 13002:3001/udp workspace-port-proof'])
    for _ in range(30):
        r = operator.exec_run(['curl', '--max-time', '3', '-fsS', '--socks5-hostname',
                               '127.0.0.1:1055', f'http://{address}:13001/'])
        if r.exit_code == 0 and r.output == b'docker-proof':
            break
        time.sleep(1)
    else:
        raise RuntimeError('Docker published TCP port did not forward')
    execute(kernel, ['python3', '-c', udp_probe.replace('53000', '13002')])
    execute(outer, ['docker', 'exec', 'workspace_cvm', 'docker', 'rm', '-f', 'port-proof'])
    time.sleep(5)
    config = json.loads(execute(server, ['cat', '/var/run/tailscale/serve-workspace.json']))
    assert '13001' not in config['TCP']
    print('PASS: actual Docker published TCP/UDP ports automatically added and removed', flush=True)
    execute(operator, ['sh', '-c', 'printf transfer > /tmp/upload'])
    put(operator, '/tmp', 'sftp.batch', b'put /tmp/upload /home/coder/sftp-proof\nput /tmp/upload /data/sftp-proof\nget /home/coder/legacy-proof /tmp/retained\n')
    execute(operator, ['timeout', '30', 'sftp', '-b', '/tmp/sftp.batch', *ssh[1:], 'coder@' + address])
    assert execute(outer, ['docker', 'exec', 'workspace_cvm', 'cat', '/home/coder/sftp-proof']) == b'transfer'
    assert execute(outer, ['docker', 'exec', 'workspace_cvm', 'cat', '/data/sftp-proof']) == b'transfer'
    assert execute(operator, ['cat', '/tmp/retained']) == b'retained'
    print('PASS: native Tailscale SFTP and inner Web/SSH share retained home and data', flush=True)
    for c, user in [(operator, 'root'), (denied, 'coder')]:
        r = c.exec_run(['timeout', '15', *ssh, user + '@' + address, 'true'])
        assert r.exit_code not in [0, 124], 'SSH ACL deny unexpectedly accepted'
    print('PASS: root SSH and other-user SSH denied by unchanged native ACL', flush=True)
    assert execute(outer, ['docker', 'exec', 'workspace_cvm', 'cat', '/home/coder/legacy-proof']) == b'retained'
    check = run('code-envbox-retained', 'code-server-tailnet:test', user='0:0', command=['sh', '-c', 'sleep infinity'],
                entrypoint='/usr/bin/env', volumes={home.name: {'bind': '/volume', 'mode': 'ro'}})
    assert execute(check, ['cat', '/volume/legacy-proof']) == b'retained'
    assert execute(check, ['stat', '-c', '%u:%g', '/volume/legacy-proof']).strip() == b'1000:1000'
    assert execute(check, ['stat', '-c', '%u:%g %a', '/volume/.local']).strip() == b'0:0 700'
    print('PASS: original home remains untouched and recoverable', flush=True)
finally:
    for c in reversed(containers):
        c.remove(force=True, v=True)
    for v in volumes:
        v.remove()
    network.remove()
