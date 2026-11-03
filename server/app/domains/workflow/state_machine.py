"""Every legal move between workflow states. Anything not listed here raises."""

from app.domains.workflow.models import WorkflowState as S


class IllegalTransition(Exception):
    def __init__(self, current: S, target: S) -> None:
        super().__init__(f"illegal workflow transition: {current} -> {target}")
        self.current = current
        self.target = target


TRANSITIONS: dict[S, frozenset[S]] = {
    S.INTAKE: frozenset({S.SEARCHING, S.LIMIT_REACHED, S.FAILED}),
    S.SEARCHING: frozenset({S.COMPARING, S.INTAKE, S.LIMIT_REACHED, S.FAILED}),
    S.COMPARING: frozenset({S.SEARCHING, S.INTAKE, S.AWAITING_APPROVAL, S.LIMIT_REACHED, S.FAILED}),
    S.AWAITING_APPROVAL: frozenset({S.EXECUTING, S.COMPARING, S.INTAKE, S.FAILED}),
    # Executing can only move to verifying. After a timeout, nothing reaches a retry or DONE
    # without passing the status check first.
    S.EXECUTING: frozenset({S.VERIFYING, S.FAILED}),
    S.VERIFYING: frozenset({S.DONE, S.EXECUTING, S.COMPARING, S.FAILED}),
    # A provider can cancel a finished booking, which reopens the errand.
    S.DONE: frozenset({S.DISRUPTED}),
    S.DISRUPTED: frozenset({S.COMPARING, S.FAILED}),
    S.LIMIT_REACHED: frozenset({S.INTAKE, S.FAILED}),
    S.FAILED: frozenset(),
}


def check_transition(current: S, target: S) -> None:
    if target not in TRANSITIONS[current]:
        raise IllegalTransition(current, target)
