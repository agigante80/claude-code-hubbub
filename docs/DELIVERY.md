# Delivery semantics, and how messages actually get lost

Written after investigating [issue #9](https://github.com/agigante80/claude-code-inter-session/issues/9)
against real logs from a machine running seven concurrent sessions
(2026-08-14). Read this before proposing "reliability" features — two of
the failure modes people expect the bus to have, it does not have, and
the one that actually bit is not in this codebase at all.

## What the bus guarantees

Delivery is a **write to a live WebSocket**, nothing more:

- `server.py::_resolve_send_target` resolves the target in a single
  locked phase: exact `session_id` → exact `name` → name prefix →
  `session_id` prefix (≥4 chars). Any prefix tie returns `AMBIGUOUS`
  *with the candidate list*; no match returns `UNKNOWN_PEER`.
- On success the frame is written to the peer's socket and appended to
  `messages.log`.

So within the bus there is **no silent misdelivery and no silent drop**.
A sender that addresses a name nobody holds gets an error frame back.

## What it does not guarantee

- **No offline queue.** A peer that is not connected cannot be sent to —
  the send fails immediately rather than being stored. There is no such
  thing as an undrained queue on this bus.
- **No processing ack.** The bus confirms the socket write, not that the
  receiving agent read the notification, understood it, or acted. A
  registry entry means "a monitor process holds an open connection",
  which is weaker than "a session is paying attention".
- **No liveness signal in `list`.** Rows carry `session_id`, `name`,
  `label`, `cwd`, `since` — an age, not a last-activity time. A peer
  connected 124 hours ago and a peer that answered a second ago look
  identical.
- **Names are not stable identities.** A name belongs to whoever holds
  it *now*. On the machine studied, `arivit` had been held by six
  different `session_id`s since 2026-07-12 as sessions restarted. A name
  you were given yesterday may address a different conversation today;
  `session_id` is the stable handle.

## The failure that actually happened

Both real incidents were **cross-transport**, not bus failures.

A machine can run two agent-messaging systems at once: this bus, and
Claude Code's own peer messaging (`ListAgents` / `SendMessage`, Remote
Control). They are separate namespaces that cannot see each other, and
the same project routinely has a live session in *each*:

| Transport | Roster entry | Process |
| :-------- | :----------- | :------ |
| this bus  | `arivit-social` | `claude --remote-control arivit-social` |
| harness   | `arivit-social-b1 [3c9090]` | interactive session in tmux `cc-arivit-social` |

Different sessions. Different conversations. Near-identical names.

**Incident 1 (2026-08-14, reproduced end to end).** A session received a
question over the bus at 12:18:30, had it in context at 12:18:31, and
answered at 12:20:27 — using the harness's `SendMessage`, addressed to
`arivit-social-b1 [3c9090]`. The bus peer that asked never got a reply.
Neither side saw an error. This is what the
[reply-on-the-same-transport rule](../skills/talk/SKILL.md)
exists to prevent, and `tests/test_reaction_policy.py` keeps that rule
in the prose.

**Incident 2 (2026-08-10, the original 4-day report).** Two messages
sent through the harness were *held* on arrival rather than delivered:

```
Held peer message — from an unidentified session [verified pid 2955486]
(peer claims name: Understand project overview); preview: «Hola: t…»
```

The receiving harness verified the sender's pid correctly but resolved
its *name* from the process title — which Claude Code sets to the
current task summary ("Understand project overview"), not to anything
identity-shaped. Unmatched, so the message was held; the sender was
never told. Five such events appear across that machine's transcripts.

This is a Claude Code harness behaviour, not something this project can
fix — but note that **this repo hit the same trap and solved it**: see
the `find_cc_ancestor_pid` invariant in `CLAUDE.md`, which matches on
`cmdline[0]` precisely because `Process.name()` returns the proctitle.

## Consequences for anyone extending this

- Don't add retries or dead-lettering to paper over "no ack" — the
  useful primitive is an explicit application-level reply (`done:`,
  `answer:`), which the reaction policy already mandates.
- If you add a liveness column to `list`, make it *last activity*, not
  uptime; uptime is what misleads senders today.
- Anything that resolves a peer by name should say which namespace the
  name came from. Most real-world confusion is one project appearing
  twice under two spellings.
