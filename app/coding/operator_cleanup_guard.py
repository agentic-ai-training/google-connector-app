"""Fixed Coolify cleanup guard on the approved VM; never a model-selected tool.

Only changes forced daily cleanup to disk-threshold cleanup. It cannot run cleanup,
delete data, restore images, deploy code, extend allocation or alter host policy.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import subprocess  # nosec B404
import sys
import time


SERVER_UUID = 'frdwr0tdglbolht3kqtokzqa'
STOP_UNIX = 1792430672
MAX_DOCUMENT = 8192
BEFORE = {'docker_cleanup_frequency': '0 0 * * *', 'docker_cleanup_threshold': 80,
          'force_docker_cleanup': True, 'delete_unused_volumes': False,
          'delete_unused_networks': False, 'disable_application_image_retention': False}
AFTER = {**BEFORE, 'force_docker_cleanup': False}
PHP = r'''require '/var/www/html/vendor/autoload.php';
$app=require '/var/www/html/bootstrap/app.php';
$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
$request=json_decode(stream_get_contents(STDIN,8193),true,512,JSON_THROW_ON_ERROR);
if(!is_array($request)||array_keys($request)!==['operation','before','after']) {
    throw new RuntimeException('fixed cleanup request required');
}
$result=Illuminate\Support\Facades\DB::transaction(function()use($request){
    $server=Illuminate\Support\Facades\DB::table('servers')->where('id',0)
        ->where('uuid','frdwr0tdglbolht3kqtokzqa')->lockForUpdate()->sole();
    $row=Illuminate\Support\Facades\DB::table('server_settings')->where('server_id',0)
        ->lockForUpdate()->sole();
    $actual=[];
    foreach($request['before'] as $key=>$expected){
        $value=$row->$key;
        $actual[$key]=is_bool($expected)?(bool)$value:(is_int($expected)?(int)$value:$value);
    }
    if($request['operation']==='read')return $actual;
    if($request['operation']!=='apply'||($actual!==$request['before']&&$actual!==$request['after'])){
        throw new RuntimeException('cleanup settings drifted');
    }
    if($actual===$request['before']) {
        $count=Illuminate\Support\Facades\DB::table('server_settings')->where('id',$row->id)
            ->update(['force_docker_cleanup'=>false]);
        if($count!==1)throw new RuntimeException('cleanup update unconfirmed');
    }
    return $request['after'];
});
echo json_encode($result,JSON_THROW_ON_ERROR);
'''


class CleanupGuardError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def settings(operation):
    # Secrets stay inside the already-authenticated Laravel runtime. Generated
    # source and arbitrary commands are never accepted by this fixed backend.
    request = {'operation': operation, 'before': BEFORE, 'after': AFTER}
    result = subprocess.run(  # nosec B603
        ['/usr/bin/docker', '--host=unix:///var/run/docker.sock', 'exec', '-i',
         '--workdir=/var/www/html', 'coolify', 'php', '-r', PHP],
        input=json.dumps(request).encode(), capture_output=True, timeout=30,
        check=False, env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent'})
    if result.returncode or len(result.stdout) > MAX_DOCUMENT:
        raise CleanupGuardError('fixed cleanup backend unconfirmed')
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or set(value) != set(BEFORE):
        raise CleanupGuardError('cleanup settings projection differs')
    return value


def apply(journal, custody, request, observed, read, write):
    """Write-ahead fixed decision; callbacks are trusted operator backends only."""
    if observed not in (BEFORE, AFTER):
        raise CleanupGuardError('unexpected cleanup settings require review')
    if request['expected_settings_sha256'] != digest(BEFORE):
        raise CleanupGuardError('cleanup before-state authority differs')
    if journal.db.execute("SELECT 1 FROM jobs WHERE state<>'cleaned' LIMIT 1").fetchone():
        raise CleanupGuardError('unfinished host execution blocks cleanup configuration')
    plan = {'version': 1, 'server_uuid': SERVER_UUID, 'server_id': 0, 'stop_unix': STOP_UNIX,
            'controller_sha256': request['controller_sha256'],
            'bundle_sha256': request['expected_snapshot_hash'], 'before': BEFORE, 'after': AFTER}
    plan_path = journal.directory / 'coolify-cleanup-guard-plan.json'
    receipt_path = journal.directory / 'coolify-cleanup-guard-receipt.json'
    if plan_path.exists():
        if custody.document(plan_path) != plan:
            raise CleanupGuardError('retained cleanup decision differs')
    else:
        if observed != BEFORE:
            raise CleanupGuardError('changed cleanup state has no original audit')
        custody.write_document(plan_path, plan)
    if receipt_path.exists():
        expected = {'version': 1, 'plan_sha256': digest(plan), 'settings_sha256': digest(AFTER)}
        if custody.document(receipt_path) != expected or read() != AFTER:
            raise CleanupGuardError('committed cleanup decision drifted')
    else:
        if write() != AFTER or read() != AFTER:
            raise CleanupGuardError('cleanup update needs reconciliation')
        custody.write_document(receipt_path,
                               {'version': 1, 'plan_sha256': digest(plan), 'settings_sha256': digest(AFTER)})
    return {'settings': AFTER, 'plan_sha256': digest(plan), 'settings_sha256': digest(AFTER),
            'cleanup_invoked': False, 'images_restored': False, 'allocation_changed': False,
            'policy_changed': False, 'volumes_deleted': False, 'networks_deleted': False}


def main():
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise CleanupGuardError('fixed Linux operator cleanup guard required')
    body = sys.stdin.buffer.read(MAX_DOCUMENT + 1)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CleanupGuardError('duplicate cleanup request field')
            result[key] = value
        return result
    request = json.loads(body, object_pairs_hook=unique)
    if (len(body) > MAX_DOCUMENT or not isinstance(request, dict)
            or set(request) != {'version', 'operation', 'expected_snapshot_hash',
                                'expected_settings_sha256', 'controller_sha256'}
            or type(request['version']) is not int or request['version'] != 1
            or request['operation'] not in {'probe', 'apply'}
            or any(not isinstance(request[k], str) or not re.fullmatch('[a-f0-9]{64}', request[k])
                   for k in ('expected_snapshot_hash', 'expected_settings_sha256', 'controller_sha256'))):
        raise CleanupGuardError('cleanup request authority differs')
    argv = Path('/proc/self/cmdline').read_bytes().split(b'\0')
    if hashlib.sha256(argv[argv.index(b'-c') + 1]).hexdigest() != request['controller_sha256']:
        raise CleanupGuardError('cleanup controller source differs')
    gateway = runpy.run_path('/usr/local/libexec/gca/operator_toolchain_host.py')
    gateway['target_identity']()
    if gateway['snapshot']() != request['expected_snapshot_hash'] or int(time.time()) >= STOP_UNIX:
        raise CleanupGuardError('cleanup source or hosting allocation changed')
    policy, policy_hash, registry_hash = gateway['configuration']()
    with gateway['isolation'].Journal(Path(policy['state_directory'])) as journal:
        if gateway['snapshot']() != request['expected_snapshot_hash']:
            raise CleanupGuardError('cleanup source changed before ownership')
        observed = settings('read')
        if request['operation'] == 'probe':
            result = {'settings': observed, 'settings_sha256': digest(observed), 'provider_write': False}
        else:
            if (callable(getattr(gateway['isolation'], 'legacy_restoration_pending', None))
                    and gateway['isolation'].legacy_restoration_pending(journal.db)):
                raise CleanupGuardError('legacy image restoration blocks cleanup configuration')
            # Maintenance shares the host slot. A retained exited load is allowed,
            # but an active loader must not race an external cleanup-controller edit.
            for row in journal.toolchain_imports():
                if row['phase'] in {'load_started', 'load_uncertain'}:
                    if gateway['loader'].unit(row['id'])['MainPID'] != '0':
                        raise CleanupGuardError('live image load blocks cleanup configuration')
            result = apply(journal, gateway['custody'], request, observed,
                           lambda: settings('read'), lambda: settings('apply'))
        _, after_policy, after_registry = gateway['configuration']()
        if (policy_hash, registry_hash) != (after_policy, after_registry):
            raise CleanupGuardError('host configuration changed during cleanup guard')
    print(json.dumps({'operation': request['operation'], **result}, sort_keys=True))


if __name__ == '__main__':
    main()
