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


def run_method_for_action(store: Store, verification_id: str, action_id: str):
    """Resolve a requirement-bound Verification's registered method and run it
    through the established run_verification path against a specific executed
    Action. The ONLY way a PASS can come to exist; this helper never writes
    results itself — it composes the registered evaluator with the canonical
    run_verification path."""
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
    return evidence.run_verification(store, verification_id, evaluator)
