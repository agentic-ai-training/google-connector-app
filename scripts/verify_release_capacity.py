"""Native Linux image/startup proof with isolated, owned synthetic Docker data.

No cloud/model credentials, host mounts, public ports, external network or user
database. Not an LLM/RAG/Google load test or proof of every Railway service.
"""
import argparse
import asyncio
import base64
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time
from uuid import uuid4

from app.coding.owned_process import OwnedProcessError, run_owned

LABEL = 'gca.release-capacity'
IMAGE_LIMIT = 4_000_000_000
MEMORY_LIMIT = 1_000_000_000
IMAGES = {'api': 'control-api:test', 'worker': 'candidate-worker:test',
          'candidate': 'candidate-api:test', 'builder': 'candidate-builder:test'}


class CapacityError(RuntimeError):
    pass


def command(*args, timeout=30, check=True):
    # Never inherit API keys, Docker auth config, proxy or remote daemon selectors.
    env = {'PATH': os.environ.get('PATH', ''), 'HOME': os.environ.get('HOME', ''),
           'DOCKER_CONFIG': '/nonexistent-gca-docker-config'}
    try:
        # Fixed shell program only merges streams. All command text remains argv,
        # never interpolated shell code; public owned-process cleanup is reused.
        code, output = asyncio.run(run_owned(['bash','--noprofile','--norc','-c',
            'exec "$@" 2>&1','gca-capacity-command', *args], cwd=Path.cwd(), env=env,
            timeout=timeout, output_limit=2_000_000))
        result = subprocess.CompletedProcess(args, code, stdout=output, stderr=b'')
    except (OwnedProcessError, OSError):
        raise CapacityError('bounded_command_unavailable_or_uncertain') from None
    if len(result.stdout) + len(result.stderr) > 2_000_000:
        raise CapacityError('bounded_command_output_required')
    if check and result.returncode:
        raise CapacityError('command_failed_without_printing_arguments_or_private_output')
    return result


def decode(result):
    try:
        return json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise CapacityError('valid_inert_docker_json_required') from None


def inspect(kind, identity, *, size=False, optional=False):
    args = ['docker', kind, 'inspect'] + (['--size'] if size else []) + [identity]
    result = command(*args, check=not optional)
    if result.returncode:
        # A missing daemon/socket is NOT proof a resource is absent. Recognize
        # only canonical resource-specific messages naming this exact object.
        message = (result.stdout+result.stderr).decode(errors='replace').lower()
        identifier = identity.lower()
        missing = (f'no such {kind}: {identifier}', f'no such object: {identifier}')
        if kind == 'network':
            missing += (f'network {identifier} not found',)
        if optional and any(re.search(re.escape(value) + r"(?=$|[\s\"'])", message) for value in missing):
            return None
        raise CapacityError('resource_absence_or_identity_not_proven')
    values = decode(result)
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
        raise CapacityError('one_exact_resource_required')
    return values[0]


def check_image(value, source):
    if (value.get('Os') != 'linux' or value.get('Architecture') != 'amd64'
            or not re.fullmatch('sha256:[0-9a-f]{64}', value.get('Id', ''))
            or type(value.get('Size')) is not int or not 0 < value['Size'] < IMAGE_LIMIT
            or (value.get('Config', {}).get('Labels') or {}).get('org.opencontainers.image.revision') != source):
        raise CapacityError('native_source_labelled_image_under_four_billion_bytes_required')
    return {'id': value['Id'], 'os': 'linux', 'architecture': 'amd64', 'uncompressed_bytes': value['Size']}


def check_container(value, owner, network):
    if (value.get('Config', {}).get('Labels') or {}).get(LABEL) != owner:
        raise CapacityError('owned_container_identity_required')
    host = value.get('HostConfig') or {}
    state = value.get('State') or {}
    if (state.get('Running') is not True or state.get('OOMKilled') is not False
            or host.get('Memory') != MEMORY_LIMIT or host.get('MemorySwap') != MEMORY_LIMIT
            or host.get('NanoCpus') != 2_000_000_000 or host.get('PidsLimit') != 1000
            or host.get('PortBindings') or host.get('Privileged')
            or host.get('Binds') or value.get('Mounts')
            or set((value.get('NetworkSettings') or {}).get('Networks') or {}) != {network}):
        raise CapacityError('running_owned_unprivileged_unpublished_resource_limited_container_required')
    writable = value.get('SizeRw')
    if type(writable) is not int or not 0 <= writable < 1_000_000_000:
        raise CapacityError('writable_layer_under_one_billion_bytes_required')
    return writable


def jwt_token(secret):
    def encoded(value):
        return base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).rstrip(b'=')
    message = encoded({'alg': 'HS256', 'typ': 'JWT'}) + b'.' + encoded({
        'sub': 'capacity@example.invalid', 'admin': False, 'exp': int(time.time()) + 600})
    return (message + b'.' + base64.urlsafe_b64encode(hmac.new(secret.encode(), message, hashlib.sha256).digest()).rstrip(b'=')).decode()


def verify_completed_run(value):
    """Validate the real API's JSON NUMERIC representation, not Python DB types."""
    try:
        complete = all(not isinstance(value.get(key), bool)
            and Decimal(str(value[key])).is_finite() and Decimal(str(value[key])) == 100
            for key in ('functional_completion','technical_completion','user_visible_completion'))
    except (InvalidOperation, ValueError, KeyError, TypeError):
        complete = False
    steps = value.get('steps')
    if (value.get('status') != 'completed' or type(value.get('model_call_count')) is not int
            or value['model_call_count'] != 0 or not complete
            or not isinstance(steps, list) or not steps
            or any(not isinstance(step,dict) or step.get('status') != 'completed'
                or not isinstance(step.get('output_data'),dict) or not step['output_data'].get('output') for step in steps)):
        raise CapacityError('truthful_verified_deterministic_run_required')


HTTP_PROBE = '''import json,sys,urllib.request
url,method,body,token=sys.argv[1:]
headers={'Content-Type':'application/json'}
if token: headers['Authorization']='Bearer '+token
request=urllib.request.Request(url,data=body.encode() if body else None,headers=headers,method=method)
with urllib.request.urlopen(request,timeout=5) as response:
 data=response.read(262145)
 if len(data)>262144: raise RuntimeError('bounded health/run response required')
 print(data.decode())
'''
CGROUP_PROBE = '''import json,pathlib,os
p=pathlib.Path('/sys/fs/cgroup')
def number(name):
 value=(p/name).read_text().strip()
 return int(value)
events=dict(line.split() for line in (p/'memory.events').read_text().splitlines())
print(json.dumps({'page_size':os.sysconf('SC_PAGE_SIZE'),'current_bytes':number('memory.current'),'peak_bytes':number('memory.peak'),
 'limit_bytes':number('memory.max'),'swap_max_bytes':number('memory.swap.max'),
 'pids_current':number('pids.current'),'pids_max':number('pids.max'),
 'oom':int(events.get('oom',0)),'oom_kill':int(events.get('oom_kill',0))}))
'''


class Fixture:
    def __init__(self, source):
        self.source = source
        self.owner = uuid4().hex
        self.network = 'gca-capacity-' + self.owner
        self.containers = []
        self.network_planned = False
        self.unconfirmed = set()

    def create_network(self):
        self.network_planned = True  # Write-ahead ownership even if the CLI ACK is lost.
        self.unconfirmed.add(self.network)
        command('docker', 'network', 'create', '--internal', '--label', LABEL+'='+self.owner, self.network)
        self.unconfirmed.discard(self.network)
        value = inspect('network', self.network)
        if value.get('Internal') is not True or (value.get('Labels') or {}).get(LABEL) != self.owner:
            raise CapacityError('owned_internal_network_required')

    def run(self, role, image, environment):
        name = self.network + '-' + role
        self.containers.append(name)  # Reconcile by exact name + label on all failures.
        self.unconfirmed.add(name)
        args = ['docker', 'run', '--detach', '--name', name, '--label', LABEL+'='+self.owner,
            '--network', self.network, '--memory', str(MEMORY_LIMIT), '--memory-swap', str(MEMORY_LIMIT),
            '--cpus', '2', '--pids-limit', '1000']
        if role == 'db':
            args += ['--tmpfs', '/var/lib/postgresql/data:rw,size=268435456', '--shm-size', '67108864']
        for key, value in sorted(environment.items()):
            args += ['--env', key+'='+value]
        command(*args, image, timeout=45)
        self.unconfirmed.discard(name)
        return name

    def cleanup(self):
        errors = []
        for name in reversed(self.containers):
            try:
                value = inspect('container', name, optional=True)
                if value is None:
                    if name in self.unconfirmed:
                        raise CapacityError('unconfirmed_create_absence_does_not_prove_cleanup')
                    continue
                if (value.get('Config', {}).get('Labels') or {}).get(LABEL) != self.owner:
                    raise CapacityError('refuse_to_delete_unowned_container')
                identity = value.get('Id', '')
                if not re.fullmatch('[0-9a-f]{64}', identity):
                    raise CapacityError('immutable_owned_container_id_required')
                command('docker', 'rm', '--force', identity, timeout=20)
                if inspect('container', identity, optional=True) is not None:
                    raise CapacityError('container_cleanup_not_proven')
            except CapacityError:
                errors.append('container_cleanup_not_proven')
        if self.network_planned:
            try:
                value = inspect('network', self.network, optional=True)
                if value is None and self.network in self.unconfirmed:
                    raise CapacityError('unconfirmed_network_create_absence_does_not_prove_cleanup')
                if value is not None:
                    if (value.get('Labels') or {}).get(LABEL) != self.owner:
                        raise CapacityError('refuse_to_delete_unowned_network')
                    identity = value.get('Id', '')
                    if not re.fullmatch('[0-9a-f]{64}', identity):
                        raise CapacityError('immutable_owned_network_id_required')
                    command('docker', 'network', 'rm', identity, timeout=15)
                    if inspect('network', identity, optional=True) is not None:
                        raise CapacityError('network_cleanup_not_proven')
            except CapacityError:
                errors.append('network_cleanup_not_proven')
        if errors:
            raise CapacityError(','.join(sorted(set(errors))))


def wait(predicate, *, timeout=90, phase='readiness'):
    deadline = time.monotonic() + timeout
    last_error = 'not_ready'
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except CapacityError as exc:
            last_error = str(exc)
        time.sleep(1)
    raise CapacityError(phase+'_bounded_deadline_exceeded:'+last_error)


def request(container, path, *, method='GET', payload=None, token='', port=8000):
    return decode(command('docker', 'exec', container, 'python', '-I', '-c', HTTP_PROBE,
        f'http://127.0.0.1:{port}'+path, method, json.dumps(payload) if payload else '', token, timeout=10))


def measure(container, fixture):
    writable = check_container(inspect('container', container, size=True), fixture.owner, fixture.network)
    value = decode(command('docker', 'exec', container, 'python', '-I', '-c', CGROUP_PROBE))
    validate_cgroup(value, role=container.removeprefix(fixture.network+'-'))
    return {**value, 'configured_memory_bytes':MEMORY_LIMIT, 'writable_layer_bytes': writable}


def validate_cgroup(value, *, role='synthetic'):
    page = value.get('page_size')
    if type(page) is not int or not 4096 <= page <= 65536 or page & (page-1):
        raise CapacityError('valid_kernel_page_size_required')
    # memory.max counts whole pages. Keep Docker's exact byte ceiling unchanged;
    # accept only its kernel-rounded DOWN value, never extra memory or swap.
    expected = MEMORY_LIMIT // page * page
    if (value.get('limit_bytes') != expected or value.get('swap_max_bytes') != 0
            or value.get('pids_max') != 1000 or value.get('oom') != 0 or value.get('oom_kill') != 0
            or not 0 < value.get('current_bytes', 0) <= value.get('peak_bytes', 0) <= expected*4//5
            or not 0 < value.get('pids_current', 0) < 1000):
        print(json.dumps({'capacity_measurement_failed':role,'expected_page_aligned_limit':expected,
            'cgroup':{key:value.get(key) for key in ('page_size','current_bytes','peak_bytes','limit_bytes',
                'swap_max_bytes','pids_current','pids_max','oom','oom_kill') if type(value.get(key)) is int}}), flush=True)
        raise CapacityError('cgroup_v2_bounds_no_oom_and_twenty_percent_smoke_headroom_required')


def prove(source):
    if not re.fullmatch('[0-9a-f]{40}', source):
        raise CapacityError('exact_source_commit_required')
    info = decode(command('docker', 'info', '--format', '{{json .}}'))
    if info.get('OSType') != 'linux' or info.get('Architecture') not in {'x86_64', 'amd64'}:
        raise CapacityError('native_linux_amd64_docker_required')
    if shutil.disk_usage(Path.cwd()).free < 5_000_000_000:
        raise CapacityError('five_gb_free_before_disposable_runtime_fixture_required')
    report = {'source_commit': source, 'images': {role: check_image(inspect('image', image), source)
        for role, image in IMAGES.items()}, 'external_calls_allowed': False,
        'scope': 'synthetic_startup_and_deterministic_run_not_live_models_google_rag_or_all_services'}
    fixture = Fixture(source)
    secret = secrets.token_hex(32)  # Ephemeral fixture-only JWT secret; never persisted.
    try:
        fixture.create_network()
        password = secrets.token_hex(16)
        db = fixture.run('db', 'pgvector/pgvector:pg16', {'POSTGRES_USER':'capacity_user',
            'POSTGRES_PASSWORD':password, 'POSTGRES_DB':'gca_capacity'})
        wait(lambda: command('docker','exec',db,'pg_isready','-U','capacity_user','-d','gca_capacity',check=False).returncode == 0,
            phase='disposable_postgres_startup')
        dsn = 'postgresql://capacity_user:'+password+'@'+db+':5432/gca_capacity'
        common = {'DATABASE_URL':dsn, 'ASYNC_DATABASE_URL':dsn, 'JWT_SECRET_KEY':secret,
            'ALLOW_DEV_AUTH':'false', 'RUNTIME_MODEL_PROVIDER':'gemini', 'RUNTIME_API_KEY':'synthetic-capacity-no-network-key',
            'DEPLOYMENT_VERSION':source, 'EXECUTOR_VERSION':source, 'EXECUTOR_ROLE':'control',
            'LANGCHAIN_TRACING_V2':'false', 'LANGSMITH_TRACING':'false', 'OTEL_ENABLED':'false',
            'CODING_AGENT_ENABLED':'false', 'CODING_WORKER_ENABLED':'false', 'GOVERNED_IMPROVEMENTS_ENABLED':'false',
            'EMBEDDED_WORKER_ENABLED':'false'}
        api = fixture.run('api', IMAGES['api'], common)
        def healthy_api():
            value = request(api, '/health')
            return value if value.get('status') == 'ok' and value.get('executor_version') == source and value.get('executor_role') == 'control' else None
        wait(healthy_api, phase='control_api_startup')
        worker = fixture.run('worker', IMAGES['worker'], common)
        def worker_ready():
            raw = command('docker','logs','--tail','100',worker)
            text = (raw.stdout+raw.stderr).decode(errors='replace')
            return 'worker_ready role=control' in text and 'executor_version='+source in text
        wait(worker_ready, phase='control_worker_startup')
        token = jwt_token(secret)
        accepted = request(api, '/runs', method='POST', token=token,
            payload={'message':'What can you do?', 'session_id':'capacity-synthetic', 'idempotency_key':fixture.owner})
        identity = accepted.get('run_id', '')
        if not re.fullmatch('[0-9a-f-]{36}', identity):
            raise CapacityError('durable_synthetic_run_not_accepted')
        def completed():
            value = request(api, '/runs/'+identity, token=token)
            if value.get('status') in {'failed','partial','cancelled'}:
                raise CapacityError('synthetic_run_failed')
            return value if value.get('status') == 'completed' else None
        run = wait(completed, timeout=60, phase='synthetic_durable_run')
        verify_completed_run(run)
        candidate = fixture.run('candidate', IMAGES['candidate'], {**common,'EXECUTOR_ROLE':'candidate',
            'EMBEDDED_WORKER_ENABLED':'true'})
        def healthy_candidate():
            value = request(candidate,'/health')
            return value.get('status') == 'ok' and value.get('executor_role') == 'candidate' and value.get('executor_version') == source
        wait(healthy_candidate, phase='candidate_api_startup')
        builder_dsn = dsn+'?application_name=gca-capacity-builder'
        builder = fixture.run('builder', IMAGES['builder'], {**common,
            'DATABASE_URL':builder_dsn,'ASYNC_DATABASE_URL':builder_dsn})
        # Builder has no model key or queued jobs: only idle initialization is measured.
        def builder_initialized():
            if not inspect('container',builder)['State'].get('Running'):
                return False
            value = command('docker','exec',db,'psql','-U','capacity_user','-d','gca_capacity','-Atc',
                "SELECT count(*) FROM pg_stat_activity WHERE application_name='gca-capacity-builder'")
            return int(value.stdout.strip()) >= 2  # asyncpg's two-connection pool, not Alembic's transient connection.
        wait(builder_initialized, phase='builder_idle_initialization')
        time.sleep(5)
        report['runtime'] = {role: measure(name, fixture) for role, name in {
            'api':api,'worker':worker,'candidate':candidate,'builder_idle_only':builder}.items()}
        report['synthetic_durable_run'] = {'status':'completed','model_calls':0,'functional_completion':100}
    except BaseException as exc:
        # Whitelist only content-free state. Never echo Env, arbitrary logs, argv
        # or a private database URL while diagnosing startup.
        states = []
        for name in fixture.containers:
            try:
                value = inspect('container', name, optional=True)
                if value and (value.get('Config', {}).get('Labels') or {}).get(LABEL) == fixture.owner:
                    state = value.get('State') or {}
                    states.append({'role':name.removeprefix(fixture.network+'-'),
                        **{key:state.get(key) for key in ('Status','Running','OOMKilled','ExitCode')}})
            except CapacityError:
                states.append({'role':name.removeprefix(fixture.network+'-'),'inspection':'unavailable'})
        print(json.dumps({'capacity_status':'failed','error':str(exc) if isinstance(exc,CapacityError) else type(exc).__name__,
            'owned_container_states':states}), flush=True)
        raise
    finally:
        fixture.cleanup()
    report['cleanup_verified'] = True
    report['status'] = 'passed'
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--output', default='release-capacity.json')
    args = parser.parse_args()
    result = prove(args.source_commit)
    # Refuse overwrite/symlink; the report contains only whitelisted synthetic facts.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(result, handle, sort_keys=True, indent=2)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except CapacityError as exc:
        raise SystemExit(str(exc)) from None
