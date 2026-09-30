#!/usr/bin/env python3
"""SCRATCH — corpus re-run driver for Conversational Boundary v1.

NON-DELIVERABLE SCRATCH: drives the REAL interactive terminal (with the real
provider and the real conversational boundary) over the investigation's exact
10-input corpus, one fresh DB per input, and records classification outcome,
rendering path, exit code, and canonical row counts. Deliverable artifact:
artifacts/conversational_boundary_corpus_rerun.json.

Credentials: env / ~/Jarvis/.env only; never printed.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

from dotenv import load_dotenv
load_dotenv(os.path.expanduser("~/Jarvis/.env"))

import sqlite3

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

SCRIPT = pathlib.Path(__file__).resolve().parent / "scripts" / "run_interactive.py"


def row_counts(db_path):
    conn = sqlite3.connect(db_path)
    try:
        out = {}
        for t in ("goals", "tasks", "plans", "steps", "actions",
                  "confirmations", "observations"):
            out[t] = conn.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()[0]
        return out
    finally:
        conn.close()


def classify_outcome(stdout):
    if "jarvis: " in stdout:
        return "conversation"
    if "CONFIRMATION_REQUIRED" in stdout:
        return "work (gate-paused)"
    if "OK: Task COMPLETED" in stdout:
        return "work (completed)"
    if "ERROR" in stdout:
        return "work (error/halt)"
    return "unknown"


def main():
    workroot = pathlib.Path(tempfile.mkdtemp(prefix="cb_rerun_"))
    results = []
    for i, text in enumerate(CORPUS):
        db = workroot / f"case_{i}.db"
        try:
            proc = subprocess.run(
                [sys.executable, str(SCRIPT)],
                input=f"{text}\n/quit\n",
                capture_output=True, text=True, timeout=420,
                env={**os.environ, "JARVIS_V5_DB": str(db),
                     "JARVIS_ENV_FILE": os.path.expanduser("~/Jarvis/.env")},
            )
            stdout, rc = proc.stdout, proc.returncode
        except subprocess.TimeoutExpired:
            stdout, rc = "", -1
        outcome = classify_outcome(stdout)
        counts = row_counts(db) if db.exists() else {}
        reply_line = next((l for l in stdout.splitlines()
                           if l.startswith("jarvis: ")), None)
        results.append({
            "input": text,
            "exit_code": rc,
            "classification_outcome": outcome,
            "conversational_reply": reply_line,
            "gate_paused": "CONFIRMATION_REQUIRED" in stdout,
            "canonical_row_counts": counts,
            "stdout_excerpt": stdout[:400],
        })
        print(f"[{i}] {text!r} -> {outcome} | rows={counts} | rc={rc}", flush=True)
        if reply_line:
            print(f"     reply: {reply_line[:100]}", flush=True)
        time.sleep(3)

    out = pathlib.Path(__file__).resolve().parent / "artifacts" / \
        "conversational_boundary_corpus_rerun.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"\nwritten: {out}")


if __name__ == "__main__":
    main()
