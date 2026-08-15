# Security tickets

Findings from the 2026-07-12 security review of the runtime source
(`skills/talk/bin/*.py`). Threat model per the project README: single
user, single machine, same-UID code trusted; peer sessions are semi-trusted
(another session's LLM may itself be prompt-injected), and the code deliberately
tries to keep the receiving-session notification line's sender attribution
un-forgeable.

| ID | Title | Reviewed severity | Status |
| :--- | :--- | :--- | :--- |
| [SEC-001](SEC-001-unescaped-label-header-spoofing.md) | Peer `label` not escaped → notification-header / sender spoofing | Low (Medium arguable) | **Fixed in this fork** |
| [SEC-002](SEC-002-unescaped-text-structural-chars.md) | Message `text` structural chars not escaped → forged trailing directive | Low / Informational | **Fixed in this fork** |

Both were `Open` here long after they were fixed, which is its own small
hazard — a reader checking whether they are exposed would have concluded
yes. SEC-001 is closed at two layers: `validate_label` rejects
`LABEL_FORBIDDEN_CHARS` (`"`, `[`, `]`) at the boundary and
`sanitize_label_for_display` re-neutralises them to look-alikes at render,
the redundancy covering labels that never passed live validation.
SEC-002 is closed by the reaction policy stating that only the *leading*
header is authoritative. Regression tests:
`test_client.py::test_label_cannot_forge_header` and
`test_reaction_policy.py::test_only_leading_header_is_authoritative`.

Both are still open **upstream** (PRs #8 and #9 on
`yilunzhang/claude-code-inter-session`, unreviewed since 2026-07-12), which
is presumably how the status drifted.

## Restated for always-on (2026-08-15)

Everything above and below was written when joining the bus was **per-session
opt-in**: a session was reachable only once its own user invoked the skill.
Since `0.2.0` the shipped `monitors.json` says `when: "always"`, so every
Claude Code session on the machine joins at open.

**No per-message guardrail changed.** Label escaping, leading-header
authority, the confirm-first gate on destructive ops, the bearer token, the
`role=control` nonce cross-check — all unchanged. Peers were already treated
as semi-trusted, on the assumption that another session's LLM may itself be
prompt-injected. That assumption is unaffected by how many sessions there are.

**What changed is blast radius**, and it is worth stating plainly rather than
leaving implied:

1. A single prompt-injected session can address *every* session on the host,
   not only those that opted in.
2. The reachable set is no longer implicitly curated. Previously the user
   chose where to run `/hubbub:talk`; now sessions join in projects where the
   user may not know a monitor is running — while the reaction policy's
   default is to act on peer messages as if the user typed them.
3. Sessions that auto-join and are never used still hold a WebSocket and
   appear in `list`, so a peer can address a session nobody is watching.

**This is a deliberate trade, and as of `0.2.0` an explicitly consented one.**
A bus you must remember to join is a bus nobody is on when a peer needs them.
The install prompt now asks about it directly (`auto_start`, fork #22), so
leaving it on is a choice the user makes rather than a default they have to
discover. Beyond that point the exposure is the user's to accept: the tool's
entire purpose is letting one session drive another, and that cannot be made
safe against a user who does not want it.

Reversible per machine at any time with `/hubbub:talk auto-start off`, which
is durable across `/plugin update`.

**One genuinely new surface**, noted for completeness: the opt-out is a
presence check on `<data-dir>/autostart-off`, so any same-UID process can
create it and silence every monitor on the machine. That is consistent with
the same-UID trust model — such a process could equally read the token — but
it is a denial-of-service vector that did not exist when joining was opt-in,
and it fails *silently*: a suppressed session is indistinguishable from one
nobody messaged.

## Not ticketed (checked and cleared)

- **Bearer token** — 256-bit `secrets.token_urlsafe(32)`, `0600`, `O_EXCL` create,
  symlink-refusing. Non-constant-time compare not attackable over localhost.
- **role=control impersonation** — gated by `for_session` + `nonce`; nonce is never
  echoed in any frame, lives only in the `0600` state file.
- **`verify_server_identity`** — fails closed on missing/dead/mismatched pidfile
  and on cmdline lacking `bin/server.py`.
- **`cwd`** — peer-controlled but sanitized server-side before storage
  (`server.py:318`); not vulnerable (contrast SEC-001, where `label` is not).
- **Subprocess / deserialization** — all `json.loads`; `spawn.py`/`discover.py` use
  list-form args, no shell.
- **Size caps** — `TEXT_CAP` 10 MB, broadcast 256 KB, frame 16 MB, codepoint-safe
  truncation.

## Related (non-security, tracked elsewhere)

- Server-election identity-wipe race (two simultaneous clients both "win" the
  `bind()`, the loser's cleanup deletes the winner's pidfile → `server identity
  check failed`). Reliability/availability bug, not in scope for this security
  review; documented in the top-level `CLAUDE.md`. Surfaces as four failing
  two-listener tests.
