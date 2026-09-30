"""Conversational boundary — host-layer classification (Option A).

Implements exactly INVESTIGATION_conversational_boundary.md §4
(audit/conversational-boundary @ e85ed77): a classification step that runs
BEFORE the Work pipeline is ever invoked, deciding whether an input is a
genuine work request or ordinary conversation. The investigation established
that the live loop structurally cannot avoid turning conversational input
into committed canonical Work (single-tool goal stage + never-emit-free-text
prompt); this module is the boundary that prevents the pipeline from starting
at all for non-Work input.

HOST LAYER ONLY — the mirror image of the confirmation gate's design (a
proceed/not-proceed decision made OUTSIDE the frozen mutation surface by an
authority that is not Cognition):

  * imports nothing from the Cognition module — the frozen four-function
    surface is untouched; the classify tool below is host-owned and is
    deliberately NOT added to `_OPERATION_FIELDS`;
  * performs NO Work Service mutation of any kind (no goal creation, no
    store writes — canonical state is never touched on this path);
  * is fail-safe in the conservative direction: any classifier exception,
    timeout, malformed/unparseable output, or ambiguous result falls back to
    `"work"`, which is byte-for-byte today's pre-boundary behavior. The
    boundary degrades to the status quo, never to silence.

The conversational reply path carries a rendering-policy guard: replies are
instructed never to claim Work outcomes, and a post-hoc claim guard enforces
it mechanically — a reply containing a first-person completion claim
("I've created your file", "I've deleted that", ...) is replaced by an honest
canned line. Prompt instruction alone is not trusted here.
"""
from __future__ import annotations

import re

from v5.models import Ok, Result
from v5.store import Store

# ── the host-owned classify tool (never added to the frozen Cognition
#    operation table — see the structural test that enforces this) ──────────

CLASSIFY_TOOL = {
    "name": "classify",
    "description": "Classify the user's message as a work request or conversation.",
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["work", "conversation"]},
        },
        "required": ["kind"],
    },
}

_KINDS = ("work", "conversation")

_CLASSIFY_INSTRUCTION = (
    "You are a strict classifier. Decide whether the user's message is a "
    "request to perform concrete WORK or ordinary CONVERSATION. WORK means a "
    "request for the system to actually do something to the filesystem or "
    "system state: create, write, read, or delete files; run an operation; "
    "complete a task. CONVERSATION means greetings, small talk, questions "
    "about you or your capabilities, thanks, chit-chat, or anything you can "
    "simply answer in a sentence. When unsure, classify as work. Reply with "
    "the classify tool call only."
)

_REPLY_INSTRUCTION = (
    "You are JARVIS in conversational mode. The user's message is "
    "conversation, not a work request. Reply briefly, naturally, and helpfully. "
    "STRICT RULE: you are in a mode where NO actions are performed. Never "
    "claim to have performed, completed, or done any work or action — "
    "phrases like \"I've created your file\", \"I've deleted that\", \"done\", "
    "or any Work-completion claim are forbidden. If the user seems to be "
    "asking for real work, tell them to ask for it directly so it can be "
    "set up as a task."
)

# ── the reply claim guard (mechanical, not prompt-only) ──────────────────────

# First-person Work-completion claims, per the investigation's named phrases.
_REPLY_CLAIM_PATTERNS = [
    re.compile(r"\bi(?:'ve| have|’ve)?\s+(?:already\s+)?(?:created|made|"
               r"written|wrote|deleted|removed|completed|executed|saved|"
               r"set\s+up|built|generated)\b", re.I),
    re.compile(r"\bi(?:'ve| have|’ve)?\s+(?:already\s+)?done\s+(?:it|that|"
               r"this|so)\b", re.I),
    # completion claims without an explicit "I" are still claims
    re.compile(r"\bdone\s+(?:it|that|this)\b", re.I),
    # a bare "done!" as the entire reply is a completion claim
    re.compile(r"^done\b\s*[!.]?$", re.I),
    # "I just <verbed>" — simple-past completion claim
    re.compile(r"\bi\s+just\s+(?:created|made|wrote|written|deleted|removed|"
               r"completed|executed|saved|built|generated)\b", re.I),
]

_GUARDED_REPLY = ("I haven't performed any actions — this is conversation "
                  "mode. If you'd like me to actually do that, just ask "
                  "directly and I'll set it up as a task.")

_UNAVAILABLE_REPLY = "I'm having trouble answering right now."


def reply_has_work_claim(text: str) -> bool:
    """Mechanical enforcement of the conversational rendering contract: True
    when the reply contains a first-person Work-completion claim that must
    not be shown as-is (the model was instructed not to produce these; this
    guarantees it)."""
    return any(p.search(text or "") for p in _REPLY_CLAIM_PATTERNS)


# ── classification (fail-safe to "work") ─────────────────────────────────────

def _extract_function_call(response, name: str):
    """Provider-neutral extraction of a genuine tool-call slot: the same
    neutral response shape the live loop uses (candidates[].content.parts
    with .function_call / .text). Returns (args_dict, text_joined) or None."""
    for c in getattr(response, "candidates", []) or []:
        for part in getattr(getattr(c, "content", None), "parts", []) or []:
            fc = getattr(part, "function_call", None)
            if fc is not None and getattr(fc, "name", None) == name:
                return fc.args
    return None


def _extract_text(response) -> str:
    texts = []
    for c in getattr(response, "candidates", []) or []:
        for part in getattr(getattr(c, "content", None), "parts", []) or []:
            t = getattr(part, "text", None)
            if t:
                texts.append(t)
    return "".join(texts)


def classify_request(store: Store, session_id: str, instruction: str,
                     provider) -> Result:
    """One additional deterministic model call (same provider/config as the
    live loop — temperature 0, native function calling) exposing exactly one
    host-owned tool: classify {kind: work|conversation}.

    Fail-safe is a HARD requirement (investigation §1.4): ANY failure —
    exception (including provider timeouts, which raise), missing tool call,
    unparseable arguments, or an out-of-enum kind — returns kind="work",
    which routes to today's existing run_live_slice path unchanged. Never
    raises, never mutates, never renders. `store`/`session_id` are part of
    the report-specified seam signature; this path touches neither."""
    try:
        resp = provider.call(
            [{"role": "user", "parts": [
                {"text": _CLASSIFY_INSTRUCTION
                 + f"\n\nUser message: {instruction}"}]}],
            [CLASSIFY_TOOL],
            ("classify",),
        )
        args = _extract_function_call(resp, "classify")
        if not isinstance(args, dict):
            return Ok({"kind": "work",
                       "fallback": "no classify tool call in response"})
        kind = args.get("kind")
        if kind not in _KINDS:
            return Ok({"kind": "work",
                       "fallback": f"unparseable kind: {kind!r}"})
        return Ok({"kind": kind, "fallback": None})
    except Exception as e:  # timeouts/HTTP/provider errors raise — all fall back
        return Ok({"kind": "work",
                   "fallback": f"classifier failure: {type(e).__name__}"})


# ── the conversational reply (no tools; guarded claims) ─────────────────────

def conversation_reply(store: Store, session_id: str, instruction: str,
                       provider) -> Result:
    """Generate the direct conversational reply (one text-only model call —
    no tools exposed). Zero canonical state is touched. The reply is passed
    through the mechanical claim guard: a Work-completion claim is replaced
    by the honest canned line. Any generation failure returns an honest
    canned unavailable line — NEVER routed to the work path (that would
    recreate the pollution this boundary exists to prevent) and never
    rendered as an ERROR (conversation is not a failure)."""
    try:
        resp = provider.call(
            [{"role": "user", "parts": [
                {"text": _REPLY_INSTRUCTION
                 + f"\n\nUser message: {instruction}"}]}],
            [],
            (),
        )
        text = _extract_text(resp).strip()
        if not text:
            return Ok({"reply": _UNAVAILABLE_REPLY, "unavailable": True})
        if reply_has_work_claim(text):
            return Ok({"reply": _GUARDED_REPLY, "claim_guarded": True})
        return Ok({"reply": text, "claim_guarded": False})
    except Exception as e:
        return Ok({"reply": _UNAVAILABLE_REPLY,
                  "unavailable": True,
                  "error": f"{type(e).__name__}"})
