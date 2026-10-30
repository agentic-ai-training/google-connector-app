"""Explicit optional worker construction; importing this module starts nothing."""
from datetime import timedelta
from app.runs.temporal_activities import WorkspaceActivities
from app.runs.temporal_workflow import WorkspaceWorkflow


def build_workspace_worker(client, pool, app, *, namespace, task_queue, workflow_version, executor_version):
    from temporalio.worker import Worker
    if client.namespace != namespace:
        raise ValueError('Temporal worker namespace mismatch')
    if not all(isinstance(value, str) and 1 <= len(value) <= 128
               for value in (namespace, task_queue, workflow_version)):
        raise ValueError('bounded explicit worker binding required')
    if not isinstance(executor_version, str) or not 1 <= len(executor_version) <= 512:
        raise ValueError('bounded executor version required')
    activities = WorkspaceActivities(pool, app, namespace=namespace, task_queue=task_queue,
        workflow_version=workflow_version, executor_version=executor_version)
    return Worker(client, task_queue=task_queue, workflows=[WorkspaceWorkflow],
        activities=[activities.inspect, activities.execute_step, activities.finalize],
        max_concurrent_activities=4, max_concurrent_workflow_tasks=4,
        max_cached_workflows=100, graceful_shutdown_timeout=timedelta(seconds=30))
