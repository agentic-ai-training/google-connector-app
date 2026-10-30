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
    generation: int = 0
    permit_id: str | None = None


@dataclass(frozen=True)
class RunSnapshot:
    status: str
    ready: list[StepReference] = field(default_factory=list)


@dataclass(frozen=True)
class StepRequest:
    run: WorkflowReference
    step_id: str
    generation: int = 0
    permit_id: str | None = None

    def validate(self):
        self.run.validate()
        if (str(UUID(self.step_id)) != self.step_id or type(self.generation) is not int
                or not 0 <= self.generation <= 3
                or (self.generation == 0 and self.permit_id is not None)
                or (self.generation > 0 and (not self.permit_id or str(UUID(self.permit_id)) != self.permit_id))):
            raise ValueError('invalid step generation reference')

    @property
    def activity_id(self):
        self.validate()
        return ('step.' + self.step_id if self.generation == 0 else
                f'step.{self.step_id}.g{self.generation}.{self.permit_id}')


@dataclass(frozen=True)
class FinalizeRequest:
    run: WorkflowReference
    reason: str = 'verified_steps'
