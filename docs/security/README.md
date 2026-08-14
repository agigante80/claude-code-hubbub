# Security tickets

Findings from the 2026-07-12 security review of the runtime source
(`skills/talk/bin/*.py`). Threat model per the project README: single
user, single machine, same-UID code trusted; peer sessions are semi-trusted
(another session's LLM may itself be prompt-injected), and the code deliberately
tries to keep the receiving-session notification line's sender attribution
un-forgeable.

| ID | Title | Reviewed severity | Status |
| :--- | :--- | :--- | :--- |
| [SEC-001](SEC-001-unescaped-label-header-spoofing.md) | Peer `label` not escaped → notification-header / sender spoofing | Low (Medium arguable) | Open |
| [SEC-002](SEC-002-unescaped-text-structural-chars.md) | Message `text` structural chars not escaped → forged trailing directive | Low / Informational | Open |

## Threat-model change in `0.2.0`: auto-start is on by default

The review above assumed **joining the bus is per-session opt-in** — a
session was only reachable once its own user invoked the skill. Since
`0.2.0` the shipped `monitors.json` uses `when: "always"`, so every
Claude Code session on the machine joins at open, including sessions
whose user has never used hubbub and does not know it is running.

Nothing about the per-message guardrails changed: SEC-001's label
escaping, SEC-002's "only the leading header is authoritative" rule, and
the confirm-first gate on destructive operations all still apply
unchanged, and peers are still semi-trusted rather than trusted. What
changed is **blast radius**. A single prompt-injected session can now
address every other session on the host rather than only those that had
opted in, and the reachable set is no longer something the user
implicitly curated by choosing where to run `/hubbub:talk`.

That is a deliberate trade for reachability — a bus you have to remember
to join is a bus nobody is on when a peer needs them — and it is
reversible per machine with `/hubbub:talk auto-start off`, which is now
durable across plugin updates. It is recorded here because the tickets
below were written against the opt-in assumption, and anyone re-reading
them should know it no longer holds.

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
