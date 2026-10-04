"""Capability registry + the file_write capability (the vertical slice).

Capabilities are the only authority that produces runtime effects and
execution-derived Evidence/Observations (Law 29). Each capability declares
its idempotency class (Law 11) and whether confirmation is required (Law 21/
32/33 — risk-classed, confirmation-bound).
"""
from __future__ import annotations

import fnmatch
import os
import pathlib
import re
from dataclasses import dataclass
from typing import Callable

from v5 import paths as _paths
from v5.enums import IdempotencyClass


@dataclass
class CapabilitySpec:
    name: str
    idempotency_class: IdempotencyClass
    requires_confirmation: bool
    execute: Callable[[dict], dict]
    # Cognition Implementation Contract v1.1 §12.1/§42: the declared argument
    # schema. Called by proposal validation (fast-fail) AND by the execution
    # authority before an Action is accepted (authoritative). Returns an error
    # string describing the first violation, or None when the arguments are
    # schema-valid. None = "no schema declared" (pre-Contract rows/capabilities
    # keep existing behavior).
    validate_args: Callable[[dict], str | None] | None = None
    # Investigation Capability v1: registry-driven Intent Boundary + terminal
    # guidance derive their operation lists from these two host-owned fields
    # instead of hardcoding capability names in prompts (single source of
    # truth = the registry). Empty defaults keep legacy specs valid.
    summary: str = ""        # e.g. "searching and reading the JARVIS source"
    arguments_hint: str = ""  # e.g. "(arguments {\"path\"})"


class DefiniteNoEffect(Exception):
    """Raised by a capability when it can PROVE no external effect occurred
    (e.g. argument validation failed before any write). The executor converts
    this into Action.status = FAILED. Any other exception becomes
    UNKNOWN_OUTCOME + an obligation (Law 9: default-unsafe)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


_REGISTRY: dict[str, CapabilitySpec] = {}


def register(spec: CapabilitySpec):
    _REGISTRY[spec.name] = spec


def get(name: str) -> CapabilitySpec | None:
    return _REGISTRY.get(name)


# ── file_write ───────────────────────────────────────────────────────────────

def _file_write_validate_args(args: dict) -> str | None:
    """Declared argument schema for file_write (§42). Runs BEFORE persistence —
    at Cognition proposal time and at Action acceptance — never only at effect
    time. Bounds here are the capability's own contract; the Cognition-layer
    bounds (collection sizes/depth) are enforced separately."""
    if not isinstance(args, dict):
        return "arguments must be an object"
    allowed = {"path", "content"}
    unknown = sorted(set(args) - allowed)
    if unknown:
        return f"unknown argument field(s): {', '.join(unknown)}"
    path = args.get("path")
    if path is None:
        return "path is required"
    if not isinstance(path, str) or not path.strip():
        return "path must be a non-empty string"
    if len(path) > 4096:
        return "path exceeds 4096 chars"
    content = args.get("content")
    if content is not None and not isinstance(content, str):
        return "content must be a string"
    if isinstance(content, str) and len(content) > 1024 * 1024:
        return "content exceeds 1MiB"
    # Path Resolution v1: host-owned workspace policy at proposal time —
    # native early rejection instead of a later mysterious OS error
    _, path_err = _paths.resolve_workspace_path(path)
    if path_err is not None:
        return path_err
    return None


def _file_write(args: dict) -> dict:
    path = args.get("path")
    content = args.get("content", "")
    if not path:
        raise DefiniteNoEffect("missing required argument: path")
    if not isinstance(content, str):
        raise DefiniteNoEffect("content must be a string")
    # Path Resolution v1: the HOST resolves the path inside the canonical
    # JARVIS workspace (bare/relative names resolve there; absolute paths
    # are preserved only when already inside it). Defense in depth — the
    # same policy already rejected invalid targets at proposal validation.
    p, err = _paths.resolve_workspace_path(path)
    if err is not None:
        raise DefiniteNoEffect(err)
    # Guard: never write into the V5 repository itself (untrusted-input /
    # self-protection boundary; retained even though the default workspace
    # is outside the repo, in case the workspace is configured inside it).
    repo_root = pathlib.Path(__file__).resolve().parent.parent.resolve()
    if repo_root in p.parents or p == repo_root:
        raise DefiniteNoEffect(f"refusing to write inside the V5 repo: {p}")
    existing = p.read_text(encoding="utf-8") if p.exists() else None
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {
        "path": str(p),
        "bytes_written": len(content.encode("utf-8")),
        "existed": existing is not None,
        "overwrote": existing is not None and existing != content,
    }


register(CapabilitySpec(
    name="file_write",
    idempotency_class=IdempotencyClass.IDEMPOTENT,  # same content -> same result
    requires_confirmation=True,                        # external filesystem effect
    execute=_file_write,
    validate_args=_file_write_validate_args,
    summary="creating or overwriting files",
    arguments_hint='(arguments {"path", "content"}; user files live in the JARVIS workspace)',
))


# ── file_read ───────────────────────────────────────────────────────────────
# A query, not a mutation: zero external side effect. Verification still
# matters — a capability could CLAIM to read content it never read, and the
# fabricate-vs-truth gap is the whole point of the milestone. The capability
# reports what it observed (or not-found); it never claims "correct".

def _file_read_validate_args(args: dict) -> str | None:
    if not isinstance(args, dict):
        return "arguments must be an object"
    allowed = {"path"}
    unknown = sorted(set(args) - allowed)
    if unknown:
        return f"unknown argument field(s): {', '.join(unknown)}"
    path = args.get("path")
    if path is None:
        return "path is required"
    if not isinstance(path, str) or not path.strip():
        return "path must be a non-empty string"
    if len(path) > 4096:
        return "path exceeds 4096 chars"
    # Path Resolution v1: host-owned workspace policy at proposal time
    _, path_err = _paths.resolve_workspace_path(path)
    if path_err is not None:
        return path_err
    return None


def _file_read(args: dict) -> dict:
    path = args.get("path")
    if not path:
        raise DefiniteNoEffect("missing required argument: path")
    if not isinstance(path, str):
        raise DefiniteNoEffect("path must be a string")
    # Path Resolution v1: resolve under the host workspace policy
    p, err = _paths.resolve_workspace_path(path)
    if err is not None:
        raise DefiniteNoEffect(err)
    if not p.exists():
        # definite no-effect: the file provably does not exist, so the Action
        # FAILED rather than UNKNOWN_OUTCOME. The result reports the truth.
        raise DefiniteNoEffect(f"file does not exist: {p}")
    if not p.is_file():
        raise DefiniteNoEffect(f"not a regular file: {p}")
    content = p.read_text(encoding="utf-8")
    return {
        "path": str(p),
        "found": True,
        "content": content,
        "bytes_read": len(content.encode("utf-8")),
    }


register(CapabilitySpec(
    name="file_read",
    idempotency_class=IdempotencyClass.IDEMPOTENT,  # read-only: no effect at all
    requires_confirmation=False,                       # read-only, no mutation
    execute=_file_read,
    validate_args=_file_read_validate_args,
    summary="reading files",
    arguments_hint='(arguments {"path"}; user files live in the JARVIS workspace)',
))


# ── file_delete ─────────────────────────────────────────────────────────────
# A destructive mutation — the confirmation-gate milestone's capability. It
# exists to exercise the Law 32/33 confirmation boundary end-to-end through
# the live Cognition Loop; it is deliberately NOT the start of a filesystem-
# capability expansion. The capability never judges its own success; the
# independent verifier does (see v5/verification.py::verify_file_delete).

def _file_delete_validate_args(args: dict) -> str | None:
    if not isinstance(args, dict):
        return "arguments must be an object"
    allowed = {"path"}
    unknown = sorted(set(args) - allowed)
    if unknown:
        return f"unknown argument field(s): {', '.join(unknown)}"
    path = args.get("path")
    if path is None:
        return "path is required"
    if not isinstance(path, str) or not path.strip():
        return "path must be a non-empty string"
    if len(path) > 4096:
        return "path exceeds 4096 chars"
    # Path Resolution v1: host-owned workspace policy at proposal time
    _, path_err = _paths.resolve_workspace_path(path)
    if path_err is not None:
        return path_err
    return None


def _file_delete(args: dict) -> dict:
    path = args.get("path")
    if not path:
        raise DefiniteNoEffect("missing required argument: path")
    if not isinstance(path, str):
        raise DefiniteNoEffect("path must be a string")
    # Path Resolution v1: resolve under the host workspace policy
    p, err = _paths.resolve_workspace_path(path)
    if err is not None:
        raise DefiniteNoEffect(err)
    # Guard: never delete inside the V5 repository itself (the same
    # self-protection boundary file_write enforces).
    repo_root = pathlib.Path(__file__).resolve().parent.parent.resolve()
    if repo_root in p.parents or p == repo_root:
        raise DefiniteNoEffect(f"refusing to delete inside the V5 repo: {p}")
    if not p.exists():
        raise DefiniteNoEffect(f"file does not exist: {p}")
    if not p.is_file():
        raise DefiniteNoEffect(f"not a regular file: {p}")
    try:
        p.unlink()
    except FileNotFoundError:
        # vanished between the exists() check and unlink — the capability can
        # prove IT effected nothing (and the requested end state holds)
        raise DefiniteNoEffect(f"file already gone: {p}")
    return {
        "path": str(p),
        "existed": True,
        "deleted": True,
    }


register(CapabilitySpec(
    name="file_delete",
    idempotency_class=IdempotencyClass.IDEMPOTENT,  # same request -> same end state (absent)
    requires_confirmation=True,   # destructive external effect — Law 21/32/33 gate
    execute=_file_delete,
    validate_args=_file_delete_validate_args,
    summary="deleting files",
    arguments_hint='(arguments {"path"}; user files live in the JARVIS workspace)',
))


# ── codebase_query: bounded JARVIS source investigation (Investigation
#    Capability v1) ─────────────────────────────────────────────────────────
# A pure-read capability so investigation directives ("Find out what the
# implementation does when an action ends in UNKNOWN_OUTCOME") become genuine
# Work instead of conversational dead ends. It is NOT a shell: exactly two
# operations, bounded by the source-root containment in v5/paths.py, with
# hard caps on bytes read, files scanned, and matches returned. The shared
# private helpers below are the single measurement instrument used by BOTH
# the capability and its registered verifier — the verifier recomputes the
# ground truth itself and compares it against the Observation's claim, never
# trusting the capability's self-report.

_READ_CAP_BYTES = 64 * 1024          # per-file read cap
_SEARCH_MAX_FILES = 200             # files scanned per search
_SEARCH_MAX_MATCHES = 50            # match records returned
_SEARCH_LINE_CAP = 200              # chars kept per matched line
_SEARCH_MAX_FILE_BYTES = 1024 * 1024  # skip files larger than this
_PATTERN_CAP = 200                  # regex pattern length cap


def _codebase_query_validate_args(args: dict) -> str | None:
    """Declared argument schema (runs at proposal time AND Action acceptance,
    like every registered capability). Operation-typed: read_file takes a
    source path (validated through the source-root resolver), search takes a
    bounded regex plus optional file_glob."""
    if not isinstance(args, dict):
        return "arguments must be an object"
    allowed = {"operation", "path", "pattern", "file_glob"}
    unknown = sorted(set(args) - allowed)
    if unknown:
        return f"unknown argument field(s): {', '.join(unknown)}"
    op = args.get("operation")
    if op not in ("read_file", "search"):
        return 'operation must be "read_file" or "search"'
    if op == "read_file":
        if "pattern" in args:
            return 'pattern is only valid for operation "search"'
        path = args.get("path")
        if path is None:
            return 'path is required for operation "read_file"'
        _, err = _paths.resolve_source_path(path)
        if err is not None:
            return err
    else:
        if "path" in args:
            return 'path is only valid for operation "read_file"'
        pattern = args.get("pattern")
        if pattern is None:
            return 'pattern is required for operation "search"'
        if not isinstance(pattern, str) or not pattern.strip():
            return "pattern must be a non-empty string"
        if "\x00" in pattern:
            return "pattern contains NUL bytes"
        if len(pattern) > _PATTERN_CAP:
            return f"pattern exceeds {_PATTERN_CAP} chars"
        try:
            re.compile(pattern)
        except re.error as e:
            return f"pattern is not a valid regex: {e}"
        glob = args.get("file_glob", "*.py")
        if not isinstance(glob, str) or not glob.strip():
            return "file_glob must be a non-empty string"
        if len(glob) > 128:
            return "file_glob exceeds 128 chars"
    return None


def _source_read(p: pathlib.Path) -> dict:
    """Bounded, deterministic read of one resolved source file. Shared
    measurement instrument: used by the capability AND independently by
    verify_codebase_query."""
    root = _paths.source_root()
    rel = str(p.relative_to(root))
    if not p.exists():
        raise DefiniteNoEffect(f"file does not exist in the JARVIS source tree: {rel}")
    if not p.is_file():
        raise DefiniteNoEffect(f"not a regular file: {rel}")
    if p.stat().st_size > _SEARCH_MAX_FILE_BYTES:
        raise DefiniteNoEffect(f"file exceeds the source-read size cap: {rel}")
    try:
        raw = p.read_bytes()
    except OSError as e:
        raise DefiniteNoEffect(f"file could not be read: {rel} ({e})")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise DefiniteNoEffect(f"file is not UTF-8 text: {rel}")
    truncated = len(raw) > _READ_CAP_BYTES
    content = text[:_READ_CAP_BYTES] if truncated else text
    return {
        "operation": "read_file",
        "path": str(p),
        "rel_path": rel,
        "content": content,
        "truncated": truncated,
        "bytes_read": len(raw),
    }


def _source_search(pattern: str, file_glob: str) -> dict:
    """Bounded, deterministic regex search over the source tree. Shared
    measurement instrument for the capability and its verifier. Symlink
    escapes are impossible: os.walk(followlinks=False) plus a per-file
    realpath containment check."""
    root = _paths.source_root()
    rx = re.compile(pattern)
    matches: list[dict] = []
    files_scanned = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        if files_scanned >= _SEARCH_MAX_FILES:
            truncated = True
            break
        for fname in sorted(filenames):
            if files_scanned >= _SEARCH_MAX_FILES:
                truncated = True
                break
            if not fnmatch.fnmatch(fname, file_glob):
                continue
            full = pathlib.Path(dirpath) / fname
            try:
                resolved = full.resolve()
                resolved.relative_to(root)   # symlink escape -> skip, not crash
            except (OSError, ValueError):
                continue
            try:
                if resolved.stat().st_size > _SEARCH_MAX_FILE_BYTES:
                    continue
                text = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            files_scanned += 1
            for line_no, line in enumerate(text.splitlines(), start=1):
                if len(matches) >= _SEARCH_MAX_MATCHES:
                    truncated = True
                    break
                if rx.search(line):
                    matches.append({
                        "file": str(resolved.relative_to(root)),
                        "line_no": line_no,
                        "line": line[:_SEARCH_LINE_CAP],
                    })
            if len(matches) >= _SEARCH_MAX_MATCHES:
                break
    return {
        "operation": "search",
        "pattern": pattern,
        "file_glob": file_glob,
        "matches": matches,
        "files_scanned": files_scanned,
        "truncated": truncated,
    }


def _codebase_query(args: dict) -> dict:
    op = args.get("operation")
    if op == "read_file":
        p, err = _paths.resolve_source_path(args.get("path"))
        if err is not None:
            raise DefiniteNoEffect(err)
        return _source_read(p)
    pattern = args.get("pattern")
    if not isinstance(pattern, str):
        raise DefiniteNoEffect("pattern must be a string")
    return _source_search(pattern, args.get("file_glob", "*.py"))


register(CapabilitySpec(
    name="codebase_query",
    idempotency_class=IdempotencyClass.IDEMPOTENT,  # pure read, no effect
    requires_confirmation=False,                      # read-only, no mutation
    execute=_codebase_query,
    validate_args=_codebase_query_validate_args,
    summary="searching and reading the JARVIS source code to investigate its "
            "implementation, architecture, or behavior",
    arguments_hint='(arguments {"operation": "read_file"|"search", "path" for '
                   'read_file, "pattern" (+ optional "file_glob") for search; '
                   "reads only inside the JARVIS source tree)",
))


# ── the executor: the single place that runs capabilities ──────────────────

def execute_action(store: Store, action, capability: CapabilitySpec,
                   safety_check=None, plan_id_for_validation: str | None = None):
    """Run one action through the full contracted pipeline:

        begin_executing (durable, Law 8)   <- external effect only AFTER this
        capability.execute(arguments)      <- external effect (crash point 3)
        mark_observed (one transaction, crash points 4/5)

    Returns (Ok(payload), None) on success or (None, Rejected) on any
    contracted rejection. SimulatedCrash propagates to the caller (process
    death is not an error path). DefiniteNoEffect -> FAILED; any other
    exception -> UNKNOWN_OUTCOME + OPEN obligation (Law 9/11).
    """
    from v5.models import Ok, Rejected
    from v5 import execution as ex

    result = ex.begin_executing(
        store, action.id, action.revision,
        expected_plan_id=plan_id_for_validation, safety_check=safety_check,
    )
    if isinstance(result, Rejected):
        return None, result
    started = result.value  # the EXECUTING action

    store.crash_point("after_executing_before_external")  # crash point 2 handled
    # (point 2 is between commit and the external call; the commit is done)
    try:
        raw = capability.execute(dict(started.arguments))
    except Exception as e:  # crash point 3 territory: the external call itself
        if isinstance(e, DefiniteNoEffect):
            r = ex.mark_failed(store, started.id, started.revision, e.reason,
                               definite_no_effect=True)
            if isinstance(r, Rejected):
                return None, r
            return Ok({"status": "FAILED", "reason": e.reason}), None
        # not SimulatedCrash and not definitely-safe: uncertain outcome.
        # NOTE: SimulatedCrash must propagate (process death), which the
        # generic handler below deliberately re-raises first.
        from v5.store import SimulatedCrash
        if isinstance(e, SimulatedCrash):
            raise
        r = ex.mark_unknown_outcome(
            store, started.id, started.revision,
            unknown_reason=f"capability raised {type(e).__name__}: {e}",
            possible_external_effect="external effect may have partially occurred",
        )
        if isinstance(r, Rejected):
            return None, r
        return Ok({"status": "UNKNOWN_OUTCOME", "obligation_id": r.value["obligation_id"]}), None

    obs = ex.mark_observed(
        store, started.id, started.revision, raw_result=raw,
        execution_source=capability.name,
        expected_plan_id=plan_id_for_validation,
    )
    if isinstance(obs, Rejected):
        return None, obs
    return Ok({"status": "OBSERVED", "observation_id": obs.value["observation_id"],
               "raw": raw}), None
