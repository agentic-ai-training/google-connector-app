"""Operator-only reversible optional gVisor sidecar exclusion.

Default is read-only. This never removes a binary, restarts Docker, changes the
default runtime, grants private workload authority or creates a cloud resource.
The pinned runtime explicitly tolerates this optional sidecar being non-executable.
"""
import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess  # nosec B404

from app.coding.validation_transport import IAPValidationHost, _protected, command


REMOTE_SOURCE = r'''
import fcntl,hashlib,json,os,pathlib,sqlite3,stat,sys,tempfile
BINARY=pathlib.Path('/usr/bin/gvisor-bin/runsc-fd-parking')
POLICY=pathlib.Path('/etc/gca-validation/policy.json')
GROUP=pathlib.Path('/sys/fs/cgroup/gcapackaging.slice')
PINS={
 '/usr/bin/runsc':'2eb94b58769293ce5041beecf4cbc14b173604371bfaac7cb3f3a84a28e44248',
 '/usr/bin/gvisor-bin/gvisor_sentry':'4405affdaf81675d50d8d4c89f07efe2687c16fa69082f0d88978cae1dfe6697',
 str(BINARY):'03af90d07ea466c10ecc3e3ba839b2a59540959dd3286aea3886917cba850c11'}
class RuntimeErrorEvidence(RuntimeError):pass
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()
def safe(path):
 for entry in (path,*path.parents):
  info=entry.lstat()
  if entry.is_symlink() or info.st_uid!=os.geteuid() or info.st_mode&0o022:
   raise RuntimeErrorEvidence('operator runtime path is not protected')
  if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
   raise RuntimeErrorEvidence('operator runtime inode differs')
  if stat.S_ISREG(info.st_mode) and info.st_nlink!=1:
   raise RuntimeErrorEvidence('operator runtime inode has multiple links')
 return path
def read(path,maximum=16384):
 safe(path)
 with path.open('rb') as stream:body=stream.read(maximum+1)
 if len(body)>maximum:raise RuntimeErrorEvidence('operator observation exceeds bound')
 return body
def sync(path):
 fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
 try:os.fsync(fd)
 finally:os.close(fd)
def persist(path,value):
 safe(path.parent)
 fd,temporary=tempfile.mkstemp(prefix='.sidecar-intent-',dir=path.parent)
 try:
  with os.fdopen(fd,'wb') as stream:
   stream.write(canonical(value));stream.flush();os.fsync(stream.fileno())
  os.chmod(temporary,0o600);os.replace(temporary,path);sync(path.parent)
 finally:
  if os.path.exists(temporary):os.unlink(temporary)
def snapshot(state):
 observations={}
 for name,expected in PINS.items():
  path=safe(pathlib.Path(name));info=path.stat()
  if info.st_size>128*1024**2:raise RuntimeErrorEvidence('runtime binary exceeds bound')
  digest=hashlib.sha256()
  with path.open('rb') as stream:
   for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
  if digest.hexdigest()!=expected:raise RuntimeErrorEvidence('pinned runtime package differs')
  mode=stat.S_IMODE(info.st_mode)
  if mode not in ({0o644,0o755} if path==BINARY else {0o755}):
   raise RuntimeErrorEvidence('runtime binary mode differs')
  observations[name]={'hash':digest.hexdigest(),'mode':mode,'device':info.st_dev,'inode':info.st_ino}
 intent_path=state/'optional-sidecar.json'
 intent=json.loads(read(intent_path)) if intent_path.exists() or intent_path.is_symlink() else None
 if intent is not None and (not isinstance(intent,dict) or set(intent)!={'version','phase','target_mode','binary_hash'}
  or intent['version']!=1 or intent['phase'] not in {'prepared','committed'}
  or intent['target_mode'] not in {0o644,0o755} or intent['binary_hash']!=PINS[str(BINARY)]):
  raise RuntimeErrorEvidence('sidecar intent differs')
 data={'binaries':observations,'intent':intent}
 return data,hashlib.sha256(canonical(data)).hexdigest()
def idle(state):
 with sqlite3.connect('file:'+str(safe(state/'jobs.sqlite3'))+'?mode=ro',uri=True) as database:
  if database.execute("SELECT 1 FROM jobs WHERE state<>'cleaned' LIMIT 1").fetchone():
   raise RuntimeErrorEvidence('unreconciled host work prevents runtime preparation')
 safe(GROUP)
 for directory in [GROUP,*[entry for entry in GROUP.iterdir() if entry.is_dir()]]:
  if read(directory/'cgroup.procs').strip():raise RuntimeErrorEvidence('runtime resource group is occupied')
 if any(entry.is_dir() and entry.name!='controller' for entry in GROUP.iterdir()):
  raise RuntimeErrorEvidence('foreign resource group requires review')
def operate(request):
 if not isinstance(request,dict) or request.get('operation') not in {'probe','exclude','restore'}:
  raise RuntimeErrorEvidence('invalid fixed runtime operation')
 fields={'operation'} if request['operation']=='probe' else {'operation','expected_snapshot_hash'}
 if set(request)!=fields:raise RuntimeErrorEvidence('invalid runtime envelope')
 state=pathlib.Path(json.loads(read(POLICY))['state_directory'])
 if not state.is_absolute():raise RuntimeErrorEvidence('runtime state is not absolute')
 safe(state)
 fd=os.open(safe(state/'slot.lock'),os.O_RDWR|os.O_NOFOLLOW)
 try:
  fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  idle(state)
  data,digest=snapshot(state)
  if request['operation']!='probe':
   if request['expected_snapshot_hash']!=digest:raise RuntimeErrorEvidence('operator runtime snapshot changed')
   target=0o644 if request['operation']=='exclude' else 0o755
   previous=data['intent']
   if previous and previous['phase']=='prepared' and previous['target_mode']!=target:
    raise RuntimeErrorEvidence('interrupted sidecar transition requires same-target reconciliation')
   intent={'version':1,'phase':'prepared','target_mode':target,'binary_hash':PINS[str(BINARY)]}
   persist(state/'optional-sidecar.json',intent)
   binary_fd=os.open(BINARY,os.O_RDONLY|os.O_NOFOLLOW)
   try:
    expected=data['binaries'][str(BINARY)];actual=os.fstat(binary_fd)
    if (actual.st_dev,actual.st_ino)!=(expected['device'],expected['inode']):
     raise RuntimeErrorEvidence('runtime binary inode changed')
    os.fchmod(binary_fd,target);os.fsync(binary_fd)
   finally:os.close(binary_fd)
   sync(BINARY.parent)
   verified,_=snapshot(state)
   if verified['binaries'][str(BINARY)]['mode']!=target:raise RuntimeErrorEvidence('sidecar mode unconfirmed')
   intent['phase']='committed';persist(state/'optional-sidecar.json',intent)
   data,digest=snapshot(state)
  return {'operation':request['operation'],'snapshot_hash':digest,'state':data,'journal_idle':True,
   'optional_sidecar_excluded':data['binaries'][str(BINARY)]['mode']==0o644,
   'production_packaging_activated':False,'cloud_resource_created':False,'docker_restarted':False}
 finally:os.close(fd)
def main():
 if sys.platform!='linux' or os.geteuid()!=0:raise RuntimeErrorEvidence('operator Linux root required')
 body=sys.stdin.buffer.read(16385)
 if len(body)>16384:raise RuntimeErrorEvidence('runtime request exceeds bound')
 print(json.dumps(operate(json.loads(body)),sort_keys=True))
if __name__=='__main__':main()
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--operator-config', type=Path, required=True)
    choices = parser.add_mutually_exclusive_group()
    choices.add_argument('--exclude', action='store_true')
    choices.add_argument('--restore', action='store_true')
    parser.add_argument('--expected-snapshot-hash')
    args = parser.parse_args()
    if (args.exclude or args.restore) and not re.fullmatch(r'[a-f0-9]{64}', args.expected_snapshot_hash or ''):
        parser.error('Runtime change requires a fresh exact snapshot hash')
    compile(REMOTE_SOURCE, 'reviewed-optional-sidecar-transition', 'exec')
    host = IAPValidationHost.model_validate_json(_protected(args.operator_config.resolve(), private=True).read_text())
    argv, environment = command(host)
    argv[-1] = shlex.join(['sudo', '-n', '/usr/bin/python3', '-I', '-B', '-c', REMOTE_SOURCE])
    operation = 'exclude' if args.exclude else 'restore' if args.restore else 'probe'
    request = {'operation': operation}
    if operation != 'probe':
        request['expected_snapshot_hash'] = args.expected_snapshot_hash
    result = subprocess.run(  # nosec B603
        argv, input=json.dumps(request).encode(), env=environment, capture_output=True, timeout=60, check=False,
    )
    if result.returncode or len(result.stdout) > 16384:
        raise RuntimeError('Runtime transition unconfirmed; probe before retry, never blind redispatch')
    evidence = json.loads(result.stdout)
    if (evidence.get('operation') != operation or evidence.get('journal_idle') is not True
            or any(evidence.get(key) is not False for key in
                   ('production_packaging_activated', 'cloud_resource_created', 'docker_restarted'))
            or (operation != 'probe' and evidence.get('optional_sidecar_excluded') is not args.exclude)):
        raise RuntimeError('Fixed runtime evidence is invalid')
    print(json.dumps(evidence, sort_keys=True))


if __name__ == '__main__':
    main()
