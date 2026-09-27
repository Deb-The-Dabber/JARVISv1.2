#!/usr/bin/env python3
"""Live LLM slice proof, v2 — two-step plan (write, then read) driven by a
real deterministic model call chain. Writes the full transcript to
artifacts/live_slice_v2_transcript.json (never overwrites the v1 artifact).
Credentials come ONLY from env (~/Jarvis/.env is loaded if present).
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


def main() -> int:
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="jarvis_live_v2_"))
    target = str(workdir / "foo.txt")
    content = "Hello World"
    instruction = ("Create foo.txt containing Hello World, "
                   "then read it back and confirm its contents.")

    store = Store(str(workdir / "state.db"))
    sess = sessions.create_session(store, cognition_authorized=True)
    client = live_loop.NvidiaNimLiveClient()

    tr = live_loop.run_live_slice(store, sess.id, instruction, target, content, client,
                                  transcript_path=str(workdir / "transcript.json"))
    out = pathlib.Path(__file__).resolve().parent.parent / "artifacts" / "live_slice_v2_transcript.json"
    out.write_text(json.dumps(tr, indent=2, default=str))
    sessions.terminate_session(store, sess.id)
    store.close()

    print(f"WORKDIR={workdir}")
    print(f"ARTIFACT={out}")
    print(f"OK={tr['ok']}")
    print("FINAL=" + json.dumps(tr["final"], indent=1))
    return 0 if tr["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
