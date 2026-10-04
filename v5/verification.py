"""Verification method registry (Cognition Implementation Contract v1.1 §28).

Every verification method Cognition can name in a StepProposal must resolve
against this registry. Unknown method names are rejected at proposal time —
the model cannot invent verifier names and have something dynamically import
or "best-effort" them.

The registry maps method_name -> how to build the deterministic evaluator for
a given Action. Evaluators are built around the verifier's INDEPENDENT truth
source (§34/§35):

  * expectations (what SHOULD be true) come from the Action's canonical
    arguments — the user-approved request side;
  * truth (what IS true) comes from an independent re-read of the filesystem;
  * the evaluator NEVER consults the Observation's raw_result, the Action's
    status, any capability success message, or any LLM judgment.

Only evidence.run_verification (the established execution path) may persist a
result — this module provides evaluators, never a result-writing shortcut.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from v5 import paths as _paths
from v5.store import Store


class VerificationFabricationError(Exception):
    pass


@dataclass(frozen=True)
class VerificationMethodSpec:
    name: str
    applies_to_capability: str
    # (store, action_id) -> evaluator(claim_row, evidence_rows) -> bool | None
    make_evaluator: Callable


_METHODS: dict[str, VerificationMethodSpec] = {}


def register_method(spec: VerificationMethodSpec) -> None:
    _METHODS[spec.name] = spec


def get_method(name: str) -> VerificationMethodSpec | None:
    return _METHODS.get(name)


def registered_methods() -> tuple[str, ...]:
    return tuple(sorted(_METHODS))


# ── verify_file_write (§34/§35): deterministic, filesystem-grounded ─────────

def _make_verify_file_write_evaluator(store: Store, action_id: str):
    """Evaluator factory for verify_file_write. Reads the Action's canonical
    arguments (the REQUEST side: what was asked to be written) and compares
    against actual current filesystem bytes (the TRUTH side). Never reads the
    Observation.

    Returns: True  -> PASS   (path exists, is a file, bytes match exactly)
             False -> FAIL   (path missing / not a file / content mismatch)
             None  -> INCONCLUSIVE (cannot even attempt the read)"""
    def _load_args() -> dict | None:
        r = store.read().execute(
            "SELECT arguments FROM actions WHERE id = ?", (action_id,)
        ).fetchone()
        if r is None:
            return None
        import json
        return json.loads(r["arguments"])

    expected = _load_args()

    def evaluate(claim_row, evidence_rows) -> bool | None:
        if expected is None:
            return None
        path_raw = expected.get("path")
        content = expected.get("content", "")
        if not isinstance(path_raw, str) or not isinstance(content, str):
            return None
        # Path Resolution v1: the verifier resolves the canonical args
        # through the SAME host policy — never the capability's self-report.
        # A policy-rejected path is INCONCLUSIVE here (nothing executed for
        # the verifier to inspect).
        path, path_err = _paths.resolve_workspace_path(path_raw)
        if path_err is not None:
            return None
        try:
            if not path.exists():
                return False
            if not path.is_file():
                return False
            actual = path.read_bytes()
        except OSError:
            return None  # genuinely inconclusive — we could not read
        return actual == content.encode("utf-8")

    return evaluate


register_method(VerificationMethodSpec(
    name="verify_file_write",
    applies_to_capability="file_write",
    make_evaluator=_make_verify_file_write_evaluator,
))


# ── verify_file_read: same structural shape, different polarity ──────────────
# verify_file_write compares "what the Action asked to write" (request side)
# against actual filesystem bytes (truth side). verify_file_read compares
# "what the capability's Observation CLAIMS it read" (claim side) against the
# actual current filesystem bytes the verifier reads itself (truth side).
# A fabricated Observation cannot pass: the only way to PASS is for the
# claimed bytes to match what is genuinely on disk right now.

def _make_verify_file_read_evaluator(store: Store, action_id: str):
    """Evaluator factory for verify_file_read.

    PASS  = file exists and its current bytes equal the Observation's claimed
            content (the capability reads truthfully),
    FAIL  = file missing, or actual bytes differ from the claim (fabricated
            or stale Observation),
    None  = INCONCLUSIVE (the verifier itself could not read the file at all,
            e.g. permissions changed mid-flight).
    The claim is taken from the committed Observation row for this Action —
    the only legitimate carrier of "what the capability claims" (Law 9/29)."""
    row = store.read().execute(
        "SELECT arguments FROM actions WHERE id = ?", (action_id,)
    ).fetchone()
    expected_args = None
    if row is not None:
        import json as _json
        expected_args = _json.loads(row["arguments"])

    obs_row = store.read().execute(
        "SELECT raw_result FROM observations WHERE action_id = ?", (action_id,)
    ).fetchone()
    claimed = None
    if obs_row is not None:
        import json as _json
        claimed = _json.loads(obs_row["raw_result"])

    def evaluate(claim_row, evidence_rows) -> bool | None:
        if expected_args is None:
            return None
        if claimed is None:
            # Emission Is Not Occurrence gate should have caught this (a
            # non-OBSERVED Action has no Observation); defensive None.
            return None
        path_raw = expected_args.get("path")
        if not isinstance(path_raw, str) or not path_raw:
            return None
        claimed_content = claimed.get("content")
        if not isinstance(claimed_content, str):
            # capability did not actually report a content claim — a
            # claim-less observation cannot prove anything
            return False
        # Path Resolution v1: independent resolution via the host policy
        path, path_err = _paths.resolve_workspace_path(path_raw)
        if path_err is not None:
            return None
        try:
            if not path.exists():
                return False
            if not path.is_file():
                return False
            actual = path.read_bytes()
        except OSError:
            return None  # genuinely inconclusive — verifier itself failed to read
        return actual == claimed_content.encode("utf-8")

    return evaluate


register_method(VerificationMethodSpec(
    name="verify_file_read",
    applies_to_capability="file_read",
    make_evaluator=_make_verify_file_read_evaluator,
))


# ── verify_file_delete: independent absence-of-target check ──────────────────
# The verifier does NOT trust the capability's return value, the Action status,
# the Observation, or any model claim. It loads the target path from the
# Action's canonical arguments and inspects the LIVE filesystem itself:
# PASS only when the target is actually gone; the target still existing is
# FAIL. (That the target existed before execution is a harness precondition
# of the deletion flow — the capability refuses to run on a missing file via
# DefiniteNoEffect, so an OBSERVED deletion only happens for a real target.)

def _make_verify_file_delete_evaluator(store: Store, action_id: str):
    """Evaluator factory for verify_file_delete.

    PASS  = independent filesystem inspection finds the target absent,
    FAIL  = the target still exists (deletion not established — this is what
            catches a sabotaged capability that reports success without
            deleting),
    None  = INCONCLUSIVE (the verifier itself could not inspect)."""
    row = store.read().execute(
        "SELECT arguments FROM actions WHERE id = ?", (action_id,)
    ).fetchone()
    expected_args = None
    if row is not None:
        import json as _json
        expected_args = _json.loads(row["arguments"])

    def evaluate(claim_row, evidence_rows) -> bool | None:
        if expected_args is None:
            return None
        path_raw = expected_args.get("path")
        if not isinstance(path_raw, str) or not path_raw:
            return None
        # Path Resolution v1: independent resolution via the host policy
        path, path_err = _paths.resolve_workspace_path(path_raw)
        if path_err is not None:
            return None
        try:
            return not path.exists()
        except OSError:
            return None  # genuinely inconclusive — verifier could not inspect

    return evaluate


register_method(VerificationMethodSpec(
    name="verify_file_delete",
    applies_to_capability="file_delete",
    make_evaluator=_make_verify_file_delete_evaluator,
))


# ── verify_codebase_query: independent re-computation (Investigation v1) ────
# The verifier NEVER trusts the capability's return value or the Observation
# text. It loads the Action's CANONICAL arguments, recomputes the ground truth
# itself using the same shared measurement instrument the capability used
# (_source_read / _source_search — the bounded, deterministic primitives), and
# compares that ground truth against the Observation's CLAIMED result. A
# fabricated or misleading capability result (wrong content, invented match
# lines, hidden matches) cannot PASS: the recomputation disagrees.

def _make_verify_codebase_query_evaluator(store: Store, action_id: str):
    """PASS  = the independently recomputed result equals the Observation's
              claimed result (the capability reported the codebase truth),
    FAIL  = the claim disagrees with the recomputed ground truth, or the
            claim cannot be reproduced at all,
    None  = INCONCLUSIVE (args missing/malformed, or an OSError during the
            recomputation)."""
    import json as _json
    from v5.capabilities import DefiniteNoEffect, _source_read, _source_search

    row = store.read().execute(
        "SELECT arguments FROM actions WHERE id = ?", (action_id,)
    ).fetchone()
    expected_args = _json.loads(row["arguments"]) if row is not None else None

    obs_row = store.read().execute(
        "SELECT raw_result FROM observations WHERE action_id = ?", (action_id,)
    ).fetchone()
    claimed = _json.loads(obs_row["raw_result"]) if obs_row is not None else None

    def _claim_fields(claim):
        if not isinstance(claim, dict):
            return None
        if claim.get("operation") == "read_file":
            return ("read_file",
                    (claim.get("rel_path"), claim.get("content"),
                     bool(claim.get("truncated"))))
        if claim.get("operation") == "search":
            matches = claim.get("matches")
            if not isinstance(matches, list):
                return None
            norm = [tuple(sorted(m.items())) if isinstance(m, dict) else None
                    for m in matches]
            return ("search", matches, claim.get("files_scanned"),
                    bool(claim.get("truncated")))
        return None

    def evaluate(claim_row, evidence_rows) -> bool | None:
        if expected_args is None or claimed is None:
            return None
        op = expected_args.get("operation")
        try:
            if op == "read_file":
                p, err = _paths.resolve_source_path(expected_args.get("path"))
                if err is not None:
                    return None
                ground = _source_read(p)
                claim = _claim_fields(claimed)
                if claim is None or claim[0] != "read_file":
                    return False
                return (claim[1] == (ground["rel_path"], ground["content"],
                                     ground["truncated"]))
            pattern = expected_args.get("pattern")
            if not isinstance(pattern, str):
                return None
            ground = _source_search(pattern, expected_args.get("file_glob", "*.py"))
            claim = _claim_fields(claimed)
            if claim is None or claim[0] != "search":
                return False
            g_matches = [{"file": m["file"], "line_no": m["line_no"],
                          "line": m["line"]} for m in ground["matches"]]
            c_matches = [{k: m.get(k) for k in ("file", "line_no", "line")}
                         if isinstance(m, dict) else None for m in claim[1]]
            return (c_matches == g_matches
                    and claim[2] == ground["files_scanned"]
                    and claim[3] == ground["truncated"])
        except DefiniteNoEffect:
            # ground truth could not be recomputed (file vanished etc.) —
            # the claim is not reproducible, so it cannot PASS
            return False
        except OSError:
            return None  # genuinely inconclusive

    return evaluate


register_method(VerificationMethodSpec(
    name="verify_codebase_query",
    applies_to_capability="codebase_query",
    make_evaluator=_make_verify_codebase_query_evaluator,
))


def run_method_for_action(store: Store, verification_id: str, action_id: str):
    """Resolve a requirement-bound Verification's registered method and run it
    through the established run_verification path against a specific executed
    Action. The ONLY way a PASS can come to exist; this helper never writes
    results itself — it composes the registered evaluator with the canonical
    run_verification path.

    The underlying run_verification enforces the Emission Is Not Occurrence
    gate in its own transaction: the Action must be OBSERVED (execution
    actually happened and produced an Observation) and, for requirement-bound
    verifications, must belong to the Step the requirement was declared on.
    A never-executed PENDING Action — or any FAILED/UNKNOWN_OUTCOME one — is
    rejected, not verified. An unrelated Action (different Step) is rejected,
    not laundered through matching args."""
    from v5 import evidence
    ver = evidence.load_verification(store, verification_id)
    if ver is None:
        from v5.models import Rejected, R_NOT_FOUND
        return Rejected(R_NOT_FOUND, f"verification {verification_id}", None)
    spec = get_method(ver.method)
    if spec is None:
        from v5.models import Rejected
        return Rejected("UNKNOWN_VERIFICATION_METHOD", f"method {ver.method}", None)
    evaluator = spec.make_evaluator(store, action_id)
    return evidence.run_verification(store, verification_id, evaluator,
                                     for_action_id=action_id)
