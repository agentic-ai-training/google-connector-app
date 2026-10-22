"""Kafka adapter contract tests; no broker/network and no SDK installation claim."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import types
import sys
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.runs import event_outbox as outbox, kafka_delivery as kafka


def body(**changes):
    return json.dumps({'schema': 'gca.workflow.event.v1', 'event_id': str(uuid4()),
        'tenant_id': str(uuid4()), 'run_id': str(uuid4()), 'sequence': 1, 'source_event_id': 1,
        'event_type': 'step_started', 'phase': 'executing', 'created_at': datetime.now(timezone.utc).isoformat(),
        **changes}).encode()


def test_schema_rejects_content_credentials_duplicate_fields_and_oversized_messages():
    for value in (b'{"event_id":1,"event_id":2}', b'x' * (outbox.MAX_WIRE_BYTES + 1),
                  body(sequence=True), body(email_body='private'), body(created_at='2026-10-22T00:00:00')):
        with pytest.raises(outbox.EventDeliveryError):
            outbox.decode(value)


@pytest.mark.parametrize('brokers', [('public.example:9092',), ('localhost:9092/private',),
    ('user:password@localhost:9092',), ('https://localhost:9092',), ('localhost',)])
def test_plaintext_cannot_target_remote_or_credentialed_endpoints(brokers):
    with pytest.raises(ValidationError):
        kafka.KafkaDeliveryConfig(brokers=brokers, local_plaintext=True)


def test_tls_ack_idempotence_and_secret_repr_are_explicit():
    value = kafka.KafkaDeliveryConfig(brokers=('broker.example:9093',), username='publisher', password='synthetic-only')
    assert 'synthetic-only' not in repr(value)
    options = kafka.producer_options(value)
    assert options['acks'] == 'all' and options['enable_idempotence']
    assert options['security_protocol'] == 'SASL_SSL'
    assert options['ssl_context'].check_hostname
    assert options['max_request_size'] == outbox.MAX_WIRE_BYTES + 1024


@pytest.mark.asyncio
async def test_adapter_waits_for_ack_and_keeps_exact_bytes():
    calls = []
    class Producer:
        async def send_and_wait(self, topic, value, **kwargs):
            calls.append((topic, value, kwargs))
            return SimpleNamespace(topic=kafka.TOPIC, partition=1, offset=123)
    value = body()
    event = outbox.decode(value)
    key = (str(event.tenant_id) + ':' + str(event.run_id)).encode()
    adapter = kafka.KafkaDelivery(Producer())
    assert await adapter.publish(key=key, body=value) == outbox.BrokerAcknowledgement(event.event_id, kafka.TOPIC, 1, 123)
    assert calls == [(kafka.TOPIC, value, {'key': key})]
    with pytest.raises(outbox.EventDeliveryError, match='partition identity'):
        await adapter.publish(key=b'foreign-tenant', body=value)
    assert len(calls) == 1


class Admin:
    def __init__(self):
        self.topics = [kafka.TOPIC]
        self.partitions = [{'partition': i, 'error_code': 0, 'leader': 1, 'replicas': [1, 2, 3], 'isr': [1, 2, 3]} for i in range(3)]
        self.limits = kafka.TOPIC_LIMITS.copy()
    async def list_topics(self):
        return self.topics
    async def describe_topics(self, topics):
        assert topics == [kafka.TOPIC]
        return [{'error_code': 0, 'topic': kafka.TOPIC, 'partitions': self.partitions}]
    async def describe_configs(self, resources):
        assert resources == ['synthetic-resource']
        return [SimpleNamespace(to_object=lambda: {'resources': [{'error_code': 0, 'resource_type': 2,
            'resource_name': kafka.TOPIC, 'config_entries': [{'config_names': k, 'config_value': v}
                                                          for k, v in self.limits.items()]}]})]


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['none', 'missing_topic', 'partitions', 'replicas', 'retention', 'message_limit'])
async def test_existing_topic_policy_is_read_back_without_creation(fault):
    admin = Admin()
    if fault == 'missing_topic':
        admin.topics = []
    elif fault == 'partitions':
        admin.partitions.pop()
    elif fault == 'replicas':
        admin.partitions[0]['replicas'] = [1]
    elif fault == 'retention':
        admin.limits['retention.ms'] = '-1'
    elif fault == 'message_limit':
        admin.limits['max.message.bytes'] = '1000000'
    if fault == 'none':
        await kafka.verify_topic(admin, 'synthetic-resource')
    else:
        with pytest.raises(outbox.EventDeliveryError):
            await kafka.verify_topic(admin, 'synthetic-resource')


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['none', 'policy', 'stop'])
async def test_adapter_closes_admin_even_when_policy_or_producer_shutdown_fails(monkeypatch, failure):
    closed = []
    class Client(Admin):
        def __init__(self, **options):
            super().__init__()
            assert 'acks' not in options and options['security_protocol'] == 'PLAINTEXT'
            if failure == 'policy':
                self.topics = []
        async def start(self):
            pass
        async def close(self):
            closed.append('admin')
    class Producer:
        def __init__(self, **options):
            assert options['acks'] == 'all' and options['enable_idempotence']
        async def start(self):
            pass
        async def stop(self):
            closed.append('producer')
            if failure == 'stop':
                raise RuntimeError('synthetic stop failure')
    modules = {name: types.ModuleType(name) for name in ('aiokafka', 'aiokafka.admin', 'aiokafka.admin.config_resource')}
    modules['aiokafka'].AIOKafkaProducer = Producer
    modules['aiokafka.admin'].AIOKafkaAdminClient = Client
    modules['aiokafka.admin.config_resource'].ConfigResource = lambda *args: 'synthetic-resource'
    modules['aiokafka.admin.config_resource'].ConfigResourceType = SimpleNamespace(TOPIC=2)
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    config = kafka.KafkaDeliveryConfig(brokers=('localhost:9092',), local_plaintext=True)
    if failure == 'none':
        async with kafka.connect(config):
            pass
        assert closed == ['producer', 'admin']
    else:
        with pytest.raises((outbox.EventDeliveryError, RuntimeError)):
            async with kafka.connect(config):
                pass
        assert closed[-1] == 'admin'


def test_publisher_cli_requires_explicit_database_and_keeps_errors_private(monkeypatch, capsys):
    from scripts import publish_workflow_events as cli
    with pytest.raises(ValueError, match='database URL'):
        cli.configuration({})
    async def denied(**kwargs):
        raise RuntimeError('synthetic-secret-dsn-and-password')
    monkeypatch.setattr(cli, 'run', denied)
    monkeypatch.setattr(sys, 'argv', ['publish_workflow_events'])
    assert cli.main() == 1
    output = capsys.readouterr().out
    assert 'synthetic-secret' not in output and 'unconfirmed' in output


@pytest.mark.asyncio
async def test_publisher_cli_batch_bounds_precede_any_connection():
    from scripts import publish_workflow_events as cli
    for value in (0, 1001, True):
        with pytest.raises(ValueError, match='batch count'):
            await cli.run(max_batches=value)


def test_publisher_partial_delivery_is_not_a_success_exit(monkeypatch, capsys):
    from scripts import publish_workflow_events as cli
    async def partial(**kwargs):
        return {'published': 1, 'lease_lost': 0, 'unconfirmed': 1}
    monkeypatch.setattr(cli, 'run', partial)
    monkeypatch.setattr(sys, 'argv', ['publish_workflow_events'])
    assert cli.main() == 1
    assert json.loads(capsys.readouterr().out)['delivery_state'] == 'partial'
