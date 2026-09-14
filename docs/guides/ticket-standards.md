<!-- template-version: 6 -->

# Ticket standards (canonical)

This is the **single source of truth** for what a *ready* work ticket must contain. The four
work issue-templates (`feature`, `bug`, `security`, `infrastructure`) carry the form fields
that collect this content; this document holds the **rules and the rationale**. The
`ticket-gate` agent (plugin-registered, `forge-kit-governance`) enforces the rules and reads
this file when it is present.

The `template-version` marker above says *which form set this doc describes*. It is
version-locked to the four templates by `scripts/check-template-lockstep.sh` (run in CI by
`.github/workflows/template-lockstep.yml`), so the standard cannot silently drift apart from
the forms that implement it. Bump it only together with the templates.

## Why single-source

The requirement text used to be restated in each template, in `CLAUDE.md`, and in the gate.
Copies drift: prose says one thing while a template says another, and nobody notices until a
ticket is gated against a stale rule. Keeping the rules here, referenced (not restated)
elsewhere, plus the lockstep guard, makes "the standard is the same everywhere" mechanically
true rather than a matter of discipline.

## Required sections

A ready work ticket must satisfy every rule below whose scope the ticket actually touches.
Applicability is decided by the gate from the ticket type and the areas it affects; a rule a
ticket does not touch is marked N/A with a one-line justification, never failed. A rule that
*does* apply and is absent fails the gate.

This project's templates carry three sections beyond the numbered rules: `invariants`
(feature), `interpreters` and `version_lockstep` (infrastructure). They exist because the
non-obvious invariants in `CLAUDE.md`, the CPython 3.12 / 3.14 split, and the two plugin
manifests are where this codebase has actually broken before. The gate scores them like any
other required field.

### 1. GWT scenarios (Given / When / Then)

At least one positive and one negative scenario per independent condition, written against
the specific wire op (`hello`, `send`, `broadcast`, `rename`, `relabel`, `bye`, `ping`),
entry-point (`client.py`, `server.py`, `send.py`, `list.py`, `relabel.py`, `doctor.py`,
`auto_start.py`), env var, or stdout notification line the ticket makes evident. Vague
restatements of the description do not count.

**Scope:** any ticket with an observable behaviour change. The gate derives this from the
ticket type and the areas it affects; the author never self-declares it. N/A is permitted only
where no behaviour delta exists (a docs-only change, a research spike), and the gate scores
that N/A claim like any other.

**Quality bar** (each point scorable by the gate):

- exactly ONE `When` per scenario; multiple When/Then pairs mean multiple behaviours, split them
- declarative, not step-by-step imperative
- names a real op, script, env var, or notification prefix where the ticket makes one evident
- the negative scenario asserts a SPECIFIC error `code`, exit status, or `[hubbub]`
  notice text, never "it fails"
- not a restatement of the summary

### 2. Unit test specs

Concrete cases: a specific `tests/test_*.py` path, a concrete input value, and the expected
output or error code. "Add unit tests" is not a spec. **When a ticket adds or modifies a wire
op or a `_handle_*` branch in `server.py`**, coverage of that op is enumerated case by case:
happy path; malformed or oversized frame with the specific error `code`; **no token → refused;
wrong token → refused; `role=control` without a matching `for_session` + `nonce` → rejected**;
the cap or rate limit it is subject to; impersonation (session A cannot send as session B). A
single generic auth test does not satisfy this: the three refusal cases are distinct.

Where the change touches `shared.py` or `spawn.py`, the test names the invariant it pins and
the regression it prevents. Tests follow `docs/coding-standards.md` → *Tests*: `tmp_data_dir`,
`free_port`, `HUBBUB_PPID_OVERRIDE` for sibling subprocesses, waits from `tests/waiting.py`
(never a bare `time.sleep()` before an assertion, never a bare `readline()`), and
`coverage_env()` spliced into every new clean-env subprocess call site.

### 3. Integration / subprocess test specs

There is no UI here; the equivalent of E2E is a subprocess test that spawns a real monitor or
server and asserts on its stdout via `read_line`. For any behaviour a running session would
observe, name the test file, the setup (how many monitors, which port fixture), the action,
and the assertion, for both the happy and unhappy paths. **Pure-`shared.py` or docs-only
tickets mark this N/A with justification** rather than inventing a subprocess flow.

The ticket also states which interpreters the test must pass under: a green `make test`
(uv-provisioned 3.14) is not evidence the shipped code is green under `python3` (3.12);
`make test-both` is the bar for anything touching path resolution or subprocess spawning.

<!-- adapt-dropped: emulator-clause -->

### 4. Data handling

This project has no accounts and no regulated personal data: single user, single machine,
localhost only. What it *does* persist is fixed and short, and the ticket states, for each item
it touches, what changes and why: (1) `messages.log` (full peer text, rotating 50 MB × 5);
(2) `clients/<pid>.session` (bearer nonce, port, `session_id`); (3)
`profiles/<sha256(project_root)>.json` (display label); (4) any absolute path or hostname that
would newly reach a log line or a notification header. A ticket that touches none of these
marks this N/A with that reason.

No jurisdiction is named on purpose, and the `privacy-regime` lens is deliberately not
installed: there is no personal-data processing here for it to name a regime for. If that ever
changes — an account, a remote transport, a hosted relay — this section is the one that has to
be rewritten first.

### 5. Security checklist

For every change that lets a peer-controlled string reach the notification header, a log, or
a filesystem path: the boundary reject **and** the render-time neutralisation, both named
(SEC-001/002/003 — one without the other is how each of those was introduced). For every new
op or field: the `role` / `nonce` cross-check, the server-identity verification before the
token is sent, the size cap it falls under (frame 16 MB, direct 10 MB, broadcast 256 KB,
stdout body 400 chars), and the rate limit or a justification for none. Relevant OWASP items
are injection into the header and denial of service through caps.

**When a ticket adds a field to the notification header, the security test has to cover
*that* field** — the SEC-003 lesson is that the regression test covered the field that was
already fixed, not the one being opened.

### 6. Required reviews

The reviews the ticket must pass before it is considered done, checked off explicitly:
`ticket-gate` before implementation. This is the ticket author's acknowledgement of the gate,
not a substitute for it.

### 7. Documentation currency

The ticket names the documentation it affects — `docs/`, `skills/talk/SKILL.md`, the
`CLAUDE.md` invariants, and the root `README.md` — or states none with a reason. **Scope is
deliberately every work ticket**: this is the one rule where always-asked is the point. It
stays passable because "none, no user-visible surface" is a legitimate answer, and the gate
scores that claim like any other N/A, judged against the ticket's own file list. A code
change that leaves the README describing the old behaviour is not finished work.

### 8. Implementation and dependency concreteness

Judged against whichever fields the template provides (`implementation`, `dependencies`,
`files`), not a new form field: the templates already collect this content, so this rule adds
no section and no `template-version` bump.

- File paths and implementation steps are concrete, and match the conventions in
  `docs/coding-standards.md` (the canonical coding standards: style, naming, imports, error
  handling, env vars, filesystem writes, peer strings, tests, interpreters, docs, releases)
  and the invariants in `CLAUDE.md`.
- The build and test commands the ticket relies on are named: `make test`, `make test-both`,
  `make coverage`, or a single `.venv/bin/pytest tests/test_x.py::TestY -v`.
- Every new dependency is justified against the standard library and the two already present
  (`websockets`, `psutil`), and lands in **both** `skills/talk/requirements.txt` and the
  runtime venv path (`install-deps`), or it will be missing under one of the two interpreters.
- Known scalability risks the approach introduces are named: per-message work on the server's
  event loop, `messages.log` growth, reconnect storms, and anything that adds a blocking call
  to `client.py`'s read path.

A ticket whose template carries none of these fields (for example `infrastructure`) records
N/A by domain, per the N/A rule below.

## Precedence

The plugin-registered `ticket-gate` restates parts of this doc (its hard-fail bars, the
security lens checklist, the GWT quality bar) so they hold in projects without it. Those
restatements are convenience copies, never forks. forge-kit verifies its own restatement
inventory against its own gate file in its CI; that inventory is not reproduced here because
this project carries no adapted gate copy for it to anchor into.

Where this doc is installed, its text governs, and three cases are distinguished:

- **Conflict.** The gate's copy and this doc state different things: **this doc wins.**
- **Absence.** A rule is missing from this (adapted-down) doc: that is NOT divergence. Absence
  never relaxes a gate bar; only explicit text here does.
- **A stricter restatement.** The gate says the same thing at finer granularity than this doc.
  The extra strictness is **advisory, never blocking**; the gate reports it as a gap in this
  doc, and the fix is to tighten the doc so the bar becomes legitimately blocking.

## The N/A rule (load-bearing)

A coverage or subprocess-test requirement that a docs-only, research, or pure-`shared.py`
ticket cannot satisfy makes that ticket **un-passable**, which trains people to box-tick and
rots the whole gate. Every rule here is scoped: it applies only to tickets whose type and
affected areas bring it into play, and the gate derives that scope rather than asking the
author to self-declare it. When you add a new rule with a coverage-style requirement, give it
an explicit type-and-area scope here, or it will backfire.
