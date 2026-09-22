"""Shared fixtures: every test gets an isolated SQLite store under tmp_path."""
from __future__ import annotations

import pytest

from v5.models import CompletionPolicy, RetryBudget
from v5.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "state.db"))
    yield s
    s.close()


def make_goal_task_plan_step(store, *, required_step=True):
    """E2E scaffolding: Goal -> Task -> Plan(DRAFT) -> one Step(PENDING).

    Returns (goal, task, plan, step). The Task is left PENDING — tests call
    work.transition_object to activate it explicitly."""
    from v5 import work

    goal = work.create_goal(store, CompletionPolicy(rule="ALL_REQUIRED"))
    task_r = work.create_task(store, goal.id, CompletionPolicy(rule="ALL_REQUIRED"),
                              RetryBudget(max_attempts=2))
    assert isinstance(task_r, Ok_type()), task_r
    task = task_r.value
    plan_r = work.create_plan(store, task.id)
    assert isinstance(plan_r, Ok_type())
    plan = plan_r.value
    step_r = work.create_step(store, plan.id, required=required_step)
    assert isinstance(step_r, Ok_type())
    step = step_r.value
    return goal, task, plan, step


def Ok_type():
    from v5.models import Ok
    return Ok


def activated(store, task, plan):
    """Activate a task then its plan (the legal order)."""
    from v5 import work
    from v5.enums import TaskStatus
    r = work.transition_object(store, task.id, TaskStatus.ACTIVE, task.revision)
    assert isinstance(r, Ok_type()), r
    task = r.value
    r = work.activate_plan(store, task.id, plan.id, task.revision)
    assert isinstance(r, Ok_type()), r
    return r.value


def pending_action(store, step, capability="file_write", args=None,
                   idempotency_class=None):
    from v5 import execution
    from v5.enums import IdempotencyClass
    r = execution.create_action(
        store, step.id, capability, args or {"path": "/tmp/should_not_exist.txt", "content": "x"},
        idempotency_class or IdempotencyClass.IDEMPOTENT,
    )
    assert isinstance(r, Ok_type()), r
    return r.value
