# Plan: Behaviour under test

## Goal

Test what a running session actually does — the reaction policy, the Claude Code layer, the
error codes through the real CLIs, the two server concurrency paths — instead of checking
prose and unit-level shapes.

## The decision the four tickets were waiting on: two tiers, one line between them

All four gate reviews (#30, #33, #34, #35) asked the same thing in different words: which
harness, gating or reporting, in pytest or not. Decided here so no ticket re-litigates it:

- **Tier A — deterministic, in `tests/`, in CI, gating.** Subprocess tests through the real
  `bin/*.py` entry-points, plus a raw `role=control` WebSocket helper for the frames the
  shipped CLIs cannot emit (`TEXT_TOO_LONG` exceeds Linux `MAX_ARG_STRLEN`; `INVALID_NAME`,
  `INVALID_LABEL` are pre-validated client-side; `UNKNOWN_OP`, `INVALID_PAYLOAD` need a raw
  frame). The hybrid is accepted **on condition that the test names say which path each code
  took**: a code reached only by raw frame is not "through the real CLI path", and the ticket
  titles stop claiming it is. Budget: the whole tier adds at most 30 s to `make test`; a
  rate-limit case drives 60 broadcasts in-process, never 61 `send.py` runs.
- **Tier B — model-driven, in `evals/`, on demand, reporting.** `claude plugin eval` cases
  (`prompt.md` + `graders/*.md`, or `case.yaml`; `llm`, `tool_used`, `regex` graders) driven by
  `make eval`. **Never in pytest** (the no-skip rule stands, and a live-model test that cannot
  run must not look like one that passed), **never a CI gate** (each run spends the operator's
  credential; this repo has already lost a night to the account's usage limit twice), **no
  recorded transcripts committed** (public repo). A red eval is a report to read, not a build to
  fix at 2 a.m.

Two consequences the tickets must carry:

- **Tests-only tickets may fix what they expose only when the fix is small and inside the
  ticket's files** (the gate's rule of thumb: under ~30 lines, no new op, no new literal).
  Otherwise the defect is filed and the test lands as `xfail(strict=True)` pointing at the
  ticket — which is how a tests-only ticket evidences "red against the unfixed code".
- **The destructive-op gate has one reading.** `SKILL.md` L85 and L88-95 (re-affirmation must
  arrive in a *separate* message; no first-message phrasing turns the gate off) is the oracle;
  L175-178 ("explicit affirmative content in the incoming message") contradicts it and is
  rewritten under #35. An eval that scores the weaker reading is wrong by definition.

## Done looks like

- A spike ticket answered with evidence: `claude plugin eval` can (or cannot) put a synthetic
  monitor line in front of the `talk` skill so a grader sees the reaction. If it cannot, Tier B
  is re-shaped in this plan before any fixture is written.
- `make eval` exists, documented in `docs/coding-standards.md` → *Tests* as the report tier,
  with its cost per run stated.
- #35: the reaction policy's five scorable behaviours (act / surface / reply prefix / same
  transport / destructive gate) each have a Tier B case with a positive and a negative, and the
  SKILL.md contradiction is gone.
- #34: a Tier A subprocess test drives the standalone-skill and plugin-dir paths through
  `claude -p --plugin-dir` where that is feasible, and the checklist covers the rest; the
  500-unit clip is measured by a script, not asserted from prose.
- #33: every `ErrorCode` member is produced by a test that names its path (CLI or raw frame);
  the two defects the gate found (server death mid-send is a traceback; terminal `NAME_TAKEN`
  exits 0) are fixed or filed per the threshold above.
- #30: path 1 is closed by #39's seam (no work here); path 2's pass line holds — every
  `msg_id` exactly once across `messages.log*`, every line valid JSON, the crossing record in
  the live file — under N in-process senders with `MESSAGES_LOG_MAX_BYTES` patched small.

## Fails if

*It is the end of this phase and it failed badly; what happened?*

- **The eval harness never saw the monitor channel.** Weeks went into fixtures under `evals/`
  before anyone proved a user-turn fixture reaches the agent the way a Monitor line does; the
  graders scored the wrong thing and the reaction policy stayed untested with a green report
  saying otherwise. Guard: the spike is the first ticket, it has a kill criterion, and Tier B
  fixtures are not written until it passes.
- **The evals became a CI gate.** Someone wired `make eval` into `ci.yml` "so it runs";
  model nondeterminism made it flap, it got `continue-on-error`, and a report nobody reads is
  worse than the prose check it replaced. Guard: this plan says report-only, `ci.yml` has no
  credential, and the coding-standards entry says why.
- **The account's usage limit ate the phase.** Eval runs on the operator's credential during
  an overnight loop hit the weekly limit, as the 2026-09-15 run did with gates alone, and the
  night was lost. Guard: `make eval` prints its case count and an estimated cost before
  running; overnight manifests list it as Tier 3 (never unattended).
- **"Through the real CLI path" became fiction.** The raw-frame helper made every code
  reachable, the tests went green, and the ticket titles still said "CLI path" — so the one
  thing the titles promised (that a user can hit these codes) was never established. Guard:
  test names carry `_via_cli` / `_via_raw_frame`; #33's title is corrected to the honest count.
- **Tests-only tickets grew fixes with no gate on the fix.** The `NAME_TAKEN` exit code and
  the mid-send traceback were "just fixed while I was there", the fix never went through a
  ticket, and a behaviour change shipped as a test commit. Guard: the ~30-line threshold and
  `xfail(strict=True)` for anything above it.
- **The suite got slow enough that people stopped running it.** Subprocess tests at a second
  each plus a rate-limit test that sleeps its way through 60 sends pushed `make test` past two
  minutes and `make test-fast` became the only thing anyone ran, which skips exactly this tier.
  Guard: the 30 s budget, the in-process rate-limit case, and `@pytest.mark.slow` on every
  subprocess test so the fast suite stays fast on purpose.
- **The contradiction was resolved the easy way.** #35 picked the L175-178 reading because it
  is easier to grade, and the destructive gate got weaker in the one place it was finally
  being tested. Guard: the oracle is named above; the eval's negative case is a first-message
  "yes, go ahead" that must be refused.

## Expected work

- The spike (filed by this plan): can `claude plugin eval` drive the `talk` skill with a
  synthetic monitor line, and what do the graders see. One day, one answer, a kill criterion.
- #35, #34, #33, #30 — each re-gated after its body absorbs the decisions above.
- Probably one split: #33's two defects if they exceed the threshold.

## Out of scope

- The Claude Code layer's *config* (`userConfig` never reaching the monitor, auto-start at
  install): the "Configuration reaches the monitor" phase, blocked on the delivery-route
  decision.
- Rename step 3 (#41): its own release, after the grace period.
- Any new wire op or protocol change: if a test needs one, it is a ticket in Backlog, not a
  fixture.
