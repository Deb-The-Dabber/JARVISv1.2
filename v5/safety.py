"""Safety primitives — confirmation binding (Law 32) and the execution-time
safety gate (Law 33).

Law 32: a confirmation is bound to EXACT action identity, revision, target
(capability), and arguments. Any material change invalidates it. It cannot be
reused across a different action — the binding row is keyed by action_id.

Law 33: safety evaluates the actual external effect (capability + arguments),
not the tool name — the binding check compares capability and arguments
verbatim, so decomposition/aliasing cannot reuse a confirmation.

The execution-time gate is passed to begin_executing as a `safety_check`
callable and evaluated INSIDE the atomic mutation boundary.
"""
from __future__ import annotations

from v5.ids import new_id
from v5.models import Ok, Rejected, Result, R_CONFIRMATION_REQUIRED, R_NOT_FOUND
from v5.store import Store, iso, jdump, jload, utcnow


def create_confirmation(store: Store, action_id: str, action_revision: int,
                        capability: str, arguments: dict) -> Result:
    """Stage a confirmation bound to exact action identity + revision +
    capability + arguments (Law 32). Not yet confirmed."""
    cfm_id = new_id("confirmation")
    with store.write() as conn:
        conn.execute(
            "INSERT INTO confirmations (id, action_id, action_revision, capability, arguments, confirmed_at) "
            "VALUES (?,?,?,?,?, NULL)",
            (cfm_id, action_id, action_revision, capability, jdump(arguments)),
        )
        return Ok(cfm_id)


def confirm(store: Store, confirmation_id: str) -> Result:
    """Human confirms the staged binding."""
    with store.write() as conn:
        r = conn.execute("SELECT * FROM confirmations WHERE id = ?", (confirmation_id,)).fetchone()
        if r is None:
            return Rejected(R_NOT_FOUND, f"confirmation {confirmation_id}", None)
        conn.execute(
            "UPDATE confirmations SET confirmed_at = ? WHERE id = ? AND confirmed_at IS NULL",
            (iso(utcnow()), confirmation_id),
        )
        return Ok()


def confirmation_for(store: Store, action_id: str):
    r = store.read().execute(
        "SELECT * FROM confirmations WHERE action_id = ?", (action_id,)
    ).fetchone()
    return r


def make_confirmation_gate(required: bool):
    """Build the safety_check callable that begin_executing evaluates inside
    its transaction. `required` comes from the capability spec — the actual
    external effect class (Law 33), not a tool name convention.

    Law 32: the confirmation is bound to EXACT action identity, REVISION,
    capability, and arguments. A confirmation staged at revision N is invalid
    once the Action legitimately advances to revision N+1 — even when capability
    and arguments are identical. Both lookup paths (the general per-action
    lookup and the action.confirmation_id-specific lookup) enforce the same
    four-part binding."""
    def gate(conn, action) -> Rejected | None:
        if not required:
            return None
        row = conn.execute(
            "SELECT * FROM confirmations WHERE action_id = ? AND confirmed_at IS NOT NULL "
            "ORDER BY confirmed_at DESC LIMIT 1",
            (action.id,),
        ).fetchone()
        if row is None:
            return Rejected(R_CONFIRMATION_REQUIRED,
                            f"capability {action.capability} requires confirmation (Law 32/33)", action)
        if row["action_revision"] != action.revision:
            return Rejected(R_CONFIRMATION_REQUIRED,
                            f"confirmation was staged for revision {row['action_revision']} but the action "
                            f"is at revision {action.revision} — a material change invalidates the "
                            "confirmation (Law 32)", action)
        if row["capability"] != action.capability or row["arguments"] != jdump(action.arguments):
            return Rejected(R_CONFIRMATION_REQUIRED,
                            "confirmation bound to different capability/arguments (Law 32)", action)
        if action.confirmation_id and row["id"] != action.confirmation_id:
            # the action may only execute under ITS OWN staged binding
            mine = conn.execute(
                "SELECT * FROM confirmations WHERE id = ?", (action.confirmation_id,)
            ).fetchone()
            if mine is None or mine["confirmed_at"] is None:
                return Rejected(R_CONFIRMATION_REQUIRED,
                                "action's own binding is unconfirmed (Law 32)", action)
            if mine["action_revision"] != action.revision:
                return Rejected(R_CONFIRMATION_REQUIRED,
                                f"action's own binding was staged for revision {mine['action_revision']} "
                                f"but the action is at revision {action.revision} (Law 32)", action)
            if mine["capability"] != action.capability or mine["arguments"] != jdump(action.arguments):
                return Rejected(R_CONFIRMATION_REQUIRED,
                                "action's binding does not match its arguments (Law 32)", action)
        return None
    return gate
