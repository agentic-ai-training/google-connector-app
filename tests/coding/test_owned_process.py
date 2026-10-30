"""Real synthetic child processes; no model, generated code or cloud authority."""
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
from types import SimpleNamespace

import pytest

from app.coding import worker
from app.coding.owned_process import OwnedProcessError, run_owned


def environment(tmp_path):
    return {'PATH':os.environ.get('PATH','/usr/bin:/bin'),'HOME':str(tmp_path)}


@pytest.mark.asyncio
async def test_real_child_has_explicit_environment_and_cwd(tmp_path,monkeypatch):
    monkeypatch.setenv('RUNTIME_API_KEY','SYNTHETIC_SECRET_NEVER_FORWARD')
    source="import json,os; print(json.dumps({'cwd':os.getcwd(),'key':os.getenv('RUNTIME_API_KEY'),'home':os.getenv('HOME')}))"
    code,result=await run_owned([sys.executable,'-c',source],cwd=tmp_path,env=environment(tmp_path),timeout=5)
    assert code==0 and json.loads(result)=={'cwd':str(tmp_path),'key':None,'home':str(tmp_path)}


@pytest.mark.asyncio
@pytest.mark.parametrize('stream',['stdout','stderr'])
async def test_output_flood_is_bounded_and_not_exposed(tmp_path,stream):
    source=f"import sys; sys.{stream}.write('SYNTHETIC_PRIVATE_OUTPUT'*10000); sys.{stream}.flush()"
    with pytest.raises(OwnedProcessError) as error:
        await run_owned([sys.executable,'-c',source],cwd=tmp_path,env=environment(tmp_path),timeout=5,output_limit=1024)
    assert error.value.category=='output_limit' and 'PRIVATE' not in str(error.value)


@pytest.mark.asyncio
async def test_timeout_stops_parent_and_ignoring_grandchild(tmp_path):
    marker=tmp_path/'pids.json'
    source='''import json,os,signal,subprocess,sys,time
signal.signal(signal.SIGTERM,signal.SIG_IGN)
child=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)'])
with open(sys.argv[1],'w') as out: json.dump([os.getpid(),child.pid],out)
time.sleep(30)
'''
    with pytest.raises(OwnedProcessError) as error:
        await run_owned([sys.executable,'-c',source,str(marker)],cwd=tmp_path,env=environment(tmp_path),timeout=.25)
    assert error.value.category=='timeout'
    for pid in json.loads(marker.read_text()):
        try:
            os.kill(pid,0)
        except ProcessLookupError:
            continue
        # A killed grandchild may briefly be an init-owned zombie. It must not
        # still be executing; the root child was reaped by run_owned.
        proc=await asyncio.create_subprocess_exec('ps','-o','stat=','-p',str(pid),stdout=asyncio.subprocess.PIPE)
        output,_=await proc.communicate()
        assert not output.strip() or output.strip().startswith(b'Z')


@pytest.mark.asyncio
async def test_live_guard_loss_stops_process_before_returning(tmp_path):
    marker=tmp_path/'pid'
    source="import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(30)"
    async def guard():
        if marker.exists():
            raise worker.CodingExecutionFenced('synthetic_lease_lost')
    with pytest.raises(worker.CodingExecutionFenced):
        await run_owned([sys.executable,'-c',source,str(marker)],cwd=tmp_path,env=environment(tmp_path),timeout=5,guard=guard)
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()),0)


@pytest.mark.asyncio
async def test_cancellation_waits_for_owned_child_cleanup(tmp_path):
    marker=tmp_path/'pid'
    source="import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(30)"
    task=asyncio.create_task(run_owned([sys.executable,'-c',source,str(marker)],cwd=tmp_path,
        env=environment(tmp_path),timeout=5))
    async with asyncio.timeout(3):
        while not marker.exists():
            await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()),0)


@pytest.mark.asyncio
async def test_cancel_during_spawn_does_not_abandon_created_child(tmp_path,monkeypatch):
    from app.coding import owned_process
    original=owned_process._spawn_owned
    entered,release=asyncio.Event(),asyncio.Event()
    children=[]
    handles=[]
    async def delayed_spawn(*args,**kwargs):
        transport,protocol=await original(*args,**kwargs)
        children.append(transport.get_pid())
        handles.append((transport,protocol))
        entered.set()
        await release.wait()
        return transport,protocol
    monkeypatch.setattr(owned_process,'_spawn_owned',delayed_spawn)
    task=asyncio.create_task(run_owned([sys.executable,'-c','import time; time.sleep(30)'],
        cwd=tmp_path,env=environment(tmp_path),timeout=5))
    await asyncio.wait_for(entered.wait(),timeout=3)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(children)==1
    with pytest.raises(ProcessLookupError):
        os.kill(children[0],0)
    assert handles[0][0].is_closing() and handles[0][1].closed.done()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['success', 'stdout', 'stderr', 'timeout', 'cancel'])
async def test_every_exit_joins_pipe_transport_before_return(tmp_path,monkeypatch,mode):
    from app.coding import owned_process
    original=owned_process._spawn_owned
    handles=[]
    async def record_spawn(*args,**kwargs):
        handle=await original(*args,**kwargs)
        handles.append(handle)
        return handle
    monkeypatch.setattr(owned_process,'_spawn_owned',record_spawn)
    source={'success':"print('ok')", 'stdout':"import sys; sys.stdout.write('x'*300000)",
            'stderr':"import sys; sys.stderr.write('x'*300000)",
            'timeout':"import time; time.sleep(30)", 'cancel':"import time; time.sleep(30)"}[mode]
    task=asyncio.create_task(run_owned([sys.executable,'-c',source],cwd=tmp_path,
        env=environment(tmp_path),timeout=.1 if mode=='timeout' else 5,output_limit=1024))
    if mode=='cancel':
        async with asyncio.timeout(3):
            while not handles:
                await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif mode=='success':
        assert await task==(0,b'ok\n')
    else:
        with pytest.raises(OwnedProcessError) as error:
            await task
        assert error.value.category==('timeout' if mode=='timeout' else 'output_limit')
    transport,protocol=handles[0]
    assert transport.is_closing() and protocol.closed.done() and protocol.exited.done()
    assert len(protocol.output)<=1024
    for fd in (1,2):
        assert transport.get_pipe_transport(fd).is_closing()


@pytest.mark.asyncio
async def test_protocol_discards_overflow_and_never_retains_stderr():
    from app.coding.owned_process import _OwnedProtocol
    protocol=_OwnedProtocol(4)
    protocol.pipe_data_received(2,b'safe')
    assert protocol.output==b''
    protocol.pipe_data_received(1,b'abcd')
    protocol.pipe_data_received(1,b'e')
    protocol.pipe_data_received(1,b'f')
    assert protocol.failure.result()=='output_limit' and protocol.output==b''


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_interrupt_group_kill(tmp_path):
    marker=tmp_path/'pid'
    source="import os,signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(30)"
    task=asyncio.create_task(run_owned([sys.executable,'-c',source,str(marker)],cwd=tmp_path,
        env=environment(tmp_path),timeout=5))
    async with asyncio.timeout(3):
        while not marker.exists():
            await asyncio.sleep(.01)
    task.cancel()
    await asyncio.sleep(.05)
    task.cancel()
    await asyncio.sleep(.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task,timeout=4)
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()),0)


@pytest.mark.asyncio
async def test_initial_guard_denial_never_creates_a_process(tmp_path):
    async def guard():
        raise worker.CodingExecutionFenced('synthetic_denial')
    with pytest.raises(worker.CodingExecutionFenced):
        await run_owned(['/does/not/exist'],cwd=tmp_path,env=environment(tmp_path),guard=guard)


@pytest.mark.asyncio
async def test_missing_process_group_support_fails_before_spawn(tmp_path,monkeypatch):
    from app.coding import owned_process
    monkeypatch.setattr(owned_process.os,'killpg',None)
    with pytest.raises(OwnedProcessError) as error:
        await run_owned(['/does/not/exist'],cwd=tmp_path,env=environment(tmp_path))
    assert error.value.category=='unsupported_platform'


@pytest.mark.asyncio
@pytest.mark.parametrize('command',['doctor','execute-plan'])
async def test_worker_never_gives_model_key_to_non_planning_command(tmp_path,monkeypatch,command):
    binary=tmp_path/'gca-local'
    binary.touch()
    monkeypatch.setattr(worker,'get_settings',lambda:SimpleNamespace(coding_local_runner_binary=str(binary)))
    with pytest.raises(worker.CodingWorkerError) as error:
        await worker._run_local([command],coding_key='SYNTHETIC_DEDICATED')
    assert error.value.category=='runtime_policy'


@pytest.mark.parametrize('fault',['outside','symlink','parent_symlink','hardlink','fifo','large'])
def test_evidence_reader_does_not_follow_untrusted_file_authority(tmp_path,fault):
    root=tmp_path/'state'
    root.mkdir()
    path=root/'plan.json'
    if fault=='outside':
        path=tmp_path/'outside'
        path.write_text('SYNTHETIC_PRIVATE_OUTSIDE')
    elif fault=='symlink':
        target=tmp_path/'outside'
        target.write_text('SYNTHETIC_PRIVATE_OUTSIDE')
        path.symlink_to(target)
    elif fault=='parent_symlink':
        (root/'linked').symlink_to(tmp_path,target_is_directory=True)
        path=root/'linked'/'outside'
        (tmp_path/'outside').write_text('SYNTHETIC_PRIVATE_OUTSIDE')
    elif fault=='hardlink':
        target=tmp_path/'outside'
        target.write_text('SYNTHETIC_PRIVATE_OUTSIDE')
        path.hardlink_to(target)
    elif fault=='fifo':
        os.mkfifo(path)
    else:
        path.write_bytes(b'x'*100)
    with pytest.raises((OSError,worker.CodingWorkerError)):
        worker._bounded_private_file(path,root,16)


def test_evidence_reader_accepts_only_bounded_regular_private_file(tmp_path):
    path=tmp_path/'plan.json'
    path.write_bytes(b'{"actions":[]}')
    assert worker._bounded_private_file(path,tmp_path,100)==b'{"actions":[]}'
