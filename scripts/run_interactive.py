#!/usr/bin/env python3
"""Interactive JARVIS V5 terminal — the thin adapter around the real live
loop and the real provider. Run:

    python3 scripts/run_interactive.py

Environment (none are secrets; none are hardcoded):
  JARVIS_V5_DB    state DB path (default: ~/.jarvis_v5/state.db)
  JARVIS_V5_WORKSPACE  canonical writable workspace for file capabilities
                  (default: ~/.jarvis_v5/workspace — bare filenames and
                  relative paths resolve there; absolute paths outside it
                  are rejected)
  JARVIS_ENV_FILE dotenv file for provider credentials
                  (default: ~/Jarvis/.env — the repository's existing
                  convention; values are never printed)
  NVIDIA_NEMOTRON_API_KEY  provider credential, read only from the
                           environment/dotenv file by NvidiaNimLiveClient

Exit statuses: 0 for clean/handled termination (quit, EOF, request
failures, confirmation denial/EOF); 1 for unrecoverable initialization or
runtime failure.
"""
from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from v5 import live_loop, sessions
from v5.store import Store
from v5.terminal import InteractiveTerminal


def _default_db() -> pathlib.Path:
    return pathlib.Path.home() / ".jarvis_v5" / "state.db"


def main() -> int:
    db_path = pathlib.Path(os.environ.get("JARVIS_V5_DB")
                           or str(_default_db()))
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = Store(str(db_path))
        # Path Resolution v1: create the canonical writable workspace
        from v5 import paths as _paths
        _paths.workspace_root(create=True)
    except Exception as e:
        print(f"FATAL: state store initialization failed: {e}", file=sys.stderr)
        return 1
    session = None
    try:
        session = sessions.create_session(store)

        def provider_factory():
            # lazy: local commands/EOF need no credentials; the provider is
            # built on the first live request through the real integration
            dotenv = os.environ.get("JARVIS_ENV_FILE",
                                    os.path.expanduser("~/Jarvis/.env"))
            return live_loop.NvidiaNimLiveClient(dotenv_path=dotenv)

        term = InteractiveTerminal(store, session, provider_factory)
        return term.run()
    except KeyboardInterrupt:
        print("\ninterrupted.")
        return 1
    finally:
        if session is not None:
            try:
                sessions.terminate_session(store, session.id)
            except Exception:
                pass
        try:
            store.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
