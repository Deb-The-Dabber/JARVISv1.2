"""Enums from Implementation Contract §2 — exact, no invented values.

Constitutional notes encoded here (do not "fix" them):
  * PlanStatus has NO ACTIVE value — Plan activity is derived from
    Task.active_plan_id (Law 6 / Contract §4). Never store activity.
  * ObligationDisposition has NO TRANSFERRED value — transfer is an
    ownership mutation plus immutable history (Law 18/19). A transferred
    obligation remains OPEN until RESOLVED or ABANDONED.
"""
from __future__ import annotations

from enum import Enum


class GoalStatus(Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    ARCHIVED = "ARCHIVED"


class TaskStatus(Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ARCHIVED = "ARCHIVED"


class PlanStatus(Enum):
    DRAFT = "DRAFT"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"
    ARCHIVED = "ARCHIVED"
    # NOTE: no ACTIVE value here — see Law 6 / Contract §4.


class StepStatus(Enum):
    PENDING = "PENDING"
    READY = "READY"
    EXECUTING = "EXECUTING"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


class ActionStatus(Enum):
    PENDING = "PENDING"
    EXECUTING = "EXECUTING"
    OBSERVED = "OBSERVED"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class VerificationResult(Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class EvidenceStatus(Enum):
    CONFIRMED_SOURCE = "CONFIRMED_SOURCE"
    CONFIRMED_RUNTIME = "CONFIRMED_RUNTIME"
    INFERENCE = "INFERENCE"
    UNKNOWN = "UNKNOWN"


class ClaimConfidence(Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNVERIFIED = "UNVERIFIED"


class ObligationDisposition(Enum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    ABANDONED = "ABANDONED"
    # NOTE: no TRANSFERRED value — transfer is ownership change + history event.


class IdempotencyClass(Enum):
    IDEMPOTENT = "IDEMPOTENT"
    CONDITIONALLY_IDEMPOTENT = "CONDITIONALLY_IDEMPOTENT"
    NON_IDEMPOTENT = "NON_IDEMPOTENT"
    UNKNOWN = "UNKNOWN"


class IntegrityStatus(Enum):
    OK = "OK"
    FROZEN = "FROZEN"
    ABANDONED_UNREPAIRABLE = "ABANDONED_UNREPAIRABLE"


# ── Explicit transition tables (Law 34: no implicit transitions) ─────────────
# Nothing is permitted merely because it isn't prohibited. These tables ARE the
# transition contract for each lifecycle. Terminal states never reopen (Law 14).

GOAL_TRANSITIONS: dict[GoalStatus, frozenset] = {
    GoalStatus.ACTIVE: frozenset(
        {GoalStatus.PAUSED, GoalStatus.BLOCKED, GoalStatus.COMPLETED,
         GoalStatus.CANCELLED, GoalStatus.ARCHIVED}
    ),
    GoalStatus.PAUSED: frozenset(
        {GoalStatus.ACTIVE, GoalStatus.COMPLETED, GoalStatus.CANCELLED,
         GoalStatus.ARCHIVED}
    ),
    GoalStatus.BLOCKED: frozenset({GoalStatus.ACTIVE, GoalStatus.CANCELLED}),
}

TASK_TRANSITIONS: dict[TaskStatus, frozenset] = {
    TaskStatus.PENDING: frozenset(
        {TaskStatus.ACTIVE, TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.ARCHIVED}
    ),
    TaskStatus.ACTIVE: frozenset(
        {TaskStatus.PAUSED, TaskStatus.BLOCKED, TaskStatus.COMPLETED,
         TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.ARCHIVED}
    ),
    TaskStatus.PAUSED: frozenset(
        {TaskStatus.ACTIVE, TaskStatus.COMPLETED, TaskStatus.CANCELLED,
         TaskStatus.ARCHIVED}
    ),
    TaskStatus.BLOCKED: frozenset({TaskStatus.ACTIVE, TaskStatus.CANCELLED}),
}

PLAN_TRANSITIONS: dict[PlanStatus, frozenset] = {
    PlanStatus.DRAFT: frozenset(
        {PlanStatus.BLOCKED, PlanStatus.COMPLETED, PlanStatus.FAILED,
         PlanStatus.CANCELLED, PlanStatus.SUPERSEDED, PlanStatus.ARCHIVED}
    ),
    PlanStatus.BLOCKED: frozenset({PlanStatus.DRAFT, PlanStatus.CANCELLED}),
}

STEP_TRANSITIONS: dict[StepStatus, frozenset] = {
    StepStatus.PENDING: frozenset(
        {StepStatus.READY, StepStatus.BLOCKED, StepStatus.CANCELLED, StepStatus.SKIPPED}
    ),
    StepStatus.READY: frozenset(
        {StepStatus.EXECUTING, StepStatus.BLOCKED, StepStatus.CANCELLED,
         StepStatus.SKIPPED, StepStatus.FAILED}
    ),
    StepStatus.EXECUTING: frozenset(
        {StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.BLOCKED,
         StepStatus.CANCELLED}
    ),
    StepStatus.BLOCKED: frozenset(
        {StepStatus.READY, StepStatus.CANCELLED, StepStatus.SKIPPED}
    ),
}

ACTION_TRANSITIONS: dict[ActionStatus, frozenset] = {
    ActionStatus.PENDING: frozenset({ActionStatus.EXECUTING, ActionStatus.CANCELLED}),
    ActionStatus.EXECUTING: frozenset(
        {ActionStatus.OBSERVED, ActionStatus.UNKNOWN_OUTCOME, ActionStatus.FAILED,
         ActionStatus.CANCELLED}
    ),
}

# Terminal states per type — Law 14: never silently reopen.
GOAL_TERMINAL = frozenset(
    {GoalStatus.COMPLETED, GoalStatus.CANCELLED, GoalStatus.ARCHIVED}
)
TASK_TERMINAL = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.ARCHIVED}
)
PLAN_TERMINAL = frozenset(
    {PlanStatus.COMPLETED, PlanStatus.FAILED, PlanStatus.CANCELLED,
     PlanStatus.SUPERSEDED, PlanStatus.ARCHIVED}
)
STEP_TERMINAL = frozenset(
    {StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.CANCELLED, StepStatus.SKIPPED}
)
ACTION_TERMINAL = frozenset(
    {ActionStatus.OBSERVED, ActionStatus.UNKNOWN_OUTCOME, ActionStatus.FAILED,
     ActionStatus.CANCELLED}
)
