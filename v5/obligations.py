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


def _validate_owner_locked(conn, owner: str) -> str | None:
    """Laws 17/18: an obligation's canonical owner is either the UOR sentinel
    or a real existing Task. Returns a rejection reason, or None if valid."""
    if owner == UOR:
        return None
    if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (owner,)).fetchone() is None:
        return "OBLIGATION_OWNER_INVALID"
    return None


def create_obligation(store: Store, origin_action_id: str, owner: str,
                      unknown_reason: str, possible_external_effect: str,
                      resolution_budget: int = 3,
                      safe_retry_conditions: str | None = None) -> Result:
    """Create an OPEN obligation owned by `owner`. Laws 17/18: the owner must
    be the UOR sentinel or a REAL existing Task — validated inside the same
    transaction as creation, so no orphan/detached obligation row can commit.
    Called by the Execution Service when an Action's outcome becomes
    uncertain (Law 9)."""
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
        reason = _validate_owner_locked(conn, owner)
        if reason is not None:
            return Rejected(reason,
                            f"owner must be the UOR sentinel or an existing task id, got {owner!r} "
                            "(Laws 17/18)", None)
        save_new_obligation(conn, obl)
        return Ok(obl)


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
    # Law 18: ownership moves only to a canonical owner (a real Task or UOR)
    invalid = _validate_owner_locked(conn, new_owner)
    if invalid is not None:
        return Rejected(invalid,
                         f"new owner must be the UOR sentinel or an existing task id, got {new_owner!r} "
                         "(Law 18)", obl)
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
    """Resolve an OPEN obligation. Resolution requires:
      1. the supplied Verification exists and has result == PASS, AND
      2. provenance: the Verification's claim cites at least one piece of
         runtime Evidence that derives (via its origin Observation) from the
         SAME Action that created the obligation (Law 23/25/29 — a PASS
         verification about an unrelated action must not resolve this
         obligation).

    Canonical chain walked: Verification.verifies → Claim.based_on →
    Evidence.origin_observation_id → Observation.action_id ==
    Obligation.origin_action_id."""
    with store.write() as conn:
        row = conn.execute("SELECT * FROM obligations WHERE id = ?", (obligation_id,)).fetchone()
        if row is None:
            return Rejected(R_NOT_FOUND, f"obligation {obligation_id}", None)
        obl = _row_to_obligation(conn, row)
        if obl.revision != expected_revision:
            return Rejected(R_STALE_REVISION, f"revision={obl.revision} expected={expected_revision}", obl)
        if obl.disposition.value != "OPEN":
            return Rejected(R_NOT_OPEN, f"disposition={obl.disposition.value}", obl)
        vrow = conn.execute("SELECT * FROM verifications WHERE id = ?", (verification_id,)).fetchone()
        if vrow is None or vrow["result"] != "PASS":
            return Rejected(R_NOT_OPEN, "resolution requires a PASS verification", obl)
        if not _verification_concerns_obligation_locked(conn, vrow, obl):
            return Rejected("RESOLUTION_PROVENANCE",
                            f"verification {verification_id} does not trace to the obligation's "
                            f"origin action {obl.origin_action_id} (Laws 23/25/29)", obl)
        conn.execute(
            "UPDATE obligations SET disposition = 'RESOLVED', revision = revision + 1 "
            "WHERE id = ? AND revision = ?",
            (obligation_id, expected_revision),
        )
        conn.execute(
            "INSERT INTO obligation_events (obligation_id, kind, from_owner, to_owner, authorized_by, reason, timestamp) "
            "VALUES (?,?,?,?,?,?,?)",
            (obligation_id, EV_RESOLVED, obl.owner, None, resolved_by,
             f"verification {verification_id} PASS (provenance to {obl.origin_action_id})",
             iso(utcnow())),
        )
        return Ok()


def _verification_concerns_obligation_locked(conn, vrow, obl) -> bool:
    """Walk Verification → Claim → cited Evidence → origin Observation, and
    check that at least one cited runtime evidence derives from an
    Observation of the obligation's origin Action. INFERENCE/UNKNOWN evidence
    carries no observation provenance and cannot establish the link."""
    claim_row = conn.execute(
        "SELECT based_on FROM claims WHERE id = ? ORDER BY version DESC LIMIT 1",
        (vrow["verifies"],),
    ).fetchone()
    if claim_row is None:
        return False
    for ev_id in jload(claim_row["based_on"], []):
        ev = conn.execute(
            "SELECT origin_observation_id FROM evidence WHERE id = ?", (ev_id,)
        ).fetchone()
        if ev is None or ev["origin_observation_id"] is None:
            continue
        obs = conn.execute(
            "SELECT action_id FROM observations WHERE id = ?", (ev["origin_observation_id"],)
        ).fetchone()
        if obs is not None and obs["action_id"] == obl.origin_action_id:
            return True
    return False


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
