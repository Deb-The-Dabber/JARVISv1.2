"""Adversarial regression tests — 2026-09 hardening pass.

Each test maps to a concrete defect closed in the hardening pass:
  Fix 1: confirmation binding must check Action revision (both lookup paths)
  Fix 2: repair must not fabricate PASS verifications (PENDING/RUNNING/FAIL -> PASS)
  Fix 3: obligation resolution requires provenance to origin_action_id
  Fix 4: abandon_unrepairable revision CAS (stale rejected) — concurrent race
         additionally covered in test_repair.py::test_abandon_unrepairable_stale_revision_rejected
  Fix 5: obligation owner validation (UOR or existing Task) — no orphan rows
  Fix 6: runtime evidence persists origin_observation_id (schema migration included)

Plus new defects found by the semantic audit during the pass:
  Audit A: _REPAIRABLE_FIELDS granted integrity on tables with no integrity column
  Audit B: freeze_object crashed on plan/step (no integrity column)
  Audit C: abandon_unrepairable crashed on non-integrity-bearing objects
  Audit D: step.depends_on repair could diverge from the committed plan graph
  Audit E: plan.dependency_graph repair skipped the Law 13 cycle check
"""
from __future__ import annotations

import sqlite3

import pytest

from tests.conftest import activated, make_goal_task_plan_step, pending_action
from v5 import evidence, execution, obligations, repair as repair_mod, store as store_mod, work
from v5.enums import ClaimConfidence, IdempotencyClass, ObligationDisposition
from v5.ids import UOR
from v5.models import Ok, Rejected
from v5.safety import confirm, create_confirmation, make_confirmation_gate

def _fresh_plan(store, plan):
    return work.load_plan(store.read(), plan.id)


@pytest.fixture
def store(tmp_path):
    s = store_mod.Store(str(tmp_path / "state.db"))
    yield s
    s.close()


# ── Fix 1: confirmation revision binding (Law 32) ────────────────────────────

class TestConfirmationRevisionBinding:
    def _staged_and_confirmed(self, store, action, action_revision):
        cfm = create_confirmation(store, action.id, action_revision,
                                  action.capability, action.arguments).value
        confirm(store, cfm)
        return cfm

    def test_rev_n_confirmation_works_at_rev_n(self, store):
        """Control: a confirmation staged at the action's CURRENT revision is accepted."""
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        action = pending_action(store, step, capability="file_write",
                                args={"path": "/tmp/x.txt", "content": "y"})
        self._staged_and_confirmed(store, action, action.revision)
        gate = make_confirmation_gate(required=True)
        with store.read() as conn:
            assert gate(conn, execution.load_action(conn, action.id)) is None, \
                "current-revision confirmation must pass the gate"

    def test_rev_n_confirmation_rejected_after_revision_advance_general_path(self, store):
        """General lookup: confirmation at rev 0, action legitimately advances to
        rev 1 (repair changes a repairable field) -> stale, rejected."""
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        action = pending_action(store, step, capability="file_write",
                                args={"path": "/tmp/x.txt", "content": "y"})
        self._staged_and_confirmed(store, action, 0)
        # legitimate revision advance on the action (repairable field change)
        auth = repair_mod.authorize("debasish")
        r = repair_mod.repair_object(store, auth, action.id, 0,
                                     target_state={"confirmation_id": None}, reason="stage clear")
        # even if the field repair is a no-op rejection, force the advance another way:
        reloaded = execution.load_action(store.read(), action.id)
        if reloaded.revision == 0:
            # repair changed nothing (None filtered) — use status repair instead
            r = repair_mod.repair_object(store, auth, action.id, 0,
                                         target_state={"status": "PENDING"}, reason="touch")
        reloaded = execution.load_action(store.read(), action.id)
        assert reloaded.revision == 1
        gate = make_confirmation_gate(required=True)
        with store.read() as conn:
            verdict = gate(conn, reloaded)
        assert isinstance(verdict, Rejected) and verdict.reason == "CONFIRMATION_REQUIRED"
        assert "revision" in verdict.detail

    def test_rev_n_confirmation_rejected_after_revision_advance_specific_path(self, store):
        """Action-specific lookup (action.confirmation_id set): same revision rule."""
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        # action created WITH the confirmation binding (specific path)
        action = pending_action(store, step, capability="file_write",
                                args={"path": "/tmp/x.txt", "content": "y"})
        cfm = self._staged_and_confirmed(store, action, 0)
        auth = repair_mod.authorize("debasish")
        r = repair_mod.repair_object(store, auth, action.id, 0,
                                     target_state={"confirmation_id": cfm}, reason="bind confirmation")
        assert isinstance(r, Ok), r
        reloaded = execution.load_action(store.read(), action.id)
        assert reloaded.revision == 1 and reloaded.confirmation_id == cfm
        gate = make_confirmation_gate(required=True)
        with store.read() as conn:
            verdict = gate(conn, reloaded)
        # the bound confirmation was staged at revision 0; action is now at 1
        assert isinstance(verdict, Rejected) and verdict.reason == "CONFIRMATION_REQUIRED"

    def test_new_confirmation_at_new_revision_still_works(self, store):
        """After a material change, a FRESH confirmation at the new revision restores the gate."""
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        action = pending_action(store, step, capability="file_write",
                                args={"path": "/tmp/x.txt", "content": "y"})
        self._staged_and_confirmed(store, action, 0)
        auth = repair_mod.authorize("debasish")
        repair_mod.repair_object(store, auth, action.id, 0,
                                 target_state={"status": "PENDING"}, reason="touch")
        reloaded = execution.load_action(store.read(), action.id)
        assert reloaded.revision == 1
        self._staged_and_confirmed(store, reloaded, 1)  # restage at current revision
        gate = make_confirmation_gate(required=True)
        with store.read() as conn:
            assert gate(conn, execution.load_action(conn, action.id)) is None


# ── Fix 2: repair cannot fabricate PASS verifications (Law 29/30) ─────────────

class TestVerificationRepairFabrication:
    def _make_claim_and_ver(self, store, task):
        ev = evidence.create_inference_evidence(store, "t", {"g": 1},
                                                relevance_to=task.id).value
        claim = evidence.create_claim(store, "c", based_on=[ev.id], made_by="t",
                                      confidence=ClaimConfidence.LOW).value
        ver = evidence.create_verification(store, claim.id, "m", "deterministic").value
        return ver

    def test_pending_to_pass_rejected(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        ver = self._make_claim_and_ver(store, task)  # never run -> PENDING
        auth = repair_mod.authorize("debasish")
        r = repair_mod.repair_object(store, auth, ver.id, 0,
                                     target_state={"result": "PASS"}, reason="claim it passed")
        assert isinstance(r, Rejected) and r.reason == "REPAIR_FABRICATION"

    def test_running_to_pass_rejected(self, store):
        """RUNNING = evaluation started, outcome never persisted (crash #6).
        Repair must not turn that into PASS."""
        goal, task, plan, step = make_goal_task_plan_step(store)
        ver = self._make_claim_and_ver(store, task)
        with store.write() as conn:  # test setup: leave the verification mid-run
            conn.execute("UPDATE verifications SET result = 'RUNNING' WHERE id = ?", (ver.id,))
        auth = repair_mod.authorize("debasish")
        r = repair_mod.repair_object(store, auth, ver.id, 0,
                                     target_state={"result": "PASS"}, reason="it probably finished")
        assert isinstance(r, Rejected) and r.reason == "REPAIR_FABRICATION"

    def test_fail_to_pass_rejected(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        ver = self._make_claim_and_ver(store, task)
        evidence.run_verification(store, ver.id, lambda c, e: False)  # persisted FAIL
        auth = repair_mod.authorize("debasish")
        r = repair_mod.repair_object(store, auth, ver.id, 0,
                                     target_state={"result": "PASS"}, reason="human override")
        assert isinstance(r, Rejected) and r.reason == "REPAIR_FABRICATION"

    def test_inconclusive_to_pass_rejected(self, store):
        from v5.enums import VerificationResult
        from v5.models import R_FABRICATION
        goal, task, plan, step = make_goal_task_plan_step(store)
        ver = self._make_claim_and_ver(store, task)
        with store.write() as conn:
            conn.execute("UPDATE verifications SET result = 'INCONCLUSIVE' WHERE id = ?", (ver.id,))
        auth = repair_mod.authorize("debasish")
        r = repair_mod.repair_object(store, auth, ver.id, 0,
                                     target_state={"result": "PASS"}, reason="maybe pass")
        assert isinstance(r, Rejected) and r.reason == R_FABRICATION

    def test_normal_execution_still_establishes_pass(self, store):
        """Control: repair is never the way to get PASS; run_verification still is."""
        goal, task, plan, step = make_goal_task_plan_step(store)
        ver = self._make_claim_and_ver(store, task)
        r = evidence.run_verification(store, ver.id, lambda c, e: True)
        assert isinstance(r, Ok), r
        reloaded = evidence.load_verification(store, ver.id)
        assert reloaded.result.value == "PASS"

    def test_pass_to_pass_is_a_noop_not_fabrication(self, store):
        """PASS -> PASS carries no fabrication (already-passed stays passed)."""
        goal, task, plan, step = make_goal_task_plan_step(store)
        ver = self._make_claim_and_ver(store, task)
        evidence.run_verification(store, ver.id, lambda c, e: True)
        auth = repair_mod.authorize("debasish")
        r = repair_mod.repair_object(store, auth, ver.id, 0,
                                     target_state={"result": "PASS"}, reason="no-op confirmation")
        assert isinstance(r, Ok), r


# ── Fix 3: obligation resolution provenance (Laws 23/25/29) ──────────────────

class TestObligationResolutionProvenance:
    def _executed_chain(self, store, step, capability, args):
        """Action -> begin_executing -> mark_observed -> runtime Evidence ->
        Claim -> Verification run to PASS. Returns (action_id, verification_id)."""
        action = pending_action(store, step, capability=capability, args=args)
        r = execution.begin_executing(store, action.id, action.revision,
                                      safety_check=make_confirmation_gate(required=False))
        assert isinstance(r, Ok), r
        r2 = execution.mark_observed(store, action.id, r.value.revision if hasattr(r.value, "revision") else action.revision + 1,
                                     raw_result={"ok": True}, execution_source="test")
        if isinstance(r2, Rejected):
            # revision bookkeeping: fetch current
            cur = execution.load_action(store.read(), action.id)
            r2 = execution.mark_observed(store, action.id, cur.revision,
                                         raw_result={"ok": True}, execution_source="test")
        assert isinstance(r2, Ok), r2
        obs_id = r2.value["observation_id"] if isinstance(r2.value, dict) else None
        if obs_id is None:
            obs_id = execution.load_observation(store, action.id).id
        ev = evidence.record_runtime_evidence(store, obs_id, source=capability,
                                              relevance_to=step.plan_id, content={"ok": True}).value
        claim = evidence.create_claim(store, f"{capability} succeeded", based_on=[ev.id],
                                      made_by="test", confidence=ClaimConfidence.HIGH).value
        ver = evidence.create_verification(store, claim.id, "check", "deterministic").value
        evidence.run_verification(store, ver.id, lambda c, e: True)
        return action.id, ver.id

    def test_owning_chain_verification_resolves(self, store):
        """Verification whose evidence derives from the obligation's origin Action
        resolves the obligation."""
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        action_id, ver_id = self._executed_chain(store, step, "file_write",
                                                 {"path": "/tmp/a.txt", "content": "x"})
        obl = obligations.create_obligation(store, action_id, task.id, "unknown", "effect").value
        r = obligations.resolve_obligation(store, obl.id, ver_id, expected_revision=obl.revision)
        assert isinstance(r, Ok), r
        reloaded = obligations.load_obligation(store.read(), obl.id)
        assert reloaded.disposition == ObligationDisposition.RESOLVED

    def test_unrelated_action_verification_rejected(self, store):
        """A PASS verification about Action B must NOT resolve Action A's obligation."""
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        action_a, _ver_a = self._executed_chain(store, step, "file_write",
                                                {"path": "/tmp/a.txt", "content": "x"})
        _action_b, ver_b = self._executed_chain(store, step, "file_write",
                                                {"path": "/tmp/b.txt", "content": "y"})
        obl = obligations.create_obligation(store, action_a, task.id, "unknown", "effect").value
        r = obligations.resolve_obligation(store, obl.id, ver_b, expected_revision=obl.revision)
        assert isinstance(r, Rejected) and r.reason == "RESOLUTION_PROVENANCE"
        reloaded = obligations.load_obligation(store.read(), obl.id)
        assert reloaded.disposition == ObligationDisposition.OPEN  # not resolved

    def test_inference_only_verification_cannot_resolve(self, store):
        """Evidence with no Observation provenance (INFERENCE-typed) cannot
        establish the origin-action link, even when the claim happens to be true."""
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        action = pending_action(store, step)
        obl = obligations.create_obligation(store, action.id, task.id, "unknown", "effect").value
        inf = evidence.create_inference_evidence(store, "llm", {"belief": 1},
                                                 relevance_to=task.id).value
        claim = evidence.create_claim(store, "it probably worked", based_on=[inf.id],
                                      made_by="llm", confidence=ClaimConfidence.LOW).value
        ver = evidence.create_verification(store, claim.id, "vibe", "llm_evaluation").value
        evidence.run_verification(store, ver.id, lambda c, e: True)
        r = obligations.resolve_obligation(store, obl.id, ver.id, expected_revision=obl.revision)
        assert isinstance(r, Rejected) and r.reason == "RESOLUTION_PROVENANCE"


# ── Fix 5: obligation owner validation (Laws 17/18) ──────────────────────────

class TestObligationOwnerValidation:
    def test_arbitrary_string_owner_rejected_no_orphan(self, store):
        r = obligations.create_obligation(store, "act_x", "totally_made_up",
                                          "unknown", "effect")
        assert isinstance(r, Rejected)
        n = store.read().execute("SELECT COUNT(*) AS n FROM obligations").fetchone()["n"]
        assert n == 0, "rejected creation must not leave an orphan row"

    def test_ghost_task_id_owner_rejected(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        r = obligations.create_obligation(store, "act_x", "task_nonexistent_12345",
                                          "unknown", "effect")
        assert isinstance(r, Rejected)

    def test_uor_owner_accepted(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        r = obligations.create_obligation(store, "act_x", UOR, "unknown", "effect")
        assert isinstance(r, Ok), r
        assert r.value.owner == UOR

    def test_real_task_owner_accepted(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        r = obligations.create_obligation(store, "act_x", task.id, "unknown", "effect")
        assert isinstance(r, Ok), r
        assert r.value.owner == task.id

    def test_transfer_to_ghost_owner_rejected(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        obl = obligations.create_obligation(store, "act_x", task.id, "unknown", "effect").value
        r = obligations.transfer_obligation(store, obl.id, "not_a_task", expected_owner=task.id)
        assert isinstance(r, Rejected)
        reloaded = obligations.load_obligation(store.read(), obl.id)
        assert reloaded.owner == task.id  # unchanged


# ── Fix 6: runtime evidence provenance + schema migration ────────────────────

class TestRuntimeEvidenceProvenance:
    def test_runtime_evidence_persists_origin_observation(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        task = activated(store, task, plan)
        action = pending_action(store, step)
        r = execution.begin_executing(store, action.id, action.revision,
                                      safety_check=make_confirmation_gate(required=False))
        assert isinstance(r, Ok), r
        cur = execution.load_action(store.read(), action.id)
        r2 = execution.mark_observed(store, action.id, cur.revision,
                                     raw_result={"ok": True}, execution_source="test")
        assert isinstance(r2, Ok), r2
        obs = execution.load_observation(store, action.id)
        ev = evidence.record_runtime_evidence(store, obs.id, source=action.capability,
                                              relevance_to=step.plan_id, content={"ok": True}).value
        reloaded = evidence.load_evidence(store, ev.id)
        assert reloaded.origin_observation_id == obs.id

    def test_schema_migration_adds_column_and_preserves_rows(self, tmp_path):
        """A legacy v1 database (evidence table without origin_observation_id)
        is upgraded additively: column added, old rows preserved."""
        db = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(db)
        conn.executescript("""
        CREATE TABLE evidence (id TEXT PRIMARY KEY, status TEXT NOT NULL,
                               acquisition_method TEXT NOT NULL, source TEXT NOT NULL,
                               relevance_to TEXT NOT NULL, timestamp TEXT NOT NULL,
                               content TEXT NOT NULL);
        INSERT INTO evidence VALUES ('ev_old', 'COLLECTED', 'manual', 's', 'r', 't', '{}');
        """)
        conn.commit()
        conn.close()

        s = store_mod.Store(db)  # init must migrate
        try:
            cols = [r[1] for r in s.read().execute("PRAGMA table_info(evidence)").fetchall()]
            assert "origin_observation_id" in cols
            assert s.read().execute("PRAGMA user_version").fetchone()[0] == s.SCHEMA_VERSION
            row = s.read().execute("SELECT * FROM evidence WHERE id = 'ev_old'").fetchone()
            assert row is not None and row["status"] == "COLLECTED"  # rows preserved
        finally:
            s.close()

    def test_migration_is_idempotent(self, tmp_path):
        """Reopening an up-to-date store runs no migration and stays correct."""
        db = str(tmp_path / "fresh.db")
        s1 = store_mod.Store(db)
        s1.close()
        s2 = store_mod.Store(db)  # second open: user_version already SCHEME_VERSION
        try:
            assert s2.read().execute("PRAGMA user_version").fetchone()[0] == s2.SCHEMA_VERSION
        finally:
            s2.close()


# ── Audit A: repairable-fields integrity is schema-truthful ──────────────────

class TestRepairSchemaTruthful:
    def test_integrity_repair_on_plan_rejected_not_crash(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        auth = repair_mod.authorize("debasish")
        r = repair_mod.repair_object(store, auth, plan.id, _fresh_plan(store, plan).revision,
                                     target_state={"integrity": "FROZEN"}, reason="freeze plan")
        assert isinstance(r, Rejected) and r.reason == "REPAIR_FIELD_FORBIDDEN"

    def test_integrity_repair_on_step_rejected_not_crash(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        auth = repair_mod.authorize("debasish")
        r = repair_mod.repair_object(store, auth, step.id, step.revision,
                                     target_state={"integrity": "OK"}, reason="unfreeze step")
        assert isinstance(r, Rejected) and r.reason == "REPAIR_FIELD_FORBIDDEN"

    def test_integrity_repair_on_verification_rejected_not_crash(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        ev = evidence.create_inference_evidence(store, "t", {"g": 1}, relevance_to=task.id).value
        claim = evidence.create_claim(store, "c", based_on=[ev.id], made_by="t",
                                      confidence=ClaimConfidence.LOW).value
        ver = evidence.create_verification(store, claim.id, "m", "deterministic").value
        auth = repair_mod.authorize("debasish")
        r = repair_mod.repair_object(store, auth, ver.id, 0,
                                     target_state={"integrity": "FROZEN"}, reason="freeze verification")
        assert isinstance(r, Rejected) and r.reason == "REPAIR_FIELD_FORBIDDEN"

    def test_integrity_repair_on_obligation_rejected_not_crash(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        obl = obligations.create_obligation(store, "act_x", task.id, "u", "e").value
        auth = repair_mod.authorize("debasish")
        r = repair_mod.repair_object(store, auth, obl.id, obl.revision,
                                     target_state={"integrity": "FROZEN"}, reason="freeze obligation")
        assert isinstance(r, Rejected) and r.reason == "REPAIR_FIELD_FORBIDDEN"


# ── Audit B: freeze_object only for integrity-bearing types ──────────────────

class TestFreezeObjectTypeGate:
    def test_freeze_plan_rejected_not_sql_crash(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        r = work.freeze_object(store, plan.id, "suspect plan")
        assert isinstance(r, Rejected) and r.reason == "INVALID_TRANSITION"

    def test_freeze_step_rejected_not_sql_crash(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        r = work.freeze_object(store, step.id, "suspect step")
        assert isinstance(r, Rejected) and r.reason == "INVALID_TRANSITION"

    def test_freeze_task_works_and_is_idempotent(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        r1 = work.freeze_object(store, task.id, "mismatch")
        assert isinstance(r1, Ok), r1
        r2 = work.freeze_object(store, task.id, "again")
        assert isinstance(r2, Ok) and r2.value.get("idempotent") is True


# ── Audit C: abandon_unrepairable only for integrity-bearing types ───────────

class TestAbandonTypeGate:
    def test_abandon_plan_rejected_not_sql_crash(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        auth = repair_mod.authorize("debasish")
        r = repair_mod.abandon_unrepairable(store, auth, plan.id, "cannot fix",
                                            expected_revision=_fresh_plan(store, plan).revision)
        assert isinstance(r, Rejected) and r.reason == "INVALID_TRANSITION"

    def test_abandon_obligation_rejected_not_sql_crash(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        obl = obligations.create_obligation(store, "act_x", task.id, "u", "e").value
        auth = repair_mod.authorize("debasish")
        r = repair_mod.abandon_unrepairable(store, auth, obl.id, "cannot fix",
                                            expected_revision=obl.revision)
        assert isinstance(r, Rejected) and r.reason == "INVALID_TRANSITION"


# ── Audit D/E: dependency graph repair discipline (Law 13) ───────────────────

class TestDependencyGraphRepairDiscipline:
    def test_step_depends_on_repair_forbidden(self, store):
        """step.depends_on is a denormalized copy of the plan's committed graph —
        repairing it independently would diverge the two representations."""
        goal, task, plan, step = make_goal_task_plan_step(store)
        auth = repair_mod.authorize("debasish")
        r = repair_mod.repair_object(store, auth, step.id, step.revision,
                                     target_state={"depends_on": ["whatever"]}, reason="rewire")
        assert isinstance(r, Rejected) and r.reason == "REPAIR_FIELD_FORBIDDEN"

    def test_cyclic_dependency_graph_repair_rejected(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        s2 = work.create_step(store, plan.id, required=True).value
        auth = repair_mod.authorize("debasish")
        r = repair_mod.repair_object(
            store, auth, plan.id, _fresh_plan(store, plan).revision,
            target_state={"dependency_graph": {step.id: [s2.id], s2.id: [step.id]}},
            reason="human correction",
        )
        assert isinstance(r, Rejected) and r.reason == "AGGREGATE_INVARIANT_VIOLATION"

    def test_acyclic_dependency_graph_repair_allowed(self, store):
        goal, task, plan, step = make_goal_task_plan_step(store)
        s2 = work.create_step(store, plan.id, required=True).value
        auth = repair_mod.authorize("debasish")
        r = repair_mod.repair_object(
            store, auth, plan.id, _fresh_plan(store, plan).revision,
            target_state={"dependency_graph": {s2.id: [step.id]}},
            reason="human correction",
        )
        assert isinstance(r, Ok), r
        reloaded = work.load_plan(store.read(), plan.id)
        assert reloaded.dependency_graph.get(s2.id) == [step.id] or \
               reloaded.dependency_graph.get(s2.id) == {step.id}
