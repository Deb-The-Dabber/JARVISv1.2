"""Live-loop adversarial tests (offline scripted provider).

These tests prove the NEW wiring in v5/live_loop.py — tool-schema generation
from the frozen adapter table, stage gating, the function-call-arguments-only
extraction path — handles every hostile/off-spec shape the contract requires,
including identity injection (§1c) driven all the way through the new wiring.

The scripted provider below emits hand-constructed *function-call* payloads
(the adversarial surface is the payload content, not provider identity). The
separate full-slice proof against a real deterministic provider is the live
run whose transcript is captured in the final report / committed artifact.
"""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

import pytest

from v5 import cognition, execution, live_loop, sessions, work
from v5.models import CompletionPolicy, Ok, Rejected


# ── scripted fake provider: emits provider-shaped function_call payloads ─────

@dataclass
class FakeFunctionCall:
    name: str
    args: dict


@dataclass
class FakePart:
    function_call: FakeFunctionCall | None = None
    text: str | None = None


@dataclass
class FakeContent:
    parts: list


@dataclass
class FakeCandidate:
    content: FakeContent


@dataclass
class FakeResponse:
    candidates: list


class ScriptedProvider:
    """Plays back a fixed script of (function_call | narration) turns."""

    model_name = "scripted-offline-adversary"

    def __init__(self, script: list[dict]):
        # script entries: {"calls": [(name, args), ...]} or {"text": "..."}
        self._script = script
        self.turns = 0

    def call(self, contents, tools, allowed_names):
        turn = self._script[min(self.turns, len(self._script) - 1)]
        self.turns += 1
        parts = []
        for name, args in turn.get("calls", []):
            parts.append(FakePart(function_call=FakeFunctionCall(name, args)))
        if "text" in turn:
            parts.append(FakePart(text=turn["text"]))
        return FakeResponse(candidates=[FakeCandidate(content=FakeContent(parts))])


@pytest.fixture
def store(tmp_path):
    from v5.store import Store
    s = Store(str(tmp_path / "state.db"))
    yield s
    s.close()


@pytest.fixture
def session(store):
    return sessions.create_session(store, cognition_authorized=True)


def _on(**kw):
    base = {"rule": "ALL_REQUIRED"}
    base.update(kw)
    return base


def _goal_args():
    return {"statement": "Create x", "completion_policy": _on()}


# ── §0 drift guard + schema generation from the frozen table ─────────────────

class TestFrozenTableIsSingleSource:
    def test_tool_schemas_derive_from_frozen_operation_fields(self):
        schemas = live_loop.tool_schemas()
        assert set(schemas) == set(cognition._OPERATION_FIELDS)
        for op, fields in cognition._OPERATION_FIELDS.items():
            assert set(schemas[op]["parameters"]["required"]) == set(fields["required"])
            assert set(schemas[op]["parameters"]["properties"]) == \
                set(fields["required"]) | set(fields["optional"])
        # step + verification sub-schemas mirror the frozen tables
        step_items = schemas["propose_plan"]["parameters"]["properties"]["steps"]["items"]
        assert set(step_items["required"]) == set(cognition._STEP_REQUIRED)
        vr_items = step_items["properties"]["verification_requirements"]["items"]
        assert set(vr_items["required"]) == set(cognition._VR_REQUIRED)

    def test_schema_generation_fails_loudly_on_unmapped_field(self, monkeypatch):
        """If the frozen table gains a field with no JSON-schema mapping, the
        generator raises (loud drift guard) instead of silently dropping it."""
        rogue = deepcopy(cognition._OPERATION_FIELDS)
        rogue["propose_goal"]["required"] += ("brand_new_field",)
        monkeypatch.setattr(live_loop, "_OPERATION_FIELDS", rogue)
        with pytest.raises(KeyError):
            live_loop.tool_schemas()


# ── §4 adversarial payload tests (through the new wiring) ────────────────────


class TestAdversarialPayloads:
    def test_missing_required_field_no_state_created(self, store, session, tmp_path):
        """propose_goal missing 'statement' → adapter rejects, nothing persists."""
        provider = ScriptedProvider([{
            "calls": [("propose_goal", {"completion_policy": _on()})],
        }])
        r = live_loop.run_live_slice(store, session.id, "x", str(tmp_path / "f.txt"), "c",
                                     provider)
        assert r["ok"] is False
        n = store.read().execute("SELECT COUNT(*) n FROM goals").fetchone()["n"]
        assert n == 0
        discard = [e for e in r["events"] if e["kind"] == "result"]
        assert any(e["detail"].get("reason") == "MALFORMED_PROPOSAL"
                   and "statement is required" in (e["detail"].get("detail") or "")
                   for e in discard if e["detail"].get("status") == "rejected")

    def test_unknown_field_invented_by_model_rejected(self, store, session, tmp_path):
        provider = ScriptedProvider([{
            "calls": [("propose_goal", {**_goal_args(), "trust_me": True})],
        }])
        r = live_loop.run_live_slice(store, session.id, "x", str(tmp_path / "f.txt"), "c",
                                     provider)
        assert r["ok"] is False
        assert store.read().execute("SELECT COUNT(*) n FROM goals").fetchone()["n"] == 0
        rej = [e for e in r["events"] if e["kind"] == "result" and
               e["detail"].get("status") == "rejected"]
        assert any("unknown field" in (e["detail"].get("detail") or "") for e in rej)

    def test_identity_injection_has_zero_effect(self, store, session, tmp_path):
        """§1c at the NEW boundary: model slips session_id/principal_id into
        the tool-call args — the new wiring must not let it reach anywhere;
        the membrane rejects it and the authenticated context is unchanged."""
        cognition.bind_store(store)   # host harness binding for the ctx assertions
        ctx_before = cognition.authenticate_session(session.id).value
        payload = {**_goal_args(), "session_id": "evil_session", "principal_id": "attacker"}
        provider = ScriptedProvider([{"calls": [("propose_goal", payload)]}])
        r = live_loop.run_live_slice(store, session.id, "x", str(tmp_path / "f.txt"), "c",
                                     provider)
        assert r["ok"] is False
        assert store.read().execute("SELECT COUNT(*) n FROM goals").fetchone()["n"] == 0
        rej = [e for e in r["events"] if e["kind"] == "result" and
               e["detail"].get("status") == "rejected"]
        assert any(e["detail"]["reason"] == "MALFORMED_PROPOSAL" for e in rej)
        ctx_after = cognition.current_context()
        assert ctx_after.session_id == ctx_before.session_id
        assert ctx_after.principal_id == ctx_before.principal_id

    def test_hallucinated_goal_id_rejected_not_found(self, store, session, tmp_path):
        """Correct first turn (goal created), then a hallucinated goal_id."""
        provider = ScriptedProvider([
            {"calls": [("propose_goal", _goal_args())]},
            {"calls": [("propose_task", {
                "goal_id": "goal_01THISISAHALLUCINATEDID000",
                "expected_goal_revision": 0, "statement": "x",
                "completion_policy": _on()})]},
        ])
        r = live_loop.run_live_slice(store, session.id, "x", str(tmp_path / "f.txt"), "c",
                                     provider)
        assert any(e["kind"] == "result" and e["detail"].get("status") == "ok"
                   and e["detail"].get("operation") == "propose_goal" for e in r["events"])
        rejs = [e for e in r["events"] if e["detail"].get("operation") == "propose_task"]
        assert any(e["detail"]["status"] == "rejected"
                   and e["detail"]["reason"] == "NOT_FOUND" for e in rejs)
        assert store.read().execute("SELECT COUNT(*) n FROM tasks").fetchone()["n"] == 0

    def test_wrong_capability_mismatch_rejected(self, store, session, tmp_path):
        """Model proposes delete_file for the file_write-declared Step
        ('if your harness can force this' — the scripted adversary forces it)."""
        target = str(tmp_path / "cap.txt")
        provider = ScriptedProvider([
            {"calls": [("propose_goal", _goal_args())]},
            {"calls": [], "text": None},   # placeholder; task stage built dynamically below
        ])
        # turn 1: goal. then recompute ids dynamically isn't possible in a
        # static script, so read state AFTER the goal lands via a "dynamic"
        # provider that consults the store between turns.
        class Dyn(ScriptedProvider):
            def call(self, contents, tools, allowed_names):
                turn = self.turns
                self.turns += 1
                if turn == 0:
                    parts = [FakePart(function_call=FakeFunctionCall(
                        "propose_goal", _goal_args()))]
                elif turn == 1:
                    g = store.read().execute("SELECT * FROM goals LIMIT 1").fetchone()
                    parts = [FakePart(function_call=FakeFunctionCall("propose_task", {
                        "goal_id": g["id"], "expected_goal_revision": g["revision"],
                        "statement": "do it", "completion_policy": _on()}))]
                elif turn == 2:
                    t = store.read().execute("SELECT * FROM tasks LIMIT 1").fetchone()
                    parts = [FakePart(function_call=FakeFunctionCall("propose_plan", {
                        "task_id": t["id"], "expected_task_revision": t["revision"],
                        "steps": [{"description": "write", "required": True,
                                   "depends_on_index": [],
                                   "execution_capability": "file_write",
                                   "verification_requirements": [
                                       {"method_name": "verify_file_write",
                                        "applies_to_capability": "file_write"}]}]}))]
                else:
                    s = store.read().execute("SELECT * FROM steps LIMIT 1").fetchone()
                    parts = [FakePart(function_call=FakeFunctionCall("propose_action", {
                        "step_id": s["id"], "expected_step_revision": s["revision"],
                        "capability": "delete_file",   # ← adversarial deviation
                        "arguments": {"path": target}}))]
                return FakeResponse(candidates=[FakeCandidate(content=FakeContent(parts))])
        r = live_loop.run_live_slice(store, session.id, "x", target, "c", Dyn([]))
        rejs = [e for e in r["events"] if e["detail"].get("operation") == "propose_action"
                and e["detail"].get("status") == "rejected"]
        assert any(e["detail"]["reason"] == "CAPABILITY_MISMATCH" for e in rejs)
        # no Action, no file, step untouched
        assert store.read().execute("SELECT COUNT(*) n FROM actions").fetchone()["n"] == 0
        assert not Path(target).exists()

    def test_narration_only_turn_halts_cleanly(self, store, session, tmp_path):
        """The model narrates instead of tool-calling — twice — and the loop
        discards the prose and halts cleanly (never scrapes it into JSON)."""
        provider = ScriptedProvider([
            {"text": "I'll call file_write now and create the file."},
            {"text": "Writing the file — done!"},
        ])
        r = live_loop.run_live_slice(store, session.id, "x", str(tmp_path / "f.txt"), "c",
                                     provider)
        assert r["ok"] is False
        assert "halted" in r["reason"] or "narrat" in r["reason"]
        assert store.read().execute("SELECT COUNT(*) n FROM goals").fetchone()["n"] == 0
        assert not (tmp_path / "f.txt").exists()
        narr = [e for e in r["events"] if e["kind"] == "narration_discarded"]
        assert len(narr) == 2   # both prose turns discarded as no-proposal

    def test_same_turn_duplicate_tool_calls_deduped(self, store, session, tmp_path):
        """Two identical tool calls in ONE emission are one proposal (§26)."""
        provider = ScriptedProvider([
            {"calls": [("propose_goal", _goal_args()),
                       ("propose_goal", _goal_args())]},
        ])
        r = live_loop.run_live_slice(store, session.id, "x", str(tmp_path / "f.txt"), "c",
                                     provider)
        # exactly ONE goal committed despite two identical calls in one turn
        assert store.read().execute("SELECT COUNT(*) n FROM goals").fetchone()["n"] == 1


# ── offline happy-path: scripted-but-well-formed call sequence ═══════════════

class TestLoopMechanicsOffline:
    def test_full_chain_through_scripted_provider(self, store, session, tmp_path):
        """Proves the orchestration mechanics (not the model): each stage's
        real ids/revisions feed the next, and the host execution boundary runs."""
        target = tmp_path / "offline.txt"
        content = "offline proof"
        class WellFormed(ScriptedProvider):
            def call(self, contents, tools, allowed_names):
                turn = self.turns
                self.turns += 1
                c = store.read()
                if turn == 0:
                    name, args = "propose_goal", {"statement": f"Create {target} containing {content}.",
                                                  "completion_policy": _on()}
                elif turn == 1:
                    g = c.execute("SELECT * FROM goals LIMIT 1").fetchone()
                    name, args = "propose_task", {"goal_id": g["id"],
                                                  "expected_goal_revision": g["revision"],
                                                  "statement": "write it",
                                                  "completion_policy": _on()}
                elif turn == 2:
                    t = c.execute("SELECT * FROM tasks LIMIT 1").fetchone()
                    name, args = "propose_plan", {"task_id": t["id"],
                                                  "expected_task_revision": t["revision"],
                                                  "steps": [{
                                                      "description": f"Create {target}",
                                                      "required": True, "depends_on_index": [],
                                                      "execution_capability": "file_write",
                                                      "verification_requirements": [
                                                          {"method_name": "verify_file_write",
                                                           "applies_to_capability": "file_write"}]}]}
                else:
                    s = c.execute("SELECT * FROM steps LIMIT 1").fetchone()
                    name, args = "propose_action", {"step_id": s["id"],
                                                    "expected_step_revision": s["revision"],
                                                    "capability": "file_write",
                                                    "arguments": {"path": str(target),
                                                                  "content": content}}
                return FakeResponse(candidates=[FakeCandidate(content=FakeContent(
                    [FakePart(function_call=FakeFunctionCall(name, args))]))])
        r = live_loop.run_live_slice(store, session.id,
                                     live_loop.MILESTONE_INSTRUCTION_TEMPLATE.format(
                                         path=str(target), content=content),
                                     str(target), content, WellFormed([]))
        assert r["ok"] is True, r.get("reason")
        assert r["final"]["file_exists"] and r["final"]["file_bytes_match"]
        assert r["final"]["task_status"] == "COMPLETED"
        assert Path(target).read_text() == content
