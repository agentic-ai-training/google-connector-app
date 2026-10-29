"""Capacity verifier contracts; Docker/cloud commands are inert except public child tests."""
import json
from decimal import Decimal
import os
from pathlib import Path
import subprocess

import pytest
import yaml

from scripts import verify_release_capacity as capacity

SOURCE = 'a'*40
IMAGE = {'Os':'linux','Architecture':'amd64','Id':'sha256:'+'b'*64,'Size':900_000_000,
    'Config':{'Labels':{'org.opencontainers.image.revision':SOURCE}}}


@pytest.mark.parametrize('fault', ['none','arch','os','source','size','zero','digest'])
def test_image_identity_and_byte_limits(fault):
    value = json.loads(json.dumps(IMAGE))
    if fault != 'none':
        if fault == 'source':
            value['Config']['Labels']['org.opencontainers.image.revision'] = 'c'*40
        else:
            key, changed = {'arch':('Architecture','arm64'),'os':('Os','windows'),
                'size':('Size',4_000_000_000),'zero':('Size',0),'digest':('Id','tag-only')}[fault]
            value[key] = changed
        with pytest.raises(capacity.CapacityError):
            capacity.check_image(value,SOURCE)
    else:
        assert capacity.check_image(value,SOURCE)['uncompressed_bytes'] == 900_000_000


def container(owner, network):
    return {'Config':{'Labels':{capacity.LABEL:owner}},'State':{'Running':True,'OOMKilled':False},
        'HostConfig':{'Memory':1_000_000_000,'MemorySwap':1_000_000_000,'NanoCpus':2_000_000_000,
            'PidsLimit':1000,'PortBindings':{},'Privileged':False,'Binds':None},'SizeRw':10000,
        'Mounts':[],'NetworkSettings':{'Networks':{network:{}}}}


@pytest.mark.parametrize('fault', ['none','owner','memory','swap','cpu','pids','port','mount','network','oom','exit'])
def test_actual_runtime_limits_and_isolation_are_required(fault):
    value = container('owner','internal')
    if fault == 'owner': value['Config']['Labels'][capacity.LABEL] = 'another-owner'
    elif fault in {'memory','swap','cpu','pids','port'}:
        key = {'memory':'Memory','swap':'MemorySwap','cpu':'NanoCpus','pids':'PidsLimit','port':'PortBindings'}[fault]
        value['HostConfig'][key] = {'8000/tcp':[{'HostPort':'8000'}]} if fault == 'port' else 0
    elif fault == 'mount': value['Mounts'] = [{'Source':'/user/private'}]
    elif fault == 'network': value['NetworkSettings']['Networks']['bridge'] = {}
    elif fault == 'oom': value['State']['OOMKilled'] = True
    elif fault == 'exit': value['State']['Running'] = False
    if fault == 'none':
        assert capacity.check_container(value,'owner','internal') == 10000
    else:
        with pytest.raises(capacity.CapacityError): capacity.check_container(value,'owner','internal')


def test_owned_process_environment_excludes_all_credentials_and_literal_argv_is_not_shell_code(monkeypatch):
    monkeypatch.setenv('CODING_GROQ_API_KEY','SYNTHETIC_PRIVATE_KEY_MUST_NOT_EGRESS')
    monkeypatch.setenv('DOCKER_HOST','tcp://private-daemon.invalid')
    value = capacity.command('env').stdout.decode()
    assert 'CODING_GROQ_API_KEY' not in value and 'DOCKER_HOST' not in value
    literal = '; echo not-executable $(false)'
    assert capacity.command('printf','%s',literal).stdout.decode() == literal


def test_command_overflow_and_failure_never_echo_argv_or_private_output():
    with pytest.raises(capacity.CapacityError) as failure:
        capacity.command('sh','-c','echo SYNTHETIC_PRIVATE_ERROR; exit 2')
    assert 'SYNTHETIC_PRIVATE' not in str(failure.value)
    with pytest.raises(capacity.CapacityError):
        capacity.command('python3','-c','print("x"*2100000)')


@pytest.mark.parametrize('owned', [True,False])
def test_cleanup_only_removes_proven_owned_exact_resources(monkeypatch, owned):
    fixture = capacity.Fixture(SOURCE)
    fixture.network_planned = True
    fixture.containers = [fixture.network+'-api']
    removed, seen = [], set()
    def inspect(kind, name, **kwargs):
        if name in seen: return None
        label = fixture.owner if owned else 'someone-else'
        return {'Id':'f'*64,'Config':{'Labels':{capacity.LABEL:label}}} if kind == 'container' else {'Id':'e'*64,'Labels':{capacity.LABEL:label}}
    def command(*args, **kwargs):
        removed.append(args)
        seen.add(args[-1])
        return subprocess.CompletedProcess(args,0,stdout=b'',stderr=b'')
    monkeypatch.setattr(capacity,'inspect',inspect)
    monkeypatch.setattr(capacity,'command',command)
    if owned:
        fixture.cleanup()
        assert removed == [('docker','rm','--force','f'*64),('docker','network','rm','e'*64)]
    else:
        with pytest.raises(capacity.CapacityError): fixture.cleanup()
        assert removed == []


def test_lost_run_ack_retains_planned_name_for_reconciliation(monkeypatch):
    fixture = capacity.Fixture(SOURCE)
    def uncertain(*args, **kwargs):
        raise capacity.CapacityError('synthetic_lost_ack')
    monkeypatch.setattr(capacity,'command',uncertain)
    with pytest.raises(capacity.CapacityError): fixture.run('api','image',{})
    assert fixture.containers == [fixture.network+'-api']
    assert fixture.containers[0] in fixture.unconfirmed


def test_absence_after_unconfirmed_create_is_not_falsely_verified_cleanup(monkeypatch):
    fixture = capacity.Fixture(SOURCE)
    name = fixture.network+'-api'
    fixture.containers = [name]
    fixture.unconfirmed.add(name)
    monkeypatch.setattr(capacity,'inspect',lambda *args,**kwargs:None)
    with pytest.raises(capacity.CapacityError,match='cleanup_not_proven'):
        fixture.cleanup()


def test_fixture_commands_never_mount_or_publish_and_apply_exact_caps(monkeypatch):
    fixture, calls = capacity.Fixture(SOURCE), []
    def command(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args,0,stdout=b'owned-id',stderr=b'')
    monkeypatch.setattr(capacity,'command',command)
    fixture.run('api','image',{'SYNTHETIC':'value'})
    argv = calls[0]
    for flag,value in {'--memory':'1000000000','--memory-swap':'1000000000','--cpus':'2','--pids-limit':'1000'}.items():
        assert argv[argv.index(flag)+1] == value
    assert not any(flag in argv for flag in ('-v','--volume','--mount','-p','--publish','--privileged'))


def test_native_workflow_builds_and_measures_all_role_images_without_cloud_secrets():
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root/'.github/workflows/candidate-validation.yml').read_text())
    job = workflow['jobs']['backend']
    step = next(item for item in job['steps'] if item.get('name') == 'Validate candidate with no production credentials')
    text = step['run']
    for name in capacity.IMAGES.values(): assert name in text
    assert text.count('--label "org.opencontainers.image.revision=$CANDIDATE_HEAD_SHA"') == 4
    assert 'python -m scripts.verify_release_capacity --source-commit "$CANDIDATE_HEAD_SHA"' in text
    assert 'secrets.' not in str(job)
    assert any(item.get('with',{}).get('path') == 'release-capacity.json' for item in job['steps'])


@pytest.mark.parametrize('completion', [100,100.0,'100.00',Decimal('100.00')])
def test_real_json_numeric_serialization_is_not_mistaken_for_failed_completion(completion):
    value = {'status':'completed','model_call_count':0,'functional_completion':completion,
        'technical_completion':completion,'user_visible_completion':completion,
        'steps':[{'status':'completed','output_data':{'output':'Synthetic capabilities'}}]}
    # The actual route serializer uses json.dumps(default=str) for NUMERIC values.
    capacity.verify_completed_run(json.loads(json.dumps(value,default=str)))


@pytest.mark.parametrize('fault', ['partial','tokens','nan','bool','no-output','pending-step','empty-steps'])
def test_run_verifier_still_rejects_false_success(fault):
    value = {'status':'completed','model_call_count':0,'functional_completion':'100.00',
        'technical_completion':'100.00','user_visible_completion':'100.00',
        'steps':[{'status':'completed','output_data':{'output':'Synthetic'}}]}
    if fault in {'partial','nan','bool'}: value['functional_completion'] = {'partial':'50.00','nan':'NaN','bool':True}[fault]
    elif fault == 'tokens': value['model_call_count'] = 1
    elif fault == 'no-output': value['steps'][0]['output_data'] = {}
    elif fault == 'pending-step': value['steps'][0]['status'] = 'pending'
    else: value['steps'] = []
    with pytest.raises(capacity.CapacityError): capacity.verify_completed_run(value)


@pytest.mark.parametrize('kind,message', [('network','Error response from daemon: {"message":"network owned-id not found"}'),
    ('container','Error: No such container: owned-id'),('container','Error: No such object: owned-id')])
def test_actual_resource_specific_docker_absence_formats(monkeypatch,kind,message):
    monkeypatch.setattr(capacity,'command',lambda *args,**kwargs:subprocess.CompletedProcess(args,1,stdout=('[]\n'+message).encode(),stderr=b''))
    assert capacity.inspect(kind,'owned-id',optional=True) is None


@pytest.mark.parametrize('message', ['dial unix /var/run/docker.sock: no such file or directory',
    'Error: No such container: another-id','Error response from daemon: network another-id not found',
    'Cannot connect to Docker daemon', 'Error: No such object: owned-id-other',
    'Error response from daemon: network owned-id-other not found'])
def test_daemon_failure_or_other_resource_absence_cannot_prove_our_cleanup(monkeypatch,message):
    monkeypatch.setattr(capacity,'command',lambda *args,**kwargs:subprocess.CompletedProcess(args,1,stdout=message.encode(),stderr=b''))
    with pytest.raises(capacity.CapacityError): capacity.inspect('network','owned-id',optional=True)
