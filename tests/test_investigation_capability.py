"""Investigation Capability v1 — codebase_query, registry-driven boundary.

Pins the approved milestone:
  * codebase_query: registration, strict schema, source-root containment,
    bounded deterministic read/search execution, OBSERVED Observations;
  * verify_codebase_query: INDEPENDENT re-computation — honest claims PASS,
    fabricated/misleading results FAIL, unreproducible claims FAIL;
  * registry-driven Intent Boundary: WORK operations derive from the live
    registry; investigation directives about the JARVIS codebase are WORK;
    discussion/hypothetical/capability-question forms stay CONVERSATION;
    out-of-registry directives (web research) stay CONVERSATION with an
    honest capability-unavailable decline (no fabricated re-route, no
    "submit it as a task" invitation);
  * end-to-end lifecycle for the canonical example with the REAL source
    tree: Goal → Task → Plan → Action → Execution → Observation →
    independent Verification → COMPLETED.
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest

from v5 import capabilities as caps
from v5 import paths, sessions, verification
from v5.capabilities import DefiniteNoEffect
from v5.models import Ok, Rejected
from v5.store import Store
from v5.terminal import TERMINAL_GUIDANCE, InteractiveTerminal
from tests.test_conversational_boundary import ClassifyProvider
from tests.test_terminal import (StateAwareProvider, count, make_inputs,
                                 make_terminal)

ALL_TABLES = ("goals", "tasks", "plans", "steps", "actions",
              "observations", "verifications", "obligations")

# The five directives from the investigation report. Three are fulfillable
# codebase investigations (WORK via codebase_query); two fundamentally need
# external evidence (external-model benchmarks / a third-party product
# evaluation) and must stay CONVERSATION with an honest decline.
CODEBASE_WORK_EXAMPLES = [
    "Investigate the current JARVIS architecture and identify its biggest bottleneck.",
    "Find out what the current implementation of JARVIS does when an action ends in UNKNOWN_OUTCOME.",
    "Investigate how confirmation is enforced in JARVIS.",
    "Find where UNKNOWN_OUTCOME is created and explain the lifecycle.",
    "Compare two approaches for local tool calling and recommend the better one for JARVIS.",
]
EXTERNAL_RESEARCH_EXAMPLES = [
    "Investigate whether a 121M parameter specialized tool-calling model could realistically replace a much larger model for JARVIS's Cognition layer.",
    "Research whether Needle is a good fit for JARVIS.",
]
CONVERSATION_CONTROLS = [
    "What is UNKNOWN_OUTCOME?",
    "Explain how UNKNOWN_OUTCOME works.",
    "What would happen if an action ended in UNKNOWN_OUTCOME?",
    "Can you investigate UNKNOWN_OUTCOME?",
    "Explain how I could improve JARVIS's tool-calling reliability.",
    "What is the difference between RAM and storage?",
]


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "state.db"))
    yield s
    s.close()


@pytest.fixture
def session(store):
    return sessions.create_session(store, cognition_authorized=True)


@pytest.fixture
def src_tree(tmp_path):
    """A synthetic source tree with known content (the autouse fixture
    already points JARVIS_V5_SOURCE_ROOT at tmp_path/src)."""
    tree = tmp_path / "src"
    (tree / "v5").mkdir(exist_ok=True)
    (tree / "v5" / "engine.py").write_text(
        '"""demo module."""\n'
        "def run():\n"
        "    # outcome becomes UNKNOWN_OUTCOME on timeout\n"
        "    return 'unknown'\n", encoding="utf-8")
    (tree / "v5" / "safety.py").write_text(
        "CONFIRM = 'required'\n"
        "def gate():\n"
        "    return CONFIRM\n", encoding="utf-8")
    (tree / "notes.txt").write_text("plain text file\n", encoding="utf-8")
    (tree / "blob.bin").write_bytes(bytes([0xFF, 0xFE, 0x00, 0x81]))
    return tree


# ── registration + registry-driven prompts ──────────────────────────────────

class TestRegistration:
    def test_spec_registered_read_only(self):
        spec = caps.get("codebase_query")
        assert spec is not None
        assert spec.requires_confirmation is False      # pure read
        assert spec.idempotency_class.name == "IDEMPOTENT"
        assert spec.summary and spec.arguments_hint

    def test_verifier_registered(self):
        assert verification.get_method("verify_codebase_query") is not None
        assert "verify_codebase_query" in verification.registered_methods()

    def test_classify_instruction_is_registry_driven(self):
        from v5 import conversation
        instr = conversation._classify_instruction()
        for name in sorted(caps._REGISTRY):
            assert name in instr, name
        # dynamic: a newly registered (fake) capability appears in the prompt
        caps.register(caps.CapabilitySpec(
            name="fake_probe_cap", idempotency_class=caps.IdempotencyClass.IDEMPOTENT,
            requires_confirmation=False, execute=lambda a: {},
            summary="fake summary for lockstep"))
        try:
            assert "fake_probe_cap" in conversation._classify_instruction()
            from v5.terminal import _build_terminal_guidance
            assert "fake_probe_cap" in _build_terminal_guidance()
        finally:
            del caps._REGISTRY["fake_probe_cap"]

    def test_terminal_guidance_lists_every_registered_capability_and_method(self):
        for name in sorted(caps._REGISTRY):
            assert name in TERMINAL_GUIDANCE, name
        for m in verification.registered_methods():
            assert m in TERMINAL_GUIDANCE, m


# ── argument schema ─────────────────────────────────────────────────────────

class TestSchema:
    def test_valid_read_and_search(self):
        v = caps.get("codebase_query").validate_args
        assert v({"operation": "read_file", "path": "v5/engine.py"}) is None
        assert v({"operation": "search", "pattern": "UNKNOWN_OUTCOME"}) is None
        assert v({"operation": "search", "pattern": "conf", "file_glob": "*.txt"}) is None

    def test_rejections(self):
        v = caps.get("codebase_query").validate_args
        assert v({}) is not None                                  # no operation
        assert v({"operation": "rm"}) is not None                 # bad operation
        assert v({"operation": "read_file"}) is not None          # missing path
        assert v({"operation": "search"}) is not None             # missing pattern
        assert v({"operation": "read_file", "path": "../../escape.py"}) is not None
        assert v({"operation": "read_file", "path": "/etc/passwd"}) is not None
        assert v({"operation": "search", "pattern": "a" * 500}) is not None
        assert v({"operation": "search", "pattern": "bad\x00pattern"}) is not None
        assert v({"operation": "search", "pattern": "("}) is not None  # invalid regex
        assert v({"operation": "read_file", "path": "x", "pattern": "y"}) is not None
        assert v({"operation": "search", "pattern": "x", "extra": 1}) is not None

    def test_execute_rejects_the_same_escape(self, src_tree):
        with pytest.raises(DefiniteNoEffect):
            caps._codebase_query({"operation": "read_file", "path": "../outside.py"})
        with pytest.raises(DefiniteNoEffect):
            caps._codebase_query({"operation": "read_file", "path": "/etc/passwd"})


# ── source-root boundary (resolver) ─────────────────────────────────────────

class TestSourceBoundary:
    def test_relative_and_nested_resolve_inside(self, src_tree):
        p, err = paths.resolve_source_path("v5/engine.py")
        assert err is None and p == src_tree / "v5" / "engine.py"
        p, err = paths.resolve_source_path("./v5/./engine.py")
        assert err is None and p == src_tree / "v5" / "engine.py"

    def test_absolute_inside_preserved_outside_rejected(self, src_tree):
        p, err = paths.resolve_source_path(str(src_tree / "v5/engine.py"))
        assert err is None and p == src_tree / "v5" / "engine.py"
        _, err = paths.resolve_source_path("/etc/passwd")
        assert err is not None and "source tree" in err

    def test_traversal_rejected(self):
        _, err = paths.resolve_source_path("../escape.txt")
        assert err is not None and "escapes" in err
        _, err = paths.resolve_source_path("v5/../../escape.txt")
        assert err is not None

    def test_root_and_malformed_rejected(self):
        for bad in ("", "   ", ".", "a\x00b", 42, "x" * 5000):
            _, err = paths.resolve_source_path(bad)
            assert err is not None, bad

    def test_symlink_escape_rejected_in_tree_symlink_ok(self, src_tree, tmp_path):
        outside = tmp_path / "outside_data.txt"
        outside.write_text("secret-ish")
        link = src_tree / "leak_link.py"
        os.symlink(outside, link)
        try:
            _, err = paths.resolve_source_path("leak_link.py")
            assert err is not None      # resolves outside -> rejected
        finally:
            link.unlink()
        # a symlink INSIDE the tree to another in-tree file stays usable
        inner_link = src_tree / "alias.py"
        os.symlink(src_tree / "v5" / "engine.py", inner_link)
        try:
            p, err = paths.resolve_source_path("alias.py")
            assert err is None and p == src_tree / "v5" / "engine.py"
        finally:
            inner_link.unlink()

    def test_deterministic_and_env_driven(self, src_tree):
        a, _ = paths.resolve_source_path("v5/engine.py")
        b, _ = paths.resolve_source_path("v5/engine.py")
        assert a == b


# ── bounded execution ────────────────────────────────────────────────────────

class TestExecution:
    def test_read_file(self, src_tree):
        r = caps._codebase_query({"operation": "read_file", "path": "v5/engine.py"})
        assert r["operation"] == "read_file"
        assert r["rel_path"] == "v5/engine.py"
        assert "UNKNOWN_OUTCOME" in r["content"]
        assert r["truncated"] is False

    def test_read_missing_and_binary_fail_definitely(self, src_tree):
        with pytest.raises(DefiniteNoEffect):
            caps._codebase_query({"operation": "read_file", "path": "nope.py"})
        with pytest.raises(DefiniteNoEffect):
            caps._codebase_query({"operation": "read_file", "path": "blob.bin"})

    def test_read_cap_truncates(self, src_tree):
        big = src_tree / "big.py"
        big.write_text("x" * (paths.__dict__.get("_READ_CAP_BYTES", 65536) + 10)
                       if False else "y" * (70 * 1024), encoding="utf-8")
        r = caps._codebase_query({"operation": "read_file", "path": "big.py"})
        assert r["truncated"] is True
        assert len(r["content"]) == 64 * 1024

    def test_search_finds_matches(self, src_tree):
        r = caps._codebase_query({"operation": "search", "pattern": "UNKNOWN_OUTCOME"})
        assert r["operation"] == "search"
        files = {m["file"] for m in r["matches"]}
        assert "v5/engine.py" in files
        assert all(isinstance(m["line_no"], int) and m["line"] for m in r["matches"])

    def test_search_zero_matches_is_observed_truth_not_failure(self, src_tree):
        r = caps._codebase_query({"operation": "search", "pattern": "ZEBRA_NOTHING"})
        assert r["matches"] == [] and r["files_scanned"] >= 1

    def test_search_glob_and_binary_skip(self, src_tree):
        r = caps._codebase_query({"operation": "search", "pattern": "plain",
                                   "file_glob": "*.txt"})
        assert any(m["file"] == "notes.txt" for m in r["matches"])
        r2 = caps._codebase_query({"operation": "search", "pattern": "conf"})
        assert all(m["file"] != "blob.bin" for m in r2["matches"])

    def test_search_match_cap_truncates(self, src_tree):
        for i in range(70):
            (src_tree / f"m{i}.py").write_text(f"needle line {i}\n", encoding="utf-8")
        r = caps._codebase_query({"operation": "search", "pattern": "needle"})
        assert len(r["matches"]) == 50 and r["truncated"] is True

    def test_search_symlink_escape_cannot_smuggle(self, src_tree, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "evil.py").write_text("needle in eviltree\n", encoding="utf-8")
        link = src_tree / "evil_dir"
        os.symlink(outside, link)
        try:
            r = caps._codebase_query({"operation": "search", "pattern": "needle"})
            assert all(m["file"] != "evil_dir/evil.py" for m in r["matches"])
        finally:
            link.unlink()


# ── independent verification ────────────────────────────────────────────────

class TestVerificationIndependence:
    def _action_with_observation(self, store, args, observation):
        from v5.ids import new_id
        action_id, obs_id = new_id("action"), new_id("observation")
        with store.write() as conn:
            conn.execute(
                "INSERT INTO actions (id, revision, step_id, status, capability, "
                "arguments, idempotency_class, confirmation_id, retry_of) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (action_id, 0, "s", "OBSERVED", "codebase_query",
                 json.dumps(args), "IDEMPOTENT", None, None))
            conn.execute(
                "INSERT INTO observations (id, action_id, captured_at, raw_result, "
                "execution_source) VALUES (?,?,?,?,?)",
                (obs_id, action_id, "t", json.dumps(observation), "codebase_query"))
        return action_id

    def _evaluate(self, store, action_id):
        spec = verification.get_method("verify_codebase_query")
        return spec.make_evaluator(store, action_id)(None, [])

    def test_honest_read_passes(self, store, src_tree):
        ground = caps._codebase_query({"operation": "read_file", "path": "v5/engine.py"})
        aid = self._action_with_observation(
            store, {"operation": "read_file", "path": "v5/engine.py"}, ground)
        assert self._evaluate(store, aid) is True

    def test_fabricated_read_content_fails(self, store, src_tree):
        ground = caps._codebase_query({"operation": "read_file", "path": "v5/engine.py"})
        lie = dict(ground)
        lie["content"] = ground["content"].replace("UNKNOWN_OUTCOME", "FAKED_OUTCOME")
        aid = self._action_with_observation(
            store, {"operation": "read_file", "path": "v5/engine.py"}, lie)
        assert self._evaluate(store, aid) is False

    def test_honest_search_passes(self, store, src_tree):
        ground = caps._codebase_query({"operation": "search", "pattern": "UNKNOWN"})
        aid = self._action_with_observation(
            store, {"operation": "search", "pattern": "UNKNOWN"}, ground)
        assert self._evaluate(store, aid) is True

    def test_fabricated_match_line_fails(self, store, src_tree):
        ground = caps._codebase_query({"operation": "search", "pattern": "UNKNOWN"})
        lie = dict(ground)
        lie["matches"] = ground["matches"] + [
            {"file": "v5/ghost.py", "line_no": 999, "line": "fabricated needle"}]
        aid = self._action_with_observation(
            store, {"operation": "search", "pattern": "UNKNOWN"}, lie)
        assert self._evaluate(store, aid) is False

    def test_hidden_matches_fail(self, store, src_tree):
        # a pattern with several ground matches so suppression is detectable
        pattern = "def"
        ground = caps._codebase_query({"operation": "search", "pattern": pattern})
        assert len(ground["matches"]) >= 2
        lie = dict(ground)
        lie["matches"] = ground["matches"][:1]       # suppress real matches
        aid = self._action_with_observation(
            store, {"operation": "search", "pattern": pattern}, lie)
        assert self._evaluate(store, aid) is False

    def test_zero_match_truth_passes_zero_match_lie_fails(self, store, src_tree):
        args = {"operation": "search", "pattern": "ZEBRA_NOTHING"}
        ground = caps._codebase_query(args)
        aid = self._action_with_observation(store, args, ground)
        assert self._evaluate(store, aid) is True
        lie = dict(ground)
        lie["matches"] = [{"file": "v5/fake.py", "line_no": 1, "line": "ZEBRA"}]
        aid2 = self._action_with_observation(store, args, lie)
        assert self._evaluate(store, aid2) is False

    def test_unreproducible_claim_fails(self, store, src_tree):
        ground = caps._codebase_query({"operation": "read_file", "path": "v5/engine.py"})
        (src_tree / "v5" / "engine.py").unlink()   # claim no longer reproducible
        aid = self._action_with_observation(
            store, {"operation": "read_file", "path": "v5/engine.py"}, ground)
        assert self._evaluate(store, aid) is False


# ── intent-boundary routing (deterministic, scripted classifier) ───────────

class TestIntentRouting:
    def _conversation_turn(self, store, session, text):
        p = ClassifyProvider("conversation", reply="declined for test")
        term, out = make_terminal(store, session, p, [text, "/quit"])
        assert term.run() == 0
        return term, out

    @pytest.mark.parametrize("text", CODEBASE_WORK_EXAMPLES)
    def test_codebase_investigation_directives_route_work(self, store, session,
                                                          tmp_path, text):
        base = StateAwareProvider(store, "investigate", "codebase_query",
                                  {"operation": "search", "pattern": "UNKNOWN_OUTCOME"},
                                  "verify_codebase_query")
        # codebase_query is read-only (requires_confirmation=False): there is
        # no confirmation prompt, so no denial line in the script
        term, out = make_terminal(store, session, ClassifyProvider("work", base=base),
                                  [text, "/quit"])
        assert term.run() == 0
        assert term.requests == 1

    @pytest.mark.parametrize("text", EXTERNAL_RESEARCH_EXAMPLES)
    def test_external_research_stays_conversation_zero_rows(self, store, session, text):
        p = ClassifyProvider("conversation",
                             reply="JARVIS does not have a web-research capability yet.")
        term, out = make_terminal(store, session, p, [text, "/quit"])
        assert term.run() == 0
        assert term.requests == 0
        assert all(count(store, t) == 0 for t in ALL_TABLES)
        assert any("does not have" in o for o in out)

    @pytest.mark.parametrize("text", CONVERSATION_CONTROLS)
    def test_conversation_controls_stay_conversation(self, store, session, text):
        term, out = self._conversation_turn(store, session, text)
        assert term.requests == 0
        assert all(count(store, t) == 0 for t in ALL_TABLES)

    def test_honest_decline_rule_in_reply_prompt(self):
        from v5 import conversation
        src = conversation._REPLY_INSTRUCTION
        # the incoherent invitation is gone; the honest capability-unavailable
        # wording is present
        assert "do NOT tell them to submit it as a task" in src
        assert "does not currently have" in src or "does not have that capability" in src

    def test_work_still_reaches_confirmation_for_file_write(self, store, session, tmp_path):
        base, target = StateAwareProvider(store, "w", "file_write",
                                          {"path": "cfm.txt", "content": "x"},
                                          "verify_file_write"), None
        term, out = make_terminal(store, session, ClassifyProvider("work", base=base),
                                  ["create a file called cfm.txt", "n", "/quit"])
        assert term.run() == 0
        assert "CONFIRMATION_REQUIRED" in "".join(out)
        assert "DECLINED" in "".join(out)


# ── end-to-end lifecycle against the REAL source tree ───────────────────────

class TestEndToEndLifecycle:
    def test_unknown_outcome_investigation_full_chain(self, store, session,
                                                      tmp_path, monkeypatch):
        """The canonical example: 'Find out what the current implementation
        of JARVIS does when an action ends in UNKNOWN_OUTCOME.' — full
        canonical lifecycle against the real repository source tree, honest
        verification, no fake completion."""
        monkeypatch.setenv("JARVIS_V5_SOURCE_ROOT",
                           str(pathlib.Path(__file__).resolve().parent.parent))

        class Investigator(StateAwareProvider):
            """Scripted provider proposing the genuine investigation plan:
            search the real source for UNKNOWN_OUTCOME, then read the file
            that defines its lifecycle (evidence.py)."""
            def call(self, contents, tools, allowed_names):
                c = self.store.read()
                if allowed_names[0] == "propose_plan":
                    t = c.execute("SELECT * FROM tasks ORDER BY rowid DESC LIMIT 1").fetchone()
                    return self._resp("propose_plan", {
                        "task_id": t["id"], "expected_task_revision": t["revision"],
                        "steps": [
                            {"description": "search for UNKNOWN_OUTCOME",
                             "required": True, "depends_on_index": [],
                             "execution_capability": "codebase_query",
                             "verification_requirements": [
                                 {"method_name": "verify_codebase_query",
                                  "applies_to_capability": "codebase_query"}]},
                            {"description": "read the lifecycle implementation",
                             "required": True, "depends_on_index": [0],
                             "execution_capability": "codebase_query",
                             "verification_requirements": [
                                 {"method_name": "verify_codebase_query",
                                  "applies_to_capability": "codebase_query"}]},
                        ]})
                if allowed_names[0] == "propose_action":
                    steps = c.execute(
                        "SELECT * FROM steps ORDER BY rowid").fetchall()
                    if steps[0]["status"] != "COMPLETED":
                        target = steps[0]
                        args = {"operation": "search", "pattern": "UNKNOWN_OUTCOME"}
                    else:
                        target = steps[1]
                        # read the real file the search surfaced
                        args = {"operation": "read_file", "path": "v5/evidence.py"}
                    return self._resp("propose_action", {
                        "step_id": target["id"],
                        "expected_step_revision": target["revision"],
                        "capability": "codebase_query", "arguments": args})
                return super().call(contents, tools, allowed_names)

            def _resp(self, name, args):
                from tests.test_live_loop import (FakeCandidate, FakeContent,
                                                  FakeFunctionCall, FakePart,
                                                  FakeResponse)
                return FakeResponse(candidates=[FakeCandidate(content=FakeContent(
                    [FakePart(function_call=FakeFunctionCall(name, args))]))])

        base = Investigator(store, "investigate", "codebase_query",
                            {"operation": "search", "pattern": "UNKNOWN_OUTCOME"},
                            "verify_codebase_query")
        term, out = make_terminal(store, session, ClassifyProvider("work", base=base),
                                  ["Find out what the current implementation of "
                                   "JARVIS does when an action ends in UNKNOWN_OUTCOME.",
                                   "/quit"])
        assert term.run() == 0
        joined = "".join(out)
        assert "OK: Task COMPLETED" in joined, joined

        # canonical lifecycle — real rows, real states
        assert count(store, "goals") == 1
        assert count(store, "tasks") == 1
        assert count(store, "steps") == 2
        assert count(store, "actions") == 2
        t = store.read().execute("SELECT status FROM tasks").fetchone()
        assert t["status"] == "COMPLETED"
        steps = store.read().execute(
            "SELECT status FROM steps ORDER BY rowid").fetchall()
        assert all(s["status"] == "COMPLETED" for s in steps)
        actions = store.read().execute(
            "SELECT status, capability FROM actions ORDER BY rowid").fetchall()
        assert all(a["status"] == "OBSERVED" and a["capability"] == "codebase_query"
                   for a in actions)
        # real Observations with real repo evidence
        obs_rows = store.read().execute(
            "SELECT raw_result FROM observations ORDER BY rowid").fetchall()
        search_obs = json.loads(obs_rows[0]["raw_result"])
        assert search_obs["operation"] == "search"
        assert len(search_obs["matches"]) > 0       # real matches in the real repo
        read_obs = json.loads(obs_rows[1]["raw_result"])
        assert read_obs["rel_path"] == "v5/evidence.py"
        assert "UNKNOWN_OUTCOME" in read_obs["content"]
        # independent verification PASSed for both requirement bindings
        vers = store.read().execute(
            "SELECT result FROM verifications WHERE step_id IS NOT NULL").fetchall()
        assert len(vers) == 2 and all(v["result"] == "PASS" for v in vers)
        assert count(store, "obligations") == 0      # no failures, no retries

    def test_sabotaged_observation_cannot_complete(self, store, session,
                                                   tmp_path, monkeypatch):
        """Adversarial: a lying codebase_query (fabricated matches) gets
        OBSERVED, but the INDEPENDENT verifier catches it — no completion."""
        monkeypatch.setenv("JARVIS_V5_SOURCE_ROOT",
                           str(pathlib.Path(__file__).resolve().parent.parent))
        real = caps._REGISTRY["codebase_query"].execute

        def liar(args):
            if args.get("operation") == "search":
                return {"operation": "search", "pattern": args.get("pattern"),
                        "file_glob": "*.py",
                        "matches": [{"file": "v5/fake.py", "line_no": 1,
                                     "line": "UNKNOWN_OUTCOME handled by magic"}],
                        "files_scanned": 999, "truncated": False}
            return real(args)

        caps._REGISTRY["codebase_query"].execute = liar
        try:
            base = StateAwareProvider(store, "inv", "codebase_query",
                                      {"operation": "search", "pattern": "UNKNOWN_OUTCOME"},
                                      "verify_codebase_query")
            term, out = make_terminal(store, session, ClassifyProvider("work", base=base),
                                      ["find out", "/quit"])
            assert term.run() == 0
        finally:
            caps._REGISTRY["codebase_query"].execute = real
        joined = "".join(out)
        assert "OK: Task" not in joined
        assert "ERROR VERIFICATION_NOT_PASS" in joined
        t = store.read().execute("SELECT status FROM tasks").fetchone()
        assert t["status"] != "COMPLETED"
        ver = store.read().execute(
            "SELECT result FROM verifications WHERE step_id IS NOT NULL").fetchone()
        assert ver["result"] == "FAIL"
