"""Fixed operator-only nonsensitive gVisor environment observation.

Consumes the existing host journal slot, persists a synthetic intent and always
uses the approved offline Python image. No private input, provider key or generated
module. This observes guest/host differences; it does not certify packaging or
relax a failed check. No new VM, policy edit or financial activation.
"""
import argparse
import json
from pathlib import Path
import shlex
import subprocess  # nosec B404

from app.coding.validation_transport import IAPValidationHost, _protected, command


REMOTE_PROBE = r'''
import json,pathlib,runpy
modules=runpy.run_path('/usr/local/libexec/gca/isolated_validation.py')
policy=modules['load_policy'](pathlib.Path('/etc/gca-validation/policy.json'),None)
image=policy['images']['python_compile']
guest="""import json,pathlib,runpy
p=pathlib.Path('/proc/self/status')
status=dict(line.split(':',1) for line in p.read_text().splitlines() if ':' in line)
values={}
for name in ('memory.max','memory.swap.max','pids.max','cpu.max'):
 try: values[name]=(pathlib.Path('/sys/fs/cgroup')/name).read_text().strip()
 except OSError: values[name]='unavailable'
try:
 m=runpy.run_path('/gca/bundle/image_packaging_worker.py')
 m['require_enforcement']()
 passed=True
except RuntimeError:
 passed=False
print(json.dumps({'virtual_proc':{key:status.get(key,'unavailable').strip() for key in ('CapEff','NoNewPrivs','Seccomp')},'guest_cgroup':values,'worker_enforcement_passed':passed},sort_keys=True))
"""
with modules['Journal'](pathlib.Path(policy['state_directory'])) as journal:
 modules['ensure_toolchain_image'](image)
 job=journal.begin('python_compile',image,20)
 attempted=False
 confirmed=False
 try:
  attempted=True
  cid=modules['docker']('create','--name',job['name'],'--pull=never','--runtime=runsc',
    '--label=gca.purpose=validation','--label=gca.scope='+journal.scope,'--label=gca.job='+job['id'],
    '--network=none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges:true',
    '--user=65532:65532','--ipc=private','--cgroupns=private','--memory=3g','--memory-swap=3g',
    '--cpus=1','--pids-limit=64','--ulimit=nofile=64:64','--log-driver=none',
    '--mount','type=bind,src=/usr/local/libexec/gca,dst=/gca/bundle,readonly',
    '--entrypoint=/usr/bin/env',image,'-i','PATH=/usr/local/bin:/usr/bin:/bin',
    'HOME=/nonexistent','CI=true','LANG=C.UTF-8','PYTHONDONTWRITEBYTECODE=1',
    'python3','-I','-B','-c',guest).decode().strip()
  confirmed=True
  journal.verify_identity(job,cid)
  journal.record(job,'running',cid)
  result=modules['execute_bounded'](['start','-a',cid],20,output_limit=16384)
  spec=json.loads(modules['docker']('inspect',cid))[0]
  pid=spec['State']['Pid']
  # The process has exited; the snapshot is configuration, not live host-kernel
  # cgroup enforcement proof. Do not convert it into that claim.
  if not result['success'] or result['output_truncated']:
   # Only this fixed nonsensitive program runs here. No source, data, credential
   # or model-selected command is mounted. Return bounded exception diagnostics,
   # never turn a failed process into positive enforcement evidence.
   observed={'observation_complete':False,'synthetic_stderr':result['stderr'][:4096],
             'synthetic_stdout':result['stdout'][:4096]}
  else:
   observed=json.loads(result['stdout'])
   observed['observation_complete']=True
  observed['host_config']={key:spec['HostConfig'].get(key) for key in
      ('Runtime','Memory','MemorySwap','NanoCpus','PidsLimit','CgroupnsMode','CgroupParent','SecurityOpt')}
  observed['exited_host_pid']=pid
 finally:
  journal.cleanup(job,create_confirmed=confirmed or not attempted)
 observed['synthetic_cleanup_verified']=True
 observed['native_enforcement_verified']=False
 observed['production_packaging_activated']=False
 print(json.dumps(observed,sort_keys=True))
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--operator-config", type=Path, required=True)
    args = parser.parse_args()
    host = IAPValidationHost.model_validate_json(_protected(args.operator_config.resolve(), private=True).read_text())
    compile(REMOTE_PROBE, "reviewed-packaging-environment-probe", "exec")
    argv, environment = command(host)
    argv[-1] = shlex.join(["sudo", "-n", "/usr/bin/python3", "-I", "-B", "-c", REMOTE_PROBE])
    result = subprocess.run(  # nosec B603
        argv, capture_output=True, env=environment, timeout=60, check=False,
    )
    if result.returncode or len(result.stdout) > 16384:
        raise RuntimeError("Synthetic packaging environment unconfirmed; observe host journal before another launch")
    evidence = json.loads(result.stdout)
    if (evidence.get("synthetic_cleanup_verified") is not True
            or evidence.get("native_enforcement_verified") is not False
            or evidence.get("production_packaging_activated") is not False):
        raise RuntimeError("Synthetic probe cleanup is unconfirmed")
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    main()
