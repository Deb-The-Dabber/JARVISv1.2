"""Cognition Implementation Contract v1.1 — behavioral compliance tests.

Cognize the membrane, not the machinery: every test here exercises the
frozen propose_* boundary + adapter end-to-end against the real Store and
Work Service. No state writes happen anywhere except through Work Service.

Sections map to the contract's required test matrix (§44–§49).
"""
from __future__ import annotations

import json

import pytest

from v5 import cognition, evidence, execution, obligations, sessions, verification, work
from v5 import capabilities as caps
from v5.enums import StepStatus, TaskStatus, VerificationResult
from v5.models import CompletionPolicy, Ok, Rejected
from v5.safety import make_confirmation_gate


@pytest.fixture
def store(tmp_path):
    from v5.store import Store
    s = Store(str(tmp_path / "state.db"))
    cognition.bind_store(s)
    yield s
    cognition.deauthenticate()
    s.close()


@pytest.fixture
def session(store):
    """A live, Cognition-authorized session (§13.3)."""
    return sessions.create_session(store, cognition_authorized=True)


def _policy():
    return CompletionPolicy(rule="ALL_REQUIRED")


def _propose_chain(store, session, path, content, verify=True):
    """Drive the §40 vertical slice up through Action(PENDING).
    Returns (goal, task, plan, step, action, verification_id)."""
    cognition.authenticate_session(session.id)
    g = cognition.propose_goal(session.id, f"Create {path} containing {content}.", _policy())
    assert isinstance(g, Ok), g
    goal = g.value

    t = cognition.propose_task(goal.id, goal.revision,
                               f"Write {content} to {path}", _policy())
    assert isinstance(t, Ok), t
    task = t.value
    # Work lifecycle transitions are Work Service operations — Cognition only
    # proposed the objects (§3); activation uses the contracted path.
    task = work.transition_object(store, task.id, TaskStatus.ACTIVE, task.revision).value

    vr = []
    if verify:
        vr.append(cognition.VerificationRequirement(
            method_name="verify_file_write", applies_to_capability="file_write"))
    p = cognition.propose_plan(task.id, task.revision, [
        cognition.StepProposal(
            description=f"Create {path} containing {content}",
            required=True, depends_on_index=[], execution_capability="file_write",
            verification_requirements=vr,
        )
    ])
    assert isinstance(p, Ok), p
    plan = p.value["plan"]
    step = p.value["steps"][0]
    assert step.execution_capability == "file_write"
    ver_id = p.value["bound_verifications"][0]["verification_id"] if vr else None

    task = work.load_task(store.read(), task.id)
    plan_activation = work.activate_plan(store, task.id, plan.id, task.revision)
    assert isinstance(plan_activation, Ok), plan_activation

    a = cognition.propose_action(step.id, step.revision, "file_write",
                                 {"path": str(path), "content": content})
    assert isinstance(a, Ok), a
    action = a.value
    return goal, task, plan, step, action, ver_id


def _execute_and_verify(store, plan, task, step, action, ver_id, tmp_path):
    """The contracted execution boundary — the ONLY way the capability runs."""
    ok, rej = caps.execute_action(store, action, caps.get("file_write"),
                                  safety_check=make_confirmation_gate(required=False),
                                  plan_id_for_validation=plan.id)
    assert rej is None, rej
    assert ok.value["status"] == "OBSERVED"

    # evidence from the real observation, then the registered verifier —
    # an INDEPENDENT filesystem read, not the Observation (§34/§35)
    obs_id = ok.value["observation_id"]
    ev = evidence.record_runtime_evidence(store, obs_id, source="file_write",
                                          relevance_to=task.id,
                                          content={"action_id": action.id}).value
    assert ev.origin_observation_id == obs_id

    if ver_id is not None:
        r = verification.run_method_for_action(store, ver_id, action.id)
        assert isinstance(r, Ok), r
        assert r.value["result"] == VerificationResult.PASS

    s = work.load_step(store.read(), step.id)
    s = work.transition_object(store, s.id, StepStatus.READY, s.revision).value
    s = work.transition_object(store, s.id, StepStatus.EXECUTING, s.revision).value
    work.complete_step(store, s.id, s.revision)
    t = work.load_task(store.read(), task.id)
    cr = work.complete_object(store, t.id, t.revision)
    return cr


# ═══ §6/§40 — the vertical slice, end to end ═══════════════════════════════

class TestVerticalSlice:
    def test_full_slice_end_to_end(self, store, session, tmp_path):
        """User request → Goal → Task → Plan/Step → Action(PENDING) →
        execution → Observation → INDEPENDENT Verification → completion.
        §43 acceptance criteria in one behavioral proof."""
        target = tmp_path / "foo.txt"
        content = "Hello World"
        goal, task, plan, step, action, ver_id = _propose_chain(
            store, session, target, content)

        # §43: Action starts PENDING, nothing executed by proposal
        assert action.status.value == "PENDING"
        assert not target.exists()
        assert execution.load_observation(store, action.id) is None

        cr = _execute_and_verify(store, plan, task, step, action, ver_id, tmp_path)
        assert isinstance(cr, Ok), cr
        assert cr.value["object"].status == TaskStatus.COMPLETED
        assert target.read_text() == content
        # provenance lock: session_origin still identifies the creating session
        g_now = work.load_goal(store.read(), goal.id)
        assert g_now.session_origin == session.id

    def test_action_stays_pending_until_execution(self, store, session, tmp_path):
        """§43/§49 — proposal acceptance alone produces no execution."""
        target = tmp_path / "pending.txt"
        _, task, plan, step, action, _ = _propose_chain(store, session, target, "data")
        # nothing executed: no observation, no file
        assert execution.load_observation(store, action.id) is None
        assert not target.exists()
        loaded = execution.load_action(store.read(), action.id)
        assert loaded.status.value == "PENDING"


# ═══ §13.4 — the payoff test ( canonical Session ≠ Work sequence ) ══════════

class TestSessionNeWork:
    def test_payoff_session_a_creates_session_b_continues(self, store):
        """VERBATIM §13.4: Session A creates Goal → terminates; Session B
        authenticates fresh, resolves to the same principal, proposes a Task
        against that Goal; Goal.session_origin still reads 'Session A'."""
        sess_a = sessions.create_session(store)
        cognition.authenticate_session(sess_a.id)
        g = cognition.propose_goal(sess_a.id, "Payoff: Session≠Work", _policy())
        assert isinstance(g, Ok), g
        goal = g.value
        assert goal.session_origin == sess_a.id

        sessions.terminate_session(store, sess_a.id)
        # old session can no longer act
        r = cognition.propose_task(goal.id, goal.revision, "x", _policy())
        assert isinstance(r, Rejected) and r.reason == "UNAUTHORIZED"

        sess_b = sessions.create_session(store)   # entirely new conversation
        auth = cognition.authenticate_session(sess_b.id)
        assert isinstance(auth, Ok)
        # both sessions resolve to the SAME deployment principal (§13.1)
        assert auth.value.principal_id == sessions.OWNER_PRINCIPAL_ID

        t = cognition.propose_task(goal.id, goal.revision, "continue", _policy())
        assert isinstance(t, Ok), t   # B can address A's goal
        g_now = work.load_goal(store.read(), goal.id)
        assert g_now.session_origin == sess_a.id   # provenance preserved

    def test_session_origin_is_not_authorization(self, store):
        """Corrupting provenance must NOT grant or deny anything — the field
        is inert for authorization by construction (§44)."""
        sess_a = sessions.create_session(store)
        cognition.authenticate_session(sess_a.id)
        g = cognition.propose_goal(sess_a.id, "origin provenance test", _policy()).value
        # an adversary-like direct test-setup mutation of provenance:
        with store.write() as conn:
            conn.execute("UPDATE goals SET session_origin = 'attacker' WHERE id = ?", (g.id,))
        sess_b = sessions.create_session(store)
        cognition.authenticate_session(sess_b.id)
        t = cognition.propose_task(g.id, g.revision, "still fine", _policy())
        assert isinstance(t, Ok), t   # provenance corruption never gates access

    def test_unauthorized_session_rejected_distinctly(self, store, session):
        """§13.3: cognition_authorized=False session → R_UNAUTHORIZED, and
        it's distinct from R_NOT_FOUND/R_STALE_REVISION (§10.4)."""
        g = cognition.propose_goal(session.id, "g", _policy()).value
        sess_bad = sessions.create_session(store, cognition_authorized=False)
        cognition.authenticate_session(sess_bad.id)
        assert cognition.current_context() is None or \
            cognition.current_context().session_id != sess_bad.id
        cognition.authenticate_session(session.id)  # back to the real one
        # now authenticate the bad one explicitly and confirm propose rejects
        r = cognition.authenticate_session(sess_bad.id)
        assert isinstance(r, Rejected) and r.reason == "UNAUTHORIZED"
        r2 = cognition.propose_task(g.id, g.revision, "x", _policy())
        assert isinstance(r2, Rejected) and r2.reason == "UNAUTHORIZED"
        # and it is NOT the same as the not-found path:
        cognition.authenticate_session(session.id)
        r3 = cognition.propose_task("goal_nonexistent", 0, "x", _policy())
        assert isinstance(r3, Rejected) and r3.reason == "NOT_FOUND"


# ═══ §7/§44 — proposal validation (the adapter boundary) ═══════════════════

class TestProposalValidation:
    def test_overlong_goal_statement(self, store, session):
        r = cognition.propose_goal(session.id, "x" * (cognition.MAX_STATEMENT_LEN + 1),
                                   _policy())
        assert isinstance(r, Rejected) and r.reason == "MALFORMED_PROPOSAL"

    def test_missing_required_field(self, store, session):
        raw = json.dumps({"operation": "propose_task", "goal_id": "g",
                          "statement": "s", "completion_policy": {"rule": "ALL_REQUIRED"}})
        r = cognition.parse_proposals(raw)
        assert isinstance(r, Rejected) and r.reason == "MALFORMED_PROPOSAL"
        assert "expected_goal_revision is required" in r.detail

    def test_unknown_proposal_field(self, store, session):
        raw = json.dumps({"operation": "propose_goal", "statement": "x",
                          "completion_policy": {"rule": "ALL_REQUIRED"},
                          "trust_me": True})
        r = cognition.parse_proposals(raw)
        assert isinstance(r, Rejected) and r.reason == "MALFORMED_PROPOSAL"
        assert "unknown field" in r.detail

    def test_model_cannot_set_session_or_principal(self, store, session):
        """§13.2 — model-supplied session_id/principal_id are unknown fields
        on proposals and are rejected, never threaded."""
        raw = json.dumps({"operation": "propose_goal", "statement": "x",
                          "completion_policy": {"rule": "ALL_REQUIRED"},
                          "principal_id": "attacker-principal"})
        assert isinstance(cognition.parse_proposals(raw), Rejected)
        raw = json.dumps({"operation": "propose_task", "goal_id": "g",
                          "expected_goal_revision": 0, "statement": "s",
                          "completion_policy": {"rule": "ALL_REQUIRED"},
                          "session_id": "attacker-session"})
        assert isinstance(cognition.parse_proposals(raw), Rejected)

    def test_malformed_json_rejected_before_any_mutation(self, store, session):
        g_count = store.read().execute("SELECT COUNT(*) n FROM goals").fetchone()["n"]
        r = cognition.parse_proposals('{"operation": "propose_goal", BAD JSON')
        assert isinstance(r, Rejected) and r.reason == "MALFORMED_PROPOSAL"
        g_count2 = store.read().execute("SELECT COUNT(*) n FROM goals").fetchone()["n"]
        assert g_count2 == g_count

    def test_narrated_tool_call_is_not_a_proposal(self, store):
        r = cognition.parse_proposals('I will call file_write now')
        assert isinstance(r, Rejected) and r.reason == "MALFORMED_PROPOSAL"
        assert "not a structured proposal" in r.detail or "not a proposal" in r.detail

    def test_cyclic_dependency_graph_rejected(self, store, session):
        t = cognition.propose_goal(session.id, "cycle test", _policy()).value
        tk = cognition.propose_task(t.id, t.revision, "t", _policy()).value
        steps = [
            cognition.StepProposal("a", True, [1], "file_write"),
            cognition.StepProposal("b", True, [0], "file_write"),
        ]
        r = cognition.propose_plan(tk.id, tk.revision, steps)
        assert isinstance(r, Rejected) and r.reason == "DEPENDENCY_CYCLE"

    def test_invalid_dependency_index_rejected(self, store, session):
        t = cognition.propose_goal(session.id, "dep test", _policy()).value
        tk = cognition.propose_task(t.id, t.revision, "t", _policy()).value
        steps = [cognition.StepProposal("a", True, [7], "file_write")]
        r = cognition.propose_plan(tk.id, tk.revision, steps)
        assert isinstance(r, Rejected) and r.reason == "MALFORMED_PROPOSAL"
        assert "outside" in r.detail

    def test_unknown_verification_method_rejected(self, store, session):
        t = cognition.propose_goal(session.id, "vm", _policy()).value
        tk = cognition.propose_task(t.id, t.revision, "t", _policy()).value
        steps = [cognition.StepProposal("a", True, [], "file_write",
                                        [cognition.VerificationRequirement("nope", "file_write")])]
        r = cognition.propose_plan(tk.id, tk.revision, steps)
        assert isinstance(r, Rejected) and r.reason == "UNKNOWN_VERIFICATION_METHOD"

    def test_verification_capability_mismatch_rejected(self, store, session):
        """§11.5/§29 — applies_to_capability must equal execution_capability."""
        t = cognition.propose_goal(session.id, "vm", _policy()).value
        tk = cognition.propose_task(t.id, t.revision, "t", _policy()).value
        steps = [cognition.StepProposal("a", True, [], "file_write",
                                        [cognition.VerificationRequirement(
                                            "verify_file_write", "delete_file")])]
        r = cognition.propose_plan(tk.id, tk.revision, steps)
        assert isinstance(r, Rejected) and r.reason == "VERIFICATION_CAPABILITY_MISMATCH"

    def test_unknown_capability_rejected(self, store, session):
        t = cognition.propose_goal(session.id, "uc", _policy()).value
        tk = cognition.propose_task(t.id, t.revision, "t", _policy()).value
        steps = [cognition.StepProposal("a", True, [], "unknown_capability_xyz")]
        r = cognition.propose_plan(tk.id, tk.revision, steps)
        assert isinstance(r, Rejected)

    def test_hallucinated_id_never_guessed(self, store, session):
        """§24 — a syntactically-valid but nonexistent ID is NOT_FOUND; the
        adapter never substitutes or guesses."""
        cognition.authenticate_session(session.id)
        r = cognition.propose_task("goal_01THISDOESNOTEXIST000000000", 0, "x", _policy())
        assert isinstance(r, Rejected) and r.reason == "NOT_FOUND"
        raw = json.dumps({"operation": "propose_task",
                          "goal_id": "goal_01THISDOESNOTEXIST000000000",
                          "expected_goal_revision": 0, "statement": "x",
                          "completion_policy": {"rule": "ALL_REQUIRED"}})
        parsed = cognition.parse_proposals(raw)
        assert isinstance(parsed, Ok)
        r2 = cognition.dispatch(parsed.value[0])
        assert isinstance(r2, Rejected) and r2.reason == "NOT_FOUND"


# ═══ §12/§29/§46 — capability correspondence + execution boundary ═══════════

class TestCapabilityCorrespondence:
    def test_action_capability_mismatch_rejected(self, store, session):
        """§46 — Step declares file_write; Action proposes delete_file is
        rejected; no Action created; Step unchanged; nothing executes."""
        target = "/tmp/del_me.txt"
        _, task, plan, step, _, _ = _propose_chain(store, session, target, "data")
        before = store.read().execute("SELECT COUNT(*) n FROM actions").fetchone()["n"]
        r = cognition.propose_action(step.id, step.revision, "delete_file",
                                     {"path": target})
        assert isinstance(r, Rejected) and r.reason == "CAPABILITY_MISMATCH"
        after = store.read().execute("SELECT COUNT(*) n FROM actions").fetchone()["n"]
        assert after == before   # no Action created

    def test_propose_action_never_executes(self, store, session, monkeypatch):
        """§12.3/§49 — instrument the capability: accept the Action, execute
        count must remain zero until the contracted boundary runs it."""
        calls = {"n": 0}
        real = caps.get("file_write").execute
        def counting(args):
            calls["n"] += 1
            return real(args)
        monkeypatch.setattr(caps.get("file_write"), "execute", counting)

        target = "/tmp/persisted_only.txt"
        _, task, plan, step, action, ver = _propose_chain(store, session, target, "abc")
        assert calls["n"] == 0           # propose_action did NOT execute
        assert execution.load_observation(store, action.id) is None
        ok, rej = caps.execute_action(store, action, caps.get("file_write"),
                                      safety_check=make_confirmation_gate(required=False),
                                      plan_id_for_validation=plan.id)
        assert rej is None and calls["n"] == 1   # execution boundary invoked it
        import pathlib
        assert pathlib.Path(target).read_text() == "abc"


# ═══ §34/§35/§47/§48 — verification independence + fabrication closure ═══════

class TestVerificationIndependence:
    def test_observation_success_but_filesystem_says_otherwise(self, store, session, tmp_path):
        """§47 — capability reports success, filesystem disagrees. The
        Observation exists but verification FAILs against the truth."""
        target = tmp_path / "lie.txt"
        content = "expected bytes"
        goal, task, plan, step, action, ver_id = _propose_chain(store, session, target, content)
        # sabotage: capability claims success but writes something else
        real = caps.get("file_write").execute
        def lying(args):
            r = real(args)
            target.write_text("different bytes")   # clobber after "success"
            return r
        import v5.capabilities as capmod
        capmod._REGISTRY["file_write"].execute = lying
        ok, rej = caps.execute_action(store, action, caps.get("file_write"),
                                      safety_check=make_confirmation_gate(required=False),
                                      plan_id_for_validation=plan.id)
        capmod._REGISTRY["file_write"].execute = real
        assert rej is None and ok.value["status"] == "OBSERVED"
        # Observation exists and claims success — verification still FAILS
        r = verification.run_method_for_action(store, ver_id, action.id)
        assert isinstance(r, Ok) and r.value["result"] == VerificationResult.FAIL
        # complete the step's own lifecycle so that verification is the ONLY
        # remaining blocker; then completion is blocked by Law 16
        s = work.load_step(store.read(), step.id)
        s = work.transition_object(store, s.id, StepStatus.READY, s.revision).value
        s = work.transition_object(store, s.id, StepStatus.EXECUTING, s.revision).value
        work.complete_step(store, s.id, s.revision)
        t = work.load_task(store.read(), task.id)
        cr = work.complete_object(store, t.id, t.revision)
        assert isinstance(cr, Rejected) and cr.reason == "VERIFICATION_NOT_PASS"

    def test_missing_file_fails(self, store, session, tmp_path):
        target = tmp_path / "neverwritten.txt"
        _, task, plan, step, action, ver_id = _propose_chain(store, session, target, "data")
        import v5.capabilities as capmod
        real = caps.get("file_write").execute
        def phantom(args):   # claims success, does nothing
            return {"path": target, "bytes_written": 4, "existed": False, "overwrote": False}
        capmod._REGISTRY["file_write"].execute = phantom
        ok, rej = caps.execute_action(store, action, caps.get("file_write"),
                                      safety_check=make_confirmation_gate(required=False),
                                      plan_id_for_validation=plan.id)
        capmod._REGISTRY["file_write"].execute = real
        assert rej is None   # observation exists (the capability's own report)
        r = verification.run_method_for_action(store, ver_id, action.id)
        assert isinstance(r, Ok) and r.value["result"] == VerificationResult.FAIL

    def test_cognition_cannot_manufacture_pass(self, store, session, tmp_path):
        """§30/§48 — there is no proposal field, no adapter route, and the
        repair path refuses, by which PASS can be manufactured. The only
        route is the registered method running against the real fs."""
        target = tmp_path / "truth.txt"
        _, task, plan, step, action, ver_id = _propose_chain(store, session, target, "v")
        from v5 import repair as repair_mod
        auth = repair_mod.authorize("debasish")
        r = repair_mod.repair_object(store, auth, ver_id, 0,
                                     target_state={"result": "PASS"}, reason="attempt fabrication")
        assert isinstance(r, Rejected) and r.reason == "REPAIR_FABRICATION"
        # an adapter payload trying to smuggle a result field is rejected
        raw = json.dumps({"operation": "propose_goal", "statement": "x",
                          "completion_policy": {"rule": "ALL_REQUIRED"},
                          "result": "PASS"})
        assert isinstance(cognition.parse_proposals(raw), Rejected)


# ═══ §7/§10.3/§45 — stale-state discipline ══════════════════════════════════

class TestStaleState:
    def test_stale_revision_rejected_then_reread_then_success(self, store, session):
        """§45 verbatim: propose at revision N; someone else advances to N+1;
        stale proposal rejects; Cognition re-reads authoritative state;
        re-proposes at N+1; succeeds. No local increment arithmetic."""
        g = cognition.propose_goal(session.id, "stale", _policy()).value
        n = g.revision
        # another authorized mutation advances the goal (via a second session)
        sess_b = sessions.create_session(store)
        cognition.authenticate_session(sess_b.id)
        t = cognition.propose_task(g.id, n, "other work", _policy())
        assert isinstance(t, Ok)
        # now the first Cognition context still thinks goal is at N
        cognition.authenticate_session(session.id)
        stale = cognition.propose_task(g.id, n, "my stale proposal", _policy())
        # NOTE: task creation advances the GOAL's revision? No — tasks bump
        # the GOAL-aggregate only when the foundation says so. Our creation
        # doesn't touch goal.revision; the correct authoritative behavior for
        # THIS test is a direct goal transition.
        if isinstance(stale, Ok):
            # force a real revision advance for the stale check
            from v5.enums import GoalStatus
            fresh = work.load_goal(store.read(), g.id)
            work.transition_object(store, g.id, GoalStatus.PAUSED, fresh.revision)
            cognition.authenticate_session(session.id)
        fresh2 = work.load_goal(store.read(), g.id)
        # propose with the ORIGINAL N — now genuinely stale
        stale2 = cognition.propose_task(g.id, n, "stale for real", _policy())
        assert isinstance(stale2, Rejected) and stale2.reason == "STALE_REVISION", stale2
        # authoritative re-read, then re-propose against current
        current = work.load_goal(store.read(), g.id)
        ok = cognition.propose_task(g.id, current.revision, "fresh", _policy())
        assert isinstance(ok, Ok)
        # §45: the implementation never did local revision arithmetic — every
        # successful call went through Work Service with the CURRENT revision

    def test_stale_step_revision_rejected(self, store, session, tmp_path):
        target = tmp_path / "stale_step.txt"
        _, task, plan, step, action, ver = _propose_chain(store, session, target, "x")
        # execute the action (bumps nothing on the step), then bump the STEP
        caps.execute_action(store, action, caps.get("file_write"),
                            safety_check=make_confirmation_gate(required=False),
                            plan_id_for_validation=plan.id)
        from v5.enums import StepStatus as SS
        s = work.load_step(store.read(), step.id)
        s = work.transition_object(store, s.id, SS.READY, s.revision).value
        # now step.revision is current.revision; stale proposal uses step.revision
        r = cognition.propose_action(step.id, step.revision, "file_write",
                                     {"path": str(target), "content": "y"})
        assert isinstance(r, Rejected) and r.reason == "STALE_REVISION"

    def test_stale_task_revision_rejected(self, store, session):
        """§45 — the task-level link in the stale chain (goal/task/step all
        covered): propose_plan with a pre-activation revision rejects."""
        g = cognition.propose_goal(session.id, "stale task", _policy()).value
        t = cognition.propose_task(g.id, g.revision, "tt", _policy()).value
        # another session activates the task -> revision advances
        sess_b = sessions.create_session(store)
        cognition.authenticate_session(sess_b.id)
        work.transition_object(store, t.id, TaskStatus.ACTIVE, t.revision)
        cognition.authenticate_session(session.id)
        steps = [cognition.StepProposal("a", True, [], "file_write")]
        r = cognition.propose_plan(t.id, t.revision, steps)  # t.revision is stale (pre-activation)
        assert isinstance(r, Rejected) and r.reason == "STALE_REVISION"
        # reread authoritative state, re-propose, accepted normally
        cur = work.load_task(store.read(), t.id)
        r2 = cognition.propose_plan(t.id, cur.revision, steps)
        assert isinstance(r2, Ok), r2


# ═══ §26 — duplicates ═══════════════════════════════════════════════════════

class TestDuplicates:
    def test_same_emission_duplicate_suppressed(self, store, session):
        raw = json.dumps({"proposals": [
            {"operation": "propose_goal", "statement": "write foo",
             "completion_policy": {"rule": "ALL_REQUIRED"}},
            {"operation": "propose_goal", "statement": "write foo",
             "completion_policy": {"rule": "ALL_REQUIRED"}},
        ]})
        r = cognition.parse_proposals(raw)
        assert isinstance(r, Ok) and len(r.value) == 1

    def test_cross_emission_repeat_is_legitimate(self, store, session):
        """10:00 'write foo' and 10:05 'write foo' are both real proposals."""
        cognition.authenticate_session(session.id)
        raw = json.dumps({"operation": "propose_goal", "statement": "write foo",
                          "completion_policy": {"rule": "ALL_REQUIRED"}})
        r1 = cognition.dispatch(cognition.parse_proposals(raw).value[0])
        r2 = cognition.dispatch(cognition.parse_proposals(raw).value[0])
        assert isinstance(r1, Ok) and isinstance(r2, Ok)
        assert r1.value.id != r2.value.id   # two distinct Goals, both real


# ═══ §50/§51 — atomicity + no duplicate authority ═══════════════════════════

class TestAtomicity:
    def test_rejected_proposal_leaves_no_state(self, store, session):
        def counts():
            c = store.read()
            return {t: c.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
                    for t in ("goals", "tasks", "plans", "steps", "actions",
                              "verifications", "claims")}
        before = counts()
        r = cognition.propose_task("ghost", 0, "x", _policy())
        assert isinstance(r, Rejected)
        r2 = cognition.propose_plan("ghost", 0, [cognition.StepProposal("a", True, [], "file_write")])
        assert isinstance(r2, Rejected)
        r3 = cognition.propose_action("ghost", 0, "file_write", {"path": "/tmp/x", "content": "y"})
        assert isinstance(r3, Rejected)
        assert counts() == before

    def test_mid_plan_rejection_commits_nothing(self, store, session):
        """§50 — a plan proposal whose SECOND step is invalid must not
        commit the plan, any step, or any verification declaration."""
        t = cognition.propose_goal(session.id, "atomic", _policy()).value
        tk = cognition.propose_task(t.id, t.revision, "t", _policy()).value
        work.transition_object(store, tk.id, TaskStatus.ACTIVE, tk.revision)
        before = {
            kind: store.read().execute(f"SELECT COUNT(*) n FROM {kind}").fetchone()["n"]
            for kind in ("plans", "steps", "verifications", "claims",
                         "required_verifications")
        }
        steps = [
            cognition.StepProposal("good", True, [], "file_write"),
            cognition.StepProposal("bad", True, [], "file_write",
                                   [cognition.VerificationRequirement("phantom_method",
                                                                      "file_write")]),
        ]
        r = cognition.propose_plan(tk.id, work.load_task(store.read(), tk.id).revision, steps)
        assert isinstance(r, Rejected)
        after = {
            kind: store.read().execute(f"SELECT COUNT(*) n FROM {kind}").fetchone()["n"]
            for kind in ("plans", "steps", "verifications", "claims",
                         "required_verifications")
        }
        assert after == before


# ═══ §37/§38 — failure semantics through the membrane ════════════════════════

class TestFailureSemantics:
    def test_execution_failure_no_false_completion(self, store, session, tmp_path):
        """§38 — a definite-failure Action cannot complete the Task; the
        Step lifecycle can retry with a new Action identity (Law 10)."""
        target = tmp_path / "nodir" / "file.txt"
        goal, task, plan, step, action, ver_id = _propose_chain(store, session, target, "data")
        import v5.capabilities as capmod
        real = caps.get("file_write").execute
        def failing(args):
            raise capmod.DefiniteNoEffect("disk full")
        capmod._REGISTRY["file_write"].execute = failing
        ok, rej = caps.execute_action(store, action, caps.get("file_write"),
                                      safety_check=make_confirmation_gate(required=False),
                                      plan_id_for_validation=plan.id)
        capmod._REGISTRY["file_write"].execute = real
        assert rej is None and ok.value["status"] == "FAILED"
        # the historical Action was not rewritten; completion is blocked
        a = execution.load_action(store.read(), action.id)
        assert a.status.value == "FAILED"
        t = work.load_task(store.read(), task.id)
        cr = work.complete_object(store, t.id, t.revision)
        assert isinstance(cr, Rejected)

    def test_unknown_outcome_persists_and_transfers(self, store, session, tmp_path):
        """§37 — UNKNOWN_OUTCOME is neither success nor failure; the
        obligation survives and transfers on completion (Law 9/20)."""
        target = tmp_path / "uncertain.txt"
        goal, task, plan, step, action, ver_id = _propose_chain(store, session, target, "data")
        import v5.capabilities as capmod
        real = caps.get("file_write").execute
        def timeout(args):
            raise TimeoutError("no response")
        capmod._REGISTRY["file_write"].execute = timeout
        ok, rej = caps.execute_action(store, action, caps.get("file_write"),
                                      safety_check=make_confirmation_gate(required=False),
                                      plan_id_for_validation=plan.id)
        capmod._REGISTRY["file_write"].execute = real
        assert rej is None and ok.value["status"] == "UNKNOWN_OUTCOME"
        open_obls = obligations.list_open_obligations(store, owner=task.id)
        assert len(open_obls) == 1
        # verification truth: the file does not exist -> FAIL, no false PASS
        r = verification.run_method_for_action(store, ver_id, action.id)
        assert r.value["result"] == VerificationResult.FAIL


# ═══ fabrication sweep (§48 compact) + structural membrane guarantees ═══════

class TestFabricationSweep:
    def test_no_public_route_writes_pass(self, store, session, tmp_path):
        """Every non-verification route to Verification.result == PASS fails:
        proposal fields (adapter unknown/missing), direct create_verification
        is always PENDING, repair rejects fabrication."""
        target = tmp_path / "fp.txt"
        _, task, plan, step, action, ver_id = _propose_chain(store, session, target, "data")
        # (a) direct verification creation can only produce PENDING
        ev = evidence.create_inference_evidence(store, "t", {"x": 1},
                                                relevance_to=task.id).value
        claim = evidence.create_claim(store, "c", based_on=[ev.id],
                                      made_by="t", confidence=evidence.ClaimConfidence.LOW).value
        v2 = evidence.create_verification(store, claim.id, "verify_file_write",
                                          "deterministic").value
        assert v2.result.value == "PENDING"
        # (b) repair of the result field to PASS rejected
        from v5 import repair as repair_mod
        auth = repair_mod.authorize("debasish")
        r = repair_mod.repair_object(store, auth, v2.id, 0,
                                     target_state={"result": "PASS"}, reason="forge")
        assert isinstance(r, Rejected)

    def test_completed_requires_actual_pass(self, store, session, tmp_path):
        """No false completion when the bound verification was never run."""
        target = tmp_path / "notyet.txt"
        _, task, plan, step, action, ver_id = _propose_chain(store, session, target, "data")
        ok, rej = caps.execute_action(store, action, caps.get("file_write"),
                                      safety_check=make_confirmation_gate(required=False),
                                      plan_id_for_validation=plan.id)
        assert rej is None
        v = evidence.load_verification(store, ver_id)
        assert v.result.value == "PENDING"
        # complete the step lifecycle; its PENDING requirement must still block
        s = work.load_step(store.read(), step.id)
        s = work.transition_object(store, s.id, StepStatus.READY, s.revision).value
        s = work.transition_object(store, s.id, StepStatus.EXECUTING, s.revision).value
        work.complete_step(store, s.id, s.revision)
        t = work.load_task(store.read(), task.id)
        cr = work.complete_object(store, t.id, t.revision)
        assert isinstance(cr, Rejected) and cr.reason == "VERIFICATION_NOT_PASS"
        # then ONLY the real verifier opens the gate
        r = verification.run_method_for_action(store, ver_id, action.id)
        assert r.value["result"] == VerificationResult.PASS
        t = work.load_task(store.read(), task.id)
        cr = work.complete_object(store, t.id, t.revision)
        assert isinstance(cr, Ok)


# ═══ migration v3 + canonical-data regression ═══════════════════════════════

class TestCognitionFoundation:
    def test_v3_migration_on_existing_v2_db(self, tmp_path):
        """A v2 database (steps without the new columns, no sessions table,
        user_version=2) upgrades additively under v3."""
        import sqlite3
        from v5.store import Store
        db = str(tmp_path / "v2.db")
        conn = sqlite3.connect(db)
        old_steps = """CREATE TABLE steps (id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                       plan_id TEXT NOT NULL, status TEXT NOT NULL,
                       required INTEGER NOT NULL, depends_on TEXT NOT NULL DEFAULT '[]');"""
        conn.executescript(old_steps)
        conn.execute("INSERT INTO steps (id, revision, plan_id, status, required) "
                     "VALUES ('step_old', 0, 'p', 'PENDING', 1)")
        conn.execute("PRAGMA user_version = 2")
        conn.commit(); conn.close()

        s = Store(db)
        cols = [r[1] for r in s.read().execute("PRAGMA table_info(steps)").fetchall()]
        assert "description" in cols and "execution_capability" in cols
        assert s.read().execute("SELECT 1 FROM sessions LIMIT 1")  # table now exists
        s.close()

    def test_sessions_table_has_no_principal_column(self, store):
        """§13.2 boundary: principal_id lives ONLY in the threaded context +
        deployment config — never as a column on canonical state. If this
        test ever fails, principal_id leaked into persistent Work/Session
        state, which the contract explicitly forbids."""
        cols = [r[1] for r in store.read().execute("PRAGMA table_info(sessions)").fetchall()]
        assert "principal_id" not in cols
        for tbl in ("goals", "tasks", "plans", "steps", "actions"):
            cols = [r[1] for r in store.read().execute(
                f"PRAGMA table_info({tbl})").fetchall()]
            assert "principal_id" not in cols
        # and the runtime value is the single deployment constant
        s = sessions.create_session(store)
        ctx = cognition.authenticate_session(s.id).value
        assert ctx.principal_id == sessions.OWNER_PRINCIPAL_ID
        s2 = sessions.create_session(store)
        ctx2 = cognition.authenticate_session(s2.id).value
        assert ctx2.principal_id == ctx.principal_id  # one principal, one value


# ═══ frozen-signature guard (Interface Contract §2) ═════════════════════════

class TestFrozenInterface:
    def test_propose_signatures_unchanged(self):
        import inspect
        assert [p for p in inspect.signature(cognition.propose_goal).parameters] == \
            ["session_id", "statement", "completion_policy"]
        assert [p for p in inspect.signature(cognition.propose_task).parameters] == \
            ["goal_id", "expected_goal_revision", "statement", "completion_policy"]
        assert [p for p in inspect.signature(cognition.propose_plan).parameters] == \
            ["task_id", "expected_task_revision", "steps"]
        assert [p for p in inspect.signature(cognition.propose_action).parameters] == \
            ["step_id", "expected_step_revision", "capability", "arguments"]
        # no other public propose_* mutation functions exist
        publics = [n for n in dir(cognition)
                   if n.startswith("propose_") and not n.startswith("propose_goal")
                   and not n.startswith("propose_task")
                   and not n.startswith("propose_plan")
                   and not n.startswith("propose_action")]
        assert publics == []

    def test_cognition_module_never_opens_write_transactions(self):
        """Structural membrane proof: the module contains no direct
        store.write() call and imports no repair machinery — every mutation
        routes through Work Service."""
        import inspect
        src = inspect.getsource(cognition)
        assert ".write()" not in src           # no direct DB writes (§53)
        assert "v5.repair" not in src          # no repair-module import (§39)
        assert "repair_object" not in src
        assert "abandon_unrepairable" not in src

