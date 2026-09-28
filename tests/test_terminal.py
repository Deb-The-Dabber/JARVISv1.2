"""Interactive terminal adapter tests (§21 of the terminal contract).

Covers: basic interaction, the exact input reader semantics, the multiline
protocol, EOF behavior in all three contexts, exit statuses (both run()
return values and real process return codes), error rendering, debug mode,
and the full confirmation pause/resume lifecycle (approval, denial, EOF).

The live loop is driven by a scripted state-aware provider (offline, per the
repository's existing test pattern); the real-provider interactive smoke is
performed separately and reported in the milestone report. Canonical state
assertions query the real store — plausible terminal text is never accepted
as proof.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

from v5 import sessions, verification, work
from v5 import capabilities as caps
from v5.store import Store, jdump
from v5.terminal import InteractiveTerminal, TERMINAL_GUIDANCE
from tests.test_live_loop import (FakeCandidate, FakeContent, FakeFunctionCall,
                                  FakePart, FakeResponse)


# ── fixtures & helpers ────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "state.db"))
    yield s
    s.close()


@pytest.fixture
def session(store):
    return sessions.create_session(store, cognition_authorized=True)


def make_inputs(lines):
    """Sequential one-line input source; EOF when exhausted (each call
    consumes exactly one line — no read-ahead)."""
    it = iter(lines)

    def fn():
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    return fn


class StateAwareProvider:
    """Scripted provider deriving each proposal from the loop's own stage
    signal (allowed_names[0]) plus the LATEST canonical state, so it works
    across multiple turns. Records the last prompt contents so tests can
    assert exactly which instruction text reached the live loop."""

    model_name = "scripted-terminal-state"

    def __init__(self, store, statement, capability, action_args, verifier):
        self.store = store
        self.statement = statement
        self.capability = capability
        self.action_args = action_args
        self.verifier = verifier
        self.last_contents = None
        self.calls = 0

    def call(self, contents, tools, allowed_names):
        self.calls += 1
        self.last_contents = contents
        op = allowed_names[0]
        c = self.store.read()
        if op == "propose_goal":
            args = {"statement": self.statement,
                    "completion_policy": {"rule": "ALL_REQUIRED"}}
        elif op == "propose_task":
            g = c.execute("SELECT * FROM goals ORDER BY rowid DESC LIMIT 1").fetchone()
            args = {"goal_id": g["id"], "expected_goal_revision": g["revision"],
                    "statement": self.statement,
                    "completion_policy": {"rule": "ALL_REQUIRED"}}
        elif op == "propose_plan":
            t = c.execute("SELECT * FROM tasks ORDER BY rowid DESC LIMIT 1").fetchone()
            args = {"task_id": t["id"], "expected_task_revision": t["revision"],
                    "steps": [{"description": self.statement, "required": True,
                               "depends_on_index": [],
                               "execution_capability": self.capability,
                               "verification_requirements": [
                                   {"method_name": self.verifier,
                                    "applies_to_capability": self.capability}]}]}
        else:
            s = c.execute("SELECT * FROM steps ORDER BY rowid DESC LIMIT 1").fetchone()
            args = {"step_id": s["id"], "expected_step_revision": s["revision"],
                    "capability": self.capability,
                    "arguments": dict(self.action_args)}
        return FakeResponse(candidates=[FakeCandidate(content=FakeContent(
            [FakePart(function_call=FakeFunctionCall(op, args))]))])


def make_terminal(store, session, provider, lines, debug=False):
    out = []
    term = InteractiveTerminal(
        store, session, lambda: provider,
        input_fn=make_inputs(lines), output_fn=out.append, debug=debug)
    return term, out


def instruction_reached(provider) -> str:
    """The exact instruction text the live loop received (from the prompt)."""
    text = provider.last_contents[0]["parts"][0]["text"]
    marker = "Instruction: "
    return text[text.index(marker) + len(marker):]


def count(store, table):
    return store.read().execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]


def write_provider(store, tmp_path, name="w.txt", content="hello"):
    target = tmp_path / name
    return StateAwareProvider(store, f"write {name}", "file_write",
                              {"path": str(target), "content": content},
                              "verify_file_write"), target


def delete_provider(store, tmp_path, name="victim.txt"):
    target = tmp_path / name
    target.write_text("delete me")
    return StateAwareProvider(store, f"delete {name}", "file_delete",
                              {"path": str(target)}, "verify_file_delete"), target


def read_provider(store, tmp_path, name="r.txt", content="read me"):
    target = tmp_path / name
    target.write_text(content)
    return StateAwareProvider(store, f"read {name}", "file_read",
                              {"path": str(target)}, "verify_file_read"), target


# ── basic interaction ────────────────────────────────────────────────────────

class TestBasics:
    def test_help_renders_and_loop_not_invoked(self, store, session, tmp_path):
        p, _ = write_provider(store, tmp_path)
        term, out = make_terminal(store, session, p, ["/help", "/quit"])
        assert term.run() == 0
        assert any("Multiline input:" in o for o in out)
        assert any("<<EOF" in o and "END" in o for o in out)
        assert p.calls == 0 and term.requests == 0

    def test_debug_toggle_no_reset(self, store, session, tmp_path):
        p, target = write_provider(store, tmp_path)
        term, out = make_terminal(store, session, p, ["/debug", "/debug", "/quit"])
        assert term.run() == 0
        assert "debug: on\n" in out and "debug: off\n" in out
        assert p.calls == 0 and term.requests == 0
        # session untouched: exactly one LIVE session
        assert count(store, "sessions") == 1
        assert sessions.resolve_session(store, session.id).state.value == "LIVE"

    def test_quit_clean_exit(self, store, session, tmp_path):
        p, _ = write_provider(store, tmp_path)
        term, out = make_terminal(store, session, p, ["/quit"])
        assert term.run() == 0
        assert p.calls == 0 and term.requests == 0

    def test_unknown_local_command_is_local_error(self, store, session, tmp_path):
        p, _ = write_provider(store, tmp_path)
        term, out = make_terminal(store, session, p, ["/bogus", "/quit"])
        assert term.run() == 0
        assert "LOCAL ERROR: unknown command\n" in out
        assert p.calls == 0 and term.requests == 0

    def test_empty_input_ignored(self, store, session, tmp_path):
        p, _ = write_provider(store, tmp_path)
        term, out = make_terminal(store, session, p, ["", "", "/quit"])
        assert term.run() == 0
        assert p.calls == 0 and term.requests == 0

    def test_normal_request_success(self, store, session, tmp_path):
        p, target = write_provider(store, tmp_path)
        term, out = make_terminal(store, session, p, ["write the file", "y", "/quit"])
        assert term.run() == 0
        assert out.count("OK: Task COMPLETED\n") == 1
        assert target.read_text() == "hello"
        assert term.requests == 1

    def test_multiple_turns_one_session(self, store, session, tmp_path):
        """Two full turns (each with its own confirmation) in ONE session."""
        p, target = write_provider(store, tmp_path)
        term, out = make_terminal(store, session, p,
                                  ["first write", "y", "second write", "y", "/quit"])
        assert term.run() == 0
        assert out.count("OK: Task COMPLETED\n") == 2
        assert term.requests == 2
        assert count(store, "sessions") == 1
        assert sessions.resolve_session(store, session.id).state.value == "LIVE"

    def test_mixed_capability_turns_one_session(self, store, session, tmp_path):
        """A confirmation-requiring turn (file_write) followed by a
        non-confirmation turn (file_read) in the same session proves the
        three channels interleave correctly across turns."""
        target = tmp_path / "both.txt"
        target.write_text("probe")

        class TwoCap(StateAwareProvider):
            def __init__(self, store):
                super().__init__(store, "mixed", "file_write",
                                 {"path": str(target), "content": "written"},
                                 "verify_file_write")
                self.write_mode = True

            def call(self, contents, tools, allowed_names):
                if allowed_names[0] == "propose_action" and self.write_mode:
                    self.calls += 1
                    self.last_contents = contents
                    self.write_mode = False   # the write action lands: next turn reads
                    c = self.store.read()
                    s = c.execute("SELECT * FROM steps ORDER BY rowid DESC LIMIT 1").fetchone()
                    return FakeResponse(candidates=[FakeCandidate(content=FakeContent(
                        [FakePart(function_call=FakeFunctionCall("propose_action", {
                            "step_id": s["id"], "expected_step_revision": s["revision"],
                            "capability": "file_write",
                            "arguments": {"path": str(target),
                                          "content": "written"}}))]))])
                if allowed_names[0] == "propose_goal" and not self.write_mode:
                    # turn 2 (the read turn) starts: switch spec
                    self.statement, self.capability = "read it", "file_read"
                    self.action_args = {"path": str(target)}
                    self.verifier = "verify_file_read"
                return super().call(contents, tools, allowed_names)

        p = TwoCap(store)
        term, out = make_terminal(store, session, p,
                                  ["write it", "y", "read it", "/quit"])
        assert term.run() == 0
        assert out.count("OK: Task COMPLETED\n") == 2
        assert target.read_text() == "written"
        assert count(store, "sessions") == 1
        assert term.requests == 2


# ── input reader semantics (§8) ──────────────────────────────────────────────

class TestInputReader:
    def test_exact_line_preserved_verbatim(self, store, session, tmp_path):
        p, _ = write_provider(store, tmp_path)
        line = "  spaced   request  "
        term, out = make_terminal(store, session, p, [line, "y", "/quit"])
        assert term.run() == 0
        assert instruction_reached(p) == line

    def test_whitespace_only_line_is_submitted(self, store, session, tmp_path):
        p, _ = write_provider(store, tmp_path)
        term, out = make_terminal(store, session, p, ["   ", "y", "/quit"])
        assert term.run() == 0
        assert instruction_reached(p) == "   "
        assert term.requests == 1

    def test_sequential_no_readahead_across_confirmation(self, store, session, tmp_path):
        """The confirmation decision consumes exactly one line; the NEXT line
        lands on the next normal prompt (as /quit), not on a phantom second
        confirmation or an EOF."""
        p, target = write_provider(store, tmp_path)
        term, out = make_terminal(store, session, p, ["write it", "y", "/quit"])
        assert term.run() == 0
        assert term.requests == 1
        assert p.calls == 4                    # goal, task, plan, action — nothing more
        assert "EOF: closing.\n" not in out    # /quit was consumed as a command

    def test_normal_eof_exits_zero(self, store, session, tmp_path):
        p, _ = write_provider(store, tmp_path)
        term, out = make_terminal(store, session, p, [])
        assert term.run() == 0
        assert "EOF: closing.\n" in out
        assert p.calls == 0 and term.requests == 0


# ── multiline protocol (§10) ────────────────────────────────────────────────

class TestMultiline:
    def _read_turn(self, store, session, tmp_path, body_lines):
        p, target = read_provider(store, tmp_path)
        term, out = make_terminal(store, session, p,
                                  ["<<EOF", *body_lines, "END", "/quit"])
        assert term.run() == 0
        return p, term, out

    def test_exact_syntax_blank_lines_whitespace_preserved(self, store, session, tmp_path):
        p, term, out = self._read_turn(store, session, tmp_path,
                                       ["  indented", "", "end line   "])
        assert instruction_reached(p) == "  indented\n\nend line   "
        assert term.requests == 1
        assert out.count("OK: Task COMPLETED\n") == 1

    def test_commands_inside_body_are_literal(self, store, session, tmp_path):
        p, term, out = self._read_turn(store, session, tmp_path,
                                       ["has /debug inside", "and /help too"])
        assert instruction_reached(p) == "has /debug inside\nand /help too"
        assert "debug: on\n" not in out     # /debug inside the body did NOT toggle
        assert term.requests == 1

    def test_end_must_be_standalone(self, store, session, tmp_path):
        p, term, out = self._read_turn(store, session, tmp_path,
                                       ["  END", "ENDX"])
        assert instruction_reached(p) == "  END\nENDX"
        assert term.requests == 1

    def test_empty_body_not_submitted(self, store, session, tmp_path):
        p, target = read_provider(store, tmp_path)
        term, out = make_terminal(store, session, p, ["<<EOF", "END", "/quit"])
        assert term.run() == 0
        assert p.calls == 0 and term.requests == 0

    def test_multiline_eof_discards_and_exits_zero(self, store, session, tmp_path):
        p, target = read_provider(store, tmp_path)
        term, out = make_terminal(store, session, p, ["<<EOF", "partial line"])
        assert term.run() == 0
        assert "EOF: incomplete multiline request discarded.\n" in out
        assert p.calls == 0 and term.requests == 0

    def test_end_consumed_exactly_once_then_quit(self, store, session, tmp_path):
        p, target = read_provider(store, tmp_path)
        term, out = make_terminal(store, session, p,
                                  ["<<EOF", "read body", "END", "/quit"])
        assert term.run() == 0
        assert term.requests == 1
        assert "EOF: closing.\n" not in out    # /quit hit the normal prompt


# ── exit statuses (§12) ─────────────────────────────────────────────────────

class TestExitStatuses:
    def test_denial_then_quit_exits_zero(self, store, session, tmp_path):
        p, target = delete_provider(store, tmp_path)
        term, out = make_terminal(store, session, p, ["delete it", "n", "/quit"])
        assert term.run() == 0

    def test_confirmation_eof_exits_zero(self, store, session, tmp_path):
        p, target = delete_provider(store, tmp_path)
        term, out = make_terminal(store, session, p, ["delete it"])
        assert term.run() == 0
        assert "NOT APPROVED: pending action was not approved.\n" in "".join(out)

    def test_fatal_provider_init_returns_one(self, store, session):
        def broken_factory():
            raise RuntimeError("provider credential missing")
        out = []
        term = InteractiveTerminal(store, session, broken_factory,
                                   input_fn=make_inputs(["do something"]),
                                   output_fn=out.append)
        assert term.run() == 1
        assert any(o.startswith("FATAL:") for o in out)

    def test_handled_jarvis_failure_exits_zero(self, store, session, tmp_path):
        p = StateAwareProvider(store, "bogus", "file_write",
                               {"path": "/tmp/x", "content": "c"},
                               "verify_bogus_method")
        term, out = make_terminal(store, session, p, ["do it", "/quit"])
        assert term.run() == 0
        assert any(o.startswith("ERROR") for o in out)
        assert "FATAL" not in "".join(out)


class TestScriptExitCodes:
    """Real process return codes for scripts/run_interactive.py — fully
    offline (sanitized env, no credentials): local commands and EOF never
    construct the provider (lazy init), so no network or key is needed."""

    @staticmethod
    def _run_script(stdin_text, tmp_path):
        env = {k: v for k, v in os.environ.items() if "NVIDIA" not in k}
        env["JARVIS_V5_DB"] = str(tmp_path / "sub.db")
        env["JARVIS_ENV_FILE"] = str(tmp_path / "no.env")   # nonexistent
        return subprocess.run(
            [sys.executable, str(pathlib.Path("scripts/run_interactive.py").resolve())],
            input=stdin_text, capture_output=True, text=True, timeout=60, env=env)

    def test_quit_exit_zero(self, tmp_path):
        r = self._run_script("/quit\n", tmp_path)
        assert r.returncode == 0, r.stderr
        assert "JARVIS V5 interactive terminal" in r.stdout

    def test_eof_exit_zero(self, tmp_path):
        r = self._run_script("", tmp_path)
        assert r.returncode == 0
        assert "EOF: closing." in r.stdout

    def test_unknown_command_local_error_exit_zero(self, tmp_path):
        r = self._run_script("/whatever\n", tmp_path)
        assert r.returncode == 0
        assert "LOCAL ERROR: unknown command" in r.stdout

    def test_help_renders(self, tmp_path):
        r = self._run_script("/help\n/quit\n", tmp_path)
        assert r.returncode == 0
        assert "Multiline input:" in r.stdout and "<<EOF" in r.stdout

    def test_request_without_credentials_fatals_exit_one(self, tmp_path):
        r = self._run_script("create a file\n", tmp_path)
        assert r.returncode == 1
        assert "FATAL" in r.stdout


# ── error rendering (§13) ───────────────────────────────────────────────────

class TestErrorRendering:
    def test_live_loop_rejection_rendered_with_native_code(self, store, session, tmp_path):
        p = StateAwareProvider(store, "bogus", "file_write",
                               {"path": "/tmp/x", "content": "c"},
                               "verify_bogus_method")
        term, out = make_terminal(store, session, p, ["go", "/quit"])
        assert term.run() == 0
        assert "ERROR too many membrane rejections\n" in out

    def test_execution_failure_rendered_no_fake_success(self, store, session, tmp_path):
        ghost = tmp_path / "ghost.txt"   # never created -> DefiniteNoEffect
        p = StateAwareProvider(store, "delete ghost", "file_delete",
                               {"path": str(ghost)}, "verify_file_delete")
        term, out = make_terminal(store, session, p, ["delete it", "y", "/quit"])
        assert term.run() == 0
        assert "ERROR STEP_NOT_COMPLETED\n" in out
        assert "OK: Task" not in "".join(out)
        assert not ghost.exists()

    def test_verification_failure_rendered_file_intact(self, store, session, tmp_path):
        target = tmp_path / "survivor.txt"
        target.write_text("still here")
        p = StateAwareProvider(store, "delete survivor", "file_delete",
                               {"path": str(target)}, "verify_file_delete")
        real = caps.get("file_delete").execute
        caps._REGISTRY["file_delete"].execute = lambda a: {
            "path": a["path"], "existed": True, "deleted": True}
        try:
            term, out = make_terminal(store, session, p, ["delete it", "y", "/quit"])
            assert term.run() == 0
        finally:
            caps._REGISTRY["file_delete"].execute = real
        assert "ERROR VERIFICATION_NOT_PASS\n" in out
        assert target.exists() and target.read_text() == "still here"

    def test_unexpected_exception_sanitized_and_continues(self, store, session, monkeypatch):
        monkeypatch.setenv("X_TEST_API_KEY", "supersecretvalue123")

        class Exploding:
            model_name = "exploding"
            def call(self, *a, **k):
                raise RuntimeError("request failed with supersecretvalue123 inside")

        out = []
        term = InteractiveTerminal(store, session, lambda: Exploding(),
                                   input_fn=make_inputs(["go", "/quit"]),
                                   output_fn=out.append)
        assert term.run() == 0
        joined = "".join(out)
        assert "ERROR RuntimeError:" in joined
        assert "supersecretvalue123" not in joined
        assert "***" in joined


# ── debug mode (§16) ────────────────────────────────────────────────────────

class TestDebugMode:
    def test_debug_renders_real_structured_events(self, store, session, tmp_path):
        p, target = write_provider(store, tmp_path)
        term, out = make_terminal(store, session, p,
                                  ["/debug", "write it", "y", "/quit"])
        assert term.run() == 0
        debug_lines = [o for o in out if o.startswith("[debug]")]
        assert any("execute_action" in o for o in debug_lines)
        assert any("execution_blocked" in o and "CONFIRMATION_REQUIRED" in o for o in debug_lines)
        assert any("confirmation_granted" in o for o in debug_lines)
        assert any("complete_step" in o for o in debug_lines)
        assert any("verify_file_write" in o for o in debug_lines)
        assert any("[debug] turn complete" in o for o in debug_lines)
        assert "OK: Task COMPLETED\n" in out

    def test_debug_off_no_debug_lines(self, store, session, tmp_path):
        p, target = write_provider(store, tmp_path)
        term, out = make_terminal(store, session, p, ["write it", "y", "/quit"])
        assert term.run() == 0
        assert not [o for o in out if o.startswith("[debug]")]

    def test_debug_no_secrets_in_output(self, store, session, tmp_path, monkeypatch):
        monkeypatch.setenv("X_TEST_API_KEY", "supersecretvalue123")
        p, target = write_provider(store, tmp_path)
        term, out = make_terminal(store, session, p,
                                  ["/debug", "write it", "y", "/quit"])
        assert term.run() == 0
        assert "supersecretvalue123" not in "".join(out)

    def test_guidance_names_only_registered_capabilities(self):
        for cap in ("file_write", "file_read", "file_delete"):
            assert cap in TERMINAL_GUIDANCE
            assert caps.get(cap) is not None
        for m in ("verify_file_write", "verify_file_read", "verify_file_delete"):
            assert m in TERMINAL_GUIDANCE
            assert verification.get_method(m) is not None


# ── confirmation pause/resume (§18, §21) ─────────────────────────────────────

class TestConfirmationApproval:
    def test_approval_full_lifecycle_same_action_no_duplicates(self, store, session, tmp_path):
        p, target = delete_provider(store, tmp_path)
        term, out = make_terminal(store, session, p, ["delete it", "y", "/quit"])
        assert term.run() == 0
        joined = "".join(out)

        # terminal rendering: the pause, the prompt, the final verified result
        assert "CONFIRMATION_REQUIRED\n" in joined
        assert "action: file_delete" in joined
        assert "OK: Task COMPLETED\n" in joined
        assert joined.index("CONFIRMATION_REQUIRED") < joined.index("OK: Task COMPLETED")

        # canonical state: NO duplicate work of any kind
        assert count(store, "goals") == 1
        assert count(store, "tasks") == 1
        assert count(store, "plans") == 1
        assert count(store, "steps") == 1
        assert count(store, "actions") == 1

        # the SAME action resumed and executed exactly once
        arow = store.read().execute("SELECT * FROM actions").fetchone()
        assert arow["status"] == "OBSERVED"
        assert arow["capability"] == "file_delete"
        assert json.loads(arow["arguments"]) == {"path": str(target)}

        # the confirmation went through the EXISTING mechanism, bound to the
        # same action identity + revision + capability + arguments. The
        # binding was staged at the Action's PENDING revision (0); execution
        # later bumped the Action to OBSERVED revision 2 (EXECUTING+OBSERVED).
        crow = store.read().execute(
            "SELECT * FROM confirmations WHERE action_id = ?", (arow["id"],)).fetchone()
        assert crow is not None
        assert crow["confirmed_at"] is not None
        assert crow["action_revision"] == 0                    # staged pre-execution
        assert crow["capability"] == "file_delete"
        assert crow["arguments"] == arow["arguments"]

        # exactly one real Observation + independent verification PASS
        obs = store.read().execute(
            "SELECT * FROM observations WHERE action_id = ?", (arow["id"],)).fetchone()
        assert obs is not None
        ver = store.read().execute(
            "SELECT result FROM verifications WHERE step_id IS NOT NULL").fetchone()
        assert ver["result"] == "PASS"
        assert not target.exists()    # the deletion actually happened

        # step + task completed only after verification
        srow = store.read().execute("SELECT * FROM steps").fetchone()
        assert srow["status"] == "COMPLETED"
        trow = store.read().execute("SELECT * FROM tasks").fetchone()
        assert trow["status"] == "COMPLETED"

        # no extra model request was issued for the confirmation
        assert term.requests == 1
        assert p.calls == 4           # goal, task, plan, action — nothing more
        assert "DECLINED" not in joined
        assert joined.count("OK: Task COMPLETED\n") == 1   # no duplicate result render


class TestConfirmationDenial:
    def test_denial_blocks_execution_truthfully(self, store, session, tmp_path):
        p, target = delete_provider(store, tmp_path)
        term, out = make_terminal(store, session, p, ["delete it", "n", "/quit"])
        assert term.run() == 0
        joined = "".join(out)
        assert "DECLINED: action was not approved; nothing was executed.\n" in joined
        assert "OK: Task" not in joined

        arow = store.read().execute("SELECT * FROM actions").fetchone()
        assert arow["status"] == "PENDING"                    # never executed
        assert count(store, "observations") == 0
        assert target.exists() and target.read_text() == "delete me"
        assert store.read().execute(
            "SELECT COUNT(*) n FROM confirmations WHERE confirmed_at IS NOT NULL"
        ).fetchone()["n"] == 0                                 # nothing confirmed
        trow = store.read().execute("SELECT * FROM tasks").fetchone()
        assert trow["status"] != "COMPLETED"

    def test_confirmation_eof_cannot_approve(self, store, session, tmp_path):
        p, target = delete_provider(store, tmp_path)
        term, out = make_terminal(store, session, p, ["delete it"])
        assert term.run() == 0
        assert "NOT APPROVED: pending action was not approved.\n" in "".join(out)
        arow = store.read().execute("SELECT * FROM actions").fetchone()
        assert arow["status"] == "PENDING"
        assert target.exists()
        assert count(store, "confirmations") == 0              # nothing staged at all

    def test_commands_not_recognized_during_confirmation(self, store, session, tmp_path):
        p, target = delete_provider(store, tmp_path)
        term, out = make_terminal(store, session, p, ["delete it", "/debug", "/quit"])
        assert term.run() == 0
        joined = "".join(out)
        assert "DECLINED" in joined                  # /debug at confirmation = denial
        assert "debug: on\n" not in joined           # it did NOT toggle debug
        assert target.exists()                       # and nothing executed
