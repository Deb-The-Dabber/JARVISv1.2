"""Integrity Repair — the sole sanctioned exception to the transition
contracts (Law 30), plus the terminal integrity outcome (Law 29's sibling:
ABANDONED_UNREPAIRABLE).

The restricted entry point is this module. It is not imported by work,
execution, evidence, or any capability — they cannot reach it. Human
authorization is represented by the `HumanAuthorization` token, constructible
only through this module's `authorize()`; callers must supply a human identity
and a reason, which are recorded verbatim in the permanent audit.

Repair rules enforced here (Contract §7):
  * revision-checked (conflicting repairs cannot both commit)
  * validated AND committed in ONE atomic boundary (no validate-then-commit gap)
  * cannot fabricate Observation-backed state (Law 29): a target that sets
    Action.status = OBSERVED without a real Observation row is rejected —
    the honest target in that case is UNKNOWN_OUTCOME
  * aggregate validation: proposed target must satisfy the affected
    aggregate's invariants (e.g. a Task repair that sets active_plan_id must
    point at a DRAFT plan of that task — the same check activate_plan runs)
  * terminal targets trigger Law 20 obligation transfer in the same commit
  * fresh revision on commit — stale workers fail their next operation
  * permanent audit: "repaired FROM <old> TO <target> BY <human> ON <date>
    BECAUSE <reason>", never deleted
  * failed validation leaves the object FROZEN (no partial application)
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from v5.enums import (
    ACTION_TERMINAL,
    GOAL_TERMINAL,
    IntegrityStatus,
    PLAN_TERMINAL,
    STEP_TERMINAL,
    TASK_TERMINAL,
    ActionStatus,
    PlanStatus,
    TaskStatus,
)
from v5.ids import UOR
from v5.models import Ok, Rejected, Result, R_AGGREGATE_INVALID, R_FABRICATION, R_INVALID_TRANSITION, R_NOT_FOUND, R_STALE_REPAIR
from v5.obligations import transfer_subtree_obligations_to_uor_locked
from v5.store import Store, jdump, jload, utcnow


@dataclass(frozen=True)
class HumanAuthorization:
    human: str


def authorize(human: str) -> HumanAuthorization:
    """Explicit human authorization. Only this module can mint the token;
    cognition/capability code does not import repair (interface-layer
    enforcement per Contract §7 — see DECISIONS.md for the honest limits of
    module-boundary enforcement in a single Python process)."""
    if not human or not human.strip():
        raise ValueError("human authorization requires a human identity")
    return HumanAuthorization(human.strip())


# fields each object type may have repaired (storage-level whitelist).
# `integrity` is only offered on types that have a real integrity column per
# the contracted schema (Goal/Task) plus action (a documented storage addition
# for Law 5 execution-freeze). Plans/Steps/Verifications/Obligations have NO
# integrity column — granting it there would be a latent SQL error at commit.
# step.depends_on is excluded: it is a denormalized copy of the Plan's
# dependency_graph; repairing the copy would diverge from the committed graph
# (Law 13 cycle-checks run against the graph, not the step field).
_REPAIRABLE_FIELDS = {
    "goal": {"status", "integrity", "completion_policy", "session_origin"},
    "task": {"status", "integrity", "active_plan_id", "completion_policy", "retry_budget"},
    "plan": {"status", "superseded_by", "dependency_graph"},
    "step": {"status", "required"},
    "action": {"status", "integrity", "confirmation_id"},
    "verification": {"result"},
    "obligation": {"owner", "disposition"},
}

_TABLES = {
    "goal": "goals", "task": "tasks", "plan": "plans",
    "step": "steps", "action": "actions",
    "verification": "verifications", "obligation": "obligations",
}

_TERMINAL_SETS = {
    "goal": GOAL_TERMINAL, "task": TASK_TERMINAL, "plan": PLAN_TERMINAL,
    "step": STEP_TERMINAL, "action": ACTION_TERMINAL,
}


def _object_type(conn, obj_id: str) -> str | None:
    for table in ("goals", "tasks", "plans", "steps", "actions", "verifications", "obligations"):
        if conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (obj_id,)).fetchone() is not None:
            return table[:-1] if table != "obligations" else "obligation"
    return None


def repair_object(store: Store, auth: HumanAuthorization, obj_id: str,
                  expected_revision: int, target_state: dict, reason: str) -> Result:
    """Contract §7 repair_object. Validation and commit happen inside one
    `store.write()` boundary; a SimulatedCrash after validation (crash point
    #9) rolls the whole thing back leaving the object FROZEN as it was."""
    if not reason or not reason.strip():
        return Rejected("REPAIR_REQUIRES_REASON", "repair must be justified", None)
    with store.write() as conn:
        kind = _object_type(conn, obj_id)
        if kind is None:
            return Rejected(R_NOT_FOUND, f"object {obj_id}", None)
        row = conn.execute(f"SELECT * FROM {_TABLES[kind]} WHERE id = ?", (obj_id,)).fetchone()
        # Not all contracted objects carry a revision (Verification has none
        # in Contract §3) — for those the revision CAS is vacuous and the
        # Law 29 fabrication rules do the guarding. See DECISIONS.md.
        has_revision = "revision" in row.keys()
        if has_revision:
            current_revision = row["revision"]
            if current_revision != expected_revision:
                store.audit(conn, obj_id, "repair_rejected", None, None,
                            authorized_by=auth.human, reason=f"{R_STALE_REPAIR}: rev {current_revision}")
                return Rejected(R_STALE_REPAIR,
                                f"revision={current_revision} expected={expected_revision} — re-propose",
                                dict(row))

        allowed = _REPAIRABLE_FIELDS.get(kind, set())
        illegal = set(target_state) - allowed
        if illegal:
            return Rejected("REPAIR_FIELD_FORBIDDEN",
                            f"fields {sorted(illegal)} not repairable on {kind}", dict(row))

        # Law 29 — no fabricated Observation-backed state
        if kind == "action" and str(target_state.get("status", "")).upper() == "OBSERVED":
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM observations WHERE action_id = ?", (obj_id,)
            ).fetchone()["n"]
            if n == 0:
                store.audit(conn, obj_id, "repair_rejected", row["status"], None,
                            authorized_by=auth.human,
                            reason="cannot set OBSERVED without a real Observation (Law 29)")
                return Rejected(R_FABRICATION,
                                "no backing Observation exists — honest target is UNKNOWN_OUTCOME "
                                "or a UOR transfer (Law 29)", dict(row))

        # aggregate validation (Law 30): proposed result must satisfy the
        # affected aggregate's invariants, not just local fields
        agg = _validate_aggregate(conn, kind, row, target_state)
        if agg is not None:
            store.audit(conn, obj_id, "repair_rejected",
                        row["status"] if "status" in row.keys() else None, None,
                        authorized_by=auth.human, reason=agg.detail)
            return agg

        store.crash_point("repair_after_validation")  # crash #9: nothing committed yet

        old_state = {k: row[k] for k in row.keys() if k in allowed}
        for field, value in target_state.items():
            if isinstance(value, (dict, list, set)):
                value = jdump(value)
            if has_revision:
                conn.execute(
                    f"UPDATE {_TABLES[kind]} SET {field} = ? WHERE id = ? AND revision = ?",
                    (value, obj_id, expected_revision),
                )
            else:
                conn.execute(
                    f"UPDATE {_TABLES[kind]} SET {field} = ? WHERE id = ?",
                    (value, obj_id),
                )
        # fresh revision forces stale workers to re-sync (Law 30)
        if has_revision:
            conn.execute(
                f"UPDATE {_TABLES[kind]} SET revision = revision + 1 WHERE id = ?",
                (obj_id,),
            )
        # successful repair of a frozen object unfreezes it
        if "integrity" not in target_state and "integrity" in row.keys() and row["integrity"] == "FROZEN":
            conn.execute(f"UPDATE {_TABLES[kind]} SET integrity = 'OK' WHERE id = ?", (obj_id,))

        moved = 0
        terminal_set = _TERMINAL_SETS.get(kind)
        if terminal_set is not None:
            new_status = str(target_state.get("status", row["status"])).upper()
            if new_status in {s.value for s in terminal_set}:
                moved = transfer_subtree_obligations_to_uor_locked(
                    conn, obj_id, reason=f"repair to terminal {new_status} (Law 20)"
                )

        store.audit(
            conn, obj_id, "repaired",
            from_state=jdump(old_state), to_state=jdump(target_state),
            authorized_by=auth.human,
            reason=f"repaired FROM {jdump(old_state)} TO {jdump(target_state)} "
                   f"BY {auth.human} ON {utcnow().isoformat()} BECAUSE {reason}",
        )
        new_row = conn.execute(f"SELECT * FROM {_TABLES[kind]} WHERE id = ?", (obj_id,)).fetchone()
        return Ok({"object": dict(new_row), "obligations_transferred": moved})


def abandon_unrepairable(store: Store, auth: HumanAuthorization, obj_id: str,
                         reason: str, expected_revision: int | None = None) -> Result:
    """A repair that cannot be validated is not forced through. The human may
    instead terminate the object as ABANDONED_UNREPAIRABLE — an integrity
    status that is terminal for the object and STILL obeys Law 20 (open
    subtree obligations transfer to UOR atomically).

    Law 30: this is a repair-class mutation, so it is revision-checked like
    repair_object — a stale authorization racing a newer mutation is
    rejected with STALE_REPAIR, exactly-one-winner, never both landing.

    Type gate: ABANDONED_UNREPAIRABLE is an IntegrityStatus value; only
    integrity-bearing objects (goal/task/action — the types the schema gives
    an integrity column) can receive it. Plans/Steps/Verifications/Obligations
    have no integrity storage; abandoning them here would be a latent schema
    error. Their repair alternatives are status transitions (e.g. Plan
    supersession) handled through repair_object, not integrity settlement."""
    with store.write() as conn:
        kind = _object_type(conn, obj_id)
        if kind is None:
            return Rejected(R_NOT_FOUND, f"object {obj_id}", None)
        if kind not in ("goal", "task", "action"):
            return Rejected(R_INVALID_TRANSITION,
                f"ABANDONED_UNREPAIRABLE applies to integrity-bearing goal/task/action, got {kind}",
                None)
        row = conn.execute(f"SELECT * FROM {_TABLES[kind]} WHERE id = ?", (obj_id,)).fetchone()
        has_revision = "revision" in row.keys()
        if has_revision:
            if row["revision"] != expected_revision:
                store.audit(conn, obj_id, "repair_rejected", None, None,
                            authorized_by=auth.human,
                            reason=f"{R_STALE_REPAIR}: rev {row['revision']}")
                return Rejected(R_STALE_REPAIR,
                                f"revision={row['revision']} expected={expected_revision} — re-propose",
                                dict(row))
        if "integrity" in row.keys() and row["integrity"] == "ABANDONED_UNREPAIRABLE":
            return Rejected("ALREADY_ABANDONED", "object is already ABANDONED_UNREPAIRABLE", dict(row))
        if has_revision:
            conn.execute(
                f"UPDATE {_TABLES[kind]} SET integrity = 'ABANDONED_UNREPAIRABLE', revision = revision + 1 "
                "WHERE id = ? AND revision = ?",
                (obj_id, expected_revision),
            )
        else:
            conn.execute(
                f"UPDATE {_TABLES[kind]} SET integrity = 'ABANDONED_UNREPAIRABLE' WHERE id = ?",
                (obj_id,),
            )
        moved = transfer_subtree_obligations_to_uor_locked(
            conn, obj_id, reason=f"ABANDONED_UNREPAIRABLE of {obj_id} (Law 20)"
        )
        store.audit(
            conn, obj_id, "abandoned_unrepairable",
            row["integrity"] if "integrity" in row.keys() else None, "ABANDONED_UNREPAIRABLE",
            authorized_by=auth.human, reason=reason,
        )
        return Ok({"object": obj_id, "obligations_transferred": moved})


def _validate_aggregate(conn, kind: str, row, target_state: dict) -> Rejected | None:
    """Cross-object invariants the proposed repair must satisfy — the same
    checks the normal transition paths enforce, run against the proposed
    result before committing (Law 30: aggregate-scoped repair)."""
    if kind == "task" and "active_plan_id" in target_state:
        new_plan_id = target_state["active_plan_id"]
        if new_plan_id is not None:
            p = conn.execute("SELECT * FROM plans WHERE id = ?", (new_plan_id,)).fetchone()
            if p is None or p["task_id"] != row["id"]:
                return Rejected(R_AGGREGATE_INVALID,
                                "active_plan_id repair must point at a plan of this task", dict(row))
            if p["status"] not in (PlanStatus.DRAFT.value, PlanStatus.BLOCKED.value):
                return Rejected(R_AGGREGATE_INVALID,
                                "active_plan_id repair must point at a DRAFT/BLOCKED plan", dict(row))
        # current active plan, if any, must be superseded/cancelled or being replaced
        cur = row["active_plan_id"]
        if cur is not None and cur != new_plan_id:
            p = conn.execute("SELECT status FROM plans WHERE id = ?", (cur,)).fetchone()
            if p and p["status"] not in (
                PlanStatus.SUPERSEDED.value, PlanStatus.CANCELLED.value, PlanStatus.FAILED.value
            ):
                return Rejected(R_AGGREGATE_INVALID,
                                f"current active plan {cur} must be SUPERSEDED/CANCELLED/FAILED "
                                "before pointing elsewhere (Law 15/35)", dict(row))
    if kind == "plan" and "status" in target_state:
        new_status = str(target_state["status"]).upper()
        if new_status == PlanStatus.SUPERSEDED.value and not target_state.get("superseded_by") \
                and not row["superseded_by"]:
            return Rejected(R_AGGREGATE_INVALID,
                            "SUPERSEDED repair requires superseded_by (Law 34)", dict(row))
    if kind == "plan" and "dependency_graph" in target_state:
        # Law 13: a repair may not commit a cyclic graph. The plan's
        # dependency_graph is the committed authority (step.depends_on is a
        # denormalized copy kept in sync by add_dependency — which is why
        # step.depends_on is NOT independently repairable); the cycle check
        # here reuses the same `_has_cycle` the normal path enforces.
        from v5.work import _has_cycle
        try:
            proposed = (jload(target_state["dependency_graph"], None)
                        if isinstance(target_state["dependency_graph"], str)
                        else target_state["dependency_graph"])
        except Exception:
            proposed = None
        if not isinstance(proposed, dict):
            return Rejected(R_AGGREGATE_INVALID,
                            "dependency_graph repair must be a {step: [deps]} mapping", dict(row))
        # self-dependencies and cycles both violate Law 13
        graph = {k: set(v) for k, v in proposed.items()}
        if any(k in v for k, v in graph.items()):
            return Rejected(R_AGGREGATE_INVALID,
                            "dependency_graph repair contains a self-dependency (Law 13)", dict(row))
        if _has_cycle(graph):
            return Rejected(R_AGGREGATE_INVALID,
                            "dependency_graph repair would commit a cycle (Law 13)", dict(row))
    if kind == "obligation" and "owner" in target_state:
        if target_state["owner"] != UOR and \
           conn.execute("SELECT 1 FROM tasks WHERE id = ?", (target_state["owner"],)).fetchone() is None:
            return Rejected(R_AGGREGATE_INVALID,
                            "obligation owner must be a task id or UOR (Law 18)", dict(row))
    if kind == "verification" and "result" in target_state:
        new_result = str(target_state["result"]).upper()
        if new_result == "PASS" and row["result"] != "PASS":
            # Law 29 fabrication closure: a Verification's PASS is a persisted
            # evaluation outcome. Repair may not manufacture one.
            #   PENDING     -> no evaluation ever ran (nothing to substantiate PASS)
            #   RUNNING     -> the evaluation began but its outcome was never
            #                  persisted (crash #6); the human's belief that it
            #                  passed is INFERENCE-typed, never a manufactured
            #                  outcome (Law 29). Sanctioned recovery: re-run the
            #                  verification (run_verification accepts RUNNING).
            #   FAIL / INCONCLUSIVE -> a persisted outcome already exists;
            #                  rewriting it violates evidence immutability.
            # (The caller audits the rejection with the human identity.)
            return Rejected(R_FABRICATION,
                            f"verification result is {row['result']}, not PASS — repair cannot "
                            "manufacture a successful evaluation outcome. Re-run the verification "
                            "instead (recovery uses ordinary transitions; Law 29/30)", dict(row))
    return None
