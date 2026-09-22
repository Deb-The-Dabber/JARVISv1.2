"""Vertical slice: file_write end-to-end.

Goal → Task → Plan → Step → Action → Safety → file_write → Observation →
Evidence → Claim → Verification → Task completion, with persistent state
throughout. Proves the architecture on ONE real capability.
"""
from __future__ import annotations

import pathlib

from tests.conftest import make_goal_task_plan_step, activated
from v5 import capabilities as caps
from v5 import evidence, execution, work
from v5.enums import (
    ActionStatus,
    ClaimConfidence,
    EvidenceStatus,
    StepStatus,
    TaskStatus,
    VerificationResult,
)
from v5.models import Ok, Rejected
from v5.safety import confirm, create_confirmation, make_confirmation_gate


class TestFileWriteEndToEnd:
    def test_full_path_to_completion(self, store, tmp_path):
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        target = tmp_path / "slice.txt"

        action_r = execution.create_action(
            store, step.id, "file_write",
            {"path": str(target), "content": "hello v5"},
            caps.get("file_write").idempotency_class,
        )
        action = action_r.value

        # Safety: confirmation bound to exact identity+args (Law 32/33)
        gate = make_confirmation_gate(required=True)
        r = caps.execute_action(store, action, caps.get("file_write"),
                                safety_check=gate, plan_id_for_validation=plan.id)
        assert isinstance(r[1], Rejected) and r[1].reason == "CONFIRMATION_REQUIRED"
        assert not target.exists()

        cfm = create_confirmation(store, action.id, action.revision, "file_write",
                                  {"path": str(target), "content": "hello v5"}).value
        confirm(store, cfm)

        ok, rejection = caps.execute_action(store, action, caps.get("file_write"),
                                            safety_check=gate, plan_id_for_validation=plan.id)
        assert rejection is None, rejection
        assert ok.value["status"] == "OBSERVED"
        assert target.read_text() == "hello v5"

        # Step -> COMPLETED via contracted transitions
        s = work.load_step(store.read(), step.id)
        s = work.transition_object(store, s.id, StepStatus.READY, s.revision).value
        s = work.transition_object(store, s.id, StepStatus.EXECUTING, s.revision).value
        work.complete_step(store, s.id, s.revision)

        # Evidence from the real Observation (Law 25 — runtime only)
        obs_id = ok.value["observation_id"]
        ev = evidence.record_runtime_evidence(store, obs_id, source="file_write",
                                              relevance_to=task.id,
                                              content={"wrote": str(target)}).value
        assert ev.status == EvidenceStatus.CONFIRMED_RUNTIME

        # Claim justified by that evidence (Law 27 — HIGH needs CONFIRMED_*)
        claim = evidence.create_claim(store, f"the file {target.name} contains 'hello v5'",
                                      based_on=[ev.id], made_by="executor",
                                      confidence=ClaimConfidence.HIGH).value
        # and a claim over only INFERENCE evidence cannot be HIGH
        inf = evidence.create_inference_evidence(store, "executor", {"guess": True},
                                                relevance_to=task.id).value
        bad = evidence.create_claim(store, "unsupported claim", based_on=[inf.id],
                                    made_by="cognition", confidence=ClaimConfidence.HIGH)
        assert isinstance(bad, Rejected) and bad.reason == "CONFIDENCE_NOT_JUSTIFIED"

        # Verification: deterministic re-read of the file
        ver = evidence.create_verification(store, claim.id, "file_content_check",
                                           "deterministic").value
        result = evidence.run_verification(
            store, ver.id,
            lambda claim_row, evidence_rows: pathlib.Path(str(target)).exists()
            and pathlib.Path(str(target)).read_text() == "hello v5",
        )
        assert result.value["result"] == VerificationResult.PASS

        # Law 16 negative: a bound verification that is NOT PASS blocks completion
        # (a FAIL on a second claim, and a PENDING that never ran, both block).
        claim2 = evidence.create_claim(store, "the file was written twice",
                                       based_on=[ev.id], made_by="executor",
                                       confidence=ClaimConfidence.MEDIUM).value
        ver_fail = evidence.create_verification(store, claim2.id, "double_write_check",
                                                "deterministic").value
        work.bind_required_verification(store, task.id, ver_fail.id)  # PENDING, never run
        t_now = work.load_task(store.read(), task.id)
        cr = work.complete_object(store, t_now.id, t_now.revision)
        assert isinstance(cr, Rejected) and cr.reason == "VERIFICATION_NOT_PASS"
        evidence.run_verification(store, ver_fail.id, lambda c, e: False)  # -> FAIL
        t_now = work.load_task(store.read(), task.id)
        cr = work.complete_object(store, t_now.id, t_now.revision)
        assert isinstance(cr, Rejected) and cr.reason == "VERIFICATION_NOT_PASS"

        # a PASS verification satisfies Law 16 — but ver_fail is still bound and
        # FAILED, so task 1 can never complete under this criterion. The honest
        # terminal for task 1 is cancellation; then ALL_REQUIRED over the goal's
        # zero remaining children correctly refuses (the goal was not achieved),
        # so the goal's honest terminal is cancellation too.
        t_now = work.load_task(store.read(), task.id)
        cr_cancel = work.cancel_task(store, task.id, t_now.revision)
        assert isinstance(cr_cancel, Ok)
        g_now = work.load_goal(store.read(), goal.id)
        gr = work.complete_object(store, goal.id, g_now.revision)
        assert isinstance(gr, Rejected) and gr.reason == "COMPLETION_POLICY_UNSATISFIED"
        g_now = work.load_goal(store.read(), goal.id)
        assert isinstance(work.cancel_goal(store, goal.id, g_now.revision), Ok)

        # ── the clean positive path on a second goal ─────────────────────────
        from v5.models import CompletionPolicy, RetryBudget
        goal2 = work.create_goal(store, CompletionPolicy(rule="ALL_REQUIRED"))
        task2 = work.create_task(store, goal2.id, CompletionPolicy(rule="ALL_REQUIRED"),
                                 RetryBudget(max_attempts=2)).value
        plan2 = work.create_plan(store, task2.id).value
        step2 = work.create_step(store, plan2.id, required=True).value
        task2 = activated(store, task2, plan2)
        a2 = execution.create_action(store, step2.id, "file_write",
                                     {"path": str(tmp_path / "slice2.txt"), "content": "v5 again"},
                                     caps.get("file_write").idempotency_class).value
        cfm2 = create_confirmation(store, a2.id, a2.revision, "file_write",
                                   {"path": str(tmp_path / "slice2.txt"), "content": "v5 again"}).value
        confirm(store, cfm2)
        ok2, rej2 = caps.execute_action(store, a2, caps.get("file_write"),
                                         safety_check=make_confirmation_gate(required=True),
                                         plan_id_for_validation=plan2.id)
        assert rej2 is None
        s2 = work.load_step(store.read(), step2.id)
        s2 = work.transition_object(store, s2.id, StepStatus.READY, s2.revision).value
        s2 = work.transition_object(store, s2.id, StepStatus.EXECUTING, s2.revision).value
        work.complete_step(store, s2.id, s2.revision)
        ev2 = evidence.record_runtime_evidence(store, ok2.value["observation_id"],
                                               source="file_write", relevance_to=task2.id,
                                               content={"wrote": 1}).value
        claim3 = evidence.create_claim(store, "second file written", based_on=[ev2.id],
                                       made_by="executor", confidence=ClaimConfidence.HIGH).value
        ver3 = evidence.create_verification(store, claim3.id, "content_check2",
                                            "deterministic").value
        evidence.run_verification(store, ver3.id,
                                 lambda c, e: (tmp_path / "slice2.txt").read_text() == "v5 again")
        work.bind_required_verification(store, task2.id, ver3.id)
        t2 = work.load_task(store.read(), task2.id)
        cr2 = work.complete_object(store, t2.id, t2.revision)
        assert isinstance(cr2, Ok), cr2
        assert cr2.value["object"].status == TaskStatus.COMPLETED
        assert cr2.value["obligations_transferred"] == 0

        # goal 2 completes: its single child task is COMPLETED (ALL_REQUIRED)
        g2 = work.load_goal(store.read(), goal2.id)
        gr2 = work.complete_object(store, goal2.id, g2.revision)
        assert isinstance(gr2, Ok), gr2
        assert gr2.value["object"].status == work.GoalStatus.COMPLETED

    def test_unknown_outcome_flows_to_uor_on_completion(self, store, tmp_path):
        """An action that times out leaves an obligation; completing the task
        transfers it to UOR atomically (Law 20), and it stays discoverable."""
        from v5 import obligations
        from v5.ids import UOR

        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        target = tmp_path / "unknown.txt"

        action = execution.create_action(
            store, step.id, "file_write",
            {"path": str(target), "content": "x"},
            caps.get("file_write").idempotency_class,
        ).value
        gate = make_confirmation_gate(required=False)
        # simulate a timeout: capability raises a non-definite error
        def timeout_fn(args):
            raise TimeoutError("disk did not respond")
        real = caps.get("file_write").execute
        caps._REGISTRY["file_write"].execute = timeout_fn
        try:
            ok, rejection = caps.execute_action(store, action, caps.get("file_write"),
                                                safety_check=gate, plan_id_for_validation=plan.id)
        finally:
            caps._REGISTRY["file_write"].execute = real
        assert rejection is None
        assert ok.value["status"] == "UNKNOWN_OUTCOME"
        assert not target.exists()

        open_obls = obligations.list_open_obligations(store, owner=task.id)
        assert len(open_obls) == 1

        # complete the step + task: the obligation transfers to UOR in the SAME commit
        s = work.load_step(store.read(), step.id)
        s = work.transition_object(store, s.id, StepStatus.READY, s.revision).value
        s = work.transition_object(store, s.id, StepStatus.EXECUTING, s.revision).value
        work.complete_step(store, s.id, s.revision)
        t = work.load_task(store.read(), task.id)
        cr = work.complete_object(store, t.id, t.revision)
        assert isinstance(cr, Ok)
        assert cr.value["obligations_transferred"] == 1
        uor_open = obligations.list_open_obligations(store, owner=UOR)
        assert len(uor_open) == 1
        assert uor_open[0].origin_action_id == action.id
        # Law 17/22: still discoverable, still OPEN
        assert uor_open[0].disposition.value == "OPEN"

        # resolve it with a PASS verification later — transfer was not resolution
        obs_id = "obs_resolve_0" * 2
        with store.write() as conn:
            conn.execute(
                "INSERT INTO observations (id, action_id, captured_at, raw_result, execution_source) "
                "VALUES (?,?,?,?,?)",
                (obs_id, action.id, "2026-01-01T00:00:00Z", "{}", "manual_check"),
            )
        ev = evidence.record_runtime_evidence(store, obs_id, source="manual_check",
                                              relevance_to=task.id, content={"confirmed_absent": True}).value
        claim = evidence.create_claim(store, "the file never existed",
                                     based_on=[ev.id], made_by="human_check",
                                     confidence=ClaimConfidence.HIGH).value
        ver = evidence.create_verification(store, claim.id, "existence_check",
                                            "direct_observation").value
        evidence.run_verification(store, ver.id, lambda c, e: not target.exists())
        obl_now = obligations.load_obligation(store.read(), uor_open[0].id)
        rr = obligations.resolve_obligation(store, obl_now.id, ver.id, expected_revision=obl_now.revision)
        assert isinstance(rr, Ok)
        final = obligations.load_obligation(store.read(), obl_now.id)
        assert final.disposition.value == "RESOLVED"
        assert final.owner == UOR  # resolution does not move it back
        assert obligations.list_open_obligations(store, owner=UOR) == []
