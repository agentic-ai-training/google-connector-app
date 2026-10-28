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


class _OwnedProtocol(asyncio.SubprocessProtocol):
    """Bound output without abandoning paused StreamReader pipe transports.

    Keep the public SubprocessTransport so cleanup can close every pipe even
    during spawn cancellation or an output flood. No private Process internals.
    """
    def __init__(self, limit):
        loop = asyncio.get_running_loop()
        self.exited, self.closed, self.failure = (loop.create_future() for _ in range(3))
        self.limits = {1: limit, 2: min(limit, 1_048_576)}
        self.counts = {1: 0, 2: 0}
        self.output = bytearray()

    def connection_made(self, transport):
        self.transport = transport

    def pipe_data_received(self, fd, data):
        self.counts[fd] += len(data)
        if self.counts[fd] > self.limits[fd]:
            self.output.clear()
            if not self.failure.done():
                self.failure.set_result('output_limit')
        elif fd == 1 and not self.failure.done():
            self.output.extend(data)
        # Stderr and overflow bytes are immediately discarded, never echoed.

    def pipe_connection_lost(self, fd, exc):
        if exc is not None and not self.failure.done():
            self.failure.set_result('pipe_failure')

    def process_exited(self):
        if not self.exited.done():
            self.exited.set_result(self.transport.get_returncode())

    def connection_lost(self, exc):
        if exc is not None and not self.failure.done():
            self.failure.set_result('pipe_failure')
        if not self.closed.done():
            self.closed.set_result(None)


async def _spawn_owned(argv, cwd, env, limit):
    return await asyncio.get_running_loop().subprocess_exec(
        lambda: _OwnedProtocol(limit), *argv, cwd=cwd, env=env, start_new_session=True,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)


async def stop_group(transport, protocol):
    # The group belongs to this newly started process, not an arbitrary PID from
    # a model. Clean grandchildren even if their original leader already exited.
    try:
        with suppress(ProcessLookupError):
            os.killpg(transport.get_pid(), signal.SIGTERM)
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(protocol.exited), timeout=2)
    finally:
        try:
            with suppress(ProcessLookupError):
                os.killpg(transport.get_pid(), signal.SIGKILL)
        finally:
            transport.close()
            # Reaping a PID alone does not prove that stdout/stderr descriptors
            # have closed. Join the public transport's complete lifecycle too.
            try:
                await asyncio.wait_for(asyncio.shield(protocol.closed), timeout=2)
            except asyncio.TimeoutError:
                raise OwnedProcessError('cleanup_incomplete') from None


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
    spawn = asyncio.create_task(_spawn_owned(argv, cwd, env, output_limit))
    try:
        transport, protocol = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        # Cancellation can arrive during OS process creation, before the caller
        # has a handle. Do not abandon a child that may already have started.
        with suppress(Exception):
            (transport, protocol), _ = await _join_shielded(spawn)
            await _join_shielded(asyncio.create_task(stop_group(transport, protocol)))
        raise
    async def monitor():
        while True:
            await asyncio.sleep(0.25)
            if guard:
                await guard()
    monitor_task = asyncio.create_task(monitor())
    try:
        async with asyncio.timeout(timeout):
            done, _ = await asyncio.wait([protocol.closed, protocol.failure, monitor_task],
                                        return_when=asyncio.FIRST_COMPLETED)
            if protocol.failure in done:
                raise OwnedProcessError(protocol.failure.result())
            if monitor_task in done:
                await monitor_task
            if guard:
                await guard()
            return transport.get_returncode(), bytes(protocol.output)
    except TimeoutError:
        raise OwnedProcessError('timeout') from None
    finally:
        # Wait for cleanup even when the caller was cancelled. It must not remove
        # the temporary HOME/workspace before its child has been stopped.
        async def clean():
            try:
                await stop_group(transport, protocol)
            finally:
                monitor_task.cancel()
                await asyncio.gather(monitor_task, return_exceptions=True)
        _, interrupted = await _join_shielded(asyncio.create_task(clean()))
        if interrupted:
            raise asyncio.CancelledError()
