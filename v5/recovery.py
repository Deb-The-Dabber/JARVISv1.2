"""Recovery — startup/crash handling.

Constitution (Law 30): Recovery is NOT a fifth authority. It uses the SAME
transition contracts a live process would use: an EXECUTING Action
discovered after a crash transitions to UNKNOWN_OUTCOME through the ordinary
contracted transition (EXECUTING -> UNKNOWN_OUTCOME is legal; EXECUTING ->
OBSERVED without evidence is not), and the obligation is created through the
normal path. Recovery gets none of repair's latitude.
"""
from __future__ import annotations

from v5.enums import ActionStatus, VerificationResult
from v5.models import Ok, Rejected, Result, R_NOT_FOUND
from v5.store import Store


def recover(store: Store) -> dict:
    """Idempotent startup recovery. Runs the ordinary transitions:

    1. Actions stuck in EXECUTING (crash between pre-effect persistence and
       observation) -> UNKNOWN_OUTCOME via the contracted transition, with an
       OPEN obligation created for each (Law 9: uncertain outcome is never
       guessed; Law 8/9 crash points 2-5).
    2. INTEGRITY_VIOLATION detection: any Observation row whose Action is not
       OBSERVED is an event/state mismatch (should be impossible — crash
       point 5 makes them one transaction — but detect and freeze rather than
       silently continuing). Law 5: freeze, preserve both representations.

    Verifications left in RUNNING are left as-is: PENDING/RUNNING are the
    contract-allowed post-crash states (crash point 6) and completion
    requires PASS, so an interrupted claim is never treated as established.
    """
    conn = store.read()
    stuck = [
        r["id"] for r in conn.execute(
            "SELECT id FROM actions WHERE status = 'EXECUTING'"
        ).fetchall()
    ]
    recovered_actions: list[str] = []
    frozen_actions: list[str] = []
    for action_id in stuck:
        # Integrity first: observation present but action not OBSERVED is a
        # mismatch — freeze, never guess.
        from v5.execution import (
            INTEGRITY_VIOLATION,
            begin_executing,  # noqa: F401  (import for contract clarity)
            classify_action_state,
            freeze_action,
            load_action,
            mark_unknown_outcome,
        )
        if classify_action_state(store, action_id) == INTEGRITY_VIOLATION:
            freeze_action(store, action_id,
                          "observation exists but action not OBSERVED after restart (Law 5)")
            frozen_actions.append(action_id)
            continue
        action = load_action(store.read(), action_id)
        result = mark_unknown_outcome(
            store, action_id, action.revision,
            unknown_reason="process restart: external outcome could not be confirmed (Law 9)",
            possible_external_effect="the external effect may or may not have occurred",
        )
        if isinstance(result, Ok):
            recovered_actions.append(action_id)
    return {"recovered_to_unknown": recovered_actions, "frozen": frozen_actions}


def verify_recovered_state(store: Store) -> dict:
    """Post-recovery audit: assert the five-outcome categorization for every
    action. Used by tests to prove the crash matrix holds."""
    from v5.execution import classify_action_state
    conn = store.read()
    out = {}
    for r in conn.execute("SELECT id FROM actions").fetchall():
        out[r["id"]] = classify_action_state(store, r["id"])
    return out
