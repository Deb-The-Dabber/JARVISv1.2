"""Repair (Law 29/30) — the sole sanctioned exception, tested hard.

Covers: human-authorization-only entry, revision checking, fabrication
rejection (no OBSERVED without Observation), aggregate validation, atomic
validate+commit, permanent audit, terminal-target Law 20 transfer,
ABANDONED_UNREPAIRABLE, and frozen-object semantics.
"""
from __future__ import annotations

import pytest

from tests.conftest import make_goal_task_plan_step, activated, pending_action
from v5 import execution, obligations, recovery, work
from v5.enums import ActionStatus, ObligationDisposition, TaskStatus
from v5.ids import UOR
from v5.models import Ok, Rejected
from v5 import repair as repair_mod


class TestRepairAuthorization:
    def test_authorize_requires_human(self):
        with pytest.raises(ValueError):
            repair_mod.authorize("   ")

    def test_repair_requires_auth_token_and_reason(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        auth = repair_mod.authorize("debasish")
        r = repair_mod.repair_object(store, auth, task.id, task.revision,
                                     target_state={"status": "BLOCKED"}, reason="  ")
        assert isinstance(r, Rejected) and r.reason == "REPAIR_REQUIRES_REASON"
        # no token: repair_object is not reachable from other modules at all —
        # assert the interface boundary: work/execution do not import repair.
        import v5.work, v5.execution, v5.evidence
        assert "repair" not in dir(v5.work) or not callable(getattr(v5.work, "repair", None))
        assert not hasattr(v5.execution, "repair_object")
        assert not hasattr(v5.evidence, "repair_object")


class TestRepairRevisionCheck:
    def test_stale_repair_rejected_with_current_state(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        auth = repair_mod.authorize("debasish")
        r = repair_mod.repair_object(store, auth, task.id, task.revision - 1,
                                     target_state={"status": "BLOCKED"}, reason="x")
        assert isinstance(r, Rejected) and r.reason == "STALE_REPAIR"
        assert r.current is not None
        # authoritative state unchanged
        t = work.load_task(store.read(), task.id)
        assert t.revision == task.revision and t.status == TaskStatus.ACTIVE


class TestRepairFabrication:
    def test_repair_cannot_fabricate_observed(self, store):
        """Law 29: a human's belief is typed evidence, never a manufactured
        Observation. Repairing Action.status to OBSERVED with no Observation
        row must be rejected; the honest target is UNKNOWN_OUTCOME."""
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        action = pending_action(store, step)
        started = execution.begin_executing(store, action.id, action.revision).value

        auth = repair_mod.authorize("debasish")
        r = repair_mod.repair_object(store, auth, started.id, started.revision,
                                     target_state={"status": "OBSERVED"},
                                     reason="human believes it succeeded")
        assert isinstance(r, Rejected) and r.reason == "REPAIR_FABRICATION"
        assert "UNKNOWN_OUTCOME" in r.detail
        # the honest target IS allowed
        r2 = repair_mod.repair_object(store, auth, started.id, started.revision,
                                       target_state={"status": "UNKNOWN_OUTCOME"},
                                       reason="outcome could not be confirmed")
        assert isinstance(r2, Ok)

    def test_repair_observed_with_real_observation_allowed(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        action = pending_action(store, step)
        started = execution.begin_executing(store, action.id, action.revision).value
        obs = execution.mark_observed(store, started.id, started.revision,
                                      raw_result={"ok": 1}, execution_source="file_write").value
        # simulate an integrity crash that left the action not-OBSERVED: not
        # possible via normal flow — use repair on a fresh frozen action
        a = execution.load_action(store.read(), started.id)
        auth = repair_mod.authorize("debasish")
        r = repair_mod.repair_object(store, auth, a.id, a.revision,
                                     target_state={"status": "FAILED"},
                                     reason="downgrade with real observation present")
        assert isinstance(r, Ok)  # legal: Observation exists, no fabrication


class TestRepairAggregateValidation:
    def test_active_plan_repair_requires_valid_plan(self, store):
        goal, task, plan_a, _ = make_goal_task_plan_step(store)
        task = activated(store, task, plan_a)
        plan_b = work.create_plan(store, task.id).value
        auth = repair_mod.authorize("debasish")

        # pointing active_plan_id at another task's plan: rejected (aggregate)
        other_goal = work.create_goal(store, work.CompletionPolicy(rule="ALL_REQUIRED"))
        from v5.models import CompletionPolicy, RetryBudget
        other_task = work.create_task(store, other_goal.id,
                                       CompletionPolicy(rule="ALL_REQUIRED"),
                                       RetryBudget(max_attempts=1)).value
        other_plan = work.create_plan(store, other_task.id).value
        r = repair_mod.repair_object(store, auth, task.id, task.revision,
                                     target_state={"active_plan_id": other_plan.id},
                                     reason="bad pointer")
        assert isinstance(r, Rejected) and r.reason == "AGGREGATE_INVARIANT_VIOLATION"

        # pointing at a non-DRAFT plan of this task: rejected
        r2 = repair_mod.repair_object(store, auth, task.id, task.revision,
                                      target_state={"active_plan_id": task.id},  # not a plan id
                                      reason="bad pointer")
        assert isinstance(r2, Rejected)

    def test_verification_pass_fabrication_rejected(self, store):
        """Repair cannot forge a PASS verification out of a FAIL."""
        from v5 import evidence
        from v5.enums import ClaimConfidence
        goal, task, plan, step = make_goal_task_plan_step(store)
        ev = evidence.create_inference_evidence(store, "t", {"g": 1},
                                                relevance_to=task.id).value
        claim = evidence.create_claim(store, "c", based_on=[ev.id], made_by="t",
                                      confidence=ClaimConfidence.LOW).value
        ver = evidence.create_verification(store, claim.id, "m", "deterministic").value
        evidence.run_verification(store, ver.id, lambda c, e: False)  # -> FAIL
        auth = repair_mod.authorize("debasish")
        r = repair_mod.repair_object(store, auth, ver.id, 1,
                                     target_state={"result": "PASS"}, reason="human override")
        assert isinstance(r, Rejected) and r.reason == "REPAIR_FABRICATION"


class TestRepairAuditAndAtomicity:
    def test_repair_audited_permanently(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        auth = repair_mod.authorize("debasish")
        r = repair_mod.repair_object(store, auth, task.id, task.revision,
                                     target_state={"status": "BLOCKED"}, reason="human correction")
        assert isinstance(r, Ok)
        rows = store.audit_rows(object_id=task.id, kind="repaired")
        assert len(rows) == 1
        entry = rows[0]
        assert entry["authorized_by"] == "debasish"
        assert "BY debasish" in entry["reason"] and "BECAUSE human correction" in entry["reason"]
        assert "FROM" in entry["reason"] and "TO" in entry["reason"]

    def test_frozen_object_unfrozen_by_successful_repair(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        work.freeze_object(store, task.id, "mismatch")
        t = work.load_task(store.read(), task.id)
        assert t.integrity.value == "FROZEN"
        auth = repair_mod.authorize("debasish")
        r = repair_mod.repair_object(store, auth, task.id, t.revision,
                                     target_state={"status": "ACTIVE"}, reason="resolved mismatch")
        assert isinstance(r, Ok)
        t2 = work.load_task(store.read(), task.id)
        assert t2.integrity.value == "OK"          # repaired
        assert t2.revision == t.revision + 1       # fresh revision


class TestRepairTerminalObligationTransfer:
    def test_repair_to_terminal_transfers_obligations(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        obl = obligations.create_obligation(store, "act_x", task.id, "r", "e")
        auth = repair_mod.authorize("debasish")
        r = repair_mod.repair_object(store, auth, task.id, task.revision,
                                     target_state={"status": "CANCELLED"}, reason="cancel via repair")
        assert isinstance(r, Ok)
        assert r.value["obligations_transferred"] == 1  # Law 20 applies to repair too
        loaded = obligations.load_obligation(store.read(), obl.id)
        assert loaded.owner == UOR and loaded.disposition == ObligationDisposition.OPEN


class TestAbandonUnrepairable:
    def test_abandoned_unrepairable_is_terminal_and_transfers(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        obl = obligations.create_obligation(store, "act_x", task.id, "r", "e")
        auth = repair_mod.authorize("debasish")
        r = repair_mod.abandon_unrepairable(store, auth, task.id, "cannot validate")
        assert isinstance(r, Ok)
        assert r.value["obligations_transferred"] == 1
        t = work.load_task(store.read(), task.id)
        assert t.integrity.value == "ABANDONED_UNREPAIRABLE"
        # terminal: no lifecycle mutation possible
        r2 = work.transition_object(store, task.id, TaskStatus.ACTIVE, t.revision)
        assert isinstance(r2, Rejected)
        # double-abandon rejected
        r3 = repair_mod.abandon_unrepairable(store, auth, task.id, "again")
        assert isinstance(r3, Rejected) and r3.reason == "ALREADY_ABANDONED"


class TestRecoveryNoSpecialPowers:
    def test_recovery_uses_normal_transitions_only(self, store, tmp_path):
        """Law 30: Recovery has no repair powers. A crashed EXECUTING action
        recovers to UNKNOWN_OUTCOME (a contracted transition) — recovery may
        not mark it OBSERVED or FAILED without evidence."""
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        action = pending_action(store, step)
        started = execution.begin_executing(store, action.id, action.revision).value

        store.close()
        store2 = work.Store(str(tmp_path / "state.db"))
        out = recovery.recover(store2)
        assert started.id in out["recovered_to_unknown"]
        a = execution.load_action(store2.read(), started.id)
        assert a.status == ActionStatus.UNKNOWN_OUTCOME
        # recovery did NOT fabricate an observation or a PASS
        assert execution.observation_count(store2, started.id) == 0
        # and recovery is idempotent — the second run finds nothing EXECUTING
        out2 = recovery.recover(store2)
        assert out2["recovered_to_unknown"] == []
        store2.close()
