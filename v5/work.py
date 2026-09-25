"""Work Service — the sole owner of Goal/Task/Plan/Step lifecycle (Law 12).

Cognition may propose; it may not mutate. There is no cognition module yet —
the lifecycle functions below ARE the only mutation surface, and every one of
them is a revision-checked atomic transition.

Implements Contract §4 (activate_plan / plan exclusivity), §5 (completion
policy + complete_object), §6-adjacent terminal transitions (cancel/archive),
and Law 13 dependencies (cycle-checked at commit time).
"""
from __future__ import annotations

from v5.enums import (
    GOAL_TERMINAL,
    GOAL_TRANSITIONS,
    PLAN_TERMINAL,
    PLAN_TRANSITIONS,
    STEP_TERMINAL,
    STEP_TRANSITIONS,
    TASK_TERMINAL,
    TASK_TRANSITIONS,
    GoalStatus,
    PlanStatus,
    StepStatus,
    TaskStatus,
)
from v5.ids import UOR, new_id
from v5.models import (
    CompletionPolicy,
    Goal,
    Ok,
    Plan,
    Rejected,
    Result,
    RetryBudget,
    Step,
    Task,
    R_COMPLETION_POLICY,
    R_DEPENDENCY_CYCLE,
    R_FROZEN,
    R_INVALID_TRANSITION,
    R_NOT_FOUND,
    R_PLAN_EXCLUSIVITY,
    R_PLAN_MISMATCH,
    R_PLAN_NOT_DRAFT,
    R_STALE_REVISION,
    R_TERMINAL,
    R_VERIFICATION_NOT_PASS,
)
from v5.obligations import transfer_subtree_obligations_to_uor_locked
from v5.store import Store, iso, jdump, jload, utcnow

# ── criterion registry for CompletionPolicy.CRITERION (Contract §5) ─────────
_CRITERIA: dict[str, object] = {}


def register_criterion(name: str, fn):
    """Register a named, testable evaluator for CRITERION policies.
    The evaluator receives the filtered children statuses and returns bool.
    Unknown criterion names evaluate to False (fail-safe)."""
    _CRITERIA[name] = fn


# ── row mapping ──────────────────────────────────────────────────────────────

def _policy_from_json(s: str) -> CompletionPolicy:
    d = jload(s, {})
    return CompletionPolicy(rule=d["rule"], n=d.get("n"), criterion_ref=d.get("criterion_ref"))


def load_goal(conn, goal_id: str) -> Goal | None:
    r = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    if not r:
        return None
    return Goal(
        id=r["id"], revision=r["revision"], status=GoalStatus(r["status"]),
        completion_policy=_policy_from_json(r["completion_policy"]),
        created_at=r["created_at"], session_origin=r["session_origin"],
        integrity=_integrity(r["integrity"]),
    )


def load_task(conn, task_id: str) -> Task | None:
    r = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not r:
        return None
    budget = jload(r["retry_budget"], {})
    return Task(
        id=r["id"], revision=r["revision"], goal_id=r["goal_id"],
        status=TaskStatus(r["status"]), active_plan_id=r["active_plan_id"],
        completion_policy=_policy_from_json(r["completion_policy"]),
        retry_budget=RetryBudget(**budget) if budget else RetryBudget(max_attempts=3),
        integrity=_integrity(r["integrity"]),
    )


def load_plan(conn, plan_id: str) -> Plan | None:
    r = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if not r:
        return None
    graph = {k: set(v) for k, v in jload(r["dependency_graph"], {}).items()}
    return Plan(
        id=r["id"], revision=r["revision"], task_id=r["task_id"],
        status=PlanStatus(r["status"]), superseded_by=r["superseded_by"],
        dependency_graph=graph,
    )


def load_step(conn, step_id: str) -> Step | None:
    r = conn.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone()
    if not r:
        return None
    return Step(
        id=r["id"], revision=r["revision"], plan_id=r["plan_id"],
        status=StepStatus(r["status"]), required=bool(r["required"]),
        depends_on=set(jload(r["depends_on"], [])),
        description=r["description"] if "description" in r.keys() else "",
        execution_capability=(r["execution_capability"] if "execution_capability" in r.keys() else ""),
    )


def _integrity(name: str):
    from v5.enums import IntegrityStatus
    return IntegrityStatus(name)


# ── derived Plan activity (Contract §4 — the ONLY definition) ───────────────

def plan_is_active(plan: Plan, task: Task) -> bool:
    """Derived, never stored. There is no PlanStatus.ACTIVE to check."""
    return (
        task.active_plan_id == plan.id
        and plan.status not in (PlanStatus.CANCELLED, PlanStatus.SUPERSEDED, PlanStatus.ARCHIVED)
    )


def get_task_plan_view(store: Store, task_id: str):
    """Convenience: (task, active_plan_or_None)."""
    conn = store.read()
    task = load_task(conn, task_id)
    if task is None:
        return None, None
    plan = load_plan(conn, task.active_plan_id) if task.active_plan_id else None
    return task, plan


# ── creation ─────────────────────────────────────────────────────────────────

def create_goal(store: Store, completion_policy: CompletionPolicy,
               session_origin: str | None = None) -> Goal:
    g = Goal(
        id=new_id("goal"), revision=0, status=GoalStatus.ACTIVE,
        completion_policy=completion_policy, created_at=iso(utcnow()),
        session_origin=session_origin,
    )
    with store.write() as conn:
        conn.execute(
            "INSERT INTO goals (id, revision, status, completion_policy, created_at, session_origin, integrity) "
            "VALUES (?,?,?,?,?,?, 'OK')",
            (g.id, 0, g.status.value, jdump({
                "rule": completion_policy.rule, "n": completion_policy.n,
                "criterion_ref": completion_policy.criterion_ref,
            }), g.created_at, session_origin),
        )
        store.audit(conn, g.id, "created", None, g.status.value)
    return g


def create_task(store: Store, goal_id: str, completion_policy: CompletionPolicy,
                retry_budget: RetryBudget | None = None,
                expected_goal_revision: int | None = None) -> Result:
    with store.write() as conn:
        goal = load_goal(conn, goal_id)
        if goal is None:
            return Rejected(R_NOT_FOUND, f"goal {goal_id}", None)
        if expected_goal_revision is not None and goal.revision != expected_goal_revision:
            return Rejected(R_STALE_REVISION,
                            f"goal revision={goal.revision} expected={expected_goal_revision}", goal)
        if goal.status in GOAL_TERMINAL:
            return Rejected(R_TERMINAL, f"goal {goal.status.value} — new work needs new identity (Law 14)", goal)
        budget = retry_budget or RetryBudget(max_attempts=3)
        t = Task(
            id=new_id("task"), revision=0, goal_id=goal_id, status=TaskStatus.PENDING,
            active_plan_id=None, completion_policy=completion_policy,
            retry_budget=budget,
        )
        conn.execute(
            "INSERT INTO tasks (id, revision, goal_id, status, active_plan_id, completion_policy, retry_budget, integrity) "
            "VALUES (?,?,?,?,?,?,?, 'OK')",
            (t.id, 0, goal_id, t.status.value, None,
             jdump({"rule": completion_policy.rule, "n": completion_policy.n,
                    "criterion_ref": completion_policy.criterion_ref}),
             jdump({"max_attempts": budget.max_attempts, "attempts_used": budget.attempts_used})),
        )
        store.audit(conn, t.id, "created", None, t.status.value)
        return Ok(t)


def create_plan(store: Store, task_id: str) -> Result:
    with store.write() as conn:
        task = load_task(conn, task_id)
        if task is None:
            return Rejected(R_NOT_FOUND, f"task {task_id}", None)
        if task.status in TASK_TERMINAL:
            return Rejected(R_TERMINAL, f"task {task.status.value} (Law 14)", task)
        p = Plan(
            id=new_id("plan"), revision=0, task_id=task_id, status=PlanStatus.DRAFT,
            superseded_by=None, dependency_graph={},
        )
        conn.execute(
            "INSERT INTO plans (id, revision, task_id, status, superseded_by, dependency_graph) "
            "VALUES (?,?,?,?,?, '{}')",
            (p.id, 0, task_id, p.status.value, None),
        )
        store.audit(conn, p.id, "created", None, p.status.value)
        return Ok(p)


def create_step(store: Store, plan_id: str, required: bool,
                depends_on: set[str] | None = None,
                description: str = "",
                execution_capability: str = "") -> Result:
    depends_on = set(depends_on or ())
    with store.write() as conn:
        plan = load_plan(conn, plan_id)
        if plan is None:
            return Rejected(R_NOT_FOUND, f"plan {plan_id}", None)
        if plan.status in PLAN_TERMINAL:
            return Rejected(R_TERMINAL, f"plan {plan.status.value} (Law 14)", plan)
        s = Step(
            id=new_id("step"), revision=0, plan_id=plan_id, status=StepStatus.PENDING,
            required=required, depends_on=depends_on,
            description=description, execution_capability=execution_capability,
        )
        graph = dict(plan.dependency_graph)
        graph[s.id] = set(depends_on)
        if _has_cycle(graph):
            return Rejected("DEPENDENCY_CYCLE", "step depends_on would create a cycle (Law 13)", plan)
        conn.execute(
            "INSERT INTO steps (id, revision, plan_id, status, required, depends_on, description, execution_capability) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (s.id, 0, plan_id, s.status.value, 1 if required else 0, jdump(sorted(depends_on)),
             description, execution_capability),
        )
        conn.execute(
            "UPDATE plans SET dependency_graph = ?, revision = revision + 1 WHERE id = ?",
            (jdump({k: sorted(v) for k, v in graph.items()}), plan_id),
        )
        store.audit(conn, s.id, "created", None, s.status.value)
        return Ok(s)


# ── dependencies (Law 13: acyclic, checked against the COMMITTED graph in the
#    same atomic mutation — never a separate pre-check) ───────────────────────

def _has_cycle(graph: dict[str, set[str]]) -> bool:
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {k: WHITE for k in graph}

    def visit(node: str) -> bool:
        color[node] = GRAY
        for dep in graph.get(node, ()):  # edge node -> dep (node depends on dep)
            if color.get(dep, WHITE) == GRAY:
                return True
            if color.get(dep, WHITE) == WHITE and visit(dep):
                return True
        color[node] = BLACK
        return False

    for node in list(graph):
        if color.get(node, WHITE) == WHITE:
            if visit(node):
                return True
    return False


def add_dependency(store: Store, plan_id: str, step_id: str,
                   depends_on_step_id: str, expected_plan_revision: int) -> Result:
    """Atomically add `step_id depends_on depends_on_step_id` to the plan's
    committed graph. The cycle check runs against the graph AS IT WILL BE
    after this commit, inside the same transaction (Law 13; Contract §9 row 8).
    Self-dependency is a trivial cycle and is rejected."""
    with store.write() as conn:
        plan = load_plan(conn, plan_id)
        if plan is None:
            return Rejected(R_NOT_FOUND, f"plan {plan_id}", None)
        if plan.revision != expected_plan_revision:
            return Rejected(R_STALE_REVISION, f"revision={plan.revision} expected={expected_plan_revision}", plan)
        if step_id == depends_on_step_id:
            return Rejected("DEPENDENCY_CYCLE", "self-dependency (Law 13)", plan)
        for sid in (step_id, depends_on_step_id):
            if conn.execute("SELECT 1 FROM steps WHERE id = ? AND plan_id = ?", (sid, plan_id)).fetchone() is None:
                return Rejected(R_NOT_FOUND, f"step {sid} not in plan", plan)
        graph = {k: set(v) for k, v in plan.dependency_graph.items()}
        graph.setdefault(step_id, set()).add(depends_on_step_id)
        if _has_cycle(graph):
            return Rejected("DEPENDENCY_CYCLE", "committed graph would contain a cycle (Law 13)", plan)
        conn.execute(
            "UPDATE plans SET dependency_graph = ?, revision = revision + 1 WHERE id = ? AND revision = ?",
            (jdump({k: sorted(v) for k, v in graph.items()}), plan_id, plan.revision),
        )
        row = conn.execute("SELECT depends_on FROM steps WHERE id = ?", (step_id,)).fetchone()
        deps = set(jload(row["depends_on"], []))
        deps.add(depends_on_step_id)
        conn.execute("UPDATE steps SET depends_on = ? WHERE id = ?", (jdump(sorted(deps)), step_id))
        store.audit(conn, plan_id, "dependency_added", None, f"{step_id} -> {depends_on_step_id}")
        return Ok()


def create_plan_with_steps(store: Store, task_id: str, step_specs: list[dict],
                           expected_task_revision: int) -> Result:
    """Atomic plan acceptance (Cognition Implementation Contract §11.6/§50):
    create the Plan, its Steps, and every verification-requirement binding in
    ONE transaction. Either the whole proposal commits or nothing does —
    propose_plan never leaves partial canonical state.

    step_specs entries (validated by Cognition first; re-validated here with
    the SAME rules, because Cognition validation is fast-fail, never the
    authority — §6):
      {
        "description": str,
        "required": bool,
        "depends_on_index": list[int],          # positions in THIS list only
        "execution_capability": str,
        "verification_requirements": [
            {"method_name": str, "applies_to_capability": str}, ...
        ],
      }

    Authoritative checks inside this transaction:
      * Task exists, revision CAS, lifecycle (not terminal);
      * non-empty step collection; every execution_capability is registered;
      * dependency indices valid, unique, non-self; graph acyclic (Law 13 —
        _has_cycle, the same check add_dependency uses);
      * for every verification requirement: method registered (§28) AND
        applies_to_capability == step.execution_capability (§11.5/§29);
      * requirement binding: create the claim + PENDING verification rows and
        bind them to the Task via the canonical binding table, in the SAME
        commit — Cognition never calls bind_required_verification (§27).
    """
    from v5 import capabilities as _caps
    from v5 import evidence as _ev
    from v5.enums import ClaimConfidence
    from v5.verification import get_method

    with store.write() as conn:
        task = load_task(conn, task_id)
        if task is None:
            return Rejected(R_NOT_FOUND, f"task {task_id}", None)
        if task.revision != expected_task_revision:
            return Rejected(R_STALE_REVISION,
                            f"task revision={task.revision} expected={expected_task_revision}", task)
        if task.status in TASK_TERMINAL:
            return Rejected(R_TERMINAL, f"task {task.status.value} (Law 14)", task)
        if not step_specs:
            return Rejected("MALFORMED_PROPOSAL", "steps must be a non-empty list", None)

        # authoritative re-validation of the step specs (mirror of the
        # adapter's checks — the trust boundary re-check, §6)
        n = len(step_specs)
        for i, spec in enumerate(step_specs):
            cap = spec.get("execution_capability", "")
            if not cap or not isinstance(cap, str):
                return Rejected("MALFORMED_PROPOSAL", f"step {i}: execution_capability is required", None)
            if _caps.get(cap) is None:
                return Rejected("MALFORMED_PROPOSAL", f"step {i}: unknown capability {cap!r}", None)
            deps = spec.get("depends_on_index", [])
            seen: set[int] = set()
            for d in deps:
                if not isinstance(d, int) or isinstance(d, bool):
                    return Rejected("MALFORMED_PROPOSAL", f"step {i}: dependency index {d!r} is not an int", None)
                if d < 0 or d >= n:
                    return Rejected("MALFORMED_PROPOSAL",
                                    f"step {i}: dependency index {d} is outside this plan's step list", None)
                if d in seen:
                    return Rejected("MALFORMED_PROPOSAL", f"step {i}: duplicate dependency index {d}", None)
                seen.add(d)
            for req in spec.get("verification_requirements", []):
                mn = req.get("method_name", "")
                atc = req.get("applies_to_capability", "")
                if not mn or not isinstance(mn, str):
                    return Rejected("MALFORMED_PROPOSAL", f"step {i}: verification method_name is required", None)
                if get_method(mn) is None:
                    return Rejected("UNKNOWN_VERIFICATION_METHOD",
                                    f"step {i}: unknown verification method {mn!r} (§28)", None)
                if atc != cap:
                    return Rejected("VERIFICATION_CAPABILITY_MISMATCH",
                                    f"step {i}: verification requirement applies_to_capability "
                                    f"{atc!r} != execution_capability {cap!r} (§11.5/§29)", None)

        # dependency graph over positions -> build after ids are minted
        plan = Plan(id=new_id("plan"), revision=0, task_id=task_id,
                    status=PlanStatus.DRAFT, superseded_by=None, dependency_graph={})
        step_ids = [new_id("step") for _ in step_specs]
        graph: dict[str, set] = {}
        for i, spec in enumerate(step_specs):
            graph[step_ids[i]] = {step_ids[d] for d in spec.get("depends_on_index", [])}
        if _has_cycle(graph):
            return Rejected(R_DEPENDENCY_CYCLE, "dependency graph would contain a cycle (Law 13)", None)

        conn.execute(
            "INSERT INTO plans (id, revision, task_id, status, superseded_by, dependency_graph) "
            "VALUES (?,?,?,?,?,?)",
            (plan.id, 0, task_id, plan.status.value, None,
             jdump({k: sorted(v) for k, v in graph.items()})),
        )
        steps: list[Step] = []
        bound_requirements: list[dict] = []
        for i, spec in enumerate(step_specs):
            s = Step(id=step_ids[i], revision=0, plan_id=plan.id,
                     status=StepStatus.PENDING, required=bool(spec.get("required", True)),
                     depends_on=set(graph[step_ids[i]]),
                     description=spec.get("description", ""),
                     execution_capability=spec["execution_capability"])
            conn.execute(
                "INSERT INTO steps (id, revision, plan_id, status, required, depends_on, description, execution_capability) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (s.id, 0, plan.id, s.status.value, 1 if s.required else 0,
                 jdump(sorted(graph[step_ids[i]])), s.description, s.execution_capability),
            )
            store.audit(conn, s.id, "created", None, s.status.value)
            steps.append(s)
            for req in spec.get("verification_requirements", []):
                # declare the requirement as claim + PENDING verification, bound
                # to the Task (the completion authority's Law 16 gate) —
                # Work Service is the sole caller of the binding path (§27)
                claim = _ev._create_claim_locked(
                    store, conn,
                    f"step outcome satisfied: {spec.get('description', '')[:120]}",
                    [], "work_service", ClaimConfidence.UNVERIFIED,
                )
                if isinstance(claim, Rejected):
                    return claim
                ver = _ev._create_verification_locked(
                    store, conn, claim.value.id, req["method_name"], "deterministic",
                    step_id=s.id,   # requirement provenance: the Step that
                                    # declared this verification (audit fix)
                )
                if isinstance(ver, Rejected):
                    return ver
                bound = _bind_required_verification_locked(conn, task_id, ver.value.id)
                if isinstance(bound, Rejected):
                    return bound
                bound_requirements.append(
                    {"step_id": s.id, "method_name": req["method_name"],
                     "verification_id": ver.value.id, "claim_id": claim.value.id}
                )
        store.audit(conn, plan.id, "created", None, plan.status.value,
                    reason=f"plan with {len(steps)} steps (cognition proposal)")
        return Ok({"plan": plan, "steps": steps, "bound_verifications": bound_requirements})


# ── Plan activation (Contract §4) ─────────────────────────────────────────────
def activate_plan(store: Store, task_id: str, plan_id: str,
                  expected_task_revision: int) -> Result:
    """SINGLE atomic operation — the only legal way to make a Plan active.

    Precondition (all checked in the SAME transaction):
      - Task.revision == expected_task_revision
      - Task.status in {ACTIVE, BLOCKED}
      - Task.active_plan_id is None OR points to a plan already
        SUPERSEDED/CANCELLED/FAILED
      - Plan.status == DRAFT (and the plan belongs to this task)
    Postcondition: Task.active_plan_id = plan_id; Task.revision += 1.
    Failure: atomic reject, authoritative current Task returned (Law 4).
    """
    with store.write() as conn:
        task = load_task(conn, task_id)
        if task is None:
            return Rejected(R_NOT_FOUND, f"task {task_id}", None)
        if task.revision != expected_task_revision:
            store.audit(conn, task_id, "rejected", None, None, reason=f"activate_plan {R_STALE_REVISION}")
            return Rejected(R_STALE_REVISION, f"revision={task.revision} expected={expected_task_revision}", task)
        if task.status not in (TaskStatus.ACTIVE, TaskStatus.BLOCKED):
            store.audit(conn, task_id, "rejected", task.status.value, None,
                        reason="activate_plan: task not ACTIVE/BLOCKED")
            return Rejected(R_INVALID_TRANSITION, f"task status {task.status.value}", task)
        if task.active_plan_id is not None:
            cur = load_plan(conn, task.active_plan_id)
            if cur is None or cur.status not in (
                PlanStatus.SUPERSEDED, PlanStatus.CANCELLED, PlanStatus.FAILED
            ):
                return Rejected(R_PLAN_EXCLUSIVITY,
                                f"active plan {task.active_plan_id} not superseded/cancelled/failed", task)
        plan = load_plan(conn, plan_id)
        if plan is None:
            return Rejected(R_NOT_FOUND, f"plan {plan_id}", task)
        if plan.task_id != task_id:
            return Rejected(R_PLAN_MISMATCH, "plan belongs to another task", task)
        if plan.status != PlanStatus.DRAFT:
            return Rejected(R_PLAN_NOT_DRAFT, f"plan status {plan.status.value}", task)

        store.crash_point("activate_plan_commit")
        conn.execute(
            "UPDATE tasks SET active_plan_id = ?, revision = revision + 1 WHERE id = ? AND revision = ?",
            (plan_id, task_id, expected_task_revision),
        )
        store.audit(conn, task_id, "plan_activated", task.active_plan_id, plan_id)
        return Ok(load_task(conn, task_id))


def supersede_plan(store: Store, task_id: str, old_plan_id: str, new_plan_id: str,
                   expected_task_revision: int) -> Result:
    """Atomic plan replacement: old plan -> SUPERSEDED (superseded_by = new)
    AND Task.active_plan_id -> new plan in ONE transaction.

    This operation is the storage representation the Contract's crash test #7
    requires: SUPERSEDED write and active_plan_id write must be unobservable
    as two steps. activate_plan (§4) handles the no-active/dead-active case;
    this handles the live-active case with the same guards.

    Same preconditions as activate_plan, plus Task.active_plan_id == old_plan_id.
    """
    with store.write() as conn:
        task = load_task(conn, task_id)
        if task is None:
            return Rejected(R_NOT_FOUND, f"task {task_id}", None)
        if task.revision != expected_task_revision:
            return Rejected(R_STALE_REVISION, f"revision={task.revision} expected={expected_task_revision}", task)
        if task.status not in (TaskStatus.ACTIVE, TaskStatus.BLOCKED):
            return Rejected(R_INVALID_TRANSITION, f"task status {task.status.value}", task)
        if task.active_plan_id != old_plan_id:
            return Rejected(R_PLAN_EXCLUSIVITY,
                            f"active plan is {task.active_plan_id}, not {old_plan_id}", task)
        old = load_plan(conn, old_plan_id)
        if old is None or old.task_id != task_id:
            return Rejected(R_PLAN_MISMATCH, "old plan mismatch", task)
        new = load_plan(conn, new_plan_id)
        if new is None or new.task_id != task_id:
            return Rejected(R_PLAN_MISMATCH, "new plan mismatch", task)
        if new.status != PlanStatus.DRAFT:
            return Rejected(R_PLAN_NOT_DRAFT, f"new plan status {new.status.value}", task)

        store.crash_point("supersede_plan_commit")
        conn.execute(
            "UPDATE plans SET status = 'SUPERSEDED', superseded_by = ?, revision = revision + 1 "
            "WHERE id = ? AND revision = ?",
            (new_plan_id, old_plan_id, old.revision),
        )
        store.crash_point("supersede_between_writes")  # crash #7: the two writes are ONE transaction
        conn.execute(
            "UPDATE tasks SET active_plan_id = ?, revision = revision + 1 WHERE id = ? AND revision = ?",
            (new_plan_id, task_id, expected_task_revision),
        )
        store.audit(conn, task_id, "plan_superseded", old_plan_id, new_plan_id)
        return Ok(load_task(conn, task_id))


# ── completion policy (Contract §5) ───────────────────────────────────────────

_SATISFYING_STEP = {StepStatus.COMPLETED, StepStatus.SKIPPED}
_SATISFYING_TASK = {TaskStatus.COMPLETED}
_EXCLUDED_STEP = {StepStatus.CANCELLED}   # obligations already transferred at cancel (Law 20)
_EXCLUDED_TASK = {TaskStatus.CANCELLED, TaskStatus.ARCHIVED}


def evaluate_completion(policy: CompletionPolicy,
                        children_status: list) -> bool:
    """Pure function over the declared policy (Contract §5). Does NOT touch
    obligations. Children filtered by the caller (see complete_object).

    Interpretations (DECISIONS.md): SKIPPED counts as satisfying (deliberate,
    Work-authorized); CANCELLED/ARCHIVED children are excluded upstream because
    their unresolved obligations were already transferred at their own
    terminal transition; unknown CRITERION names fail safe (False).
    """
    if not children_status and policy.rule in ("ALL_REQUIRED",):
        return False  # empty set of children cannot demonstrate achievement
    if policy.rule == "ALL_REQUIRED":
        sat = _SATISFYING_STEP if all(isinstance(s, StepStatus) for s in children_status) else _SATISFYING_TASK
        return all(s in sat for s in children_status)
    if policy.rule == "ANY_REQUIRED":
        sat = _SATISFYING_STEP if all(isinstance(s, StepStatus) for s in children_status) else _SATISFYING_TASK
        return any(s in sat for s in children_status)
    if policy.rule == "N_OF_M":
        sat = _SATISFYING_STEP if all(isinstance(s, StepStatus) for s in children_status) else _SATISFYING_TASK
        return sum(1 for s in children_status if s in sat) >= (policy.n or 0)
    if policy.rule == "CRITERION":
        fn = _CRITERIA.get(policy.criterion_ref or "")
        if fn is None:
            return False  # fail safe: unknown criterion cannot complete anything
        return bool(fn(children_status))
    return False


def _children_status_locked(conn, obj) -> list:
    """Children of a Task = steps of its ACTIVE plan (if any), CANCELLED steps
    excluded (their obligations transferred at their own terminal transition).
    Children of a Goal = its tasks, CANCELLED/ARCHIVED excluded."""
    if isinstance(obj, Task):
        if not obj.active_plan_id:
            return []
        rows = conn.execute(
            "SELECT status FROM steps WHERE plan_id = ? AND status != 'CANCELLED'", (obj.active_plan_id,)
        ).fetchall()
        return [StepStatus(r["status"]) for r in rows]
    if isinstance(obj, Goal):
        rows = conn.execute(
            "SELECT status FROM tasks WHERE goal_id = ? AND status NOT IN ('CANCELLED','ARCHIVED')",
            (obj.id,),
        ).fetchall()
        return [TaskStatus(r["status"]) for r in rows]
    return []


def bind_required_verification(store: Store, object_id: str, verification_id: str) -> Result:
    """Persist which Verifications a completion criterion requires (Law 16:
    completion requires every required Verification PASS — not merely
    present). This binding table is the storage representation of "required
    Verification" (contract leaves the representation unspecified).

    Both endpoints must exist: a dangling binding to a nonexistent object is
    an orphan row that proves nothing about anything (the bound object is the
    thing completion is claimed for). The completion authority supports
    Task|Goal (`complete_object`) — bindings to other kinds have no effect and
    are rejected at bind time: fail fast, not silently."""
    with store.write() as conn:
        return _bind_required_verification_locked(conn, object_id, verification_id)


def _bind_required_verification_locked(conn, object_id: str, verification_id: str) -> Result:
    """Locked core — composable inside Work Service's own transactions (the
    atomic plan acceptance binds declared requirements in the same commit)."""
    obj, kind = _load_object(conn, object_id)
    if obj is None:
        return Rejected(R_NOT_FOUND, f"binding target {object_id}", None)
    if kind not in ("task", "goal"):
        return Rejected(R_INVALID_TRANSITION,
                        f"required-verification bindings apply to completable Task|Goal, got {kind}",
                        obj)
    if conn.execute("SELECT 1 FROM verifications WHERE id = ?", (verification_id,)).fetchone() is None:
        return Rejected(R_NOT_FOUND, f"verification {verification_id}", None)
    conn.execute(
        "INSERT OR IGNORE INTO required_verifications (object_id, verification_id) VALUES (?,?)",
        (object_id, verification_id),
    )
    return Ok()


def complete_object(store: Store, obj_id: str, expected_revision: int) -> Result:
    """Contract §5 complete_object — Task or Goal.

    Precondition (one transaction):
      - revision matches
      - integrity not FROZEN (no autonomous mutation on frozen objects)
      - completion policy satisfied (evaluate_completion over filtered children)
      - every required Verification has result == PASS (Law 16)
    Postcondition (SAME atomic operation):
      - status = COMPLETED, revision += 1
      - every OPEN subtree obligation transfers to UOR (Law 20 — no exceptions)
      - Task terminal transitions clear active_plan_id (derived activity stays
        consistent: a terminal task has no active plan)
    """
    with store.write() as conn:
        obj, kind = _load_object(conn, obj_id)
        if obj is None:
            return Rejected(R_NOT_FOUND, f"object {obj_id}", None)
        if obj.revision != expected_revision:
            store.audit(conn, obj_id, "rejected", None, reason=f"complete {R_STALE_REVISION}")
            return Rejected(R_STALE_REVISION, f"revision={obj.revision} expected={expected_revision}", obj)
        if obj.integrity.value == "FROZEN":
            return Rejected(R_FROZEN, "frozen objects cannot autonomously complete (Law 5)", obj)
        if obj.integrity.value == "ABANDONED_UNREPAIRABLE":
            return Rejected(R_FROZEN, "ABANDONED_UNREPAIRABLE is terminal", obj)
        if kind not in ("task", "goal"):
            return Rejected(R_INVALID_TRANSITION, f"complete_object supports Task|Goal, got {kind}", obj)

        table = "tasks" if kind == "task" else "goals"
        transitions = TASK_TRANSITIONS if kind == "task" else GOAL_TRANSITIONS
        status_enum = TaskStatus if kind == "task" else GoalStatus
        if obj.status not in transitions:
            return Rejected(R_INVALID_TRANSITION,
                            f"{obj.status.value} is terminal or non-completable (Law 14)", obj)

        children = _children_status_locked(conn, obj)
        if not evaluate_completion(obj.completion_policy, children):
            store.audit(conn, obj_id, "rejected", obj.status.value, reason="completion policy unsatisfied")
            return Rejected(R_COMPLETION_POLICY,
                            f"rule={obj.completion_policy.rule} children={[s.value for s in children]}", obj)

        for row in conn.execute(
            "SELECT verification_id FROM required_verifications WHERE object_id = ?", (obj_id,)
        ).fetchall():
            v = conn.execute(
                "SELECT result FROM verifications WHERE id = ?", (row["verification_id"],)
            ).fetchone()
            if v is None or v["result"] != "PASS":
                return Rejected(R_VERIFICATION_NOT_PASS,
                                f"verification {row['verification_id']} result={v['result'] if v else 'MISSING'}", obj)

        store.crash_point("complete_object_commit")
        conn.execute(
            f"UPDATE {table} SET status = 'COMPLETED', revision = revision + 1 WHERE id = ? AND revision = ?",
            (obj_id, expected_revision),
        )
        if kind == "task":
            conn.execute(
                "UPDATE tasks SET active_plan_id = NULL WHERE id = ? AND active_plan_id IS NOT NULL",
                (obj_id,),
            )
        moved = transfer_subtree_obligations_to_uor_locked(
            conn, obj_id, reason=f"terminal COMPLETED transition of {obj_id} (Law 20)",
            store=store,
        )
        # crash point INSIDE the transfer loop window: nothing may commit partially
        store.crash_point("complete_object_after_transfer")
        store.audit(conn, obj_id, "completed", obj.status.value, "COMPLETED")
        return Ok({"object": _load_object(conn, obj_id)[0], "obligations_transferred": moved})


# ── other terminal transitions (cancel / archive) ────────────────────────────

def cancel_task(store: Store, task_id: str, expected_revision: int) -> Result:
    return _terminal_transition(store, "tasks", task_id, expected_revision,
                                TaskStatus.CANCELLED, TaskStatus, TASK_TRANSITIONS, clear_plan=True)


def cancel_goal(store: Store, goal_id: str, expected_revision: int) -> Result:
    return _terminal_transition(store, "goals", goal_id, expected_revision,
                               GoalStatus.CANCELLED, GoalStatus, GOAL_TRANSITIONS)


def archive_object(store: Store, obj_id: str, expected_revision: int) -> Result:
    with store.write() as conn:
        obj, kind = _load_object(conn, obj_id)
        if obj is None:
            return Rejected(R_NOT_FOUND, obj_id, None)
    if kind == "task":
        return _terminal_transition(store, "tasks", obj_id, expected_revision,
                                    TaskStatus.ARCHIVED, TaskStatus, TASK_TRANSITIONS, clear_plan=True)
    if kind == "goal":
        return _terminal_transition(store, "goals", obj_id, expected_revision,
                                    GoalStatus.ARCHIVED, GoalStatus, GOAL_TRANSITIONS)
    return Rejected(R_INVALID_TRANSITION, f"archive supports Task|Goal, got {kind}", obj)


def _terminal_transition(store: Store, table: str, obj_id: str, expected_revision: int,
                         target, status_enum, transitions, clear_plan: bool = False) -> Result:
    """Generic terminal transition: status CAS + revision bump + atomic Law 20
    subtree obligation transfer, all in ONE transaction. Terminal states are
    guarded by the explicit transition table (Law 14/34)."""
    with store.write() as conn:
        obj, kind = _load_object(conn, obj_id)
        if obj is None:
            return Rejected(R_NOT_FOUND, obj_id, None)
        if obj.revision != expected_revision:
            return Rejected(R_STALE_REVISION, f"revision={obj.revision} expected={expected_revision}", obj)
        if obj.integrity.value == "FROZEN":
            return Rejected(R_FROZEN, "frozen objects cannot transition (Law 5)", obj)
        if obj.integrity.value == "ABANDONED_UNREPAIRABLE":
            return Rejected(R_FROZEN, "ABANDONED_UNREPAIRABLE is terminal", obj)
        if obj.status not in transitions:
            return Rejected(R_INVALID_TRANSITION, f"{obj.status.value} is terminal (Law 14)", obj)
        if target not in transitions.get(obj.status, frozenset()):
            return Rejected(R_INVALID_TRANSITION, f"{obj.status.value} -> {target.value} not contracted", obj)

        conn.execute(
            f"UPDATE {table} SET status = ?, revision = revision + 1 WHERE id = ? AND revision = ?",
            (target.value, obj_id, expected_revision),
        )
        if clear_plan and table == "tasks":
            conn.execute("UPDATE tasks SET active_plan_id = NULL WHERE id = ?", (obj_id,))
        moved = transfer_subtree_obligations_to_uor_locked(
            conn, obj_id, reason=f"terminal {target.value} transition of {obj_id} (Law 20)",
            store=store,
        )
        store.audit(conn, obj_id, f"terminal:{target.value.lower()}", obj.status.value, target.value)
        return Ok({"object": _load_object(conn, obj_id)[0], "obligations_transferred": moved})


# ── non-terminal lifecycle transitions ────────────────────────────────────────

def transition_object(store: Store, obj_id: str, target_status, expected_revision: int) -> Result:
    """Explicit non-terminal transition (pause/resume/block/unblock etc.).
    Every transition must satisfy the table — nothing implicit (Law 34).

    Work objects only: actions execute through the Execution Service's own
    contracted transitions (Law 8/9/14), so an action id here is a
    routing error, not a transition — rejected, never a KeyError crash."""
    with store.write() as conn:
        obj, kind = _load_object(conn, obj_id)
        if obj is None:
            return Rejected(R_NOT_FOUND, obj_id, None)
        if obj.revision != expected_revision:
            return Rejected(R_STALE_REVISION, f"revision={obj.revision} expected={expected_revision}", obj)
        if kind == "action":
            return Rejected(R_INVALID_TRANSITION,
                            "actions transition through the Execution Service (begin/mark/...), "
                            "not transition_object (Law 12: authority separation)", obj)
        table, enum, transitions = {
            "goal": ("goals", GoalStatus, GOAL_TRANSITIONS),
            "task": ("tasks", TaskStatus, TASK_TRANSITIONS),
            "plan": ("plans", PlanStatus, PLAN_TRANSITIONS),
            "step": ("steps", StepStatus, STEP_TRANSITIONS),
        }[kind]
        integrity = getattr(obj, "integrity", None)
        if integrity is not None and integrity.value in ("FROZEN", "ABANDONED_UNREPAIRABLE"):
            store.audit(conn, obj_id, "rejected", None, None,
                        reason=f"integrity={integrity.value} — no autonomous transition (Law 5/29)")
            return Rejected(R_FROZEN, f"integrity={integrity.value} — no autonomous transition (Law 5)", obj)
        allowed = transitions.get(obj.status, frozenset())
        if target_status not in allowed:
            store.audit(conn, obj_id, "rejected", obj.status.value, None,
                        reason=f"{target_status.value} not contracted from {obj.status.value} (Law 34)")
            return Rejected(R_INVALID_TRANSITION,
                            f"{obj.status.value} -> {target_status.value} not contracted (Law 34)", obj)
        conn.execute(
            f"UPDATE {table} SET status = ?, revision = revision + 1 WHERE id = ? AND revision = ?",
            (target_status.value, obj_id, expected_revision),
        )
        store.audit(conn, obj_id, "transition", obj.status.value, target_status.value)
        return Ok(_load_object(conn, obj_id)[0])


def complete_step(store: Store, step_id: str, expected_revision: int) -> Result:
    with store.write() as conn:
        step = load_step(conn, step_id)
        if step is None:
            return Rejected(R_NOT_FOUND, step_id, None)
        if step.revision != expected_revision:
            return Rejected(R_STALE_REVISION, f"revision={step.revision}", step)
        allowed = STEP_TRANSITIONS.get(step.status, frozenset())
        if StepStatus.COMPLETED not in allowed:
            return Rejected(R_INVALID_TRANSITION, f"{step.status.value} -> COMPLETED not contracted", step)
        conn.execute(
            "UPDATE steps SET status = 'COMPLETED', revision = revision + 1 WHERE id = ? AND revision = ?",
            (step_id, expected_revision),
        )
        store.audit(conn, step_id, "transition", step.status.value, "COMPLETED")
        return Ok(load_step(conn, step_id))


# ── helpers ───────────────────────────────────────────────────────────────────

# ── integrity freeze (Law 5) for Work-owned objects ──────────────────────────

def freeze_object(store: Store, obj_id: str, reason: str) -> Result:
    """Law 5 integrity freeze for goals/tasks: no autonomous mutation until
    repair. Both conflicting representations are preserved verbatim — nothing
    is merged or deleted here.

    Type gate: only Goal/Task carry an `integrity` column per the contracted
    schema. Plans/Steps do not — the freeze concept does not exist for them in
    the Contract (their lifecycle is governed by status transitions and Plan
    supersession). Freezing them here would attempt to set a non-existent
    column (latent SQL error); they are rejected explicitly instead. Idempotent:
    an object already FROZEN returns Ok without another mutation/audit entry."""
    with store.write() as conn:
        obj, kind = _load_object(conn, obj_id)
        if obj is None:
            return Rejected(R_NOT_FOUND, obj_id, None)
        if kind not in ("goal", "task"):
            return Rejected(R_INVALID_TRANSITION,
                            f"Law 5 freeze applies to integrity-bearing goal/task, got {kind}", obj)
        if obj.integrity.value == "FROZEN":
            return Ok({"frozen": obj_id, "idempotent": True})
        table = {"goal": "goals", "task": "tasks"}[kind]
        conn.execute(
            f"UPDATE {table} SET integrity = 'FROZEN', revision = revision + 1 "
            "WHERE id = ? AND revision = ?",
            (obj_id, obj.revision),
        )
        store.audit(conn, obj_id, "integrity_frozen", obj.status.value, None, reason=reason)
        return Ok(_load_object(conn, obj_id)[0])


def _load_object(conn, obj_id: str):
    for loader, kind in ((load_goal, "goal"), (load_task, "task"),
                          (load_plan, "plan"), (load_step, "step")):
        obj = loader(conn, obj_id)
        if obj is not None:
            return obj, kind
    from v5.execution import load_action
    obj = load_action(conn, obj_id)
    return (obj, "action") if obj is not None else (None, None)


def get_owning_task_id(store: Store, action_id: str) -> str | None:
    """action -> step -> plan -> task navigation."""
    conn = store.read()
    a = conn.execute("SELECT step_id FROM actions WHERE id = ?", (action_id,)).fetchone()
    if not a:
        return None
    s = conn.execute("SELECT plan_id FROM steps WHERE id = ?", (a["step_id"],)).fetchone()
    if not s:
        return None
    p = conn.execute("SELECT task_id FROM plans WHERE id = ?", (s["plan_id"],)).fetchone()
    return p["task_id"] if p else None
