"""Live Cognition Loop v2 — two-capability / two-step-Plan adversarial tests.

The milestone-proving test is the file_read verification-independence sabotage:
a read capability that lies about content it claimed to have read must FAIL
the independent verifier, because the verifier reads the live filesystem
itself — the Observation cannot self-certify.

Scripted providers here drive the membrane exactly like the offline tests in
test_live_loop.py; the real-model run is separate (artifacts/live_slice_v2...).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from v5 import cognition, evidence, execution, live_loop, sessions, verification, work
from v5 import capabilities as caps
from v5.enums import ActionStatus, StepStatus, TaskStatus, VerificationResult
from v5.models import CompletionPolicy, Ok, Rejected
from v5.safety import make_confirmation_gate
from tests.test_live_loop import (FakeCandidate, FakeContent, FakeFunctionCall,
                                  FakePart, FakeResponse, ScriptedProvider)


@pytest.fixture
def store(tmp_path):
    from v5.store import Store
    s = Store(str(tmp_path / "state.db"))
    yield s
    s.close()


@pytest.fixture
def session(store):
    return sessions.create_session(store, cognition_authorized=True)


def _p():
    return CompletionPolicy(rule="ALL_REQUIRED")


def _chain_two_step(store, session, path, content):
    """Propose goal + two-step plan + task activation (identical to the
    milestone's host-side scaffolding, but constructed without the model)."""
    cognition.bind_store(store)
    cognition.authenticate_session(session.id)
    g = cognition.propose_goal(session.id, "write then read", _p()).value
    t = cognition.propose_task(g.id, g.revision, "t", _p()).value
    t = work.transition_object(store, t.id, TaskStatus.ACTIVE, t.revision).value
    plan_res = cognition.propose_plan(t.id, t.revision, [
        cognition.StepProposal("write it", True, [], "file_write",
                               [cognition.VerificationRequirement(
                                   "verify_file_write", "file_write")]),
        cognition.StepProposal("read it back", True, [0], "file_read",
                               [cognition.VerificationRequirement(
                                   "verify_file_read", "file_read")]),
    ])
    assert isinstance(plan_res, Ok), plan_res
    plan = plan_res.value["plan"]
    steps = plan_res.value["steps"]
    bindings = plan_res.value["bound_verifications"]
    work.activate_plan(store, t.id, plan.id,
                       work.load_task(store.read(), t.id).revision)
    return g, work.load_task(store.read(), t.id), plan, steps, bindings


def _execute(store, plan, step, capability_name, args):
    action = execution.create_action(store, step.id, capability_name, args,
                                     caps.get(capability_name).idempotency_class).value
    ok, rej = caps.execute_action(store, action, caps.get(capability_name),
                                  safety_check=make_confirmation_gate(required=False),
                                  plan_id_for_validation=plan.id)
    return action, ok, rej


# ── capability registration shape (§2.1) ─────────────────────────────────────

class TestFileReadRegistration:
    def test_file_read_registered_like_file_write(self):
        spec = caps.get("file_read")
        assert spec is not None
        assert spec.requires_confirmation is False   # read-only: no mutation
        assert spec.idempotency_class.name == "IDEMPOTENT"
        from v5.verification import get_method, registered_methods
        assert get_method("verify_file_read") is not None
        assert "verify_file_read" in registered_methods()

    def test_file_read_missing_path_definite_failure(self, store, tmp_path):
        """A read of a nonexistent path produces Action FAILED, not an
        UNKNOWN_OUTCOME or silently-successful Observation (the capability
        provably could not have read anything; nothing is reported as read)."""
        goal, task, plan, steps, bindings = _chain_two_step(
            store, session_fixture(store), tmp_path / "x.txt", "X")
        target = tmp_path / "ghost.txt"
        a2 = execution.create_action(store, steps[1].id, "file_read",
                                     {"path": str(target)},
                                     caps.get("file_read").idempotency_class).value
        ok, rej = caps.execute_action(store, a2, caps.get("file_read"),
                                      safety_check=make_confirmation_gate(required=False),
                                      plan_id_for_validation=plan.id)
        assert rej is None
        assert ok.value["status"] == "FAILED"
        a2_now = execution.load_action(store.read(), a2.id)
        assert a2_now.status.value == "FAILED"


def session_fixture(store):
    return sessions.create_session(store, cognition_authorized=True)


# ── wrong capability + malformed args (§4) ───────────────────────────────────

class TestFileReadProposalValidation:
    def test_wrong_capability_for_file_read_step(self, store, session, tmp_path):
        """Step declared file_read; model tries file_write on it →
        CAPABILITY_MISMATCH; no Action row is created."""
        goal, task, plan, steps, bindings = _chain_two_step(
            store, session, tmp_path / "x.txt", "X")
        before = store.read().execute("SELECT COUNT(*) n FROM actions").fetchone()["n"]
        r = cognition.propose_action(steps[1].id, steps[1].revision,
                                     "file_write", {"path": "/tmp/x", "content": "y"})
        assert isinstance(r, Rejected) and r.reason == "CAPABILITY_MISMATCH"
        after = store.read().execute("SELECT COUNT(*) n FROM actions").fetchone()["n"]
        assert after == before

    def test_missing_path_rejected(self, store, session, tmp_path):
        goal, task, plan, steps, bindings = _chain_two_step(
            store, session, tmp_path / "x.txt", "X")
        # adapter-level fast-fail is MALFORMED_PROPOSAL (the membrane's
        # documented shape); the authoritative layer (create_action via its
        # capability-schema validation) is CAPABILITY_ARGS_INVALID.
        r = cognition.propose_action(steps[1].id, steps[1].revision, "file_read", {})
        assert isinstance(r, Rejected) and r.reason == "MALFORMED_PROPOSAL"
        assert "path is required" in r.detail
        # and the authoritative layer says the same material thing the hard way
        r2 = execution.create_action(store, steps[1].id, "file_read", {},
                                     caps.get("file_read").idempotency_class)
        assert isinstance(r2, Rejected) and r2.reason == "CAPABILITY_ARGS_INVALID"

    def test_wrong_type_path_rejected(self, store, session, tmp_path):
        goal, task, plan, steps, bindings = _chain_two_step(
            store, session, tmp_path / "x.txt", "X")
        r = cognition.propose_action(steps[1].id, steps[1].revision,
                                     "file_read", {"path": 12345})
        assert isinstance(r, Rejected) and r.reason == "MALFORMED_PROPOSAL"
        r2 = execution.create_action(store, steps[1].id, "file_read",
                                     {"path": 12345},
                                     caps.get("file_read").idempotency_class)
        assert isinstance(r2, Rejected) and r2.reason == "CAPABILITY_ARGS_INVALID"

    def test_extra_argument_rejected(self, store, session, tmp_path):
        """file_read declares only 'path'; a model inventing extra keys is
        rejected — the capability schema is the authority."""
        goal, task, plan, steps, bindings = _chain_two_step(
            store, session, tmp_path / "x.txt", "X")
        r = cognition.propose_action(steps[1].id, steps[1].revision,
                                     "file_read", {"path": "/tmp/x", "encoding": "utf-8"})
        assert isinstance(r, Rejected) and r.reason == "MALFORMED_PROPOSAL"
        assert "unknown argument field" in r.detail


# ── wrong-order dependency attempt (§4, hand-driven) ─────────────────────────

class TestWrongOrderDependency:
    def test_step2_action_before_step1_terminal_fails_honestly(self, store, session, tmp_path):
        """Hand-drive step 2's file_read action before step 1 has even a
        PENDING write. What protects the chain:
          1. the capability itself raises DefiniteNoEffect — the file was
             never written — and the Action goes FAILED (not OBSERVED), so
          2. verify gate rejects: only OBSERVED actions may be verified
             (R_ACTION_NOT_EXECUTED from the audit fix's for_action gate),
          3. Step 2 cannot complete (verification never passed),
          4. Task aggregate completion is rejected COMPLETION_POLICY_UNSATISFIED.
        No new dependency gate is required — the existing lifecycle + audit
        gate + completion-policy combination already covers this."""
        # construct plan but DO NOT drive step 1 to completion first
        goal, task, plan, steps, bindings = _chain_two_step(
            store, session, tmp_path / "never_written.txt", "X")

        # Try to complete step 2's action first: dependency target missing.
        a2 = execution.create_action(store, steps[1].id, "file_read",
                                     {"path": str(tmp_path / "never_written.txt")},
                                     caps.get("file_read").idempotency_class).value
        ok, rej = caps.execute_action(store, a2, caps.get("file_read"),
                                      safety_check=make_confirmation_gate(required=False),
                                      plan_id_for_validation=plan.id)
        assert rej is None                       # capability executed
        assert ok.value["status"] == "FAILED"    # definite-no-effect: proven non-occurrence

        v2 = [b for b in bindings if b["step_id"] == steps[1].id][0]["verification_id"]
        vr = verification.run_method_for_action(store, v2, a2.id)
        assert isinstance(vr, Rejected) and vr.reason == "ACTION_NOT_EXECUTED"

        # step 2 cannot reach COMPLETED honestly
        s2 = work.load_step(store.read(), steps[1].id)
        assert s2.status == StepStatus.PENDING

        # and the aggregate completion gate refuses (existing policy eval)
        t = work.load_task(store.read(), task.id)
        cr = work.complete_object(store, t.id, t.revision)
        assert isinstance(cr, Rejected) and \
            cr.reason == "COMPLETION_POLICY_UNSATISFIED"


# ── the milestone-critical sabotage test (§4) ────────────────────────────────

class TestFileReadVerificationIndependence:
    def test_fabricated_read_content_caught(self, store, session, tmp_path):
        """The capability lies: the Observation claims the file says
        'Goodbye World', but the real file contains 'Hello World'. The
        verifier reads the real filesystem itself → FAIL. This is the proof
        that verify_file_read is not a no-op."""
        target = tmp_path / "truth.txt"
        target.write_text("Hello World")

        goal, task, plan, steps, bindings = _chain_two_step(store, session, target, "Hello World")

        # execute step 1's write honestly
        a1 = execution.create_action(store, steps[0].id, "file_write",
                                     {"path": str(target), "content": "Hello World"},
                                     caps.get("file_write").idempotency_class).value
        ok1, rej1 = caps.execute_action(store, a1, caps.get("file_write"),
                                        safety_check=make_confirmation_gate(required=False),
                                        plan_id_for_validation=plan.id)
        assert rej1 is None and ok1.value["status"] == "OBSERVED"
        v1 = [b for b in bindings if b["step_id"] == steps[0].id][0]["verification_id"]
        assert verification.run_method_for_action(store, v1, a1.id).value["result"].value == "PASS"

        # step 2: sabotage the file_read capability to claim wrong content
        real_read = caps.get("file_read").execute
        def liar(args):
            r = real_read(args)
            return {**r, "content": "Goodbye World"}   # the Observation lies
        caps._REGISTRY["file_read"].execute = liar
        try:
            a2 = execution.create_action(store, steps[1].id, "file_read",
                                         {"path": str(target)},
                                         caps.get("file_read").idempotency_class).value
            ok2, rej2 = caps.execute_action(store, a2, caps.get("file_read"),
                                            safety_check=make_confirmation_gate(required=False),
                                            plan_id_for_validation=plan.id)
        finally:
            caps._REGISTRY["file_read"].execute = real_read
        assert rej2 is None and ok2.value["status"] == "OBSERVED"
        # claim is OBSERVED — but the independent verifier reads the real file
        v2 = [b for b in bindings if b["step_id"] == steps[1].id][0]["verification_id"]
        vr = verification.run_method_for_action(store, v2, a2.id)
        assert isinstance(vr, Ok)
        assert vr.value["result"] == VerificationResult.FAIL  # caught the lie

        # the failed verification blocks step/task completion
        t = work.load_task(store.read(), task.id)
        cr = work.complete_object(store, t.id, t.revision)
        assert isinstance(cr, Rejected) and cr.reason in (
            "VERIFICATION_NOT_PASS", "COMPLETION_POLICY_UNSATISFIED")

    def test_honest_read_passes(self, store, session, tmp_path):
        """Control: capability reads truthfully → PASS → step can proceed."""
        target = tmp_path / "honest.txt"
        target.write_text("Real content")
        goal, task, plan, steps, bindings = _chain_two_step(store, session, target, "Real content")
        a2 = execution.create_action(store, steps[1].id, "file_read", {"path": str(target)},
                                     caps.get("file_read").idempotency_class).value
        ok2, rej2 = caps.execute_action(store, a2, caps.get("file_read"),
                                        safety_check=make_confirmation_gate(required=False),
                                        plan_id_for_validation=plan.id)
        assert rej2 is None
        v2 = [b for b in bindings if b["step_id"] == steps[1].id][0]["verification_id"]
        vr = verification.run_method_for_action(store, v2, a2.id)
        assert vr.value["result"] == VerificationResult.PASS

    def test_read_of_missing_file_fail_not_inconclusive(self, store, session, tmp_path):
        """Fabricated-claim axis: observation says 'found' but the file was
        deleted between action and verification — verifier knows."""
        target = tmp_path / "vanishes.txt"
        target.write_text("temporary")
        goal, task, plan, steps, bindings = _chain_two_step(store, session, target, "temporary")
        a2 = execution.create_action(store, steps[1].id, "file_read", {"path": str(target)},
                                     caps.get("file_read").idempotency_class).value
        ok2, rej2 = caps.execute_action(store, a2, caps.get("file_read"),
                                        safety_check=make_confirmation_gate(required=False),
                                        plan_id_for_validation=plan.id)
        assert rej2 is None and ok2.value["status"] == "OBSERVED"
        # now truth changes: real file is gone; claimed content must FAIL
        target.unlink()
        v2 = [b for b in bindings if b["step_id"] == steps[1].id][0]["verification_id"]
        vr = verification.run_method_for_action(store, v2, a2.id)
        assert vr.value["result"] == VerificationResult.FAIL


# ── failure → structured rejection → corrected proposal → success (§4) ───────

class TestFailureRetryFlow:
    def test_file_read_wrong_path_retry_with_correct(self, store, session, tmp_path):
        """Model proposes a read of the wrong path; Action fails FAILED with a
        structured reason; model (scripted stand-in allowed here) proposes the
        corrected Action as a NEW identity (the failed one is never mutated),
        the corrected one drives to completion."""
        target = tmp_path / "real.txt"
        target.write_text("Good")
        wrong = tmp_path / "typo.txt"

        goal, task, plan, steps, bindings = _chain_two_step(store, session, target, "Good")

        a_bad = cognition.propose_action(steps[1].id, steps[1].revision, "file_read",
                                         {"path": str(wrong)}).value
        ok_fail, _ = caps.execute_action(store, a_bad, caps.get("file_read"),
                                         safety_check=make_confirmation_gate(required=False),
                                         plan_id_for_validation=plan.id)
        assert ok_fail.value["status"] == "FAILED"
        a_bad_now = execution.load_action(store.read(), a_bad.id)
        assert a_bad_now.status.value == "FAILED"

        # correction = a NEW Action identity with the right path (Law 10 —
        # the old attempt is history, never rewritten)
        a_ok = cognition.propose_action(steps[1].id, steps[1].revision, "file_read",
                                        {"path": str(target)}).value
        assert a_ok.id != a_bad.id
        assert execution.load_action(store.read(), a_ok.id).retry_of is None  # not a retry link
        bad_row = execution.load_action(store.read(), a_bad.id)
        assert bad_row.status.value == "FAILED" and bad_row.arguments["path"] == str(wrong)

        # now the corrected path executes, observes, verifies
        ok2, rej2 = caps.execute_action(store, a_ok, caps.get("file_read"),
                                        safety_check=make_confirmation_gate(required=False),
                                        plan_id_for_validation=plan.id)
        assert rej2 is None and ok2.value["status"] == "OBSERVED"
        v2 = [b for b in bindings if b["step_id"] == steps[1].id][0]["verification_id"]
        vr = verification.run_method_for_action(store, v2, a_ok.id)
        assert vr.value["result"] == VerificationResult.PASS


# ── full two-step offline slice (through the live-loop drive machinery) ──────

class TestTwoStepSliceThroughLoop:
    def test_two_step_dependency_order_drive_offline(self, store, session, tmp_path):
        """Scripted provider that reads canonical state each turn to propose
        for the step the loop says is next, driving both steps to completion
        in dependency order."""
        target = tmp_path / "two_step.txt"
        content = "Hello World"

        class TwoStepProvider(ScriptedProvider):
            def call(self, contents, tools, allowed_names):
                turn = self.turns
                self.turns += 1
                c = store.read()
                if turn == 0:
                    name, args = "propose_goal", {"statement": f"Create {target} containing {content} then read it back",
                                                  "completion_policy": {"rule": "ALL_REQUIRED"}}
                elif turn == 1:
                    g = c.execute("SELECT * FROM goals LIMIT 1").fetchone()
                    name, args = "propose_task", {"goal_id": g["id"],
                                                  "expected_goal_revision": g["revision"],
                                                  "statement": "write then read",
                                                  "completion_policy": {"rule": "ALL_REQUIRED"}}
                elif turn == 2:
                    t = c.execute("SELECT * FROM tasks LIMIT 1").fetchone()
                    name, args = "propose_plan", {"task_id": t["id"],
                                                  "expected_task_revision": t["revision"],
                                                  "steps": [
                                                      {"description": f"Write {content} to {target}",
                                                       "required": True, "depends_on_index": [],
                                                       "execution_capability": "file_write",
                                                       "verification_requirements": [
                                                           {"method_name": "verify_file_write",
                                                            "applies_to_capability": "file_write"}]},
                                                      {"description": f"Read {target} back",
                                                       "required": True, "depends_on_index": [0],
                                                       "execution_capability": "file_read",
                                                       "verification_requirements": [
                                                           {"method_name": "verify_file_read",
                                                            "applies_to_capability": "file_read"}]}]}
                else:
                    srow = c.execute(
                        "SELECT * FROM steps WHERE status != 'COMPLETED' ORDER BY rowid LIMIT 1"
                    ).fetchone()
                    cap = srow["execution_capability"]
                    args_ = {"path": str(target)}
                    if cap == "file_write":
                        args_["content"] = content
                    name, args = "propose_action", {"step_id": srow["id"],
                                                    "expected_step_revision": srow["revision"],
                                                    "capability": cap,
                                                    "arguments": args_}
                return FakeResponse(candidates=[FakeCandidate(content=FakeContent(
                    [FakePart(function_call=FakeFunctionCall(name, args))]))])

        r = live_loop.run_live_slice(
            store, session.id,
            f"Create {Path(target).name} containing {content}, then read it back and confirm its contents.",
            str(target), content, TwoStepProvider([]))

        assert r["ok"] is True, r.get("reason")
        assert r["final"]["file_exists"] is True
        assert r["final"]["file_bytes_match"] is True
        assert r["final"]["task_status"] == "COMPLETED"
        assert all(sv == "COMPLETED" for sv in r["final"]["step_statuses"].values())
        # structural assertion: both steps present as host-drives in order
        drive_ids = [e["detail"].get("step_id") for e in r["events"]
                     if e["kind"] == "host_step" and e["detail"].get("op") == "complete_step"]
        assert len(drive_ids) == 2
        # step verification events exist for BOTH capabilities
        verify_ops = [e["detail"].get("operation") for e in r["events"]
                      if e["kind"] == "host_step" and e["stage"] == "verify"]
        assert "verify_file_write" in verify_ops and "verify_file_read" in verify_ops
