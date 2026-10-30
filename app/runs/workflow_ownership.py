"""Business execution fences, separate from event delivery and policy cohorts."""
from uuid import UUID


class WorkflowOwnershipError(RuntimeError):
    pass


def workflow_id(kind: str, identity) -> str:
    if kind not in {'workspace', 'coding'}:
        raise WorkflowOwnershipError('unknown workflow kind')
    return 'gca.' + kind + '.' + str(UUID(str(identity)))


async def postgres_claim_predicate(conn) -> str:
    # Staged compatibility with pre-026 schemas. Presence is checked inside the
    # claim transaction. Database triggers independently reject old-worker leases.
    present = await conn.fetchval("SELECT to_regclass('workflow_ownership_control') IS NOT NULL")
    return "orchestration_owner='postgres'" if present else 'TRUE'


async def require_postgres_lease(pool, kind: str, run) -> None:
    queries = {
        'workspace': 'SELECT *,lease_expires_at>now() AS lease_current FROM agent_runs WHERE id=$1',
        'coding': 'SELECT *,lease_expires_at>now() AS lease_current FROM coding_runs WHERE id=$1',
    }
    if kind not in queries:
        raise WorkflowOwnershipError('unknown workflow kind')
    async with pool.acquire() as conn:
        row = await conn.fetchrow(queries[kind], run['id'])
    if (not row or row.get('orchestration_owner', 'postgres') != 'postgres'
            or row['user_id'] != run['user_id'] or row['deleted_at'] is not None
            or not row['lease_current'] or not row['lease_owner']
            or row['lease_owner'] != run['lease_owner']
            or row['executor_version'] != run['executor_version']
            or row['status'] not in {'running', 'planning', 'executing', 'ci_running'}):
        raise WorkflowOwnershipError('current PostgreSQL workflow ownership and lease required')
