"""Core schemas from Implementation Contract §3, as dataclasses.

Fields are exactly the contracted fields plus the minimal storage the
contract's own function signatures require (documented in DECISIONS.md):
  * Obligation.revision — required by abandon_obligation(expected_revision)
    and the abandon-vs-resolve race (Contract §9 row 9).
  * CompletionPolicy / RetryBudget value objects (Contract §3 / §5).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from v5.enums import (
    ActionStatus,
    ClaimConfidence,
    EvidenceStatus,
    GoalStatus,
    IdempotencyClass,
    IntegrityStatus,
    ObligationDisposition,
    PlanStatus,
    StepStatus,
    TaskStatus,
    VerificationResult,
)


@dataclass
class CompletionPolicy:
    """Contract §5 — explicit, persisted child-satisfaction rule.
    Kept deliberately separate from obligation closure (Law 20)."""
    rule: str  # "ALL_REQUIRED" | "ANY_REQUIRED" | "N_OF_M" | "CRITERION"
    n: int | None = None          # only for N_OF_M
    criterion_ref: str | None = None  # only for CRITERION — named evaluator


@dataclass
class RetryBudget:
    """Bound on retry attempts for a Task's actions (Law 11: retries are
    bounded; unknown ≠ infinite paralysis). Attempts_used is incremented
    atomically by retry_action (Contract §9 row 7)."""
    max_attempts: int
    attempts_used: int = 0


@dataclass
class Goal:
    id: str
    revision: int
    status: GoalStatus
    completion_policy: CompletionPolicy
    created_at: datetime
    session_origin: str | None
    integrity: IntegrityStatus = IntegrityStatus.OK


@dataclass
class Task:
    id: str
    revision: int
    goal_id: str
    status: TaskStatus
    active_plan_id: str | None          # THE canonical ownership field (Law 15/35)
    completion_policy: CompletionPolicy
    retry_budget: RetryBudget
    integrity: IntegrityStatus = IntegrityStatus.OK


@dataclass
class Plan:
    id: str
    revision: int
    task_id: str
    status: PlanStatus                  # never ACTIVE — Law 6 / Contract §4
    superseded_by: str | None
    dependency_graph: dict[str, set[str]]  # step_id -> depends_on step_ids (acyclic, Law 13)


@dataclass
class Step:
    id: str
    revision: int
    plan_id: str
    status: StepStatus
    required: bool                     # requiredness is Work's to set
    depends_on: set[str]


@dataclass
class Action:
    id: str
    revision: int
    step_id: str
    status: ActionStatus
    capability: str
    arguments: dict
    idempotency_class: IdempotencyClass
    confirmation_id: str | None
    retry_of: str | None = None         # prior action id when this is a retry (new identity, Law 10)
    integrity: str = "OK"               # storage field for Law 5 freeze on execution objects


@dataclass
class Observation:
    id: str
    action_id: str
    captured_at: datetime
    raw_result: Any
    execution_source: str


@dataclass
class Evidence:
    id: str
    status: EvidenceStatus
    acquisition_method: str             # orthogonal to status (Law 23)
    source: str
    relevance_to: str
    timestamp: datetime
    content: Any                       # immutable once written (Law 24)


@dataclass
class Claim:
    id: str
    asserts: str
    based_on: list[str]                 # evidence ids
    confidence: ClaimConfidence         # justified per relevant evidence (Law 27)
    made_by: str
    version: int                        # substantive change = new version


@dataclass
class Verification:
    id: str
    verifies: str                       # claim id
    method: str
    independence_level: str
    result: VerificationResult
    timestamp: datetime


@dataclass
class ObligationEvent:
    kind: str                           # "transferred" | "resolved" | "abandoned"
    from_owner: str
    to_owner: str | None
    authorized_by: str | None           # human identity for ABANDON (Law 21)
    reason: str
    timestamp: datetime


@dataclass
class Obligation:
    id: str
    origin_action_id: str
    owner: str                          # task id OR the sentinel "UOR" (Law 18)
    disposition: ObligationDisposition
    resolution_budget: int
    unknown_reason: str
    possible_external_effect: str
    safe_retry_conditions: str | None
    revision: int = 0                   # see module docstring — CAS token
    history: list[ObligationEvent] = field(default_factory=list)


# ── Mutation results (Law 4: rejections return authoritative current state) ──

@dataclass
class Ok:
    value: Any = None
    noop: bool = False                  # idempotent no-op (e.g. transfer to current owner)


@dataclass
class Rejected:
    reason: str                         # machine-readable, e.g. STALE_REVISION
    detail: str = ""
    current: Any = None                 # authoritative current state


Result = Ok | Rejected


# Rejection reason constants (asserted by tests; losers must see these, not
# generic exceptions).
R_NOT_FOUND = "NOT_FOUND"
R_STALE_REVISION = "STALE_REVISION"
R_INVALID_TRANSITION = "INVALID_TRANSITION"
R_FROZEN = "FROZEN"
R_PLAN_EXCLUSIVITY = "PLAN_EXCLUSIVITY"
R_PLAN_NOT_DRAFT = "PLAN_NOT_DRAFT"
R_PLAN_MISMATCH = "PLAN_MISMATCH"
R_COMPLETION_POLICY = "COMPLETION_POLICY_UNSATISFIED"
R_VERIFICATION_NOT_PASS = "VERIFICATION_NOT_PASS"
R_NOT_OPEN = "OBLIGATION_NOT_OPEN"
R_WRONG_OWNER = "WRONG_OWNER"
R_RECOVERY_FORBIDDEN = "RECOVERY_FORBIDDEN"
R_CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
R_RETRY_BUDGET = "RETRY_BUDGET_EXHAUSTED"
R_NOT_RETRYABLE = "NOT_RETRYABLE"
R_DEPENDENCY_CYCLE = "DEPENDENCY_CYCLE"
R_FABRICATION = "REPAIR_FABRICATION"    # Law 29: no manufactured Observation-backed state
R_STALE_REPAIR = "STALE_REPAIR"
R_AGGREGATE_INVALID = "AGGREGATE_INVARIANT_VIOLATION"
R_INTEGRITY_TERMINAL = "INTEGRITY_TERMINAL"
R_TERMINAL = "ALREADY_TERMINAL"
R_CONFIDENCE = "CONFIDENCE_NOT_JUSTIFIED"
R_EVIDENCE_STATUS = "EVIDENCE_STATUS_FORBIDDEN"
