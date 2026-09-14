# Plan: Rename step 2 and the first-gate defects

## Goal

Ship the first tagged release of hubbub, carrying the emitter half of the prefix rename and the
four defects the first readiness-gate run found in the tree.

## Done looks like

- A `v0.3.0` tag on `main`, the two plugin manifests at `0.3.0` in the same commit, CI green at it.
- `client.py` and `shared.py` emit `[hubbub …]` everywhere (21 literals, none mixed);
  `test_emitter_has_not_moved_yet` deleted in that commit; the policy still accepts both spellings
  (step 3 stays out — see Out of scope).
- The worst-case notification header fits inside 512 characters, with a test that builds the
  worst case (#38).
- A monitor whose handshake is cut by a server shutdown re-elects instead of printing
  "non-inter-session service" and exiting (#39).
- A relabel survives a reconnect (#40).
- `HOP_LIMIT` / `MAX_HOPS` (and the two same-shaped constants the gate named) are either enforced
  or gone, with a guard that every `ErrorCode` has a producer (#36).
- `README.md` Sponsor section and `.github/FUNDING.yml` (#37), with the fork decision recorded.

## Fails if

*It is the end of this phase and it failed badly; what happened?*

- **The tag went on before the emitter flip was staged, and a 0.2.x monitor met a 0.3.0 policy.**
  Nothing broke loudly: the agent simply stopped recognising peer messages, exactly the silent
  failure the three-release staging exists to prevent. Cause: step 2 and step 3 were done in one
  sweep because step 3 "looked like a one-liner". Guard: `TestPrefixRenameStaging` goes red on a
  mixed emitter, and the plan keeps step 3 out of scope by name.
- **The header fix changed the wire-visible header shape** to make room, and the reaction policy's
  parser (and every `test_reaction_policy` fixture) had to move in the same release as the
  spelling — two contract changes in one tag, and a bug report could not say which one bit.
  Guard: #38's option 1 (shrink the body, not the header) is the one that keeps the header
  contract; anything else is a separate phase.
- **#39's fix taught the monitor to treat every 5xx as "transient, retry"**, and a real port
  squatter that answers 503 kept the monitor in a reconnect loop forever, with the bearer token
  never sent (good) but no diagnostic either (bad). Guard: the acceptance criterion says the
  transient case is only taken when the identity file *matches*.
- **The phase absorbed #14** (rename in place) because it touches the same `renamed` path as #40,
  and a small release became a protocol change. Guard: #14 is in Out of scope with its phase named.
- **Both interpreters were not actually run before the tag.** `make test` was green on 3.14 and the
  shipped 3.12 monitors crashed at startup, as in #19. Guard: CI's "both interpreters were actually
  different" step, green since `f92e961`; the release commit is the one that must not be pushed
  with CI skipped.

## Expected work

#10 (step 2 only), #36, #37, #38, #39, #40, and the version-bump commit with its tag. Each has a
gate review on it with the required changes; re-run `/gate-ticket <n>` after addressing them and
implement on PASS.

## Out of scope

- Step 3 of the rename (drop the legacy spelling from the policy): a release *after* this one, by
  design. Ticket to be split off #10 once step 2 lands, per the gate's advisory → next phase after
  this closes, or `backlog`.
- #14 rename in place: protocol-adjacent, wants its own release → `backlog` until re-shaped.
- Everything under **Behaviour under test** and **Configuration reaches the monitor**: their own
  phases.
