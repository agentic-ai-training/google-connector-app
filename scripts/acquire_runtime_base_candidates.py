"""Fixed public-runtime candidate acquisition on the existing pinned IAP host.

Defaults to read-only probe. Acquisition does not approve a runtime, load Docker,
write production policy, start an app, spend model credit, or extend hosting.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess  # nosec B404

from app.coding import runtime_base_acquisition as acquisition
from app.coding.validation_transport import IAPValidationHost, _protected, command


REVIEWED_PROGRAMS = {'4c84fe15aab840529d0647249f5bf26647128951b908b0b18d2280cab463df72',
                     'd657abde6f78ccd8ed2c2a84f32e87c85e14e74b6c20936a61b409816ea33649'}


def frozen_program(expected):
    if expected not in REVIEWED_PROGRAMS:
        raise RuntimeError('historical runtime acquisition controller is not reviewed')
    result = subprocess.run(  # nosec B603
        ['/usr/bin/git', '-C', str(Path(__file__).resolve().parents[1]), '--no-replace-objects',
         'cat-file', 'blob', 'refs/gca/runtime-acquisitions/' + expected],
        capture_output=True, timeout=10, check=False,
        env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'GIT_CONFIG_NOSYSTEM': '1',
             'GIT_CONFIG_GLOBAL': '/dev/null'})
    if (result.returncode or not 0 < len(result.stdout) <= 100000
            or hashlib.sha256(result.stdout).hexdigest() != expected):
        raise RuntimeError('historical runtime acquisition source custody differs')
    source = result.stdout.decode()
    compile(source, 'retained-reviewed-runtime-acquisition', 'exec')
    return source


REMOTE_SOURCE = r'''
import hashlib,json,os,pathlib,runpy,shutil,signal,sys,time,types
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()
def sha(value):return hashlib.sha256(value).hexdigest()
def main():
 body=sys.stdin.buffer.read(8193)
 request=json.loads(body,object_pairs_hook=g['unique_object'])
 if len(body)>8192 or not isinstance(request,dict) or set(request)!={'version','operation','authority','controller_sha256'}:
  raise RuntimeError('fixed runtime candidate request required')
 if type(request['version']) is not int or request['version']!=1 or request['operation'] not in {'probe','acquire'}:
  raise RuntimeError('fixed runtime candidate operation required')
 argv=pathlib.Path('/proc/self/cmdline').read_bytes().split(bytes([0]))
 if sha(argv[argv.index(b'-c')+1])!=request['controller_sha256']:
  raise RuntimeError('runtime acquisition controller differs')
 authority=request['authority']
 if not isinstance(authority,dict) or set(authority)!={'bundle_sha256','policy_sha256','registry_sha256','catalog_sha256'}:
  raise RuntimeError('runtime acquisition authority differs')
 g['target_identity']()
 policy,ph,rh=g['configuration']()
 expected={'bundle_sha256':g['snapshot'](),'policy_sha256':ph,'registry_sha256':rh,'catalog_sha256':sha(canonical(a.CATALOG))}
 if authority!=expected:raise RuntimeError('runtime acquisition source/configuration changed')
 root=pathlib.Path('/var/lib/gca-runtime-candidates-v1')
 with g['isolation'].Journal(pathlib.Path(policy['state_directory'])) as journal:
  journal.require_available()
  if g['snapshot']()!=authority['bundle_sha256'] or g['configuration']()!=(policy,ph,rh):
   raise RuntimeError('runtime acquisition configuration changed before ownership')
  host=g['isolation']._packaging_helper()
  host.require_host_limits()
  if request['operation']=='probe':
   receipts=[]
   if root.exists():
    a.protected(root)
    for role in sorted(a.CATALOG):
     path=root/role/'receipt.json'
     if path.exists():
      receipts.append(a.observe_candidate(root,role=role))
   return {'operation':'probe','catalog':a.CATALOG,'journal_available':True,'candidate_root_exists':root.exists(),'receipts':receipts,'runtime_approved':False,'docker_loaded':False}
  if int(time.time())+300>=g['STOP_UNIX']:raise RuntimeError('runtime acquisition hosting lifetime exhausted')
  if shutil.disk_usage(journal.directory).free<2*1024**3:raise RuntimeError('runtime acquisition disk headroom insufficient')
  root.mkdir(mode=0o700,exist_ok=True);a.protected(root)
  plan={'version':1,'authority':authority,'controller_sha256':request['controller_sha256'],'stop_unix':g['STOP_UNIX'],'runtime_approval_requested':False}
  plan_sha256=a.acquisition_plan(root,plan)
  signal.alarm(270)
  try:
   receipts=[]
   for role in sorted(a.CATALOG):
    registry=a.PublicRegistry(role,timeout_seconds=240)
    receipts.append(a.acquire_candidate(root,role=role,registry=registry))
   return {'operation':'acquire','receipts':receipts,'plan_sha256':plan_sha256,
    'runtime_approved':False,'docker_loaded':False,'application_deployed':False,'cloud_resource_created':False,
    'model_calls':0,'source_snapshot':g['snapshot'](),'journal_available':True}
  finally:signal.alarm(0)
try:print(json.dumps(main(),sort_keys=True))
except Exception as error:
 print(json.dumps({'operation':'failed','error_class':type(error).__name__},sort_keys=True))
 raise SystemExit(1)
'''


def program():
    body = Path(acquisition.__file__).read_text()
    compile(body, acquisition.__name__, 'exec')
    # Bootstrap protected OCI helpers first, then only this reviewed public client.
    prefix = ("import runpy,sys,types\n"
              "g=runpy.run_path('/usr/local/libexec/gca/operator_toolchain_host.py')\n"
              "a=types.ModuleType('app.coding.runtime_base_acquisition')\n"
              "sys.modules['app.coding.runtime_base_acquisition']=a\n"
              "exec(compile(" + repr(body) + ",'reviewed_runtime_acquisition','exec'),a.__dict__)\n")
    value = prefix + REMOTE_SOURCE
    if len(value.encode()) > 100000:
        raise RuntimeError('runtime acquisition source exceeds bound')
    return value


def verify(request, result):
    if (not isinstance(result, dict) or result.get('operation') != request['operation']
            or result.get('runtime_approved') is not False or result.get('docker_loaded') is not False
            or result.get('journal_available') is not True):
        raise RuntimeError('runtime candidate response is unconfirmed')
    if request['operation'] == 'acquire':
        if (result.get('application_deployed') is not False or result.get('cloud_resource_created') is not False
                or result.get('model_calls') != 0 or result.get('source_snapshot') != request['authority']['bundle_sha256']
                or not isinstance(result.get('receipts'), list) or len(result['receipts']) != len(acquisition.CATALOG)):
            raise RuntimeError('runtime candidate evidence differs')
        for role, receipt in zip(sorted(acquisition.CATALOG), result['receipts']):
            expected = acquisition.pin(role)
            if (not isinstance(receipt, dict) or receipt.get('role') != role
                    or any(receipt.get(key) != expected[key] for key in
                           ('index_digest', 'manifest_digest', 'config_digest', 'layer_bytes', 'layer_count'))
                    or receipt.get('rootfs_verified') is not True
                    or any(receipt.get(key) is not False for key in
                           ('runtime_approved', 'vulnerability_scan_verified', 'docker_loaded', 'application_behavior_verified'))):
                raise RuntimeError('runtime candidate receipt does not bind the reviewed catalog')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--operator-config', type=Path, required=True)
    parser.add_argument('--acquire', action='store_true')
    parser.add_argument('--reviewed-program-sha256', choices=sorted(REVIEWED_PROGRAMS))
    for name in ('bundle', 'policy', 'registry'):
        parser.add_argument('--expected-' + name + '-sha256', required=True)
    args = parser.parse_args()
    authority = {name + '_sha256': getattr(args, 'expected_' + name + '_sha256')
                 for name in ('bundle', 'policy', 'registry')}
    authority['catalog_sha256'] = hashlib.sha256(acquisition.oci.canonical(acquisition.CATALOG)).hexdigest()
    if any(not re.fullmatch('[a-f0-9]{64}', value) for value in authority.values()):
        parser.error('exact source/configuration hashes required')
    source = frozen_program(args.reviewed_program_sha256) if args.reviewed_program_sha256 else program()
    request = {'version': 1, 'operation': 'acquire' if args.acquire else 'probe', 'authority': authority,
               'controller_sha256': hashlib.sha256(source.encode()).hexdigest()}
    host = IAPValidationHost.model_validate_json(_protected(args.operator_config.resolve(), private=True).read_text())
    argv, environment = command(host)
    argv[-1] = shlex.join(['sudo', '-n', '/usr/bin/python3', '-I', '-B', '-c', source])
    result = subprocess.run(argv, input=acquisition.oci.canonical(request), env=environment,  # nosec B603
                            capture_output=True, timeout=300, check=False)
    if result.returncode or len(result.stdout) > 16384:
        raise RuntimeError('runtime acquisition unconfirmed; probe retained candidate files before retry')
    print(json.dumps(verify(request, json.loads(result.stdout)), sort_keys=True))


if __name__ == '__main__':
    main()
