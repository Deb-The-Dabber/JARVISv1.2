"""Session authority (Cognition Implementation Contract v1.1 §13).

Sessions are ephemeral conversation identities. They are NOT Work state and
carry no ownership semantics — §13.3's single authorization rule is a
session-legitimacy check (the session exists, is LIVE, and is
Cognition-authorized), never an identity match against anything recorded on a
Work object. `Goal.session_origin` remains provenance only.

Single-principal deployment (§13.1): every valid session in this deployment
resolves to exactly one durable principal, `OWNER_PRINCIPAL_ID`, read from
deployment configuration — never model-supplied, never stored on canonical
Work objects, never used as a per-object authorization key.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from v5.enums import SessionState
from v5.ids import new_id
from v5.models import Ok, Rejected, Result, R_NOT_FOUND
from v5.store import Store, iso, utcnow

# Deployment configuration (§13.1): exactly one value for the life of the
# deployment. Env override is the deployment-time setting mechanism; the
# default keeps local/tests honest without inventing a config subsystem.
OWNER_PRINCIPAL_ID: str = os.environ.get("JARVIS_OWNER_PRINCIPAL_ID", "principal:owner")


@dataclass(frozen=True)
class Session:
    id: str
    state: SessionState
    cognition_authorized: bool
    created_at: str
    terminated_at: str | None


def create_session(store: Store, cognition_authorized: bool = True) -> Session:
    """Open a new ephemeral session. Sessions are cheap and disposable —
    a conversation ends, the session terminates, and anything it created in
    canonical Work remains addressable by later sessions of the same
    single-principal deployment (§13.3/§13.4)."""
    s = Session(id=new_id("session"), state=SessionState.LIVE,
                cognition_authorized=cognition_authorized,
                created_at=iso(utcnow()), terminated_at=None)
    with store.write() as conn:
        conn.execute(
            "INSERT INTO sessions (id, state, cognition_authorized, created_at, terminated_at) "
            "VALUES (?,?,?,?,NULL)",
            (s.id, s.state.value, 1 if s.cognition_authorized else 0, s.created_at),
        )
        store.audit(conn, s.id, "session_created", None, SessionState.LIVE.value)
    return s


def resolve_session(store: Store, session_id: str) -> Session | None:
    r = store.read().execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if r is None:
        return None
    return Session(id=r["id"], state=SessionState(r["state"]),
                   cognition_authorized=bool(r["cognition_authorized"]),
                   created_at=r["created_at"], terminated_at=r["terminated_at"])


def terminate_session(store: Store, session_id: str) -> Result:
    """Terminate a session. Idempotent: terminating a terminated session is a
    no-op Ok. Termination never affects the addressability of Work the session
    created (§13.3 — authorization is a property of the LIVE session doing the
    asking, not of whatever session is named in provenance)."""
    with store.write() as conn:
        r = conn.execute("SELECT state FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if r is None:
            return Rejected(R_NOT_FOUND, f"session {session_id}", None)
        if SessionState(r["state"]) == SessionState.TERMINATED:
            return Ok(noop=True)
        conn.execute(
            "UPDATE sessions SET state = 'TERMINATED', terminated_at = ? WHERE id = ? AND state = 'LIVE'",
            (iso(utcnow()), session_id),
        )
        store.audit(conn, session_id, "session_terminated", "LIVE", "TERMINATED")
        return Ok()


def session_is_legitimate(store: Store, session_id: str) -> bool:
    """The §13.3 legitimacy predicate: exists, LIVE, Cognition-authorized.
    This is the ONLY session check the Cognition membrane performs; no identity
    comparison against any Work object exists anywhere in the model."""
    s = resolve_session(store, session_id)
    return (
        s is not None
        and s.state == SessionState.LIVE
        and s.cognition_authorized
    )
