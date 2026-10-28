"""Live PostgreSQL coding-phase admission and SQL-only completion fences.

Temporal-owned runs remain denied here; its activity bridge must supply its own
journal authority, never a fabricated PostgreSQL lease.
"""
import asyncio
from contextlib import asynccontextmanager


class CodingExecutionFenced(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__('Coding phase authority changed; further work is fenced.')


class BorrowedConnection:
    """Expose only the connection already owned by a locked SQL transaction."""
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


SOURCE_FIELDS = ('id','user_id','executor_version','repository','base_ref','base_commit','request_hash',
    'encrypted_request','plan_hash','encrypted_plan','approval_action_hash','planner_model','model_policy_version',
    'tool_policy_version','okf_bundle_version','source_egress_consented_at','attempt_count',
    'candidate_commit','branch_name','pull_request_number','pull_request_url')


async def check(conn, expected, *, approval_required=True):
    current = await conn.fetchrow('''SELECT *,lease_expires_at>clock_timestamp() AS live_lease,
      approval_expires_at>clock_timestamp() AS live_approval FROM coding_runs WHERE id=$1 AND user_id=$2''',
      expected['id'], expected['user_id'])
    if (not current or current['deleted_at'] is not None or not current['live_lease']
            or not current['lease_owner'] or current['lease_owner'] != expected['lease_owner']
            or current.get('orchestration_owner','postgres') != 'postgres'
            or current['status'] not in {'planning','executing','ci_running'} or current['status'] != expected['status']
            or any(current.get(key) != expected.get(key) for key in SOURCE_FIELDS)):
        raise CodingExecutionFenced('live_owner_or_source_changed')
    if approval_required and current['status'] == 'executing' and (current['approval_status'] != 'approved' or not current['live_approval']
            or not current['plan_hash'] or current['approval_action_hash'] != current['plan_hash']
            or current['approved_by'] != current['user_id']):
        raise CodingExecutionFenced('current_plan_approval_required')
    return current


async def require_coding_authority(pool, expected):
    try:
        async with asyncio.timeout(5), pool.acquire(timeout=2) as conn:
            return await check(conn, expected)
    except CodingExecutionFenced:
        raise
    except Exception:
        raise CodingExecutionFenced('admission_unavailable') from None


@asynccontextmanager
async def coding_commit(pool, expected, *, approval_required=True):
    """No provider/network/process work may be done inside this SQL transaction."""
    async with pool.acquire(timeout=5) as conn, conn.transaction():
        await conn.execute("SET LOCAL lock_timeout='2s'; SET LOCAL statement_timeout='5s'")
        if not await conn.fetchval('SELECT id FROM coding_runs WHERE id=$1 AND user_id=$2 FOR UPDATE',
                expected['id'], expected['user_id']):
            raise CodingExecutionFenced('run_not_visible')
        await check(conn, expected, approval_required=approval_required)
        yield conn
