"""Fixed operator-only synthetic worker/barrier/storage proof on the existing VM.

No private source, provider keys, production dispatch, runtime approval or public
deployment. Default inspection starts no workload; --run uses an explicitly fake
base containing no executable and fixed nonsensitive output files.
"""
import argparse
import json
from pathlib import Path
import shlex
import subprocess  # nosec B404

from app.coding.validation_transport import IAPValidationHost, _protected, command


REMOTE_SOURCE = r'''
import gzip,hashlib,io,json,pathlib,runpy,shutil,signal,sys,tarfile,time,uuid
def tar_bytes(files):
 output=io.BytesIO()
 with tarfile.open(fileobj=output,mode='w') as archive:
  for name,body in sorted(files.items()):
   item=tarfile.TarInfo(name);item.size=len(body);item.mode=0o644
   archive.addfile(item,io.BytesIO(body))
 return output.getvalue()
def synthetic_data(host,validation):
 oci=host.oci
 raw=tar_bytes({'synthetic-runtime.txt':b'Not an executable application runtime. Operator test data only.'})
 layer=gzip.compress(raw,mtime=0)
 config=oci.canonical({'architecture':'amd64','os':'linux','config':{'Labels':{'synthetic-only':'true'}},
  'rootfs':{'type':'layers','diff_ids':[oci.digest(raw)]},'history':[]})
 manifest=oci.canonical({'schemaVersion':2,'mediaType':oci.OCI_MANIFEST,
  'config':{'digest':oci.digest(config),'size':len(config),'mediaType':oci.OCI_CONFIG},
  'layers':[{'digest':oci.digest(layer),'size':len(layer),'mediaType':oci.OCI_LAYER}]})
 base=oci.ApprovedRuntimeBase('nginx_static_v1',oci.digest(manifest),manifest,config,(layer,))
 base.inspect()
 artifact=tar_bytes({'dist/index.html':b'<html>Fixed nonsensitive assembly probe</html>',
  'dist/assets/probe.js':b'/* fixed operator test; never executed */'})
 inspected=host.artifacts.inspect_build_artifact(artifact,'react_build')
 now=int(time.time());claim=str(uuid.uuid4());validation_job=uuid.uuid4().hex
 source=hashlib.sha256(b'fixed-nonsensitive-packaging-probe-v1').hexdigest()
 reference={'artifact_id':validation_job+'-'+inspected.archive_sha256+'.tar',
  'archive_sha256':inspected.archive_sha256,'file_set_sha256':hashlib.sha256(json.dumps(
    inspected.file_hashes,separators=(',',':')).encode()).hexdigest(),
  'file_count':inspected.file_count,'content_bytes':inspected.content_bytes,'profile':'react_build',
  'validation_job_id':validation_job,'claim_id':claim,'source_hash':source,'expires_at_unix':now+300}
 runtime_policy=oci.canonical({'version':1,'roles':{'nginx_static_v1':{
  'manifest_digest':base.approved_manifest_digest,'layout_directory':'/opt/gca-synthetic-base'}}})
 binding={'version':1,'build_job_id':str(uuid.uuid4()),'dispatch_token':str(uuid.uuid4()),
  'request_hash':hashlib.sha256(b'operator-synthetic-only-no-production-authority').hexdigest(),
  'source_hash':source,'validation_claim_id':claim,'validation_job_id':validation_job,
  'profile':'react_build','artifact_sha256':reference['archive_sha256'],
  'base_manifest_digest':base.approved_manifest_digest,'runtime_policy_hash':hashlib.sha256(runtime_policy).hexdigest(),
  'execution_deadline_unix':now+180,'output_expires_at_unix':now+300,'timeout_seconds':45,
  'target_hash':hashlib.sha256(b'no-deployment-target').hexdigest(),
  'server_uuid':validation['server_uuid'],'project_id':'project-ee0c1944-144c-4364-ad4'}
 host.outputs.verify_binding(binding)
 bundle=pathlib.Path('/usr/local/libexec/gca')
 policy={'bundle_directory':str(bundle),'bundle_hashes':{
  name+'.py':hashlib.sha256(host._protected(bundle/(name+'.py')).read_bytes()).hexdigest()
  for name in host.BUNDLE_NAMES}}
 return artifact,reference,runtime_policy,base,binding,policy
def operate(request):
 if not isinstance(request,dict) or set(request)!={'operation'} or request['operation'] not in {'probe','run'}:
  raise RuntimeError('unsupported fixed synthetic operation')
 modules=runpy.run_path('/usr/local/libexec/gca/isolated_validation.py')
 validation=modules['load_policy'](pathlib.Path('/etc/gca-validation/policy.json'),None)
 host=modules['_packaging_helper']()
 evidence={'synthetic_artifact_only':True,'synthetic_base_not_runtime_approval':True,
  'production_packaging_activated':False,'authenticated_production_dispatch':False,
  'cloud_resource_created':False,'generated_code_executed':False,'model_calls':0,
  'worker_barrier_verified':False,'output_rehashed':False,'staging_removed':False}
 with modules['Journal'](pathlib.Path(validation['state_directory'])) as journal:
  journal.require_available()
  evidence['journal_idle']=True
  if request['operation']=='probe':return evidence
  host.require_host_limits();host.require_optional_sidecar_excluded()
  if shutil.disk_usage(journal.directory).free<host.VOLUME_BYTES+512*1024**2:
   raise RuntimeError('insufficient existing disk for bounded synthetic volume')
  image=validation['images']['python_compile'];modules['ensure_toolchain_image'](image)
  artifact,reference,runtime_policy,base,binding,policy=synthetic_data(host,validation)
  job=journal.begin('image_packaging',image,45,binding,purpose='image_packaging')
  attempted=confirmed=False
  signal.alarm(180)
  try:
   try:
    directory,mounts=host.prepare(journal,job,policy,binding,reference,artifact,runtime_policy,base)
    attempted=True
    cid=modules['docker'](*host.create_arguments(journal,job,mounts)).decode().strip()
    confirmed=True
    host.verify_container(journal,job,cid,mounts);journal.record(job,'running',cid)
    def started(child):
     host.publish_readiness(journal,job,cid,mounts,binding,directory/'work/input',child)
     evidence['native_readiness_published']=True
    result=modules['execute_bounded'](['start','-a',cid],45,output_limit=16384,on_started=started)
   finally:
    try:journal.cleanup(job,create_confirmed=confirmed,create_not_attempted=not attempted)
    except (RuntimeError,OSError,ValueError):
     journal.record(job,'cleanup_uncertain')
     raise RuntimeError('synthetic worker termination unconfirmed') from None
    evidence['execution_cleanup_verified']=True
   if result['success'] and not result['output_truncated']:
    recovered=host.recover(journal,binding)
    receipt=host.oci._metadata(result['stdout'].encode())
    expected={key:value for key,value in recovered.items()
     if key not in {'packaging_job_id','execution_cleanup_verified'}}
    if receipt!=expected:raise RuntimeError('synthetic worker receipt differs from host rehash')
    evidence['worker_barrier_verified']=True;evidence['output_rehashed']=True
    evidence['output_archive_sha256']=recovered['output_archive_sha256']
    evidence['output_archive_bytes']=recovered['output_archive_bytes']
   else:
    evidence['synthetic_worker_exit_code']=result['exit_code']
    evidence['synthetic_worker_stdout']=result['stdout'][:4096]
    evidence['synthetic_worker_stderr']=result['stderr'][:4096]
  finally:
   try:
    if evidence.get('execution_cleanup_verified') is True:
     removed=host.remove(journal,binding)
     evidence['staging_removed']=removed['staging_removed'] is True and removed['output_removed'] is True
   finally:signal.alarm(0)
  evidence['journal_idle']=not bool(journal.db.execute("SELECT 1 FROM jobs WHERE state<>'cleaned' LIMIT 1").fetchone())
 return evidence
def main():
 body=sys.stdin.buffer.read(16385)
 if len(body)>16384:raise RuntimeError('synthetic request exceeds bound')
 print(json.dumps(operate(json.loads(body)),sort_keys=True))
if __name__=='__main__':main()
'''


def validate(evidence, *, executed):
    if (not isinstance(evidence, dict) or evidence.get('journal_idle') is not True
            or any(evidence.get(key) is not True for key in
                   ('synthetic_artifact_only', 'synthetic_base_not_runtime_approval'))
            or any(evidence.get(key) is not False for key in
                   ('production_packaging_activated', 'authenticated_production_dispatch',
                    'cloud_resource_created', 'generated_code_executed'))
            or type(evidence.get('model_calls')) is not int or evidence['model_calls'] != 0
            or (executed and (evidence.get('execution_cleanup_verified') is not True
                              or evidence.get('staging_removed') is not True))):
        raise RuntimeError('Synthetic pipeline outcome unconfirmed; reconcile the existing journal')
    return evidence


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--operator-config', type=Path, required=True)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    compile(REMOTE_SOURCE, 'reviewed-fixed-synthetic-pipeline', 'exec')
    host = IAPValidationHost.model_validate_json(_protected(args.operator_config.resolve(), private=True).read_text())
    argv, environment = command(host)
    argv[-1] = shlex.join(['sudo', '-n', '/usr/bin/python3', '-I', '-B', '-c', REMOTE_SOURCE])
    result = subprocess.run(  # nosec B603
        argv, input=json.dumps({'operation': 'run' if args.run else 'probe'}).encode(),
        env=environment, capture_output=True, timeout=210 if args.run else 60, check=False,
    )
    if result.returncode or len(result.stdout) > 16384:
        raise RuntimeError('Synthetic pipeline unconfirmed; inspect existing journal, never blind redispatch')
    print(json.dumps(validate(json.loads(result.stdout), executed=args.run), sort_keys=True))


if __name__ == '__main__':
    main()
