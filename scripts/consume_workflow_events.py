"""Operator-started fixed SQL projection consumer; never runs Google/deployment tools."""
import argparse
import asyncio
import json
import os
import signal
import ssl

import asyncpg

from app.runs.kafka_consumer import ConsumerStartupError, connect_consumer, consume_batches
from app.runs.workflow_projection import ProjectionDeliveryError
from scripts.publish_workflow_events import configuration


async def run(*, max_batches=1, initialize_new_group=False):
    if type(max_batches) is not int or not 1 <= max_batches <= 1000 or type(initialize_new_group) is not bool:
        raise ValueError('bounded batches and explicit bootstrap choice required')
    dsn, host, kafka = configuration(os.environ)
    context = False if host in {'localhost', '127.0.0.1', '::1'} else ssl.create_default_context()
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3, timeout=10,
        command_timeout=5, ssl=context, server_settings={'application_name': 'gca-workflow-projection'})
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    handlers = []
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(signum, stop.set)
                handlers.append(signum)
            except (NotImplementedError, RuntimeError):
                pass
        async with connect_consumer(kafka, initialize_new_group=initialize_new_group) as (consumer, state):
            result = await consume_batches(pool, consumer, state, max_batches=max_batches, stop_event=stop)
            return {'consumer_state': 'stopped' if stop.is_set() else 'batch_finished', **result}
    finally:
        for signum in handlers:
            loop.remove_signal_handler(signum)
        async with asyncio.timeout(10):
            await pool.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-batches', type=int, default=1)
    parser.add_argument('--initialize-new-group', action='store_true',
                        help='Explicitly start missing group offsets at the retained beginning; never reset existing offsets')
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(run(max_batches=args.max_batches,
            initialize_new_group=args.initialize_new_group)), sort_keys=True))
        return 0
    except Exception as error:
        category = error.category if isinstance(error, (ConsumerStartupError, ProjectionDeliveryError)) else 'consumer_unconfirmed'
        print(json.dumps({'consumer_state': 'unconfirmed', 'error_class': type(error).__name__,
                          'error_category': category,
                          'private_details_omitted': True}, sort_keys=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
