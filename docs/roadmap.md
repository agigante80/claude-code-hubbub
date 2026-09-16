# hubbub roadmap

Rolling wave planning. This file owns **which phases exist and what state each is in**; GitHub
owns **which phase each ticket is in**, as the milestone. Different facts, so neither duplicates
the other. forge-kit's `roadmap-phases` skill (plugin-registered) is canonical for the rules, and
its `check-phases.sh` enforces four of them; `sync-phases.sh` applies this file to the milestones.

**Tickets closed before this file existed (everything up to #32) carry no phase, by decision on
2026-09-14.** Backfilling them would invent a plan that was never made. Rule 1 governs open
tickets, so the guard is right to ignore them.

Only the `open` phase carries commitment. A `planned` phase's prose below is a reason, never a
promise, and it is a bucket: file tickets against it as they occur, and its plan gets written from
the prose plus whatever accumulated by the time it opens.

## Phase: Rename step 2 and the first-gate defects
state: done
plan: docs/plans/rename-step-2-and-first-gate-defects.md

Closed 2026-09-16, outcome **done**: `v0.3.0` tagged at fa8a3fb, the repo's first tag, with
all six tickets merged (#10 step 2, #36, #37, #38, #39, #40). Reviewed against the
plan's expected work, nothing vanished; one thing changed shape — #10 was split, and step 3 became #41
(Backlog) with a checkable precondition instead of a promise. Work that *appeared*: every
implementation PR's security pass found a low item, each either fixed on the branch or
filed — #48 (server stores the raw label, validates NFC length), #49 (a malformed frame
from a rogue server exits the monitor), #51 (a sustained 5xx refusal is silent when not
verbose) — plus #43 (the one clip measurement not taken). All five sit in Backlog, assigned.
One number in the plan's Done-looks-like moved under it: the header budget is stated in the
code as 500 UTF-16 units measured, not "512", and the body floor as 215/275, not 268.

The `[inter-session …]` stdout prefix is the last identifier still on the old name (#10), and
step 2 of its three-release staging is the one that needs a *release boundary* to be safe — which
this repo has never cut: `0.2.0` was bumped in the manifests but never tagged. This phase is the
first tag. It carries step 2 plus the four defects the first readiness-gate run found in the tree
rather than in the tickets: the worst-case notification header overrunning Claude Code's clip
(#38), a monitor mid-handshake exiting for good on a server shutdown (#39), a relabel reverted by
the next reconnect (#40), and two protocol constants declared but never enforced (#36). Small,
mostly-ready, and each one a thing a user of 0.2.x can hit today. #37 rides along because it is
README-only and wants the same release note.

## Phase: Behaviour under test
state: open
plan: docs/plans/behaviour-under-test.md

The largest untested surface is the Claude Code layer: the reaction policy is checked only as prose
(#35), nothing drives the monitor through a real session (#34), 9 of 10 error codes never cross the
real CLI path (#33), and two server concurrency paths were named but never probed (#30). All four
gated BLOCKED or NEEDS-WORK on the same open question — what harness, at what cost, gating or
reporting — so they are planned together and the plan answers it once.

Opened 2026-09-16. The plan makes the harness call: two tiers — deterministic subprocess
tests in pytest and CI, model-driven `claude plugin eval` cases on demand and report-only —
with a spike (#52) first to prove the eval harness can see a monitor line at all.

## Phase: Configuration reaches the monitor
state: planned
plan: docs/plans/configuration-reaches-the-monitor.md

`userConfig` has never reached the auto-started monitor (#28), and auto-start cannot be surfaced
at install time until it does (#22). Both wait on one decision — the delivery route — which the
gate run on #28 changed the shape of: Claude Code 2.1.270 persists `/plugin config` answers in
`~/.claude/settings.json` under `pluginConfigs[<id>].options`, so a config file the monitor can
read already exists. Decide, then both tickets become ordinary work.

## Phase: Backlog
state: backlog

The permanent holding phase. #9 is an umbrella (peer liveness, lock sweep, cross-transport) the
gate asked to split into children before any of it is implementable; #31 (`doctor --repair`) has
six open design questions and a standing "read-only on purpose" invariant to argue with. Both are
decisions to decide later, and visible as such.
