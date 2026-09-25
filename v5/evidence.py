"""Evidence, Claim, and Verification pipeline.

Constitutional invariants implemented here:
  * Law 23 (Provenance): Evidence carries acquisition_method AND status;
    they are orthogonal — a user statement is never CONFIRMED_SOURCE.
  * Law 24 (Evidence Immutability): historical Evidence is never rewritten;
    there is no update path at all (insert-only).
  * Law 25 (Inference Separation): only the execution/capability pipeline may
    create execution-derived Evidence (CONFIRMED_RUNTIME). The cognition
    surface (`create_inference_evidence`) can only produce typed INFERENCE
    evidence referencing existing Evidence/Claims. This module enforces that
    boundary at the API layer: there is no public function through which a
    caller can write CONFIRMED_RUNTIME evidence except the execution
    pipeline's `record_runtime_evidence`, which requires a real Observation.
  * Law 27 (Confidence Justification): a Claim citing only
    INFERENCE/UNKNOWN evidence cannot be HIGH confidence; HIGH requires at
    least one CONFIRMED_* evidence cited.
  * Claims are versioned: substantive change creates a new version row; the
    prior version is preserved verbatim.
"""
from __future__ import annotations

from v5.enums import (
    ClaimConfidence,
    EvidenceStatus,
    VerificationResult,
)
from v5.ids import new_id
from v5.models import (
    Claim,
    Evidence,
    Ok,
    Rejected,
    Result,
    Verification,
    R_CONFIDENCE,
    R_EVIDENCE_STATUS,
    R_NOT_FOUND,
    R_STALE_REVISION,
)
from v5.store import Store, iso, jdump, jload, utcnow


# ── row mapping ──────────────────────────────────────────────────────────────

def _row_to_evidence(r) -> Evidence:
    return Evidence(
        id=r["id"], status=EvidenceStatus(r["status"]),
        acquisition_method=r["acquisition_method"], source=r["source"],
        relevance_to=r["relevance_to"], timestamp=r["timestamp"],
        content=jload(r["content"]),
        origin_observation_id=r["origin_observation_id"] if "origin_observation_id" in r.keys() else None,
    )


def load_evidence(store: Store, evidence_id: str) -> Evidence | None:
    r = store.read().execute("SELECT * FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
    return _row_to_evidence(r) if r else None


def load_claim(store: Store, claim_id: str, version: int | None = None) -> Claim | None:
    conn = store.read()
    if version is None:
        row = conn.execute(
            "SELECT * FROM claims WHERE id = ? ORDER BY version DESC LIMIT 1", (claim_id,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM claims WHERE id = ? AND version = ?", (claim_id, version)
        ).fetchone()
    if not row:
        return None
    return Claim(
        id=row["id"], version=row["version"], asserts=row["asserts"],
        based_on=jload(row["based_on"], []),
        confidence=ClaimConfidence(row["confidence"]), made_by=row["made_by"],
    )


def load_verification(store: Store, verification_id: str) -> Verification | None:
    r = store.read().execute(
        "SELECT * FROM verifications WHERE id = ?", (verification_id,)
    ).fetchone()
    if not r:
        return None
    return Verification(
        id=r["id"], verifies=r["verifies"], method=r["method"],
        independence_level=r["independence_level"], result=VerificationResult(r["result"]),
        timestamp=r["timestamp"],
    )


# ── Evidence creation ────────────────────────────────────────────────────────

def _insert_evidence(conn, status: EvidenceStatus, acquisition_method: str,
                     source: str, relevance_to: str, content,
                     origin_observation_id: str | None = None) -> Evidence:
    ev = Evidence(
        id=new_id("evidence"), status=status, acquisition_method=acquisition_method,
        source=source, relevance_to=relevance_to, timestamp=iso(utcnow()),
        content=content, origin_observation_id=origin_observation_id,
    )
    conn.execute(
        "INSERT INTO evidence (id, status, acquisition_method, source, relevance_to, timestamp, content, origin_observation_id) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (ev.id, status.value, acquisition_method, source, relevance_to,
         ev.timestamp, jdump(content), origin_observation_id),
    )
    return ev


def record_runtime_evidence(store: Store, observation_id: str, source: str,
                            relevance_to: str, content) -> Result:
    """Execution-derived Evidence (Law 25): ONLY the execution pipeline may
    call this, and ONLY with a real Observation backing it. The observation
    requirement is enforced here — cognition cannot fabricate runtime evidence
    because it has no Observation to cite."""
    with store.write() as conn:
        obs = conn.execute(
            "SELECT * FROM observations WHERE id = ?", (observation_id,)
        ).fetchone()
        if obs is None:
            return Rejected(R_NOT_FOUND, f"observation {observation_id} does not exist", None)
        ev = _insert_evidence(
            conn, EvidenceStatus.CONFIRMED_RUNTIME,
            acquisition_method="capability_execution", source=source,
            relevance_to=relevance_to, content=content,
            origin_observation_id=observation_id,  # Law 23 provenance — this is
            # the link obligation resolution walks to prove the chain
            # Obligation→Action→Observation→Evidence.
        )
        store.audit(conn, ev.id, "evidence_recorded", None, EvidenceStatus.CONFIRMED_RUNTIME.value)
        return Ok(ev)


def create_inference_evidence(store: Store, made_by: str, content,
                              relevance_to: str,
                              based_on: list[str] | None = None) -> Result:
    """The ONLY evidence path available to cognition (Law 25): typed INFERENCE
    evidence. Status is forced — a caller cannot request CONFIRMED_* here.
    based_on must reference existing Evidence or Claims (inference references;
    it never masquerades as observation)."""
    with store.write() as conn:
        for ref in based_on or []:
            if conn.execute("SELECT 1 FROM evidence WHERE id = ?", (ref,)).fetchone() is None and \
               conn.execute("SELECT 1 FROM claims WHERE id = ?", (ref,)).fetchone() is None:
                return Rejected(R_NOT_FOUND, f"based_on reference {ref} not found", None)
        ev = _insert_evidence(
            conn, EvidenceStatus.INFERENCE,
            acquisition_method=f"inference:{made_by}", source=made_by,
            relevance_to=relevance_to, content=content,
        )
        store.audit(conn, ev.id, "evidence_recorded", None, EvidenceStatus.INFERENCE.value)
        return Ok(ev)


def record_user_evidence(store: Store, source: str, content,
                        relevance_to: str) -> Result:
    """User-attested evidence. Law 23: a user statement is NEVER
    CONFIRMED_SOURCE — provenance status and acquisition method are orthogonal.
    User statements are typed UNKNOWN (attested but unverified) unless a
    capability later confirms them at runtime."""
    with store.write() as conn:
        ev = _insert_evidence(
            conn, EvidenceStatus.UNKNOWN,
            acquisition_method="user_statement", source=source,
            relevance_to=relevance_to, content=content,
        )
        store.audit(conn, ev.id, "evidence_recorded", None, EvidenceStatus.UNKNOWN.value)
        return Ok(ev)


# ── Claims (versioned — Law: substantive change = new version, not edit) ────

def _confidence_justified(conn, based_on: list[str], confidence: ClaimConfidence) -> bool:
    """Law 27 (structural portion): HIGH requires at least one CONFIRMED_*
    evidence among the cited records; a claim resting only on
    INFERENCE/UNKNOWN evidence cannot be HIGH. (Semantic relevance of each
    cited item to the proposition is the claimant's responsibility — a
    mechanical relevance measure is not specified by the contract; see
    DECISIONS.md.)"""
    if confidence is not ClaimConfidence.HIGH:
        return True
    for ref in based_on:
        row = conn.execute("SELECT status FROM evidence WHERE id = ?", (ref,)).fetchone()
        if row and row["status"] in ("CONFIRMED_SOURCE", "CONFIRMED_RUNTIME"):
            return True
    return False


def create_claim(store: Store, asserts: str, based_on: list[str],
                 made_by: str, confidence: ClaimConfidence) -> Result:
    with store.write() as conn:
        return _create_claim_locked(store, conn, asserts, based_on, made_by, confidence)


def _create_claim_locked(store: Store, conn, asserts: str, based_on: list[str],
                         made_by: str, confidence: ClaimConfidence) -> Result:
    """create_claim's validation+insert+audit, expressed against an already-
    open write transaction. The public wrapper and this core share the exact
    same checks; Work Service's atomic plan acceptance composes this inside
    its own transaction (no partial proposal state, no parallel authority —
    the checks are the same code)."""
    for ref in based_on:
        if conn.execute("SELECT 1 FROM evidence WHERE id = ?", (ref,)).fetchone() is None:
            return Rejected(R_NOT_FOUND, f"cited evidence {ref} not found", None)
    if not _confidence_justified(conn, based_on, confidence):
        return Rejected(R_CONFIDENCE,
                        "HIGH confidence requires at least one CONFIRMED_* evidence (Law 27)", None)
    claim_id = new_id("claim")
    conn.execute(
        "INSERT INTO claims (id, version, asserts, based_on, confidence, made_by) VALUES (?,?,?,?,?,?)",
        (claim_id, 1, asserts, jdump(based_on), confidence.value, made_by),
    )
    store.audit(conn, claim_id, "claim_created", None, confidence.value)
    return Ok(Claim(id=claim_id, version=1, asserts=asserts, based_on=based_on,
                    confidence=confidence, made_by=made_by))


def revise_claim(store: Store, claim_id: str, asserts: str, based_on: list[str],
                 made_by: str, confidence: ClaimConfidence) -> Result:
    """Substantive change = a NEW VERSION of the claim; the prior version is
    preserved verbatim (never rewritten)."""
    with store.write() as conn:
        row = conn.execute(
            "SELECT MAX(version) AS v FROM claims WHERE id = ?", (claim_id,)
        ).fetchone()
        if row is None or row["v"] is None:
            return Rejected(R_NOT_FOUND, f"claim {claim_id} not found", None)
        for ref in based_on:
            if conn.execute("SELECT 1 FROM evidence WHERE id = ?", (ref,)).fetchone() is None:
                return Rejected(R_NOT_FOUND, f"cited evidence {ref} not found", None)
        if not _confidence_justified(conn, based_on, confidence):
            return Rejected(R_CONFIDENCE,
                            "HIGH confidence requires at least one CONFIRMED_* evidence (Law 27)", None)
        version = row["v"] + 1
        conn.execute(
            "INSERT INTO claims (id, version, asserts, based_on, confidence, made_by) VALUES (?,?,?,?,?,?)",
            (claim_id, version, asserts, jdump(based_on), confidence.value, made_by),
        )
        store.audit(conn, claim_id, "claim_revised", None, confidence.value)
        return Ok(Claim(id=claim_id, version=version, asserts=asserts, based_on=based_on,
                        confidence=confidence, made_by=made_by))


def claim_versions(store: Store, claim_id: str) -> list[Claim]:
    conn = store.read()
    rows = conn.execute(
        "SELECT * FROM claims WHERE id = ? ORDER BY version", (claim_id,)
    ).fetchall()
    return [
        Claim(id=r["id"], version=r["version"], asserts=r["asserts"],
              based_on=jload(r["based_on"], []), confidence=ClaimConfidence(r["confidence"]),
              made_by=r["made_by"])
        for r in rows
    ]


# ── Verification ─────────────────────────────────────────────────────────────

def create_verification(store: Store, verifies_claim_id: str, method: str,
                        independence_level: str) -> Result:
    if independence_level not in (
        "deterministic", "direct_observation", "independent_capability",
        "derived_computation", "llm_evaluation", "self_report",
    ):
        return Rejected("INVALID_INDEPENDENCE", f"independence_level {independence_level}", None)
    with store.write() as conn:
        return _create_verification_locked(store, conn, verifies_claim_id, method, independence_level)


def _create_verification_locked(store: Store, conn, verifies_claim_id: str,
                                method: str, independence_level: str) -> Result:
    """create_verification's checks+insert+audit against an open write
    transaction (composed by Work Service's atomic plan acceptance)."""
    if independence_level not in (
        "deterministic", "direct_observation", "independent_capability",
        "derived_computation", "llm_evaluation", "self_report",
    ):
        return Rejected("INVALID_INDEPENDENCE", f"independence_level {independence_level}", None)
    if conn.execute("SELECT 1 FROM claims WHERE id = ?", (verifies_claim_id,)).fetchone() is None:
        return Rejected(R_NOT_FOUND, f"claim {verifies_claim_id} not found", None)
    v = Verification(
        id=new_id("verification"), verifies=verifies_claim_id, method=method,
        independence_level=independence_level, result=VerificationResult.PENDING,
        timestamp=iso(utcnow()),
    )
    conn.execute(
        "INSERT INTO verifications (id, verifies, method, independence_level, result, timestamp) "
        "VALUES (?,?,?,?,?,?)",
        (v.id, v.verifies, v.method, v.independence_level, v.result.value, v.timestamp),
    )
    store.audit(conn, v.id, "verification_created", None, VerificationResult.PENDING.value)
    return Ok(v)


def run_verification(store: Store, verification_id: str, evaluator) -> Result:
    """Evaluate a verification. The RUNNING state is durably committed FIRST
    (its own transaction), then the evaluation runs and the result commits.
    Crash point #6: if the process dies mid-run the persisted result is PENDING
    or RUNNING — the owning Claim is never treated as established (completion
    requires PASS, Law 16). Evaluator: (claim_row, evidence_rows) -> bool|None
    (None -> INCONCLUSIVE)."""
    with store.write() as conn:
        r = conn.execute("SELECT * FROM verifications WHERE id = ?", (verification_id,)).fetchone()
        if r is None:
            return Rejected(R_NOT_FOUND, f"verification {verification_id}", None)
        if r["result"] not in (VerificationResult.PENDING.value, VerificationResult.RUNNING.value):
            return Rejected("ALREADY_FINAL", f"result={r['result']}", None)
        conn.execute(
            "UPDATE verifications SET result = 'RUNNING' WHERE id = ?", (verification_id,)
        )
        store.audit(conn, verification_id, "verification_running", "PENDING", "RUNNING")

    store.crash_point("verification_midrun")  # crash #6: RUNNING is committed, result is not

    with store.write() as conn:
        r = conn.execute("SELECT * FROM verifications WHERE id = ?", (verification_id,)).fetchone()
        claim = conn.execute(
            "SELECT * FROM claims WHERE id = ? ORDER BY version DESC LIMIT 1", (r["verifies"],)
        ).fetchone()
        evidence_rows = []
        for ref in jload(claim["based_on"], []):
            er = conn.execute("SELECT * FROM evidence WHERE id = ?", (ref,)).fetchone()
            if er is not None:
                evidence_rows.append(er)
        try:
            outcome = evaluator(claim, evidence_rows)
        except Exception as e:
            conn.execute(
                "UPDATE verifications SET result = 'INCONCLUSIVE' WHERE id = ?", (verification_id,)
            )
            store.audit(conn, verification_id, "verification_inconclusive", "RUNNING", "INCONCLUSIVE",
                        reason=f"evaluator error: {e}")
            return Ok({"result": VerificationResult.INCONCLUSIVE})
        if outcome is None:
            result = VerificationResult.INCONCLUSIVE
        else:
            result = VerificationResult.PASS if outcome else VerificationResult.FAIL
        conn.execute("UPDATE verifications SET result = ? WHERE id = ?", (result.value, verification_id))
        store.audit(conn, verification_id, "verification_finished", "RUNNING", result.value)
        return Ok({"result": result})
