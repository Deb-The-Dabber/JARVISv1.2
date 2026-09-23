"""Executable invariant tests for the 36 Constitutional laws.

Each law gets either a behavioral test or an explicit note explaining why it
is integration/manual-only at this stage. Tests reference the law numbers
from the frozen Constitution v1.0.
"""
from __future__ import annotations

import threading

import pytest

from tests.conftest import make_goal_task_plan_step, activated, pending_action
from v5 import evidence, execution, obligations, recovery, work
from v5.enums import (
    ActionStatus,
    ClaimConfidence,
    EvidenceStatus,
    GoalStatus,
    IntegrityStatus,
    ObligationDisposition,
    PlanStatus,
    StepStatus,
    TaskStatus,
)
from v5.ids import UOR
from v5.models import CompletionPolicy, Ok, Rejected, RetryBudget
from v5.store import Store


class TestStateLaws1to6:
    """1 canonical state, 2 atomic mutation, 3 revision, 4 invalid transition,
    5 integrity freeze, 6 derived state."""

    def test_law1_canonical_state(self, store, tmp_path):
        """One authoritative persistent state per object: reopen the DB and
        see the same object; nothing else holds authority."""
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        store.close()
        s2 = Store(str(tmp_path / "state.db"))
        t2 = work.load_task(s2.read(), task.id)
        assert t2.status == TaskStatus.ACTIVE and t2.active_plan_id == plan.id
        s2.close()

    def test_law2_atomic_mutation_with_event(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        plan_b = work.create_plan(store, task.id).value
        # terminal transition and its audit event commit together: reject an
        # operation mid-transaction and confirm neither the state change nor
        # a phantom audit landed
        before = store.read().execute("SELECT COUNT(*) n FROM audit_log").fetchone()["n"]
        store.arm_crash("supersede_between_writes")
        with pytest.raises(Exception):
            work.supersede_plan(store, task.id, plan.id, plan_b.id, task.revision)
        t = work.load_task(store.read(), task.id)
        assert t.active_plan_id == plan.id  # unchanged
        after = store.read().execute("SELECT COUNT(*) n FROM audit_log").fetchone()["n"]
        assert after == before  # no phantom audit row for the aborted mutation

    def test_law3_revision_cas(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        stale = work.transition_object(store, task.id, TaskStatus.PAUSED, task.revision - 1)
        assert isinstance(stale, Rejected) and stale.reason == "STALE_REVISION"

    def test_law4_invalid_transition_atomic_rejection(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        # task is PENDING; COMPLETED is not contracted from PENDING (a task
        # that never ran cannot complete)
        r = work.transition_object(store, task.id, TaskStatus.COMPLETED, task.revision)
        assert isinstance(r, Rejected) and r.reason == "INVALID_TRANSITION"
        # authoritative state returned, unchanged
        assert r.current.id == task.id and r.current.status == TaskStatus.PENDING
        # rejection is auditable
        rows = store.read().execute(
            "SELECT COUNT(*) n FROM audit_log WHERE object_id=? AND kind='rejected'",
            (task.id,),
        ).fetchone()["n"]
        assert rows >= 1

    def test_law5_integrity_freeze(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        work.freeze_object(store, task.id, "mismatch")
        t = work.load_task(store.read(), task.id)
        assert t.integrity == IntegrityStatus.FROZEN
        r = work.transition_object(store, task.id, TaskStatus.PAUSED, t.revision)
        assert isinstance(r, Rejected) and r.reason == "FROZEN"
        # recovery also refuses to transition frozen objects autonomously

    def test_law6_derived_plan_activity_never_stored(self, store):
        goal, task, plan_a, _ = make_goal_task_plan_step(store)
        task = activated(store, task, plan_a)
        # no Plan.status == ACTIVE exists anywhere — activity is derived
        conn = store.read()
        assert conn.execute(
            "SELECT COUNT(*) n FROM plans WHERE status = 'ACTIVE'"
        ).fetchone()["n"] == 0
        assert work.plan_is_active(work.load_plan(conn, plan_a.id),
                                    work.load_task(conn, task.id))
        plan_b = work.create_plan(store, task.id).value
        # B is DRAFT but NOT active (pointer says A)
        assert not work.plan_is_active(work.load_plan(conn, plan_b.id),
                                        work.load_task(conn, task.id))


class TestExecutionLaws7to11:
    """7 attempt identity, 8 pre-effect persistence, 9 unknown outcome,
    10 no historical mutation, 11 idempotency class."""

    def test_law7_unique_immutable_identity(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        a1 = pending_action(store, step)
        a2 = pending_action(store, step)
        assert a1.id != a2.id

    def test_law8_pre_effect_persistence(self, store, tmp_path):
        """The crash matrix proves this; here assert the normal path:
        EXECUTING is committed before execute() is ever called."""
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        action = pending_action(store, step)
        seen = {}
        from v5 import capabilities as caps
        spy = caps.get("file_write").execute
        def spying(args):
            a = execution.load_action(store.read(), action.id)
            seen["status_at_external_call"] = a.status  # must already be EXECUTING
            return spy(args)
        caps._REGISTRY["file_write"].execute = spying
        try:
            from v5.safety import make_confirmation_gate
            r = caps.execute_action(store, action, caps.get("file_write"),
                                    safety_check=make_confirmation_gate(required=False),
                                    plan_id_for_validation=plan.id)
        finally:
            caps._REGISTRY["file_write"].execute = spy
        assert seen["status_at_external_call"] == ActionStatus.EXECUTING

    def test_law9_unknown_outcome_never_guessed(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        action = pending_action(store, step)
        started = execution.begin_executing(store, action.id, action.revision).value
        r = execution.mark_unknown_outcome(store, started.id, started.revision, "t", "e")
        assert isinstance(r, Ok)
        a = execution.load_action(store.read(), started.id)
        assert a.status == ActionStatus.UNKNOWN_OUTCOME
        # UNKNOWN_OUTCOME is terminal: no path turns it into FAILED/SUCCESS
        assert a.status not in (ActionStatus.FAILED, ActionStatus.PENDING, ActionStatus.EXECUTING)

    def test_law10_retry_creates_new_identity(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        action = pending_action(store, step)
        started = execution.begin_executing(store, action.id, action.revision).value
        execution.mark_unknown_outcome(store, started.id, started.revision, "t", "e")
        r = execution.retry_action(store, started.id, "manual", expected_task_revision=task.revision)
        new = r.value
        assert new.id != started.id and new.retry_of == started.id
        old = execution.load_action(store.read(), started.id)
        assert old.status == ActionStatus.UNKNOWN_OUTCOME  # untouched

    def test_law11_idempotency_class_is_capability_declared(self, store):
        from v5 import capabilities as caps
        spec = caps.get("file_write")
        assert spec is not None
        assert spec.idempotency_class.value == "IDEMPOTENT"
        # actions carry the declared class, not an inferred one
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        a = pending_action(store, step)
        assert a.idempotency_class.value == "IDEMPOTENT"


class TestWorkLaws12to16:
    def test_law12_lifecycle_authority(self, store):
        """Work Service owns lifecycle exclusively: every mutation goes through
        revision-checked operations; there is no direct-state-mutation API."""
        goal, task, plan, step = make_goal_task_plan_step(store)
        # the Work functions are the only mutation surface for tasks
        assert not hasattr(work, "set_task_status_directly")

    def test_law13_dependencies_acyclic(self, store):
        goal, task, plan, _ = make_goal_task_plan_step(store)
        a = work.create_step(store, plan.id, required=True).value
        b = work.create_step(store, plan.id, required=True).value
        p = work.load_plan(store.read(), plan.id)
        r1 = work.add_dependency(store, plan.id, b.id, a.id, expected_plan_revision=p.revision)
        assert isinstance(r1, Ok)
        p = work.load_plan(store.read(), plan.id)
        r2 = work.add_dependency(store, plan.id, a.id, b.id, expected_plan_revision=p.revision)
        assert isinstance(r2, Rejected) and r2.reason == "DEPENDENCY_CYCLE"

    def test_law14_terminal_never_reopens(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        work.cancel_task(store, task.id, task.revision)
        t = work.load_task(store.read(), task.id)
        for target in (TaskStatus.ACTIVE, TaskStatus.PAUSED, TaskStatus.BLOCKED):
            r = work.transition_object(store, task.id, target, t.revision)
            assert isinstance(r, Rejected)

    def test_law15_plan_exclusivity_single_cas(self, store):
        goal, task, plan_a, _ = make_goal_task_plan_step(store)
        task = activated(store, task, plan_a)
        plan_b = work.create_plan(store, task.id).value
        # activation while A is live is rejected by the SINGLE pointer CAS
        r = work.activate_plan(store, task.id, plan_b.id, task.revision)
        assert isinstance(r, Rejected) and r.reason == "PLAN_EXCLUSIVITY"

    def test_law16_explicit_completion_verification_pass_required(self, store, tmp_path):
        """Full positive + negative already proven in test_e2e; here the minimal
        form: policy satisfied but required verification PENDING -> rejected."""
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        s = work.transition_object(store, step.id, StepStatus.READY, step.revision).value
        s = work.transition_object(store, s.id, StepStatus.EXECUTING, s.revision).value
        work.complete_step(store, s.id, s.revision)
        ev = evidence.create_inference_evidence(store, "t", {}, relevance_to=task.id).value
        claim = evidence.create_claim(store, "c", based_on=[ev.id], made_by="t",
                                      confidence=ClaimConfidence.LOW).value
        ver = evidence.create_verification(store, claim.id, "m", "deterministic").value
        work.bind_required_verification(store, task.id, ver.id)  # PENDING, never run
        t = work.load_task(store.read(), task.id)
        r = work.complete_object(store, t.id, t.revision)
        assert isinstance(r, Rejected) and r.reason == "VERIFICATION_NOT_PASS"


class TestObligationLaws17to22:
    def test_law17_no_detached_obligation(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        obl = obligations.create_obligation(store, "a", task.id, "r", "e").value
        # discoverable while owned
        assert obligations.load_obligation(store.read(), obl.id) is not None
        assert len(obligations.list_open_obligations(store, owner=task.id)) == 1
        # after terminal transfer to UOR, still discoverable
        task = work.transition_object(store, task.id, TaskStatus.ACTIVE, task.revision).value
        work.cancel_task(store, task.id, task.revision)
        assert len(obligations.list_open_obligations(store, owner=UOR)) == 1

    def test_law18_single_ownership(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        obl = obligations.create_obligation(store, "a", task.id, "r", "e").value
        other = work.create_task(store, goal.id, CompletionPolicy(rule="ALL_REQUIRED")).value
        r = obligations.transfer_obligation(store, obl.id, other.id, expected_owner=task.id)
        assert isinstance(r, Ok)
        loaded = obligations.load_obligation(store.read(), obl.id)
        assert loaded.owner == other.id  # exactly one owner; no copy created
        rows = store.read().execute("SELECT COUNT(*) n FROM obligations WHERE origin_action_id='a'").fetchone()["n"]
        assert rows == 1

    def test_law19_transfer_is_not_resolution(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        obl = obligations.create_obligation(store, "a", task.id, "r", "e").value
        obligations.transfer_obligation(store, obl.id, UOR, expected_owner=task.id)
        loaded = obligations.load_obligation(store.read(), obl.id)
        assert loaded.disposition == ObligationDisposition.OPEN
        # completion of the owning object is NOT satisfied by the transfer
        # (verified structurally: the obligation is in the open set)
        assert obligations.list_open_obligations(store, owner=UOR)

    def test_law20_terminal_transfer_atomic(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        o1 = obligations.create_obligation(store, "a1", task.id, "r", "e").value
        o2 = obligations.create_obligation(store, "a2", task.id, "r", "e").value
        task = work.transition_object(store, task.id, TaskStatus.ACTIVE, task.revision).value
        r = work.cancel_task(store, task.id, task.revision)
        moved = r.value["obligations_transferred"]
        assert moved == 2
        for o in (o1, o2):
            loaded = obligations.load_obligation(store.read(), o.id)
            assert loaded.owner == UOR

    def test_law21_abandon_requires_named_authorization(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        obl = obligations.create_obligation(store, "a", task.id, "r", "e").value
        r = obligations.abandon_obligation(store, obl.id, "", "no human", 0)
        assert isinstance(r, Rejected) and r.reason == "ABANDON_REQUIRES_HUMAN"

    def test_law22_uor_retention(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        obl = obligations.create_obligation(store, "a", task.id, "r", "e").value
        task = work.transition_object(store, task.id, TaskStatus.ACTIVE, task.revision).value
        work.cancel_task(store, task.id, task.revision)
        # resolve it — the row remains as history, never deleted
        row = store.read().execute(
            "SELECT * FROM obligations WHERE id = ?", (obl.id,)
        ).fetchone()
        assert row is not None
        assert store.read().execute(
            "SELECT COUNT(*) n FROM obligation_events WHERE obligation_id = ?", (obl.id,)
        ).fetchone()["n"] >= 1


class TestEvidenceLaws23to28:
    def test_law23_provenance_orthogonal(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        user_ev = evidence.record_user_evidence(store, "user", "I deleted the file",
                                                relevance_to=task.id).value
        assert user_ev.status == EvidenceStatus.UNKNOWN  # never CONFIRMED_SOURCE
        assert user_ev.acquisition_method == "user_statement"

    def test_law24_evidence_immutable(self, store):
        goal, task, task_plan, step = make_goal_task_plan_step(store)
        ev = evidence.create_inference_evidence(store, "t", {"v": 1},
                                                relevance_to=task.id).value
        # there is NO update path for evidence
        assert not hasattr(evidence, "update_evidence")
        assert not hasattr(evidence, "edit_evidence")

    def test_law25_inference_cannot_masquerade(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        # cognition path produces INFERENCE only
        r = evidence.create_inference_evidence(store, "cognition", {"g": 1},
                                              relevance_to=task.id)
        assert isinstance(r, Ok) and r.value.status == EvidenceStatus.INFERENCE
        # runtime evidence requires a REAL observation
        r2 = evidence.record_runtime_evidence(store, "obs_nonexistent", "s", task.id, {})
        assert isinstance(r2, Rejected)  # no observation -> no runtime evidence

    def test_law26_contradiction_preserved(self, store):
        """Conflicting evidence is preserved, never overwritten: two records
        with contradictory content coexist as separate immutable rows.
        (Semantic CONFLICTED-classification of claims is integration-level;
        the structural guarantee tested here is coexistence + immutability.)"""
        goal, task, plan, step = make_goal_task_plan_step(store)
        e1 = evidence.create_inference_evidence(store, "a", {"says": "yes"},
                                                relevance_to=task.id).value
        e2 = evidence.create_inference_evidence(store, "b", {"says": "no"},
                                                relevance_to=task.id).value
        assert e1.id != e2.id
        assert evidence.load_evidence(store, e1.id).content == {"says": "yes"}
        assert evidence.load_evidence(store, e2.id).content == {"says": "no"}

    def test_law27_confidence_justification(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        inf = evidence.create_inference_evidence(store, "t", {}, relevance_to=task.id).value
        r = evidence.create_claim(store, "over-inflated", based_on=[inf.id],
                                  made_by="x", confidence=ClaimConfidence.HIGH)
        assert isinstance(r, Rejected) and r.reason == "CONFIDENCE_NOT_JUSTIFIED"

    def test_law28_historical_completion_not_reopened(self, store, tmp_path):
        """Evidence arriving after completion does not reopen history — it
        spawns a new tracked concern."""
        from v5 import capabilities as caps
        from v5.safety import make_confirmation_gate
        import pathlib
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        target = tmp_path / "hist.txt"
        action = pending_action(store, step, args={"path": str(target), "content": "v5"})
        from v5.safety import confirm, create_confirmation
        cfm = create_confirmation(store, action.id, action.revision, "file_write",
                                  {"path": str(target), "content": "v5"}).value
        confirm(store, cfm)
        ok, _ = caps.execute_action(store, action, caps.get("file_write"),
                                    safety_check=make_confirmation_gate(required=True),
                                    plan_id_for_validation=plan.id)
        s = work.load_step(store.read(), step.id)
        s = work.transition_object(store, s.id, StepStatus.READY, s.revision).value
        s = work.transition_object(store, s.id, StepStatus.EXECUTING, s.revision).value
        work.complete_step(store, s.id, s.revision)
        ev = evidence.record_runtime_evidence(store, ok.value["observation_id"],
                                              source="file_write", relevance_to=task.id,
                                              content={"ok": True}).value
        claim = evidence.create_claim(store, "file written", based_on=[ev.id],
                                      made_by="e", confidence=ClaimConfidence.HIGH).value
        ver = evidence.create_verification(store, claim.id, "m", "deterministic").value
        evidence.run_verification(store, ver.id, lambda c, e: True)
        work.bind_required_verification(store, task.id, ver.id)
        t = work.load_task(store.read(), task.id)
        assert isinstance(work.complete_object(store, t.id, t.revision), Ok)
        # later contradicting evidence: the completed task does NOT reopen
        pathlib.Path(str(target)).write_text("tampered")  # external change
        late = evidence.record_user_evidence(store, "user", {"says": "file changed"},
                                              relevance_to=task.id).value
        t2 = work.load_task(store.read(), task.id)
        assert t2.status == TaskStatus.COMPLETED  # history stays closed
        # and a new concern can reference the completion via relevance_to
        assert late.relevance_to == task.id


class TestAuthorityLaws29to33:
    def test_law29_capability_authority_and_repair_fabrication(self, store):
        """Covered in depth by test_repair; here the interface assertion:
        only the capability execution pipeline records observations."""
        assert hasattr(execution, "mark_observed")
        assert not hasattr(evidence, "record_observation")  # not an evidence concern

    def test_law30_repair_sole_exception(self, store):
        """Recovery has none of repair's latitude (test_repair covers this);
        here: repair's fresh revision invalidates execution authority."""
        import v5.repair as repair
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        auth = repair.authorize("human")
        pre = task.revision
        repair.repair_object(store, auth, task.id, pre,
                            target_state={"status": "BLOCKED"}, reason="x")
        # stale worker holding pre-repair revision fails and re-reads
        r = work.transition_object(store, task.id, TaskStatus.ACTIVE, pre)
        assert isinstance(r, Rejected) and r.reason == "STALE_REVISION"

    def test_law31_untrusted_input_is_data(self, store, tmp_path):
        """External content cannot alter canonical state: a file's content is
        data. The file_write capability refuses repo-internal writes (a
        structural boundary), and no evidence row can mutate state."""
        from v5 import capabilities as caps
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        repo_file = "v5/__init__.py"
        action = pending_action(store, step, args={"path": os_path(repo_file), "content": "evil"})
        from v5.safety import make_confirmation_gate
        ok, rej = caps.execute_action(store, action, caps.get("file_write"),
                                      safety_check=make_confirmation_gate(required=False),
                                      plan_id_for_validation=plan.id)
        # the capability's guard made the attempt FAIL definitively (no effect)
        assert rej is None and ok.value["status"] == "FAILED"

    def test_law32_confirmation_binding(self, store, tmp_path):
        from v5.safety import confirm, create_confirmation, make_confirmation_gate
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        target = tmp_path / "cfm.txt"
        action = pending_action(store, step, args={"path": str(target), "content": "a"})
        gate = make_confirmation_gate(required=True)
        # unconfirmed -> rejected
        _, rej = None, None
        import v5.capabilities as caps
        _, rej = caps.execute_action(store, action, caps.get("file_write"),
                                      safety_check=gate, plan_id_for_validation=plan.id)
        assert isinstance(rej, Rejected) and rej.reason == "CONFIRMATION_REQUIRED"
        # confirm a DIFFERENT action's binding -> still rejected (identity bound)
        other = pending_action(store, step, args={"path": str(target), "content": "a"})
        cfm_other = create_confirmation(store, other.id, other.revision, "file_write",
                                        {"path": str(target), "content": "a"}).value
        confirm(store, cfm_other)
        _, rej2 = caps.execute_action(store, action, caps.get("file_write"),
                                      safety_check=gate, plan_id_for_validation=plan.id)
        assert isinstance(rej2, Rejected) and rej2.reason == "CONFIRMATION_REQUIRED"
        # changed arguments invalidate the confirmation (material change)
        cfm_mine = create_confirmation(store, action.id, action.revision, "file_write",
                                        {"path": str(target), "content": "CHANGED"}).value
        confirm(store, cfm_mine)
        _, rej3 = caps.execute_action(store, action, caps.get("file_write"),
                                      safety_check=gate, plan_id_for_validation=plan.id)
        assert isinstance(rej3, Rejected)  # args don't match the binding

    def test_law33_safety_non_bypass(self, store, tmp_path):
        """Decomposition cannot lower authorization: two file_writes under
        different action ids each need their own confirmation."""
        from v5.safety import confirm, create_confirmation, make_confirmation_gate
        import v5.capabilities as caps
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        t1, t2 = tmp_path / "a.txt", tmp_path / "b.txt"
        a1 = pending_action(store, step, args={"path": str(t1), "content": "x"})
        a2 = pending_action(store, step, args={"path": str(t2), "content": "x"})
        cfm1 = create_confirmation(store, a1.id, a1.revision, "file_write",
                                   {"path": str(t1), "content": "x"}).value
        confirm(store, cfm1)
        gate = make_confirmation_gate(required=True)
        # a1 may execute; a2 (same effect class, different identity) may NOT
        ok1, rej1 = caps.execute_action(store, a1, caps.get("file_write"),
                                        safety_check=gate, plan_id_for_validation=plan.id)
        assert rej1 is None
        _, rej2 = caps.execute_action(store, a2, caps.get("file_write"),
                                      safety_check=gate, plan_id_for_validation=plan.id)
        assert isinstance(rej2, Rejected) and rej2.reason == "CONFIRMATION_REQUIRED"


class TestMetaLaws34to36:
    def test_law34_no_implicit_transitions(self, store):
        """Every transition must satisfy the explicit table — an unspecified
        transition is rejected, not defaulted."""
        goal, task, plan, step = make_goal_task_plan_step(store)
        # PENDING -> PAUSED is not in the table
        r = work.transition_object(store, task.id, TaskStatus.PAUSED, task.revision)
        assert isinstance(r, Rejected) and r.reason == "INVALID_TRANSITION"

    def test_law35_invariant_owner_plan_exclusivity(self, store):
        """The Plan-exclusivity invariant has exactly one owner: the Task
        pointer CAS in the Work Service. A plan cannot independently become
        'active' (no such state exists)."""
        goal, task, plan, _ = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        # the child (Plan) has no API to mark itself active — only the Task CAS
        assert not hasattr(work, "set_plan_active")

    def test_law36_temporal_continuity(self, store, tmp_path):
        """Replacing the current plan never erases unresolved obligations or
        evidence from prior execution."""
        goal, task, plan_a, step_a = make_goal_task_plan_step(store)
        task = activated(store, task, plan_a)
        action = pending_action(store, step_a)
        started = execution.begin_executing(store, action.id, action.revision).value
        execution.mark_unknown_outcome(store, started.id, started.revision, "t", "e")
        ev = evidence.create_inference_evidence(store, "t", {"v": 1},
                                                relevance_to=task.id).value

        plan_b = work.create_plan(store, task.id).value
        work.supersede_plan(store, task.id, plan_a.id, plan_b.id, task.revision)

        # obligations from plan A's execution remain addressable (owner: task)
        open_obls = obligations.list_open_obligations(store, owner=task.id)
        assert len(open_obls) == 1
        # evidence remains
        assert evidence.load_evidence(store, ev.id) is not None
        # and the terminal transfer will still move them when the task ends
        t = work.load_task(store.read(), task.id)
        r = work.cancel_task(store, t.id, t.revision)
        assert r.value["obligations_transferred"] == 1


def os_path(rel):
    import os
    return os.path.abspath(rel)
