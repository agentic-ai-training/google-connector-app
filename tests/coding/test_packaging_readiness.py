"""Bound readiness/kernel decision tests; native observations are simulated."""
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.coding import isolated_packaging as host, image_packaging_worker as worker
from app.coding import oci_packager as oci
from tests.coding.test_image_packaging_worker import context as context_fixture

context = context_fixture
CID = 'b' * 64


def proof_for(binding):
    return dict(version=2, runtime='runsc', native_profile=host.NATIVE_PROFILE,
                binding_hash=host.hashlib.sha256(oci.canonical(binding)).hexdigest(),
                container_id=CID, native_pid=4242, native_start_ticks=100,
                native_cgroup='/gcapackaging.slice/docker-' + CID + '.scope',
                memory_max=3 * 1024**3, swap_max=0, cpu_quota=100000, cpu_period=100000, tasks_max=64,
                aggregate_memory_max=3 * 1024**3, aggregate_swap_max=0,
                aggregate_cpu_quota=100000, aggregate_cpu_period=100000, aggregate_tasks_max=128,
                native_seccomp=2, native_cap_eff=0x8001F, native_no_new_privs=1, helper_pids=[4242, 4243],
                observed_at_unix=int(time.time()), expires_at_unix=binding['execution_deadline_unix'])


@pytest.mark.parametrize('field,value', [
    ('binding_hash', '0' * 64), ('native_seccomp', 0), ('native_cap_eff', 1), ('native_no_new_privs', 0),
    ('runtime', 'runc'), ('native_pid', 0), ('native_start_ticks', True), ('tasks_max', 65),
    ('aggregate_tasks_max', 129), ('memory_max', 4 * 1024**3), ('aggregate_swap_max', 1),
    ('cpu_quota', 200000), ('native_cgroup', '/system.slice/docker-other.scope'),
    ('expires_at_unix', 0), ('observed_at_unix', 0), ('helper_pids', []), ('helper_pids', [4242, True]),
    ('version', 1), ('native_profile', 'model_supplied_profile'), ('native_cap_eff', 0),
])
def test_forged_cross_job_expired_or_unbounded_readiness_rejected(context, field, value):
    binding = context[-1]
    proof = {**proof_for(binding), field: value}
    with pytest.raises(worker.PackagingWorkerError, match='readiness proof'):
        worker.verify_host_proof(binding, proof)


def test_current_proof_is_bound_but_not_standalone_authentication(context):
    proof = proof_for(context[-1])
    assert worker.verify_host_proof(context[-1], proof) == proof


def test_header_cannot_switch_dispatch_after_readiness_before_body(context):
    binding = context[-1]
    header = {'operation': 'assemble', 'binding': binding, 'reference': context[4], 'archive_bytes': len(context[3])}

    class MetadataOnly(io.BytesIO):
        def read(self, *args):
            pytest.fail('private artifact body consumed before dispatch lineage check')

    with pytest.raises(worker.PackagingWorkerError, match='readiness dispatch'):
        worker.serve(MetadataOnly(oci.canonical(header) + b'\n' + context[3]),
                     runtime_policy_path=context[1], output_root=context[0], workspace=context[2],
                     expected_binding={**binding, 'request_hash': '0' * 64})


@pytest.mark.parametrize('case', ['readonly_proof', 'writable_input', 'native_only', 'wrong_path'])
def test_gvisor_guard_requires_readonly_host_barrier_not_virtual_cgroups(context, monkeypatch, case):
    monkeypatch.setattr(worker.sys, 'platform', 'linux')
    monkeypatch.setattr(worker.os, 'geteuid', lambda: 65532)
    monkeypatch.setattr(worker.os, 'environ', {'PATH': '/usr/bin:/bin'})
    checked = []
    monkeypatch.setattr(worker, 'wait_for_host_proof', lambda *args: checked.append(args))
    monkeypatch.setattr(worker.resource, 'setrlimit', lambda *args: None)
    original_read = Path.read_text
    original_iter = Path.iterdir

    def read(path, *args, **kwargs):
        if path == Path('/proc/self/status'):
            return 'CapEff: 0000000000000000\nNoNewPrivs: 1\nSeccomp: 0'
        if path == Path('/proc/self/mountinfo'):
            mode = 'rw' if case == 'writable_input' else 'ro'
            return '1 2 0:1 / / ro - overlay overlay ro\n2 1 0:2 / /gca/input ' + mode + ' - bind bind ' + mode
        if path.is_relative_to('/sys/fs/cgroup'):
            pytest.fail('emulated cgroup file used as native authority')
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read)
    monkeypatch.setattr(Path, 'iterdir', lambda path: iter([Path('/sys/class/net/lo')])
                        if path == Path('/sys/class/net') else original_iter(path))
    proof_path = None if case == 'native_only' else Path('/gca/input/host-proof.json')
    if case == 'wrong_path':
        proof_path = Path('/gca/workspace/forged-proof.json')
    if case == 'readonly_proof':
        worker.require_enforcement(binding=context[-1], proof_path=proof_path, workspace=Path('/gca/workspace'))
        assert checked == [(context[-1], proof_path, Path('/gca/workspace'))]
    else:
        with pytest.raises(worker.PackagingWorkerError):
            worker.require_enforcement(binding=context[-1], proof_path=proof_path, workspace=Path('/gca/workspace'))
        assert not checked


def test_missing_proof_has_a_bounded_wait_and_consumes_no_artifact(context, monkeypatch):
    times = iter([100.0, 100.0, 101.0, 120.0])
    monkeypatch.setattr(worker.time, 'monotonic', lambda: next(times))
    monkeypatch.setattr(worker.time, 'sleep', lambda seconds: None)

    def absent(*args, **kwargs):
        raise FileNotFoundError('synthetic readiness absent')

    monkeypatch.setattr(worker.runtimes, '_read', absent)
    with pytest.raises(worker.PackagingWorkerError, match='readiness deadline'):
        worker.wait_for_host_proof(context[-1], Path('/gca/input/host-proof.json'), Path('/gca/workspace'))


@pytest.fixture
def kernel(monkeypatch):
    identity = '/gcapackaging.slice/docker-' + CID + '.scope'
    values = {identity + '/memory.max': str(3 * 1024**3), identity + '/memory.swap.max': '0',
              identity + '/cpu.max': '100000 100000', identity + '/pids.max': '64',
              identity + '/cgroup.procs': '4242\n4243',
              '/gcapackaging.slice/memory.max': str(3 * 1024**3), '/gcapackaging.slice/memory.swap.max': '0',
              '/gcapackaging.slice/cpu.max': '100000 100000', '/gcapackaging.slice/pids.max': '128',
              '/proc/4242/status': 'CapEff: 000000000008001f\nCapPrm: 000000000008001f\nCapBnd: 000000000008001f\n'
                                   'Uid: 0 0 0 0\nGid: 0 0 0 0\nNoNewPrivs: 1\nSeccomp: 2',
              '/proc/4243/status': 'CapEff: 000000000004001f\nCapPrm: 000000000004001f\nCapBnd: 000000000004001f\n'
                                   'Uid: 0 0 0 0\nGid: 0 0 0 0\nNoNewPrivs: 1\nSeccomp: 2',
              '/proc/4242/cgroup': '0::' + identity, '/proc/4243/cgroup': '0::' + identity,
              '/proc/4242/cmdline': '/usr/bin/runsc\0boot\0' + CID,
              '/proc/4243/cmdline': '/usr/bin/runsc\0gofer\0' + CID,
              '/proc/self/uid_map': '0 0 4294967295'}
    for pid in (4242, 4243):
        for name in ('uid_map', 'gid_map'):
            values[f'/proc/{pid}/{name}'] = '0 0 4294967295'

    def read(path, maximum=65536):
        name = str(path)
        return values[name.removeprefix('/sys/fs/cgroup') if name.startswith('/sys/fs/cgroup') else name]

    monkeypatch.setattr(host, '_native_text', read)
    monkeypatch.setattr(host, 'require_optional_sidecar_excluded', lambda: None)
    monkeypatch.setattr(host, '_start_ticks', lambda pid: 100)
    monkeypatch.setattr(host.os, 'pidfd_open', lambda pid, flags: 1000, raising=False)
    real_close = host.os.close
    monkeypatch.setattr(host.os, 'close', lambda fd: None if fd == 1000 else real_close(fd))
    monkeypatch.setattr(host, '_native_binary', lambda process, cache=None: ('gofer', 0x4001F)
                        if process.name == '4243' else ('boot', 0x8001F))

    def readlink(path):
        if str(path).endswith('/exe'):
            return '/usr/bin/gvisor-bin/gvisor_sentry' if '/4242/' in str(path) else '/usr/bin/runsc'
        return 'namespace:[4000]' if '/self/' in str(path) or '/1/' in str(path) else 'namespace:[5000]'

    monkeypatch.setattr(host.os, 'readlink', readlink)
    original_stat = Path.stat
    monkeypatch.setattr(Path, 'stat', lambda path, *args, **kwargs:
                        SimpleNamespace(st_dev=-1, st_ino=42) if str(path) in {'/proc/4242/root', '/proc/4243/root'}
                        else original_stat(path, *args, **kwargs))
    monkeypatch.setattr(host.select, 'poll', lambda: type('Poll', (), {
        'register': lambda *args: None, 'poll': lambda *args: []})())
    original_iter = Path.iterdir
    monkeypatch.setattr(Path, 'iterdir', lambda path: iter([Path('/proc/4242'), Path('/proc/4243')])
                        if path == Path('/proc') else original_iter(path))
    return values, {'Id': CID, 'State': {'Running': True, 'Pid': 4242}}, identity


def test_native_proof_requires_live_pid_and_actual_host_limits(context, kernel):
    proof = host.verify_native_worker(kernel[1], context[-1])
    assert proof == {**proof_for(context[-1]), 'observed_at_unix': proof['observed_at_unix']}


@pytest.mark.parametrize('case', ['dead', 'wrong_group', 'no_filter', 'swap', 'cpu', 'helper_outside', 'pid_reuse'])
def test_native_kernel_mismatch_cannot_publish_readiness(context, kernel, monkeypatch, case):
    values, spec, identity = kernel
    if case == 'dead':
        spec['State']['Running'] = False
    elif case == 'wrong_group':
        values['/proc/4242/cgroup'] = '0::/another-tenant.slice'
    elif case == 'no_filter':
        values['/proc/4242/status'] = values['/proc/4242/status'].replace('Seccomp: 2', 'Seccomp: 0')
    elif case == 'swap':
        values[identity + '/memory.swap.max'] = '1'
    elif case == 'cpu':
        values['/gcapackaging.slice/cpu.max'] = '200000 100000'
    elif case == 'helper_outside':
        values['/proc/4243/cgroup'] = '0::/system.slice/unbounded-helper.scope'
    else:
        ticks = iter([100, 100, 100, 100, 100, 200])
        monkeypatch.setattr(host, '_start_ticks', lambda pid: next(ticks))
    with pytest.raises(host.PackagingHostError):
        host.verify_native_worker(spec, context[-1])


@pytest.mark.parametrize('case', ['extra_capability', 'permitted_mismatch', 'uid_map', 'same_namespace',
                                 'host_root', 'wrong_role', 'missing_primary', 'unknown_scope_member',
                                 'nested_controller', 'controller_mapping'])
def test_native_role_profile_mismatches_are_not_accepted(context, kernel, monkeypatch, case):
    values, spec, _ = kernel
    if case == 'extra_capability':
        values['/proc/4242/status'] = values['/proc/4242/status'].replace('000000000008001f', '000000000008003f')
    elif case == 'permitted_mismatch':
        values['/proc/4243/status'] = values['/proc/4243/status'].replace('CapPrm: 000000000004001f', 'CapPrm: 0')
    elif case == 'uid_map':
        values['/proc/4243/uid_map'] = '0 1000 1'
    elif case == 'same_namespace':
        original = host.os.readlink
        monkeypatch.setattr(host.os, 'readlink', lambda path: 'namespace:[4000]'
                            if str(path) == '/proc/4243/ns/user' else original(path))
    elif case == 'host_root':
        original = Path.stat
        monkeypatch.setattr(Path, 'stat', lambda path, *args, **kwargs: original(Path('/'))
                            if str(path) == '/proc/4242/root' else original(path, *args, **kwargs))
    elif case == 'wrong_role':
        values['/proc/4242/cmdline'] = '/usr/bin/gvisor-bin/gvisor_sentry\0unapproved-role\0' + CID
    elif case == 'missing_primary':
        monkeypatch.setattr(host, 'native_helpers', lambda cid: [(4243, values['/proc/4243/cgroup'])])
    elif case == 'nested_controller':
        original = host.os.readlink
        monkeypatch.setattr(host.os, 'readlink', lambda path: 'namespace:[6000]'
                            if str(path) == '/proc/self/ns/user' else original(path))
    elif case == 'controller_mapping':
        values['/proc/self/uid_map'] = '0 1000 1'
    else:
        values[kernel[2] + '/cgroup.procs'] += '\n4244'
    with pytest.raises(host.PackagingHostError):
        host.verify_native_worker(spec, context[-1])


def test_docker_absence_cannot_hide_surviving_native_helpers(kernel):
    with pytest.raises(host.PackagingHostError, match='helpers remain'):
        host.require_native_cleanup(CID)


@pytest.mark.parametrize('events', ['populated 1\nfrozen 0', 'frozen 0', 'populated 0\nfrozen 0', None])
def test_native_cleanup_requires_empty_or_removed_resource_group(monkeypatch, events):
    monkeypatch.setattr(host, 'native_helpers', lambda container_id: [])

    def read(path, maximum=65536):
        assert str(path) == '/sys/fs/cgroup/gcapackaging.slice/docker-' + CID + '.scope/cgroup.events'
        if events is None:
            raise FileNotFoundError('synthetic removed resource group')
        return events

    monkeypatch.setattr(host, '_native_text', read)
    if events is None or 'populated 0' in events:
        host.require_native_cleanup(CID)
    else:
        with pytest.raises(host.PackagingHostError, match='remains populated'):
            host.require_native_cleanup(CID)


def test_native_kernel_reads_are_bounded_before_allocation(tmp_path):
    path = tmp_path / 'synthetic-kernel-file'
    path.write_bytes(b'a' * 100)
    with pytest.raises(host.PackagingHostError, match='exceeds bound'):
        host._native_text(path, 16)


@pytest.mark.parametrize('binary', ['/usr/bin/gvisor-bin/runsc-fd-parking', '/unapproved/runtime-helper'])
def test_scope_inventory_never_hides_unapproved_executable(kernel, monkeypatch, binary):
    values, _, identity = kernel
    values['/proc/4244/cgroup'] = '0::' + identity
    original_iter = Path.iterdir
    monkeypatch.setattr(Path, 'iterdir', lambda path: iter([Path('/proc/4244')])
                        if path == Path('/proc') else original_iter(path))
    monkeypatch.setattr(host.os, 'readlink', lambda path: binary)
    assert host.native_helpers(CID) == [(4244, '0::' + identity)]
    with pytest.raises(host.PackagingHostError, match='helpers remain'):
        host.require_native_cleanup(CID)


def test_known_parking_helper_outside_scope_remains_visible_for_cleanup(kernel, monkeypatch):
    values, _, _ = kernel
    values['/proc/4244/cgroup'] = '0::/unexpected-scope'
    values['/proc/4244/cmdline'] = 'runsc-fd-parking'
    original_iter = Path.iterdir
    monkeypatch.setattr(Path, 'iterdir', lambda path: iter([Path('/proc/4244')])
                        if path == Path('/proc') else original_iter(path))
    monkeypatch.setattr(host.os, 'readlink', lambda path: '/usr/bin/gvisor-bin/runsc-fd-parking')
    assert host.native_helpers(CID) == [(4244, '0::/unexpected-scope')]


@pytest.mark.parametrize('case', ['excluded', 're_enabled', 'changed_digest'])
def test_optional_sidecar_exclusion_is_a_pinned_readiness_prerequisite(tmp_path, monkeypatch, case):
    binary = tmp_path / 'parking'
    binary.write_bytes(b'\x7fELF\xff\x00pinned-binary')
    binary.chmod(0o755 if case == 're_enabled' else 0o644)
    monkeypatch.setattr(host, '_protected', lambda path: binary)
    monkeypatch.setattr(host, 'OPTIONAL_SIDECAR_HASH',
                        '0' * 64 if case == 'changed_digest' else host.hashlib.sha256(binary.read_bytes()).hexdigest())
    original_stat = Path.stat

    def stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path == binary:
            return SimpleNamespace(st_uid=0, st_mode=result.st_mode, st_nlink=1, st_size=result.st_size)
        return result

    monkeypatch.setattr(Path, 'stat', stat)
    if case == 'excluded':
        host.require_optional_sidecar_excluded()
    else:
        with pytest.raises(host.PackagingHostError, match='optional native sidecar'):
            host.require_optional_sidecar_excluded()


@pytest.mark.parametrize('case', ['approved', 'changed_digest', 'nonroot_owner', 'writable', 'foreign_inode'])
def test_native_executable_requires_exact_package_and_live_inode(tmp_path, monkeypatch, case):
    binary = tmp_path / 'pinned-binary'
    binary.write_bytes(b'synthetic-reviewed-native-binary')
    process = tmp_path / 'process'
    process.mkdir()
    (process / 'exe').symlink_to(binary)
    fixed = '/usr/bin/gvisor-bin/gvisor_sentry'
    expected = host.hashlib.sha256(binary.read_bytes()).hexdigest()
    if case == 'changed_digest':
        expected = '0' * 64
    monkeypatch.setitem(host.NATIVE_BINARIES, fixed, ('boot', expected, 0x8001F))
    monkeypatch.setattr(host.os, 'readlink', lambda path: fixed)
    monkeypatch.setattr(host, '_protected', lambda path: binary)
    original = Path.stat

    def stat(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == binary:
            return SimpleNamespace(st_uid=501 if case == 'nonroot_owner' else 0,
                                   st_mode=0o100777 if case == 'writable' else 0o100755,
                                   st_size=result.st_size, st_dev=result.st_dev, st_ino=result.st_ino)
        if path == process / 'exe' and case == 'foreign_inode':
            return SimpleNamespace(st_dev=result.st_dev, st_ino=result.st_ino + 1)
        return result

    monkeypatch.setattr(Path, 'stat', stat)
    if case == 'approved':
        assert host._native_binary(process) == ('boot', 0x8001F)
    else:
        with pytest.raises(host.PackagingHostError):
            host._native_binary(process)


@pytest.mark.parametrize('case', ['approved', 'foreign_parent', 'cycle', 'nonempty_unknown_role'])
def test_empty_argv_native_clone_requires_pinned_sentry_ancestry(context, kernel, monkeypatch, case):
    values, spec, identity = kernel
    values[identity + '/cgroup.procs'] += '\n4244'
    values['/proc/4244/status'] = values['/proc/4242/status'] + '\nPPid: 4242'
    values['/proc/4244/cgroup'] = '0::' + identity
    values['/proc/4244/cmdline'] = ''
    values['/proc/4244/uid_map'] = values['/proc/4244/gid_map'] = '0 0 4294967295'
    monkeypatch.setattr(host, 'native_helpers', lambda cid: [(pid, '0::' + identity) for pid in (4242, 4243, 4244)])
    stat = Path.stat
    monkeypatch.setattr(Path, 'stat', lambda path, *args, **kwargs:
                        SimpleNamespace(st_dev=-1, st_ino=42) if str(path) == '/proc/4244/root'
                        else stat(path, *args, **kwargs))
    if case == 'foreign_parent':
        values['/proc/4244/status'] = values['/proc/4244/status'].replace('PPid: 4242', 'PPid: 1')
    elif case == 'cycle':
        values['/proc/4244/status'] = values['/proc/4244/status'].replace('PPid: 4242', 'PPid: 4244')
    elif case == 'nonempty_unknown_role':
        values['/proc/4244/cmdline'] = 'unknown-runtime-role'
    if case == 'approved':
        assert host.verify_native_worker(spec, context[-1])['helper_pids'] == [4242, 4243, 4244]
    else:
        with pytest.raises(host.PackagingHostError):
            host.verify_native_worker(spec, context[-1])
