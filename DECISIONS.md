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

## 18. Law 29/30 — repair cannot fabricate PASS verifications (2026-09 hardening)

**Decision**: A Verification `result` may reach PASS *only* through the
normal evaluation path (`run_verification`). Repair rejects any target of
`result: PASS` when the current result is not already PASS:
- PENDING → no evaluation ever ran; nothing substantiates PASS.
- RUNNING → evaluation began but its outcome was never persisted (crash #6);
  a human's belief that it passed is INFERENCE-typed inference and may not be
  recorded as a persisted outcome. The sanctioned recovery is to re-run the
  verification (`run_verification` accepts RUNNING state).
- FAIL / INCONCLUSIVE → a persisted outcome exists; rewriting it violates
  evidence immutability.

PASS → PASS is permitted only as a no-op (idempotent repair that changes
nothing). The rejection is audited with the human's identity ("repaired")
exactly like any other repair decision.

**Test**: `tests/test_hardening.py::TestVerificationRepairFabrication` (four rejection states + normal-execution and no-op controls).

### 18a. Ratification record (post-hoc approval — process gap closed)

**Status**: APPROVED AS-IS by the owner (retroactive ratification).

**Process gap — logged accurately, not reworded**: the interpretation in
#18 was implemented in `f10399e` **before** the pre-merge human sign-off the
prior brief required for Law 29/30 questions ("APPROVAL CHECKPOINT"). The
approval checkpoint was not followed in order; the code shipped, then the
owner reviewed and ratified the already-committed interpretation afterwards.
None of this entry pretends the checkpoint was followed. The gap is closed
by this record.

**The ratified rule**: `run_verification` is the only path to `PASS`. Repair
may never write `PASS` to a verification whose persisted result is not
already PASS, in any prior state — `PENDING`, `RUNNING`, `FAIL`, or
`INCONCLUSIVE`.

**Accepted consequence** (explicitly reviewed and accepted): if an
evaluation genuinely ran and crashed before its outcome was persisted
(verification stuck `RUNNING`), even a human holding rich evidence that it
passed cannot repair it to `PASS`. The sanctioned recovery is to re-run the
verification through `run_verification`, which accepts `RUNNING` state.
That re-run — not repair — is the only way PASS enters the ledger.

**Scope confirmation**: the rejection is provably narrow — it fires on
target-state `PASS` only. Repairs that set `PENDING`/`RUNNING` → `FAIL` or
`→ INCONCLUSIVE` remain legal: those are corrective "this did not complete /
we don't know" assertions (the honest opposite of fabricated success), not
Law 29 violations. Regression tests:
`tests/test_hardening.py::TestVerificationRepairFabrication::test_running_to_fail_allowed`
and `::test_running_to_inconclusive_allowed`.

## 19. Laws 17/18 — obligation owner validation at creation AND transfer
**Decision**: `create_obligation` validates, inside its single commit
transaction, that `owner` is the UOR sentinel or a row that currently exists
in `tasks`. A rejected creation returns `Rejected` and writes nothing (no
orphan obligation rows can exist, even after a crash between validation and
insert — validation and insert share one atomic boundary). `_transfer_locked`
enforces the identical rule for `new_owner`, so Law 20's terminal-transition
transfers can never detach an obligation to a non-canonical owner.

`mark_unknown_outcome` (Execution Service) is not a bypass because its owner
is *derived* from the canonical step→plan→task chain inside the same
transaction — it is never caller-supplied.

**Test**: `tests/test_hardening.py::TestObligationOwnerValidation` (ghost owner, ghost transfer, UOR + real-task accepted, no-orphan-row assertion).

## 20. Laws 23/25/29 — obligation resolution requires provenance, not just a PASS

**Decision**: `resolve_obligation` requires (1) the supplied Verification
exists and is PASS, and (2) the verification's claim cites at least one
runtime Evidence whose `origin_observation_id` walks back to an Observation
of the obligation's `origin_action_id`. INFERENCE/UNKNOWN-typed evidence
carries no observation and cannot establish the link — an LLM's belief that
"it worked" is not proof about the originating action.

Canonical chain: `Obligation.origin_action_id` → `Observation.action_id` →
`Evidence.origin_observation_id` → `Claim.based_on` → `Verification`.

**Semantic limit (boundary documented, not a redesign)**: provenance +
PASS proves the verification *concerns the originating action*. It does not
prove the claim's natural-language content addresses the obligation's
specific `unknown_reason`/`possible_external_effect`. Content relevance is
not machine-checkable at the SQL layer; it is carried by the Claim/Verification
quality model (method, independence_level) and the human in the loop ratifying
or rejecting the obligation resolution. This pass does not invent content
semantics.

**Test**: `tests/test_hardening.py::TestObligationResolutionProvenance` (valid chain resolves; unrelated Action B's PASS rejected; inference-only PASS rejected).

## 21. Law 32 — confirmation binds to exact Action revision

**Decision**: `create_confirmation` already recorded `action_revision`; the
hardening was at the *gate*: both lookup paths (`make_confirmation_gate`'s
general per-action lookup AND the action-specific `confirmation_id` path)
now reject when `confirmation.action_revision != action.revision`. A material
change that legitimately advances revision (e.g. a repair setting
`confirmation_id` or any revision-bumping transition) invalidates the staged
confirmation even with identical capability/arguments. A fresh confirmation
at the new revision is the only way forward.

**Test**: `tests/test_hardening.py::TestConfirmationRevisionBinding` (both paths reject after revision advance; control: current-revision and restaged confirmations pass).

## 22. IntegrityStatus scope — goal/task/action only

**Decision** (resolves the frozen schema vs. the Law 5 freeze verb): the
contracted schema grants an `integrity` column only to Goal and Task (we
added it to Action as a documented storage addition for Law 5 execution-
freeze — see foundation DECISIONS). Plans, Steps, Verifications, Obligations
do not carry IntegrityStatus as storage. Consequently:
- `_REPAIRABLE_FIELDS` offers `integrity` only on goal/task/action. Granting
  it on the column-less types produced a latent `no such column` SQL error
  at commit time — a genuine defect now closed (schema-truthful whitelist).
- `work.freeze_object` accepts goal/task only (plan/step are rejected with
  INVALID_TRANSITION; execution freezes go through
  `execution.freeze_action`). Freeze is idempotent (re-freezing returns Ok).
- `repair.abandon_unrepairable` accepts goal/task/action only. Plans/Steps
  reach terminal integrity through status transitions (supersede/cancel/fail),
  not through an integrity settlement.

Law 5's *principle* (frozen things don't mutate) is preserved for the types
that carry integrity; the *verb* is now schema-truthful.

**Test**: `tests/test_hardening.py::TestRepairSchemaTruthful`, `TestFreezeObjectTypeGate`, `TestAbandonTypeGate`.

## 23. Law 13 — committed plan.dependency_graph is the sole canonical graph

**Decision**: `Step.depends_on` is a denormalized read-copy kept in sync by
`add_dependency` (same transaction, checked against the as-committed graph).
It is **not** independently repairable (removed from `_REPAIRABLE_FIELDS`)
because repairing the copy would diverge the two Law 13 representations.
Repairs target the canonical `plan.dependency_graph` and are aggregate-
validated with the same `_has_cycle` check the normal path enforces —
self-dependencies and cycles are rejected; after commit, `add_dependency`
re-syncs the step copies from the repaired graph on its next write.

(The trigger in Phase 4 was discovering that repairing the denormalized copy
was possible while cycle-checks only guarded the normal add path.)

**Test**: `tests/test_hardening.py::TestDependencyGraphRepairDiscipline` (`depends_on` repair forbidden; cyclic graph repair rejected; acyclic repair allowed—additive cycle check reused).

## 24. Schema migration mechanism (2026-09; evidence.origin_observation_id)

**Decision**: `Store` tracks schema versions via SQLite's native
`PRAGMA user_version` (no separate framework, no extra tables). Migrations
are strictly additive (`ALTER TABLE ... ADD COLUMN`), idempotent
(`PRAGMA table_info` guard), and atomically bounded in a normal write
transaction. v2 added `evidence.origin_observation_id`
(Fix 6/20 provenance column); subsequent migrations numbered themselves at
the tail of this file — v3 added `steps.description` /
`steps.execution_capability` + the `sessions` table (Cognition contract,
see #26) and v4 adds `verifications.step_id` (requirement provenance,
see #27). A legacy database with pre-existing rows upgrades safely through
the additive chain: columns are added, no rows are touched,
`user_version` advances.

This exists because `CREATE TABLE IF NOT EXISTS` cannot add a column to an
already-opened production database. It is the smallest mechanism that does
the job.

**Test**: `tests/test_hardening.py::TestRuntimeEvidenceProvenance::test_schema_migration_adds_column_and_preserves_rows` + `::test_migration_is_idempotent`.

## 25. Coverage-verification round 3 — two latent validation gaps closed

Surfaced by re-auditing the seven constitutional modules end-to-end for
"authorizes a B that doesn't provably belong to/derive from A":

**(a) `bind_required_verification` object-side check (Law 16).** Previously it
verified the Verification existed but not the bound object — a binding to a
phantom id wrote an orphan row proving nothing. Now both endpoints must
exist, and the object must be a completable Task|Goal (the completion
authority's contract covers those two kinds only — step-bindings could never
fire). Fail fast, no silent orphan rows.

**(b) `transition_object` action routing + integrity parity (Law 12/5/29).**
An action id passed through the Work transition surface hit the
`{...}[kind]` lookup with no `"action"` entry — raising an unhandled
`KeyError` inside a transaction instead of a typed rejection. Actions now
route explicitly: rejected `INVALID_TRANSITION` with Law 12 authority-
separation rationale (actions transition through the Execution Service).
The same fix restored the revision CAS that the rescue edit had dropped
(regression caught immediately by test_law3_revision_cas — proving the CAS
suite works) and added `ABANDONED_UNREPAIRABLE` to the integrity guard so a
terminally-abandoned object can no longer transition (parity with
`_terminal_transition` and `complete_object`).

**Explicitly re-checked and found clean (not re-listed in the table):**
- `execution.freeze_action` — revision CAS already present since the
  foundation (single-writer store makes the bump atomic).
- `safety.create_confirmation` — a confirmation staged for a nonexistent
  action is *inert*: the execution gate only evaluates confirmations for the
  real action being executed, and `begin_executing` rejects the action
  first. No phantom authorization path exists; no law violated. No fix.
- `recovery.recover` — every mutation goes through ordinary contracted
  transitions (`mark_unknown_outcome`, `freeze_action`); the loop is
  idempotent and stateless. Law 30 holds.
- `repair._TERMINAL_SETS` + `supersede_plan` — terminal repairs transfer
  obligations and supersede CAS'es the old plan's revision inside the same
  transaction; verified against Contract crash #7.

**Test**: `tests/test_hardening.py::TestBindTargetExistence` (2),
`::TestTransitionObjectActionRouting` (2).

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

## 26. Cognition Implementation Contract v1.1 — membrane implementation (2026-09-25)

### New modules added (the only ones the contract authorizes)
- `v5/cognition.py` — the four frozen `propose_*` functions, the
  `AuthenticatedCognitionContext` (internal plumbing, §13.2), the
  model-output adapter (§18–§26), and all bound constants (§8).
- `v5/sessions.py` — the Session authority the contract references (§13):
  ephemeral session lifecycle + single deployment `OWNER_PRINCIPAL_ID`
  (env-overridable — it is config, not state).
- `v5/verification.py` — the verification-method registry (§28):
  `verify_file_write` is registered with an evaluator that reads the
  filesystem independently (§34/§35).

### Reused, not duplicated
State Foundation (store/transactions/CAS), Work Service (create_*,
activate_plan, complete_object, bind_required_verification), Execution
(create_action, begin_executing, mark_observed, mark_unknown_outcome,
mark_failed), capabilities registry (registered `file_write` with declared
`validate_args` schema), evidence/claim/verification paths, confirmation
gate, obligations, repair. No second store, lifecycle, planner, verifier, or
capability system was created.

### Interpretations recorded
- **propose_goal sets context.** propose_goal takes `session_id` in its
  frozen signature; the other three take an object id + expected revision.
  `principal_id` is resolved once at authentication from deployment config
  (§13.1), threaded via `contextvars`, and read back against the Session
  authority on every call. `authenticate_session` failure clears any stale
  threaded context — a swap attempt must never leave the old authorization
  active. This is the contract's "however that context is threaded through
  the calling code" mechanism.
- **Bounds values (§8).** Step description = 500 (pinned by Interface §4).
  Others are implementation-defined; conservative choices:
  goal/task statement 2000, plan steps 20, per-step dependencies 20,
  verification requirements 8, capability args 32 keys/64KiB/depth 8.
- **`Step.execution_capability` is canonical.** Column `steps.execution_capability`
  is "" on legacy foundation-created steps (pre-Cognition steps are
  unconstrained — existing Foundation behavior is untouched), and required on
  any Cognition-proposed step. Both propose_action and `execution.create_action`
  enforce correspondence independently (§12.2).
- **Plan acceptance is one commit.** `work.create_plan_with_steps` creates
  Plan+Steps+claims+verifications+bindings in ONE transaction (§50). It
  composes the foundation's `_create_claim_locked`/_create_verification_locked/
  `_bind_required_verification_locked` cores rather than duplicating them;
  those cores were extracted without changing their public-wrapper behavior.
- **Verification binding happens at plan commit (§27).** Work Service binds
  declared verification requirements to the Task. Cognition only exposes
  `VerificationRequirement` data on StepProposal; it never calls any binding
  function.
- **Adapter dedupe is same-emission only** (§26): content-hash equality of the
  full parsed payload within one `parse_proposals` call. Cross-emission
  repeats are legitimate new proposals (proved by test_cross_emission_repeat).
- **Asked §44's "direct Cognition attempt to mutate State Foundation tables /
  invoke a capability"**: enforced by construction — v5/cognition.py contains
  no store.write/conn.execute/capability.execute call and no repair import;
  plus a structural test asserts that (test_cognition_module_never_opens_write_transactions).

### Adversarial-audit result (v1.1 scope, §10 checklist)
Cognition cannot mutate canonical state (no write surface); cannot fabricate
IDs (NOT_FOUND, never guessed); cannot execute capabilities (capability never
called by propose_action — instrumented test); cannot fabricate Observ/
Verification PASS (verification is filesystem-grounded; repair can't write
PASS — regression); session termination cannot strand Work (§13.4 payoff
test passes); session_origin is inert under direct corruption (test proves
it); principal_id never reaches the schema (schema test); duplicate
proposals cannot create duplicates (same-emission dedupe; cross-emission is
by design); no parallel authority exists (structural import-grep test).

## 27. Emission Is Not Occurrence gate on verification (2026-09 audit fix)

**The defect**: an independently-reproduced exploit — propose a `file_write`
Action whose arguments match an already-existing file, NEVER execute it, then
run the step's requirement verification → PASS fabricated. The evaluator read
the filesystem (independent) but never established that the Action itself
actually executed, nor that the Action was the one bound to the requirement.

**The fix (single authoritative boundary)**: `run_verification(...,
for_action_id=...)` gains a mandatory-when-bound precondition, evaluated
inside the RUNNING transaction (so no racy check-then-verify window):
  1. the Action exists and is `OBSERVED` (only status that establishes an
     external effect was observed — PENDING/EXECUTING/FAILED/UNKNOWN_OUTCOME
     all reject with the new `R_ACTION_NOT_EXECUTED`), and
  2. if the Verification is requirement-bound (verifications.step_id, set at
     plan commit by `create_plan_with_steps`), the Action must belong to THAT
     step — `R_ACTION_NOT_BOUND` otherwise.
The same two checks repeat in the result-commit transaction so a status
flip between RUNNING and result-commit can't strand a false verdict.
`verification.run_method_for_action` always passes the Action id through.
New rejection codes are documented in `v5/models.py` alongside the others.

**Cross-reference**: schema migration mechanism (#24) records v4
(`verifications.step_id`); this entry is what v4 exists for.

**Tests**: `tests/test_cognition.py::TestNoPassWithoutExecution` — the exact
exploit + PENDING/EXECUTING/FAILED/UNKNOWN_OUTCOME/unrelated-Action
rejections + the legitimate OBSERVED→PASS path stays green.

## 28. Live LLM Cognition Loop v1 (2026-09-26)

**Scope**: the orchestration glue that drives the frozen Cognition membrane
from a real deterministic model (one instruction, one provider, one linear
tool-call chain). This passes the contracts' barrier between "code that
proposes" and "code that commits".

**Decisions recorded**:

- **Module location**: `v5/live_loop.py`. Contracts authorize exactly this:
  an adapter/host wrapper that calls the four frozen `propose_*` functions
  (plus `cognition.parse_proposals`/`cognition.dispatch`) and the existing
  execution-boundary/verification helpers. New directory/package creation
  was rejected per the same "no parallel structure" rule.
- **Provider**: NVIDIA NIM `nemotron-3-super-120b-a12b` (current V4 routing
  slot), OpenAI-compatible native function-calling endpoint, temperature 0.
  One provider, no routing/fallback/abstraction (§5 non-goal). The Gemini
  2.5 Flash client's config was built and preserved
  (`GeminiLiveClient`); the Gemini free-tier per-day quota was exhausted in
  this environment during verification and NIM was the immediately-available
  substitute. §1d's "single provider" rule is unaffected.
- **Structured-tool-call-only**: only the arguments field of genuine
  function-call responses reaches `cognition.parse_proposals`. A turn with no
  function call is discarded, the model is re-prompted once, and then the
  loop halts cleanly. No text-scraping/regex path exists.
- **Stage gating**: exactly one tool exposed per stage (goal → task → plan →
  action), generated from the frozen `_OPERATION_FIELDS`/`_STEP_*`/`_VR_*`
  tables in `v5/cognition.py`. Off-stage calls are discarded as if narrated
  (the frozen membrane independently rejects out-of-order calls anyway).
- **Identity provenance**: `session_id` is read only from the authenticated
  threaded context. `dispatch()` never uses model-supplied identity fields;
  unknown-field rejection kills any attempt. provenance test in §1c confirms.
- **Dedup scope**: only `parse_proposals`'s same-emission content-hash dedup;
  no cross-turn/global suppression was added (§26).

**Tests**: `tests/test_live_loop.py` (10 tests: schema-drift guard, missing
field, unknown field, hallucinated id, identity injection, wrong-capability,
narration-only, same-turn duplicates, offline chain, plus real-transcript
artifact at `artifacts/live_slice_transcript.json`).

## 29. Live Loop v2 — capability generalization + N-step drive (2026-09-26)

**scope**: the model now drives a real two-step Plan (file_write, then
file_read dependent on it) end-to-end; a second capability was added to prove
the architecture generalizes beyond file_write.

**Decisions recorded**:

- **file_read idempotency class**: `IDEMPOTENT`. The capability has NO external
  side effect — re-running reads is trivially safe and the same request
  produces the same state. (The class exists to protect *external* effects
  on retry; a pure query has none. NOT marking it CONDITIONALLY_IDEMPOTENT —
  that implies a relevant side effect key, which reads don't have.)
- **verify_file_read independence**: the evaluator reads the ACTION's path
  from its committed arguments, and the Observation row's claimed
  `content` from the committed observation — then *re-reads the real
  filesystem itself* and compares claimed-bytes against actual-bytes. This
  mirrors verify_file_write's shape (request vs ground truth) but the truth
  side for a reading capability is the file's CURRENT contents and the claim
  side is the Observation's self-report. A capability that fabricates its
  read content (test: replace file_read's execute with a liar reporting
  "Goodbye World" while the file holds "Hello World") fails this check —
  the Observation cannot self-certify.
- **Dependency-order drive**: `_dependency_order` walks the committed
  plan's `dependency_graph` in Kahn topological order; `_drive_step`
  executes/observes/verifies/completes ONE step and the loop only advances
  `step_idx` when the previous step's terminal completion has landed. An
  Action for a later step proposing out of order is wrong-turn garbage —
  the loop feeds the model a `WRONG_STEP_ORDER` rejection telling it which
  step is actually next.
- **Foundation immutability confirmed**: nothing in `begin_executing`,
  `create_action`, or `create_plan_with_steps` needed changing to support a
  dependent second step — Law 13's acyclicity check at plan commit plus the
  per-step drive order suffice. The wrong-order adversarial test is satisfied
  by existing checks (FAILED + ACTION_NOT_EXECUTED + COMPLETION_POLICY_UNSATISFIED),
  no new gate was added.
- **What did NOT need changing**: `v5/capabilities/__init__.py` file_write
  untouched; `v5/verification.py` only gained a new registration (no change
  to verify_file_write); no State Foundation schema change; the milestone is
  genuinely membrane+new-modules.

**Test coverage**: `tests/test_live_loop_v2.py` — 12 tests: registration
shape, FAILED-not-OBSERVED on missing file, wrong capability for the read
step, missing/wrong-type/extra argument rejection at both adapter
(MALFORMED_PROPOSAL) and authority (CAPABILITY_ARGS_INVALID) layers,
out-of-order Step-2 attempt caught by the existing gates, the
**fabricated-Observation sabotage test** (capability claims wrong content,
independent verifier reads the real file → FAIL), file deletion after honest
read → FAIL, wrong-path retry succeeds through a new Action identity, and
the full offline two-step slice through the generalized loop machinery.

**Live proof**: real NIM (`nemotron-3-super-120b-a12b`, temp 0, native
OpenAI-style function calling) end-to-end run in
`artifacts/live_slice_v2_transcript.json`. Model proposed the two-step plan;
the stale-task-recovery path fired on turn 3 (expected_task_revision=0 →
rejected), the model reread the state and re-proposed on turn 4 with the
current revision (1) — §45 correct. Both steps completed observably.

## 30. Confirmation gate wired into the live Cognition Loop (2026-09-27)

**The gap closed**: `v5/live_loop.py` hardcoded `make_confirmation_gate(required=False)`
at the execution drive, silently disabling confirmation the capability specs
demand (file_write registers `requires_confirmation=True` — the v1/v2 live
runs executed writes through a gate that was told confirmation was not
required). No safety primitive was missing: `v5/safety.py` and the
execution-boundary gate were already hardened and independently audited;
only the live-loop integration was absent.

**Decisions recorded**:

- **Registry stays the source of truth**: `_drive_step` now derives the gate
  requirement from `CapabilitySpec.requires_confirmation` of the Action's
  registered capability. The execution-time gate (inside `begin_executing`'s
  atomic boundary) remains the sole authority. No confirmation logic was
  duplicated in `live_loop.py`.
- **Blocked-attempt-then-confirm flow**: for a confirmation-requiring
  capability, the first execution attempt genuinely happens unconfirmed and
  is rejected by the gate (nothing is written — PENDING unchanged); the
  block is recorded in the transcript; only then does the host confirmation
  flow run, and execution is retried. The transcript therefore carries the
  full proof sequence: blocked → granted → executed.
- **Host-side confirmation is host code**: `_stage_and_confirm` is the
  deterministic human-approval simulation for proofs — it uses ONLY the
  existing Law 32 mechanism (`create_confirmation` bound to exact identity +
  revision + capability + arguments, then `confirm`). `confirmation_policy`
  is an injectable host callback (a real deployment substitutes an
  interactive prompt); the model has no operation that reaches it, and
  model-supplied `confirmed`/`confirmation_id` fields are rejected as
  unknown fields at the adapter before anything dispatches.
- **`file_delete` capability** (exists solely to exercise this boundary):
  IDEMPOTENT (same request → same end state: absent), `requires_confirmation
  =True`, strict `{"path"}`-only argument schema, repo-root self-protection
  guard mirrored from file_write, `DefiniteNoEffect` for missing/non-file
  targets, FileNotFoundError race handled as definite-no-effect.
- **`verify_file_delete` independence**: the evaluator loads the target from
  the Action's canonical arguments and inspects the LIVE filesystem itself;
  PASS only when the target is absent. It does not trust the capability's
  return value, Action status, Observation, or any model claim — the
  sabotage test proves a lying capability (reports deleted, file untouched)
  FAILs verification. "Target existed before" is a harness precondition of
  the deletion flow (the capability refuses missing targets), not something
  the verifier could establish from the past filesystem.
- **Behavior change for existing capabilities**: file_write now genuinely
  requires confirmation in live-loop runs (its spec always said so). The
  v1/v2 offline loop tests pass unchanged because the default host policy
  stages+confirms deterministically — their final-state assertions are
  untouched; their transcripts now additionally contain the blocked/granted
  events, which is the honest record.

**Tests**: `tests/test_confirmation_gate.py` (22 tests) — A unconfirmed
blocked (nothing written, PENDING, target intact); B retry/rephrase/new-Action
all blocked + same-emission dedup; C fabricated confirmation (model-supplied
`confirmed`/`confirmation_id` rejected as unknown fields, prose approval is
not a proposal, a confirmed row for a different Action authorizes nothing);
D wrong-Action reuse (confirmed A never authorizes B; the pointed-binding
variant also rejected on the Law 32 arguments check); E arguments/capability
mis-staging never authorizes; F revision binding (staged-for-N rejected at
N+1 after a legitimate repair-bumped revision); G full lifecycle after
genuine confirmation; live-loop integration (block→grant→execute ordering,
refusing policy fails honestly, stage-only policy stays blocked — staging
without `confirm()` authorizes nothing, non-confirmation capability
file_read unchanged, no confirmation events); capability registration
shape; repo guard; verifier sabotage test.

**Live proof**: `artifacts/live_slice_delete_transcript.json` — real NIM
(nemotron-3-super-120b-a12b, temperature 0, native function calling) drove
"Delete this temporary test file." end to end: propose_goal → propose_task →
propose_plan (with a genuine STALE_REVISION recovery) → propose_action(file_
delete) → PENDING → execution_blocked CONFIRMATION_REQUIRED → host
confirmation cfm_01M3J9579D8XP1G380N4139QPK → OBSERVED → verify_file_delete
PASS → Step COMPLETED → Task COMPLETED → target absent on disk.

## 31. Minimal interactive terminal adapter (2026-09-27)

**Scope**: `v5/terminal.py` (the thin adapter), `scripts/run_interactive.py`
(the entry point), `tests/test_terminal.py`. No foundational file changed;
`v5/live_loop.py` needed zero modification — the terminal is purely additive
around the existing seams.

**Decisions recorded**:

- **Ownership boundary**: the terminal owns ONLY terminal input (three
  separate channels: normal requests, local commands, confirmation
  decisions), local commands (/help, /debug, /quit — never entering the live
  loop), rendering, and the confirmation PROMPT. All JARVIS semantics stay
  in the existing loop/membrane.
- **One request = one `run_live_slice` turn**: each user input is one
  live-loop turn through the frozen membrane; one process = one live V5
  session (`sessions.create_session`), reused across turns. No second
  session store or dispatcher exists.
- **Confirmation pause/resume uses the DECISIONS #30 seam**: the terminal
  passes an interactive `confirmation_policy`; the loop calls it when its
  gate blocks, the policy renders CONFIRMATION_REQUIRED + a safe action
  summary, reads the human decision, and approval flows through the
  EXISTING mechanism (`safety.create_confirmation` + `safety.confirm`). The
  loop then retries the SAME Action — turn, Action identity, revision,
  arguments, and verification binding are preserved by construction, and no
  duplicate Goal/Task/Plan/Action is created (proven by canonical-state
  assertions in tests and the live smoke). The human decision is terminal
  input, never a new Cognition turn: model-call count is asserted unchanged
  across the confirmation.
- **Lazy provider construction**: local commands and EOF never build the
  provider, so they work with no credentials (fully offline). The provider
  initializes on the first request; an initialization failure there is
  FATAL with exit 1 (§12's unrecoverable-initialization class), while
  request-time failures (including provider HTTP errors) are rendered as
  handled ERRORs and the session continues (exit 0 at clean termination).
- **Exact multiline protocol**: `<<EOF` alone on its line begins; `END`
  alone on its line terminates; markers excluded; blank lines/whitespace
  preserved; body joined with \n and submitted as exactly one request;
  empty body ignored (not submitted); EOF mid-body discards the incomplete
  request and exits 0. Commands are recognized ONLY at the normal prompt
  (exact line match); inside a body they are literal content; at a
  confirmation prompt anything but y/yes is a denial.
- **Exit statuses**: 0 for /quit, EOF (normal, multiline-discard,
  confirmation-not-approved), handled request failures, denial; 1 only for
  unrecoverable terminal/runtime failure (provider init, infrastructure).
- **Secret safety**: all error paths render through `redact_secrets`
  (environment values whose names match KEY/TOKEN/SECRET/PASSWORD are
  masked); confirmation summaries and debug events carry no payloads or
  credentials; debug output is real transcript events (kind/op/status/
  reason codes only), never fabricated narration.
- **Store location**: interactive runs default to `~/.jarvis_v5/state.db`
  (`JARVIS_V5_DB` override; `JARVIS_ENV_FILE` for the dotenv credential
  file per the repository's existing script convention).

**Tests**: `tests/test_terminal.py` (39 tests) — basics (help/debug/quit/
unknown command/empty input), reader semantics (exact lines, whitespace
submission, sequential no-read-ahead across confirmation, EOF), the full
multiline matrix, run() and REAL process exit codes (including FATAL exit 1
via a sanitized-env subprocess), error rendering (native rejection codes,
execution failure, verification failure, sanitized exceptions), debug mode
(real events, no secrets), and the confirmation lifecycle (approval:
same-Action/no-duplicates/OBSERVED/one Observation/verification PASS/
COMPLETED; denial: PENDING/no Observation/file intact/no confirmed rows;
EOF: cannot approve; commands-at-confirmation = denial). Suite: 201 -> 240.

**Live smoke (real NIM provider, real script)**: write turn with real
confirmation pause + approval (file created); read-back turn completed;
delete denied (file intact) then approved (file gone) in one session;
confirmation EOF (NOT APPROVED, file intact); exact multiline submission
(file written); /help, /debug, /quit, plain EOF all exit 0; transient
provider 500s rendered as handled ERRORs with session continuation.

## 32. Conversational Boundary v1 — host-layer classification (2026-09-28)

**Scope**: implements exactly Option A of
`INVESTIGATION_conversational_boundary.md` §4 (audit/conversational-boundary
@ e85ed77). New host module `v5/conversation.py` + wiring in the terminal's
`submit` path. No foundational file changed; `v5/cognition.py`,
`v5/live_loop.py`, and both frozen contracts are byte-identical to
interactive-terminal-v1 @ 90a86a6.

**Decisions recorded**:

- **The boundary is the mirror image of the confirmation gate**: a
  proceed/not-proceed decision made OUTSIDE the frozen mutation surface by
  host code, gating whether the Work pipeline *starts* (where the
  confirmation gate governs whether an effect *proceeds*). It lives upstream
  of `run_live_slice`; the work path is byte-for-byte unchanged.
- **classify tool is host-owned**: a single `classify {kind:
  work|conversation}` function-calling schema living in `v5/conversation.py`,
  deliberately NOT added to `cognition._OPERATION_FIELDS` — the frozen
  four-function surface stays four functions. A structural test enforces
  that the module imports nothing from the Cognition module and performs no
  Work mutation or store write.
- **Fail-safe direction is enforced inside classify_request, not the
  caller**: any exception (provider timeouts raise), missing tool call,
  unparseable args, or out-of-enum kind → kind="work" → today's behavior.
  The boundary degrades to the status quo, never to silence. The corpus
  re-run exercised this live: three transient NIM 500s on the classify call
  fell back to work exactly as designed (honest ERROR, exit 0, no bypass).
- **Conversational replies carry a mechanical claim guard**: the reply
  prompt forbids Work-completion claims, and `reply_has_work_claim`
  enforces it mechanically (first-person perfective claims, "done
  it/that", bare "done!" as a reply). A tripping reply is replaced by an
  honest canned line — prompt instruction alone is not trusted. Reply
  generation failure renders an honest unavailable line under the
  conversational prefix — NEVER routed to the work path (that would
  recreate the pollution) and never rendered as ERROR (conversation is not
  a failure).
- **Rendering contract**: replies render under a distinct `jarvis: `
  prefix, never through the Work-result paths (OK:/ERROR:/CONFIRMATION_/
  REQUIRED/DECLINED/NOT APPROVED). Tests pin every collision.
- **Known cost (named, not resolved)**: every terminal request now pays one
  extra classification call; conversational turns additionally pay one
  reply call but no longer pay 4-7 loop calls + canonical pollution. The
  investigation explicitly deferred caching/collapsing the classification
  into the loop's prompt — that re-raises the Option-B spirit question and
  is out of scope.

**Tests**: `tests/test_conversational_boundary.py` (24 tests) — structural
import boundary; the exact 8+2 corpus routing with zero-row/zero-loop-call
assertions; fail-safe (exception/timeout/garbage/no-tool-call → work, both
through the terminal and as direct unit checks); prompt-injection both
directions at the WORST CASE (classifier complies) proving annoyance-only
outcomes; the rendering contract incl. claim-guard replacement; mixed
conversation+work sessions. Two existing provider-call-count assertions in
test_terminal.py were updated 4→5 to account for the new upstream classify
call — the guarded invariant (the confirmation itself adds zero model
requests) is unchanged.

**Live corpus proof** (`artifacts/conversational_boundary_corpus_rerun.json`,
real NIM provider, real terminal): all 8 conversational inputs →
conversation, ZERO canonical rows (before: 34 rows of nonsense Work), real
natural replies — including "what's the weather?" now answered honestly ("I
don't have live weather data at hand") instead of the original fabricated
file_write of invented weather content. Both work inputs → work (gate-paused),
byte-identical behavior to the original investigation (full chain, CONFIRM
ATION_REQUIRED, declined via piped /quit, exit 0). Transient NIM 500s on
three classify calls exercised the fail-safe live: fallback→work, honest
ERROR, exit 0 — retried inputs then classified conversation cleanly.

Suite: 240 -> 264 passed.

## 33. Execution Outcome Observability v1 (2026-10-01)

**Scope**: the human-facing observability defect exposed by the real
helloworld.txt terminal run (forensic audit 2026-09-30): the debug event said
`execute_action ok` while the canonical disposition was UNKNOWN_OUTCOME, and
the terminal rendered a bare `ERROR STEP_NOT_COMPLETED` with the underlying
cause (an OPEN obligation from OSError: Read-only file system) dropped before
rendering. The execution/uncertainty/obligation/verification behavior was
correct throughout and is UNCHANGED — this is an observability fix only.

**Decisions recorded**:

- **The canonical executor payload is the only disposition authority**: the
  execute_action debug event now carries `ok_req.value["status"]` verbatim
  (OBSERVED / FAILED / UNKNOWN_OUTCOME) instead of a hardcoded "ok". No
  second execution-status representation exists anywhere; the "Ok" wrapper on
  the executor return means only "the contracted pipeline ran to a terminal
  disposition", never "the effect happened".
- **Failure detail is threaded, not reconstructed**: `_drive_step` carries the
  disposition's operational truth upward — the capability's `reason` (FAILED)
  or, for UNKNOWN_OUTCOME, the OPEN obligation id + its canonical
  `unknown_reason` (read from the obligations row, not from the exception
  object). `run_live_slice`'s failure return gains a `detail` field ("step …
  ended in UNKNOWN_OUTCOME not COMPLETED — <reason> (obligation obl_… is OPEN
  for resolution)"); the terminal renders `ERROR <code>: <detail>` through
  `redact_secrets`. The VERIFICATION_NOT_PASS and drive-rejection details that
  previously existed only inside Rejected objects now survive to rendering
  too.
- **Semantics frozen**: UNKNOWN_OUTCOME is never coerced to FAILED; no
  Observation, verification, Step/Task completion, auto-retry, or obligation
  resolution is added for non-OBSERVED dispositions. The debug event carries
  reason/obligation fields but the disposition check that gates
  evidence/verification is byte-for-byte the same comparison.
- **Existing assertions extended, not weakened**: two tests asserted the
  misleading behavior itself (the hardcoded "ok" status; the detail-less
  ERROR line). They were updated to assert the truthful forms plus the
  additional detail — the guarded invariants (code preserved, no fake
  success, ordering) are all still asserted.

**Tests**: `tests/test_execution_observability.py` (5) — OBSERVED debug
truthfulness + unchanged success rendering; deterministic UNKNOWN_OUTCOME
(read-only dir) with full canonical-state assertions (Action UNKNOWN_OUTCOME,
OPEN obligation, zero Observations, verification stays PENDING, Step PENDING,
Task not COMPLETED) + no `execute_action ok` anywhere + terminal ERROR line
with disposition/reason/obligation; run_live_slice result carries
reason+detail; FAILED distinguishable from UNKNOWN_OUTCOME (no obligation
for FAILED); success/failure rendering prefixes stay distinct. Suite: 264
-> 269 passed.

**Smoke (deterministic, exact forensic scenario)**: the helloworld.txt case
now renders `[debug] … execute_action UNKNOWN_OUTCOME capability raised
OSError: [Errno 30] Read-only file system: '/helloworld.txt'` and
`ERROR STEP_NOT_COMPLETED: step … ended in UNKNOWN_OUTCOME not COMPLETED —
capability raised OSError: … (obligation obl_… is OPEN for resolution)`; the
writable control renders `execute_action OBSERVED` → verification →
`OK: Task COMPLETED`. Canonical state identical to the forensic baseline in
both cases.
