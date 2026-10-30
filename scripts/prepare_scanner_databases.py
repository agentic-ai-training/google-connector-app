"""Operator-only public data preparation on the existing fixed IAP host."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess  # nosec B404

from app.coding import scanner_database as database, runtime_base_acquisition as cas
from app.coding.validation_transport import IAPValidationHost, _protected, command


REVIEWED_PROGRAMS = {"b7726790ee87433b7af1093488a58ad80e588b8639fe722f0ca4f2e051dffe3f"}


REMOTE_SOURCE = r'''
import hashlib,json,pathlib,shutil,signal,time
def sha(body):return hashlib.sha256(body).hexdigest()
def main():
 body=sys.stdin.buffer.read(8193)
 request=json.loads(body,object_pairs_hook=g['unique_object'])
 if len(body)>8192 or not isinstance(request,dict) or set(request)!={'version','operation','authority','controller_sha256','roles'}:
  raise RuntimeError('fixed scanner database request required')
 if type(request['version']) is not int or request['version']!=1 or request['operation'] not in {'probe','acquire'}:
  raise RuntimeError('fixed scanner database operation required')
 if request['roles']!=sorted(set(request['roles'])) or not request['roles'] or set(request['roles'])-set(d.CATALOG):
  raise RuntimeError('fixed scanner database roles required')
 argv=pathlib.Path('/proc/self/cmdline').read_bytes().split(bytes([0]))
 if sha(argv[argv.index(b'-c')+1])!=request['controller_sha256']:
  raise RuntimeError('scanner database controller differs')
 g['target_identity']()
 policy,ph,rh=g['configuration']()
 expected={'bundle_sha256':g['snapshot'](),'policy_sha256':ph,'registry_sha256':rh,'catalog_sha256':sha(a.oci.canonical(d.CATALOG))}
 if request['authority']!=expected:raise RuntimeError('scanner database source/configuration differs')
 root=pathlib.Path('/var/lib/gca-scanner-databases-v1')
 with g['isolation'].Journal(pathlib.Path(policy['state_directory'])) as journal:
  journal.require_available()
  if g['snapshot']()!=expected['bundle_sha256'] or g['configuration']()!=(policy,ph,rh):
   raise RuntimeError('scanner database authority changed before ownership')
  host=g['isolation']._packaging_helper();host.require_host_limits()
  if int(time.time())+300>=g['STOP_UNIX']:raise RuntimeError('scanner database host lifetime exhausted')
  if request['operation']=='acquire':
   if shutil.disk_usage(journal.directory).free<12*1024**3:raise RuntimeError('scanner database disk headroom insufficient')
   root.mkdir(mode=0o700,exist_ok=True)
  if not root.exists():return {'operation':'probe','store_exists':False,'receipts':[],'journal_available':True,'scan_completed':False,'runtime_approved':False}
  a.protected(root);d.store_bytes(root)
  snapshot=root/expected['catalog_sha256']
  if request['operation']=='acquire':snapshot.mkdir(mode=0o700,exist_ok=True)
  receipts=[]
  if not snapshot.exists():return {'operation':'probe','store_exists':True,'current_catalog_exists':False,'receipts':[],'journal_available':True,'scan_completed':False,'runtime_approved':False}
  a.protected(snapshot)
  if request['operation']=='acquire':
   plan={'version':1,'authority':expected,'controller_sha256':request['controller_sha256'],'stop_unix':g['STOP_UNIX'],'runtime_approval_requested':False}
   a.acquisition_plan(snapshot,plan)
  signal.alarm(270)
  try:
   for role in request['roles']:
    if request['operation']=='acquire':
     d.acquire_database(snapshot,role,d.PublicDatabase(role),now=d.datetime.now(d.timezone.utc),store_root=root)
    if (snapshot/role/'receipt.json').exists():
     receipts.append(d.observe_database(snapshot,role,now=d.datetime.now(d.timezone.utc)))
   return {'operation':request['operation'],'store_exists':True,'current_catalog_exists':True,'receipts':receipts,
    'journal_available':True,'staged_bytes':d.store_bytes(root),'source_snapshot':g['snapshot'](),
    'scan_completed':False,'runtime_approved':False,'cloud_resource_created':False,'model_calls':0}
  finally:signal.alarm(0)
try:print(json.dumps(main(),sort_keys=True))
except Exception as error:
 print(json.dumps({'operation':'failed','error_class':type(error).__name__},sort_keys=True))
 raise SystemExit(1)
'''


def program():
    prefix = "import runpy,sys,types\ng=runpy.run_path('/usr/local/libexec/gca/operator_toolchain_host.py')\n"
    for name, module, alias in (("runtime_base_acquisition", cas, "a"),
                                ("scanner_database", database, "d")):
        body = Path(module.__file__).read_text()
        compile(body, name, "exec")
        prefix += (alias + "=types.ModuleType('app.coding." + name + "')\n"
                   "sys.modules['app.coding." + name + "']=" + alias + "\n"
                   "exec(compile(" + repr(body) + "," + repr("reviewed_" + name)
                   + ",'exec')," + alias + ".__dict__)\n")
    result = prefix + REMOTE_SOURCE
    if len(result.encode()) > 100000:
        raise RuntimeError("scanner database source exceeds its transport bound")
    return result


def frozen_program(expected):
    if expected not in REVIEWED_PROGRAMS:
        raise RuntimeError("scanner database controller is not source reviewed")
    result = subprocess.run(  # nosec B603
        ["/usr/bin/git", "-C", str(Path(__file__).resolve().parents[1]), "--no-replace-objects",
         "cat-file", "blob", "refs/gca/scanner-databases/" + expected],
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_CONFIG_GLOBAL": "/dev/null"}, capture_output=True, timeout=10, check=False)
    if result.returncode or hashlib.sha256(result.stdout).hexdigest() != expected:
        raise RuntimeError("scanner database retained source differs")
    compile(result.stdout, "reviewed_scanner_database_controller", "exec")
    return result.stdout.decode()


def verify(request, result):
    if (not isinstance(result, dict) or result.get("operation") != request["operation"]
            or result.get("journal_available") is not True or result.get("scan_completed") is not False
            or result.get("runtime_approved") is not False or not isinstance(result.get("receipts"), list)):
        raise RuntimeError("scanner database observation is unconfirmed")
    if request["operation"] == "acquire":
        if (result.get("source_snapshot") != request["authority"]["bundle_sha256"]
                or result.get("cloud_resource_created") is not False or result.get("model_calls") != 0
                or [x.get("role") for x in result["receipts"]] != request["roles"]):
            raise RuntimeError("scanner database acquisition is incomplete")
        for receipt in result["receipts"]:
            if (receipt.get("catalog") != database.pin(receipt["role"])
                    or any(receipt.get(k) is not False for k in
                           ("publisher_signature_verified", "database_opened_by_scanner",
                            "scan_completed", "runtime_approved"))):
                raise RuntimeError("scanner database evidence differs from its catalog")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--operator-config", type=Path, required=True)
    parser.add_argument("--acquire", action="store_true")
    parser.add_argument("--role", action="append", choices=sorted(database.CATALOG))
    parser.add_argument("--reviewed-program-sha256", choices=sorted(REVIEWED_PROGRAMS))
    for name in ("bundle", "policy", "registry"):
        parser.add_argument("--expected-" + name + "-sha256", required=True)
    args = parser.parse_args()
    if args.acquire and args.reviewed_program_sha256 is None:
        parser.error("acquisition requires retained source-reviewed controller")
    authority = {name + "_sha256": getattr(args, "expected_" + name + "_sha256")
                 for name in ("bundle", "policy", "registry")}
    authority["catalog_sha256"] = hashlib.sha256(cas.oci.canonical(database.CATALOG)).hexdigest()
    if any(not re.fullmatch("[a-f0-9]{64}", value) for value in authority.values()):
        parser.error("exact source/configuration hashes required")
    source = frozen_program(args.reviewed_program_sha256) if args.reviewed_program_sha256 else program()
    request = {"version": 1, "operation": "acquire" if args.acquire else "probe",
               "authority": authority, "controller_sha256": hashlib.sha256(source.encode()).hexdigest(),
               "roles": sorted(set(args.role or database.CATALOG))}
    host = IAPValidationHost.model_validate_json(_protected(args.operator_config.resolve(), private=True).read_text())
    argv, environment = command(host)
    argv[-1] = shlex.join(["sudo", "-n", "/usr/bin/python3", "-I", "-B", "-c", source])
    response = subprocess.run(argv, input=cas.oci.canonical(request), env=environment,  # nosec B603
                              capture_output=True, timeout=300, check=False)
    if response.returncode or len(response.stdout) > 16384:
        raise RuntimeError("scanner database preparation unconfirmed; probe retained public files before retry")
    print(json.dumps(verify(request, json.loads(response.stdout)), sort_keys=True))


if __name__ == "__main__":
    main()
