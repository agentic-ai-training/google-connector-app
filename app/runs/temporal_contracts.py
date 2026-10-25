"""Reference-only workflow history contracts; never private task/tool content."""
from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True)
class WorkflowReference:
    schema: str
    kind: str
    run_id: str
    workflow_version: str
    executor_version: str

    def validate(self):
        if (self.schema != 'gca.temporal.start.v1' or self.kind != 'workspace'
                or str(UUID(self.run_id)) != self.run_id
                or not 1 <= len(self.workflow_version) <= 128
                or not 1 <= len(self.executor_version) <= 512):
            raise ValueError('invalid Workspace workflow reference')


@dataclass(frozen=True)
class StepReference:
    step_id: str
    read_only: bool


@dataclass(frozen=True)
class RunSnapshot:
    status: str
    ready: list[StepReference] = field(default_factory=list)


@dataclass(frozen=True)
class StepRequest:
    run: WorkflowReference
    step_id: str


@dataclass(frozen=True)
class FinalizeRequest:
    run: WorkflowReference
    reason: str = 'verified_steps'
