"""Bounded, cancellable owned process groups for the trusted coding CLI.

This is process lifecycle management, not an OS sandbox for generated programs.
"""
import asyncio
from contextlib import suppress
import os
import signal


class OwnedProcessError(RuntimeError):
    def __init__(self, category):
        self.category = category
        super().__init__('Coding CLI ' + category)


async def _drain(reader, limit, *, capture):
    parts, count = [], 0
    while True:
        part = await reader.read(65536)
        if not part:
            return b''.join(parts)
        count += len(part)
        if count > limit:
            raise OwnedProcessError('output_limit')
        if capture:
            parts.append(part)


async def stop_group(process):
    # The group belongs to this newly started process, not an arbitrary PID from
    # a model. Clean grandchildren even if their original leader already exited.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except asyncio.TimeoutError:
        pass
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=2)


async def _join_shielded(future):
    """Finish owned cleanup despite repeated cancellation of its caller."""
    interrupted = False
    while True:
        try:
            return await asyncio.shield(future), interrupted
        except asyncio.CancelledError:
            if future.done():
                # Do not spin forever if the cleanup task itself was cancelled
                # by event-loop shutdown rather than by the parent caller.
                return future.result(), True
            interrupted = True


async def run_owned(argv, *, cwd, env, timeout=960, output_limit=2_097_152, guard=None):
    if os.name != 'posix' or not callable(getattr(os, 'killpg', None)):
        raise OwnedProcessError('unsupported_platform')
    if not 0 < timeout <= 960 or not 0 < output_limit <= 16_777_216:
        raise OwnedProcessError('invalid_bounds')
    if guard:
        await guard()
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(*argv, cwd=cwd, env=env, start_new_session=True,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE))
    try:
        process = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        # Cancellation can arrive during OS process creation, before the caller
        # has a handle. Do not abandon a child that may already have started.
        with suppress(Exception):
            process, _ = await _join_shielded(spawn)
            await _join_shielded(asyncio.create_task(stop_group(process)))
        raise
    async def monitor():
        while True:
            await asyncio.sleep(0.25)
            if guard:
                await guard()
    stdout = asyncio.create_task(_drain(process.stdout, output_limit, capture=True))
    # Never retain/echo stderr: it can contain private source or provider errors.
    stderr = asyncio.create_task(_drain(process.stderr, min(output_limit, 1_048_576), capture=False))
    waiter = asyncio.create_task(process.wait())
    monitor_task = asyncio.create_task(monitor())
    io_task = asyncio.gather(stdout, stderr, waiter)
    try:
        async with asyncio.timeout(timeout):
            done, _ = await asyncio.wait([io_task, monitor_task], return_when=asyncio.FIRST_COMPLETED)
            if monitor_task in done:
                await monitor_task
            output, _, code = await io_task
            if guard:
                await guard()
            return code, output
    except TimeoutError:
        raise OwnedProcessError('timeout') from None
    finally:
        # Wait for cleanup even when the caller was cancelled. It must not remove
        # the temporary HOME/workspace before its child has been stopped.
        cleanup = asyncio.create_task(stop_group(process))
        _, interrupted = await _join_shielded(cleanup)
        monitor_task.cancel()
        io_task.cancel()
        for task in (stdout, stderr, waiter):
            task.cancel()
        _, interrupted_again = await _join_shielded(asyncio.gather(monitor_task, io_task, stdout, stderr, waiter,
            return_exceptions=True))
        if interrupted or interrupted_again:
            raise asyncio.CancelledError()
