"""Operator-only fixed systemd group preparation on the existing IAP host.

Defaults to inspection. No model-selected command, VM resource, runtime approval,
production admission or financial activation. Lost responses require a fresh probe.
"""
import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess  # nosec B404

from app.coding.validation_transport import IAPValidationHost, _protected, command


REMOTE_SOURCE = r'''
import fcntl,hashlib,json,os,pathlib,sqlite3,stat,subprocess,sys,tempfile
UNIT=pathlib.Path('/etc/systemd/system/gcapackaging.slice')
GROUP=pathlib.Path('/sys/fs/cgroup/gcapackaging.slice')
POLICY=pathlib.Path('/etc/gca-validation/policy.json')
BODY=b'[Unit]\nDescription=GCA bounded packaging aggregate\n\n[Slice]\nCPUAccounting=yes\nMemoryAccounting=yes\nTasksAccounting=yes\nCPUQuota=100%\nMemoryMax=3221225472\nMemorySwapMax=0\nTasksMax=128\n'
ENV={'PATH':'/usr/bin:/bin','LANG':'C','HOME':'/nonexistent'}
class GroupError(RuntimeError): pass
def canonical(value): return json.dumps(value,sort_keys=True,separators=(',',':')).encode()
def safe(path):
 for entry in (path,*path.parents):
  info=entry.lstat()
  if entry.is_symlink() or info.st_uid!=os.geteuid() or info.st_mode&0o022:
   raise GroupError('operator group path is not protected')
  if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
   raise GroupError('operator group inode differs')
  if stat.S_ISREG(info.st_mode) and info.st_nlink!=1:
   raise GroupError('operator group inode has multiple links')
 return path
def read(path):
 safe(path)
 with path.open('rb') as stream: value=stream.read(16385)
 if len(value)>16384: raise GroupError('operator observation exceeds bound')
 return value
def snapshot():
 unit=read(UNIT) if UNIT.exists() or UNIT.is_symlink() else None
 if unit is not None and unit!=BODY: raise GroupError('foreign packaging unit requires operator review')
 values={}
 if GROUP.exists() or GROUP.is_symlink():
  safe(GROUP)
  children=[p.name for p in GROUP.iterdir() if p.is_dir()]
  if any(name!='controller' for name in children): raise GroupError('packaging resource group is occupied')
  for name in ('memory.max','memory.swap.max','cpu.max','pids.max','cgroup.procs'):
   values[name]=read(GROUP/name).decode('ascii').strip()
  if values['cgroup.procs']: raise GroupError('aggregate contains native processes')
  controller=GROUP/'controller'
  if controller.exists():
   if read(controller/'cgroup.procs').strip(): raise GroupError('controller group is occupied')
  values['controller_exists']=controller.exists()
 data={'unit_hash':hashlib.sha256(unit).hexdigest() if unit is not None else None,'kernel':values}
 return data,hashlib.sha256(canonical(data)).hexdigest()
def ready(values):
 try:
  quota,period=map(int,values['cpu.max'].split())
  return (int(values['memory.max'])==3221225472 and int(values['memory.swap.max'])==0
   and 0<quota<=period and int(values['pids.max'])==128 and values['controller_exists'] is True)
 except (KeyError,ValueError,TypeError): return False
def sync(path):
 fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
 try:os.fsync(fd)
 finally:os.close(fd)
def install_unit():
 safe(UNIT.parent)
 fd,temporary=tempfile.mkstemp(prefix='.gca-group-',dir=UNIT.parent)
 try:
  with os.fdopen(fd,'wb') as stream:
   stream.write(BODY);stream.flush();os.fsync(stream.fileno())
  os.chmod(temporary,0o600)
  os.replace(temporary,UNIT);sync(UNIT.parent)
 finally:
  if os.path.exists(temporary):os.unlink(temporary)
def systemctl(*args):
 result=subprocess.run(['/usr/bin/systemctl',*args],stdin=subprocess.DEVNULL,
  stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=ENV,timeout=15,check=False)
 if result.returncode:raise GroupError('fixed systemd group preparation is unconfirmed')
def operate(request):
 if not isinstance(request,dict) or request.get('operation') not in {'probe','apply'}:
  raise GroupError('invalid operator operation')
 expected={'operation'} if request['operation']=='probe' else {'operation','expected_snapshot_hash'}
 if set(request)!=expected:raise GroupError('invalid operator envelope')
 state=pathlib.Path(json.loads(read(POLICY))['state_directory'])
 if not state.is_absolute():raise GroupError('host state is not absolute')
 safe(state)
 fd=os.open(safe(state/'slot.lock'),os.O_RDWR|os.O_NOFOLLOW)
 try:
  fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  database=safe(state/'jobs.sqlite3')
  with sqlite3.connect('file:'+str(database)+'?mode=ro',uri=True) as connection:
   if connection.execute("SELECT 1 FROM jobs WHERE state<>'cleaned' LIMIT 1").fetchone():
    raise GroupError('unreconciled host work prevents resource-group preparation')
  data,digest=snapshot()
  if request['operation']=='apply':
   if request['expected_snapshot_hash']!=digest:raise GroupError('operator snapshot changed')
   if data['unit_hash'] is None:install_unit()
   systemctl('daemon-reload')
   systemctl('start','gcapackaging.slice')
   safe(GROUP)
   controller=GROUP/'controller'
   if not controller.exists():controller.mkdir(mode=0o755)
   data,digest=snapshot()
   if not ready(data['kernel']):raise GroupError('native aggregate limits are not verified')
  return {'operation':request['operation'],'snapshot_hash':digest,'state':data,
   'kernel_limits_verified':ready(data['kernel']),'production_packaging_activated':False,
   'cloud_resource_created':False}
 finally:os.close(fd)
def main():
 if sys.platform!='linux' or os.geteuid()!=0:raise GroupError('operator Linux root required')
 body=sys.stdin.buffer.read(16385)
 if len(body)>16384:raise GroupError('operator request exceeds bound')
 print(json.dumps(operate(json.loads(body)),sort_keys=True))
if __name__=='__main__':main()
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--operator-config', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-snapshot-hash')
    args = parser.parse_args()
    if args.apply and not re.fullmatch(r'[a-f0-9]{64}', args.expected_snapshot_hash or ''):
        parser.error('Apply requires a fresh read-only snapshot hash')
    compile(REMOTE_SOURCE, 'reviewed-operator-packaging-group', 'exec')
    host = IAPValidationHost.model_validate_json(_protected(args.operator_config.resolve(), private=True).read_text())
    argv, environment = command(host)
    argv[-1] = shlex.join(['sudo', '-n', '/usr/bin/python3', '-I', '-B', '-c', REMOTE_SOURCE])
    request = {'operation': 'apply' if args.apply else 'probe'}
    if args.apply:
        request['expected_snapshot_hash'] = args.expected_snapshot_hash
    result = subprocess.run(  # nosec B603
        argv, input=json.dumps(request).encode(), env=environment, capture_output=True, timeout=60, check=False,
    )
    if result.returncode or len(result.stdout) > 16384:
        raise RuntimeError('Operator group outcome unconfirmed; probe before further action')
    evidence = json.loads(result.stdout)
    if (evidence.get('operation') != request['operation']
            or evidence.get('production_packaging_activated') is not False
            or evidence.get('cloud_resource_created') is not False
            or (args.apply and evidence.get('kernel_limits_verified') is not True)):
        raise RuntimeError('Operator group evidence invalid')
    print(json.dumps(evidence, sort_keys=True))


if __name__ == '__main__':
    main()
