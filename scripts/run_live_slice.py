#!/usr/bin/env python3
"""Live LLM slice proof — one real deterministic model call chain.

Runs the frozen vertical slice ("Create foo.txt containing Hello World")
through the actual Gemini tool-calling API, the Cognition membrane, the real
execution boundary, and the independent verifier, and writes the full
transcript to artifacts/. Credentials come ONLY from the environment (a local
~/Jarvis/.env is loaded for convenience if present, contents never printed).
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
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="jarvis_live_slice_"))
    target = str(workdir / "foo.txt")
    content = "Hello World"
    instruction = f"Create foo.txt containing Hello World."

    store_path = str(workdir / "state.db")
    store = Store(store_path)

    sess = sessions.create_session(store, cognition_authorized=True)
    # single deterministic provider (§1d): NVIDIA NIM, temperature 0, native
    # tool calling. Key read from NVIDIA_NEMOTRON_API_KEY env var (loaded from
    # the local ~/Jarvis/.env convenience file). Never printed/logged.
    client = live_loop.NvidiaNimLiveClient()

    transcript = live_loop.run_live_slice(
        store, sess.id, instruction, target, content, client,
        transcript_path=str(workdir / "transcript.json"))

    # save to artifacts dir in repo for the report
    out = pathlib.Path(__file__).resolve().parent.parent / "artifacts" / "live_slice_transcript.json"
    out.write_text(json.dumps(transcript, indent=2, default=str))
    sessions.terminate_session(store, sess.id)
    store.close()

    print(f"WORKDIR={workdir}")
    print(f"ARTIFACT={out}")
    print(f"OK={transcript['ok']}")
    print(f"FINAL={json.dumps(transcript['final'], indent=1)}")
    return 0 if transcript["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
