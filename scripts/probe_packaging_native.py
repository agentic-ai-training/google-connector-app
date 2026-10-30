"""Operator-only live native observation of a fixed nonsensitive Python sleeper.

No private artifact, model key or generated code. Does not approve a runtime image,
activate packaging or claim a full packaging proof. Uses the existing host slot.
"""
import argparse
import json
from pathlib import Path
import shlex
import subprocess  # nosec B404

from app.coding.validation_transport import IAPValidationHost, _protected, command


REMOTE_SOURCE = r'''
import json,pathlib,runpy,time
modules=runpy.run_path('/usr/local/libexec/gca/isolated_validation.py')
policy=modules['load_policy'](pathlib.Path('/etc/gca-validation/policy.json'),None)
host=modules['_packaging_helper']()
image=policy['images']['python_compile']
with modules['Journal'](pathlib.Path(policy['state_directory'])) as journal:
 host.require_host_limits()
 modules['ensure_toolchain_image'](image)
 job=journal.begin('python_compile',image,20)
 attempted=confirmed=False
 observed={'native_enforcement_verified':False,'production_packaging_activated':False}
 try:
  attempted=True
  cid=modules['docker']('create','--name',job['name'],'--pull=never','--runtime=runsc',
   '--label=gca.purpose=validation','--label=gca.scope='+journal.scope,'--label=gca.job='+job['id'],
   '--network=none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges:true',
   '--user=65532:65532','--ipc=private','--cgroupns=private','--memory=3g','--memory-swap=3g',
   '--cpus=1','--pids-limit=64','--cgroup-parent=gcapackaging.slice',
   '--ulimit=nofile=64:64','--log-driver=none','--entrypoint=/usr/bin/env',image,
   '-i','PATH=/usr/local/bin:/usr/bin:/bin','HOME=/nonexistent','LANG=C.UTF-8',
   'PYTHONDONTWRITEBYTECODE=1','python3','-I','-B','-c',
   'import time; time.sleep(10); print("synthetic-native-observation-finished")').decode().strip()
  confirmed=True
  journal.verify_identity(job,cid)
  journal.record(job,'running',cid)
  def inspect_started(child):
   deadline=time.monotonic()+8
   observed['native_observation_attempts']=0
   while time.monotonic()<deadline and child.poll() is None:
    spec=json.loads(modules['docker']('inspect',cid))[0]
    if spec.get('State',{}).get('Running'):
     pid=spec['State']['Pid']
     status=dict(line.split(':',1) for line in host._native_text(pathlib.Path('/proc')/str(pid)/'status').splitlines() if ':' in line)
     observed['native_status']={key:status.get(key,'unavailable').strip() for key in ('Uid','Gid','CapEff','CapPrm','CapBnd','NoNewPrivs','Seccomp')}
     observed['native_cgroup']=host._native_text(pathlib.Path('/proc')/str(pid)/'cgroup')
     helpers=host.native_helpers(cid)
     if not any(helper_pid==pid for helper_pid,_ in helpers):
      helpers.append((pid,observed['native_cgroup']))
     observed['helper_groups']=[group for _,group in helpers[:16]]
     members=list(map(int,host._native_text(pathlib.Path('/sys/fs/cgroup/gcapackaging.slice')/('docker-'+cid+'.scope/cgroup.procs')).split()))
     observed['scope_process_count']=len(set(members))
     observed['helper_process_count']=len(set(helper_pid for helper_pid,_ in helpers))
     observed['scope_members_missing_from_helper_inventory']=sorted(set(members)-set(helper_pid for helper_pid,_ in helpers))[:16]
     observed['missing_scope_member_details']=[]
     for missing_pid in observed['scope_members_missing_from_helper_inventory']:
      missing_root=pathlib.Path('/proc')/str(missing_pid)
      missing_fields=dict(line.split(':',1) for line in host._native_text(missing_root/'status').splitlines() if ':' in line)
      try:missing_exe=host.os.readlink(missing_root/'exe')
      except FileNotFoundError:missing_exe='unavailable'
      threads=[]
      for thread in sorted((missing_root/'task').iterdir(),key=lambda item:int(item.name))[:16]:
       thread_fields=dict(line.split(':',1) for line in host._native_text(thread/'status').splitlines() if ':' in line)
       try:thread_exe=host.os.readlink(thread/'exe')
       except FileNotFoundError:thread_exe='unavailable'
       threads.append({'tid':int(thread.name),'executable':thread_exe,
        'status':{key:thread_fields.get(key,'unavailable').strip() for key in ('State','PPid','Tgid','NSpid','Uid','Gid','CapEff','Seccomp')}})
      observed['missing_scope_member_details'].append({'pid':missing_pid,'executable':missing_exe,
       'status':{key:missing_fields.get(key,'unavailable').strip() for key in ('State','PPid','Tgid','NSpid','Uid','Gid','CapEff','Seccomp')},'threads':threads})
     observed['native_isolation']=[]
     for helper_pid,group in helpers[:16]:
      root=pathlib.Path('/proc')/str(helper_pid)
      fields=dict(line.split(':',1) for line in host._native_text(root/'status').splitlines() if ':' in line)
      names=('user','mnt','net','pid','ipc','uts')
      arguments=host._native_text(root/'cmdline').split('\x00')
      executable=host.os.readlink(root/'exe')
      role=next((name for name in ('boot','gofer') if name in arguments),'unknown')
      host_root=host.os.stat('/');process_root=host.os.stat(root/'root')
      observed['native_isolation'].append({'is_primary':helper_pid==pid,'group':group,
       'role':role,'executable':executable,'cmdline_empty':not any(arguments),'root_distinct':(host_root.st_dev,host_root.st_ino)!=(process_root.st_dev,process_root.st_ino),
       'status':{key:fields.get(key,'unavailable').strip() for key in ('PPid','TracerPid','Tgid','Uid','Gid','CapEff','CapPrm','CapBnd','NoNewPrivs','Seccomp')},
       'namespaces_distinct':{name:host.os.readlink(root/'ns'/name)!=host.os.readlink(pathlib.Path('/proc/self/ns')/name) for name in names},
       'uid_map':host._native_text(root/'uid_map',4096),'gid_map':host._native_text(root/'gid_map',4096)})
     try:
      observed['native_observation_attempts']+=1
      proof=host.verify_native_worker(spec,{'execution_deadline_unix':job['deadline']})
      observed['native_enforcement_verified']=True
      observed['proof_fields']=sorted(proof)
     except host.NativeObservationChanged as error:
      observed['native_guard_failure']=str(error)[:256]
      time.sleep(0.1)
      continue
     except RuntimeError as error:observed['native_guard_failure']=str(error)[:256]
     return
    time.sleep(0.1)
   observed.setdefault('native_guard_failure','synthetic worker did not become live within observation deadline')
   observed['observation_deadline_reached']=True
  result=modules['execute_bounded'](['start','-a',cid],20,output_limit=16384,on_started=inspect_started)
  observed['synthetic_exit_code']=result['exit_code']
  observed['synthetic_execution_success']=result['success'] and not result['output_truncated']
 finally:
  try:
   journal.cleanup(job,create_confirmed=confirmed,create_not_attempted=not attempted)
   if job.get('container_id'):host.require_native_cleanup(job['container_id'])
  except (RuntimeError,OSError,ValueError):
   journal.record(job,'cleanup_uncertain')
   raise RuntimeError('synthetic native cleanup remains uncertain') from None
 observed['synthetic_cleanup_verified']=True
 print(json.dumps(observed,sort_keys=True))
'''


def validate(evidence):
    if (not isinstance(evidence, dict) or evidence.get('synthetic_cleanup_verified') is not True
            or evidence.get('production_packaging_activated') is not False
            or type(evidence.get('native_enforcement_verified')) is not bool):
        raise RuntimeError('Synthetic native observation is unconfirmed')
    return evidence


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--operator-config', type=Path, required=True)
    args = parser.parse_args()
    compile(REMOTE_SOURCE, 'reviewed-synthetic-native-probe', 'exec')
    host = IAPValidationHost.model_validate_json(_protected(args.operator_config.resolve(), private=True).read_text())
    argv, environment = command(host)
    argv[-1] = shlex.join(['sudo', '-n', '/usr/bin/python3', '-I', '-B', '-c', REMOTE_SOURCE])
    result = subprocess.run(  # nosec B603
        argv, env=environment, capture_output=True, timeout=60, check=False,
    )
    if result.returncode or len(result.stdout) > 16384:
        raise RuntimeError('Native probe unconfirmed; reconcile the existing journal before another launch')
    print(json.dumps(validate(json.loads(result.stdout)), sort_keys=True))


if __name__ == '__main__':
    main()
