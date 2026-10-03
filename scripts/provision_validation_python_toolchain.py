"""Restore the synthetic Python toolchain from an immutable official AMD64 image.

Operator-only maintenance on the existing VM. Holds the validation slot, exercises
valid/invalid synthetic compilation, archives the reviewed image for offline reload,
then changes only the Python compile pin. No customer source or provider key is used.
"""
import argparse
import json
from pathlib import Path
import shlex
import subprocess  # nosec B404

from app.coding.validation_transport import IAPValidationHost, _protected, command


REPOSITORY_DIGEST = (
    "docker.io/library/python@sha256:"
    "6b1f85a08c199d29d5b6d71ab9c27bd5b3b393492e01216a15758ff69c4be8b8"
)
# This host uses Docker's containerd store: its exact image ID is the platform
# manifest digest, not the config blob digest. Do not accept either interchangeably.
IMAGE_ID = "sha256:6b1f85a08c199d29d5b6d71ab9c27bd5b3b393492e01216a15758ff69c4be8b8"
CONFIG_DIGEST = "sha256:414a398990af718f018ff9c23cea0e7489b7986eb54f2b1d7cc874c99ebc7364"
OLD_IMAGE_ID = "sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f"


def maintenance_failure(stderr: bytes) -> str:
    """Expose only fixed operator errors, never arbitrary SSH/provider output."""
    messages = (
        "invalid toolchain maintenance envelope", "toolchain operator path is not protected",
        "Python toolchain pin changed", "validation slot is unreconciled",
        "immutable toolchain pull failed", "immutable toolchain identity differs",
        "synthetic compilation proof failed", "toolchain archive export failed",
        "toolchain archive exceeds quota",
    )
    lines = stderr[-8192:].decode("utf-8", errors="replace").splitlines()
    for message in messages:
        if "RuntimeError: " + message in lines:
            return message
    return "operator state or transport requires inspection"

REMOTE_PROVISIONER = r'''
import hashlib,json,os,pathlib,runpy,subprocess,sys,tempfile
request=json.loads(sys.stdin.read(8193))
if set(request)!={'repository_digest','image_id','old_image_id'}:
    raise RuntimeError('invalid toolchain maintenance envelope')
root=pathlib.Path('/etc/gca-validation'); policy_path=root/'policy.json'
def safe(path):
    for item in (path,*path.parents):
        info=item.lstat()
        if info.st_uid!=0 or info.st_mode & 0o022 or item.is_symlink():
            raise RuntimeError('toolchain operator path is not protected')
safe(policy_path)
policy=json.loads(policy_path.read_text())
if policy['images'].get('python_compile') not in (request['old_image_id'],request['image_id']):
    raise RuntimeError('Python toolchain pin changed')
module=runpy.run_path('/usr/local/libexec/gca/isolated_validation.py')
state=pathlib.Path(policy['state_directory'])
with module['Journal'](state) as authority:
    authority.reap()
    if authority.db.execute("SELECT 1 FROM jobs WHERE state<>'cleaned' LIMIT 1").fetchone():
        raise RuntimeError('validation slot is unreconciled')
    pull=subprocess.run(['/usr/bin/docker','pull','--platform=linux/amd64',
                         request['repository_digest']],capture_output=True,timeout=120)
    if pull.returncode: raise RuntimeError('immutable toolchain pull failed')
    observed=subprocess.run(['/usr/bin/docker','image','inspect',request['repository_digest'],
        '--format','{{json .Id}} {{json .Architecture}} {{json .Os}}'],
        capture_output=True,timeout=10,check=True)
    expected=' '.join(json.dumps(value) for value in (request['image_id'],'amd64','linux'))
    if observed.stdout.decode().strip()!=expected:
        raise RuntimeError('immutable toolchain identity differs')
    probes=pathlib.Path('/var/lib/gca-validation-probes'); probes.mkdir(mode=0o700,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='restored-toolchain-',dir=probes) as temporary:
        base=pathlib.Path(temporary); workspace=base/'source'
        (workspace/'app').mkdir(parents=True); (workspace/'scripts').mkdir()
        source=workspace/'app/main.py'; source.write_text('answer=42\n')
        journal=base/'journal'; journal.mkdir(mode=0o700)
        probe_policy={'version':2,'runtime':'runsc','images':{'python_compile':request['image_id']},
                      'state_directory':str(journal)}
        valid=module['validate'](workspace,'python_compile',20,probe_policy)
        source.write_text('def invalid(:\n')
        invalid=module['validate'](workspace,'python_compile',20,probe_policy)
        if not valid['success'] or invalid['success']:
            raise RuntimeError('synthetic compilation proof failed')
    archives=pathlib.Path('/var/lib/gca-toolchains'); archives.mkdir(mode=0o700,exist_ok=True)
    safe(archives)
    archive=archives/(request['image_id'].split(':')[1]+'.tar')
    if not archive.exists():
        staged=archive.with_suffix('.tmp')
        save=subprocess.run(['/usr/bin/docker','image','save',request['image_id'],'-o',str(staged)],
                            capture_output=True,timeout=60)
        if save.returncode: raise RuntimeError('toolchain archive export failed')
        os.chmod(staged,0o600); os.replace(staged,archive)
    safe(archive)
    if archive.stat().st_size>512*1024*1024: raise RuntimeError('toolchain archive exceeds quota')
    digest=hashlib.sha256()
    with archive.open('rb') as stream:
        while chunk:=stream.read(1024*1024): digest.update(chunk)
    registry={'version':1,'images':{request['image_id']:{'archive_path':str(archive),
        'archive_sha256':digest.hexdigest(),'archive_bytes':archive.stat().st_size,
        'repository_digest':request['repository_digest']}}}
    def write_json(path,value):
        fd,temporary=tempfile.mkstemp(prefix='.toolchain-',dir=path.parent)
        try:
            with os.fdopen(fd,'w') as stream:
                json.dump(value,stream,sort_keys=True); stream.flush(); os.fsync(stream.fileno())
            os.chmod(temporary,0o600); os.replace(temporary,path)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
    write_json(root/'toolchain-registry.json',registry)
    policy['images']['python_compile']=request['image_id']; write_json(policy_path,policy)
    print(json.dumps({'python_compile_image':request['image_id'],'synthetic_valid_passed':True,
        'synthetic_invalid_rejected':True,'offline_archive_sha256':digest.hexdigest(),
        'offline_archive_bytes':archive.stat().st_size,'private_source_used':False,
        'new_cloud_resource_created':False}))
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--operator-config", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", required=True)
    args = parser.parse_args()
    config = _protected(args.operator_config.resolve(), private=True)
    host = IAPValidationHost.model_validate_json(config.read_text())
    argv, environment = command(host)
    argv[-1] = shlex.join(["sudo", "-n", "/usr/bin/python3", "-I", "-c", REMOTE_PROVISIONER])
    payload = json.dumps({"repository_digest": REPOSITORY_DIGEST, "image_id": IMAGE_ID,
                          "old_image_id": OLD_IMAGE_ID}).encode()
    result = subprocess.run(  # nosec B603
        argv, input=payload, capture_output=True, env=environment, timeout=240, check=False,
    )
    if result.returncode or len(result.stdout) > 8192:
        raise RuntimeError("Toolchain maintenance unconfirmed: " + maintenance_failure(result.stderr))
    print(json.dumps(json.loads(result.stdout), sort_keys=True))


if __name__ == "__main__":
    main()
