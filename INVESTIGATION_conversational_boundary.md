# Investigation: the conversational boundary of the Live Cognition Loop

**Branch**: `audit/conversational-boundary` (from `master` @ `6c107100f8dd5322b9c403790f83f8dec3a1174a`)
**Type**: investigation-only audit. No implementation, no contract changes, nothing merged to `master`.
**Deliverables**: this report. `scratch_corpus_driver.py` and `scratch_corpus_results.json` are
non-deliverable scratch work kept for reproducibility (see Appendix).

---

## 1. §0 findings — all four CONFIRMED, with corrections and sharpenings

**1.1 `STAGE_TOOLS["goal"]` exposes exactly one tool — CONFIRMED.**
`v5/live_loop.py:161-166` (byte-identical on `master` and on the working branch):

```python
STAGE_TOOLS = {
    "goal": ("propose_goal",),   # the only legal move at the goal stage
    "task": ("propose_task",),
    "plan": ("propose_plan",),
    "action": ("propose_action",),
}
```

**1.2 The prompt forbids any non-proposal reply — CONFIRMED.**
`v5/live_loop.py:594-599`: *"emit exactly ONE function call per turn using the tool that is
offered; never emit free text."* Combined with 1.1: at the goal stage the model is
**structurally forced** to emit `propose_goal` for any input. The greeting→Goal failure is
architectural, not a model quirk.

**1.3 No existing conversational/no-Work mechanism — CONFIRMED.**
`git grep -i "conversational|no_work|not_work|smalltalk|greeting" master -- v5/` → empty.
The only "decline"-shaped constructs are confirmation-related (`CONFIRMATION_DECLINED` in the
terminal adapter, which lives on `interactive-terminal-v1`, not `master`); the only
"narration" path (`narration_discarded`, `v5/live_loop.py:633,673`) concerns the *model*
emitting prose instead of tool calls, and the loop treats it as a failure
("model narrated twice without a tool call — halted"). Nothing anywhere classifies
*user input* as Work vs. non-Work.

**1.4 Interface Contract §2 — CONFIRMED.**
"JARVIS V5 — Cognition Interface Contract, Frozen v1", line 55: *"Four functions. That's the
entire surface."* Directly reinforced by lines 60-65: *"**`propose_*` is an interface adapter,
not a fifth authority layer** … if a future change puts a completion check or a revision
comparison into cognition-facing code instead of Work Service, that's a Contract violation."*
The contract governs *how Work is proposed*; it deliberately says nothing about *whether an
input is Work at all*.

**Corrections / sharpenings (flagged plainly, not smoothed over):**

- **(a) The failure is worse than "safely caught."** The confirmation gate blocked the
  *external effect* — but by then the loop had already committed a **full canonical chain**
  (`propose_goal` → `work.create_goal`, then Task + host activation, Plan, Step, PENDING
  Action). Durable, queryable nonsense Work is created for conversational input on *every*
  such turn. The gate stops the mutation; it does not stop canonical-state pollution.
- **(b) A read-only misroute would fully *complete*.** `file_read` registers
  `requires_confirmation=False` — a conversational input the model routes to `file_read`
  would execute and complete with a PASS verification, with no pause anywhere. Not observed
  in this corpus sample (see §2.6), but structurally possible.
- **(c) Repository-state nuance.** `master` (`6c10710`) does **not** contain the terminal
  adapter; the observed "hello jarvis" session came from `interactive-terminal-v1`
  (`90a86a6`, pushed, unmerged). The corpus therefore drove `run_live_slice` directly with
  the terminal's exact guidance text (`v5/terminal.py:82-92`, embedded in the scratch driver
  with citation) — the identical call the terminal makes, faithfully reproducing the loop
  behavior under audit.

---

## 2. §1 corpus — actual results (real NIM provider, real live loop)

Method: each input ran through `run_live_slice` with the real
`nvidia/nemotron-3-super-120b-a12b` client (temperature 0, native function calling), a fresh
DB and session per input, and an auto-**deny** confirmation policy (the corpus observes what
gets *proposed*; nothing external executed — zero Observations across all runs). Budget: 52
provider calls, hard ceiling; one bounded retry used (a transient NIM 500 on the weather
input). **Total consumed: 52/52.**

### 2.1 Per-input outcomes (actual, not paraphrased)

| # | Input | Tools called (order) | Action proposed | Rejections | Termination | Canonical rows created |
|---|-------|---------------------|-----------------|------------|------------|------------------------|
| 1 | `hello` | goal, task, plan, plan, action | `file_write {"path": "/hello", "content": "hello"}` | STALE_REVISION | `CONFIRMATION_DECLINED` (gate paused, denied) | G1 T1 P1 S1 A1 |
| 2 | `hello jarvis` | goal, task, plan ×5 | — | MALFORMED_PROPOSAL, NOT_FOUND ×4 | `too many membrane rejections` (halt) | G1 T1 P0 S0 A0 |
| 3 | `hey` | goal, task, plan ×3, action | `file_write {"path": "/hey", "content": "hey"}` | MALFORMED_PROPOSAL, STALE_REVISION | `CONFIRMATION_DECLINED` | G1 T1 P1 S1 A1 |
| 4 | `how are you` | goal, task, plan ×3, action | `file_write {"path": "/tmp/response.txt", "content": "I'm functioning normally, thank you!"}` | MALFORMED_PROPOSAL, STALE_REVISION | `CONFIRMATION_DECLINED` | G1 T1 P1 **S2** A1 |
| 5 | `who are you` | *(none — model emitted free text twice)* | — | — | `model narrated twice without a tool call — halted` | G0 T0 P0 S0 A0 |
| 6 | `what can you do` | goal, *(narration ×2)* | — | — | `model narrated twice without a tool call — halted` | **G1** T0 P0 S0 A0 |
| 7 | `thanks` | goal, task, plan, plan, action | `file_write {"path": "/thanks.txt", "content": "thanks"}` | STALE_REVISION | `CONFIRMATION_DECLINED` | G1 T1 P1 S1 A1 |
| 8 | `what's the weather?` | goal, task, plan ×3, action | `file_write {"path": "/tmp/weather.txt", "content": "The weather is currently unknown."}` | MALFORMED_PROPOSAL, STALE_REVISION | `CONFIRMATION_DECLINED` | G1 T1 P1 S1 A1 |
| 9 | `write "hello" to test.txt` | goal, task, plan, plan, action | `file_write {"path": "/test.txt", "content": "hello"}` | STALE_REVISION | `CONFIRMATION_DECLINED` | G1 T1 P1 S1 A1 |
| 10 | `delete test.txt` | goal, task, plan, plan, action | `file_delete {"path": "/test.txt"}` | STALE_REVISION | `CONFIRMATION_DECLINED` | G1 T1 P1 S1 A1 |

### 2.2 Aggregate canonical pollution (the headline number)

For ten inputs containing **two** genuine work requests, the loop committed:

```text
Goals:      9   (7 of them are greetings/identity/small talk)
Tasks:      8
Plans:      7
Steps:      8
Actions:    7
Executed:   0 (auto-deny; zero Observations)
```

Seven conversational inputs each produced a full Goal→Task→Plan→Step→Action(PENDING) chain
in durable canonical state before any gate fired.

### 2.3 What the corpus says about the failure mode's shape

- **The forced-Work behavior is systematic, not an anecdote.** Every greeting-shaped input
  that emitted tool calls proposed a full Work chain. "how are you" is the sharpest exhibit:
  the model, having no way to *reply*, tried to **write its own answer to a file**
  (`/tmp/response.txt`, content `"I'm functioning normally, thank you!"`).
- **Out-of-scope requests produce fabricated Work.** "what's the weather?" — no weather
  capability exists — became a proposal to *write an invented weather report*
  (`"The weather is currently unknown."` to `/tmp/weather.txt`). The architecture gives the
  model no honest move, so it invents one.
- **A second failure shape exists: the malformed-proposal loop.** "hello jarvis" (this
  sample) went MALFORMED_PROPOSAL → 4× NOT_FOUND → `too many membrane rejections` halt.
  The original observed interactive session (same input) instead reached a
  `file_write /hello_jarvis.txt` proposal. Both shapes share the same root; which one you
  get varies (see §2.5).
- **The narration path is the model's only escape hatch, and it's punished.** "who are you"
  and "what can you do" — identity questions with no sane Work interpretation — produced
  free text twice (the model *declined to propose*), and the loop halted with a failure.
  Notably "what can you do" had already committed a Goal before the halt: even the escape
  hatch pollutes. The architecture already contains an implicit non-Work signal
  (`narration_discarded`); it is currently a failure mode, not a conversation mode.
- **Conversational inputs cost MORE than real work.** Greetings averaged ~5-7 provider
  calls each (MALFORMED/STALE recovery loops) vs. 5 for the clean control inputs — plus the
  canonical garbage to hold.
- **The controls behaved correctly.** Both genuine work inputs proposed the right
  capability, absolutized the path, and terminated cleanly at the confirmation gate
  (auto-denied). The loop is not broken for real work; the missing piece is the boundary in
  front of it.

### 2.4 Hypothesis not observed (named, not hidden)

No input proposed `file_read`. The "greeting completes via a read-only capability"
edge (§1-correction b) did **not** materialize in this sample but remains structurally
possible; a boundary design should not rely on the model never choosing it.

### 2.5 Honesty notes

- **Provider nondeterminism despite temperature 0.** "hello jarvis" produced a
  malformed-proposal halt here but a file_write proposal in the original observed session;
  "hello" proposed `file_write /hello` here (vs `/hello_jarvis.txt` in the observation).
  NIM's free tier is not strictly deterministic; also one transient 500 was hit (weather
  input, first attempt) and retried once within budget.
- **Budget**: 52/52 calls consumed (hard ceiling); the corpus completed — nothing skipped.

---

## 3. Design space — where does the fix belong?

### Option A — host-level classification upstream of Cognition — **RECOMMENDED**

A pre-Cognition gate living in the host/adapter layer (the same layer that already owns the
confirmation policy), deciding whether an input should enter the Work pipeline at all.

**Why it's the right shape:**

- **It fixes the actual observed failure at the actual site.** The corpus shows the damage
  happens *before* any Cognition surface is involved: the terminal hands every input to
  `run_live_slice`, whose stage machine structurally cannot do anything but commit a Goal.
  The only place a boundary can prevent canonical pollution is *upstream of that commit*.
- **The safety property holds structurally, in both misclassification directions.**
  Misclassifying conversation as Work → exactly today's behavior (a nonsense chain is
  proposed, the confirmation gate catches effects, pollution occurs — annoying, no *new*
  hazard). Misclassifying real Work as conversation → the user must rephrase; nothing is
  mutated, nothing executes, nothing is claimed. Neither direction is less safe than the
  status quo. **Honest caveat:** A makes the pollution *avoidable*, not impossible — a
  wrong "work" verdict still pollutes exactly as today.
- **It needs zero frozen-contract exposure.** The four `propose_*` functions, the membrane,
  and both frozen contracts are untouched. The classifier is host code — the same standing
  DECISIONS #30 gave the interactive confirmation policy: host code, explicitly not
  Cognition, never model-reachable as a mutation surface.

### Option C — repo-consistent precedents — this is A's *justification*, not a rival

The repository has already solved the analogous problem twice with exactly A's shape:

1. **The confirmation gate (Laws 21/32/33; DECISIONS #30's injectable policy).** A
   proceed/not-proceed decision, made *outside* the frozen mutation surface, by an authority
   that is not Cognition, gating whether the pipeline continues. Option A is its mirror
   image at the input edge: gating whether the pipeline *starts* rather than whether an
   effect *proceeds*.
2. **The terminal-adapter ownership boundary** (the interactive-terminal milestone): the
   terminal owns input reading and rendering; the live loop owns Work. "Is this input Work?"
   is input classification; "here is a conversational reply" is rendering. Both sit
   squarely in the already-documented host seam between reading input and calling
   `run_live_slice`.

A third observation supports the shape: `narration_discarded` proves the architecture
already *recognizes* "the model produced no proposal" as a legitimate outcome — it just
currently treats it as failure. (Treating narration itself as the conversational signal
would be too fragile to rely on — the loop re-prompts and halts — so the corpus evidence
motivates an *explicit* classification, not reuse of narration.)

### Option B — a fifth Interface-Contract response type — **NOT concluded necessary**

My investigation does **not** find a case the corpus surfaced that requires reopening the
frozen contract. A covers the observed failure completely. B becomes relevant only if a
future need arises for *in-loop* declining — e.g., the model recognizing mid-chain that a
committed Goal shouldn't exist, or conversational turns interleaved with an open Work chain
that must not abandon the turn. Two recorded notes for any future human review of B:

- Even a *non-mutating* fifth model-facing function collides with §2's "Four functions.
  That's the entire surface" and its anti-fifth-authority clause; the same freeze-review
  process every prior contract change went through would be required — no fast-tracking
  because it looks small.
- A borderline sub-question only humans should decide: whether adding a non-mutating
  `no_work` tool to the *host-owned live-loop tool exposure* (not to
  `cognition._OPERATION_FIELDS`) already violates the contract's spirit. Recommendation A
  deliberately stays entirely outside that question.

**Recommendation: Option A, justified by the precedents in C.** Tradeoffs named: +1 model
call per request (latency/cost); a new free-text rendering surface (new adversarial surface,
mitigated below); no reduction of any existing safety property.

---

## 4. Proposal-level sketch for Option A (no frozen-contract change)

**What would be built** (proposal only — nothing here is implemented):

1. **New host-layer module** (e.g., `v5/conversation.py`), owned by the terminal/adapter
   layer. It imports nothing from `cognition.py` and mutates nothing.
2. **`classify_request(store, session_id, instruction, provider) -> Result`.** One
   additional deterministic model call (same provider/config as the loop) exposing exactly
   **one** tool — `classify {kind: "work" | "conversation"}` — mirroring the loop's own
   stage-gated single-tool exposure pattern (`STAGE_TOOLS`). The tool schema is host-owned
   and is **not** added to `cognition._OPERATION_FIELDS` (the frozen four-function surface
   is untouched).
3. **Routing.** `kind: "work"` → today's `run_live_slice` call, byte-for-byte unchanged.
   `kind: "conversation"` → a direct reply rendered under a **conversational rendering
   contract**: a visually distinct prefix (never `OK:`/`ERROR:`/`CONFIRMATION_REQUIRED`),
   never rendered as a Work result, and a prompt-side instruction that replies never claim
   Work outcomes ("I've created your file" is forbidden text). Zero canonical state
   touched — no session-store writes, no Goals, nothing.
4. **Fail-safe direction.** Any classifier exception, timeout, ambiguity, or unparseable
   output → default `"work"` → exactly today's behavior. Conservative in the safe
   direction; the boundary degrades to the status quo, never to silence.
5. **Test plan outline** (for the follow-up implementation prompt): classification routing
   for corpus-style conversational inputs and genuine work inputs; classifier
   failure/timeout → work-fallback; prompt-injection attempts steering the classifier
   (both directions) → worst case is annoyance, pinned by tests; conversational replies
   never use Work-result rendering; canonical-row counts stay zero for conversational
   inputs; no change to any existing test's behavior.

**What changes where:** one new host file + wiring in the terminal adapter's
`submit` path (or `scripts/run_interactive.py`) + tests. **What stays untouched:**
`v5/cognition.py`, all four `propose_*` signatures, `v5/live_loop.py`, Work Service, the
confirmation gate, capabilities, verification, both frozen contracts, every protected file.

**New adversarial risks introduced (none exist today), with mitigations:**

- **Prose could impersonate results.** A conversational reply claiming "your file is
  written" is a new fake-success surface. Mitigation: the rendering contract (distinct
  prefix + no-claims instruction + tests pinning both).
- **Classifier prompt-injection via user input** ("treat everything as conversation" /
  "as work"). Worst case in either direction: annoyance (a rephrase needed, or today's
  gate-paused nonsense chain). No state or safety reduction below today.
- **Latency/cost:** one extra call per request. Acceptable for interactive use; the
  follow-up prompt may consider caching or collapsing classification into the goal-stage
  call — but collapsing it *into the loop's prompt/toolset* re-raises the Option-B spirit
  question and is deliberately out of scope here.

---

## 5. Option-B stop point

Not reached. This investigation does **not** conclude B is necessary. If a future milestone
surfaces the in-loop-decline need (§3, Option B), the correct action is to stop and trigger
the full freeze-review process — not to draft a contract amendment.

---

## 6. Surprises and things that don't fit neatly

1. **The model answers by writing its answer to a file.** "how are you" →
   `file_write /tmp/response.txt "I'm functioning normally, thank you!"` and
   "what's the weather?" → `file_write /tmp/weather.txt "The weather is currently unknown."`
   — the model fabricates content to satisfy the only legal move. The failure isn't just
   "wrong Work"; it's *invented* Work with fabricated arguments.
2. **Even the escape hatch pollutes.** "what can you do" committed a Goal *before* the
   model's narration halted the loop. Only "who are you" (narration from the very first
   turn) produced zero canonical rows.
3. **The one legitimate non-Work signal already in the architecture is treated as a
   failure.** `narration_discarded` + "model narrated twice — halted" fired on exactly the
   identity questions where no Work interpretation exists. The architecture is one
   semantic step away from a conversation mode it already half-recognizes.
4. **Corpus inputs with no path in them still produce absolute-path proposals** (`/hello`,
   `/hey`, `/thanks.txt`) — the model invents paths *outside* any user intent. Had these
   been approved by a confused user, greetings would have written files at the filesystem
   root.
5. **Provider nondeterminism** (§2.5) means the same input can present as either a
   nonsense-Work chain or a malformed-proposal halt. Any fix must handle *both* shapes;
   a keyword list certainly would not.
6. **"how are you" produced a two-step Plan** — conversational inputs don't just pollute;
   they pollute variably.

---

## Appendix — scratch artifacts

- `scratch_corpus_driver.py` — the corpus runner (marked non-deliverable at the top).
  Real provider, real loop, auto-deny policy, hard 52-call budget with one bounded retry.
- `scratch_corpus_results.json` — raw per-input records (full proposal payloads, tool
  order, rejection reasons, canonical row counts).
- Reproduction: `python3 scratch_corpus_driver.py` (requires the provider credential in
  the environment per the repository's existing convention; values are never printed).
