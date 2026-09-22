<arg_value># DECISIONS.md — Contract Interpretations

Every place the Implementation Contract left a genuine gap is documented here.
Each entry cites the law/section it interprets, the decision made, and the
test that encodes it. No new semantics were invented; the smallest practical
implementation consistent with the frozen Constitution was chosen.

---

## 1. Obligation.revision (Contract §3 vs §6)

**Gap**: The Obligation schema (§3) lists no `revision` field, but
`abandon_obligation(obligation_id, authorized_by, reason, expected_revision)`
(§6) and race #9 (§9, abandon vs resolve) require a revision CAS.

**Decision**: Obligations carry a `revision` column starting at 0. Transfer,
abandon, and resolve all bump it; `abandon_obligation` and `resolve_obligation`
CAS on it. Transfer checks ownership (per §6) rather than revision, since §6
specifies `expected_owner` for transfer.

**Test**: `test_concurrency.py::TestRace9AbandonVsResolve`.

## 2. Law 20 subtree scope for obligations

**Gap**: "an object whose subtree holds an OPEN obligation" — which objects
can own obligations? §3 fixes `Obligation.owner` to "Task id OR the literal
sentinel 'UOR'". Only Tasks (and the UOR) can canonically own obligations.

**Decision**: Terminal transitions on **Tasks** transfer obligations they
own; terminal transitions on **Goals** transfer obligations owned by any of
the Goal's tasks. Terminal transitions on Plans/Steps/Actions do NOT transfer
Task-owned obligations — the owning Task is still live and remains the
durable address (Law 17); transfer fires when the owning Task or ancestor Goal
terminates. This is the only reading consistent with §3's owner field.

**Test**: `test_invariants.py::TestObligationLaws17to22::test_law20_terminal_transfer_atomic`.

## 3. supersede_plan — the atomic plan-replacement operation (Contract §4 + crash #7)

**Gap**: `activate_plan`'s preconditions (§4) require the current active plan
to be "already SUPERSEDED/CANCELLED/FAILED" — but no operation marks it so.
Crash #7 (§8) requires that the SUPERSEDED write and the active_plan_id write
be unobservable as two steps.

**Decision**: `supersede_plan(task_id, old_plan_id, new_plan_id, expected_rev)`
performs both writes in ONE transaction (old → SUPERSEDED with
`superseded_by=new`, Task.active_plan_id → new), with the same guards §4
specifies for activate_plan. `activate_plan` remains the literal §4 operation
for the no-active / dead-active case. Crash #7 arms between the two writes
inside the transaction and proves neither lands.

**Test**: `test_crash.py::TestCrash7PlanSupersedeAtomicity`.

## 4. Required-Verification binding (Contract §5)

**Gap**: `complete_object` requires "every Verification required by the
completion criterion has result == PASS" — but the contract does not specify
how a criterion names its required Verifications.

**Decision**: A `required_verifications(object_id, verification_id)` table
persisted by the Work Service (`bind_required_verification`). `complete_object`
checks every bound verification is PASS. With zero bindings the check is
vacuously satisfied (the criterion named none).

**Test**: `test_e2e_file_write.py` (PENDING-bound and FAIL-bound verifications
block; PASS-bound completes).

## 5. evaluate_completion over children (Contract §5)

**Gap**: The signature takes `children_status` but does not define which
children count, or what "satisfying" means per status.

**Decision**: For a **Task**, children = steps of its **active plan**
(CANCELLED steps excluded — their obligations transferred at their own
terminal). For a **Goal**, children = its tasks (CANCELLED/ARCHIVED tasks
excluded, same reasoning). Satisfying statuses: Step ∈ {COMPLETED, SKIPPED};
Task ∈ {COMPLETED}. SKIPPED counts as satisfying (deliberate, Work-authorized
skips). `ALL_REQUIRED` over an **empty** children list returns **False** —
an empty set cannot demonstrate achievement (a goal whose only task was
cancelled was not achieved). `CRITERION` with an unregistered evaluator name
returns False (fail-safe). `N_OF_M` counts satisfying statuses against `n`.

**Tests**: `test_e2e_file_write.py`, `test_crash.py::TestCrash8` (policy gate),
invariant tests for Law 16.

## 6. Law 26 (Contradiction) — CONFLICTED classification

**Gap**: Law 26 says a claim built over a contradiction is "CONFLICTED until
resolved", but the contracted `ClaimConfidence` enum has no CONFLICTED value.

**Decision**: Mechanical contradiction *detection* is not specified anywhere
in the contract; the foundation implements the testable portion: conflicting
evidence rows are preserved verbatim (coexistence + immutability, proven),
and claims citing only INFERENCE/UNKNOWN evidence cannot reach HIGH
confidence (Law 27 gate). Semantic CONFLICTED-classification is recorded as
integration-only (requires an evaluator that understands proposition
semantics — deferred to the verification layer above the foundation).

**Test**: `test_invariants.py::test_law26_contradiction_preserved`.

## 7. Repair of non-revisioned objects (Contract §7)

**Gap**: `repair_object(obj_id, expected_revision, ...)` — but Verification
rows have no revision in the §3 schema.

**Decision**: Repair CASes on revision only for tables that carry one; for
non-revisioned objects (e.g., verifications) the revision check is vacuous
and the Law 29 fabrication rules (§7 precondition 2, extended to PASS
verifications) do the guarding. Documented as `has_revision` in repair.py.

**Test**: `test_repair.py::TestRepairFabrication` (PASS-from-FAIL rejected).

## 8. Integrity freeze on execution objects (Law 5)

**Gap**: §3 lists `integrity` on Goal and Task only; crash #5 requires
freezing Actions (Observation present, Action not OBSERVED).

**Decision**: The `actions` table carries an `integrity` column (storage
field on the Action row; the dataclass exposes it as a plain string). All
execution-path mutations reject FROZEN actions. `freeze_object` in work.py
freezes Goal/Task/Plan/Step; `freeze_action` in execution.py freezes Actions.
Observations are never deleted or merged (both representations preserved).

**Test**: `test_repair.py`, `test_crash.py::TestCrash5`.

## 9. Cognition boundary enforcement (Laws 12, 25, 29, 30)

**Gap**: "enforced at the interface layer, not just by convention" — in a
single Python process, true capability isolation is impossible.

**Decision**: Module-boundary enforcement: `repair.py` is not imported by
work/execution/evidence/capabilities; the `HumanAuthorization` token can
only be minted by `repair.authorize()`. `record_runtime_evidence` requires a
real Observation row (structural enforcement, not convention). Tests assert
the negative: `not hasattr(work, "repair_object")` etc. This is the honest
limit documented rather than pretended away.

**Test**: `test_repair.py::TestRepairAuthorization`.

## 10. Recovery has no special authority (Law 30)

**Decision**: `recovery.recover()` uses only the ordinary contracted
transition `EXECUTING → UNKNOWN_OUTCOME` plus the normal obligation-creation
path. It never writes OBSERVED, never fabricates, never bypasses revision
checks (it re-reads each action's current revision). It freezes
INTEGRITY_VIOLATION objects instead of guessing. It is idempotent.

**Test**: `test_repair.py::TestRecoveryNoSpecialPowers`.

## 11. Retry (Contract §9 row 7)

**Gap**: `retry_action` is not given a signature.

**Decision**: `retry_action(action_id, trigger, expected_task_revision)` —
one transaction: validate the action is retryable (UNKNOWN_OUTCOME/FAILED),
CAS the Task's revision, increment `retry_budget.attempts_used`, create a NEW
Action (new identity, `retry_of` = old id, status PENDING, same
capability/arguments/idempotency class). The loser of a concurrent retry
gets STALE_REVISION and creates nothing; the budget decrements exactly once.

**Test**: `test_concurrency.py::TestRace7RetryRace`.

## 12. resolve_obligation justification (Contract §9 row 9)

**Gap**: `resolve_obligation(O, verification_result)` signature in §9; no
contracted function definition.

**Decision**: `resolve_obligation(obligation_id, verification_id,
expected_revision, resolved_by)` requires a Verification whose result is
PASS — resolution must be justified. Disposition OPEN → RESOLVED + history
event; the owner does not change (a UOR-owned obligation stays in the UOR as
history).

**Test**: `test_e2e_file_write.py::test_unknown_outcome_flows_to_uor_on_completion`.

## 13. Confirmation binding representation (Law 32)

**Gap**: The contract does not define the storage of confirmations.

**Decision**: A `confirmations` table keyed by (id, action_id,
action_revision, capability, arguments, confirmed_at). `make_confirmation_gate`
builds the safety_check that `begin_executing` evaluates INSIDE its
transaction. The binding is keyed by exact action identity — reuse across a
different action is structurally impossible, and a material change to
arguments invalidates the match.

**Test**: `test_invariants.py::test_law32_confirmation_binding`.

## 14. Step lifecycle and plan-step navigation

**Gap**: Step transitions are not specified in the contract beyond the enum.

**Decision**: The explicit STEP_TRANSITIONS table (PENDING → READY →
EXECUTING → COMPLETED/FAILED; BLOCKED and CANCELLED/SKIPPED from
appropriate states; SKIPPED reachable from PENDING/READY/BLOCKED as a
deliberate Work-authorized act). Steps transition via the same
`transition_object` machinery.

**Test**: `test_invariants.py::TestWorkLaws12to16` + E2E.

## 15. Dependency graph and steps (Law 13)

**Gap**: §3 puts `dependency_graph` on Plan AND `depends_on` on Step — two
representations of the same edges.

**Decision**: `dependency_graph` on the Plan row is the authoritative
committed graph (it is what the cycle check runs against, atomically).
`Step.depends_on` is a denormalized convenience copy updated in the same
transaction. `add_dependency` CASes on the plan revision and cycle-checks
the graph *as it will be* after the edge is added.

**Test**: `test_concurrency.py::TestRace8DependencyRace`,
`test_invariants.py::test_law13_dependencies_acyclic`.

## 16. file_write capability guard

**Decision**: file_write refuses to write inside the V5 repository itself
(DefiniteNoEffect → FAILED with no external effect). Tests write under
tmp_path only.

**Test**: `test_invariants.py::test_law31_untrusted_input_is_data`.

## 17. Claims storage (Contract §3)

**Decision**: `claims` table with PRIMARY KEY (id, version). The "current"
version is MAX(version) per id. `revise_claim` inserts a new version row;
prior versions are preserved verbatim.

**Test**: `test_invariants.py` (Law 24 structural) + E2E.

---

## Laws not mechanically testable at this stage (with reasons)

- **Law 31 (Untrusted Input)** — partially structural (file_write repo guard,
  evidence-is-data). Full "external content cannot alter policy" requires the
  policy/cognition layers that do not exist yet. Structural tests present.
- **Law 26 CONFLICTED classification** — requires semantic proposition
  comparison; preserved-and-immutable portion tested (see #6 above).
- **Law 17 "eligible for the system's normal attention mechanism"** —
  discoverability is tested (`list_open_obligations`); the attention
  mechanism itself is a cognition-layer concern.
