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
state: open
plan: docs/plans/rename-step-2-and-first-gate-defects.md

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
state: planned
plan: docs/plans/behaviour-under-test.md

The largest untested surface is the Claude Code layer: the reaction policy is checked only as prose
(#35), nothing drives the monitor through a real session (#34), 8 of 11 error codes never cross the
real CLI path (#33), and two server concurrency paths were named but never probed (#30). All four
gated BLOCKED or NEEDS-WORK on the same open question — what harness, at what cost, gating or
reporting — so they are planned together and the plan answers it once.

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
