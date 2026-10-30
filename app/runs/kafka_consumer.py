"""Explicit bounded Kafka-to-SQL projection; no execution authority or provisioning."""
from contextlib import asynccontextmanager
import asyncio

from app.runs.kafka_delivery import TOPIC, producer_options, verify_topic
from app.runs.workflow_projection import CONSUMER_GROUP, ProjectionDeliveryError, apply, record_failure


class ConsumerStartupError(RuntimeError):
    def __init__(self, category):
        if category not in {'record_invalid', 'offset_bootstrap_required', 'assignment_initialization_failed'}:
            raise ValueError('closed consumer startup category required')
        self.category = category
        super().__init__(category)


class AssignmentState:
    """Local epochs fence fetched batches. Kafka/SQL supply distributed durability."""
    def __init__(self):
        self.generation = 0
        self.owned = {}
        self.active = {}
        self.error = None

    def revoke(self, partitions):
        self.generation += 1
        for tp in partitions:
            self.owned.pop(tp, None)
            task = self.active.get(tp)
            if task is not None:
                task.cancel()
        # Never await getmany/commit or acquire application locks in this callback.
        # Uncommitted offsets replay; an already committed SQL inbox deduplicates.

    async def assign(self, partitions, consumer, *, initialize_new_group):
        self.generation += 1
        for tp in partitions:
            if tp.topic != TOPIC or type(tp.partition) is not int or not 0 <= tp.partition <= 2:
                self.error = 'record_invalid'
                return
            async with asyncio.timeout(5):
                committed = await consumer.committed(tp)
                if committed is None:
                    if not initialize_new_group:
                        self.error = 'offset_bootstrap_required'
                        return
                    await consumer.seek_to_beginning(tp)
                elif type(committed) is not int or committed < 0:
                    self.error = 'record_invalid'
                    return
            self.owned[tp] = self.generation

    def current(self, tp, epoch):
        return self.error is None and epoch is not None and self.owned.get(tp) == epoch

    async def close(self):
        tasks = list(self.active.values())
        for task in tasks:
            task.cancel()
        if tasks:
            async with asyncio.timeout(6):
                await asyncio.gather(*tasks, return_exceptions=True)


def consumer_options(config):
    connection_keys = {'bootstrap_servers', 'security_protocol', 'ssl_context', 'sasl_mechanism',
                       'sasl_plain_username', 'sasl_plain_password', 'request_timeout_ms'}
    options = {key: value for key, value in producer_options(config).items() if key in connection_keys}
    options.update(client_id='gca-workflow-projection-v1', group_id=CONSUMER_GROUP,
        enable_auto_commit=False, auto_offset_reset='none', max_poll_records=8,
        fetch_max_bytes=98304, max_partition_fetch_bytes=24576, max_poll_interval_ms=120000,
        session_timeout_ms=10000, heartbeat_interval_ms=3000, rebalance_timeout_ms=10000,
        isolation_level='read_committed', check_crcs=True)
    return options


@asynccontextmanager
async def connect_consumer(config, *, initialize_new_group=False):
    if type(initialize_new_group) is not bool:
        raise ValueError('explicit new-group initialization choice required')
    try:
        from aiokafka import AIOKafkaConsumer
        from aiokafka.abc import ConsumerRebalanceListener
        from aiokafka.admin import AIOKafkaAdminClient
        from aiokafka.admin.config_resource import ConfigResource, ConfigResourceType
    except ImportError:
        raise RuntimeError('install the pinned optional orchestration requirements') from None

    state = AssignmentState()
    consumer = None
    class Listener(ConsumerRebalanceListener):
        async def on_partitions_revoked(self, revoked):
            state.revoke(revoked)

        async def on_partitions_assigned(self, assigned):
            try:
                # Bound the complete callback below the ten-second rebalance
                # timeout, not five seconds independently for every partition.
                async with asyncio.timeout(5):
                    await state.assign(assigned, consumer, initialize_new_group=initialize_new_group)
            except Exception:
                # SDK callback exceptions must not be logged with credential-bearing details.
                state.error = 'assignment_initialization_failed'

    options = consumer_options(config)
    admin_keys = {'bootstrap_servers', 'security_protocol', 'ssl_context', 'sasl_mechanism',
                  'sasl_plain_username', 'sasl_plain_password', 'request_timeout_ms', 'client_id'}
    admin = AIOKafkaAdminClient(**{key: value for key, value in options.items() if key in admin_keys})
    try:
        async with asyncio.timeout(30):
            await admin.start()
            await verify_topic(admin, ConfigResource(ConfigResourceType.TOPIC, TOPIC))
            consumer = AIOKafkaConsumer(**options)
            consumer.subscribe([TOPIC], listener=Listener())
            await consumer.start()
        if state.error:
            raise ConsumerStartupError(state.error)
        yield consumer, state
    finally:
        try:
            await state.close()
        finally:
            try:
                if consumer is not None:
                    async with asyncio.timeout(10):
                        await consumer.stop()
            finally:
                async with asyncio.timeout(5):
                    await admin.close()


async def _deliver(pool, consumer, state, tp, epoch, record):
    if not state.current(tp, epoch):
        raise ProjectionDeliveryError('assignment_lost')
    try:
        async with asyncio.timeout(5):
            outcome = await apply(pool, key=record.key, body=record.value)
    except ProjectionDeliveryError:
        raise
    except Exception:
        raise ProjectionDeliveryError('projection_failed') from None
    if outcome not in {'applied', 'duplicate', 'erased'}:
        raise ProjectionDeliveryError('projection_failed')
    # apply returns only after commit. Never commit the whole fetched batch.
    if not state.current(tp, epoch):
        raise ProjectionDeliveryError('assignment_lost')
    try:
        async with asyncio.timeout(5):
            await consumer.commit({tp: record.offset + 1})
    except Exception:
        # Broker may have accepted this commit before losing its response. SQL is
        # already committed; a future redelivery is harmless and is not a new write.
        raise ProjectionDeliveryError('commit_unconfirmed') from None
    return outcome


async def consume_batches(pool, consumer, state, *, max_batches=1, stop_event=None):
    if type(max_batches) is not int or not 1 <= max_batches <= 1000:
        raise ValueError('bounded consumer batch count required')
    counts = {'applied': 0, 'duplicate': 0, 'erased': 0, 'committed': 0}
    async with asyncio.timeout(300):
        for _ in range(max_batches):
            if stop_event is not None and stop_event.is_set():
                break
            if state.error:
                raise ConsumerStartupError(state.error)
            generation = state.generation
            async with asyncio.timeout(5):
                batch = await consumer.getmany(timeout_ms=1000, max_records=8)
            if sum(len(records) for records in batch.values()) > 8:
                raise ProjectionDeliveryError('record_invalid')
            if not any(batch.values()):
                break
            # A batch crossing rebalance is discarded only by stopping. Continuing
            # from an advanced fetch position could skip uncommitted records.
            if state.error or state.generation != generation:
                raise ProjectionDeliveryError('assignment_lost')
            epochs = state.owned.copy()
            for tp, records in batch.items():
                previous = -1
                for record in records:
                    if stop_event is not None and stop_event.is_set():
                        return counts
                    if (tp.topic != TOPIC or type(tp.partition) is not int or not 0 <= tp.partition <= 2
                            or record.topic != tp.topic or record.partition != tp.partition
                            or type(record.offset) is not int or record.offset < 0 or record.offset <= previous):
                        raise ProjectionDeliveryError('record_invalid')
                    previous = record.offset
                    task = asyncio.create_task(_deliver(pool, consumer, state, tp, epochs.get(tp), record))
                    state.active[tp] = task
                    try:
                        try:
                            outcome = await task
                        except asyncio.CancelledError:
                            if asyncio.current_task().cancelling():
                                raise
                            raise ProjectionDeliveryError('assignment_lost') from None
                    except ProjectionDeliveryError as error:
                        # One bounded attempt, then stop without advancing. No
                        # automatic poison skip, model retry loop or body logging.
                        async with asyncio.timeout(5):
                            await record_failure(pool, partition=tp.partition, offset=record.offset,
                                                 body=record.value, category=error.category)
                        raise
                    finally:
                        state.active.pop(tp, None)
                    counts[outcome] += 1
                    counts['committed'] += 1
                    if stop_event is not None and stop_event.is_set():
                        return counts
    return counts
