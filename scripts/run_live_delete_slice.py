#!/usr/bin/env python3
"""Live confirmation-gate proof — a real deterministic model proposes a
file_delete, the gate blocks unconfirmed execution, the host confirms, and
the deletion completes through Observation + independent verification.

Writes the full transcript to artifacts/live_slice_delete_transcript.json
(a NEW file — the v1/v2 artifacts are never overwritten). Credentials come
ONLY from the environment (~/Jarvis/.env loaded for convenience, values
never printed).
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(os.path.expanduser("~/Jarvis/.env"))

from v5.store import Store
from v5 import live_loop, sessions


GUIDANCE_HEAD = (
    "propose_plan carries one StepProposal for this milestone: a single "
    "file_delete step (execution_capability \"file_delete\") whose "
    "verification_requirements name verify_file_delete with "
    "applies_to_capability \"file_delete\". file_delete arguments: {\"path\"} "
    "only. Target path: ")


def main() -> int:
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="jarvis_live_delete_"))
    target = workdir / "temporary_test_file.txt"
    target.write_text("temporary test content — created solely to be deleted")
    instruction = "Delete this temporary test file."

    print(f"target: {target}")
    print(f"exists before run: {target.exists()}")

    store = Store(str(workdir / "state.db"))
    sess = sessions.create_session(store, cognition_authorized=True)
    client = live_loop.NvidiaNimLiveClient()   # reads NVIDIA_NEMOTRON_API_KEY; never printed

    tr = live_loop.run_live_slice(
        store, sess.id, instruction, str(target), "", client,
        transcript_path=str(workdir / "transcript.json"),
        plan_guidance=GUIDANCE_HEAD + json.dumps(str(target)))

    out = pathlib.Path(__file__).resolve().parent.parent / "artifacts" / "live_slice_delete_transcript.json"
    out.write_text(json.dumps(tr, indent=2, default=str))
    sessions.terminate_session(store, sess.id)
    store.close()

    print(f"exists after run:  {target.exists()}")
    print(f"ARTIFACT={out}")
    print(f"OK={tr['ok']}")
    print("FINAL=" + json.dumps(tr["final"], indent=1))
    return 0 if tr["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
