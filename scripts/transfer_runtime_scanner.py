"""Fixed IAP custody transfer of signature-bound public data; no scanner launch."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess  # nosec B404

from app.coding import runtime_base_acquisition as cas, scanner_custody as custody
from app.coding.validation_transport import IAPValidationHost, _protected, command


REVIEWED_PROGRAMS = {"83eaa6e1d1723dc8ad828b711ed9b633c1d524f0879057496651538d6e85d406"}

REMOTE_SOURCE = r'''
import hashlib,json,pathlib,shutil,signal,time
def sha(body):return hashlib.sha256(body).hexdigest()
def main():
 body=sys.argv[1].encode()
 request=json.loads(body,object_pairs_hook=g['unique_object'])
 fields={'version','operation','authority','controller_sha256','offset','prefix_sha256'}
 if len(body)>8192 or not isinstance(request,dict) or set(request)!=fields:
  raise RuntimeError('fixed scanner custody envelope required')
 if type(request['version']) is not int or request['version']!=1 or request['operation'] not in {'probe','transfer'}:
  raise RuntimeError('fixed scanner custody operation required')
 argv=pathlib.Path('/proc/self/cmdline').read_bytes().split(bytes([0]))
 if sha(argv[argv.index(b'-c')+1])!=request['controller_sha256']:
  raise RuntimeError('scanner custody source differs')
 g['target_identity']()
 policy,ph,rh=g['configuration']()
 expected={'bundle_sha256':g['snapshot'](),'policy_sha256':ph,'registry_sha256':rh,
  'binary_sha256':c.BINARY_SHA256,'signature_receipt_sha256':c.SIGNATURE_RECEIPT_SHA256}
 if request['authority']!=expected:raise RuntimeError('scanner custody authority differs')
 with g['isolation'].Journal(pathlib.Path(policy['state_directory'])) as journal:
  journal.require_available()
  if g['snapshot']()!=expected['bundle_sha256'] or g['configuration']()!=(policy,ph,rh):
   raise RuntimeError('scanner custody authority changed before ownership')
  host=g['isolation']._packaging_helper();host.require_host_limits()
  if int(time.time())+300>=g['STOP_UNIX']:raise RuntimeError('scanner custody lifetime exhausted')
  root=pathlib.Path('/var/lib/gca-runtime-scanner-v1')
  if request['operation']=='transfer':
   if shutil.disk_usage(journal.directory).free<4*1024**3:raise RuntimeError('scanner custody disk headroom insufficient')
   root.mkdir(mode=0o700,exist_ok=True)
  if not root.exists():return {'operation':'probe','store_exists':False,'custody':{'complete':False,'offset':0,'prefix_sha256':sha(b'')},'scanner_executed':False,'scan_completed':False,'runtime_approved':False}
  a.protected(root)
  snapshot=root/c.BINARY_SHA256
  if request['operation']=='transfer':snapshot.mkdir(mode=0o700,exist_ok=True)
  if not snapshot.exists():return {'operation':'probe','store_exists':True,'custody':{'complete':False,'offset':0,'prefix_sha256':sha(b'')},'scanner_executed':False,'scan_completed':False,'runtime_approved':False}
  a.protected(snapshot)
  if request['operation']=='transfer':
   a.write_exact(snapshot/'plan.json',a.oci.canonical({'version':1,'authority':expected,'controller_sha256':request['controller_sha256'],'stop_unix':g['STOP_UNIX'],'runtime_approval_requested':False}))
  signal.alarm(270)
  try:
   value=c.receive(snapshot,sys.stdin.buffer,offset=request['offset'],prefix_sha256=request['prefix_sha256']) if request['operation']=='transfer' else c.observe(snapshot)
   return {'operation':request['operation'],'store_exists':True,'custody':value,'source_snapshot':g['snapshot'](),'scanner_executed':False,'scan_completed':False,'runtime_approved':False}
  finally:signal.alarm(0)
try:print(json.dumps(main(),sort_keys=True))
except Exception as error:
 print(json.dumps({'operation':'failed','error_class':type(error).__name__},sort_keys=True))
 raise SystemExit(1)
'''


def program():
    prefix = "import runpy,sys,types\ng=runpy.run_path('/usr/local/libexec/gca/operator_toolchain_host.py')\n"
    for name, module, alias in (("runtime_base_acquisition", cas, "a"),
                                ("scanner_custody", custody, "c")):
        body = Path(module.__file__).read_text()
        compile(body, name, "exec")
        prefix += (alias + "=types.ModuleType('app.coding." + name + "')\n"
                   "sys.modules['app.coding." + name + "']=" + alias + "\n"
                   "exec(compile(" + repr(body) + "," + repr("reviewed_" + name)
                   + ",'exec')," + alias + ".__dict__)\n")
    source = prefix + REMOTE_SOURCE
    if len(source.encode()) > 100000:
        raise RuntimeError("scanner custody source exceeds transport bound")
    return source


def frozen_program(checksum):
    if checksum not in REVIEWED_PROGRAMS:
        raise RuntimeError("scanner custody controller is not source reviewed")
    result = subprocess.run(  # nosec B603
        ["/usr/bin/git", "-C", str(Path(__file__).resolve().parents[1]), "--no-replace-objects",
         "cat-file", "blob", "refs/gca/scanner-custody/" + checksum],
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_CONFIG_GLOBAL": "/dev/null"}, capture_output=True, timeout=10, check=False)
    if result.returncode or hashlib.sha256(result.stdout).hexdigest() != checksum:
        raise RuntimeError("retained scanner custody source differs")
    compile(result.stdout, "reviewed_scanner_custody_controller", "exec")
    return result.stdout.decode()


def verify(request, result):
    if (not isinstance(result, dict) or result.get("operation") != request["operation"]
            or any(result.get(key) is not False for key in
                   ("scanner_executed", "scan_completed", "runtime_approved"))):
        raise RuntimeError("scanner custody outcome unconfirmed")
    state = result.get("custody")
    if (not isinstance(state, dict) or type(state.get("complete")) is not bool
            or type(state.get("offset")) is not int or not 0 <= state["offset"] <= custody.BINARY_BYTES
            or not isinstance(state.get("prefix_sha256"), str)
            or not re.fullmatch("[a-f0-9]{64}", state["prefix_sha256"])):
        raise RuntimeError("scanner custody prefix evidence differs")
    if request["operation"] == "transfer" and (
            state.get("receipt") != custody.receipt() or not state["complete"]
            or state["offset"] != custody.BINARY_BYTES or state["prefix_sha256"] != custody.BINARY_SHA256
            or result.get("source_snapshot") != request["authority"]["bundle_sha256"]):
        raise RuntimeError("scanner custody transfer proof differs")
    return result


def invoke(host, source, request, stream=None):
    argv, environment = command(host)
    argv[-1] = shlex.join(["sudo", "-n", "/usr/bin/python3", "-I", "-B", "-c", source,
                          cas.oci.canonical(request).decode()])
    response = subprocess.run(argv, stdin=stream or subprocess.DEVNULL, env=environment,  # nosec B603
                              capture_output=True, timeout=300, check=False)
    if response.returncode or len(response.stdout) > 16384:
        raise RuntimeError("scanner custody unconfirmed; inspect retained partial before retry")
    return verify(request, json.loads(response.stdout))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--operator-config", type=Path, required=True)
    parser.add_argument("--transfer", action="store_true")
    parser.add_argument("--reviewed-program-sha256", choices=sorted(REVIEWED_PROGRAMS))
    for name in ("bundle", "policy", "registry"):
        parser.add_argument("--expected-" + name + "-sha256", required=True)
    args = parser.parse_args()
    if args.transfer and args.reviewed_program_sha256 is None:
        parser.error("transfer requires retained source-reviewed controller")
    authority = {name + "_sha256": getattr(args, "expected_" + name + "_sha256")
                 for name in ("bundle", "policy", "registry")}
    if any(not re.fullmatch("[a-f0-9]{64}", value) for value in authority.values()):
        parser.error("exact host source/configuration fingerprints required")
    authority.update(binary_sha256=custody.BINARY_SHA256,
                     signature_receipt_sha256=custody.SIGNATURE_RECEIPT_SHA256)
    source = frozen_program(args.reviewed_program_sha256) if args.reviewed_program_sha256 else program()
    request = {"version": 1, "operation": "probe", "authority": authority,
               "controller_sha256": hashlib.sha256(source.encode()).hexdigest(),
               "offset": 0, "prefix_sha256": hashlib.sha256(b"").hexdigest()}
    host = IAPValidationHost.model_validate_json(_protected(args.operator_config.resolve(), private=True).read_text())
    result = invoke(host, source, request)
    if args.transfer:
        local = Path(__file__).resolve().parents[1] / ".gca-local/runtime-scanner-assets-v1"
        with custody.verified_local_source(local) as stream:
            offset = result["custody"]["offset"]
            if custody.fingerprint(stream, offset) != result["custody"]["prefix_sha256"]:
                raise RuntimeError("remote scanner partial does not match verified local prefix")
            request.update(operation="transfer", offset=offset,
                           prefix_sha256=result["custody"]["prefix_sha256"])
            stream.seek(offset)
            result = invoke(host, source, request, stream)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
