#!/usr/bin/env python3
"""SCRATCH — corpus driver for the conversational-boundary audit.

*** NON-DELIVERABLE SCRATCH WORK *** This exists solely to run the §1 corpus
of the investigation through the REAL live loop + REAL provider. The
deliverable is INVESTIGATION_conversational_boundary.md. This driver must not
be reviewed as if it were an implementation of anything.

Notes:
- Runs run_live_slice exactly as the interactive terminal does (the terminal
  adapter lives on interactive-terminal-v1, not on this branch; its guidance
  text is embedded below verbatim with a citation).
- Every destructive-capability proposal is auto-DENIED via the
  confirmation_policy seam (the corpus observes what gets PROPOSED, not what
  executes; nothing external happens).
- Hard call budget with honest reporting; at most ONE retry per input on a
  transient provider failure, only while budget headroom remains.
- Credentials: read only from the environment / ~/Jarvis/.env; never printed.

Usage:
  python3 scratch_corpus_driver.py [--start N]
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from dotenv import load_dotenv
load_dotenv(os.path.expanduser("~/Jarvis/.env"))

from v5 import live_loop, sessions
from v5.models import Rejected
from v5.store import Store

# TERMINAL_GUIDANCE — imported verbatim from interactive-terminal-v1's
# v5/terminal.py:82-92 (the branch where the observed "hello jarvis" session
# ran). master does not contain the terminal adapter; this reproduces the
# exact run_live_slice parameters the terminal passes.
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

CORPUS = [
    "hello",
    "hello jarvis",
    "hey",
    "how are you",
    "who are you",
    "what can you do",
    "thanks",
    "what's the weather?",
    'write "hello" to test.txt',
    "delete test.txt",
]

BUDGET = 52          # hard ceiling on total provider calls for the corpus
RETRY_HEADROOM = 40  # retry an input only while total calls stay below this


class CountingProvider:
    def __init__(self, inner, counter):
        self.inner = inner
        self.counter = counter
        self.model_name = inner.model_name

    def call(self, contents, tools, allowed_names):
        if self.counter["n"] >= BUDGET:
            raise RuntimeError("CORPUS_BUDGET_EXHAUSTED")
        self.counter["n"] += 1
        return self.inner.call(contents, tools, allowed_names)


def auto_deny(store, action):
    """Corpus policy: deny every confirmation so nothing ever executes."""
    return Rejected("CONFIRMATION_DECLINED",
                    "corpus auto-deny: nothing executes", action)


def run_one(text, workdir):
    store = Store(str(workdir / "state.db"))
    try:
        sess = sessions.create_session(store, cognition_authorized=True)
        client = live_loop.NvidiaNimLiveClient()
        provider = CountingProvider(client, COUNTER)
        result = live_loop.run_live_slice(
            store, sess.id, text, "", "", provider,
            plan_guidance=TERMINAL_GUIDANCE,
            confirmation_policy=auto_deny)
        counts = {t: store.read().execute(
            f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
            for t in ("goals", "tasks", "plans", "steps", "actions",
                      "confirmations", "observations")}
        return result, counts
    finally:
        store.close()


def summarize(result, counts):
    tools = []
    action_proposal = None
    rejections = []
    confirmation_pause = False
    executed = False
    for e in result.get("events", []):
        d = e.get("detail") or {}
        if e["kind"] == "tool_call":
            for c in d.get("calls", []):
                tools.append(c["operation"])
                if c["operation"] == "propose_action":
                    action_proposal = {"capability": c["args"].get("capability"),
                                       "arguments": c["args"].get("arguments"),
                                       "step_id": c["args"].get("step_id")}
        elif e["kind"] == "result" and d.get("status") == "rejected":
            rejections.append(d.get("reason"))
        elif e["kind"] == "host_step":
            if d.get("op") == "execution_blocked":
                confirmation_pause = True
            if d.get("op") == "execute_action" and d.get("status") == "ok":
                executed = True
    return {
        "ok": result.get("ok"),
        "termination_reason": result.get("reason"),
        "tools_called_in_order": tools,
        "action_proposal": action_proposal,
        "rejection_reasons": rejections,
        "confirmation_pause": confirmation_pause,
        "executed": executed,
        "canonical_row_counts": counts,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=0)
    args = ap.parse_args()

    workroot = pathlib.Path(tempfile.gettempdir()) / "conversational_corpus_audit"
    workroot.mkdir(exist_ok=True)
    out_path = pathlib.Path(__file__).resolve().parent / "scratch_corpus_results.json"
    results = []
    if args.start > 0 and out_path.exists():
        results = json.loads(out_path.read_text())

    for i, text in enumerate(CORPUS):
        if i < args.start:
            continue
        if COUNTER["n"] >= BUDGET:
            print(f"[{i}] BUDGET EXHAUSTED before running: {text!r} — reporting as skipped")
            results.append({"input": text, "skipped": "corpus call budget exhausted",
                            "total_calls_at_skip": COUNTER["n"]})
            continue
        case_dir = workroot / f"case_{i}"
        case_dir.mkdir(exist_ok=True)
        attempt = 0
        record = None
        while True:
            before = COUNTER["n"]
            try:
                print(f"[{i}] running: {text!r} (calls so far: {before})", flush=True)
                result, counts = run_one(text, case_dir)
                record = {"input": text, "calls_this_input": COUNTER["n"] - before,
                          **summarize(result, counts)}
                break
            except RuntimeError as e:
                msg = str(e)
                if "CORPUS_BUDGET_EXHAUSTED" in msg:
                    record = {"input": text, "skipped": "corpus call budget exhausted mid-run",
                              "calls_this_input": COUNTER["n"] - before}
                    break
                attempt += 1
                if attempt > 1 or COUNTER["n"] >= RETRY_HEADROOM:
                    record = {"input": text, "provider_failure": msg[:300],
                              "calls_this_input": COUNTER["n"] - before,
                              "retries_exhausted": True}
                    break
                print(f"[{i}] provider failure (attempt {attempt}): {msg[:160]} — retrying once", flush=True)
                time.sleep(20)
            except Exception as e:  # any unexpected failure: record honestly
                record = {"input": text, "unexpected_failure":
                          f"{type(e).__name__}: {str(e)[:300]}",
                          "calls_this_input": COUNTER["n"] - before}
                break
        results.append(record)
        out_path.write_text(json.dumps(results, indent=2, default=str))
        print(f"[{i}] -> {json.dumps({k: v for k, v in record.items() if k != 'input'})[:340]}", flush=True)
        time.sleep(3)

    print(f"\nTOTAL PROVIDER CALLS: {COUNTER['n']} / budget {BUDGET}")
    print(f"results written to {out_path}")


COUNTER = {"n": 0}

if __name__ == "__main__":
    main()
