"""Confirmation-gate milestone — adversarial tests (Law 21/32/33).

Proves, through the REAL gate at the REAL execution boundary:
  A. unconfirmed execution is blocked (CONFIRMATION_REQUIRED, nothing written),
  B. model retries/rephrases/new-Action proposals cannot bypass the gate,
  C. fabricated confirmation (model-supplied `confirmed`/`confirmation_id`,
     a confirmation row belonging to a different action) cannot authorize,
  D. a confirmed Action's confirmation cannot be reused for another Action,
  E. the arguments binding holds (a staged binding with different arguments
     never authorizes; Action arguments are immutable by design),
  F. revision binding holds (staged-for-N does not authorize N+1),
  G. genuine host-side confirmation → full lifecycle completes.

Plus live-loop integration: the loop derives `required` from the capability
spec, records the blocked first attempt, runs the host confirmation flow,
and only then executes. A refusing or stage-only policy leaves the target
untouched and the run failing honestly.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from v5 import cognition, execution, live_loop, safety, sessions, verification, work
from v5 import capabilities as caps
from v5.enums import ActionStatus, StepStatus, TaskStatus, VerificationResult
from v5.models import CompletionPolicy, Ok, Rejected
from v5.safety import confirm, create_confirmation, make_confirmation_gate
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


def _chain_delete(store, session, path):
    """Scaffold the delete chain through the membrane (host test glue, no
    model): Goal → Task → single-step Plan (file_delete + verify_file_delete)
    → activate. The caller pre-creates the target file."""
    cognition.bind_store(store)
    cognition.authenticate_session(session.id)
    g = cognition.propose_goal(session.id, "delete the temp file", _p()).value
    t = cognition.propose_task(g.id, g.revision, "t", _p()).value
    t = work.transition_object(store, t.id, TaskStatus.ACTIVE, t.revision).value
    plan_res = cognition.propose_plan(t.id, t.revision, [
        cognition.StepProposal("delete the file", True, [], "file_delete",
                               [cognition.VerificationRequirement(
                                   "verify_file_delete", "file_delete")]),
    ])
    assert isinstance(plan_res, Ok), plan_res
    plan = plan_res.value["plan"]
    steps = plan_res.value["steps"]
    bindings = plan_res.value["bound_verifications"]
    work.activate_plan(store, t.id, plan.id,
                       work.load_task(store.read(), t.id).revision)
    return (work.load_task(store.read(), t.id), plan, steps, bindings)


def _propose_delete(store, step, path):
    return cognition.propose_action(step.id, step.revision, "file_delete",
                                   {"path": str(path)})


def _try_execute(store, action, plan, required=True):
    return caps.execute_action(
        store, action, caps.get("file_delete"),
        safety_check=make_confirmation_gate(required=required),
        plan_id_for_validation=plan.id)


def _stage_and_confirm(store, action, *, action_revision=None,
                       capability=None, arguments=None):
    """Host-side staging exactly as _stage_and_confirm does, with optional
    overrides the adversarial tests use to simulate WRONG staging."""
    staged = create_confirmation(
        store, action.id,
        action.revision if action_revision is None else action_revision,
        action.capability if capability is None else capability,
        action.arguments if arguments is None else arguments)
    assert isinstance(staged, Ok)
    confirmed = confirm(store, staged.value)
    assert isinstance(confirmed, Ok)
    return staged.value


# ── A. unconfirmed execution is blocked ──────────────────────────────────────

class TestUnconfirmedExecutionBlocked:
    def test_a_unconfirmed_attempt_rejected_nothing_written(self, store, session, tmp_path):
        target = tmp_path / "victim.txt"
        target.write_text("sensitive")
        task, plan, steps, bindings = _chain_delete(store, session, target)
        action = _propose_delete(store, steps[0], target).value

        ok, rej = _try_execute(store, action, plan, required=True)
        assert ok is None
        assert rej is not None and rej.reason == "CONFIRMATION_REQUIRED"

        # nothing was written: Action still PENDING (never EXECUTING/OBSERVED)
        a_now = execution.load_action(store.read(), action.id)
        assert a_now.status == ActionStatus.PENDING
        # target untouched
        assert target.exists() and target.read_text() == "sensitive"
        # no observation exists
        assert execution.load_observation(store, action.id) is None


# ── B. model retry / rephrase / new Action cannot bypass ────────────────────

class TestRetryRephraseCannotBypass:
    def test_b_repeated_and_new_proposals_all_blocked(self, store, session, tmp_path):
        target = tmp_path / "victim.txt"
        target.write_text("keep me")
        task, plan, steps, bindings = _chain_delete(store, session, target)

        attempts = []
        # same action, tried twice
        a1 = _propose_delete(store, steps[0], target).value
        attempts += [a1, a1]
        # a NEW Action proposal targeting the same file (rephrased identity)
        a2 = _propose_delete(store, steps[0], target).value
        attempts.append(a2)
        # another new one with altered "wording" in the description does not
        # change the fact: same step, same arguments
        a3 = _propose_delete(store, steps[0], target).value
        attempts.append(a3)

        for a in attempts:
            ok, rej = _try_execute(store, a, plan, required=True)
            assert ok is None and rej.reason == "CONFIRMATION_REQUIRED"

        # filesystem unchanged, all actions still PENDING
        assert target.exists() and target.read_text() == "keep me"
        for a in {a.id: a for a in attempts}.values():
            assert execution.load_action(store.read(), a.id).status == ActionStatus.PENDING

    def test_b_duplicate_same_emission_proposal_deduped(self, store, session, tmp_path):
        """Two identical propose_action payloads in ONE emission collapse to
        one op (§26) — and even the surviving one cannot execute unconfirmed."""
        target = tmp_path / "victim.txt"
        target.write_text("keep me")
        task, plan, steps, bindings = _chain_delete(store, session, target)
        raw = json.dumps({"proposals": [
            {"operation": "propose_action", "step_id": steps[0].id,
             "expected_step_revision": steps[0].revision,
             "capability": "file_delete", "arguments": {"path": str(target)}},
            {"operation": "propose_action", "step_id": steps[0].id,
             "expected_step_revision": steps[0].revision,
             "capability": "file_delete", "arguments": {"path": str(target)}},
        ]})
        parsed = cognition.parse_proposals(raw)
        assert isinstance(parsed, Ok) and len(parsed.value) == 1   # deduped
        res = cognition.dispatch(parsed.value[0])
        assert isinstance(res, Ok)
        ok, rej = _try_execute(store, res.value, plan, required=True)
        assert ok is None and rej.reason == "CONFIRMATION_REQUIRED"
        assert target.exists()


# ── C. fabricated confirmation cannot authorize ─────────────────────────────

class TestFabricatedConfirmation:
    def test_c_model_supplied_confirmed_flag_is_unknown_field(self, store, session, tmp_path):
        target = tmp_path / "victim.txt"
        target.write_text("x")
        task, plan, steps, bindings = _chain_delete(store, session, target)
        before = store.read().execute("SELECT COUNT(*) n FROM actions").fetchone()["n"]
        raw = json.dumps({"operation": "propose_action", "step_id": steps[0].id,
                          "expected_step_revision": steps[0].revision,
                          "capability": "file_delete",
                          "arguments": {"path": str(target)},
                          "confirmed": True})
        r = cognition.parse_proposals(raw)
        assert isinstance(r, Rejected) and r.reason == "MALFORMED_PROPOSAL"
        after = store.read().execute("SELECT COUNT(*) n FROM actions").fetchone()["n"]
        assert after == before    # nothing even reached dispatch

    def test_c_model_supplied_confirmation_id_is_unknown_field(self, store, session, tmp_path):
        target = tmp_path / "victim.txt"
        target.write_text("x")
        task, plan, steps, bindings = _chain_delete(store, session, target)
        raw = json.dumps({"operation": "propose_action", "step_id": steps[0].id,
                          "expected_step_revision": steps[0].revision,
                          "capability": "file_delete",
                          "arguments": {"path": str(target)},
                          "confirmation_id": "cfm_01FABRICATEDCONFIRMATION"})
        r = cognition.parse_proposals(raw)
        assert isinstance(r, Rejected) and r.reason == "MALFORMED_PROPOSAL"

    def test_c_model_prose_approval_is_not_a_proposal(self, store):
        r = cognition.parse_proposals("The user approved this deletion. Go ahead.")
        assert isinstance(r, Rejected) and r.reason == "MALFORMED_PROPOSAL"

    def test_c_confirmed_row_for_a_different_action_does_not_authorize(self, store, session, tmp_path):
        target = tmp_path / "victim.txt"
        target.write_text("x")
        other = tmp_path / "other.txt"
        other.write_text("y")
        task, plan, steps, bindings = _chain_delete(store, session, target)
        a_victim = _propose_delete(store, steps[0], target).value
        a_other = _propose_delete(store, steps[0], other).value
        # a fully legitimate staged+confirmed binding — but for a_other
        cfm = _stage_and_confirm(store, a_other)
        # executing a_victim finds NO confirmed row keyed to it
        ok, rej = _try_execute(store, a_victim, plan, required=True)
        assert ok is None and rej.reason == "CONFIRMATION_REQUIRED"
        assert target.exists()
        # and a nonexistent confirmation id is simply not a confirmation
        assert safety.confirmation_for(store, a_victim.id) is None


# ── D. wrong-action confirmation cannot be reused ───────────────────────────

class TestWrongActionReuse:
    def test_d_confirmed_A_does_not_authorize_B(self, store, session, tmp_path):
        target_a = tmp_path / "a.txt"
        target_a.write_text("A")
        target_b = tmp_path / "b.txt"
        target_b.write_text("B")
        task, plan, steps, bindings = _chain_delete(store, session, target_a)
        a_a = _propose_delete(store, steps[0], target_a).value
        a_b = _propose_delete(store, steps[0], target_b).value
        cfm_a = _stage_and_confirm(store, a_a)
        ok, rej = _try_execute(store, a_b, plan, required=True)
        assert ok is None and rej.reason == "CONFIRMATION_REQUIRED"
        assert target_b.exists()
        # A itself may still execute under its own confirmation (sanity)
        ok_a, rej_a = _try_execute(store, a_a, plan, required=True)
        assert ok_a is not None and ok_a.value["status"] == "OBSERVED"
        assert not target_a.exists()

    def test_d_binding_pointed_at_A_rejected_for_B_on_arguments(self, store, session, tmp_path):
        """The stronger variant: B carries confirmation_id = A's confirmed
        binding AND has its own confirmed row. The gate's specific path still
        rejects — A's binding does not match B's arguments (Law 32)."""
        target_a = tmp_path / "a.txt"
        target_a.write_text("A")
        target_b = tmp_path / "b.txt"
        target_b.write_text("B")
        task, plan, steps, bindings = _chain_delete(store, session, target_a)
        a_a = _propose_delete(store, steps[0], target_a).value
        cfm_a = _stage_and_confirm(store, a_a)
        # B: same step, different target, explicitly pointed at A's binding
        a_b = execution.create_action(
            store, steps[0].id, "file_delete", {"path": str(target_b)},
            caps.get("file_delete").idempotency_class,
            confirmation_id=cfm_a).value
        _stage_and_confirm(store, a_b)   # B's own row exists and matches...
        ok, rej = _try_execute(store, a_b, plan, required=True)
        # ...but the gate refuses the pointed-at foreign binding on arguments
        assert ok is None and rej.reason == "CONFIRMATION_REQUIRED"
        assert "arguments" in rej.detail or "binding" in rej.detail
        assert target_b.exists()


# ── E. arguments binding (immutable-by-design; staging-side check) ──────────

class TestArgumentsBinding:
    def test_e_mis_staged_arguments_never_authorize(self, store, session, tmp_path):
        """Action arguments are immutable by design (no public mutation
        path — a safety property). The arguments binding is therefore
        exercised through the legitimate staging mechanism: a confirmed
        binding staged for DIFFERENT arguments must not authorize."""
        target = tmp_path / "victim.txt"
        target.write_text("x")
        decoy = tmp_path / "decoy.txt"
        decoy.write_text("y")
        task, plan, steps, bindings = _chain_delete(store, session, target)
        a = _propose_delete(store, steps[0], target).value
        # host mis-stages: confirmed binding carries the DECOY's arguments
        _stage_and_confirm(store, a, arguments={"path": str(decoy)})
        ok, rej = _try_execute(store, a, plan, required=True)
        assert ok is None and rej.reason == "CONFIRMATION_REQUIRED"
        assert "capability/arguments" in rej.detail
        assert target.exists() and decoy.exists()

    def test_e_mis_staged_capability_never_authorizes(self, store, session, tmp_path):
        target = tmp_path / "victim.txt"
        target.write_text("x")
        task, plan, steps, bindings = _chain_delete(store, session, target)
        a = _propose_delete(store, steps[0], target).value
        _stage_and_confirm(store, a, capability="file_write")  # wrong capability
        ok, rej = _try_execute(store, a, plan, required=True)
        assert ok is None and rej.reason == "CONFIRMATION_REQUIRED"
        assert target.exists()


# ── F. revision binding ──────────────────────────────────────────────────────

class TestRevisionBinding:
    def test_f_staged_for_wrong_revision_rejected(self, store, session, tmp_path):
        target = tmp_path / "victim.txt"
        target.write_text("x")
        task, plan, steps, bindings = _chain_delete(store, session, target)
        a = _propose_delete(store, steps[0], target).value
        assert a.revision == 0
        _stage_and_confirm(store, a, action_revision=5)   # stale/wrong revision
        ok, rej = _try_execute(store, a, plan, required=True)
        assert ok is None and rej.reason == "CONFIRMATION_REQUIRED"
        assert "revision" in rej.detail
        assert target.exists()

    def test_f_confirmation_for_N_does_not_authorize_N_plus_1(self, store, session, tmp_path):
        """Stage+confirm at revision 0, then legitimately advance the Action
        to revision 1 (repair bumps revision — the same mechanism
        test_hardening uses). The old confirmation must not authorize."""
        from v5 import repair as repair_mod
        target = tmp_path / "victim.txt"
        target.write_text("x")
        task, plan, steps, bindings = _chain_delete(store, session, target)
        a = _propose_delete(store, steps[0], target).value
        _stage_and_confirm(store, a)                      # bound at revision 0
        auth = repair_mod.authorize("debasish")
        r = repair_mod.repair_object(store, auth, a.id, 0,
                                     target_state={"status": "PENDING"},
                                     reason="material change bumps revision")
        assert isinstance(r, Ok)
        a_now = execution.load_action(store.read(), a.id)
        assert a_now.revision == 1
        ok, rej = _try_execute(store, a_now, plan, required=True)
        assert ok is None and rej.reason == "CONFIRMATION_REQUIRED"
        assert "revision" in rej.detail
        assert target.exists()


# ── G. genuine host-side confirmation completes the lifecycle ───────────────

class TestConfirmedExecution:
    def test_g_full_lifecycle_after_confirmation(self, store, session, tmp_path):
        target = tmp_path / "victim.txt"
        target.write_text("delete me")
        task, plan, steps, bindings = _chain_delete(store, session, target)
        action = _propose_delete(store, steps[0], target).value
        assert action.status == ActionStatus.PENDING

        # blocked first
        ok, rej = _try_execute(store, action, plan, required=True)
        assert ok is None and rej.reason == "CONFIRMATION_REQUIRED"
        # genuine host confirmation through the existing mechanism
        cfm = _stage_and_confirm(store, action)
        # now executes
        ok2, rej2 = _try_execute(store, action, plan, required=True)
        assert rej2 is None and ok2.value["status"] == "OBSERVED"

        # independent verification: filesystem inspection says gone
        ver_id = bindings[0]["verification_id"]
        vr = verification.run_method_for_action(store, ver_id, action.id)
        assert isinstance(vr, Ok) and vr.value["result"] == VerificationResult.PASS
        assert not target.exists()

        # step + task complete through Work Service
        s = work.load_step(store.read(), steps[0].id)
        s = work.transition_object(store, s.id, StepStatus.READY, s.revision).value
        s = work.transition_object(store, s.id, StepStatus.EXECUTING, s.revision).value
        work.complete_step(store, s.id, s.revision)
        t = work.load_task(store.read(), task.id)
        cr = work.complete_object(store, t.id, t.revision)
        assert isinstance(cr, Ok)
        assert cr.value["object"].status == TaskStatus.COMPLETED


# ── live-loop integration: spec-derived gating + host confirmation flow ─────

class _DeleteProvider(ScriptedProvider):
    """Scripted (offline) provider driving a single file_delete chain."""
    def __init__(self, store, step, target):
        super().__init__([])
        self._store, self._step_path, self._target = store, step, target

    def call(self, contents, tools, allowed_names):
        turn = self.turns
        self.turns += 1
        c = self._store.read()
        if turn == 0:
            name, args = "propose_goal", {
                "statement": "Delete this temporary test file.",
                "completion_policy": {"rule": "ALL_REQUIRED"}}
        elif turn == 1:
            g = c.execute("SELECT * FROM goals LIMIT 1").fetchone()
            name, args = "propose_task", {
                "goal_id": g["id"], "expected_goal_revision": g["revision"],
                "statement": "delete it", "completion_policy": {"rule": "ALL_REQUIRED"}}
        elif turn == 2:
            t = c.execute("SELECT * FROM tasks LIMIT 1").fetchone()
            name, args = "propose_plan", {
                "task_id": t["id"], "expected_task_revision": t["revision"],
                "steps": [{"description": "delete the temp file",
                           "required": True, "depends_on_index": [],
                           "execution_capability": "file_delete",
                           "verification_requirements": [
                               {"method_name": "verify_file_delete",
                                "applies_to_capability": "file_delete"}]}]}
        else:
            srow = c.execute("SELECT * FROM steps LIMIT 1").fetchone()
            name, args = "propose_action", {
                "step_id": srow["id"], "expected_step_revision": srow["revision"],
                "capability": "file_delete", "arguments": {"path": str(self._target)}}
        return FakeResponse(candidates=[FakeCandidate(content=FakeContent(
            [FakePart(function_call=FakeFunctionCall(name, args))]))])


_DELETE_GUIDANCE_HEAD = (
    "propose_plan carries one StepProposal for this milestone: a single "
    "file_delete step (execution_capability \"file_delete\") whose "
    "verification_requirements name verify_file_delete with "
    "applies_to_capability \"file_delete\". file_delete arguments: {\"path\"} "
    "only. Target path: ")


class TestLiveLoopConfirmationIntegration:
    def _run(self, store, session, tmp_path, **kw):
        target = tmp_path / "to_delete.txt"
        target.write_text("temporary test content")
        provider = _DeleteProvider(store, None, target)
        result = live_loop.run_live_slice(
            store, session.id, "Delete this temporary test file.",
            str(target), "", provider,
            plan_guidance=_DELETE_GUIDANCE_HEAD + json.dumps(str(target)),
            **kw)
        return target, result

    def test_loop_blocks_then_confirms_then_completes(self, store, session, tmp_path):
        target, r = self._run(store, session, tmp_path)
        assert r["ok"] is True, r.get("reason")
        # transcript shows the exact proof sequence
        ops = [(e["detail"].get("op"), e["stage"]) for e in r["events"]
               if e["kind"] == "host_step"]
        assert ("execution_blocked", "confirm") in ops
        assert ("confirmation_granted", "confirm") in ops
        # execution succeeded AFTER confirmation
        blocked_at = max(i for i, e in enumerate(r["events"])
                         if e["kind"] == "host_step"
                         and e["detail"].get("op") == "execution_blocked")
        granted_at = max(i for i, e in enumerate(r["events"])
                         if e["kind"] == "host_step"
                         and e["detail"].get("op") == "confirmation_granted")
        executed_at = max(i for i, e in enumerate(r["events"])
                          if e["kind"] == "host_step"
                          and e["detail"].get("op") == "execute_action"
                          and e["detail"].get("status") == "ok")
        assert blocked_at < granted_at < executed_at
        # blocked reason is the real gate's reason
        blocked = [e for e in r["events"] if e["detail"].get("op") == "execution_blocked"][0]
        assert blocked["detail"]["reason"] == "CONFIRMATION_REQUIRED"
        # final state: gone, COMPLETED everywhere
        assert not target.exists()
        assert r["final"]["file_exists"] is False
        assert r["final"]["task_status"] == "COMPLETED"
        assert all(sv == "COMPLETED" for sv in r["final"]["step_statuses"].values())

    def test_loop_refused_confirmation_leaves_target(self, store, session, tmp_path):
        def refusing_policy(store_, action):
            return Rejected("CONFIRMATION_REFUSED", "host declined to confirm", action)
        target, r = self._run(store, session, tmp_path, confirmation_policy=refusing_policy)
        assert r["ok"] is False
        assert r["reason"] == "CONFIRMATION_REFUSED"
        assert target.exists()
        # the action was persisted but never executed
        arow = store.read().execute("SELECT status FROM actions ORDER BY rowid DESC LIMIT 1").fetchone()
        assert arow["status"] == "PENDING"

    def test_loop_stage_only_never_confirms_so_blocked_forever(self, store, session, tmp_path):
        """Staging alone is NOT confirmation: a policy that stages a binding
        but never calls confirm() leaves the action blocked (Law 32 requires
        the human confirm() step)."""
        def stage_only(store_, action):
            staged = create_confirmation(store_, action.id, action.revision,
                                         action.capability, action.arguments)
            assert isinstance(staged, Ok)
            return Ok({"confirmation_id": staged.value})   # staged, NOT confirmed
        target, r = self._run(store, session, tmp_path, confirmation_policy=stage_only)
        assert r["ok"] is False
        assert r["reason"] == "CONFIRMATION_REQUIRED"
        assert target.exists()

    def test_loop_non_confirmation_capability_unchanged(self, store, session, tmp_path):
        """Regression shape: file_read (requires_confirmation=False) drives
        without any confirmation events — the wiring is spec-derived, not a
        blanket gate."""
        target = tmp_path / "readme.txt"
        target.write_text("still here")

        class ReadProvider(_DeleteProvider):
            def call(self, contents, tools, allowed_names):
                turn = self.turns
                self.turns += 1
                c = self._store.read()
                if turn == 0:
                    name, args = "propose_goal", {
                        "statement": "read the file", "completion_policy": {"rule": "ALL_REQUIRED"}}
                elif turn == 1:
                    g = c.execute("SELECT * FROM goals LIMIT 1").fetchone()
                    name, args = "propose_task", {
                        "goal_id": g["id"], "expected_goal_revision": g["revision"],
                        "statement": "read", "completion_policy": {"rule": "ALL_REQUIRED"}}
                elif turn == 2:
                    t = c.execute("SELECT * FROM tasks LIMIT 1").fetchone()
                    name, args = "propose_plan", {
                        "task_id": t["id"], "expected_task_revision": t["revision"],
                        "steps": [{"description": "read the file", "required": True,
                                   "depends_on_index": [], "execution_capability": "file_read",
                                   "verification_requirements": [
                                       {"method_name": "verify_file_read",
                                        "applies_to_capability": "file_read"}]}]}
                else:
                    srow = c.execute("SELECT * FROM steps LIMIT 1").fetchone()
                    name, args = "propose_action", {
                        "step_id": srow["id"], "expected_step_revision": srow["revision"],
                        "capability": "file_read", "arguments": {"path": str(self._target)}}
                return FakeResponse(candidates=[FakeCandidate(content=FakeContent(
                    [FakePart(function_call=FakeFunctionCall(name, args))]))])

        r = live_loop.run_live_slice(
            store, session.id, "Read the file back.", str(target), "",
            ReadProvider(store, None, target),
            plan_guidance="one file_read step with verify_file_read. args: {\"path\"}")
        assert r["ok"] is True, r.get("reason")
        ops = [e["detail"].get("op") for e in r["events"] if e["kind"] == "host_step"]
        assert "execution_blocked" not in ops
        assert "confirmation_granted" not in ops
        assert target.exists() and target.read_text() == "still here"


# ── capability registration shape ────────────────────────────────────────────

class TestFileDeleteRegistration:
    def test_registry_shape(self):
        spec = caps.get("file_delete")
        assert spec is not None
        assert spec.requires_confirmation is True
        assert spec.idempotency_class.name == "IDEMPOTENT"
        assert spec.validate_args({}) is not None
        assert spec.validate_args({"path": "/tmp/x", "extra": 1}) is not None
        assert spec.validate_args({"path": 42}) is not None
        assert spec.validate_args({"path": "/tmp/x"}) is None
        from v5.verification import get_method, registered_methods
        assert get_method("verify_file_delete") is not None
        assert "verify_file_delete" in registered_methods()

    def test_repo_guard_refuses_v5_tree(self, tmp_path):
        import v5.capabilities as capmod
        repo_file = capmod.__file__          # a real path inside the V5 repo
        with pytest.raises(capmod.DefiniteNoEffect):
            capmod._file_delete({"path": repo_file})

    def test_missing_file_is_definite_no_effect(self, tmp_path):
        import v5.capabilities as capmod
        with pytest.raises(capmod.DefiniteNoEffect):
            capmod._file_delete({"path": str(tmp_path / "nope.txt")})

    def test_verifier_passes_only_on_absence(self, store, session, tmp_path):
        """Sabotage axis: a capability that lies about deleting (reports
        success, file untouched) must FAIL the independent verifier."""
        target = tmp_path / "still_here.txt"
        target.write_text("survivor")
        task, plan, steps, bindings = _chain_delete(store, session, target)
        action = _propose_delete(store, steps[0], target).value
        _stage_and_confirm(store, action)
        # sabotage: the capability "succeeds" without deleting
        real = caps.get("file_delete").execute
        caps._REGISTRY["file_delete"].execute = lambda args: {
            "path": args["path"], "existed": True, "deleted": True}
        try:
            ok, rej = _try_execute(store, action, plan, required=True)
        finally:
            caps._REGISTRY["file_delete"].execute = real
        assert rej is None and ok.value["status"] == "OBSERVED"
        ver_id = bindings[0]["verification_id"]
        vr = verification.run_method_for_action(store, ver_id, action.id)
        assert vr.value["result"] == VerificationResult.FAIL    # caught the lie
        assert target.exists()
