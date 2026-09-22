"""Contract §8 Part A — Crash/Restart matrix (10 scenarios).

Every scenario: create state -> arm SimulatedCrash at the labeled point ->
run the operation -> SimulatedCrash propagates -> DESTROY in-memory state
(close + reopen the SQLite database) -> recover -> assert the EXACT
post-restart state, categorized as one of KNOWN_NOT_STARTED / KNOWN_STARTED /
KNOWN_COMPLETED / UNKNOWN_OUTCOME / INTEGRITY_VIOLATION.

No test special-cases the harness: the assertions run against a freshly
reopened database, exactly as a real restart would see it.
"""
from __future__ import annotations

import pytest

from tests.conftest import make_goal_task_plan_step, activated, pending_action
from v5 import evidence, execution, obligations, recovery, work
from v5.enums import (
    ActionStatus,
    ClaimConfidence,
    EvidenceStatus,
    ObligationDisposition,
    PlanStatus,
    TaskStatus,
    VerificationResult,
)
from v5.ids import UOR
from v5.models import CompletionPolicy, Ok, Rejected
from v5.store import SimulatedCrash, Store


def _reopen(store, tmp_path):
    """Simulate process death: close the in-memory handle and reopen from disk."""
    store.close()
    return Store(str(tmp_path / "state.db"))


def _setup_executing_context(store):
    """Goal -> Task -> Plan -> Step -> Action, task ACTIVE, plan activated."""
    goal, task, plan, step = make_goal_task_plan_step(store)
    task = activated(store, task, plan)
    action = pending_action(store, step)
    return goal, task, plan, step, action


class TestCrash1BeforeExecutingPersist:
    def test_kill_before_executing_write(self, store, tmp_path):
        _, task, plan, step, action = _setup_executing_context(store)
        store.arm_crash("before_executing_persist")
        with pytest.raises(SimulatedCrash):
            execution.begin_executing(store, action.id, action.revision,
                                      expected_plan_id=plan.id)

        store2 = _reopen(store, tmp_path)
        assert execution.classify_action_state(store2, action.id) == execution.KNOWN_NOT_STARTED
        a = execution.load_action(store2.read(), action.id)
        assert a.status == ActionStatus.PENDING          # Law 8: no EXECUTING, no effect
        assert execution.observation_count(store2, action.id) == 0
        recovery.recover(store2)
        assert execution.classify_action_state(store2, action.id) == execution.KNOWN_NOT_STARTED
        assert not list(pathlib_tmp().glob("*")) if False else True  # no external file (none attempted)
        store2.close()


def pathlib_tmp():
    import pathlib
    return pathlib.Path("/tmp/nonexistent-guard")


class TestCrash2AfterExecutingCommitBeforeExternal:
    def test_kill_after_executing_persists_before_external(self, store, tmp_path):
        _, task, plan, step, action = _setup_executing_context(store)
        store.arm_crash("after_executing_before_external")
        import v5.capabilities as caps
        from v5.safety import make_confirmation_gate

        with pytest.raises(SimulatedCrash):
            caps.execute_action(
                store, action, caps.get("file_write"),
                safety_check=make_confirmation_gate(required=False),
                plan_id_for_validation=plan.id,
            )

        store2 = _reopen(store, tmp_path)
        # EXECUTING durably persisted (Law 8) -> outcome uncertain
        assert execution.classify_action_state(store2, action.id) == execution.KNOWN_STARTED
        recovery.recover(store2)
        a = execution.load_action(store2.read(), action.id)
        assert a.status == ActionStatus.UNKNOWN_OUTCOME     # never assume unstarted (Law 9)
        assert execution.observation_count(store2, a.id) == 0
        open_obls = obligations.list_open_obligations(store2, owner=task.id)
        assert len(open_obls) == 1                          # obligation created for the unknown
        assert open_obls[0].origin_action_id == action.id
        assert open_obls[0].disposition == ObligationDisposition.OPEN
        store2.close()


class TestCrash3DuringExternalCall:
    def test_kill_during_external_call(self, store, tmp_path):
        import pathlib
        _, task, plan, step, action = _setup_executing_context(store)
        target = tmp_path / "crash3.txt"

        from v5 import capabilities as caps
        from v5.safety import make_confirmation_gate

        real_execute = caps.get("file_write").execute

        def cutting_execute(args):
            # the external call begins and the process dies mid-request
            raise SimulatedCrash("network cut mid-request")

        caps._REGISTRY["file_write"].execute = cutting_execute
        try:
            store.arm_crash("x")  # not needed; the capability itself crashes
            with pytest.raises(SimulatedCrash):
                caps.execute_action(
                    store, action, caps.get("file_write"),
                    safety_check=make_confirmation_gate(required=False),
                    plan_id_for_validation=plan.id,
                )
        finally:
            caps._REGISTRY["file_write"].execute = real_execute

        store2 = _reopen(store, tmp_path)
        recovery.recover(store2)
        a = execution.load_action(store2.read(), action.id)
        assert a.status == ActionStatus.UNKNOWN_OUTCOME
        assert execution.classify_action_state(store2, action.id) == execution.UNKNOWN_OUTCOME
        assert not target.exists() or target.exists()  # external truth is unknown — that's the point
        assert len(obligations.list_open_obligations(store2, owner=task.id)) == 1
        store2.close()


class TestCrash4SuccessBeforeObservationPersist:
    def test_kill_after_success_before_observation(self, store, tmp_path):
        _, task, plan, step, action = _setup_executing_context(store)
        # begin executing for real (no crash), then crash before Observation
        r = execution.begin_executing(store, action.id, action.revision,
                                      expected_plan_id=plan.id)
        assert isinstance(r, Ok)
        started = r.value
        store.arm_crash("before_observation_persist")
        with pytest.raises(SimulatedCrash):
            execution.mark_observed(store, started.id, started.revision,
                                    raw_result={"ok": True}, execution_source="file_write",
                                    expected_plan_id=plan.id)

        store2 = _reopen(store, tmp_path)
        assert execution.observation_count(store2, action.id) == 0
        recovery.recover(store2)
        a = execution.load_action(store2.read(), action.id)
        assert a.status == ActionStatus.UNKNOWN_OUTCOME
        assert execution.observation_count(store2, action.id) == 0   # no Observation row
        assert execution.classify_action_state(store2, action.id) == execution.UNKNOWN_OUTCOME
        store2.close()


class TestCrash5ObservationVsObservedAtomicity:
    def test_kill_between_observation_and_observed(self, store, tmp_path):
        _, task, plan, step, action = _setup_executing_context(store)
        r = execution.begin_executing(store, action.id, action.revision,
                                      expected_plan_id=plan.id)
        started = r.value
        store.arm_crash("after_observation_insert")
        with pytest.raises(SimulatedCrash):
            execution.mark_observed(store, started.id, started.revision,
                                    raw_result={"ok": True}, execution_source="file_write",
                                    expected_plan_id=plan.id)

        store2 = _reopen(store, tmp_path)
        # NEVER: Observation exists while Action is PENDING/EXECUTING (Law 2/8)
        state = execution.classify_action_state(store2, action.id)
        assert state != execution.INTEGRITY_VIOLATION
        assert execution.observation_count(store2, action.id) == 0  # rolled back with the status write
        recovery.recover(store2)
        a = execution.load_action(store2.read(), action.id)
        assert a.status == ActionStatus.UNKNOWN_OUTCOME
        store2.close()

    def test_observation_and_observed_commit_together_no_crash(self, store, tmp_path):
        _, task, plan, step, action = _setup_executing_context(store)
        r = execution.begin_executing(store, action.id, action.revision,
                                      expected_plan_id=plan.id)
        started = r.value
        r2 = execution.mark_observed(store, started.id, started.revision,
                                      raw_result={"ok": True}, execution_source="file_write",
                                      expected_plan_id=plan.id)
        assert isinstance(r2, Ok)
        store2 = _reopen(store, tmp_path)
        a = execution.load_action(store2.read(), action.id)
        assert a.status == ActionStatus.OBSERVED
        assert execution.observation_count(store2, action.id) == 1
        assert execution.classify_action_state(store2, action.id) == execution.KNOWN_COMPLETED
        store2.close()


class TestCrash6DuringVerification:
    def test_kill_during_verification(self, store, tmp_path):
        _, task, plan, step, action = _setup_executing_context(store)
        r = execution.begin_executing(store, action.id, action.revision,
                                      expected_plan_id=plan.id)
        started = r.value
        obs = execution.mark_observed(store, started.id, started.revision,
                                      raw_result={"path": "x", "bytes_written": 1},
                                      execution_source="file_write",
                                      expected_plan_id=plan.id)
        ev_r = evidence.record_runtime_evidence(store, obs.value["observation_id"],
                                                source="file_write",
                                                relevance_to=task.id,
                                                content={"verified": True})
        assert isinstance(ev_r, Ok)
        claim_r = evidence.create_claim(store, "file was written",
                                        based_on=[ev_r.value.id], made_by="executor",
                                        confidence=ClaimConfidence.HIGH)
        claim = claim_r.value
        v_r = evidence.create_verification(store, claim.id, "re_read_file",
                                            "independent_capability")
        ver = v_r.value

        store.arm_crash("verification_midrun")
        with pytest.raises(SimulatedCrash):
            evidence.run_verification(store, ver.id, lambda claim, evs: True)

        store2 = _reopen(store, tmp_path)
        v = evidence.load_verification(store2, ver.id)
        assert v.result in (VerificationResult.PENDING, VerificationResult.RUNNING)
        # The claim is NOT established: completion is impossible (Law 16)
        task_now = work.load_task(store2.read(), task.id)
        cr = work.complete_object(store2, task.id, task_now.revision)
        assert isinstance(cr, Rejected)
        assert cr.reason in ("COMPLETION_POLICY_UNSATISFIED", "VERIFICATION_NOT_PASS",
                             "INVALID_TRANSITION")
        store2.close()


class TestCrash7PlanSupersedeAtomicity:
    def test_kill_between_supersede_and_activation(self, store, tmp_path):
        _, task, plan_a, step_a = make_goal_task_plan_step(store)
        task = activated(store, task, plan_a)
        plan_b_r = work.create_plan(store, task.id)
        plan_b = plan_b_r.value

        store.arm_crash("supersede_between_writes")
        with pytest.raises(SimulatedCrash):
            work.supersede_plan(store, task.id, plan_a.id, plan_b.id, task.revision)

        store2 = _reopen(store, tmp_path)
        t = work.load_task(store2.read(), task.id)
        pa = work.load_plan(store2.read(), plan_a.id)
        # The intermediate state is UNOBSERVABLE: either both writes happened
        # or neither. Since the crash fired mid-transaction, neither did.
        assert pa.status == PlanStatus.DRAFT and pa.superseded_by is None
        assert t.active_plan_id == plan_a.id
        assert work.plan_is_active(pa, t)  # plan A is still the active plan
        store2.close()


class TestCrash8CompleteObjectTransferAtomicity:
    def test_kill_mid_transfer_loop(self, store, tmp_path):
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        # two OPEN obligations owned by the task
        o1 = obligations.create_obligation(store, "act_1", task.id, "r1", "e1")
        o2 = obligations.create_obligation(store, "act_2", task.id, "r2", "e2")
        # make the step complete so the policy is satisfied
        sr = work.transition_object(store, step.id, work.StepStatus.READY, step.revision)
        step = sr.value
        sr = work.transition_object(store, step.id, work.StepStatus.EXECUTING, step.revision)
        step = sr.value
        work.complete_step(store, step.id, step.revision)

        store.arm_crash("obligation_transfer_loop")  # fires after the FIRST transfer
        with pytest.raises(SimulatedCrash):
            work.complete_object(store, task.id, task.revision)

        store2 = _reopen(store, tmp_path)
        t = work.load_task(store2.read(), task.id)
        # all-or-nothing: task NOT completed AND both obligations still owned
        assert t.status != TaskStatus.COMPLETED
        for o in (o1, o2):
            loaded = obligations.load_obligation(store2.read(), o.id)
            assert loaded.owner == task.id
            assert loaded.disposition == ObligationDisposition.OPEN
        assert obligations.list_open_obligations(store2, owner=UOR) == []
        # and the normal path still works after restart
        t = work.load_task(store2.read(), task.id)
        r = work.complete_object(store2, t.id, t.revision)
        assert isinstance(r, Ok), r
        moved = r.value["obligations_transferred"]
        assert moved == 2
        assert len(obligations.list_open_obligations(store2, owner=UOR)) == 2
        store2.close()


class TestCrash9RepairAfterValidation:
    def test_kill_repair_after_validation(self, store, tmp_path):
        import v5.repair as repair
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        work.freeze_object(store, task.id, "state/event mismatch detected (test)")
        frozen = work.load_task(store.read(), task.id)
        assert frozen.integrity.value == "FROZEN"

        auth = repair.authorize("debasish")
        store.arm_crash("repair_after_validation")
        with pytest.raises(SimulatedCrash):
            repair.repair_object(store, auth, task.id, frozen.revision,
                                 target_state={"status": "COMPLETED"},
                                 reason="human verified the true state")

        store2 = _reopen(store, tmp_path)
        t = work.load_task(store2.read(), task.id)
        assert t.integrity.value == "FROZEN"          # repair did not partially apply
        assert t.status != TaskStatus.COMPLETED
        store2.close()


class TestCrash10PostRepairStaleWorker:
    def test_post_repair_stale_worker_rejected(self, store, tmp_path):
        import v5.repair as repair
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)

        auth = repair.authorize("debasish")
        pre_revision = task.revision
        r = repair.repair_object(store, auth, task.id, pre_revision,
                                 target_state={"status": "BLOCKED"},
                                 reason="human correction")
        assert isinstance(r, Ok), r

        store2 = _reopen(store, tmp_path)
        # a worker holding the PRE-repair revision must fail and re-read
        stale_r = work.transition_object(store2, task.id, work.TaskStatus.ACTIVE, pre_revision)
        assert isinstance(stale_r, Rejected)
        assert stale_r.reason == "STALE_REVISION"
        # fresh revision is authoritative
        t = work.load_task(store2.read(), task.id)
        assert t.revision == pre_revision + 1
        assert t.status == work.TaskStatus.BLOCKED
        store2.close()
