"""Explicit optional Kafka adapter; no broker creation or automatic activation."""
from contextlib import asynccontextmanager
import asyncio
import ssl
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from app.runs.event_outbox import MAX_WIRE_BYTES, BrokerAcknowledgement, EventDeliveryError, decode, partition_key


TOPIC = 'gca.workflow.events.v1'
TOPIC_LIMITS = {'cleanup.policy': 'delete', 'retention.ms': '86400000',
                'retention.bytes': '67108864', 'segment.bytes': '16777216',
                'segment.ms': '3600000', 'max.message.bytes': '24576', 'min.insync.replicas': '2'}


class KafkaDeliveryConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    brokers: tuple[str, ...] = Field(min_length=1, max_length=3)
    local_plaintext: bool = False
    username: str | None = Field(default=None, max_length=128)
    password: SecretStr | None = None

    @model_validator(mode='after')
    def endpoints_and_credentials(self):
        for broker in self.brokers:
            if len(broker) > 255:
                raise ValueError('Kafka endpoint exceeds bound')
            try:
                value = urlsplit('//' + broker)
                valid = (value.hostname and value.port and not value.username and not value.password
                         and not value.path and not value.query and not value.fragment)
            except ValueError:
                valid = False
            if not valid:
                raise ValueError('Kafka endpoint must be a configured host:port')
            if self.local_plaintext and value.hostname not in {'localhost', '127.0.0.1', '::1'}:
                raise ValueError('plaintext Kafka is permitted only on loopback')
        if self.local_plaintext:
            if self.username is not None or self.password is not None:
                raise ValueError('credentials must not be sent over plaintext')
        elif not self.username or not self.password or not 1 <= len(self.password.get_secret_value()) <= 4096:
            raise ValueError('production Kafka requires explicit SASL/TLS credentials')
        return self


class KafkaDelivery:
    def __init__(self, producer):
        self.producer = producer

    async def publish(self, *, key: bytes, body: bytes):
        event = decode(body)
        expected_key = partition_key(event)
        if key != expected_key:
            raise EventDeliveryError('Kafka event partition identity differs')
        # send_and_wait, never fire-and-forget: DB publication follows the ACK.
        result = await self.producer.send_and_wait(TOPIC, body, key=key)
        if (getattr(result, 'topic', None) != TOPIC or type(getattr(result, 'partition', None)) is not int
                or not 0 <= result.partition <= 2 or type(getattr(result, 'offset', None)) is not int or result.offset < -1):
            raise EventDeliveryError('Kafka returned no matching acknowledgement metadata')
        # aiokafka documents offset=-1 for an already-acknowledged idempotent
        # duplicate whose original offset expired; preserve it, never invent one.
        return BrokerAcknowledgement(event.event_id, TOPIC, result.partition, result.offset)


def producer_options(config: KafkaDeliveryConfig) -> dict:
    options = dict(bootstrap_servers=list(config.brokers), client_id='gca-workflow-outbox-v1',
                   acks='all', enable_idempotence=True, max_request_size=MAX_WIRE_BYTES + 1024,
                   max_batch_size=MAX_WIRE_BYTES + 1024, request_timeout_ms=10000, linger_ms=0)
    if config.local_plaintext:
        options['security_protocol'] = 'PLAINTEXT'
    else:
        options.update(security_protocol='SASL_SSL', ssl_context=ssl.create_default_context(),
                       sasl_mechanism='SCRAM-SHA-512', sasl_plain_username=config.username,
                       sasl_plain_password=config.password.get_secret_value())
    return options


async def verify_topic(admin, resource):
    """Read-only profile check. Never create/alter topics or infer HA domains."""
    topics = await admin.list_topics()
    if TOPIC not in topics:
        raise EventDeliveryError('reviewed workflow topic must already exist')
    entries = await admin.describe_topics([TOPIC])
    if len(entries) != 1 or entries[0].get('error_code') != 0 or entries[0].get('topic') != TOPIC:
        raise EventDeliveryError('workflow topic metadata is unavailable')
    partitions = entries[0].get('partitions')
    if (not isinstance(partitions, list) or len(partitions) != 3
            or {x.get('partition') for x in partitions} != {0, 1, 2}
            or any(x.get('error_code') != 0 or len(set(x.get('replicas') or [])) != 3
                   or len(set(x.get('isr') or [])) < 2
                   or not set(x.get('isr') or []) <= set(x.get('replicas') or [])
                   or x.get('leader') not in (x.get('isr') or []) for x in partitions)):
        raise EventDeliveryError('workflow topic partition/replica durability differs')
    responses = await admin.describe_configs([resource])
    if len(responses) != 1:
        raise EventDeliveryError('workflow topic config response differs')
    resources = responses[0].to_object().get('resources')
    if (not isinstance(resources, list) or len(resources) != 1 or resources[0].get('error_code') != 0
            or resources[0].get('resource_type') != 2 or resources[0].get('resource_name') != TOPIC):
        raise EventDeliveryError('workflow topic limits are unavailable')
    configs = resources[0].get('config_entries') or []
    actual = {item['config_names']: item.get('config_value') for item in configs}
    if len(actual) != len(configs) or any(actual.get(key) != value for key, value in TOPIC_LIMITS.items()):
        raise EventDeliveryError('workflow topic retention/message bounds differ')


@asynccontextmanager
async def connect(config: KafkaDeliveryConfig):
    # Optional dependency stays outside the core's imports until an operator
    # explicitly starts the delivery process. This never provisions a broker.
    try:
        from aiokafka import AIOKafkaProducer
        from aiokafka.admin import AIOKafkaAdminClient
        from aiokafka.admin.config_resource import ConfigResource, ConfigResourceType
    except ImportError:
        raise EventDeliveryError('install the pinned optional orchestration requirements') from None
    options = producer_options(config)
    admin = AIOKafkaAdminClient(**{key: value for key, value in options.items()
        if key not in {'acks', 'enable_idempotence', 'max_request_size', 'max_batch_size', 'linger_ms'}})
    producer = None
    try:
        async with asyncio.timeout(15):
            await admin.start()
            await verify_topic(admin, ConfigResource(ConfigResourceType.TOPIC, TOPIC))
            producer = AIOKafkaProducer(**options)
            await producer.start()
        yield KafkaDelivery(producer)
    finally:
        try:
            async with asyncio.timeout(10):
                if producer is not None:
                    await producer.stop()
        finally:
            async with asyncio.timeout(5):
                await admin.close()
