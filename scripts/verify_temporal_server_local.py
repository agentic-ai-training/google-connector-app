"""Run real Temporal/SQL/replay proof without developer secrets or cloud access."""
import asyncio
from datetime import datetime, timezone
import importlib.metadata
import os
from pathlib import Path
import signal
import sys
import tempfile
from urllib.parse import urlsplit
from uuid import uuid4


def isolated_environment(root,home,output,dsn):
    return {'PATH':os.environ.get('PATH','/usr/bin:/bin'),'HOME':str(home),'PYTHONPATH':str(root),
        'PYTHONDONTWRITEBYTECODE':'1','PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1','LANGCHAIN_TRACING_V2':'false',
        'JWT_SECRET_KEY':'synthetic-server-proof-secret-at-least-32-bytes',
        'RUN_TEMPORAL_SERVER_TESTS':'1','COST_LEDGER_TEST_DSN':dsn,'TEMPORAL_PROOF_DIR':str(output)}


async def stop_owned_group(process):
    try:
        os.killpg(process.pid,signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(),timeout=5)
    except asyncio.TimeoutError:
        try:
            os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


async def main():
    loop,task = asyncio.get_running_loop(),asyncio.current_task()
    # Convert normal operator/CI termination into cancellation so the owned test
    # group is cleaned before this runner exits. SIGKILL cannot be intercepted.
    for signum in (signal.SIGTERM,signal.SIGINT):
        loop.add_signal_handler(signum,task.cancel)
    dsn = os.getenv('COST_LEDGER_TEST_DSN','')
    target = urlsplit(dsn)
    if target.hostname not in {'localhost','127.0.0.1','::1'} or target.query or target.fragment:
        raise SystemExit('COST_LEDGER_TEST_DSN must be a plain loopback PostgreSQL URL')
    root = Path(__file__).resolve().parents[1]
    output = root/'.gca-local'/'temporal-proof'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8])
    output.mkdir(parents=True,exist_ok=False)
    cached = Path(tempfile.gettempdir())/('temporal-sdk-python-'+importlib.metadata.version('temporalio'))
    with tempfile.TemporaryDirectory(prefix='gca-temporal-private-home-') as home:
        env = isolated_environment(root,home,output,dsn)
        if cached.is_file():
            version = await asyncio.create_subprocess_exec(str(cached),'--version',env=env,
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            stdout,_ = await asyncio.wait_for(version.communicate(),timeout=5)
            if version.returncode==0 and b'temporal version 1.9.1 ' in stdout:
                env['GCA_TEMPORAL_CLI']=str(cached)
                print(stdout.decode().strip(),flush=True)
        print('Real loopback server proof; sanitized environment, temporary HOME/cwd, no provider keys',flush=True)
        process = await asyncio.create_subprocess_exec(sys.executable,'-m','pytest','-p','pytest_asyncio.plugin',
            '-p','no:cacheprovider',str(root/'tests/integration/test_temporal_local_server.py'),'-q','-s','--tb=short',
            '--maxfail=1','--junitxml='+str(output/'junit.xml'),cwd=home,env=env,start_new_session=True)
        try:
            result = await asyncio.wait_for(process.wait(),timeout=240)
        except (asyncio.TimeoutError,asyncio.CancelledError):
            # Only this newly created process group, including its test server.
            await asyncio.shield(stop_owned_group(process))
            raise
        # A completed Python test process must not leave its owned server group
        # running. Unexpected leftovers are cleaned and fail the proof.
        try:
            os.killpg(process.pid,0)
        except ProcessLookupError:
            pass
        else:
            os.killpg(process.pid,signal.SIGTERM)
            result = 1
            print('Unexpected test child group remained; stopped it and rejected the proof',flush=True)
        if result==0:
            # Fresh process after server/worker/database fixtures have shut down.
            # No database DSN, opt-in server flag or provider configuration remains.
            offline_env = {key:value for key,value in env.items() if key not in {
                'COST_LEDGER_TEST_DSN','RUN_TEMPORAL_SERVER_TESTS','TEMPORAL_PROOF_DIR','GCA_TEMPORAL_CLI'}}
            offline = await asyncio.create_subprocess_exec(sys.executable,str(root/'scripts/replay_temporal_local_proof.py'),
                str(output),cwd=home,env=offline_env)
            try:
                result = await asyncio.wait_for(offline.wait(),timeout=90)
            except (asyncio.TimeoutError,asyncio.CancelledError):
                offline.kill()
                await offline.wait()
                raise
    print('Local proof artifacts: '+str(output.relative_to(root)),flush=True)
    return result


if __name__=='__main__':
    raise SystemExit(asyncio.run(main()))
