"""Execution Outcome Observability v1 — regression tests.

Pins the fix for the human-facing observability defect exposed by the real
terminal run ("[debug] execute_action ok" + bare "ERROR STEP_NOT_COMPLETED"
while the actual disposition was UNKNOWN_OUTCOME with an OPEN obligation
caused by OSError: Read-only file system):

  A. successful execution reports the canonical OBSERVED disposition
     (never a generic "ok") and the success rendering is unchanged;
  B. UNKNOWN_OUTCOME reports truthfully at every layer: debug disposition,
     terminal error code preserved + disposition detail + OPEN obligation
     surfaced, no Observation, verification not run, Step/Task not
     completed — Law-9 semantics exactly as before;
  C. FAILED is distinguishable from UNKNOWN_OUTCOME (and from success);
  D. no fabricated verification/Observation ever appears for non-OBSERVED
     dispositions.

All dispositions come from the canonical executor payload — there is no
second execution-status authority anywhere in these tests or the fix.
"""
from __future__ import annotations

import os

import pytest

from v5 import sessions, safety
from v5.models import Ok
from v5.store import Store
from v5.terminal import InteractiveTerminal
from tests.test_live_loop import (FakeCandidate, FakeContent, FakeFunctionCall,
                                  FakePart, FakeResponse)
from tests.test_terminal import (StateAwareProvider, count, make_inputs,
                                 make_terminal)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "state.db"))
    yield s
    s.close()


@pytest.fixture
def session(store):
    return sessions.create_session(store, cognition_authorized=True)


# ── A. successful execution ──────────────────────────────────────────────────

class TestSuccessfulExecutionObservability:
    def test_debug_reports_OBSERVED_and_success_rendering_unchanged(self, store, session, tmp_path):
        target = tmp_path / "ok.txt"
        p = StateAwareProvider(store, "write it", "file_write",
                                {"path": str(target), "content": "hello"},
                                "verify_file_write")
        term, out = make_terminal(store, session, p,
                                  ["/debug", "write it", "y", "/quit"])
        assert term.run() == 0
        debug_lines = [o for o in out if o.startswith("[debug]")]
        # the canonical successful disposition, not a generic "ok"
        assert any("execute_action OBSERVED" in o for o in debug_lines), debug_lines
        assert not any("execute_action ok" in o for o in debug_lines)
        # full truthful happy path in debug
        assert any("record_runtime_evidence" in o for o in debug_lines)
        assert any("verify_file_write" in o for o in debug_lines)
        assert any("complete_step COMPLETED" in o for o in debug_lines)
        # success rendering unchanged
        assert "OK: Task COMPLETED\n" in out
        assert target.read_text() == "hello"


# ── B + D. UNKNOWN_OUTCOME: truthful at every layer, nothing fabricated ─────

class TestUnknownOutcomeObservability:
    @pytest.fixture
    def unwritable_dir(self, tmp_path):
        ro = tmp_path / "read_only_dir"
        ro.mkdir()
        os.chmod(str(ro), 0o555)          # deterministic: non-root cannot write
        yield ro
        os.chmod(str(ro), 0o755)          # restore so tmp cleanup works

    def test_unknown_outcome_truthful_debug_and_terminal(
            self, store, session, tmp_path, unwritable_dir):
        target = unwritable_dir / "helloworld.txt"
        p = StateAwareProvider(store, "create helloworld.txt", "file_write",
                                {"path": str(target), "content": ""},
                                "verify_file_write")
        term, out = make_terminal(store, session, p,
                                  ["/debug", "create it", "y", "/quit"])
        assert term.run() == 0
        joined = "".join(out)
        debug_lines = [o for o in out if o.startswith("[debug]")]

        # DEBUG TRUTHFULNESS — the exact defect: disposition, never "ok"
        assert any("execute_action UNKNOWN_OUTCOME" in o for o in debug_lines)
        assert not any("execute_action ok" in o for o in debug_lines)
        assert any("Read-only file system" in o or "Permission denied" in o
                   for o in debug_lines), debug_lines

        # TERMINAL: code preserved + disposition detail + OPEN obligation
        error_lines = [o for o in out if o.startswith("ERROR STEP_NOT_COMPLETED:")]
        assert len(error_lines) == 1
        err = error_lines[0]
        assert "ended in UNKNOWN_OUTCOME not COMPLETED" in err
        assert "OSError" in err or "PermissionError" in err
        assert "obl_" in err and "is OPEN for resolution" in err

        # canonical state: Law-9 semantics exactly as before
        arow = store.read().execute("SELECT * FROM actions").fetchone()
        assert arow["status"] == "UNKNOWN_OUTCOME"          # NOT FAILED
        obl = store.read().execute(
            "SELECT * FROM obligations WHERE disposition = 'OPEN'").fetchone()
        assert obl is not None and obl["origin_action_id"] == arow["id"]
        assert "OSError" in obl["unknown_reason"] or "PermissionError" in obl["unknown_reason"]
        srow = store.read().execute("SELECT * FROM steps").fetchone()
        assert srow["status"] == "PENDING"                  # never completed
        trow = store.read().execute("SELECT * FROM tasks").fetchone()
        assert trow["status"] != "COMPLETED"
        assert not target.exists()                          # nothing was written

        # NO FAKE VERIFICATION / OBSERVATION (D)
        assert count(store, "observations") == 0
        ver = store.read().execute(
            "SELECT * FROM verifications WHERE step_id IS NOT NULL").fetchone()
        assert ver["result"] == "PENDING"                   # never run
        assert not any("stage=verify" in o for o in debug_lines)
        assert "PASS" not in joined
        assert "OK: Task" not in joined

    def test_unknown_outcome_detail_survives_run_slice_result(self, store, session, tmp_path,
                                                              unwritable_dir):
        """Direct live-loop result: reason AND detail (disposition + reason +
        obligation id) — the fields the terminal renders from."""
        from v5 import live_loop

        class Provider:
            model_name = "scripted"
            def __init__(self, store):
                self.store = store
                self.n = 0
            def call(self, contents, tools, allowed_names):
                self.n += 1
                c = self.store.read()
                if allowed_names[0] == "propose_goal":
                    args = {"statement": "create it",
                            "completion_policy": {"rule": "ALL_REQUIRED"}}
                elif allowed_names[0] == "propose_task":
                    g = c.execute("SELECT * FROM goals ORDER BY rowid DESC LIMIT 1").fetchone()
                    args = {"goal_id": g["id"], "expected_goal_revision": g["revision"],
                            "statement": "t", "completion_policy": {"rule": "ALL_REQUIRED"}}
                elif allowed_names[0] == "propose_plan":
                    t = c.execute("SELECT * FROM tasks ORDER BY rowid DESC LIMIT 1").fetchone()
                    args = {"task_id": t["id"], "expected_task_revision": t["revision"],
                            "steps": [{"description": "create", "required": True,
                                       "depends_on_index": [],
                                       "execution_capability": "file_write",
                                       "verification_requirements": [
                                           {"method_name": "verify_file_write",
                                            "applies_to_capability": "file_write"}]}]}
                else:
                    s = c.execute("SELECT * FROM steps ORDER BY rowid DESC LIMIT 1").fetchone()
                    args = {"step_id": s["id"], "expected_step_revision": s["revision"],
                            "capability": "file_write",
                            "arguments": {"path": str(unwritable_dir / "x.txt"),
                                          "content": ""}}
                return FakeResponse(candidates=[FakeCandidate(content=FakeContent(
                    [FakePart(function_call=FakeFunctionCall(allowed_names[0], args))]))])

        def grant(store_, action):
            staged = safety.create_confirmation(store_, action.id, action.revision,
                                                action.capability, action.arguments)
            safety.confirm(store_, staged.value)
            return Ok({"confirmation_id": staged.value})

        target = unwritable_dir / "x.txt"
        r = live_loop.run_live_slice(store, session.id, "create it", "", "",
                                     Provider(store), confirmation_policy=grant)
        assert r["ok"] is False
        assert r["reason"] == "STEP_NOT_COMPLETED"
        assert "ended in UNKNOWN_OUTCOME not COMPLETED" in r["detail"]
        assert ("Read-only file system" in r["detail"]
                or "Permission denied" in r["detail"])
        assert "obl_" in r["detail"] and "is OPEN for resolution" in r["detail"]
        # the debug event itself carries the canonical disposition
        ev = [e for e in r["events"]
              if e["kind"] == "host_step" and e["detail"].get("op") == "execute_action"][0]
        assert ev["detail"]["status"] == "UNKNOWN_OUTCOME"
        assert "obl_" in ev["detail"].get("reason", "") or \
               "Read-only" in ev["detail"].get("reason", "") or \
               "Permission denied" in ev["detail"].get("reason", "")


# ── C. definite failure is distinguishable ───────────────────────────────────

class TestDefiniteFailureObservability:
    def test_FAILED_distinguishable_from_UNKNOWN_OUTCOME(self, store, session, tmp_path):
        ghost = tmp_path / "ghost.txt"        # never exists -> DefiniteNoEffect
        p = StateAwareProvider(store, "delete ghost", "file_delete",
                               {"path": str(ghost)}, "verify_file_delete")
        term, out = make_terminal(store, session, p, ["/debug", "delete it", "y", "/quit"])
        assert term.run() == 0
        debug_lines = [o for o in out if o.startswith("[debug]")]
        assert any("execute_action FAILED" in o for o in debug_lines)
        assert not any("execute_action ok" in o for o in debug_lines)
        err = [o for o in out if o.startswith("ERROR STEP_NOT_COMPLETED:")][0]
        assert "ended in FAILED not COMPLETED" in err
        assert "file does not exist" in err          # the capability's reason
        # FAILED creates NO obligation — that's the UNKNOWN_OUTCOME-only marker
        assert count(store, "obligations") == 0
        arow = store.read().execute("SELECT * FROM actions").fetchone()
        assert arow["status"] == "FAILED"
        assert "UNKNOWN_OUTCOME" not in err          # never conflated
        assert "OK: Task" not in "".join(out)


# ── success/failure never conflated at the rendering layer ───────────────────

class TestRenderingContract:
    def test_success_and_failure_prefixes_stay_distinct(self, store, session, tmp_path):
        ok_target = tmp_path / "good.txt"
        good = StateAwareProvider(store, "write good", "file_write",
                                  {"path": str(ok_target), "content": "x"},
                                  "verify_file_write")
        term, out = make_terminal(store, session, good, ["do it", "y", "/quit"])
        assert term.run() == 0
        assert "OK: Task COMPLETED\n" in out
        assert not any(o.startswith("ERROR") for o in out)
