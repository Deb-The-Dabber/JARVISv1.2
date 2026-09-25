"""Cognition membrane (Cognition Implementation Contract v1.1).

    Cognition proposes. Work Service commits.

This module is an adapter/validation boundary, not a second authority. It:
  1. receives model intent/proposals;
  2. validates structure, bounds, references, and capabilities (fast-fail);
  3. validates session legitimacy + Cognition authorization (§13.3);
  4. calls the authoritative Work Service mutation path;
  5. returns the Work Service's Result untouched — never rewritten (§15).

It never: mutates canonical state directly, executes capabilities, creates
verification results, repairs anything, or lets model output choose
session/principal identities (§13.2).

The four propose_* signatures are frozen by the Cognition Interface
Contract v1.0 §2 and must not change. The Store and the authenticated
context are threaded internally by the calling code (Interface Contract §2's
explicit allowance), never supplied in a proposal payload.
"""
from __future__ import annotations

import contextvars
import json
import logging
from dataclasses import dataclass, field

from v5 import capabilities as _caps
from v5 import sessions as _sessions
from v5 import work as _work
from v5.models import (
    CompletionPolicy,
    Ok,
    Rejected,
    Result,
    R_MALFORMED_PROPOSAL,
    R_UNAUTHORIZED,
)
from v5.store import Store

Id = str  # Interface Contract §1 — identifiers are str (prefixed ULIDs)

log = logging.getLogger("jarvis.v5.cognition")

# ── Proposal data types (Interface Contract §4 — exact shapes) ───────────────

@dataclass
class VerificationRequirement:
    method_name: str
    applies_to_capability: str


@dataclass
class StepProposal:
    description: str
    required: bool
    depends_on_index: list[int]
    execution_capability: str
    verification_requirements: list[VerificationRequirement] = field(
        default_factory=list
    )


@dataclass
class ActionProposal:
    capability: str
    arguments: dict


# ── Bounds (§8 — centralized constants, no scattered magic numbers) ──────────
# Interface Contract §4 pins the step-description bound at 500. The remaining
# bounds are implementation-defined; conservative values, documented in
# DECISIONS.md under the Cognition implementation entry.

MAX_STATEMENT_LEN = 2000           # goal/task statements (impl-defined bound)
MAX_STEP_DESCRIPTION_LEN = 500     # Interface Contract §4 — explicitly pinned
MAX_PLAN_STEPS = 20                # one proposal = one bounded fixed Plan
MAX_DEPENDENCIES_PER_STEP = 20
MAX_VERIFICATION_REQUIREMENTS = 8  # per step
MAX_CAPABILITY_ARGS = 32           # top-level argument keys
MAX_CAPABILITY_ARG_BYTES = 64 * 1024  # serialized argument size
MAX_CAPABILITY_ARG_DEPTH = 8       # recursive nesting depth
MAX_VERIFICATION_METHOD_LEN = 128


# ── Authenticated Cognition context (§13.2) — internal plumbing only ─────────

@dataclass(frozen=True)
class AuthenticatedCognitionContext:
    session_id: Id          # ephemeral — identifies this conversation/session only
    principal_id: Id        # durable — deployment config; ONE valid value (§13.1)
    session_state: object   # SessionState
    cognition_authorized: bool


_current_store: contextvars.ContextVar[Store | None] = contextvars.ContextVar(
    "v5_cognition_store", default=None
)
_current_context: contextvars.ContextVar[AuthenticatedCognitionContext | None] = (
    contextvars.ContextVar("v5_cognition_context", default=None)
)


def bind_store(store: Store) -> None:
    """Trusted host harness binds the canonical Store to this request context.
    Internal plumbing — never exposed in a proposal payload."""
    _current_store.set(store)


def _store() -> Store:
    s = _current_store.get()
    if s is None:
        raise RuntimeError("cognition.bbind_store(store) must be set by the host first")
    return s


def authenticate_session(session_id: Id) -> Result:
    """Authenticate a session through the existing Session authority and
    thread its context. Called only by the trusted host harness (terminal/API),
    never by the model — session identity comes from trusted authentication
    state, not proposal data (§13.2)."""
    s = _sessions.resolve_session(_store(), session_id)
    if s is None or s.state.value != "LIVE" or not s.cognition_authorized:
        # a failed authentication swap must not leave a prior authorized
        # context threaded — the caller believes it is operating as the new
        # session, so the old one must be dropped immediately
        _current_context.set(None)
        return Rejected(R_UNAUTHORIZED,
                        f"session {session_id} is not a live Cognition-authorized session (§13.3)",
                        None)
    ctx = AuthenticatedCognitionContext(
        session_id=s.id,
        principal_id=_sessions.OWNER_PRINCIPAL_ID,
        session_state=s.state,
        cognition_authorized=s.cognition_authorized,
    )
    _current_context.set(ctx)
    return Ok(ctx)


def deauthenticate() -> None:
    """Drop the threaded context (end of request / session teardown)."""
    _current_context.set(None)


def current_context() -> AuthenticatedCognitionContext | None:
    return _current_context.get()


def _require_context() -> AuthenticatedCognitionContext | Rejected:
    """§13.3's one authorization rule: the threaded session must be live and
    Cognition-authorized. Re-resolved against the Session authority on every
    call so legitimacy is current, not cached — NO comparison against any
    Work object ever happens here (single-principal model)."""
    ctx = _current_context.get()
    if ctx is None:
        return Rejected(R_UNAUTHORIZED,
                        "no authenticated Cognition session in this context", None)
    s = _sessions.resolve_session(_store(), ctx.session_id)
    if s is None or s.state.value != "LIVE" or not s.cognition_authorized:
        return Rejected(R_UNAUTHORIZED,
                        f"session {ctx.session_id} is not Cognition-authorized (§13.3)", None)
    return ctx


# ── Validation helpers (§7 — fail fast, never silently normalize) ─────────────

def _malformed(reason: str) -> Rejected:
    log.info("proposal_rejected reason=%s", reason)
    return Rejected(R_MALFORMED_PROPOSAL, reason, None)


def _validate_statement(statement: str, field_name: str, max_len: int) -> Rejected | None:
    if statement is None:
        return _malformed(f"{field_name} is required")
    if not isinstance(statement, str):
        return _malformed(f"{field_name} must be a string")
    if not statement.strip():
        return _malformed(f"{field_name} must be non-empty")
    if len(statement) > max_len:
        return _malformed(f"{field_name} exceeds bound {max_len}")
    return None


def _validate_completion_policy(policy) -> Rejected | None:
    if policy is None:
        return _malformed("completion_policy is required")
    if not isinstance(policy, CompletionPolicy):
        return _malformed("completion_policy must be a CompletionPolicy")
    if policy.rule not in ("ALL_REQUIRED", "ANY_REQUIRED", "N_OF_M", "CRITERION"):
        return _malformed(f"completion_policy.rule {policy.rule!r} is invalid")
    if policy.rule == "N_OF_M" and (
        not isinstance(policy.n, int) or isinstance(policy.n, bool) or policy.n < 1
    ):
        return _malformed("completion_policy.n must be a positive int for N_OF_M")
    if policy.rule == "CRITERION" and (
        not isinstance(policy.criterion_ref, str) or not policy.criterion_ref
    ):
        return _malformed("completion_policy.criterion_ref is required for CRITERION")
    return None


def _check_arg_depth(value, depth: int) -> bool:
    if depth > MAX_CAPABILITY_ARG_DEPTH:
        return False
    if isinstance(value, dict):
        return all(
            isinstance(k, str) and _check_arg_depth(v, depth + 1)
            for k, v in value.items()
        )
    if isinstance(value, (list, tuple)):
        return all(_check_arg_depth(v, depth + 1) for v in value)
    return True


def _validate_capability_and_args(capability, arguments) -> Rejected | None:
    if capability is None:
        return _malformed("capability is required")
    if not isinstance(capability, str) or not capability:
        return _malformed("capability must be a non-empty string")
    spec = _caps.get(capability)
    if spec is None:
        return _malformed(f"unknown capability {capability!r}")
    if arguments is None:
        return _malformed("arguments is required")
    if not isinstance(arguments, dict):
        return _malformed("arguments must be an object")
    if len(arguments) > MAX_CAPABILITY_ARGS:
        return _malformed(f"arguments has {len(arguments)} keys, bound is {MAX_CAPABILITY_ARGS}")
    try:
        serialized = json.dumps(arguments)
    except (TypeError, ValueError):
        return _malformed("arguments must be JSON-serializable")
    if len(serialized) > MAX_CAPABILITY_ARG_BYTES:
        return _malformed(f"arguments exceed bound {MAX_CAPABILITY_ARG_BYTES} bytes")
    if not _check_arg_depth(arguments, 1):
        return _malformed(f"arguments exceed nesting depth {MAX_CAPABILITY_ARG_DEPTH}")
    if spec.validate_args is not None:
        err = spec.validate_args(arguments)
        if err is not None:
            return _malformed(f"capability argument invalid: {err}")
    return None


def _validate_expected_revision(value, field_name: str) -> Rejected | None:
    if value is None:
        return _malformed(f"{field_name} is required")
    if not isinstance(value, int) or isinstance(value, bool):
        return _malformed(f"{field_name} must be an integer")
    if value < 0:
        return _malformed(f"{field_name} must be >= 0")
    return None


def _validate_id(value, field_name: str) -> Rejected | None:
    """Syntax-only (§25): provenance is enforced by the authoritative boundary
    returning R_NOT_FOUND — never guessed, truncated, or fuzzy-matched."""
    if value is None:
        return _malformed(f"{field_name} is required")
    if not isinstance(value, str) or not value:
        return _malformed(f"{field_name} must be a non-empty string")
    if len(value) > 128:
        return _malformed(f"{field_name} exceeds identifier bound")
    return None


# ── The four frozen propose_* functions (Interface Contract §2) ──────────────

def propose_goal(session_id: Id, statement: str, completion_policy: CompletionPolicy) -> Result:
    """§9 — validate, then authorize the session (§13.3), then Work Service
    commits the Goal. session_origin records the session id as provenance
    only — it is never read by any authorization check (§9.3/§13.3)."""
    log.info("proposal_received op=propose_goal")
    err = (
        _validate_id(session_id, "session_id")
        or _validate_statement(statement, "statement", MAX_STATEMENT_LEN)
        or _validate_completion_policy(completion_policy)
    )
    if err is not None:
        return err
    s = _sessions.resolve_session(_store(), session_id)
    if s is None or s.state.value != "LIVE" or not s.cognition_authorized:
        log.info("proposal_rejected op=propose_goal reason=unauthorized")
        return Rejected(R_UNAUTHORIZED,
                        f"session {session_id} is not a live Cognition-authorized session (§13.3)",
                        None)
    _current_context.set(AuthenticatedCognitionContext(
        session_id=s.id, principal_id=_sessions.OWNER_PRINCIPAL_ID,
        session_state=s.state, cognition_authorized=s.cognition_authorized,
    ))
    # Work Service owns creation; goal-creation has no CAS precondition (a
    # Goal is always fresh) so it returns the committed Goal directly.
    goal = _work.create_goal(_store(), completion_policy, session_origin=session_id)
    log.info("proposal_accepted op=propose_goal goal=%s", goal.id)
    return Ok(goal)


def propose_task(goal_id: Id, expected_goal_revision: int, statement: str,
                 completion_policy: CompletionPolicy) -> Result:
    """§10 — Goal CAS + Task creation by Work Service. Session legitimacy is
    established first (§13.3), distinctly from object resolution."""
    log.info("proposal_received op=propose_task")
    ctx = _require_context()
    if isinstance(ctx, Rejected):
        return ctx
    err = (
        _validate_id(goal_id, "goal_id")
        or _validate_expected_revision(expected_goal_revision, "expected_goal_revision")
        or _validate_statement(statement, "statement", MAX_STATEMENT_LEN)
        or _validate_completion_policy(completion_policy)
    )
    if err is not None:
        return err
    result = _work.create_task(
        _store(), goal_id, completion_policy,
        expected_goal_revision=expected_goal_revision,
    )
    if isinstance(result, Rejected):
        log.info("proposal_rejected op=propose_task reason=%s", result.reason)
        return result
    log.info("proposal_accepted op=propose_task task=%s", result.value.id)
    return result


def propose_plan(task_id: Id, expected_task_revision: int,
                 steps: list[StepProposal]) -> Result:
    """§11 — Work Service atomically commits Plan+Steps+requirement bindings.
    Adapter performs fast-fail validation of the same invariants; Work Service
    re-validates them authoritatively (§6)."""
    log.info("proposal_received op=propose_plan")
    ctx = _require_context()
    if isinstance(ctx, Rejected):
        return ctx
    err = (
        _validate_id(task_id, "task_id")
        or _validate_expected_revision(expected_task_revision, "expected_task_revision")
    )
    if err is not None:
        return err
    if steps is None:
        return _malformed("steps is required")
    if not isinstance(steps, list) or not steps:
        return _malformed("steps must be a non-empty list")
    if len(steps) > MAX_PLAN_STEPS:
        return _malformed(f"steps has {len(steps)} entries, bound is {MAX_PLAN_STEPS}")

    n = len(steps)
    specs: list[dict] = []
    for i, sp in enumerate(steps):
        if not isinstance(sp, StepProposal):
            return _malformed(f"steps[{i}] must be a StepProposal")
        e = _validate_statement(sp.description, f"steps[{i}].description",
                                MAX_STEP_DESCRIPTION_LEN)
        if e is not None:
            return e
        if not isinstance(sp.required, bool):
            return _malformed(f"steps[{i}].required must be a bool")
        cap_err = (
            _malformed(f"steps[{i}].execution_capability is required")
            if not isinstance(sp.execution_capability, str) or not sp.execution_capability
            else None
        )
        if cap_err is not None:
            return cap_err
        if _caps.get(sp.execution_capability) is None:
            return _malformed(f"steps[{i}]: unknown capability {sp.execution_capability!r}")
        if sp.depends_on_index is None:
            return _malformed(f"steps[{i}].depends_on_index is required")
        if not isinstance(sp.depends_on_index, list):
            return _malformed(f"steps[{i}].depends_on_index must be a list")
        if len(sp.depends_on_index) > MAX_DEPENDENCIES_PER_STEP:
            return _malformed(
                f"steps[{i}].depends_on_index exceeds bound {MAX_DEPENDENCIES_PER_STEP}"
            )
        if not isinstance(sp.verification_requirements, list):
            return _malformed(f"steps[{i}].verification_requirements must be a list")
        if len(sp.verification_requirements) > MAX_VERIFICATION_REQUIREMENTS:
            return _malformed(
                f"steps[{i}].verification_requirements exceeds bound {MAX_VERIFICATION_REQUIREMENTS}"
            )
        reqs: list[dict] = []
        for j, req in enumerate(sp.verification_requirements):
            if not isinstance(req, VerificationRequirement):
                return _malformed(f"steps[{i}].verification_requirements[{j}] must be a VerificationRequirement")
            if not isinstance(req.method_name, str) or not req.method_name:
                return _malformed(f"steps[{i}].verification_requirements[{j}].method_name is required")
            if len(req.method_name) > MAX_VERIFICATION_METHOD_LEN:
                return _malformed(f"steps[{i}].verification_requirements[{j}].method_name exceeds bound")
            from v5.verification import get_method
            if get_method(req.method_name) is None:
                return Rejected("UNKNOWN_VERIFICATION_METHOD",
                                f"steps[{i}]: unknown verification method {req.method_name!r}", None)
            if req.applies_to_capability != sp.execution_capability:
                return Rejected("VERIFICATION_CAPABILITY_MISMATCH",
                                f"steps[{i}]: applies_to_capability {req.applies_to_capability!r} "
                                f"!= execution_capability {sp.execution_capability!r} (§11.5/§29)", None)
            reqs.append({"method_name": req.method_name,
                         "applies_to_capability": req.applies_to_capability})
        specs.append({
            "description": sp.description,
            "required": sp.required,
            "depends_on_index": list(sp.depends_on_index),
            "execution_capability": sp.execution_capability,
            "verification_requirements": reqs,
        })

    result = _work.create_plan_with_steps(
        _store(), task_id, specs, expected_task_revision=expected_task_revision,
    )
    if isinstance(result, Rejected):
        log.info("proposal_rejected op=propose_plan reason=%s", result.reason)
        return result
    log.info("proposal_accepted op=propose_plan plan=%s steps=%d",
             result.value["plan"].id, len(result.value["steps"]))
    return result


def propose_action(step_id: Id, expected_step_revision: int, capability: str,
                   arguments: dict) -> Result:
    """§12 — validate + persist Action(PENDING) via the execution authority.
    NEVER executes the capability (§12.3). Execution is a separate system.

    Check order matters: capability CORRESPONDENCE to the Step's declared
    execution_capability is the authoritative, canonical-data check (§12.2);
    it runs before capability registration/argument-shape checks so a
    mismatched capability is unambiguously a correspondence violation even
    when the name happens to be unregistered (§46's delete_file case), while
    an unknown capability on a step without a declared one is the §44
    unknown-capability failure."""
    log.info("proposal_received op=propose_action")
    ctx = _require_context()
    if isinstance(ctx, Rejected):
        return ctx
    err = (
        _validate_id(step_id, "step_id")
        or _validate_expected_revision(expected_step_revision, "expected_step_revision")
    )
    if err is not None:
        return err
    step = _work.load_step(_store().read(), step_id)
    from v5.models import R_NOT_FOUND
    if step is None:
        return Rejected(R_NOT_FOUND, f"step {step_id}", None)
    if step.execution_capability:
        if not isinstance(capability, str) or capability == "":
            return _malformed("capability is required and must be a string")
        if capability != step.execution_capability:
            return Rejected("CAPABILITY_MISMATCH",
                            f"action capability {capability!r} != step's declared "
                            f"execution_capability {step.execution_capability!r} (§12.2/§46) — "
                            "no Action may bypass the Step's declared capability", step)
    cap_err = _validate_capability_and_args(capability, arguments)
    if cap_err is not None:
        return cap_err
    from v5 import execution as _execution
    spec = _caps.get(capability)
    result = _execution.create_action(
        _store(), step_id, capability, arguments,
        spec.idempotency_class,
        expected_step_revision=expected_step_revision,
    )
    if isinstance(result, Rejected):
        log.info("proposal_rejected op=propose_action reason=%s", result.reason)
        return result
    log.info("proposal_accepted op=propose_action action=%s", result.value.id)
    return result


# ── Model-output adapter (§18–§26) ───────────────────────────────────────────
# Fail-closed: anything that isn't a recognized structured proposal is
# "no proposal made", never an interpreted guess. Unknown fields, missing
# fields, wrong types are all rejected with the specific violation named.
#
# ParsedOperation is the adapter's only product: typed data + the operation
# name. It carries NO session_id/principal_id — those can only ever come from
# trusted authentication state, and any such key in model output is an
# unknown field, which is rejected.

@dataclass
class ParsedOperation:
    operation: str                      # propose_goal / propose_task / propose_plan / propose_action
    payload: dict                       # validated keyword arguments


_OPERATION_FIELDS: dict[str, dict[str, tuple]] = {
    "propose_goal": {
        "required": ("statement", "completion_policy"),
        "optional": (),
    },
    "propose_task": {
        "required": ("goal_id", "expected_goal_revision", "statement", "completion_policy"),
        "optional": (),
    },
    "propose_plan": {
        "required": ("task_id", "expected_task_revision", "steps"),
        "optional": (),
    },
    "propose_action": {
        "required": ("step_id", "expected_step_revision", "capability", "arguments"),
        "optional": (),
    },
}

_STEP_REQUIRED = ("description", "required", "depends_on_index", "execution_capability")
_STEP_OPTIONAL = ("verification_requirements",)
_VR_REQUIRED = ("method_name", "applies_to_capability")
_VR_OPTIONAL: tuple = ()


def _coerce_completion_policy(raw, field_name: str) -> CompletionPolicy | Rejected:
    if not isinstance(raw, dict):
        return _malformed(f"{field_name} must be an object")
    unknown = set(raw) - {"rule", "n", "criterion_ref"}
    if unknown:
        return _malformed(f"{field_name}: unknown field(s) {sorted(unknown)}")
    if "rule" not in raw:
        return _malformed(f"{field_name}.rule is required")
    rule = raw["rule"]
    if rule not in ("ALL_REQUIRED", "ANY_REQUIRED", "N_OF_M", "CRITERION"):
        return _malformed(f"{field_name}.rule {rule!r} is invalid")
    n = raw.get("n")
    if rule == "N_OF_M":
        if n is None or not isinstance(n, int) or isinstance(n, bool) or n < 1:
            return _malformed(f"{field_name}.n must be a positive int for N_OF_M")
    elif n is not None:
        return _malformed(f"{field_name}.n must be null unless rule is N_OF_M")
    criterion_ref = raw.get("criterion_ref")
    if rule == "CRITERION" and (not isinstance(criterion_ref, str) or not criterion_ref):
        return _malformed(f"{field_name}.criterion_ref is required for CRITERION")
    return CompletionPolicy(rule=rule, n=n, criterion_ref=criterion_ref)


def _strict_fields(raw: dict, required: tuple, optional: tuple, where: str) -> Rejected | None:
    unknown = sorted(set(raw) - set(required) - set(optional))
    if unknown:
        return _malformed(f"{where}: unknown field(s) {', '.join(unknown)}")
    for f in required:
        if f not in raw:
            return _malformed(f"{where}: {f} is required")
    return None


def parse_proposals(raw_output: str) -> Result:
    """Parse raw model output into a list of validated ParsedOperations.

    Handles the adapter-table failure modes (Interface Contract §5):
      * malformed JSON          -> rejected parse error, propose_* never runs
      * narrated tool calls     -> rejected as "no proposal" (prose is not a proposal)
      * unknown fields          -> rejected (fail-closed)
      * missing fields          -> rejected, naming the field
      * duplicate proposals in one emission -> content-hash dedupe
      * identical proposals across emissions -> NOT deduped (each call is its
        own emission; cross-turn idempotency belongs to Work/capability, §26)
    """
    log.info("adapter_parse_started")
    if not isinstance(raw_output, str):
        return _malformed("model output must be a string")
    try:
        data = json.loads(raw_output)
    except json.JSONDecodeError as e:
        log.info("proposal_rejected reason=malformed_json detail=%s", e)
        return Rejected(R_MALFORMED_PROPOSAL,
                        f"unparseable model output (not a structured proposal): {e}", None)
    if isinstance(data, dict) and "proposals" not in data:
        data = [data]
    elif isinstance(data, dict) and isinstance(data.get("proposals"), list):
        data = data["proposals"]
    if not isinstance(data, list) or not data:
        if isinstance(data, dict) and "proposals" in data:
            return _malformed("proposals must be a non-empty list")
        return Rejected(R_MALFORMED_PROPOSAL,
                        "no structured proposal found — narrated tool calls are "
                        "not proposals (§21)", None)

    parsed: list[ParsedOperation] = []
    seen_hashes: set[str] = set()
    for idx, item in enumerate(data):
        where = f"proposals[{idx}]"
        if not isinstance(item, dict):
            return _malformed(f"{where} must be an object")
        if "operation" not in item:
            return _malformed(f"{where}: operation is required")
        op = item["operation"]
        if op not in _OPERATION_FIELDS:
            return _malformed(f"{where}: unknown operation {op!r}")
        fields = _OPERATION_FIELDS[op]
        e = _strict_fields(item, ("operation", *fields["required"]), fields["optional"], where)
        if e is not None:
            return e
        payload: dict = {}
        for f in (*fields["required"], *fields["optional"]):
            if f == "operation":
                continue
            if f not in item:
                continue
            value = item[f]
            if f == "completion_policy":
                cp = _coerce_completion_policy(value, f"{where}.completion_policy")
                if isinstance(cp, Rejected):
                    return cp
                payload[f] = cp
            elif f == "steps":
                steps = _parse_steps(value, where)
                if isinstance(steps, Rejected):
                    return steps
                payload[f] = steps
            else:
                payload[f] = value
        canonical = json.dumps({"operation": op,
                                "payload": {k: _jsonable(v) for k, v in payload.items()}},
                               sort_keys=True)
        h = hash(canonical)
        if h in seen_hashes:
            # §26: same-emission duplicate suppresses to a no-op
            log.info("proposal_deduplicated op=%s index=%d", op, idx)
            continue
        seen_hashes.add(h)
        parsed.append(ParsedOperation(operation=op, payload=payload))
    if not parsed:
        return Rejected(R_MALFORMED_PROPOSAL,
                        "emission contained only duplicate proposals", None)
    log.info("adapter_parse_finished proposals=%d", len(parsed))
    return Ok(parsed)


def _jsonable(v):
    if isinstance(v, CompletionPolicy):
        return {"rule": v.rule, "n": v.n, "criterion_ref": v.criterion_ref}
    if isinstance(v, StepProposal):
        return {"description": v.description, "required": v.required,
                "depends_on_index": v.depends_on_index,
                "execution_capability": v.execution_capability,
                "verification_requirements": [_jsonable(r) for r in v.verification_requirements]}
    if isinstance(v, VerificationRequirement):
        return {"method_name": v.method_name, "applies_to_capability": v.applies_to_capability}
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return v


def _parse_steps(value, where: str):
    if not isinstance(value, list) or not value:
        return _malformed(f"{where}.steps must be a non-empty list")
    if len(value) > MAX_PLAN_STEPS:
        return _malformed(f"{where}.steps exceeds bound {MAX_PLAN_STEPS}")
    out: list[StepProposal] = []
    for i, s in enumerate(value):
        sw = f"{where}.steps[{i}]"
        if not isinstance(s, dict):
            return _malformed(f"{sw} must be an object")
        e = _strict_fields(s, _STEP_REQUIRED, _STEP_OPTIONAL, sw)
        if e is not None:
            return e
        vrs = s.get("verification_requirements", [])
        if not isinstance(vrs, list):
            return _malformed(f"{sw}.verification_requirements must be a list")
        parsed_vrs: list[VerificationRequirement] = []
        for j, vr in enumerate(vrs):
            vw = f"{sw}.verification_requirements[{j}]"
            if not isinstance(vr, dict):
                return _malformed(f"{vw} must be an object")
            e = _strict_fields(vr, _VR_REQUIRED, _VR_OPTIONAL, vw)
            if e is not None:
                return e
            parsed_vrs.append(VerificationRequirement(
                method_name=vr["method_name"],
                applies_to_capability=vr["applies_to_capability"],
            ))
        out.append(StepProposal(
            description=s["description"],
            required=s["required"],
            depends_on_index=s["depends_on_index"],
            execution_capability=s["execution_capability"],
            verification_requirements=parsed_vrs,
        ))
    return out


def dispatch(operation: ParsedOperation) -> Result:
    """Run one adapter-parsed operation through the corresponding frozen
    propose_* function. The adapter never calls Work Service or capabilities
    itself (§3)."""
    fn = {
        "propose_goal": propose_goal,
        "propose_task": propose_task,
        "propose_plan": propose_plan,
        "propose_action": propose_action,
    }[operation.operation]
    if operation.operation == "propose_goal":
        # session_id comes from the trusted threaded context, never the payload
        ctx = _require_context()
        if isinstance(ctx, Rejected):
            return ctx
        return fn(ctx.session_id, operation.payload["statement"],
                  operation.payload["completion_policy"])
    return fn(**operation.payload)
