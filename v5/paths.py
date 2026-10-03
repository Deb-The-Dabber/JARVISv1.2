"""Host-owned workspace path resolution (Path Resolution / Workspace v1).

The filesystem is HOST territory, never model territory. The model may
propose a path exactly as the user said it — "helloworld.txt",
"notes/today.txt" — and this module deterministically resolves it inside
the canonical JARVIS workspace. The prompt is NOT the boundary: this
resolver is. (Before this module, a bare filename had no meaning, so the
model invented absolute paths like "/helloworld.txt" — on macOS that hits
the read-only system root and became UNKNOWN_OUTCOME.)

Policy (frozen for v1 — DECISIONS #35):
  * bare filename / relative path  → resolves inside the workspace
  * "./relative"                   → identical (normalized)
  * absolute path INSIDE the workspace → preserved as-is (a re-pasted
    resolved target is exactly what the relative form resolves to; nothing
    is silently rewritten)
  * absolute path OUTSIDE the workspace → native structured rejection
    ("absolute paths outside the JARVIS workspace are not allowed") —
    explicit, deterministic, and testable; the model receives it as
    ordinary proposal feedback and can retry with a relative path
  * ".." traversal that stays inside the workspace → allowed (normalized)
  * ".." traversal that escapes the workspace → native rejection
  * symlink escape (workspace/link → /outside) → caught by resolving the
    final target with realpath before the containment check
  * empty / whitespace-only / NUL-byte / non-string / oversize paths →
    native rejection
  * the workspace root itself is not a valid file target

Canonical Action arguments keep the model's literal path (auditability,
deterministic replay, Law-32 confirmation binding). Resolution happens
independently at THREE host points, each applying THIS same policy:
  1. capability `validate_args` (proposal time — native early rejection);
  2. capability execution (defense in depth — the resolved target is the
     one actually touched, and it is recorded in the Observation);
  3. the registered verifiers (independent filesystem inspection — they
     resolve the Action's canonical args themselves and never trust the
     capability's self-report).

The workspace default follows the repository's existing durable-state
convention (~/.jarvis_v5/ already holds state.db, outside the V5 repo),
and is overridable via JARVIS_V5_WORKSPACE (tests point it at tmp_path).
Resolution itself is side-effect-free; creation happens at terminal
startup and via the capabilities' existing parent-mkdir.
"""
from __future__ import annotations

import os
import pathlib

WORKSPACE_ENV = "JARVIS_V5_WORKSPACE"
DEFAULT_WORKSPACE = "~/.jarvis_v5/workspace"


def workspace_root(create: bool = False) -> pathlib.Path:
    """The canonical writable workspace root, fully resolved (symlinks in
    the configured location are resolved so containment checks compare
    apples to apples). Side-effect-free unless create=True."""
    raw = os.environ.get(WORKSPACE_ENV) or DEFAULT_WORKSPACE
    root = pathlib.Path(raw).expanduser().resolve()
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_workspace_path(raw) -> tuple[pathlib.Path | None, str | None]:
    """Resolve a user/model-supplied path under the host policy.

    Returns (resolved_path, None) on success or (None, native_reason) on
    policy rejection. Deterministic: the same input resolves to the same
    target for the same filesystem state."""
    if not isinstance(raw, str):
        return None, "path must be a non-empty string"
    if not raw or not raw.strip():
        return None, "path must be a non-empty string"
    if "\x00" in raw:
        return None, "path contains NUL bytes"
    if len(raw) > 4096:
        return None, "path exceeds 4096 chars"
    try:
        p = pathlib.Path(raw).expanduser()
        root = workspace_root()
        candidate = p if p.is_absolute() else root / p
        # lexical normalization (., .., duplicate separators) then realpath
        # (symlinks resolved on the existing portion) — containment is
        # checked against the FULLY resolved target, so a workspace symlink
        # pointing outside cannot smuggle an escape through
        normalized = os.path.normpath(str(candidate))
        resolved = pathlib.Path(os.path.realpath(normalized))
        if resolved == root:
            return None, "cannot target the JARVIS workspace root itself"
        try:
            resolved.relative_to(root)
        except ValueError:
            if p.is_absolute():
                return None, ("absolute paths outside the JARVIS workspace are "
                              "not allowed — use a relative path and the host "
                              "resolves it inside the JARVIS workspace")
            return None, "path escapes the JARVIS workspace (.. traversal)"
        return resolved, None
    except OSError as e:
        return None, f"path could not be resolved: {e}"
