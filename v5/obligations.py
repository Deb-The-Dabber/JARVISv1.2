"""Obligations (unresolved outcomes) and the Unresolved Obligations Registry.

Constitution: Laws 17–22. Contract §3 (Obligation/ObligationEvent), §6.

Key semantics (do not "fix"):
  * owner is a task id OR the literal sentinel "UOR" — exactly one canonical
    owner at any moment (Law 18). The UOR is not a separate table: the
    obligations ledger IS the registry; `owner == "UOR"` rows are UOR entries
    and `disposition == OPEN` defines the active set (Law 22: never
    destructively deleted; disposed entries become historical).
  * Transfer changes ownership, never disposition (Law 19) — a transferred
    obligation stays OPEN. Transfer to the current owner is an idempotent
    no-op (Contract §9 row 4: the losing racer "no-ops rather than erroring").
  * ABANDON requires an explicit human identity naming THIS obligation
    (Law 21) — no blanket closures.
"""
from __future__ import annotations

from v5.ids import UOR, new_id
from v5.models import (
    Obligation,
    ObligationEvent,
    Ok,
    Rejected,
    Result,
    R_NOT_FOUND,
    R_NOT_OPEN,
    R_STALE_REVISION,
    R_WRONG_OWNER,
)
from v5.store import Store, iso, jload, utcnow

EV_TRANSFERRED = "transferred"
EV_RESOLVED = "resolved"
EV_ABANDONED = "abandoned"


# ── row mapping ──────────────────────────────────────────────────────────────

def _row_to_obligation(conn, row) -> Obligation:
    history = [
        ObligationEvent(
            kind=r["kind"],
            from_owner=r["from_owner"],
            to_owner=r["to_owner"],
            authorized_by=r["authorized_by"],
            reason=r["reason"],
            timestamp=r["timestamp"],
        )
        for r in conn.execute(
            "SELECT * FROM obligation_events WHERE obligation_id = ? ORDER BY seq",
            (row["id"],),
        )
    ]
    return Obligation(
        id=row["id"],
        origin_action_id=row["origin_action_id"],
        owner=row["owner"],
        disposition=_disp(row["disposition"]),
        resolution_budget=row["resolution_budget"],
        unknown_reason=row["unknown_reason"],
        possible_external_effect=row["possible_external_effect"],
        safe_retry_conditions=row["safe_retry_conditions"],
        revision=row["revision"],
        history=history,
    )


def _disp(name: str):
    from v5.enums import ObligationDisposition
    return ObligationDisposition(name)


def load_obligation(conn, obligation_id: str) -> Obligation | None:
    row = conn.execute("SELECT * FROM obligations WHERE id = ?", (obligation_id,)).fetchone()
    return _row_to_obligation(conn, row) if row else None


def save_new_obligation(conn, obl: Obligation):
    conn.execute(
        "INSERT INTO obligations (id, origin_action_id, owner, disposition, resolution_budget, "
        "unknown_reason, possible_external_effect, safe_retry_conditions, revision) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (obl.id, obl.origin_action_id, obl.owner, obl.disposition.value,
         obl.resolution_budget, obl.unknown_reason, obl.possible_external_effect,
         obl.safe_retry_conditions, obl.revision),
    )


def create_obligation(store: Store, origin_action_id: str, owner: str,
                      unknown_reason: str, possible_external_effect: str,
                      resolution_budget: int = 3,
                      safe_retry_conditions: str | None = None) -> Obligation:
    """Create an OPEN obligation owned by `owner` (a task id). Called by the
    Execution Service when an Action's outcome becomes uncertain (Law 9)."""
    obl = Obligation(
        id=new_id("obligation"),
        origin_action_id=origin_action_id,
        owner=owner,
        disposition=_disp("OPEN"),
        resolution_budget=resolution_budget,
        unknown_reason=unknown_reason,
        possible_external_effect=possible_external_effect,
        safe_retry_conditions=safe_retry_conditions,
        revision=0,
        history=[],
    )
    with store.write() as conn:
        save_new_obligation(conn, obl)
    return obl


# ── atomic operations (Contract §6) ─────────────────────────────────────────

def transfer_obligation(store: Store, obligation_id: str, new_owner: str,
                        expected_owner: str, reason: str = "terminal transition") -> Result:
    """Contract §6 transfer_obligation. Atomic CAS on owner.

    Precondition (same transaction): owner == expected_owner, disposition OPEN.
    Postcondition: owner = new_owner, disposition REMAINS OPEN (Law 19),
    ObligationEvent(kind="transferred") appended.
    Idempotency (Contract §9 row 4): transferring to the CURRENT owner is an
    Ok no-op — the racing loser reads owner == UOR and no-ops.
    """
    with store.write() as conn:
        return _transfer_locked(conn, obligation_id, new_owner, expected_owner, reason)


def _transfer_locked(conn, obligation_id: str, new_owner: str, expected_owner: str,
                     reason: str) -> Result:
    row = conn.execute("SELECT * FROM obligations WHERE id = ?", (obligation_id,)).fetchone()
    if row is None:
        return Rejected(R_NOT_FOUND, f"obligation {obligation_id}", None)
    obl = _row_to_obligation(conn, row)
    if obl.disposition.value != "OPEN":
        return Rejected(R_NOT_OPEN, f"disposition={obl.disposition.value}", obl)
    if obl.owner == new_owner:
        return Ok(noop=True)  # idempotent no-op — never error, never duplicate
    if obl.owner != expected_owner:
        return Rejected(R_WRONG_OWNER, f"owner={obl.owner} expected={expected_owner}", obl)
    conn.execute(
        "UPDATE obligations SET owner = ?, revision = revision + 1 WHERE id = ? AND revision = ?",
        (new_owner, obligation_id, obl.revision),
    )
    conn.execute(
        "INSERT INTO obligation_events (obligation_id, kind, from_owner, to_owner, reason, timestamp) "
        "VALUES (?,?,?,?,?,?)",
        (obligation_id, EV_TRANSFERRED, expected_owner, new_owner, reason, iso(utcnow())),
    )
    return Ok()


def abandon_obligation(store: Store, obligation_id: str, authorized_by: str,
                      reason: str, expected_revision: int) -> Result:
    """Contract §6 abandon_obligation. Explicit human authorization naming
    THIS obligation (Law 21). Disposition OPEN -> ABANDONED + history event."""
    with store.write() as conn:
        return _abandon_locked(conn, obligation_id, authorized_by, reason, expected_revision)


def _abandon_locked(conn, obligation_id: str, authorized_by: str, reason: str,
                    expected_revision: int) -> Result:
    row = conn.execute("SELECT * FROM obligations WHERE id = ?", (obligation_id,)).fetchone()
    if row is None:
        return Rejected(R_NOT_FOUND, f"obligation {obligation_id}", None)
    obl = _row_to_obligation(conn, row)
    if not authorized_by or not authorized_by.strip():
        return Rejected("ABANDON_REQUIRES_HUMAN", "authorized_by must name a human", obl)
    if obl.revision != expected_revision:
        return Rejected(R_STALE_REVISION, f"revision={obl.revision} expected={expected_revision}", obl)
    if obl.disposition.value != "OPEN":
        return Rejected(R_NOT_OPEN, f"disposition={obl.disposition.value}", obl)
    conn.execute(
        "UPDATE obligations SET disposition = 'ABANDONED', revision = revision + 1 "
        "WHERE id = ? AND revision = ?",
        (obligation_id, expected_revision),
    )
    conn.execute(
        "INSERT INTO obligation_events (obligation_id, kind, from_owner, to_owner, authorized_by, reason, timestamp) "
        "VALUES (?,?,?,?,?,?,?)",
        (obligation_id, EV_ABANDONED, obl.owner, None, authorized_by, reason, iso(utcnow())),
    )
    return Ok()


def resolve_obligation(store: Store, obligation_id: str, verification_id: str,
                       expected_revision: int, resolved_by: str = "system") -> Result:
    """Resolve an OPEN obligation. Resolution must be justified by a
    Verification whose result is PASS (Contract §9 row 9 names the racing
    counterpart; the PASS requirement is the smallest justification rule)."""
    with store.write() as conn:
        row = conn.execute("SELECT * FROM obligations WHERE id = ?", (obligation_id,)).fetchone()
        if row is None:
            return Rejected(R_NOT_FOUND, f"obligation {obligation_id}", None)
        obl = _row_to_obligation(conn, row)
        if obl.revision != expected_revision:
            return Rejected(R_STALE_REVISION, f"revision={obl.revision} expected={expected_revision}", obl)
        if obl.disposition.value != "OPEN":
            return Rejected(R_NOT_OPEN, f"disposition={obl.disposition.value}", obl)
        vrow = conn.execute("SELECT result FROM verifications WHERE id = ?", (verification_id,)).fetchone()
        if vrow is None or vrow["result"] != "PASS":
            return Rejected(R_NOT_OPEN, "resolution requires a PASS verification", obl)
        conn.execute(
            "UPDATE obligations SET disposition = 'RESOLVED', revision = revision + 1 "
            "WHERE id = ? AND revision = ?",
            (obligation_id, expected_revision),
        )
        conn.execute(
            "INSERT INTO obligation_events (obligation_id, kind, from_owner, to_owner, authorized_by, reason, timestamp) "
            "VALUES (?,?,?,?,?,?,?)",
            (obligation_id, EV_RESOLVED, obl.owner, None, resolved_by,
             f"verification {verification_id} PASS", iso(utcnow())),
        )
        return Ok()


# ── queries (Law 17: canonical discoverability) ─────────────────────────────

def list_open_obligations(store: Store, owner: str | None = None) -> list[Obligation]:
    """The active set is `disposition == OPEN` (Law 22). UOR-owned entries are
    the registry view: list_open_obligations(store, owner=UOR)."""
    conn = store.read()
    if owner is None:
        rows = conn.execute(
            "SELECT * FROM obligations WHERE disposition = 'OPEN' ORDER BY id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM obligations WHERE disposition = 'OPEN' AND owner = ? ORDER BY id",
            (owner,),
        ).fetchall()
    return [_row_to_obligation(conn, r) for r in rows]


def obligation_history(store: Store, obligation_id: str) -> list[ObligationEvent]:
    conn = store.read()
    row = conn.execute("SELECT * FROM obligations WHERE id = ?", (obligation_id,)).fetchone()
    if row is None:
        return []
    return _row_to_obligation(conn, row).history


# ── subtree scan (Law 20) ────────────────────────────────────────────────────

def open_obligations_in_subtree_locked(conn, obj_id: str) -> list[Obligation]:
    """OPEN obligations in obj's subtree, inside an open write transaction.

    Interpretation (DECISIONS.md): the contract fixes `owner` to a task id or
    the UOR sentinel, so the only objects that canonically own obligations
    are Tasks. Therefore:
      * task X  -> obligations owned by X
      * goal G  -> obligations owned by any task of G
      * plan/step/action reaching a terminal state does NOT transfer the owning
        task's obligations — that task is still live and remains the durable
        address (Law 17); transfer fires when the owning Task (or ancestor
        Goal) itself terminates.
    """
    rows = conn.execute(
        "SELECT * FROM obligations WHERE disposition = 'OPEN' AND owner = ? ORDER BY id",
        (obj_id,),
    ).fetchall()
    if rows:
        return [_row_to_obligation(conn, r) for r in rows]
    # goal case: obligations owned by any of this goal's tasks
    rows = conn.execute(
        "SELECT o.* FROM obligations o JOIN tasks t ON o.owner = t.id "
        "WHERE t.goal_id = ? AND o.disposition = 'OPEN' ORDER BY o.id",
        (obj_id,),
    ).fetchall()
    return [_row_to_obligation(conn, r) for r in rows]


def transfer_subtree_obligations_to_uor_locked(conn, obj_id: str, reason: str,
                                              store: "Store | None" = None) -> int:
    """Atomically move every OPEN subtree obligation to the UOR. Called ONLY
    inside the same transaction as a terminal transition (Law 20). Returns
    the number transferred (0 is legitimate). The crash point after the
    first transfer proves all-or-nothing (Contract crash #8)."""
    moved = 0
    for obl in open_obligations_in_subtree_locked(conn, obj_id):
        result = _transfer_locked(conn, obl.id, UOR, obl.owner, reason)
        if isinstance(result, Ok) and not result.noop:
            moved += 1
            if store is not None:
                store.crash_point("obligation_transfer_loop")  # crash #8: partial must roll back
    return moved
