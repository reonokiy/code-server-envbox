import io, tarfile, time, secrets, uuid
from pathlib import Path
import docker, boto3, grpc
import csi_pb2 as csi
import csi_pb2_grpc as rpc

client=docker.from_env()
tag='envbox-managed-'+uuid.uuid4().hex[:8]
network=client.networks.create(tag)
containers=[]
volumes=[]
outer=None
node=None
mounted=False
envbox_image='code-server-envbox:test'
csi_image='cr.yandex/crp9ftr22d26age3hulg/yandex-cloud/csi-s3/csi-s3-driver:0.43.7@sha256:8e4259545a065be3c21362ddeb5739f499d980a9c3d97f71dd8c1efd0d8629ea'
try:
    mock=client.containers.run('motoserver/moto:5.1.21@sha256:93ad54da7badce7f9c13e5e6439c93564c764663c42872d2c39f718aa484047a',name=tag+'-s3',network=network.name,ports={'5000/tcp':('127.0.0.1',15000)},detach=True)
    containers.append(mock)
    access,secret=secrets.token_hex(12),secrets.token_hex(24)
    s3=boto3.client('s3',endpoint_url='http://127.0.0.1:15000',region_name='us-east-1',aws_access_key_id=access,aws_secret_access_key=secret)
    for _ in range(30):
        try: s3.create_bucket(Bucket='code-test-data'); break
        except Exception: time.sleep(1)
    s3.put_object(Bucket='code-test-data',Key='workspace/existing',Body=b'existing')
    s3.put_object(Bucket='code-test-data',Key='outside',Body=b'boundary')
    paths=['/var/lib/docker','/var/lib/coder','/var/lib/sysbox','/home/coder']
    volumes=[client.volumes.create(name=tag+'-'+str(i)) for i in range(len(paths))]
    outer=client.containers.create(envbox_image,name=tag+'-envbox',network=network.name,privileged=True,command=['sh','-c','sleep infinity'],environment={'CODER_INNER_IMAGE':'docker.io/library/ubuntu:24.04@sha256:a853f94d226358a79c740cfc7bce0c289748f3fe3488d921d038ccd752c61b60','CODER_INNER_USERNAME':'root','CODER_MOUNTS':'/home/coder:/home/coder,/data:/data:managed'},volumes={v.name:{'bind':p,'mode':'rw'} for v,p in zip(volumes,paths)},ports={'10000/tcp':('127.0.0.1',15001)})
    containers.append(outer)
    donor=client.containers.create(csi_image)
    containers.append(donor)
    for source,target in [('/s3driver','/'),('/usr/bin/geesefs','/usr/bin/')]:
        stream,_=donor.get_archive(source)
        assert outer.put_archive(target,b''.join(stream))
    outer.start()
    r=outer.exec_run(['sh','-c','test -c /dev/fuse || mknod /dev/fuse c 10 229; test -x /bin/fusermount || ln -s /bin/fusermount3 /bin/fusermount'])
    assert r.exit_code==0
    outer.exec_run(['/s3driver','--endpoint=tcp://0.0.0.0:10000','--nodeid=isolated','--v=0'],detach=True)
    channel=grpc.insecure_channel('127.0.0.1:15001')
    grpc.channel_ready_future(channel).result(timeout=30)
    node=rpc.NodeStub(channel)
    capability=csi.VolumeCapability(mount=csi.VolumeCapability.MountVolume(),access_mode=csi.VolumeCapability.AccessMode(mode=csi.VolumeCapability.AccessMode.MULTI_NODE_MULTI_WRITER))
    credentials={'accessKeyID':access,'secretAccessKey':secret,'endpoint':'http://'+tag+'-s3:5000','region':'us-east-1'}
    context={'mounter':'geesefs','options':'--no-systemd --no-detect --memory-limit 256 --uid 100000 --gid 100000 --dir-mode 0770 --file-mode 0660 --list-type 2'}
    volume='code-test-data/workspace'
    node.NodeStageVolume(csi.NodeStageVolumeRequest(volume_id=volume,staging_target_path='/stage',volume_capability=capability,secrets=credentials,volume_context=context),timeout=30)
    node.NodePublishVolume(csi.NodePublishVolumeRequest(volume_id=volume,staging_target_path='/stage',target_path='/data',volume_capability=capability,secrets=credentials,volume_context=context),timeout=30)
    mounted=True
    print('Actual CSI mount into Envbox outer: PASS',flush=True)
    outer.exec_run(['sh','-c','/envbox docker >/tmp/envbox-managed.log 2>&1 &'])
    for _ in range(120):
        r=outer.exec_run(['docker','inspect','--format','{{.State.Running}}','workspace_cvm'])
        if r.exit_code==0 and r.output.strip()==b'true': break
        time.sleep(1)
    else:
        r=outer.exec_run(['cat','/tmp/envbox-managed.log'])
        logs=r.output.decode(errors='replace').lower()
        print('Managed S3 startup FAIL signatures='+str({t:t in logs for t in ['chmod','chown','permission denied','operation not permitted','id-mapping','no space left']}),flush=True)
        raise RuntimeError('Managed S3 bind mount startup failed')
    def inner(args):
        r=outer.exec_run(['docker','exec','workspace_cvm',*args])
        if r.exit_code: raise RuntimeError('Inner S3 filesystem probe failed')
        return r.output
    print('Managed CSI mount through actual Sysbox workspace: PASS',flush=True)
    inner(['sh','-c','test "$(cat /data/existing)" = existing; printf persist > /data/persist; mkdir /data/project; printf proof > /data/project/a; mv /data/project/a /data/project/b; sync'])
    print('Inner mapped root reads existing S3 objects and writes/renames: PASS',flush=True)
    r=outer.exec_run(['docker','exec','--user','1000:0','workspace_cvm','sh','-c','test "$(cat /data/existing)" = existing; printf coder > /data/coder; sync'])
    assert r.exit_code==0, 'Inner non-root coder with mapped data group cannot write'
    r=outer.exec_run(['docker','exec','--user','1000:1000','workspace_cvm','sh','-c','cat /data/existing'])
    assert r.exit_code!=0, 'Data mount unexpectedly permits an unrelated mapped group'
    print('Inner UID 1000 with data group 0 reads/writes; UID 1000 without that group denied: PASS',flush=True)
    outer.exec_run(['docker','stop','workspace_cvm'])
    time.sleep(3)
    node.NodeUnpublishVolume(csi.NodeUnpublishVolumeRequest(volume_id=volume,target_path='/data'),timeout=30)
    node.NodeUnstageVolume(csi.NodeUnstageVolumeRequest(volume_id=volume,staging_target_path='/stage'),timeout=30)
    mounted=False
    assert s3.get_object(Bucket='code-test-data',Key='workspace/persist')['Body'].read()==b'persist'
    assert s3.get_object(Bucket='code-test-data',Key='workspace/project/b')['Body'].read()==b'proof'
    assert s3.get_object(Bucket='code-test-data',Key='outside')['Body'].read()==b'boundary'
    print('S3 content persistence and prefix isolation: PASS',flush=True)
finally:
    if outer is not None and node is not None and mounted:
        try:
            outer.exec_run(['docker','stop','workspace_cvm'])
            node.NodeUnpublishVolume(csi.NodeUnpublishVolumeRequest(volume_id='code-test-data/workspace',target_path='/data'),timeout=15)
            node.NodeUnstageVolume(csi.NodeUnstageVolumeRequest(volume_id='code-test-data/workspace',staging_target_path='/stage'),timeout=15)
        except Exception: pass
    for c in reversed(containers): c.remove(force=True,v=True)
    for v in volumes: v.remove()
    network.remove()
