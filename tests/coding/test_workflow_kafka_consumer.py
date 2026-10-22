"""Consumer transport contracts with deterministic fake SDK/records, no network."""
import asyncio
from collections import namedtuple
from types import SimpleNamespace
import sys
import types

import pytest

from app.runs import kafka_consumer as transport, kafka_delivery as kafka
from app.runs.workflow_projection import ProjectionDeliveryError


TP = namedtuple('TopicPartition', 'topic partition')


def record(offset=10):
    return SimpleNamespace(topic=kafka.TOPIC, partition=0, offset=offset, key=b'synthetic-key', value=b'synthetic-body')


class Consumer:
    def __init__(self, records=None):
        self.tp = TP(kafka.TOPIC, 0)
        self.records = [record()] if records is None else records
        self.commits = []
        self.seeks = []
        self.offset = 10
    async def getmany(self, **kwargs):
        assert kwargs == {'timeout_ms': 1000, 'max_records': 8}
        return {self.tp: self.records}
    async def commit(self, value):
        self.commits.append(value)
    async def committed(self, tp):
        return self.offset
    async def seek_to_beginning(self, tp):
        self.seeks.append(tp)


def state(consumer):
    value = transport.AssignmentState()
    value.owned[consumer.tp] = 1
    return value


def test_fixed_consumer_options_use_manual_offsets_bounded_fetch_and_verified_tls():
    options = transport.consumer_options(kafka.KafkaDeliveryConfig(
        brokers=('broker.example:9093',), username='reader', password='synthetic-only'))
    assert options['enable_auto_commit'] is False and options['auto_offset_reset'] == 'none'
    assert options['group_id'] == 'gca-workflow-projection-v1'
    assert options['max_poll_records'] == 8 and options['max_partition_fetch_bytes'] == 24576
    assert options['security_protocol'] == 'SASL_SSL' and options['ssl_context'].check_hostname
    assert options['isolation_level'] == 'read_committed'


@pytest.mark.asyncio
async def test_new_group_initialization_is_explicit_and_never_resets_existing_offsets():
    client = Consumer()
    client.offset = None
    denied = transport.AssignmentState()
    await denied.assign([client.tp], client, initialize_new_group=False)
    assert denied.error == 'offset_bootstrap_required' and client.seeks == []
    allowed = transport.AssignmentState()
    await allowed.assign([client.tp], client, initialize_new_group=True)
    assert client.seeks == [client.tp] and allowed.current(client.tp, allowed.owned[client.tp])
    client.offset = 100
    await allowed.assign([client.tp], client, initialize_new_group=True)
    assert client.seeks == [client.tp]


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['applied', 'duplicate', 'erased'])
async def test_only_completed_registered_sql_outcomes_advance_exact_partition(monkeypatch, outcome):
    client = Consumer([record(10), record(12)])  # Kafka offsets need not be contiguous.
    ownership = state(client)
    async def apply(pool, **kwargs):
        assert len(client.commits) in {0, 1}
        return outcome
    monkeypatch.setattr(transport, 'apply', apply)
    result = await transport.consume_batches(None, client, ownership)
    assert result[outcome] == 2 and result['committed'] == 2
    assert client.commits == [{client.tp: 11}, {client.tp: 13}]


@pytest.mark.asyncio
async def test_poison_record_stops_before_following_record_and_stores_no_provider_text(monkeypatch):
    client = Consumer([record(10), record(11)])
    evidence = []
    async def apply(pool, **kwargs):
        raise ProjectionDeliveryError('record_invalid')
    async def failure(pool, **kwargs):
        evidence.append(kwargs)
    monkeypatch.setattr(transport, 'apply', apply)
    monkeypatch.setattr(transport, 'record_failure', failure)
    with pytest.raises(ProjectionDeliveryError, match='record_invalid'):
        await transport.consume_batches(None, client, state(client))
    assert client.commits == [] and len(evidence) == 1 and evidence[0]['offset'] == 10


@pytest.mark.asyncio
async def test_rebalance_cancels_inflight_sql_without_advancing_or_deadlocking(monkeypatch):
    client = Consumer()
    ownership = state(client)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    failures = []
    async def apply(pool, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    async def failure(pool, **kwargs):
        failures.append(kwargs['category'])
    monkeypatch.setattr(transport, 'apply', apply)
    monkeypatch.setattr(transport, 'record_failure', failure)
    task = asyncio.create_task(transport.consume_batches(None, client, ownership))
    await asyncio.wait_for(started.wait(), 1)
    ownership.revoke([client.tp])
    with pytest.raises(ProjectionDeliveryError, match='assignment_lost'):
        await asyncio.wait_for(task, 1)
    assert cancelled.is_set() and not client.commits and failures == ['assignment_lost']
    assert ownership.active == {}


@pytest.mark.asyncio
async def test_revoke_after_sql_commit_cannot_advance_old_lease(monkeypatch):
    client = Consumer()
    ownership = state(client)
    async def apply(pool, **kwargs):
        ownership.owned.pop(client.tp)
        return 'applied'
    async def failure(pool, **kwargs):
        assert kwargs['category'] == 'assignment_lost'
    monkeypatch.setattr(transport, 'apply', apply)
    monkeypatch.setattr(transport, 'record_failure', failure)
    with pytest.raises(ProjectionDeliveryError, match='assignment_lost'):
        await transport.consume_batches(None, client, ownership)
    assert client.commits == []


@pytest.mark.asyncio
async def test_rebalance_during_fetch_stops_instead_of_skipping_advanced_position():
    client = Consumer()
    ownership = state(client)
    async def fetch(**kwargs):
        ownership.revoke([client.tp])
        return {client.tp: [record()]}
    client.getmany = fetch
    with pytest.raises(ProjectionDeliveryError, match='assignment_lost'):
        await transport.consume_batches(None, client, ownership)
    assert client.commits == []


@pytest.mark.asyncio
async def test_parent_cancellation_is_preserved_and_not_converted_to_success(monkeypatch):
    client = Consumer()
    ownership = state(client)
    started = asyncio.Event()
    async def apply(pool, **kwargs):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(transport, 'apply', apply)
    task = asyncio.create_task(transport.consume_batches(None, client, ownership))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ownership.active == {} and client.commits == []


@pytest.mark.asyncio
async def test_out_of_order_or_oversized_batch_is_not_committed(monkeypatch):
    async def apply(pool, **kwargs):
        return 'applied'
    monkeypatch.setattr(transport, 'apply', apply)
    client = Consumer([record(10), record(9)])
    with pytest.raises(ProjectionDeliveryError, match='record_invalid'):
        await transport.consume_batches(None, client, state(client))
    assert client.commits == [{client.tp: 11}]  # Never advance the stale second offset.
    client = Consumer([record()] * 9)
    with pytest.raises(ProjectionDeliveryError, match='record_invalid'):
        await transport.consume_batches(None, client, state(client))
    assert not client.commits


@pytest.mark.asyncio
async def test_stop_after_current_sql_commit_does_not_process_remaining_fetched_records(monkeypatch):
    client = Consumer([record(10), record(11)])
    stop = asyncio.Event()
    async def apply(pool, **kwargs):
        stop.set()
        return 'applied'
    monkeypatch.setattr(transport, 'apply', apply)
    result = await transport.consume_batches(None, client, state(client), stop_event=stop)
    assert result['committed'] == 1 and client.commits == [{client.tp: 11}]


@pytest.mark.asyncio
async def test_stop_signal_and_invalid_batch_count_precede_fetch():
    client = Consumer()
    stop = asyncio.Event()
    stop.set()
    assert (await transport.consume_batches(None, client, state(client), stop_event=stop))['committed'] == 0
    for invalid in (0, 1001, True):
        with pytest.raises(ValueError):
            await transport.consume_batches(None, client, state(client), max_batches=invalid)


def test_operator_consumer_cli_omits_secrets_and_uses_failure_exit(monkeypatch, capsys):
    from scripts import consume_workflow_events as cli
    async def denied(**kwargs):
        raise RuntimeError('PRIVATE PASSWORD AND DSN')
    monkeypatch.setattr(cli, 'run', denied)
    monkeypatch.setattr(sys, 'argv', ['consume_workflow_events'])
    assert cli.main() == 1
    assert 'PRIVATE' not in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['none', 'policy', 'start', 'stop', 'bootstrap', 'assignment'])
async def test_consumer_adapter_preflights_topic_and_closes_all_resources(monkeypatch, fault):
    calls = []
    class Admin:
        def __init__(self, **kwargs):
            pass
        async def start(self):
            calls.append('admin_start')
        async def close(self):
            calls.append('admin_close')
    class Client(Consumer):
        def __init__(self, **kwargs):
            super().__init__()
            assert kwargs['enable_auto_commit'] is False
            if fault == 'bootstrap':
                self.offset = None
        async def committed(self, tp):
            if fault == 'assignment':
                raise RuntimeError('PRIVATE ASSIGNMENT PROVIDER ERROR')
            return self.offset
        def subscribe(self, topics, listener):
            assert topics == [kafka.TOPIC]
            self.listener = listener
        async def start(self):
            calls.append('consumer_start')
            if fault == 'start':
                raise RuntimeError('synthetic start failure')
            await self.listener.on_partitions_assigned([self.tp])
        async def stop(self):
            calls.append('consumer_stop')
            if fault == 'stop':
                raise RuntimeError('synthetic stop failure')
    modules = {name: types.ModuleType(name) for name in ('aiokafka', 'aiokafka.abc', 'aiokafka.admin', 'aiokafka.admin.config_resource')}
    modules['aiokafka'].AIOKafkaConsumer = Client
    modules['aiokafka.abc'].ConsumerRebalanceListener = type('ConsumerRebalanceListener', (), {})
    modules['aiokafka.admin'].AIOKafkaAdminClient = Admin
    modules['aiokafka.admin.config_resource'].ConfigResource = lambda *args: 'resource'
    modules['aiokafka.admin.config_resource'].ConfigResourceType = SimpleNamespace(TOPIC=2)
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    async def preflight(admin, resource):
        calls.append('policy')
        if fault == 'policy':
            raise RuntimeError('synthetic policy failure')
    monkeypatch.setattr(transport, 'verify_topic', preflight)
    config = kafka.KafkaDeliveryConfig(brokers=('localhost:9092',), local_plaintext=True)
    if fault == 'none':
        async with transport.connect_consumer(config) as (_, ownership):
            assert ownership.owned and not ownership.error
    else:
        with pytest.raises(RuntimeError):
            async with transport.connect_consumer(config):
                pass
    assert calls[:2] == ['admin_start', 'policy'] and calls[-1] == 'admin_close'
    assert ('consumer_stop' in calls) == (fault != 'policy')
