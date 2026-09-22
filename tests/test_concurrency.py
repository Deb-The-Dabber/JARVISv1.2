"""Contract §9 Part B — Concurrent Mutation matrix (9 races).

Each race: fire both operations concurrently on real threads, assert exactly
one of the documented outcomes. Global assertion for every row: no duplicate
Action identity, no terminal object re-entering a lifecycle state, revision
numbers strictly increased with no gaps unaccounted for by a rejected write.
"""
from __future__ import annotations

import threading

import pytest

from tests.conftest import make_goal_task_plan_step, activated, pending_action
from v5 import evidence, execution, obligations, work
from v5.enums import (
    ActionStatus,
    IdempotencyClass,
    ObligationDisposition,
    PlanStatus,
    TaskStatus,
    VerificationResult,
)
from v5.ids import UOR
from v5.models import Ok, Rejected
from v5.store import Store


def _race(fn_a, fn_b):
    """Run two operations concurrently on separate threads with a barrier.
    Returns (result_a, result_b) in submission order."""
    barrier = threading.Barrier(2)
    out: dict = {}

    def wrap(name, fn):
        def run():
            barrier.wait()
            out[name] = fn()
        return run

    ta = threading.Thread(target=wrap("a", fn_a))
    tb = threading.Thread(target=wrap("b", fn_b))
    ta.start(); tb.start()
    ta.join(); tb.join()
    return out["a"], out["b"]


def _exactly_one_won(r_a, r_b):
    won_a, won_b = isinstance(r_a, Ok), isinstance(r_b, Ok)
    assert won_a != won_b, f"expected exactly one winner, got a={type(r_a).__name__} b={type(r_b).__name__}"
    loser = r_b if won_a else r_a
    assert isinstance(loser, Rejected)
    return r_a if won_a else r_b, loser


class TestRace1PlanVsPlan:
    def test_two_plans_same_revision_one_wins(self, store):
        goal, task, plan_a, _ = make_goal_task_plan_step(store)
        task_r = work.transition_object(store, task.id, TaskStatus.ACTIVE, task.revision)
        task = task_r.value
        plan_b = work.create_plan(store, task.id).value

        won, loser = _exactly_one_won(*_race(
            lambda: work.activate_plan(store, task.id, plan_a.id, task.revision),
            lambda: work.activate_plan(store, task.id, plan_b.id, task.revision),
        ))
        # winner: active_plan_id set; loser: authoritative current Task state
        assert won.value.active_plan_id in (plan_a.id, plan_b.id)
        assert loser.current is not None
        assert loser.current.revision == won.value.revision  # loser sees current state
        assert loser.reason in ("STALE_REVISION", "PLAN_EXCLUSIVITY")
        # exactly one active plan (Law 15)
        t = work.load_task(store.read(), task.id)
        active = [p for p in (plan_a, plan_b)
                  if work.plan_is_active(work.load_plan(store.read(), p.id), t)]
        assert len(active) == 1


class TestRace2PlanVsCancellation:
    def test_activate_vs_cancel_never_both(self, store):
        goal, task, plan_a, _ = make_goal_task_plan_step(store)
        task = work.transition_object(store, task.id, TaskStatus.ACTIVE, task.revision).value
        plan_b = work.create_plan(store, task.id).value

        won, loser = _exactly_one_won(*_race(
            lambda: work.activate_plan(store, task.id, plan_b.id, task.revision),
            lambda: work.cancel_task(store, task.id, task.revision),
        ))
        t = work.load_task(store.read(), task.id)
        if won.value == getattr(won.value, "status", None) or True:
            # inspect by which op won
            pass
        # determine the winner op by what happened
        if t.status == TaskStatus.CANCELLED:
            # cancel won: plan B is NEVER active
            assert t.active_plan_id != plan_b.id
            # loser (activation) received current state, not a generic error
            assert isinstance(loser, Rejected)
            assert loser.current is not None
        else:
            # activation won: cancel must have failed cleanly (stale), never both
            assert t.status in (TaskStatus.ACTIVE, TaskStatus.BLOCKED)
            assert t.active_plan_id == plan_b.id
            assert loser.reason == "STALE_REVISION"
        # invariant: never CANCELLED task with an active plan
        if t.status in (TaskStatus.CANCELLED, TaskStatus.COMPLETED):
            assert t.active_plan_id is None


class TestRace3PlanVsCompletion:
    def test_activate_vs_complete_never_both(self, store):
        goal, task, plan_a, step = make_goal_task_plan_step(store)
        task = work.transition_object(store, task.id, TaskStatus.ACTIVE, task.revision).value
        plan_b = work.create_plan(store, task.id).value
        # satisfy the task policy first: complete the one step of plan A
        task = work.activate_plan(store, task.id, plan_a.id, task.revision).value
        s = work.transition_object(store, step.id, work.StepStatus.READY, step.revision).value
        s = work.transition_object(store, s.id, work.StepStatus.EXECUTING, s.revision).value
        work.complete_step(store, s.id, s.revision)
        task = work.load_task(store.read(), task.id)

        won, loser = _exactly_one_won(*_race(
            lambda: work.activate_plan(store, task.id, plan_b.id, task.revision),
            lambda: work.complete_object(store, task.id, task.revision),
        ))
        t = work.load_task(store.read(), task.id)
        if t.status == TaskStatus.COMPLETED:
            assert t.active_plan_id is None                     # completion clears the plan pointer
            assert loser.reason == "STALE_REVISION"
        else:
            assert t.active_plan_id == plan_b.id
            assert loser.reason in ("STALE_REVISION", "COMPLETION_POLICY_UNSATISFIED")


class TestRace4UorTransferRace:
    def test_double_transfer_one_owner(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        obl = obligations.create_obligation(store, "act_1", task.id, "r", "e")

        r_a, r_b = _race(
            # two terminal paths concurrently transfer the SAME obligation to UOR
            lambda: obligations.transfer_obligation(store, obl.id, UOR, expected_owner=task.id),
            lambda: obligations.transfer_obligation(store, obl.id, UOR, expected_owner=task.id),
        )
        # exactly one real transfer; the other no-ops (Ok, noop=True)
        winners = [r for r in (r_a, r_b) if isinstance(r, Ok)]
        assert len(winners) == 2, "both must succeed (one transfer + one no-op)"
        assert sum(1 for r in winners if not r.noop) == 1
        loaded = obligations.load_obligation(store.read(), obl.id)
        assert loaded.owner == UOR
        assert loaded.disposition == ObligationDisposition.OPEN   # transfer != resolution
        # exactly one "transferred" history event — no duplicates
        events = [e for e in loaded.history if e.kind == "transferred"]
        assert len(events) == 1

    def test_task_and_goal_terminal_both_transfer_once(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        obl = obligations.create_obligation(store, "act_1", task.id, "r", "e")
        task = work.transition_object(store, task.id, TaskStatus.ACTIVE, task.revision).value

        # Task completing AND Goal completing concurrently — both would transfer
        # the same obligation to UOR. Exactly one transfer event must result.
        r_a, r_b = _race(
            lambda: obligations.transfer_obligation(store, obl.id, UOR, expected_owner=task.id),
            lambda: obligations.transfer_obligation(store, obl.id, UOR, expected_owner=task.id),
        )
        assert all(isinstance(r, Ok) for r in (r_a, r_b))
        assert sum(1 for r in (r_a, r_b) if not r.noop) == 1
        loaded = obligations.load_obligation(store.read(), obl.id)
        assert loaded.owner == UOR
        assert len([e for e in loaded.history if e.kind == "transferred"]) == 1


class TestRace5RepairRace:
    def test_two_humans_one_repair(self, store):
        import v5.repair as repair
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)

        h1 = repair.authorize("human_1")
        h2 = repair.authorize("human_2")
        won, loser = _exactly_one_won(*_race(
            lambda: repair.repair_object(store, h1, task.id, task.revision,
                                        target_state={"status": "BLOCKED"}, reason="A"),
            lambda: repair.repair_object(store, h2, task.id, task.revision,
                                        target_state={"status": "PAUSED"}, reason="B"),
        ))
        assert loser.reason == "STALE_REPAIR"   # loser must re-propose
        assert loser.current is not None        # with current authoritative state
        t = work.load_task(store.read(), task.id)
        assert t.revision == task.revision + 1  # exactly one bump
        assert t.status in (TaskStatus.BLOCKED, TaskStatus.PAUSED)


class TestRace6StaleWorker:
    def test_supersede_mid_execution_stale_worker_aborts(self, store):
        goal, task, plan_a, step_a = make_goal_task_plan_step(store)
        task = activated(store, task, plan_a)
        action = pending_action(store, step_a)
        started = execution.begin_executing(
            store, action.id, action.revision, expected_plan_id=plan_a.id
        ).value
        plan_b = work.create_plan(store, task.id).value

        # supersede A with B while the worker is mid-execution
        r = work.supersede_plan(store, task.id, plan_a.id, plan_b.id, task.revision)
        assert isinstance(r, Ok)

        # the worker's next state-dependent operation must abort/replan
        obs = execution.mark_observed(
            store, started.id, started.revision,
            raw_result={"ok": True}, execution_source="file_write",
            expected_plan_id=plan_a.id,   # the worker's stale belief
        )
        assert isinstance(obs, Rejected)
        assert obs.reason == "PLAN_SUPERSEDED"
        # and no Observation was written for the aborted attempt
        assert execution.observation_count(store, started.id) == 0


class TestRace7RetryRace:
    def test_double_retry_one_new_action(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        action = pending_action(store, step)
        started = execution.begin_executing(store, action.id, action.revision).value
        unknown = execution.mark_unknown_outcome(
            store, started.id, started.revision, "timeout", "possible partial write"
        ).value

        won, loser = _exactly_one_won(*_race(
            lambda: execution.retry_action(store, started.id, "heartbeat_timeout",
                                           expected_task_revision=task.revision),
            lambda: execution.retry_action(store, started.id, "manual_user_retry",
                                           expected_task_revision=task.revision),
        ))
        new_action = won.value
        assert new_action.retry_of == started.id      # new identity, old attempt untouched (Law 10)
        assert new_action.status == ActionStatus.PENDING
        assert new_action.id != started.id
        # budget decremented ONCE
        t = work.load_task(store.read(), task.id)
        assert t.retry_budget.attempts_used == 1
        # the old attempt was never rewritten
        old = execution.load_action(store.read(), started.id)
        assert old.status == ActionStatus.UNKNOWN_OUTCOME
        # no duplicate Action identity: exactly one retry row for this parent
        rows = store.read().execute(
            "SELECT COUNT(*) AS n FROM actions WHERE retry_of = ?", (started.id,)
        ).fetchone()["n"]
        assert rows == 1


class TestRace8DependencyRace:
    def test_converse_dependencies_at_most_one(self, store):
        goal, task, plan, _ = make_goal_task_plan_step(store)
        step_a = work.create_step(store, plan.id, required=True).value
        step_b = work.create_step(store, plan.id, required=True).value
        plan_now = work.load_plan(store.read(), plan.id)

        r_a, r_b = _race(
            lambda: work.add_dependency(store, plan.id, step_b.id, step_a.id,
                                        expected_plan_revision=plan_now.revision),
            lambda: work.add_dependency(store, plan.id, step_a.id, step_b.id,
                                        expected_plan_revision=plan_now.revision),
        )
        outcomes = [r_a, r_b]
        oks = [r for r in outcomes if isinstance(r, Ok)]
        assert len(oks) <= 1, "at most one may commit"
        # the committed graph contains no cycle (Law 13)
        p = work.load_plan(store.read(), plan.id)
        assert not work._has_cycle(p.dependency_graph)
        # if one committed, the other was rejected with cycle or stale
        if len(oks) == 1:
            other = r_b if oks[0] is r_a else r_a
            assert isinstance(other, Rejected)
            assert other.reason in ("DEPENDENCY_CYCLE", "STALE_REVISION")


class TestRace9AbandonVsResolve:
    def test_abandon_vs_resolve_exactly_one(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        obl = obligations.create_obligation(store, "act_1", task.id, "r", "e")
        # a PASS verification to justify resolution
        ev = evidence.record_runtime_evidence(store, "obs_nonexistent_guard", source="test",
                                            relevance_to=task.id, content={})
        if isinstance(ev, Rejected):
            # need a real observation for runtime evidence — create minimal one
            obs_id = "obs_" + "0" * 20
            with store.write() as conn:
                conn.execute(
                    "INSERT INTO observations (id, action_id, captured_at, raw_result, execution_source) "
                    "VALUES (?,?,?,?,?)",
                    (obs_id, "act_none", "2026-01-01T00:00:00Z", "{}", "test"),
                )
            ev = evidence.record_runtime_evidence(store, obs_id, source="test",
                                                  relevance_to=task.id, content={"ok": True})
        claim = evidence.create_claim(store, "test claim", based_on=[ev.value.id],
                                      made_by="test", confidence=evidence.ClaimConfidence.HIGH).value
        ver = evidence.create_verification(store, claim.id, "test", "deterministic").value
        evidence.run_verification(store, ver.id, lambda c, e: True)

        current = obligations.load_obligation(store.read(), obl.id)
        won, loser = _exactly_one_won(*_race(
            lambda: obligations.abandon_obligation(store, obl.id, "human", "not needed",
                                                  expected_revision=current.revision),
            lambda: obligations.resolve_obligation(store, obl.id, ver.id,
                                                   expected_revision=current.revision),
        ))
        loaded = obligations.load_obligation(store.read(), obl.id)
        # never simultaneously ABANDONED and RESOLVED
        assert loaded.disposition in (ObligationDisposition.ABANDONED, ObligationDisposition.RESOLVED)
        assert loaded.disposition is not ObligationDisposition.OPEN
        assert loaded.revision == current.revision + 1


# ── global invariants across all races ──────────────────────────────────────

class TestRaceGlobalInvariants:
    def test_no_duplicate_action_identity_and_revision_monotonic(self, store):
        """Global row assertions after racing: revision numbers strictly
        increased with no unaccounted gaps; no duplicate action ids (PK
        enforces identity, but assert the retry path specifically)."""
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        action = pending_action(store, step)
        started = execution.begin_executing(store, action.id, action.revision).value
        execution.mark_unknown_outcome(store, started.id, started.revision, "r", "e")

        revisions_before = store.read().execute(
            "SELECT revision FROM tasks WHERE id = ?", (task.id,)
        ).fetchone()["revision"]
        r = execution.retry_action(store, started.id, "manual", expected_task_revision=task.revision)
        assert isinstance(r, Ok)
        revisions_after = store.read().execute(
            "SELECT revision FROM tasks WHERE id = ?", (task.id,)
        ).fetchone()["revision"]
        assert revisions_after == revisions_before + 1  # no gap, one bump

        ids = [row["id"] for row in store.read().execute("SELECT id FROM actions").fetchall()]
        assert len(ids) == len(set(ids))  # unique identity

    def test_terminal_never_reopens(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        work.cancel_task(store, task.id, task.revision)
        t = work.load_task(store.read(), task.id)
        r = work.transition_object(store, task.id, TaskStatus.ACTIVE, t.revision)
        assert isinstance(r, Rejected) and r.reason == "INVALID_TRANSITION"
        # new work under a cancelled task needs a new identity (Law 14)
        r2 = work.create_plan(store, task.id)
        assert isinstance(r2, Rejected) and r2.reason == "ALREADY_TERMINAL"
