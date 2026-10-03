"""Path Resolution / Workspace v1 tests.

Pins the host-owned path policy:
  * bare filenames / relative paths / "./relative" / nested paths resolve
    deterministically inside the canonical JARVIS workspace;
  * absolute paths INSIDE the workspace are preserved as-is; absolute paths
    OUTSIDE it are natively rejected (proposal time AND execute time);
  * ".." traversal that escapes the workspace is natively rejected;
    traversal that stays inside is normalized and allowed;
  * symlink-based workspace escape is caught (realpath containment);
  * empty/whitespace/NUL/non-string/oversize paths are natively rejected;
  * file_write/file_read/file_delete use the resolver; the registered
    verifiers resolve the canonical args independently (never the
    capability's self-report);
  * the /helloworld.txt regression: a model-invented absolute root path is
    rejected NATIVELY at proposal validation — never an OS read-only error;
  * confirmation still gates file_write and the terminal shows the resolved
    target.

The autouse conftest fixture points JARVIS_V5_WORKSPACE at tmp_path, so
workspace_root() is tmp_path in every test here.
"""
from __future__ import annotations

import os
import pathlib

import pytest

from v5 import capabilities as caps
from v5 import paths, sessions, verification
from v5.models import Ok, Rejected
from v5.store import Store
from v5.terminal import InteractiveTerminal
from tests.test_conversational_boundary import ClassifyProvider
from tests.test_terminal import (StateAwareProvider, count, make_inputs,
                                  make_terminal)
from tests.test_live_loop import (FakeCandidate, FakeContent, FakeFunctionCall,
                                  FakePart, FakeResponse)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "state.db"))
    yield s
    s.close()


@pytest.fixture
def session(store):
    return sessions.create_session(store, cognition_authorized=True)


def resolve(raw):
    return paths.resolve_workspace_path(raw)


# ── resolution unit tests ──────────────────────────────────────────────────

class TestResolution:
    def test_bare_filename(self, tmp_path):
        p, err = resolve("helloworld.txt")
        assert err is None and p == tmp_path / "helloworld.txt"

    def test_relative_path(self, tmp_path):
        p, err = resolve("notes/hello.txt")
        assert err is None and p == tmp_path / "notes" / "hello.txt"

    def test_dot_slash_relative(self, tmp_path):
        p, err = resolve("./notes/hello.txt")
        assert err is None and p == tmp_path / "notes" / "hello.txt"

    def test_nested_relative(self, tmp_path):
        p, err = resolve("projects/jarvis/test.txt")
        assert err is None and p == tmp_path / "projects" / "jarvis" / "test.txt"

    def test_absolute_inside_workspace_preserved(self, tmp_path):
        target = tmp_path / "already.txt"
        p, err = resolve(str(target))
        assert err is None and p == target

    def test_absolute_outside_workspace_rejected(self):
        _, err = resolve("/etc/passwd")
        assert err is not None and "outside the JARVIS workspace" in err

    def test_root_level_absolute_rejected(self):
        """The original /helloworld.txt failure mode — natively rejected."""
        _, err = resolve("/helloworld.txt")
        assert err is not None and "outside the JARVIS workspace" in err

    def test_parent_traversal_rejected(self):
        _, err = resolve("../outside.txt")
        assert err is not None and "escapes the JARVIS workspace" in err

    def test_deep_parent_traversal_rejected(self):
        _, err = resolve("../../etc/passwd")
        assert err is not None and "escapes the JARVIS workspace" in err

    def test_mixed_traversal_rejected(self):
        _, err = resolve("foo/../../outside.txt")
        assert err is not None and "escapes the JARVIS workspace" in err

    def test_traversal_staying_inside_allowed(self, tmp_path):
        (tmp_path / "notes").mkdir()
        p, err = resolve("notes/../today.txt")
        assert err is None and p == tmp_path / "today.txt"

    def test_empty_path(self):
        _, err = resolve("")
        assert err is not None

    def test_whitespace_path(self):
        _, err = resolve("   ")
        assert err is not None

    def test_nul_byte_path(self):
        _, err = resolve("bad\x00name.txt")
        assert err is not None and "NUL" in err

    def test_non_string_path(self):
        _, err = resolve(42)
        assert err is not None

    def test_oversize_path(self):
        _, err = resolve("a" * 5000)
        assert err is not None

    def test_workspace_root_itself_rejected(self, tmp_path):
        _, err = resolve(".")
        assert err is not None and "workspace root" in err

    def test_deterministic_repeated_resolution(self, tmp_path):
        p1, _ = resolve("notes/today.txt")
        p2, _ = resolve("notes/today.txt")
        assert p1 == p2

    def test_home_tilde_absolute_rejected(self):
        # "~/x" expands to an absolute home path — outside the workspace
        _, err = resolve("~/escape.txt")
        assert err is not None

    def test_symlink_escape_rejected(self, tmp_path):
        """workspace/link -> /tmp (outside) must not smuggle a write out."""
        outside = tmp_path.parent / "symlink_escape_target"
        outside.mkdir(exist_ok=True)
        link = tmp_path / "escape_link"
        os.symlink(outside, link)
        try:
            _, err = resolve("escape_link/smuggled.txt")
            assert err is not None  # either escape or symlink policy reason
        finally:
            link.unlink()

    def test_symlink_inside_workspace_allowed(self, tmp_path):
        real = tmp_path / "real_dir"
        real.mkdir()
        link = tmp_path / "alias_dir"
        os.symlink(real, link)
        try:
            p, err = resolve("alias_dir/file.txt")
            assert err is None and p == real / "file.txt"
        finally:
            link.unlink()

    def test_workspace_env_override(self, tmp_path, monkeypatch):
        sub = tmp_path / "other_ws"
        monkeypatch.setenv("JARVIS_V5_WORKSPACE", str(sub))
        p, err = resolve("file.txt")
        assert err is None and p == sub / "file.txt"
        # creation is opt-in; resolution itself is side-effect-free
        assert not sub.exists()

    def test_workspace_creation_opt_in(self, tmp_path, monkeypatch):
        sub = tmp_path / "created_ws"
        monkeypatch.setenv("JARVIS_V5_WORKSPACE", str(sub))
        assert not sub.exists()
        root = paths.workspace_root(create=True)
        assert root == sub.resolve() and sub.exists()


# ── capability integration ──────────────────────────────────────────────────

class TestFileWriteIntegration:
    def test_write_bare_filename_lands_in_workspace(self, tmp_path):
        out = caps._file_write({"path": "helloworld.txt", "content": "Hello World"})
        target = tmp_path / "helloworld.txt"
        assert target.read_text() == "Hello World"
        assert out["path"] == str(target)
        assert out["bytes_written"] == 11

    def test_write_nested_relative_creates_parents(self, tmp_path):
        caps._file_write({"path": "notes/deep/today.txt", "content": "x"})
        assert (tmp_path / "notes" / "deep" / "today.txt").read_text() == "x"

    def test_write_absolute_outside_rejected_at_validate(self):
        spec = caps.get("file_write")
        err = spec.validate_args({"path": "/helloworld.txt", "content": ""})
        assert err is not None and "outside the JARVIS workspace" in err

    def test_write_traversal_rejected_at_validate(self):
        err = caps.get("file_write").validate_args({"path": "../escape.txt",
                                                    "content": ""})
        assert err is not None and "escapes the JARVIS workspace" in err

    def test_write_absolute_outside_rejected_at_execute(self):
        with pytest.raises(caps.DefiniteNoEffect):
            caps._file_write({"path": "/helloworld.txt", "content": ""})

    def test_repo_guard_retained_for_workspace_inside_repo(self, tmp_path, monkeypatch):
        """If someone configures the workspace inside the V5 repository's
        guarded region (the v5 package tree), the self-protection guard
        still fires."""
        import v5.capabilities as capmod
        guarded = pathlib.Path(capmod.__file__).resolve().parent.parent.resolve()
        ws = guarded / "ws_inside_repo"
        monkeypatch.setenv("JARVIS_V5_WORKSPACE", str(ws))
        try:
            with pytest.raises(caps.DefiniteNoEffect) as exc:
                caps._file_write({"path": "x.txt", "content": ""})
            assert "refusing to write inside the V5 repo" in str(exc.value)
        finally:
            monkeypatch.delenv("JARVIS_V5_WORKSPACE")
            if ws.exists():
                for f in ws.iterdir():
                    f.unlink()
                ws.rmdir()


class TestFileReadIntegration:
    def test_read_workspace_relative(self, tmp_path):
        (tmp_path / "readable.txt").write_text("read me")
        out = caps._file_read({"path": "readable.txt"})
        assert out["content"] == "read me"
        assert out["path"] == str(tmp_path / "readable.txt")

    def test_read_missing_file_failed_not_unknown(self, tmp_path):
        with pytest.raises(caps.DefiniteNoEffect):
            caps._file_read({"path": "nope.txt"})

    def test_read_absolute_outside_rejected(self):
        with pytest.raises(caps.DefiniteNoEffect):
            caps._file_read({"path": "/etc/passwd"})


class TestFileDeleteIntegration:
    def test_delete_workspace_relative(self, tmp_path):
        (tmp_path / "victim.txt").write_text("gone")
        out = caps._file_delete({"path": "victim.txt"})
        assert out["deleted"] and not (tmp_path / "victim.txt").exists()

    def test_delete_outside_rejected(self):
        with pytest.raises(caps.DefiniteNoEffect):
            caps._file_delete({"path": "/etc/hosts"})


# ── verification independence ───────────────────────────────────────────────

class TestVerificationIndependence:
    def _run_verifier(self, store, tmp_path, args):
        """Write an action row + run the registered verifier for it."""
        import json
        from v5.ids import new_id
        action_id = new_id("action")
        with store.write() as conn:
            conn.execute(
                "INSERT INTO actions (id, revision, step_id, status, capability, "
                "arguments, idempotency_class, confirmation_id, retry_of) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (action_id, 0, "step_x", "OBSERVED", "file_write",
                 json.dumps(args), "IDEMPOTENT", None, None))
        spec = verification.get_method("verify_file_write")
        ev = spec.make_evaluator(store, action_id)
        return ev(claim_row=None, evidence_rows=[])

    def test_verifier_resolves_workspace_relative(self, store, tmp_path):
        (tmp_path / "verified.txt").write_text("exact bytes")
        result = self._run_verifier(store, tmp_path,
                                    {"path": "verified.txt", "content": "exact bytes"})
        assert result is True

    def test_verifier_catches_wrong_bytes_at_resolved_target(self, store, tmp_path):
        (tmp_path / "liar.txt").write_text("wrong bytes")
        result = self._run_verifier(store, tmp_path,
                                    {"path": "liar.txt", "content": "expected bytes"})
        assert result is False

    def test_verifier_fails_on_missing_target(self, store, tmp_path):
        result = self._run_verifier(store, tmp_path,
                                    {"path": "absent.txt", "content": "x"})
        assert result is False

    def test_verifier_inconclusive_on_policy_rejected_path(self, store, tmp_path):
        result = self._run_verifier(store, tmp_path,
                                    {"path": "/helloworld.txt", "content": "x"})
        assert result is None

    def test_verify_file_read_resolves_independently(self, store, tmp_path):
        import json
        from v5.ids import new_id
        (tmp_path / "r.txt").write_text("truth")
        action_id = new_id("action")
        obs_id = new_id("observation")
        with store.write() as conn:
            conn.execute(
                "INSERT INTO actions (id, revision, step_id, status, capability, "
                "arguments, idempotency_class, confirmation_id, retry_of) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (action_id, 0, "step_x", "OBSERVED", "file_read",
                 json.dumps({"path": "r.txt"}), "IDEMPOTENT", None, None))
            conn.execute(
                "INSERT INTO observations (id, action_id, captured_at, raw_result, "
                "execution_source) VALUES (?,?,?,?,?)",
                (obs_id, action_id, "t", json.dumps({"content": "LIE"}), "file_read"))
        spec = verification.get_method("verify_file_read")
        ev = spec.make_evaluator(store, action_id)
        # the observation CLAIMS "LIE" — the verifier reads the real file: FAIL
        assert ev(None, []) is False

    def test_verify_file_delete_resolves_independently(self, store, tmp_path):
        import json
        from v5.ids import new_id
        action_id = new_id("action")
        with store.write() as conn:
            conn.execute(
                "INSERT INTO actions (id, revision, step_id, status, capability, "
                "arguments, idempotency_class, confirmation_id, retry_of) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (action_id, 0, "step_x", "OBSERVED", "file_delete",
                 json.dumps({"path": "still_here.txt"}), "IDEMPOTENT", None, None))
        (tmp_path / "still_here.txt").write_text("present")
        spec = verification.get_method("verify_file_delete")
        ev = spec.make_evaluator(store, action_id)
        assert ev(None, []) is False      # file still exists -> deletion NOT established
        (tmp_path / "still_here.txt").unlink()
        assert ev(None, []) is True


# ── end-to-end through the terminal (scripted provider) ─────────────────────

class TestEndToEndWorkspaceFlow:
    def _work_chain_provider(self, store, capability, args, verifier):
        return StateAwareProvider(store, f"do it", capability, args, verifier)

    def test_helloworld_end_to_end(self, store, session, tmp_path):
        """The §12 flow: 'create a file named helloworld.txt' — model proposes
        the BARE filename (as the user said), host resolves, confirmation
        shows the target, y executes, verification independently passes,
        Task COMPLETED, file exists with exact bytes."""
        p = StateAwareProvider(store, "create helloworld.txt", "file_write",
                               {"path": "helloworld.txt", "content": "Hello World"},
                               "verify_file_write")
        classify = ClassifyProvider("work", base=p)
        term, out = make_terminal(store, session, classify,
                                  ["create a file named helloworld.txt", "y", "/quit"])
        assert term.run() == 0
        joined = "".join(out)

        # confirmation intact AND shows the resolved target
        assert "CONFIRMATION_REQUIRED" in joined
        assert 'action: file_write {"content": "Hello World", "path": "helloworld.txt"}' in joined
        assert f"target: {tmp_path / 'helloworld.txt'}" in joined
        # completion + real file with exact bytes
        assert "OK: Task COMPLETED" in joined
        target = tmp_path / "helloworld.txt"
        assert target.read_text() == "Hello World"
        # the registered verifier PASSED (independently resolved)
        ver = store.read().execute(
            "SELECT result FROM verifications WHERE step_id IS NOT NULL").fetchone()
        assert ver["result"] == "PASS"

    def test_read_back_resolves_workspace_relative(self, store, session, tmp_path):
        (tmp_path / "helloworld.txt").write_text("Hello World")
        p = StateAwareProvider(store, "read helloworld.txt", "file_read",
                               {"path": "helloworld.txt"}, "verify_file_read")
        classify = ClassifyProvider("work", base=p)
        term, out = make_terminal(store, session, classify,
                                  ["read helloworld.txt", "/quit"])
        assert term.run() == 0
        joined = "".join(out)
        assert "OK: Task COMPLETED" in joined
        # no root-level accidental path was ever touched
        assert not pathlib.Path("/helloworld.txt").exists()

    def test_denial_still_prevents_execution(self, store, session, tmp_path):
        p = StateAwareProvider(store, "write it", "file_write",
                               {"path": "denied.txt", "content": "x"},
                               "verify_file_write")
        classify = ClassifyProvider("work", base=p)
        term, out = make_terminal(store, session, classify,
                                  ["write it", "n", "/quit"])
        assert term.run() == 0
        joined = "".join(out)
        assert "DECLINED" in joined
        assert not (tmp_path / "denied.txt").exists()

    def test_model_invented_absolute_path_native_rejection(self, store, session, tmp_path):
        """§14: if the model still proposes '/helloworld.txt', the host
        rejects it NATIVELY at proposal validation — the loop feeds the
        rejection back, no OS read-only error ever occurs."""
        class AbsoluteProvider(StateAwareProvider):
            def call(self, contents, tools, allowed_names):
                resp = super().call(contents, tools, allowed_names)
                # force absolute paths in every propose_action
                call = resp.candidates[0].content.parts[0].function_call
                if call.name == "propose_action":
                    call.args["arguments"] = {"path": "/helloworld.txt",
                                              "content": "Hello World"}
                return resp

        p = AbsoluteProvider(store, "create a file", "file_write",
                              {"path": "helloworld.txt", "content": "Hello World"},
                              "verify_file_write")
        classify = ClassifyProvider("work", base=p)
        term, out = make_terminal(store, session, classify,
                                  ["create a file named helloworld.txt", "/quit"])
        assert term.run() == 0
        joined = "".join(out)
        # the native rejection surfaced (never an OSError/UNKNOWN_OUTCOME)
        assert ("ERROR too many membrane rejections" in joined
                or "absolute paths outside the JARVIS workspace" in joined)
        assert "UNKNOWN_OUTCOME" not in joined
        assert "Read-only file system" not in joined
        assert count(store, "observations") == 0
        assert not pathlib.Path("/helloworld.txt").exists()
