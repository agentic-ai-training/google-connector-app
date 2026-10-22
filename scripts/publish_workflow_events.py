"""Operator-started bounded publisher; no provisioning or automatic capture enable."""
import argparse
import asyncio
import json
import os
import ssl
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg

from app.runs.event_outbox import publish_once
from app.runs.kafka_delivery import KafkaDeliveryConfig, connect


def configuration(environ):
    dsn = environ.get('WORKFLOW_DATABASE_URL', '')
    target = urlsplit(dsn)
    if target.scheme not in {'postgres', 'postgresql'} or not target.hostname or not target.path.strip('/'):
        raise ValueError('explicit workflow database URL required')
    brokers = tuple(environ.get('WORKFLOW_KAFKA_BROKERS', '').split(','))
    plaintext = environ.get('WORKFLOW_KAFKA_LOCAL_PLAINTEXT', 'false')
    if plaintext not in {'true', 'false'}:
        raise ValueError('explicit Kafka plaintext setting required')
    kafka = KafkaDeliveryConfig(brokers=brokers, local_plaintext=plaintext == 'true',
        username=environ.get('WORKFLOW_KAFKA_USERNAME'), password=environ.get('WORKFLOW_KAFKA_PASSWORD'))
    return dsn, target.hostname, kafka


async def run(*, max_batches=1):
    if type(max_batches) is not int or not 1 <= max_batches <= 1000:
        raise ValueError('bounded batch count required')
    dsn, host, kafka = configuration(os.environ)
    context = False if host in {'localhost', '127.0.0.1', '::1'} else ssl.create_default_context()
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, timeout=10,
        command_timeout=5, ssl=context, server_settings={'application_name': 'gca-workflow-publisher'})
    total = {'published': 0, 'lease_lost': 0, 'unconfirmed': 0}
    try:
        async with connect(kafka) as producer:
            owner = 'workflow-' + uuid4().hex
            async with asyncio.timeout(300):
                for _ in range(max_batches):
                    result = await publish_once(pool, producer, owner=owner)
                    for key in total:
                        total[key] += result[key]
                    if not any(result.values()):
                        break
        return total
    finally:
        async with asyncio.timeout(10):
            await pool.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-batches', type=int, default=1)
    args = parser.parse_args()
    try:
        result = asyncio.run(run(max_batches=args.max_batches))
        unconfirmed = result['unconfirmed'] + result['lease_lost']
        state = ('partial' if result['published'] else 'unconfirmed') if unconfirmed else 'batch_finished'
        print(json.dumps({'delivery_state': state, **result}, sort_keys=True))
        return 1 if unconfirmed else 0
    except Exception as error:
        # DSNs, auth failures and broker errors can contain secrets. Never print
        # raw exception messages, config reprs or a traceback from this operator CLI.
        print(json.dumps({'delivery_state': 'unconfirmed', 'error_class': type(error).__name__,
                          'private_details_omitted': True}, sort_keys=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
