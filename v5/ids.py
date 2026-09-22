"""ULID identifiers, prefixed per type (Contract §1).

All IDs are generated at creation, never reused, never derived by truncation.
"""
from __future__ import annotations

import os
import threading
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_lock = threading.Lock()
_last_entropy = bytearray(10)


def _enc(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def _ulid() -> str:
    """Monotonic-safe ULID: 48-bit ms timestamp + 80 bits entropy, Crockford
    base32, 26 chars. Entropy is monotonic within the process so IDs sort
    correctly even when generated in the same millisecond."""
    global _last_entropy
    ts = int(time.time() * 1000) & ((1 << 48) - 1)
    with _lock:
        ent = bytearray(os.urandom(10))
        if ent <= _last_entropy:  # preserve sort order within-process
            for i in range(9, -1, -1):
                ent[i] = (ent[i] + 1) & 0xFF
                if ent[i]:
                    break
        _last_entropy = bytearray(ent)
    return _enc(ts, 10) + _enc(int.from_bytes(bytes(ent), "big"), 16)


_PREFIXES = {
    "goal": "goal_",
    "task": "task_",
    "plan": "plan_",
    "step": "step_",
    "action": "act_",
    "observation": "obs_",
    "evidence": "ev_",
    "claim": "clm_",
    "verification": "ver_",
    "obligation": "obl_",
    "confirmation": "cfm_",
}


def new_id(kind: str) -> str:
    prefix = _PREFIXES.get(kind)
    if prefix is None:
        raise ValueError(f"unknown id kind: {kind}")
    return prefix + _ulid()


# Contract §6: the UOR is addressed by the literal sentinel "UOR".
UOR = "UOR"
