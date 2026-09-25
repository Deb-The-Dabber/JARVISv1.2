"""Execution Service — owns executing Actions, creating execution-derived
Observations, and producing execution-derived Evidence (Constitution §Authority).

Constitutional invariants implemented here:
  * Law 8 (Pre-Effect Persistence): begin_executing durably commits
    EXECUTING BEFORE any external effect.
  * Law 9 (Unknown Outcome): uncertain outcomes become UNKNOWN_OUTCOME and
    create an OPEN Obligation owned by the owning Task — never guessed into
    FAILED/SUCCESS.
  * Law 10/23 (Attempt Identity, immutability): executed attempts are never
    rewritten; retry creates a NEW Action identity (with a bounded,
    capability-declared retry budget on the Task).
  * Race #6 (Contract §9): every execution-path mutation re-validates the
    active plan — a stale worker operating under a superseded Plan is
    rejected and must re-sync.
"""
from __future__ import annotations

from v5.enums import (
    ACTION_TERMINAL,
    ACTION_TRANSITIONS,
    ActionStatus,
    IdempotencyClass,
    IntegrityStatus,
    StepStatus,
    TaskStatus,
)
from v5.ids import new_id
from v5.models import (
    Action,
    Ok,
    Observation,
    Rejected,
    Result,
    R_CONFIRMATION_REQUIRED,
    R_INVALID_TRANSITION,
    R_NOT_FOUND,
    R_NOT_RETRYABLE,
    R_PLAN_MISMATCH,
    R_RETRY_BUDGET,
    R_STALE_REVISION,
    R_TERMINAL,
)
from v5.store import Store, iso, jdump, jload, utcnow


# ── row mapping ──────────────────────────────────────────────────────────────

def load_action(conn, action_id: str) -> Action | None:
    r = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
    if not r:
        return None
    return Action(
        id=r["id"], revision=r["revision"], step_id=r["step_id"],
        status=ActionStatus(r["status"]), capability=r["capability"],
        arguments=jload(r["arguments"], {}),
        idempotency_class=IdempotencyClass(r["idempotency_class"]),
        confirmation_id=r["confirmation_id"], retry_of=r["retry_of"],
        integrity=r["integrity"] if "integrity" in r.keys() else "OK",
    )


def load_observation(store: Store, action_id: str) -> Observation | None:
    conn = store.read()
    r = conn.execute("SELECT * FROM observations WHERE action_id = ?", (action_id,)).fetchone()
    if not r:
        return None
    return Observation(
        id=r["id"], action_id=r["action_id"], captured_at=r["captured_at"],
        raw_result=jload(r["raw_result"]), execution_source=r["execution_source"],
    )


def observation_count(store: Store, action_id: str) -> int:
    conn = store.read()
    return conn.execute(
        "SELECT COUNT(*) AS n FROM observations WHERE action_id = ?", (action_id,)
    ).fetchone()["n"]


# ── creation ─────────────────────────────────────────────────────────────────

def create_action(store: Store, step_id: str, capability: str, arguments: dict,
                  idempotency_class: IdempotencyClass,
                  confirmation_id: str | None = None,
                  expected_step_revision: int | None = None) -> Result:
    """Persist Action(PENDING). This is the authoritative Action-acceptance
    path — Cognition reaches it only through propose_action, which may not
    execute anything (Impl. Contract §12).

    Authoritative checks added for the Cognition membrane (all evaluated
    inside the same transaction, none of them replacing the adapter's
    fast-fail layer):
      * expected_step_revision CAS, when supplied (§12.1 stale-step guard);
      * capability correspondence: if the Step declares a canonical
        execution_capability (Cognition-created steps always do), the Action's
        capability must equal it exactly (§12.2/§29) — the adapter is never
        the sole enforcement mechanism;
      * registry schema validation: a capability with a declared
        validate_args schema must have schema-valid arguments before the
        Action is accepted (§12.1/§42).

    Legacy foundation rows (steps with execution_capability = "") are not
    retro-constrained — the correspondence check applies only where a
    capability was canonically declared."""
    with store.write() as conn:
        step_row = conn.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone()
        if step_row is None:
            return Rejected(R_NOT_FOUND, f"step {step_id}", None)
        if step_row["status"] in ("CANCELLED", "SKIPPED", "COMPLETED"):
            return Rejected(R_INVALID_TRANSITION, f"step status {step_row['status']} (Law 14)", None)
        if expected_step_revision is not None and step_row["revision"] != expected_step_revision:
            return Rejected(R_STALE_REVISION,
                            f"step revision={step_row['revision']} expected={expected_step_revision}",
                            None)
        declared = step_row["execution_capability"] if "execution_capability" in step_row.keys() else ""
        if declared and capability != declared:
            return Rejected(R_INVALID_TRANSITION,
                            f"action capability {capability!r} does not match the step's declared "
                            f"execution_capability {declared!r} (§12.2/§29 correspondence)", None)
        from v5 import capabilities as _caps
        spec = _caps.get(capability)
        if spec is not None and spec.validate_args is not None:
            schema_err = spec.validate_args(arguments)
            if schema_err is not None:
                return Rejected("CAPABILITY_ARGS_INVALID",
                                f"{capability}: {schema_err}", None)
        a = Action(
            id=new_id("action"), revision=0, step_id=step_id, status=ActionStatus.PENDING,
            capability=capability, arguments=dict(arguments),
            idempotency_class=idempotency_class, confirmation_id=confirmation_id,
        )
        conn.execute(
            "INSERT INTO actions (id, revision, step_id, status, capability, arguments, idempotency_class, confirmation_id, retry_of) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (a.id, 0, step_id, a.status.value, capability, jdump(arguments),
             idempotency_class.value, confirmation_id, None),
        )
        store.audit(conn, a.id, "created", None, a.status.value)
        return Ok(a)


# ── pre-effect persistence (Law 8) ───────────────────────────────────────────

def begin_executing(store: Store, action_id: str, expected_revision: int,
                    expected_plan_id: str | None = None,
                    safety_check=None) -> Result:
    """Durably persist EXECUTING before any external effect.

    `expected_plan_id` enforces race #6: the executor states which Plan it
    believes it is operating under; if the Task's active plan has moved on,
    the operation is rejected and the worker must re-sync/replan.
    `safety_check` is an optional callable run INSIDE the transaction —
    the capability layer uses it for confirmation binding (Law 32/33).
    """
    with store.write() as conn:
        action = load_action(conn, action_id)
        if action is None:
            return Rejected(R_NOT_FOUND, f"action {action_id}", None)
        if action.revision != expected_revision:
            return Rejected(R_STALE_REVISION, f"revision={action.revision} expected={expected_revision}", action)
        if action.integrity == "FROZEN":
            return Rejected("FROZEN", "action is integrity-frozen (Law 5); repair required", action)
        if action.status not in ACTION_TRANSITIONS:
            return Rejected(R_TERMINAL, f"action {action.status.value} is terminal (Law 14)", action)
        if ActionStatus.EXECUTING not in ACTION_TRANSITIONS.get(action.status, frozenset()):
            return Rejected(R_INVALID_TRANSITION, f"{action.status.value} -> EXECUTING not contracted", action)

        # stale-plan revalidation (Contract §9 row 6)
        if expected_plan_id is not None:
            mismatch = _plan_mismatch_locked(conn, action, expected_plan_id)
            if mismatch is not None:
                store.audit(conn, action_id, "rejected", action.status.value, reason=mismatch.detail)
                return mismatch

        # safety gate (Law 32/33) — evaluated inside the same atomic boundary
        if safety_check is not None:
            verdict = safety_check(conn, action)
            if verdict is not None:
                store.audit(conn, action_id, "rejected", action.status.value, reason=verdict.detail)
                return verdict

        store.crash_point("before_executing_persist")  # crash #1: nothing written yet
        conn.execute(
            "UPDATE actions SET status = 'EXECUTING', revision = revision + 1 WHERE id = ? AND revision = ?",
            (action_id, expected_revision),
        )
        store.audit(conn, action_id, "transition", action.status.value, "EXECUTING")
        return Ok(load_action(conn, action_id))


def _plan_mismatch_locked(conn, action: Action, expected_plan_id: str) -> Rejected | None:
    """Returns a Rejected if the action's step is not under the caller's
    believed active plan (stale worker), else None."""
    s = conn.execute("SELECT plan_id FROM steps WHERE id = ?", (action.step_id,)).fetchone()
    if s is None:
        return Rejected(R_NOT_FOUND, f"step {action.step_id}", action)
    p = conn.execute("SELECT task_id FROM plans WHERE id = ?", (s["plan_id"],)).fetchone()
    if p is None:
        return Rejected(R_NOT_FOUND, f"plan {s['plan_id']}", action)
    if s["plan_id"] != expected_plan_id:
        return Rejected(R_PLAN_MISMATCH,
                        f"action's plan {s['plan_id']} != believed plan {expected_plan_id}", action)
    t = conn.execute("SELECT active_plan_id FROM tasks WHERE id = ?", (p["task_id"],)).fetchone()
    if t is None:
        return Rejected(R_NOT_FOUND, f"task {p['task_id']}", action)
    if t["active_plan_id"] != expected_plan_id:
        return Rejected("PLAN_SUPERSEDED",
                        f"task active plan is {t['active_plan_id']}, worker believed {expected_plan_id} "
                        "(stale worker must re-sync — Contract §9 row 6)", action)
    return None


# ── observation (Contract crash #5: Observation + OBSERVED in ONE transaction) ─

def mark_observed(store: Store, action_id: str, expected_revision: int,
                  raw_result, execution_source: str,
                  expected_plan_id: str | None = None) -> Result:
    """One atomic transaction: insert Observation + Action -> OBSERVED.
    Crash between the two writes rolls BOTH back — it is never observable
    that an Observation exists while the Action is PENDING/EXECUTING."""
    with store.write() as conn:
        action = load_action(conn, action_id)
        if action is None:
            return Rejected(R_NOT_FOUND, f"action {action_id}", None)
        if action.revision != expected_revision:
            return Rejected(R_STALE_REVISION, f"revision={action.revision} expected={expected_revision}", action)
        if action.integrity == "FROZEN":
            return Rejected("FROZEN", "action is integrity-frozen (Law 5)", action)
        if ActionStatus.OBSERVED not in ACTION_TRANSITIONS.get(action.status, frozenset()):
            return Rejected(R_INVALID_TRANSITION, f"{action.status.value} -> OBSERVED not contracted", action)
        if expected_plan_id is not None:
            mismatch = _plan_mismatch_locked(conn, action, expected_plan_id)
            if mismatch is not None:
                return mismatch

        obs_id = new_id("observation")
        store.crash_point("before_observation_persist")  # crash #4: before the insert
        conn.execute(
            "INSERT INTO observations (id, action_id, captured_at, raw_result, execution_source) "
            "VALUES (?,?,?,?,?)",
            (obs_id, action_id, iso(utcnow()), jdump(raw_result), execution_source),
        )
        store.crash_point("after_observation_insert")      # crash #5: between insert and status write
        conn.execute(
            "UPDATE actions SET status = 'OBSERVED', revision = revision + 1 WHERE id = ? AND revision = ?",
            (action_id, expected_revision),
        )
        store.audit(conn, action_id, "observed", "EXECUTING", "OBSERVED")
        return Ok({"action": load_action(conn, action_id), "observation_id": obs_id})


# ── unknown outcome (Law 9) ───────────────────────────────────────────────────

def mark_unknown_outcome(store: Store, action_id: str, expected_revision: int,
                        unknown_reason: str, possible_external_effect: str,
                        resolution_budget: int = 3) -> Result:
    """EXECUTING -> UNKNOWN_OUTCOME + create an OPEN Obligation owned by the
    owning Task, in ONE transaction. The outcome is never guessed."""
    from v5.obligations import save_new_obligation
    from v5.work import get_owning_task_id

    with store.write() as conn:
        action = load_action(conn, action_id)
        if action is None:
            return Rejected(R_NOT_FOUND, f"action {action_id}", None)
        if action.revision != expected_revision:
            return Rejected(R_STALE_REVISION, f"revision={action.revision} expected={expected_revision}", action)
        if action.integrity == "FROZEN":
            return Rejected("FROZEN", "action is integrity-frozen (Law 5)", action)
        if ActionStatus.UNKNOWN_OUTCOME not in ACTION_TRANSITIONS.get(action.status, frozenset()):
            return Rejected(R_INVALID_TRANSITION, f"{action.status.value} -> UNKNOWN_OUTCOME not contracted", action)

        owner = _owning_task_id_locked(conn, action)
        if owner is None:
            return Rejected(R_NOT_FOUND, "owning task not found", action)

        conn.execute(
            "UPDATE actions SET status = 'UNKNOWN_OUTCOME', revision = revision + 1 WHERE id = ? AND revision = ?",
            (action_id, expected_revision),
        )
        from v5.enums import ObligationDisposition
        from v5.models import Obligation
        obl = Obligation(
            id=new_id("obligation"), origin_action_id=action_id, owner=owner,
            disposition=ObligationDisposition.OPEN,
            resolution_budget=resolution_budget,
            unknown_reason=unknown_reason,
            possible_external_effect=possible_external_effect,
            safe_retry_conditions=None, revision=0, history=[],
        )
        save_new_obligation(conn, obl)
        store.audit(conn, action_id, "unknown_outcome", "EXECUTING", "UNKNOWN_OUTCOME")
        return Ok({"action": load_action(conn, action_id), "obligation_id": obl.id})


def _owning_task_id_locked(conn, action: Action) -> str | None:
    s = conn.execute("SELECT plan_id FROM steps WHERE id = ?", (action.step_id,)).fetchone()
    if s is None:
        return None
    p = conn.execute("SELECT task_id FROM plans WHERE id = ?", (s["plan_id"],)).fetchone()
    return p["task_id"] if p else None


# ── definite failure / cancel ─────────────────────────────────────────────────

def mark_failed(store: Store, action_id: str, expected_revision: int, reason: str,
                definite_no_effect: bool = True) -> Result:
    """EXECUTING -> FAILED. `definite_no_effect=True` asserts the capability
    itself established that no external effect occurred (e.g. validation
    failed before any write). If that is not established, the caller MUST use
    mark_unknown_outcome instead (Law 9)."""
    with store.write() as conn:
        action = load_action(conn, action_id)
        if action is None:
            return Rejected(R_NOT_FOUND, f"action {action_id}", None)
        if action.revision != expected_revision:
            return Rejected(R_STALE_REVISION, f"revision={action.revision}", action)
        if not definite_no_effect:
            return Rejected("NOT_DEFINITE", "use mark_unknown_outcome when no-effect is not established (Law 9)", action)
        if ActionStatus.FAILED not in ACTION_TRANSITIONS.get(action.status, frozenset()):
            return Rejected(R_INVALID_TRANSITION, f"{action.status.value} -> FAILED not contracted", action)
        conn.execute(
            "UPDATE actions SET status = 'FAILED', revision = revision + 1 WHERE id = ? AND revision = ?",
            (action_id, expected_revision),
        )
        store.audit(conn, action_id, "failed", "EXECUTING", "FAILED", reason=reason)
        return Ok(load_action(conn, action_id))


def cancel_action(store: Store, action_id: str, expected_revision: int) -> Result:
    with store.write() as conn:
        action = load_action(conn, action_id)
        if action is None:
            return Rejected(R_NOT_FOUND, f"action {action_id}", None)
        if action.revision != expected_revision:
            return Rejected(R_STALE_REVISION, f"revision={action.revision}", action)
        if ActionStatus.CANCELLED not in ACTION_TRANSITIONS.get(action.status, frozenset()):
            return Rejected(R_INVALID_TRANSITION, f"{action.status.value} is terminal (Law 14)", action)
        conn.execute(
            "UPDATE actions SET status = 'CANCELLED', revision = revision + 1 WHERE id = ? AND revision = ?",
            (action_id, expected_revision),
        )
        store.audit(conn, action_id, "cancelled", action.status.value, "CANCELLED")
        return Ok(load_action(conn, action_id))


# ── retry (Law 10: new identity; Law 11: bounded; race #7: budget CAS) ──────

def retry_action(store: Store, action_id: str, trigger: str,
                 expected_task_revision: int) -> Result:
    """Retry creates a NEW Action identity under the same step (Law 10 — the
    old attempt is never rewritten). The Task's retry budget is decremented
    (attempts_used + 1) in the SAME transaction, CAS'd on the task revision,
    so two concurrent retry triggers produce exactly one new attempt
    (Contract §9 row 7)."""
    with store.write() as conn:
        action = load_action(conn, action_id)
        if action is None:
            return Rejected(R_NOT_FOUND, f"action {action_id}", None)
        if action.status not in (ActionStatus.UNKNOWN_OUTCOME, ActionStatus.FAILED):
            return Rejected(R_NOT_RETRYABLE, f"action {action.status.value} is not retryable", action)
        owner = _owning_task_id_locked(conn, action)
        if owner is None:
            return Rejected(R_NOT_FOUND, "owning task not found", action)
        trow = conn.execute("SELECT * FROM tasks WHERE id = ?", (owner,)).fetchone()
        if trow is None:
            return Rejected(R_NOT_FOUND, f"task {owner}", action)
        if trow["revision"] != expected_task_revision:
            return Rejected(R_STALE_REVISION,
                           f"task revision={trow['revision']} expected={expected_task_revision}", action)
        budget = jload(trow["retry_budget"], {})
        used, mx = budget.get("attempts_used", 0), budget.get("max_attempts", 3)
        if used >= mx:
            return Rejected(R_RETRY_BUDGET, f"attempts_used={used} max={mx}", action)

        new = Action(
            id=new_id("action"), revision=0, step_id=action.step_id,
            status=ActionStatus.PENDING, capability=action.capability,
            arguments=dict(action.arguments),
            idempotency_class=action.idempotency_class,
            confirmation_id=None, retry_of=action.id,
        )
        conn.execute(
            "INSERT INTO actions (id, revision, step_id, status, capability, arguments, idempotency_class, confirmation_id, retry_of) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (new.id, 0, new.step_id, new.status.value, new.capability,
             jdump(new.arguments), new.idempotency_class.value, None, action.id),
        )
        conn.execute(
            "UPDATE tasks SET retry_budget = ?, revision = revision + 1 WHERE id = ? AND revision = ?",
            (jdump({"max_attempts": mx, "attempts_used": used + 1}), owner, expected_task_revision),
        )
        store.audit(conn, action.id, "retried", action.status.value, new.id,
                    reason=f"trigger={trigger}")
        return Ok(new)


# ── post-restart classification (Contract §8: the five outcomes) ───────────

KNOWN_NOT_STARTED = "KNOWN_NOT_STARTED"
KNOWN_STARTED = "KNOWN_STARTED"
KNOWN_COMPLETED = "KNOWN_COMPLETED"
UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
INTEGRITY_VIOLATION = "INTEGRITY_VIOLATION"


def classify_action_state(store: Store, action_id: str) -> str:
    """Categorize an Action's persisted state into exactly one of the five
    post-restart outcomes. INTEGRITY_VIOLATION covers a state/event mismatch
    (e.g. an Observation row exists while the Action is not OBSERVED)."""
    conn = store.read()
    a = conn.execute("SELECT status FROM actions WHERE id = ?", (action_id,)).fetchone()
    if a is None:
        raise KeyError(action_id)
    status = ActionStatus(a["status"])
    n_obs = conn.execute(
        "SELECT COUNT(*) AS n FROM observations WHERE action_id = ?", (action_id,)
    ).fetchone()["n"]
    if n_obs > 0 and status != ActionStatus.OBSERVED:
        return INTEGRITY_VIOLATION
    if status == ActionStatus.PENDING and n_obs == 0:
        return KNOWN_NOT_STARTED
    if status == ActionStatus.EXECUTING and n_obs == 0:
        return KNOWN_STARTED  # recovery must treat it as uncertain, never assume unstarted
    if status == ActionStatus.OBSERVED:
        return KNOWN_COMPLETED
    if status == ActionStatus.UNKNOWN_OUTCOME:
        return UNKNOWN_OUTCOME
    # FAILED/CANCELLED with no observation: contracted terminal states, not a violation
    return KNOWN_NOT_STARTED if status == ActionStatus.CANCELLED else KNOWN_NOT_STARTED


def freeze_action(store: Store, action_id: str, reason: str) -> Result:
    """Law 5 integrity freeze — no autonomous mutation until repair. Detected
    violations freeze the object; both representations are preserved verbatim
    (nothing is merged or deleted). The `integrity` column on actions is the
    storage representation of the Constitution's freeze for execution objects
    (the contract lists integrity on Goal/Task explicitly; see DECISIONS.md)."""
    with store.write() as conn:
        a = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        if a is None:
            return Rejected(R_NOT_FOUND, f"action {action_id}", None)
        obs = conn.execute(
            "SELECT id FROM observations WHERE action_id = ? ORDER BY id", (action_id,)
        ).fetchall()
        conn.execute(
            "UPDATE actions SET integrity = 'FROZEN', revision = revision + 1 WHERE id = ? AND revision = ?",
            (action_id, a["revision"]),
        )
        store.audit(conn, action_id, "integrity_frozen", a["status"], None, reason=reason)
        return Ok({"frozen": action_id, "preserved_observations": [o["id"] for o in obs]})
