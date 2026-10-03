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

def wait_state(c):
    for _ in range(90):
        r = c.exec_run(['tailscale', '--socket=/tmp/tailscaled.sock', 'status', '--json'])
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
    home, relay, data = [client.volumes.create(name='code-envbox-test-' + n)
                         for n in ['home', 'relay', 'data']]
    volumes.extend([home, relay, data])
    seed = run('code-envbox-seed', 'code-server-tailnet:test',
               entrypoint='sh', command=['-c', 'printf retained > /home/coder/legacy-proof; sleep infinity'],
               volumes={home.name: {'bind': '/home/coder', 'mode': 'rw'}})
    time.sleep(1)
    assert execute(seed, ['cat', '/home/coder/legacy-proof']) == b'retained'
    seed.stop()
    init = run('code-envbox-init', 'code-server-envbox:test', command=['/usr/local/bin/migrate-home'],
               user='0:0', cap_drop=['ALL'], cap_add=['CHOWN', 'DAC_OVERRIDE', 'FOWNER'],
               security_opt=['no-new-privileges:true'],
               volumes={home.name: {'bind': '/volume', 'mode': 'rw'},
                        relay.name: {'bind': '/run/relay', 'mode': 'rw'}})
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
    control = run('code-test-control', 'ghcr.io/juanfont/headscale:v0.29.4@sha256:8833f828b414c0907b7e5c71da76473216fe17cce0818a166b536ec552c0903f', command=['serve'], volumes={
        str(fixtures / 'headscale.yaml'): {'bind': '/etc/headscale/config.yaml', 'mode': 'ro'},
        str(fixtures / 'policy.hujson'): {'bind': '/etc/headscale/policy.hujson', 'mode': 'ro'}})
    time.sleep(3)
    users = {name: json.loads(execute(control, ['/ko-app/headscale', 'users', 'create', name, '-o', 'json']))['id']
             for name in ['operator', 'denied']}
    def node(name, user, server=False):
        args = ['/ko-app/headscale', 'preauthkeys', 'create', '--user', str(users[user]), '--expiration', '5m', '-o', 'json']
        if server:
            args += ['--tags', 'tag:code-server']
        key = json.loads(execute(control, args))['key']
        env = {'TS_AUTHKEY': key, 'TS_HOSTNAME': name, 'TS_KUBE_SECRET': '', 'TS_USERSPACE': 'true',
               'TS_STATE_DIR': '/home/coder/.local/state/tailscale' if server else '/tmp/tailscale-state',
               'TS_SOCKET': '/tmp/tailscaled.sock', 'TS_ACCEPT_DNS': 'false', 'TS_AUTH_ONCE': 'true',
               'TS_EXTRA_ARGS': '--login-server=http://code-test-control:8080' + (' --ssh' if server else '')}
        options = {'user': '1000:1000', 'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true']}
        image = 'code-server-tailnet:test'
        if server:
            image = 'code-server-relay:test'
            options.update(user='101000:101000', group_add=['100000'], network_mode='container:' + outer.id,
                           mounts=[subpath(home, '/home/coder', '.envbox/home'),
                                   docker.types.Mount('/run/relay', relay.name, type='volume', read_only=True),
                                   docker.types.Mount('/data', data.name, type='volume')])
            options['volumes'] = {str(fixtures / 'serve.json'): {'bind': '/etc/tailscale/serve.json', 'mode': 'ro'}}
            env['TS_SERVE_CONFIG'] = '/etc/tailscale/serve.json'
        c = run(name, image, entrypoint='/usr/local/bin/containerboot', environment=env, **options)
        del key
        return c, wait_state(c)
    server, address = node('code-test-workspace', 'operator', True)
    operator, _ = node('code-test-operator', 'operator')
    denied, _ = node('code-test-denied', 'denied')
    ssh = ['ssh', '-o', 'ProxyCommand=tailscale --socket=/tmp/tailscaled.sock nc %h %p',
           '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null']
    result = execute(operator, ['timeout', '30', *ssh, 'coder@' + address,
                               'id -u; command -v docker; sudo -n docker info --format "{{.ServerVersion}}"; printf shell > /home/coder/shell-proof'])
    assert b'1000' in result.splitlines() and b'/usr/bin/docker' in result
    assert execute(outer, ['docker', 'exec', 'workspace_cvm', 'cat', '/home/coder/shell-proof']) == b'shell'
    r = operator.exec_run(['timeout', '30', *ssh, 'coder@' + address, 'exit 37'])
    assert r.exit_code == 37
    r = operator.exec_run(['timeout', '30', *ssh, '-tt', 'coder@' + address, 'test -t 0 && printf tty'])
    assert r.exit_code == 0 and b'tty' in r.output
    print('PASS: native Tailnet ACL -> inner coder UID 1000, Docker, shared home, exact exit status and TTY', flush=True)
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
    check = run('code-envbox-retained', 'code-server-tailnet:test', command=['sh', '-c', 'sleep infinity'],
                entrypoint='/usr/bin/env', volumes={home.name: {'bind': '/volume', 'mode': 'ro'}})
    assert execute(check, ['cat', '/volume/legacy-proof']) == b'retained'
    assert execute(check, ['stat', '-c', '%u:%g', '/volume/legacy-proof']).strip() == b'1000:1000'
    print('PASS: original home remains untouched and recoverable', flush=True)
finally:
    for c in reversed(containers):
        c.remove(force=True, v=True)
    for v in volumes:
        v.remove()
    network.remove()
