"""Live LLM Cognition Loop v1 — the orchestration glue for the frozen slice.

This module is NOT a new authority. It is a host-harness loop that drives the
frozen Cognition membrane end to end with one deterministic model:

    one natural-language instruction (milestone-fixed shape)
        ↓
    provider API with native function calling (temperature 0)
        ↓
    tool schemas GENERATED FROM v5.cognition's frozen _OPERATION_FIELDS /
    _STEP_* / _VR_* tables — the frozen adapter schema is the single source
    of truth and is never hand-mirrored (a drift-guard test proves equality)
        ↓
    stage-gated tool exposure (see STAGE_TOOLS): exactly one tool per stage —
    propose_goal until a Goal exists, then propose_task, propose_plan,
    propose_action in order. The frozen membrane independently rejects any
    out-of-order call anyway; the gating exists to keep the run linear.
        ↓
    ONLY the arguments of genuine provider tool-call slots are serialized
    ({operation: name, **arguments}) into a JSON string and handed to
    cognition.parse_proposals → cognition.dispatch. The model's plain-text
    response is never JSON-scraped, regex-extracted, or salvaged into a
    proposal (Contract §18–§21 — no prose extraction, ever). A turn with no
    function call is discarded as "no proposal made", the model is re-prompted
    once, and if it narrates again the loop HALTS CLEANLY (chosen behavior
    over infinite re-prompting).
        ↓
    Ok/Rejected results are fed back to the model as tool responses so its
    next call carries real IDs/revisions only (never invented ones; §25)
        ↓
    after propose_action commits Action(PENDING) the HOST (this loop) drives
    the existing execution boundary exactly as test_full_slice_end_to_end:
    execution.begin_executing → real file_write capability → mark_observed,
    then the existing independent verifier (verification.run_method_for_action
    → run_verification for_action_id gate) and the Work Service completion
    path (complete_step → complete_object). The loop constructs no canonical
    rows itself and calls no Work/execution function except through
    cognition.dispatch and this existing boundary.

§26: duplicate suppression is parse_proposals' same-emission dedup — nothing
here adds cross-turn/global dedup (repeating the same logical request in a
later turn must remain a new, legitimate action).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Any

from v5 import capabilities as _caps
from v5 import cognition, evidence, execution, sessions, verification, work
from v5.cognition import _OPERATION_FIELDS, _STEP_OPTIONAL, _STEP_REQUIRED, _VR_OPTIONAL, _VR_REQUIRED
from v5.enums import StepStatus, TaskStatus
from v5.models import Ok, Rejected, Result
from v5.safety import make_confirmation_gate
from v5.store import Store

# The milestone's single fixed instruction and its parameters (§40 vertical
# slice). Paths are caller-supplied; the model sees only the instruction, the
# target path, and the content — it never sees session/principal identifiers.
MILESTONE_INSTRUCTION_TEMPLATE = "Create {path} containing {content}."


# ── field-type table (JSON Schema shapes for each frozen pipeline field) ────
# The field NAMES are never re-declared: schemas are composed from the frozen
# _OPERATION_FIELDS / _STEP_REQUIRED / _STEP_OPTIONAL / _VR_* tables at import
# time. The drift-guard test (tests/test_live_loop.py) fails loudly if the
# frozen table introduces a field this map doesn't know.

_JSON_STRING = {"type": "string"}
_JSON_INTEGER = {"type": "integer"}
_JSON_BOOL = {"type": "boolean"}

_FIELD_JSON_SCHEMA: dict[str, dict] = {
    "statement": _JSON_STRING,
    "completion_policy": {
        "type": "object",
        "properties": {
            "rule": {"type": "string",
                     "enum": ["ALL_REQUIRED", "ANY_REQUIRED", "N_OF_M", "CRITERION"]},
            "n": _JSON_INTEGER,
            "criterion_ref": _JSON_STRING,
        },
        "required": ["rule"],
    },
    "goal_id": _JSON_STRING,
    "expected_goal_revision": _JSON_INTEGER,
    "task_id": _JSON_STRING,
    "expected_task_revision": _JSON_INTEGER,
    "step_id": _JSON_STRING,
    "expected_step_revision": _JSON_INTEGER,
    "capability": _JSON_STRING,
    "arguments": {
        "type": "object",
        "properties": {
            # milestone-scoped: the only capability is file_write (§40/§54)
            # with exactly these two declared keys — the model emits values,
            # never invents keys (the adapter + create_action's validate_args
            # still reject anything else; this is not a second authority).
            "path": {"type": "string", "description": "absolute file path to write"},
            "content": {"type": "string", "description": "content to write"},
        },
        "required": ["path", "content"],
    },
    "steps": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "description": _JSON_STRING,
                "required": _JSON_BOOL,
                "depends_on_index": {"type": "array", "items": _JSON_INTEGER},
                "execution_capability": _JSON_STRING,
                "verification_requirements": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "method_name": _JSON_STRING,
                            "applies_to_capability": _JSON_STRING,
                        },
                        "required": list(_VR_REQUIRED),
                    },
                },
            },
            "required": list(_STEP_REQUIRED),
        },
    },
}

_OPTIONAL_STEP_KEYS = set(_STEP_OPTIONAL)
_OPTIONAL_VR_KEYS = set(_VR_OPTIONAL)


def tool_schemas() -> dict[str, dict]:
    """Generate each operation's tool schema FROM the frozen adapter table —
    never a hand-mirrored copy. Raises KeyError if a new frozen field lacks a
    JSON-schema mapping (that's a deliberate fail-loud drift guard, not an
    error to paper over)."""
    out: dict[str, dict] = {}
    for op, fields in _OPERATION_FIELDS.items():
        props = {}
        for name in (*fields["required"], *fields["optional"]):
            props[name] = _FIELD_JSON_SCHEMA[name]  # KeyError here IS the guard
        out[op] = {
            "name": op,
            "description": f"Cognition proposal: {op}",
            "parameters": {
                "type": "object",
                "properties": props,
                "required": list(fields["required"]),
            },
        }
    return out


# ── stage gating ─────────────────────────────────────────────────────────────

# One tool per stage. The frozen membrane's own preconditions reject out-of-
# order calls regardless; the gating is for run reliability (§2.3).
STAGE_TOOLS = {
    "goal": ("propose_goal",),
    "task": ("propose_task",),
    "plan": ("propose_plan",),
    "action": ("propose_action",),
}
_STAGE_ORDER = ["goal", "task", "plan", "action"]


# ── transcript records (cheap structured logging, never authoritative) ───────

@dataclass
class TurnEvent:
    turn: int
    stage: str
    kind: str          # tool_call | narration_discarded | result | host_step | halted
    detail: dict


# ── provider protocol (structural duck-typing, no new abstraction layer) ────
# A provider client is anything with:
#   .model_name: str
#   .call(contents, tools, allowed_names) -> response
# where response has .candidates[0].content.parts and each part may carry
# .function_call (name/args) or .text. Tests use a scripted fake; the live
# proof uses GeminiLiveClient below with native function calling at
# temperature 0 (§1d — one provider, no routing, no fallback, no abstraction).


class GeminiLiveClient:
    """Single deterministic provider: Gemini 2.5 Flash, native
    tool/function-calling, temperature 0. Read the API key from the
    environment (GOOGLE_GENAI_API_KEY); the key is never logged/printed."""

    def __init__(self, dotenv_path: str | None = None):
        from google import genai  # lazy import: offline tests never need it
        if dotenv_path is not None:
            from dotenv import load_dotenv
            load_dotenv(os.path.expanduser(dotenv_path))
        key = os.environ.get("GOOGLE_GENAI_API_KEY")
        if not key:
            raise RuntimeError("GOOGLE_GENAI_API_KEY is required (env var; never in code)")
        self._client = genai.Client(api_key=key)
        self.model_name = "gemini-2.5-flash"

    def call(self, contents, tools, allowed_names):
        """contents: provider-neutral message log produced by the loop —
        a list of {"role": ..., "parts": [{"text"|"function_call"|"function_response": ...}]}.
        This boundary converts it into the SDK's typed objects. Plain-text
        parts become text; function-call parts become typed function calls;
        results feed back as typed function responses. Nothing in the other
        direction ("read text; salvage a proposal out of it") exists."""
        from google.genai import types
        conv = []
        for c in contents:
            parts = []
            for p in c["parts"]:
                if "text" in p:
                    parts.append(types.Part.from_text(text=p["text"]))
                elif "function_call" in p:
                    fc = p["function_call"]
                    parts.append(types.Part.from_function_call(name=fc["name"], args=fc["args"]))
                elif "function_response" in p:
                    fr = p["function_response"]
                    parts.append(types.Part.from_function_response(
                        name=fr["name"], response=fr["response"]))
            conv.append(types.Content(role=c.get("role", "user"), parts=parts))
        decls = [types.FunctionDeclaration(
            name=t["name"], description=t["description"], parameters=t["parameters"],
        ) for t in tools if t["name"] in allowed_names]
        return self._client.models.generate_content(
            model=self.model_name,
            contents=conv,
            config=types.GenerateContentConfig(
                temperature=0.0,
                tools=[types.Tool(function_declarations=decls)] if decls else None,
                tool_config=types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(
                        mode="ANY" if decls else "NONE",
                        allowed_function_names=list(allowed_names) if decls else None,
                    )
                ),
            ),
        )


class NvidiaNimLiveClient:
    """Single deterministic provider (§1d): NVIDIA NIM. Model choice follows
    the project's own V4 chain — the routing/fast tier
    `nvidia/nemotron-3-super-120b-a12b` (the frontier ultra-550b slot timed
    out repeatedly in this environment at 180 s+; the nano slot was removed
    from NIM). Native OpenAI-style function calling at temperature 0.
    Credential read only from NVIDIA_NEMOTRON_API_KEY; never logged/printed."""

    ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"
    MODEL = "nvidia/nemotron-3-super-120b-a12b"

    def __init__(self, dotenv_path: str | None = None):
        import httpx  # lazy import: offline tests never need it
        if dotenv_path is not None:
            from dotenv import load_dotenv
            load_dotenv(os.path.expanduser(dotenv_path))
        key = os.environ.get("NVIDIA_NEMOTRON_API_KEY")
        if not key:
            raise RuntimeError("NVIDIA_NEMOTRON_API_KEY is required (env var; never in code)")
        self._key = key
        self._http = httpx.Client(headers={"Authorization": f"Bearer {key}"}, timeout=240.0)
        self.model_name = self.MODEL

    def call(self, contents, tools, allowed_names):
        """Same provider-neutral contract as the Gemini client's .call:
        converts the loop's plain message log into OpenAI-style chat messages,
        and the response's tool-call parts back into the neutral function_call
        shape. Plain text never becomes a proposal — the prose→JSON direction
        that the frozen contract forbids has no code path here."""
        decls = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"],
            "parameters": t["parameters"]}} for t in tools if t["name"] in allowed_names]
        messages: list[dict] = []
        # neutral log role -> provider role (Gemini uses "model"; OpenAI-style
        # chat endpoints want "assistant")
        role_map = {"model": "assistant", "user": "user", "system": "system"}
        for c in contents:
            role = role_map.get(c.get("role", "user"), "user")
            parts = c["parts"]
            texts = [p["text"] for p in parts if "text" in p]
            tcs = [p["function_call"] for p in parts if "function_call" in p]
            frs = [p["function_response"] for p in parts if "function_response" in p]
            if role == "user" and frs:
                for fr in frs:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": fr.get("_call_id", f"call_{fr['name']}"),
                        "content": json.dumps(fr["response"], default=str)})
            elif tcs:
                messages.append({
                    "role": "assistant",
                    "content": texts[0] if texts else None,
                    "tool_calls": [{"id": fc.get("_call_id", f"call_{fc['name']}"),
                                    "type": "function",
                                    "function": {"name": fc["name"],
                                                 "arguments": json.dumps(fc["args"])}}
                                   for fc in tcs],
                })
            else:
                messages.append({"role": role, "content": texts[0] if texts else ""})
        payload = {
            "model": self.MODEL,
            "messages": messages,
            "temperature": 0.0,
            # the rationale slot can otherwise produce unbounded preambles;
            # the milestone needs only the tool call, and free-tier latency
            # balloons without this cap
            "max_tokens": 4096,
        }
        if decls:
            payload["tools"] = decls
            payload["tool_choice"] = {"type": "function",
                                      "function": {"name": allowed_names[0]}}
        resp = self._http.post(self.ENDPOINT, json=payload)
        if resp.status_code != 200:
            raise RuntimeError(
                f"NIM endpoint returned {resp.status_code}: {resp.text[:400]}"
            )
        data = resp.json()
        return _OpenAIResponse(data)


class _OpenAIResponse:
    """Minimal duck-typed adapter over an OpenAI-style chat completion so the
    loop's neutral extraction path sees .candidates[].content.parts with
    function_call parts — regardless of which provider emitted it."""

    def __init__(self, data: dict):
        parts = []
        for choice in data.get("choices", []):
            msg = choice.get("message", {})
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                args = fn.get("arguments", "{}")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"__unparseable_args__": args}
                parts.append(_SimplePart(function_call=_SimpleFunctionCall(fn.get("name"), args)))
            if msg.get("content"):
                parts.append(_SimplePart(text=msg["content"]))
        self.candidates = [_OpenAICandidate(_OpenAIContent(parts))]


@dataclass
class _OpenAICandidate:
    content: "_OpenAIContent"


@dataclass
class _OpenAIContent:
    parts: list


@dataclass
class _SimplePart:
    function_call: Any = None
    text: str | None = None


@dataclass
class _SimpleFunctionCall:
    name: str
    args: dict




# ── the loop ─────────────────────────────────────────────────────────────────

def _result_payload(result: Result) -> dict:
    """Serializable view of an Ok/Rejected for the model's next turn —
    enough real IDs/revisions to chain forward without inventing any."""
    def _s(v):
        if isinstance(v, dict):
            return {k: _s(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [_s(x) for x in v]
        if hasattr(v, "__dataclass_fields__"):
            return _s(asdict(v))
        if isinstance(v, set):
            return sorted(v)
        if hasattr(v, "value") and hasattr(v, "name"):  # Enum
            return v.value
        return v
    if isinstance(result, Ok):
        return {"status": "ok", "result": _s(result.value)}
    return {"status": "rejected", "reason": result.reason, "detail": result.detail,
            "current": _s(result.current) if result.current is not None else None}


def _as_dict_args(args) -> dict:
    """Convert provider-native argument mapping to a plain dict of JSON values.
    This is the ONLY place provider output becomes adapter input — text parts
    never enter here."""
    return json.loads(json.dumps(dict(args)))


def run_live_slice(store: Store, session_id: str, instruction: str,
                   target_path: str, content: str,
                   provider, max_turns: int = 12,
                   transcript_path: str | None = None) -> dict:
    """Drive the frozen vertical slice end to end. Returns the full transcript
    plus final canonical state. Halts with ok=False on any terminal failure —
    never papers over a rejection, never salvages prose."""
    events: list[TurnEvent] = []

    def ev(turn, stage, kind, detail):
        events.append(TurnEvent(turn, stage, kind, detail))

    # trusted host wiring — session identity comes only from here, never the
    # model (§13.2/§1c)
    cognition.bind_store(store)
    auth = cognition.authenticate_session(session_id)
    if isinstance(auth, Rejected):
        return {"ok": False, "reason": f"session auth failed: {auth.detail}", "events": []}

    schemas = tool_schemas()
    state: dict[str, Any] = {"goal": None, "task": None, "plan": None,
                             "step": None, "action": None, "verifications": []}

    contents: list[dict] = [{
        "role": "user",
        "parts": [{"text": (
            "You are driving a strict state machine via function calls. Rules: "
            "emit exactly ONE function call per turn using the tool that is offered; never emit "
            "free text. Use ONLY ids and revision numbers returned by previous tool responses — "
            "never invent, guess, truncate, or compute them. Payload rules: completion_policy is "
            "{\"rule\": \"ALL_REQUIRED\"}; the plan has exactly one step; the step's "
            "execution_capability and every verification requirement's applies_to_capability are "
            "\"file_write\"; the verification method is \"verify_file_write\"; "
            f"the action arguments are exactly {{\"path\": {json.dumps(target_path)}, "
            f"\"content\": {json.dumps(content)}}}. Instruction: {instruction}"
        )}],
    }]

    stage = "goal"
    reprompts = 0
    rejections = 0
    halted_reason: str | None = None
    turn = 0

    while turn < max_turns and state["action"] is None:
        turn += 1
        resp = provider.call(contents,
                             [schemas[n] for n in STAGE_TOOLS[stage]],
                             STAGE_TOOLS[stage])
        parts = []
        for c in getattr(resp, "candidates", []) or []:
            for part in getattr(getattr(c, "content", None), "parts", []) or []:
                parts.append(part)
        calls = [p.function_call for p in parts if getattr(p, "function_call", None)]
        texts = [p.text for p in parts if getattr(p, "text", None)]

        if not calls:
            # §21: narration is not a proposal. Discard outright, re-prompt
            # once, then halt cleanly. The text is never JSON-scraped.
            ev(turn, stage, "narration_discarded", {"text": "".join(texts)[:500]})
            reprompts += 1
            if reprompts > 1:
                halted_reason = "model narrated twice without a tool call — halted"
                break
            contents.append({"role": "model", "parts": [{"text": "".join(texts) or "(no output)"}]})
            contents.append({"role": "user", "parts": [{"text":
                "No function call was received. Narration is not a proposal. "
                "Emit exactly one tool call now."}]})
            continue

        # §0/§1a: ONLY the genuine tool-call arguments reach the adapter —
        # serialized as {operation: name, **args}; the prose is dropped above.
        raw = json.dumps({"proposals": [
            {"operation": fc.name, **_as_dict_args(fc.args)} for fc in calls
        ]})
        ev(turn, stage, "tool_call", {"calls": [{"operation": fc.name,
                                                 "args": _as_dict_args(fc.args)} for fc in calls]})

        parcels = []
        for fc in calls:
            parcels.append({"name": fc.name, "args": _as_dict_args(fc.args),
                            "_call_id": f"call_{fc.name}"})
        contents.append({"role": "model", "parts": [
            {"function_call": p} for p in parcels
        ]})

        parsed = cognition.parse_proposals(raw)
        if isinstance(parsed, Rejected):
            rejections += 1
            payload = _result_payload(parsed)
            ev(turn, stage, "result", {"operation": None, **payload})
            contents.append({"role": "user", "parts": [
                {"function_response": {"name": calls[0].name, "_call_id": f"call_{calls[0].name}",
                                       "response": payload}}
            ]})
            if rejections > 4:
                halted_reason = "too many adapter rejections"
                break
            continue

        # Stage-gating enforcement: only the exposed tool may dispatch this
        # turn. The frozen membrane independently rejects genuinely out-of-
        # order calls, but propose_goal is always legal (a fresh goal) — so the
        # loop must refuse to dispatch anything but the single exposed tool,
        # else a confused model could mint unlimited goals. Off-stage calls are
        # discarded like narration: fed back as a rejection and re-prompted.
        on_stage = [c for c in calls if c.name in STAGE_TOOLS[stage]]
        off_stage = [c for c in calls if c.name not in STAGE_TOOLS[stage]]
        if off_stage:
            ev(turn, stage, "narration_discarded",
               {"text": f"off-stage tool call discarded: {[c.name for c in off_stage]} "
                        f"(stage {stage} exposes only {list(STAGE_TOOLS[stage])})"})
            if not on_stage:
                reprompts += 1
                contents.append({"role": "user", "parts": [{"text":
                    f"Wrong tool for this stage. Emit exactly one call to "
                    f"{STAGE_TOOLS[stage][0]} now."}]})
                if reprompts > 1:
                    halted_reason = "model kept calling off-stage tools — halted"
                    break
                continue
            calls = on_stage
            raw = json.dumps({"proposals": [
                {"operation": fc.name, **_as_dict_args(fc.args)} for fc in calls
            ]})
            parsed = cognition.parse_proposals(raw)
            if isinstance(parsed, Rejected):
                rejections += 1
                payload = _result_payload(parsed)
                ev(turn, stage, "result", {"operation": None, **payload})
                continue

        turn_failed = False
        turn_payloads: list[tuple[str, dict]] = []
        for op in parsed.value:
            res = cognition.dispatch(op)
            payload = _result_payload(res)
            # uniform event detail: {"operation": <name|None>, "status": ...,
            # "reason"/"detail"/"current" for rejections, "result" for ok}
            ev(turn, stage, "result", {"operation": op.operation, **payload})
            turn_payloads.append((op.operation, payload))
            if isinstance(res, Rejected):
                turn_failed = True
                continue

            # advance the chain using the real returned objects
            if op.operation == "propose_goal":
                state["goal"] = res.value
                stage = "task"
            elif op.operation == "propose_task":
                state["task"] = res.value
                # host lifecycle glue (same transitions the e2e test uses)
                state["task"] = work.transition_object(
                    store, state["task"].id, TaskStatus.ACTIVE, state["task"].revision).value
                ev(turn, stage, "host_step", {"op": "activate_task",
                                              "task_id": state["task"].id,
                                              "revision": state["task"].revision})
                stage = "plan"
            elif op.operation == "propose_plan":
                state["plan"] = res.value["plan"]
                state["step"] = res.value["steps"][0]
                state["verifications"] = res.value["bound_verifications"]
                act = work.activate_plan(
                    store, state["task"].id, state["plan"].id,
                    work.load_task(store.read(), state["task"].id).revision)
                if isinstance(act, Rejected):
                    turn_failed = True
                    ev(turn, stage, "result", {"operation": "activate_plan",
                                               **_result_payload(act)})
                    continue
                state["task"] = act.value
                ev(turn, stage, "host_step", {"op": "activate_plan",
                                              "plan_id": state["plan"].id})
                stage = "action"
            elif op.operation == "propose_action":
                state["action"] = res.value

        # feed each dispatch's structured result back so the next turn can use
        # the real ids/revisions inside it (§25 provenance — never invented;
        # note functions are keyed by the DISPATCHED ops — when same-emission
        # dedup collapsed calls, only the surviving op got a payoff)
        if turn_payloads:
            contents.append({"role": "user", "parts": [
                {"function_response": {"name": op_name, "_call_id": f"call_{op_name}",
                                       "response": payload}}
                for (op_name, payload) in turn_payloads
            ]})

        if turn_failed:
            rejections += 1
            if rejections > 4:
                halted_reason = "too many membrane rejections"
                break

    if state["action"] is None:
        return {"ok": False, "reason": halted_reason or "ran out of turns",
                "events": [asdict(e) for e in events]}

    # ── host drives the existing execution boundary (no model involved) ─────
    action = state["action"]
    ok_req, rej = _caps.execute_action(store, action, _caps.get("file_write"),
                                       safety_check=make_confirmation_gate(required=False),
                                       plan_id_for_validation=state["plan"].id)
    ev(turn, "execute", "host_step",
       {"op": "execute_action",
        "result": _result_payload(ok_req) if ok_req is not None else _result_payload(rej)})
    if rej is not None:
        return {"ok": False, "reason": f"execution rejected: {rej.reason}",
                "events": [asdict(e) for e in events]}

    # evidence + independent verification (Emission Is Not Occurrence gate)
    evi = evidence.record_runtime_evidence(
        store, ok_req.value["observation_id"], source="file_write",
        relevance_to=state["task"].id,
        content={"action_id": action.id}).value
    ev(turn, "execute", "host_step", {"op": "record_runtime_evidence", "evidence_id": evi.id})
    for binding in state["verifications"]:
        vr = verification.run_method_for_action(store, binding["verification_id"], action.id)
        ev(turn, "verify", "host_step",
           {"operation": "verify_file_write", "verification_id": binding["verification_id"],
            "result": _result_payload(vr)})

    # step + task completion through Work Service
    s = work.load_step(store.read(), state["step"].id)
    s = work.transition_object(store, s.id, StepStatus.READY, s.revision).value
    s = work.transition_object(store, s.id, StepStatus.EXECUTING, s.revision).value
    work.complete_step(store, s.id, s.revision)
    s = work.load_step(store.read(), state["step"].id)
    ev(turn, "complete", "host_step", {"op": "complete_step", "status": s.status.value})
    t = work.load_task(store.read(), state["task"].id)
    cr = work.complete_object(store, t.id, t.revision)
    ev(turn, "complete", "host_step", {"op": "complete_object(task)",
                                       "result": _result_payload(cr)})

    file_bytes = None
    import pathlib
    p = pathlib.Path(target_path)
    if p.exists() and p.is_file():
        file_bytes = p.read_text(encoding="utf-8")

    transcript = {
        "ok": isinstance(cr, Ok) and cr.value["object"].status == TaskStatus.COMPLETED,
        "events": [asdict(e) for e in events],
        "final": {
            "file_exists": p.exists(),
            "file_bytes_match": file_bytes == content,
            "task_status": (cr.value["object"].status.value if isinstance(cr, Ok)
                            else getattr(cr, "reason", "REJECTED")),
            "step_status": s.status.value,
        },
        "provider": getattr(provider, "model_name", type(provider).__name__),
    }
    if transcript_path:
        with open(transcript_path, "w") as f:
            json.dump(transcript, f, indent=2, default=str)
    return transcript
