"""Operator-only rollout of the reviewed stdlib validation bundle over pinned IAP.

Default is a read-only installation probe. Apply requires the observed old hash,
an idle journal, exact two-file bundle, compilation and an isolated import check.
This script neither changes policy/toolchain images nor runs a generated workload.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess  # nosec B404

from app.coding.validation_transport import IAPValidationHost, _protected, command


REMOTE_INSTALLER = r'''
import fcntl,hashlib,json,os,pathlib,sqlite3,subprocess,sys,tempfile
root=pathlib.Path('/usr/local/libexec/gca')
policy_path=pathlib.Path('/etc/gca-validation/policy.json')
def safe(path):
    for item in (path,*path.parents):
        info=item.lstat()
        if info.st_uid!=0 or info.st_mode & 0o022 or item.is_symlink():
            raise RuntimeError('operator path is not protected')
safe(root); safe(policy_path)
request=json.loads(sys.stdin.buffer.read(160001))
if set(request)!={'apply','expected_base_sha256','files'} or type(request['apply']) is not bool:
    raise RuntimeError('invalid installation envelope')
files=request['files']
if not isinstance(files,dict) or set(files)!={'isolated_validation.py','build_artifact.py'}:
    raise RuntimeError('invalid installation files')
for name,body in files.items():
    if not isinstance(body,str) or len(body.encode())>100000:
        raise RuntimeError('invalid installation file size')
    compile(body,name,'exec')
policy=json.loads(policy_path.read_text())
state=pathlib.Path(policy['state_directory']); safe(state)
slot=os.open(state/'slot.lock',os.O_RDWR|os.O_NOFOLLOW)
fcntl.flock(slot,fcntl.LOCK_EX|fcntl.LOCK_NB)
with sqlite3.connect('file:'+str(state/'jobs.sqlite3')+'?mode=ro',uri=True) as database:
    if database.execute("SELECT 1 FROM jobs WHERE state<>'cleaned' LIMIT 1").fetchone():
        raise RuntimeError('unreconciled validation job prevents installation')
launcher=root/'isolated_validation.py'; safe(launcher)
old_hash=hashlib.sha256(launcher.read_bytes()).hexdigest()
if not request['apply']:
    print(json.dumps({'launcher_sha256':old_hash,'journal_idle':True,
                     'helper_installed':(root/'build_artifact.py').exists(),'applied':False}))
    sys.exit(0)
if request['expected_base_sha256']!=old_hash:
    raise RuntimeError('installed launcher changed since review')
for name in files:
    if (root/name).exists(): safe(root/name)
old={name:((root/name).read_bytes(),(root/name).stat().st_mode & 0o777)
     if (root/name).exists() else None for name in files}
def replace(name,body,mode):
    fd,temporary=tempfile.mkstemp(prefix='.bundle-',dir=root)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(body); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary,mode)
        os.replace(temporary,root/name)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
try:
    replace('build_artifact.py',files['build_artifact.py'].encode(),0o644)
    replace('isolated_validation.py',files['isolated_validation.py'].encode(),0o755)
    probe=subprocess.run(['/usr/bin/python3','-I','-c',
        "import runpy; m=runpy.run_path('/usr/local/libexec/gca/isolated_validation.py'); "
        "assert m['_artifact_helper']().MAX_STORED_ARTIFACTS == 8"],
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        timeout=10,env={'PATH':'/usr/bin:/bin','HOME':'/nonexistent'})
    if probe.returncode: raise RuntimeError('installed bundle import check failed')
except BaseException:
    for name,body in old.items():
        if body is None: (root/name).unlink(missing_ok=True)
        else: replace(name,body[0],body[1])
    raise
directory=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
os.fsync(directory); os.close(directory)
print(json.dumps({'applied':True,'journal_idle':True,'isolated_import_passed':True,
    'hashes':{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in files},
    'policy_images_changed':False,'generated_workload_executed':False}))
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--operator-config", type=Path, required=True)
    parser.add_argument("--expected-base-sha256")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply and (args.expected_base_sha256 is None
                       or len(args.expected_base_sha256) != 64):
        parser.error("Apply requires the previously observed launcher SHA-256")
    config = _protected(args.operator_config.resolve(), private=True)
    host = IAPValidationHost.model_validate_json(config.read_text())
    argv, environment = command(host)
    argv[-1] = shlex.join(["sudo", "-n", "/usr/bin/python3", "-I", "-c", REMOTE_INSTALLER])
    source = Path(__file__).resolve().parents[1] / "app/coding"
    files = {name: (source / name).read_text()
             for name in ("isolated_validation.py", "build_artifact.py")}
    payload = json.dumps({"apply": args.apply, "expected_base_sha256": args.expected_base_sha256,
                          "files": files}).encode()
    result = subprocess.run(  # nosec B603
        argv, input=payload, capture_output=True, env=environment, timeout=45, check=False,
    )
    if result.returncode or len(result.stdout) > 8192:
        raise RuntimeError("Validation bundle installation unconfirmed; inspect operator logs")
    evidence = json.loads(result.stdout)
    if args.apply:
        expected = {name: hashlib.sha256(body.encode()).hexdigest() for name, body in files.items()}
        if evidence.get("hashes") != expected or evidence.get("isolated_import_passed") is not True:
            raise RuntimeError("Installed validation bundle digest mismatch")
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    main()
