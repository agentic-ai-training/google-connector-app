"""Native, keyless Alloy + CPU embedding capacity proof; no production rollout."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import time

from scripts.prepare_ollama_cpu import MODEL_MANIFEST_SHA, VERSION
from scripts import verify_release_capacity as base
from scripts.sidecar_capacity_fixture import METRICS

IMAGES = {'alloy': 'capacity-alloy:test', 'ollama': 'capacity-ollama:test',
          'probe': 'capacity-sidecar-probe:test'}
CGROUP_FILES = ('memory.current', 'memory.peak', 'memory.max', 'memory.swap.max',
                'pids.current', 'pids.max', 'memory.events')
CGROUP_SHELL = 'for f in '+ ' '.join(CGROUP_FILES) + '; do printf "%s\\n" "$f"; cat "/sys/fs/cgroup/$f" || exit 1; done'


def parse_cgroup(raw, page):
    lines = raw.decode('ascii').splitlines()
    values, current = {}, None
    for line in lines:
        if line in CGROUP_FILES:
            if line in values:
                raise base.CapacityError('unique_cgroup_files_required')
            current = line
            values[current] = []
        elif current:
            values[current].append(line)
        else:
            raise base.CapacityError('known_cgroup_header_required')
    try:
        events = dict(line.split() for line in values['memory.events'])
        names = dict(zip(CGROUP_FILES[:-1], ('current_bytes', 'peak_bytes', 'limit_bytes',
                         'swap_max_bytes', 'pids_current', 'pids_max')))
        result = {value: int(values[key][0]) for key, value in names.items() if len(values[key]) == 1}
        result.update(page_size=page, oom=int(events['oom']), oom_kill=int(events['oom_kill']))
        if len(result) != 9:
            raise ValueError('complete_measurement_required')
        return result
    except (ValueError, KeyError, IndexError):
        raise base.CapacityError('complete_numeric_cgroup_measurement_required') from None


def measure(container, fixture):
    writable = base.check_container(base.inspect('container', container, size=True), fixture.owner, fixture.network)
    # Native local Docker shares the Linux host kernel/page size. These sidecar
    # images need not install Python merely to let the capacity harness measure it.
    output = base.command('docker', 'exec', container, '/bin/sh', '-c', CGROUP_SHELL).stdout
    value = parse_cgroup(output, os.sysconf('SC_PAGE_SIZE'))
    base.validate_cgroup(value, role=container.removeprefix(fixture.network+'-'))
    return {**value, 'configured_memory_bytes': base.MEMORY_LIMIT, 'writable_layer_bytes': writable}


def request(probe, target, path, *, port, payload=None, timeout=5):
    args = ['docker', 'exec', probe, 'python', '-I', '/fixture.py', 'probe',
            '--url', f'http://{target}:{port}'+path, '--timeout', str(timeout)]
    if payload is not None:
        args += ['--payload', json.dumps(payload)]
    return base.decode(base.command(*args, timeout=timeout+10))


def verify_metrics(value):
    return (type(value.get('writes')) is int and value['writes'] > 0
            and type(value.get('series')) is int and value['series'] >= 6
            and value.get('validation_errors') == 0 and set(value.get('metrics', [])) == set(METRICS)
            and set(value.get('jobs', [])) == {'google-connector-api', 'google-connector-worker'}
            and set(value.get('scrapes', [])) == {'/metrics', '/monitoring/metrics'})


def embedding_inputs():
    # Match the CURRENT application envelope: a batch of eight inputs, each
    # clipped at 6,000 characters. Do not prove only a tiny hello-world embedding.
    sentence = 'Synthetic capacity text about headings, permissions, calendars and engineering workflows. '
    return [(f'Document {index}. '+sentence*100)[:6000] for index in range(8)]


def prove(source):
    if not re.fullmatch('[0-9a-f]{40}', source):
        raise base.CapacityError('exact_source_commit_required')
    info = base.decode(base.command('docker', 'info', '--format', '{{json .}}'))
    if info.get('OSType') != 'linux' or info.get('Architecture') not in {'x86_64', 'amd64'}:
        raise base.CapacityError('native_linux_amd64_docker_required')
    if shutil.disk_usage(Path.cwd()).free < 5_000_000_000:
        raise base.CapacityError('five_gb_free_before_disposable_runtime_fixture_required')
    images = {role: base.check_image(base.inspect('image', image), source) for role, image in IMAGES.items()}
    fixture = base.Fixture(source)
    report = {'source_commit': source, 'images': images,
              'scope': 'synthetic_metrics_and_current_embedding_batch_not_production_load_or_rollout',
              'runtime_external_calls_allowed': False, 'ollama_runtime_version': VERSION,
              'model_manifest_sha256': MODEL_MANIFEST_SHA}
    try:
        fixture.create_network()
        username, password = 'capacity', secrets.token_hex(24)
        probe = fixture.run('probe', IMAGES['probe'], {'SYNTHETIC_METRICS_USERNAME': username,
                                                     'SYNTHETIC_METRICS_PASSWORD': password})
        base.wait(lambda: request(probe, probe, '/proof', port=8080), phase='sidecar_probe_startup')
        alloy = fixture.run('alloy', IMAGES['alloy'], {
            'API_METRICS_TARGET': probe+':8080', 'WORKER_METRICS_TARGET': probe+':8080',
            'DEPLOYMENT_ENVIRONMENT': 'synthetic-capacity',
            'GRAFANA_CLOUD_PROMETHEUS_URL': 'http://'+probe+':8080/write',
            'GRAFANA_CLOUD_PROMETHEUS_USERNAME': username, 'GRAFANA_CLOUD_API_KEY': password})
        def metrics_delivered():
            value = request(probe, probe, '/proof', port=8080)
            return value if verify_metrics(value) else None
        report['alloy_delivery'] = base.wait(metrics_delivered, timeout=90, phase='filtered_authenticated_remote_write')
        ollama = fixture.run('ollama', IMAGES['ollama'], {})
        def model_ready():
            value = request(probe, ollama, '/api/tags', port=11434)
            models = value.get('models', [])
            return (len(models) == 1 and models[0].get('name') == 'nomic-embed-text:latest'
                    and models[0].get('digest') == MODEL_MANIFEST_SHA)
        base.wait(model_ready, phase='pinned_local_embedding_model_startup')
        started = time.monotonic()
        report['embedding_single'] = request(probe, ollama, '/api/embed', port=11434,
            payload={'model': 'nomic-embed-text', 'input': ['Synthetic capacity startup probe.'], 'truncate': False}, timeout=90)
        report['embedding_batch'] = request(probe, ollama, '/api/embed', port=11434,
            payload={'model': 'nomic-embed-text', 'input': embedding_inputs(), 'truncate': False}, timeout=180)
        report['embedding_probe_seconds'] = round(time.monotonic()-started, 3)
        report['embedding_batch'].update(input_count=8, characters_per_input=6000, truncation_allowed=False)
        report['runtime'] = {role: measure(container, fixture) for role, container in {
            'alloy': alloy, 'ollama': ollama, 'synthetic_probe': probe}.items()}
        report['alloy_delivery_final'] = request(probe, probe, '/proof', port=8080)
        if not verify_metrics(report['alloy_delivery_final']):
            raise base.CapacityError('continuous_metric_privacy_and_delivery_required')
    except BaseException:
        states = []
        for name in fixture.containers:
            try:
                value = base.inspect('container', name, optional=True)
                if value and (value.get('Config', {}).get('Labels') or {}).get(base.LABEL) == fixture.owner:
                    state = value.get('State') or {}
                    states.append({'role': name.removeprefix(fixture.network+'-'),
                                   **{key: state.get(key) for key in ('Status', 'Running', 'OOMKilled', 'ExitCode')}})
            except base.CapacityError:
                states.append({'role': name.removeprefix(fixture.network+'-'), 'inspection_uncertain': True})
        print(json.dumps({'sidecar_failure_container_states': states}), flush=True)
        raise
    finally:
        fixture.cleanup()
    report['owned_cleanup_verified'] = True
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-commit', required=True)
    args = parser.parse_args()
    result = prove(args.source_commit)
    fd = os.open('sidecar-capacity.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(result, output, indent=2, sort_keys=True)
        output.write('\n')
    print(json.dumps({'sidecar_capacity': 'passed', 'source_commit': args.source_commit,
                      'owned_cleanup_verified': True}), flush=True)


if __name__ == '__main__':
    main()
