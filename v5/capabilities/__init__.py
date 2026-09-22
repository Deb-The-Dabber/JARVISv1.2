"""Capability registry + the file_write capability (the vertical slice).

Capabilities are the only authority that produces runtime effects and
execution-derived Evidence/Observations (Law 29). Each capability declares
its idempotency class (Law 11) and whether confirmation is required (Law 21/
32/33 — risk-classed, confirmation-bound).
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Callable

from v5.enums import IdempotencyClass


@dataclass
class CapabilitySpec:
    name: str
    idempotency_class: IdempotencyClass
    requires_confirmation: bool
    execute: Callable[[dict], dict]


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

def _file_write(args: dict) -> dict:
    path = args.get("path")
    content = args.get("content", "")
    if not path:
        raise DefiniteNoEffect("missing required argument: path")
    if not isinstance(content, str):
        raise DefiniteNoEffect("content must be a string")
    p = pathlib.Path(path).expanduser()
    if not p.is_absolute():
        raise DefiniteNoEffect("path must be absolute")
    # Guard: never write into the V5 repository itself (untrusted-input /
    # self-protection boundary; tests write under tmp_path).
    p = p.resolve()
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
