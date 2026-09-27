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

import pathlib
from dataclasses import dataclass
from typing import Callable

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
        path = pathlib.Path(path_raw).expanduser().resolve()
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
        path = pathlib.Path(path_raw).expanduser().resolve()
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
