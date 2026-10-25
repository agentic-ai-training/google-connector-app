"""Deterministic Workspace control flow; all I/O stays in typed activities."""
import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from app.runs.temporal_contracts import (
        FinalizeRequest, RunSnapshot, StepRequest, WorkflowReference,
    )


@workflow.defn(name='gca.workspace.v1')
class WorkspaceWorkflow:
    def __init__(self):
        self.revision = 0
        self.status = 'starting'

    @workflow.signal(name='refresh')
    def refresh(self):
        # A hint only. Approval/cancellation/content NEVER comes from the signal.
        self.revision += 1

    @workflow.query(name='status')
    def query_status(self) -> str:
        return self.status

    @workflow.run
    async def run(self, reference: WorkflowReference) -> str:
        try:
            reference.validate()
        except (ValueError, TypeError, AttributeError):
            raise ApplicationError('Invalid workflow reference', type='WorkflowInput', non_retryable=True) from None
        deadline = workflow.now() + timedelta(hours=23)
        try:
            for _ in range(256):
                if workflow.now() >= deadline:
                    break
                observed_revision = self.revision
                try:
                    snapshot = await workflow.execute_activity('gca.workspace.inspect', reference,
                        result_type=RunSnapshot, start_to_close_timeout=timedelta(seconds=15),
                        retry_policy=RetryPolicy(maximum_attempts=3))
                except ActivityError as error:
                    if isinstance(error.cause, ApplicationError) and error.cause.non_retryable:
                        raise ApplicationError('Workflow metadata binding failed', type='WorkflowBinding', non_retryable=True) from None
                    await workflow.sleep(timedelta(seconds=5))
                    continue
                self.status = snapshot.status
                if snapshot.status == 'cancelled':
                    await workflow.execute_activity('gca.workspace.finalize', FinalizeRequest(reference, 'cancelled'),
                        result_type=str, start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=RetryPolicy(maximum_attempts=3))
                    return 'cancelled'
                if snapshot.status in {'completed', 'deleted'}:
                    return snapshot.status
                if snapshot.status in {'awaiting_approval', 'awaiting_clarification', 'partial', 'failed'}:
                    try:
                        await workflow.wait_condition(lambda: self.revision != observed_revision,
                            timeout=min(timedelta(hours=1), deadline - workflow.now()))
                    except asyncio.TimeoutError:
                        pass
                    continue
                if snapshot.status == 'awaiting_start_receipt':
                    await workflow.sleep(timedelta(seconds=5))
                    continue
                if snapshot.ready:
                    # Every business activity is one attempt. A repeated delivery
                    # reconciles the SQL journal; it cannot repeat an uncertain send.
                    outcomes = await asyncio.gather(*(workflow.execute_activity('gca.workspace.step', StepRequest(reference, step.step_id),
                            result_type=str, activity_id='step.' + step.step_id,
                            start_to_close_timeout=timedelta(minutes=5), heartbeat_timeout=timedelta(seconds=30),
                            retry_policy=RetryPolicy(maximum_attempts=1)) for step in snapshot.ready[:4]), return_exceptions=True)
                    if any(isinstance(outcome, BaseException) for outcome in outcomes):
                        await workflow.execute_activity('gca.workspace.finalize', FinalizeRequest(reference, 'activity_failure'),
                            result_type=str, start_to_close_timeout=timedelta(seconds=30),
                            retry_policy=RetryPolicy(maximum_attempts=3))
                else:
                    await workflow.execute_activity('gca.workspace.finalize', FinalizeRequest(reference),
                        result_type=str, start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=RetryPolicy(maximum_attempts=3))
            self.status = await workflow.execute_activity('gca.workspace.finalize', FinalizeRequest(reference, 'bounded_lifecycle'),
                result_type=str, start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3))
            return self.status
        except asyncio.CancelledError:
            # Cancellation is not proof that a provider write stopped. The
            # activity journal/finalizer records uncertain side effects.
            await asyncio.shield(workflow.execute_activity('gca.workspace.finalize', FinalizeRequest(reference, 'cancelled'),
                result_type=str, start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3)))
            raise
