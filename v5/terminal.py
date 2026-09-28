"""Minimal interactive terminal/debug adapter around the V5 live loop.

A THIN ADAPTER ONLY. This module owns exactly three things:

  1. terminal input reading (three separate channels: normal requests,
     local commands, confirmation decisions);
  2. local commands (/help, /debug, /quit) — they never enter the live loop;
  3. rendering (concise normal output, structured [debug] output, safe
     error rendering with secret redaction).

It owns NOTHING of JARVIS: it creates no Goals/Tasks/Plans/Actions, never
executes capabilities, never touches canonical state, and contains no
planner/dispatcher/safety/confirmation/execution logic of its own. Every
request is one `live_loop.run_live_slice(...)` call (one live-loop turn
through the frozen Cognition membrane); every confirmation decision flows
through the existing confirmation mechanism inside the live loop's
`confirmation_policy` seam (the loop calls the policy, the policy stages +
confirms via v5.safety, and the loop retries the SAME Action — identity,
revision, arguments, and binding are preserved by construction, and no
duplicate Work is created).

Interactive I/O lives ONLY here — never inside capabilities, execution,
Work Service, verification, safety, or the state foundation.

Input channels are sequential single-line reads from one input_fn; the
reader never reads ahead, so a line destined for a future confirmation
prompt is never consumed early.

Exit statuses (§12 of the terminal contract):
  0 — /quit, EOF (normal, multiline-discard, confirmation-not-approved),
       handled JARVIS request failures (structured rejections, execution
       failures, verification failures, confirmation denial);
  1 — unrecoverable terminal/runtime failure (provider initialization
       failure on first request, output infrastructure failure).
A JARVIS request failing is NEVER a nonzero exit; an infrastructure
failure is never silently absorbed as success.
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Callable

from v5 import live_loop, safety
from v5.models import Ok, Rejected, Result
from v5.store import Store

# ── deterministic output contract strings (§14) ─────────────────────────────

PROMPT = "jarvis> "
MULTILINE_PROMPT = "...> "
CONFIRM_PROMPT = "Confirm action? [y/N]: "
BANNER = "JARVIS V5 interactive terminal — /help for commands.\n"
EOF_EXIT_MSG = "EOF: closing.\n"
MULTILINE_EOF_MSG = "EOF: incomplete multiline request discarded.\n"
NOT_APPROVED_MSG = "NOT APPROVED: pending action was not approved.\n"
DECLINED_MSG = "DECLINED: action was not approved; nothing was executed.\n"

HELP_TEXT = """\
Commands:
  /help   show this help
  /debug  toggle structured debug output
  /quit   exit the terminal
Multiline input:
  <<EOF   begin multiline input (marker must be alone on its line)
  ...>    continuation prompt for each body line
  END     submit the request (marker must be alone on its line; the
          markers themselves are excluded from the submitted content)
Multiline rules: blank lines and whitespace are preserved; `END` on a
line by itself is the only terminator; commands inside multiline body are
literal content; an empty multiline body is ignored (nothing submitted).
Other: empty input is ignored; whitespace-only input is submitted as a
request; confirmation prompts are answered with y (approve) or anything
else (deny). Requests run through the live Cognition loop; capabilities
requiring confirmation will pause and ask.
"""

# Generic plan guidance for arbitrary terminal requests: names ONLY the
# registered capabilities/verifiers (tests assert registry consistency).
TERMINAL_GUIDANCE = (
    "propose_plan carries one StepProposal per step the instruction needs; "
    "use only the registered capabilities: file_write (arguments "
    "{\"path\", \"content\"}), file_read (arguments {\"path\"}), file_delete "
    "(arguments {\"path\"}); each step's verification_requirements name the "
    "matching method (verify_file_write, verify_file_read, verify_file_delete) "
    "with applies_to_capability equal to the step's execution_capability; "
    "later steps depend on earlier ones via depends_on_index; always use "
    "absolute paths taken from the instruction, never paths inside the "
    "JARVIS repository tree"
)

COMMANDS = ("/help", "/debug", "/quit")

# Reasons produced by this adapter's confirmation policy (consumed by
# rendering; they are ordinary structured Rejected reasons, not new state).
CONFIRM_DECLINED = "CONFIRMATION_DECLINED"
CONFIRM_EOF = "CONFIRMATION_EOF"


def redact_secrets(text: str) -> str:
    """Replace credential-ish environment values that appear in text with
    *** (never render a key/token/secret, including in error paths)."""
    try:
        for name, value in os.environ.items():
            if not value or len(value) < 8:
                continue
            if re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", name or "", re.I):
                text = text.replace(value, "***")
    except Exception:
        pass
    return text


def _stdin_line() -> str:
    """Default input reader: one line from stdin, EOF at end.

    Removes ONLY the line terminator (\\n or a preceding \\r); meaningful
    leading/trailing whitespace is preserved. Never reads ahead."""
    line = sys.stdin.readline()
    if line == "":
        raise EOFError
    if line.endswith("\n"):
        line = line[:-1]
    if line.endswith("\r"):
        line = line[:-1]
    return line


def _stdout(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


class InteractiveTerminal:
    """The thin terminal adapter. One instance = one interactive process."""

    def __init__(self, store: Store, session, provider_factory: Callable,
                 input_fn: Callable[[], str] | None = None,
                 output_fn: Callable[[str], None] | None = None,
                 debug: bool = False):
        self._store = store
        self._session = session
        self._provider_factory = provider_factory   # lazy: built on first request
        self._provider = None
        self._input = input_fn or _stdin_line
        self._emit = output_fn or _stdout
        self.debug = debug
        self.requests = 0          # live-loop submissions made (test surface)
        self.model_calls = 0       # provider invocations observed (test surface)

    # ── rendering helpers ────────────────────────────────────────────────────

    def _render_confirmation_request(self, action) -> None:
        """CONFIRMATION_REQUIRED + a SAFE action summary (no secrets, no raw
        payloads — arguments are the user's own declared values)."""
        args = redact_secrets(json.dumps(action.arguments, sort_keys=True))
        self._emit("CONFIRMATION_REQUIRED\n")
        self._emit(f"action: {action.capability} {args}\n")
        self._emit(CONFIRM_PROMPT)

    def _render_result(self, result: dict) -> None:
        if result.get("ok"):
            task = (result.get("final") or {}).get("task_status", "COMPLETED")
            self._emit(f"OK: Task {task}\n")
            return
        reason = result.get("reason") or "UNKNOWN"
        if reason == CONFIRM_DECLINED:
            self._emit(DECLINED_MSG)
        elif reason == CONFIRM_EOF:
            self._emit(NOT_APPROVED_MSG)
        else:
            self._emit(f"ERROR {reason}\n")

    def _render_debug_events(self, result: dict) -> None:
        """Structured debug output — REAL transcript events from the actual
        live-loop run, never terminal narration. No payloads/secrets: only
        event identity (kind/op/status/reason codes)."""
        for e in result.get("events", []):
            d = e.get("detail") or {}
            bits = [str(e.get("kind"))]
            for key in ("operation", "op"):
                if d.get(key):
                    bits.append(str(d[key]))
                    break
            if d.get("status"):
                bits.append(str(d["status"]))
            if d.get("reason"):
                bits.append(str(d["reason"]))
            self._emit(f"[debug] turn={e.get('turn')} stage={e.get('stage')} "
                       + " ".join(bits) + "\n")
        self._emit("[debug] turn complete\n")

    # ── channel 3: confirmation decision (via the live loop's policy seam) ──

    def confirmation_policy(self, store: Store, action) -> Result:
        """The live loop calls this when its gate blocked an unconfirmed
        Action. The turn is PAUSED here; the human decides; approval flows
        through the EXISTING confirmation mechanism (safety.create_confirmation
        + safety.confirm — the same Law 32 path the loop's default policy
        uses) and the loop then retries the SAME Action. Denial/EOF returns a
        Rejected so the loop resolves the turn truthfully (nothing executes)."""
        self._render_confirmation_request(action)
        try:
            decision = self._input()
        except EOFError:
            return Rejected(CONFIRM_EOF, "pending action not approved (EOF)", action)
        if decision.strip().lower() in ("y", "yes"):
            staged = safety.create_confirmation(store, action.id, action.revision,
                                               action.capability, action.arguments)
            if isinstance(staged, Rejected):
                return staged
            confirmed = safety.confirm(store, staged.value)
            if isinstance(confirmed, Rejected):
                return confirmed
            return Ok({"confirmation_id": staged.value})
        return Rejected(CONFIRM_DECLINED, "user declined confirmation", action)

    # ── live-loop submission (one request = one turn) ───────────────────────

    def _ensure_provider(self) -> bool:
        """Lazy provider construction: local commands and EOF work with no
        credentials at all; the provider is built on the first request.
        Returns False on unrecoverable initialization failure (FATAL, exit 1)."""
        if self._provider is None:
            try:
                self._provider = self._provider_factory()
            except Exception as e:
                self._emit(f"FATAL: provider initialization failed: "
                           f"{redact_secrets(str(e))}\n")
                return False
            if self.debug:
                self._emit(f"[debug] provider {getattr(self._provider, 'model_name', '?')}\n")
        return True

    def submit(self, instruction: str):
        """Run ONE live-loop turn. Returns None to continue the session, or an
        int exit status (0 = confirmation-EOF exit; 1 = fatal). JARVIS request
        failures are rendered and the session continues."""
        if not self._ensure_provider():
            return 1
        self.requests += 1
        if self.debug:
            head = instruction if len(instruction) <= 60 else instruction[:57] + "..."
            self._emit(f"[debug] turn submit: {redact_secrets(head)}\n")

        class _Counting:
            def __init__(s, inner):
                s.inner = inner
            def call(s, contents, tools, allowed):
                self.model_calls += 1
                return s.inner.call(contents, tools, allowed)

        try:
            result = live_loop.run_live_slice(
                self._store, self._session.id, instruction, "", "",
                _Counting(self._provider),
                plan_guidance=TERMINAL_GUIDANCE,
                confirmation_policy=self.confirmation_policy)
        except Exception as e:
            # handled request/runtime failure: render truthfully (sanitized),
            # do not fabricate success, keep the session usable
            self._emit(f"ERROR {type(e).__name__}: {redact_secrets(str(e))}\n")
            if self.debug:
                self._emit("[debug] turn aborted by exception\n")
            return None
        if self.debug:
            self._render_debug_events(result)
        self._render_result(result)
        if not result.get("ok") and result.get("reason") == CONFIRM_EOF:
            # EOF at the confirmation prompt: the action was not approved;
            # stdin is exhausted — exit cleanly.
            return 0
        return None

    # ── channel 1/2: the main read loop ─────────────────────────────────────

    def _read_multiline(self) -> str | None:
        """Read `<<EOF ... END` bodies. Returns the joined body (markers
        excluded, whitespace/blank lines preserved), "" for an empty body
        (ignored by the caller), or None on EOF (incomplete input discarded).
        Reads exactly one line per continuation prompt — no read-ahead."""
        lines: list[str] = []
        while True:
            self._emit(MULTILINE_PROMPT)
            try:
                line = self._input()
            except EOFError:
                self._emit(MULTILINE_EOF_MSG)
                return None
            if line == "END":
                return "\n".join(lines)
            lines.append(line)

    def run(self) -> int:
        """The interactive process. Returns the process exit status."""
        self._emit(BANNER)
        while True:
            self._emit(PROMPT)
            try:
                line = self._input()
            except EOFError:
                self._emit(EOF_EXIT_MSG)
                return 0
            if line == "":
                continue                                    # empty input: ignore
            if line in COMMANDS:
                if line == "/help":
                    self._emit(HELP_TEXT)
                elif line == "/debug":
                    self.debug = not self.debug
                    self._emit(f"debug: {'on' if self.debug else 'off'}\n")
                else:  # /quit
                    return 0
                continue
            if line.startswith("/"):
                self._emit("LOCAL ERROR: unknown command\n")  # never sent to the loop
                continue
            if line == "<<EOF":
                body = self._read_multiline()
                if body is None:
                    return 0                                # EOF: discard + clean exit
                if body == "":
                    continue                                # empty body: not submitted
                instruction = body
            else:
                instruction = line
            rc = self.submit(instruction)
            if rc is not None:
                return rc
