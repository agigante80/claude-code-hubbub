# Coding standards

> Canonical coding standards for this project: how code, tests, docs and
> releases are written here. Derived from the code as it is, not aspirational.
> Enforced by: no linter or formatter. What *is* mechanical: `pytest.ini`
> (`--strict-markers`), `.coveragerc` (80% floor), `tests/conftest.py`
> (skips fail the run), CI (`make test-both` with an interpreter-difference
> check, `make coverage`, manifest version lockstep, template lockstep).
> Everything else is manual review against this file.
> Last updated: 2026-09-14

**Boundary with `CLAUDE.md`.** That file holds the *architectural invariants*
(server election, the half-done rename, the data-dir migration, how state
files are keyed, what userConfig does and does not deliver, the two venvs, the
label/name split, the sanitisation layers) and the env-var table. This file
holds the *conventions* those invariants impose on anyone writing code. Where a
rule below exists because of an invariant, it links there rather than
restating it. Nothing is meant to be stated in both places; if you find a
duplicate, delete the copy here and link.

## Stack and layout

- Python only. The runtime is stdlib plus `websockets` and `psutil`
  (`skills/talk/requirements.txt`); dev adds `pytest`, `pytest-asyncio`,
  `coverage` (`requirements-dev.txt`). No build step.
- **Code must run under both CPython 3.12 and 3.14.** The suite runs 3.14
  (uv-provisioned `.venv`); the shipped monitors run the system `python3`,
  3.12 on the maintainer's machine. See *Interpreters* below.
- All runtime code lives in `skills/talk/bin/` and is imported as a package
  named `bin`: `from bin import shared`, `from bin import shared, spawn`.
  Never `import shared` bare, never relative imports. The skill directory must
  stay self-contained (a symlink of `skills/talk/` is a working standalone
  install), so nothing under `bin/` imports from outside it.
- Tests live in `tests/`, named `test_<subject>.py`: one per runtime module
  where the module is a unit (`test_shared.py`, `test_server.py`,
  `test_client.py`, `test_profile.py`, `test_doctor.py`,
  `test_auto_start.py`), `test_helpers.py` for the three control CLIs
  together, and `test_reaction_policy.py` / `test_plugin_manifest.py` for
  prose and manifests. `spawn.py` and `discover.py` are exercised through
  the client and helper tests rather than in isolation. Shared test helpers
  go in `tests/waiting.py`, not copied per file.

## Formatting

There is no formatter and no linter, on purpose: the codebase predates the
choice and a reformat commit would bury the history behind every line. Don't
add one as tidy-up; if one is ever adopted, it lands in its own commit that
changes nothing else. Until then, the de-facto style is:

- PEP 8 layout: 4-space indent, two blank lines between top-level
  definitions, one between methods.
- **Double quotes** for strings; single quotes only to avoid escaping a
  double quote inside (`'"'`, `'[a-z]"'`).
- Lines wrap at **88** columns as the working limit; up to ~100 is tolerated
  for a long string or a table-like literal (the longest line in the tree is
  109). Nothing over 120.
- Continuation lines use PEP 8 hanging or aligned indents, never backslashes.
  Long `f`-strings are split into adjacent literals inside parentheses.
- `# noqa: <code>` and `# type: ignore[<code>]` always carry the code, and a
  comment saying why, even though no tool reads them yet.

## Naming

- `snake_case` for functions, methods, variables and modules; `PascalCase`
  for classes (`Server`, `Role`, `ErrorCode`); `SCREAMING_SNAKE_CASE` for
  module constants.
- Leading underscore for anything module-private, including module-level
  state (`_MISSING_DEP`, `_BUFFERS`, `_LABEL_STRUCTURAL`). Runtime modules
  expose few public names and the helpers reach across only through them.
- **Protocol constants and limits live in `shared.py`**, not next to the
  code that enforces them: caps end in `_CAP`, durations in `_S`, compiled
  regexes in `_RE`, path helpers are `*_path()` or `*_dir()` functions
  returning `Path`. `server.py` enforces; `shared.py` owns the number.
- Wire-level strings are plain lowercase identifiers: ops (`hello`, `send`,
  `bye`), roles (`Role.AGENT == "agent"`), error codes
  (`ErrorCode.NAME_TAKEN == "name_taken"`). Add to the enum or the class;
  never emit a bare literal on the wire.
- Loggers are `logging.getLogger("hubbub.<module>")`.
- Env vars are `HUBBUB_<KEY>`, read as `shared.env("KEY")` (see
  *Environment variables*).
- Tests: `class Test<Subject>` grouping `def test_<what_it_pins>`. There are
  no top-level test functions in the suite; keep it that way. Name a
  regression test for the behaviour it protects, not the bug number
  (`test_emitter_never_mixes_the_two_spellings`, not `test_issue_27`); the
  number goes in the docstring.

## Module structure and imports

Every module opens with a docstring, then `from __future__ import
annotations`. Import order is stdlib, blank line, third-party, blank line,
`from bin import …`, with each group alphabetical. Two deliberate deviations,
both load-bearing:

1. **The re-exec bootstrap** in the six entry-points (`client.py`,
   `server.py`, `send.py`, `list.py`, `relabel.py`, `doctor.py`) sits
   *between* `from __future__` and the rest of the imports, and imports only
   `os`, `sys`, `Path`. It must run before anything that could pull in
   `websockets` or `psutil`, because it is what selects the interpreter that
   has them. From `_DATA = …` to `os.execv(…)` the block is **byte-identical
   across all six** — copy it from an existing file, never retype it, and a
   change to it touches all six in the same commit. It spells both
   `HUBBUB_NO_REEXEC` and `INTER_SESSION_NO_REEXEC` by hand because
   `shared.env()` is not importable yet at that point; that is the one place
   the two spellings appear outside `shared.env()`. `auto_start.py` and
   `discover.py` do not re-exec; a new entry-point decides which group it
   joins and says why in its docstring. Why the bootstrap exists at all is
   `CLAUDE.md` → *Two venvs*.
2. **Third-party imports are guarded at module scope** in the helpers:

   ```python
   _MISSING_DEP: ImportError | None = None
   try:
       import websockets
       import psutil  # noqa: F401
   except ImportError as _e:
       websockets = None  # type: ignore[assignment]
       _MISSING_DEP = _e
   ```

   `main()` then prints the "dependencies missing" line the skill knows how
   to react to. A bare `import websockets` would traceback before `main()`
   runs and the agent would never see the friendly line. `psutil` is guarded
   too even where it is only imported lazily later, because a half-installed
   runtime degrades silently otherwise (the ppid walk falls back to
   `getppid()` and self-discovery breaks).

Scripts that can be run directly carry the `_REPO_ROOT` shim
(`sys.path.insert(0, str(Path(__file__).resolve().parent.parent))`) so
`from bin import …` resolves; tests get the same from `tests/conftest.py`.

## Type hints and docstrings

- Runtime code annotates every function signature, including `-> None`.
  Test helpers are annotated where it helps a reader and not otherwise.
  Local variables are annotated only when the type is not obvious from the
  assignment (`seen: set[int] = set()`).
- New code writes optionals as `X | None`; `Optional[X]` survives in the
  older modules (`server.py`, `discover.py`, `profile.py`) and is not worth
  a churn commit. Don't mix the two spellings within one function.
- Docstrings are free prose: no Google, NumPy or Sphinx sections, no
  `:param:` lines. First line states what the function does; the rest is for
  *why* and for the trap the next reader will otherwise fall into. Types live
  in the signature, not the docstring.
- No type checker runs. `# type: ignore[assignment]` is used where the
  intent would otherwise be unclear to a reader, not to satisfy a tool.

## Comments

Comments in this codebase record decisions, and the bar is that a reader who
disagrees with a line can see what it was weighed against.

- **Explain why, not what.** A comment that restates the code is deleted in
  review. The typical comment here is a paragraph.
- **Cite the origin**: `fork #N`, `issue #N`, `#N`, `SEC-00N`, or a short
  commit hash, at the point where the code exists because of it. The bare
  `#N` form is used once the reader knows the context.
- **When you reverse an earlier decision, write the rationale into the code
  at that spot.** Reviews here have no memory of the last round; an
  un-commented reversal gets re-litigated. The same applies to something that
  was tried and reverted (`Don't add a list.py --self pre-check back into the
  connect step — that was tried and reverted because …`).
- A `Don't …` in a comment is a guardrail, not style: it names a change that
  looks like a simplification and is not. Keep them terse and keep the reason
  attached.
- Comments and docstrings are English, sentence-cased, with real punctuation.

## Error handling

- **No custom exception classes.** Failures inside a process propagate as the
  stdlib exception that occurred; failures across the wire are `error`
  frames carrying a `shared.ErrorCode` constant. Add a code to that class
  before emitting a new one; never send an ad-hoc string.
- Catch the exception you mean: `OSError`, `json.JSONDecodeError`,
  `ImportError`, `websockets.ConnectionClosed`. `except Exception` is
  permitted at exactly three shapes, each with a comment: the top of a
  long-running handler or main loop (`log.exception(…)` and continue); a
  cleanup arm that must not mask the original error (`close(); unlink();
  raise`); and a best-effort peer close whose failure is uninteresting.
  Never a bare `except:`. There are none in the tree.
- **`Path.resolve()` is not total under 3.12** (it raises `RuntimeError` on
  a symlink loop, which no `except OSError` catches). Anything that resolves
  a path under the data dir, `$HOME`, or a user-supplied argument goes
  through `shared.resolve_safe()`, which returns `None` instead. Direct
  `.resolve()` is reserved for paths the process owns: `__file__`,
  `sys.prefix`, the venv path in the bootstrap.
- Use `os.lstat` / `Path.lexists()` when the question is "is there a
  filesystem entry here", and `exists()` only when following the symlink is
  the intent. `shared.py` has had that bug three separate times.
- Helpers that can fail benignly return `None`/`False` rather than raising
  (`secure_dir`, `resolve_safe`, `find_listener_state`, `verify_server_identity`)
  and do not log; the caller decides whether that is fatal and is the one
  that reports it, once. Don't log and re-raise the same failure.

## Output: stdout is a wire contract, stderr and logging are not

- **`client.py`'s stdout is the notification channel** that Claude Code
  turns into agent notifications. Every line it writes goes through
  `_print_line()` (write + flush) and begins with the literal
  `[inter-session …]` prefix: `[inter-session msg=…]` headers for messages,
  `[inter-session]` for operational notices. That prefix is the contract with
  the reaction policy in `SKILL.md`; the count of literals is pinned by
  `tests/test_reaction_policy.py::TestPrefixRenameStaging`. A new notice
  copies an existing prefix literal exactly, and the count in `CLAUDE.md` →
  *The rename is deliberately half-done* is updated. Don't spell the prefix
  from a variable, and don't move it to `[hubbub …]` on your own — the flip
  is a three-step release plan documented there.
- Anything that is not a notification (`logging`, debugging, progress) goes
  to **stderr** or the `hubbub.<module>` logger. Nothing else may write to
  the monitor's stdout.
- **Helper CLIs** (`send.py`, `list.py`, `relabel.py`, `doctor.py`,
  `auto_start.py`) print human-readable results to stdout, errors to stderr
  with `print(msg, file=sys.stderr)`, and exit via `sys.exit(main())` where
  `main() -> int` returns `0`/`1`. They do not log; they are short-lived.
- Anything peer-controlled that reaches stdout passes through
  `shared.sanitize_for_stdout()` (control characters, ANSI, newlines) first —
  see *Peer-controlled strings*.

## Environment variables

- **Every env var read goes through `shared.env("KEY")`**, which tries
  `HUBBUB_KEY` then the legacy `INTER_SESSION_KEY`. Never
  `os.environ.get("HUBBUB_…")` directly in runtime code; the one exception
  is the re-exec bootstrap (see *Module structure*), which runs before
  `shared` is importable and therefore spells both by hand. A consequence
  for readers: grepping for a literal `HUBBUB_FOO` misses the
  `shared.env("FOO")` call sites — grep for the key.
- Both spellings are honoured indefinitely. Don't remove the fallback and
  don't add a deprecation warning for the old one.
- A new variable gets a row in `CLAUDE.md` → *Every env var, in one place*
  (var, which module reads it, purpose), and, if a user is expected to set
  it, a mention in `README.md`. The table is the only documentation some of
  them have.
- `send.py` and `list.py` do not read the port from the environment, by
  design; the reason is under that table.
- Tests set the `HUBBUB_` spelling and, where it matters, `delenv` the
  `INTER_SESSION_` one (`tmp_data_dir` does this), so a stray export in the
  developer's shell cannot leak in. Older subprocess helpers still set
  `INTER_SESSION_*` in the child's env and work through the fallback; new
  helpers use `HUBBUB_*`.

## Filesystem state

Everything under the data dir is single-user, mode-restricted, and can be
read by a sibling process at any moment.

- Create directories with `shared.secure_dir()` (0o700) before writing into
  them; tighten files with `shared.secure_file()` (0o600) or by passing
  `mode=` to the write helper.
- **Anything a reader could observe mid-write uses
  `shared.atomic_write_text()`** (tempfile in the same directory, `fchmod`,
  `fsync`, `os.replace`). That is every state file: `clients/<key>.session`,
  profiles, `monitors.json`. A plain `write_text()` is acceptable only for a
  file whose readers are ordered by something else — the server pidfile is
  written with `write_text()` because it is written *before* `listen()`, so
  no client can be probing it yet (the ordering is an invariant, see
  `CLAUDE.md` → *Server election*).
- Secrets are created with `os.open(…, O_CREAT | O_EXCL | O_WRONLY, 0o600)`
  (`shared.ensure_token`), never read-modify-write.
- Cross-process mutual exclusion is `fcntl.flock`, non-blocking and polled
  with a deadline, never a blocking `LOCK_EX` (a stuck holder must not wedge
  the caller). Lock files live under the data dir with a `*_lock_path()`
  helper in `shared.py`.
- **`shared.data_dir()` is pure**: it never touches the filesystem. The
  migration it might trigger is called explicitly by each entry-point's
  `main()`. `test_data_dir_has_no_filesystem_side_effects` guards this;
  `CLAUDE.md` → *The data-dir migration* says why it matters.
- Never derive a filesystem name from a caller-supplied string. Profiles are
  keyed by `sha256(project_root)[:32]`; state files by a pid the code
  resolved itself.

## Peer-controlled strings

`name`, `label`, `session_id` and `text` arrive from a peer. The full
security reasoning is in `docs/security/` and `CLAUDE.md` → *Peer-controlled
strings that reach the header are sanitized twice*; the coding rule it
imposes is:

- **Every peer field gets two layers**: a boundary reject (`validate_*` in
  `shared.py`, returning `bool`, enforced by `server.py::_handle_*` with a
  specific `ErrorCode`) *and* a render-time neutralisation at the point it
  is interpolated into stdout or the `list` table (`sanitize_for_stdout`,
  `sanitize_label_for_display`, `short_session_id`). One without the other
  is how SEC-001, SEC-002 and SEC-003 were each introduced. The render layer
  is not redundant: it covers values persisted by an older client, replayed
  from `messages.log`, or reaching `_format_msg` by a path that never
  validated.
- `validate_*` functions type-check first (`if not isinstance(s, str):
  return False`) — the value came off a JSON frame and may be anything.
- `name` is the routing handle and is strict ASCII (`NAME_RE`); `label` is
  display-only Unicode, NFC-normalised, category-restricted. Don't route by
  label, don't relax `NAME_RE`.
- **When you add a field to the notification header, the security test must
  cover *that* field.** The SEC-003 regression test covered the label path
  while the new `sid=` field was wide open. Add a case to the existing
  header-injection tests in `tests/test_shared.py` / `tests/test_client.py`
  for the new field, with a payload containing `\n`, `[`, `"` and a
  control character.

## Tests

The suite is ~500 tests in ~70 s, 20 of them `@pytest.mark.slow`. Async
tests need no marker (`asyncio_mode = auto`); a new marker must be registered
in `pytest.ini` (`--strict-markers`).

### Fixtures and isolation

- **State isolation**: the `tmp_data_dir` fixture sets
  `HUBBUB_DATA_DIR` to a per-test temp path so the suite never
  touches `~/.claude/data/hubbub/`. Any test that resolves a data-dir path
  takes it, or it inherits the developer's real `$HOME`.
- **Free ports**: the `free_port` fixture binds port `0` to find an
  ephemeral port. **Port isolation is very good, not absolute.** `free_port`
  *closes* the socket before returning the number, so a concurrent session
  can be handed the same port. On a collision one session's client probes
  the other's server and fails `verify_server_identity` (different data
  dir → different token and pidfile), surfacing as `server identity check
  failed`. Rare, and it reads as a product bug when it happens.
- **PPID override**: subprocesses spawned in a single test share the
  pytest parent pid, which would collide on the ppid flock. Set
  `HUBBUB_PPID_OVERRIDE` to give each subprocess a distinct
  pseudo-ppid.
- **Collision-retry budget**: `HUBBUB_MAX_COLLISION_RETRIES` (default 3).
  Set it to `0` to make the first name collision terminal, which is the
  only deterministic way to exercise retry exhaustion — otherwise you
  need four sessions racing one cwd-derived name and the outcome depends
  on their interleaving. Same shape and purpose as the ppid override.
- **Re-exec is off**: `tests/conftest.py` sets `HUBBUB_NO_REEXEC=1`
  process-wide. Set the same for any manual repro, or the script you are
  debugging re-execs into the runtime venv and you debug the wrong
  dependencies.
- Process-global state in `shared.py` (`_unmigrated_this_run`) is reset by
  an autouse fixture. If you add module-level mutable state, add it to that
  fixture.

### Waits and deadlines

Two concurrent pytest sessions are **expected to pass** (fork #17). Each
session gets its own `tmp_data_dir`, so they do not contend for *state*; what
broke them was contention for *CPU*, against subprocess tests that asserted on
fixed `time.sleep()` durations.

The rule is therefore **don't assert on a sleep, and never read a pipe
without an enforceable deadline**. A blocking `readline()` in a loop that
checks its deadline only *between* reads cannot honour it — the read that
never returns is exactly the one the timeout is for — so a dropped message
hung the whole suite instead of failing it, with no assertion message and the
enclosing `finally` never reaping the subprocesses.

- **The shared waits live in `tests/waiting.py`**: `wait_for(predicate)` for
  a condition, `read_line(proc)` for a pipe read with a deadline it can
  actually enforce. Use them; the identical bug has been found three times
  in this suite (#17, #23, #27) and each time only the copy that failed got
  fixed. The only acceptable fixed `time.sleep` is a poll interval inside a
  wait loop — none precedes an assertion.
- `read_line` owns `proc.stdout`; don't mix it with `readline()`, `read()`
  or `communicate()` on the same process.
- A wait helper that returns `None` on timeout must be **asserted on by the
  caller**; a silent `None` turns "never registered" into a confusing
  failure fifteen seconds later.
- A wait helper's `timeout` defaults to `waiting.DEFAULT_TIMEOUT` (15 s),
  not a private number, so every wait in a test is generous by the same
  amount; `_wait_for_state` was 5 s while everything around it waited 15.

### Subprocess tests

- **Slow tests** (`@pytest.mark.slow`): subprocess-spawning, >1 s. Mark
  them so `make test-fast` can skip them.
- Spawn with `sys.executable`, never `python3`, so the child runs the
  interpreter the suite chose.
- Build the child's env one of two ways. *Inherit* (`os.environ.copy()` +
  overrides) when the test needs the developer's `PATH`; *clean* (`{"PATH":
  "/usr/bin:/bin", "HUBBUB_NO_REEXEC": "1", …}`) when the test must prove
  nothing leaks in. **A clean env must splice in
  `**waiting.coverage_env()`**, which is empty outside a coverage run;
  without it the child is invisible to `make coverage` and the module it
  exercises silently reads as 0%. Inherited envs pass the coverage hook
  through on their own.
- Long-lived children (`subprocess.Popen`) get `stdin=subprocess.DEVNULL`,
  `stdout=PIPE`, `stderr=PIPE`, `text=True`, and are reaped in a `finally`
  (`terminate()`, then `wait(timeout=…)`). One-shot helpers use
  `subprocess.run(…, capture_output=True, text=True, timeout=…)` with an
  explicit timeout, never an unbounded one.
- Never `pkill -f 'bin/(client|server).py'` during local testing — it kills
  real user monitors in other Claude Code sessions. Target specific pids via
  the pidfile (`~/.claude/data/hubbub/server.<port>.pid`) or
  `TaskList()`-derived monitor task IDs.
- Tests that touch a *tracked* file (`test_auto_start.py` captures and
  restores the real `monitors/monitors.json`) must restore it in a
  `finally`, and the Makefile's `.NOTPARALLEL` exists because two such tests
  interleaving would leave the tracked file mutated.

### What a test is for

- A regression test's docstring names the invariant it pins and the failure
  it prevents, with the fork/issue number. When a guard is a *deliberately
  backwards* assertion (e.g. `test_emitter_has_not_moved_yet`), the
  docstring says which commit is expected to delete it.
- Prose that carries behaviour (`SKILL.md`'s reaction policy) is pinned by
  static tests in `tests/test_reaction_policy.py`; a guardrail added to the
  prose gets a check there, or an edit can drop it unnoticed.
- When a fix touches one of several copies of a pattern, the test covers
  the copy that was *not* fixed — the SEC-003 and `TestPrefixRenameStaging`
  lessons are both "the guard covered the field that was already fixed".
- **Skips fail the run.** `conftest.pytest_sessionfinish` turns any skipped
  test into a red build, because `495 passed, 1 skipped` reads as success and
  the test that did not run is the one nobody looks at. The only conditional
  skips here are guarded on `os.geteuid() == 0` — they need `chmod 000` to
  actually deny access, and root bypasses DAC — so **run the suite as a
  non-root user**, which is what keeps the count at zero. `--allow-skips` is
  the escape hatch when a skip is genuinely intended.

## Interpreters

The suite runs CPython 3.14 (uv-provisioned `.venv`) while the shipped
monitors run whatever `python3` resolves to — 3.12 on the maintainer's
machine. That difference is not cosmetic: `Path.resolve()` raises
`RuntimeError` on a symlink loop in 3.12 and silently returns the link in
3.14, which hid a real startup crash from `make test` until #19.

The cause is in the Makefile: `make test` bootstraps `.venv` with uv when uv
is present, and asks it for **`UV_PY` (3.14)**, which uv downloads — so `.venv`
is never the system Python. Without uv it falls back to `python3 -m venv` and
the two agree — so whether your suite matches production depends on whether
you have uv installed, which is not a property anyone reasons about. The pin
is load-bearing: an unpinned `uv venv` accepts any interpreter it finds, and
on a fresh CI runner that is the system 3.12, which is exactly what CI's
"both interpreters were actually different" step caught on its first two runs.

So **a green `make test` is not by itself evidence that the shipped code is
green.** `make test-system` (#24) builds a second venv, `.venv-system`, from
`python3` explicitly and never uv, and runs the same suite there. **Use
`make test-both` before shipping, and always for changes touching path
resolution, subprocess spawning, or anything else where CPython versions have
drifted.** `make versions` prints what each venv actually resolved to.

Write for the older interpreter: no syntax or stdlib API newer than 3.12,
and when the two versions disagree on behaviour (as `Path.resolve()` does),
the code handles both and a comment says which version does what.

## Coverage

`make coverage` is the only supported way to measure it; it fails below the
80% floor in `.coveragerc`. Current figures and the thinnest modules are
under `CLAUDE.md` → *Coverage*.

**Do not measure it by hand with a bare `coverage run`.** Most of the
integration value here is subprocess tests, and their children are separate
processes, so without `parallel = True` plus the startup hook `make coverage`
installs into `.venv`, `auto_start.py` and `doctor.py` report **0%** — they
are reached *only* through subprocesses — and the total reads 68% instead of
82%. That looks like two untested modules when they are 90% and 78%.

The subprocess helpers that build a *clean* env dict are trustworthy for
exactly the reason the hook does not reach their child. Every such call site
therefore splices in `tests/waiting.coverage_env()`, which is empty outside a
coverage run. **Add it to any new clean-env subprocess call site**, or that
code will silently read as uncovered.

## Dependencies

- Justify a new dependency against the standard library and the two already
  present (`websockets`, `psutil`). The bar is high: the skill has to be
  installable by `/hubbub:talk install-deps` into a user's runtime venv.
- A new runtime dependency lands in **both** `skills/talk/requirements.txt`
  and the `install-deps` path, or it is missing under one of the two
  interpreters. Dev-only deps go in `requirements-dev.txt`, which inherits
  the runtime file.
- Pin with a range (`websockets>=12.0,<14.0`, `psutil>=5.9`), not an exact
  version: the runtime venv is built on the user's machine, not from a
  lockfile.

## Documentation

- **English only.** One `README.md`, no `README.<lang>.md`, no localised
  docs or skill content. A Simplified Chinese README existed until
  2026-08-14 and was deleted — it drifted out of sync with the English one,
  and a stale translation is worse than none. If a translation shows up in a
  PR or a patch, drop it rather than maintaining it.
- A code change that leaves `README.md` describing the old behaviour is not
  finished work. The places a change can need to touch: `README.md` (user
  surface), `skills/talk/SKILL.md` (agent reaction policy; edits are pinned
  by tests), `CLAUDE.md` (invariants and the env-var table), this file
  (conventions), `docs/DELIVERY.md` (what the bus promises),
  `docs/security/` (findings).
- Docs that state a number the tree can contradict (a literal count, a test
  count, a coverage figure) say when they were measured, and the commit that
  changes the number updates the doc.
- `docs/guides/ticket-standards.md` carries a `template-version` marker
  locked to the issue templates by `scripts/check-template-lockstep.sh`;
  bump it only together with all four templates, in one commit.

## Releases and manifests

- **Don't bump the version in only one of the two plugin manifests.**
  `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` both
  carry a `version` field and are consulted by different code paths
  (plugin.json drives installed-plugin update detection; marketplace.json
  drives the marketplace listing). They must stay in sync — every version
  bump touches both files in the same commit.
  `tests/test_plugin_manifest.py::test_version_matches_marketplace` and a
  CI job both enforce it.
- The `[inter-session …]` → `[hubbub …]` rename is a per-release, three-step
  plan; step 2 is a single commit that flips every literal in `client.py`
  *and* `shared.py` and deletes `test_emitter_has_not_moved_yet`. Don't do
  part of it. The plan is `CLAUDE.md` → *The rename is deliberately
  half-done*.

## Commits

- Subject in the imperative, sentence case, no type prefix, no trailing
  period: `Fail the run on any skipped test`, `Guard the write, not the
  path: close #29's symlink bypass`. A colon or semicolon may introduce a
  second clause; most subjects fit in 70 characters.
- The body explains *why* and what was tried, cites the issue or fork
  number, and ends with what was verified (`tests/test_reaction_policy.py:
  33 passed`, `496 tests collected`). A reader should be able to reconstruct
  the decision from the body without the PR.
- A commit that changes a documented number (literal counts, test counts,
  coverage) updates the doc in the same commit.
- Docs corrections that contradict earlier prose say what the old text
  claimed and why it was wrong, so the history is not a series of silent
  reversals.
