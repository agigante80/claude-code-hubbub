# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An agent-to-agent messaging bus for Claude Code: multiple CC sessions on
the same Unix machine connect to a localhost WebSocket server and
exchange messages that drive actions in the receiving session.

Two install modes, **both supported and tested**:

- **Plugin** (recommended): `/plugin marketplace add …` → `/plugin
  install hubbub@hubbub`, or `claude --plugin-dir <repo>`
  for local dev. Adds `userConfig` (port, idle-shutdown) and
  `monitors.json`. User invokes as `/hubbub:talk …`.
- **Standalone skill**: clone or symlink `skills/talk/` to
  `~/.claude/skills/talk/` (e.g. `ln -s
  <repo>/skills/talk ~/.claude/skills/talk`). The
  skill is self-contained — `bin/`, `requirements.txt`, and `SKILL.md`
  all live inside `skills/talk/`, so a copy or symlink of just
  that subdirectory is a fully working skill. User invokes as
  `/hubbub:talk …` (no plugin namespace). No `userConfig`; override
  defaults via `HUBBUB_PORT` / `HUBBUB_IDLE_MINUTES` env vars if
  needed.

The skill content (`skills/talk/SKILL.md`) is install-mode
agnostic: the connect step has **no upfront dedup check**. It picks a
name and calls `Monitor()` directly. If a monitor is already running
for this CC session, `bin/client.py`'s ppid-flock catches the duplicate
and the new spawn exits with `[inter-session] another monitor for this
session is already running`, which the LLM surfaces via the Error
notifications path. Skipping the pre-check optimizes the common case
(not connected yet → straight spawn, ~50-100ms faster) and lets the
flock be the single source of truth for race-safety. Don't add a
`list.py --self` or `TaskList()` pre-check back into the connect step
— that was tried and reverted because the optimization paid more in
the common case than it saved in the edge case.

Layout follows the conventional `skills/<name>/SKILL.md` auto-discovery
pattern that the current CC plugin schema requires (`"skills": ["./"]`
is rejected with "Path escapes plugin directory"). **`bin/` lives
inside the skill dir** (`skills/talk/bin/`). The plugin's
monitor `when` defaults to `always` (every session joins the bus at
open); `bin/auto_start.py` flips it to `on-skill-invoke:talk` when the
user runs `/hubbub:talk auto-start off`. Empirically `on-skill-invoke` may not
reliably auto-spawn a working monitor in current CC versions, so the
LLM's `Monitor()` call in the skill is what actually establishes the
connection most of the time.

When CLAUDE.md and other docs reference `bin/<script>.py` as an
abbreviated label, the actual path is
`skills/talk/bin/<script>.py`.

Single user, single machine. Unix-only (macOS / Linux / WSL2).

## Project memory

Durable notes live in `.claude/memory/`, indexed by `.claude/memory/MEMORY.md`
— one fact per file, frontmatter with `name` / `description` /
`metadata.type` (`user`, `feedback`, `project`, `reference`). Read the index
first; write new memories there, not to the path-keyed store under
`~/.claude/projects/`, which is orphaned by a checkout rename (that happened
once already — see `hubbub-local-workspace.md`).

Both `.claude/memory/` and `.claude/handoffs/` are gitignored: they hold
working notes about the maintainer, and this repo is public. Two lines in
`.gitignore` to change that.

## Common commands

Local dev runs entirely in a project-local venv at `.venv`. The
Makefile bootstraps it on first use (uv preferred, stdlib `venv` as
fallback). System Python is never touched.

```bash
make test                                    # full suite (~70 s), .venv
make coverage                                # suite under coverage; gate at 80%
make test-fast                               # skip the 19 @pytest.mark.slow tests
make test-system                             # same suite under the SYSTEM python3
make test-both                               # both interpreters, sequentially
make versions                                # which Python each venv resolves to
make clean                                   # remove both venvs
```

To run pytest with non-make flags, use the venv's pytest directly:

```bash
.venv/bin/pytest tests/test_server.py -v                      # one file
.venv/bin/pytest tests/test_server.py::TestX::test_y -v       # one test
.venv/bin/pytest -k "election" -v                             # by substring
```

Two concurrent pytest sessions are **expected to pass** as of the #17 fix —
verified by running two full suites at once, both green. Each
session gets its own `tmp_data_dir`, so they do not contend for *state*; what
broke them was contention for *CPU*, against subprocess tests that asserted on
fixed `time.sleep()` durations.

The rule is therefore no longer "don't run two", it is **don't assert on a
sleep, and never read a pipe without an enforceable deadline**. A blocking
`readline()` in a loop that checks its deadline only *between* reads cannot
honour it — the read that never returns is exactly the one the timeout is
for — so a dropped message hung the whole suite instead of failing it, with
no assertion message and the enclosing `finally` never reaping the
subprocesses.

Two caveats, so this doesn't read as a guarantee it isn't:

- **Port isolation is very good, not absolute.** `free_port` binds port 0 and
  *closes* the socket before returning the number, so a concurrent session can
  be handed the same port. On a collision one session's client probes the
  other's server and fails `verify_server_identity` (different data dir →
  different token and pidfile), surfacing as `server identity check failed`.
  Rare, and it reads as a product bug when it happens.
- **The shared waits live in `tests/waiting.py`** — `wait_for` for a
  condition, `read_line` for a pipe read with a deadline it can actually
  enforce. Use them; the identical bug has been found three times in this
  suite (#17, #23, #27) and each time only the copy that failed got fixed.
  Across `test_helpers.py` and `test_client.py` the only remaining fixed
  `time.sleep` calls are poll intervals inside wait loops, which is what a
  poll interval is for — none precedes an assertion.

Also note the suite runs CPython 3.14 (uv-provisioned `.venv`) while the
shipped monitors run whatever `python3` resolves to — 3.12 on this machine.
That difference is not cosmetic: `Path.resolve()` raises `RuntimeError` on a
symlink loop in 3.12 and silently returns the link in 3.14, which hid a real
startup crash from `make test` until #19.

The cause is in the Makefile: `make test` bootstraps `.venv` with uv when uv
is present, and **uv supplies its own Python** rather than the system one.
Without uv it falls back to `python3 -m venv` and the two agree — so whether
your suite matches production depends on whether you have uv installed, which
is not a property anyone reasons about.

So **a green `make test` is not by itself evidence that the shipped code is
green.** `make test-system` (#24) builds a second venv, `.venv-system`, from
`python3` explicitly and never uv, and runs the same suite there. Use
`make test-both` before shipping, and always for changes touching path
resolution, subprocess spawning, or anything else where CPython versions have
drifted. `make versions` prints what each venv actually resolved to.

No build step, no linter configured. Runtime deps live at
`skills/talk/requirements.txt` (websockets + psutil); dev
deps inherit those plus pytest via `requirements-dev.txt`. Both reqs
files install into `.venv` via `make test` — there's nothing to install
by hand.

### Suite status

Green as of 2026-08-15: `491 passed in ~70 s` on Linux 7.0 / CPython
3.14. The four tests that used to fail all start **two listeners at
once**, and they were reporting the real server-election race — fixed
in `0e33123` by the election flock (see the election invariant below).
If any of them regresses, suspect the election, not the assertions.

One `PytestUnraisableExceptionWarning` from CPython 3.14's asyncio
`_SelectorTransport.__del__` is expected noise, not a product bug.

### Coverage, and the trap in measuring it

`make coverage` reports **82%** (line + branch) and fails below the 80% floor
in `.coveragerc`. Thinnest: `discover.py` 61% and `relabel.py` 65% — both are
mostly error branches needing a real process tree or a live listener, and
`discover.py` is the process-tree walk this file already flags as trap-laden,
so that is the least comfortable number in the set.

**Do not measure it by hand with a bare `coverage run`.** Most of the
integration value here is subprocess tests, and their children are separate
processes, so without `parallel = True` plus the startup hook `make coverage`
installs into `.venv`, `auto_start.py` and `doctor.py` report **0%** — they are
reached *only* through subprocesses — and the total reads 68% instead of 82%.
That looks like two untested modules when they are 90% and 78%.

The subprocess helpers deliberately build a *clean* env dict, which is why
they are trustworthy and also why the hook does not reach the child. Every
such call site therefore splices in `tests/waiting.coverage_env()`, which is
empty outside a coverage run. **Add it to any new subprocess call site**, or
that code will silently read as uncovered.

## Architecture (big picture)

Three process classes share a localhost WebSocket bus:

1. **`bin/server.py`** — single detached asyncio websockets server per
   port. Started by whichever client wins the election flock. Owns
   the registry of connected agents, mints `msg_id`s, writes
   `messages.log`. Idle-shutdown after N minutes.

2. **`bin/client.py`** — long-lived per-session monitor. Each stdout
   line becomes a Claude Code notification. Manages reconnect with
   exponential backoff, ping/pong, and a dedup flock keyed by the CC
   ancestor pid (see the state-file invariant below — it is *not*
   `getppid()`). On registering it writes `clients/<key>.session`, the
   state file the helper CLIs read to find their own session.

3. **`bin/{send,list,relabel}.py`** — short-lived control CLIs. Connect
   with `role=control`, do not register as agents, never appear in
   `list`. Discover their owning session via `bin/discover.py`
   (process-tree walk + per-listener state file).

`bin/spawn.py` is the election + spawn boundary; `bin/shared.py` is
paths, validation, sanitizer, atomic bearer-token, identity helpers.
`bin/profile.py` persists the per-project display label.
`bin/auto_start.py` rewrites the `when` field in `monitors/monitors.json`.

Wire protocol is one JSON object per WebSocket frame, dispatched on
`op`: `hello`, `list`, `send`, `broadcast`, `rename`, `relabel`, `bye`,
`ping`.
Everything the server enforces (caps, rate limits, name/label rules,
`role`/`nonce` cross-checks) lives in `server.py::_handle_*`; the
constants it enforces against live in `shared.py`. Broadcasts are rate
limited to 60/min per sender; `messages.log` rotates at 50 MB × 5.

## Non-obvious invariants (read before changing the affected code)

### The rename is deliberately half-done

The project was `inter-session` through `0.1.4`; the plugin is now
`hubbub` and the skill is `talk` (`/hubbub:talk`). **Identity was
renamed, runtime identifiers were not**, and that asymmetry is
intentional:

| Renamed | Renamed, with a compatibility shim | Still `inter-session` |
| :------ | :--------------------------------- | :-------------------- |
| plugin + marketplace `name`, repo/docs, `skills/talk/`, `monitors.json` (`hubbub-client`, description `hubbub messages`) | `~/.claude/data/hubbub/` (`0.2.0`; legacy path left as a symlink), `HUBBUB_*` env vars (`INTER_SESSION_*` still honoured) | the `[inter-session …]` stdout prefix |

The stdout prefix is the last piece, and it is deliberately still
outstanding — see issue #10. It is the wire contract between `client.py`
and the reaction policy in `SKILL.md`, so changing it means teaching the
policy to accept both spellings for a release before the emitter moves.
Don't "finish the rename" in one sweep and assume it's cosmetic.

**Step 1 of 3 is done.** The policy now accepts `[hubbub …]` *and*
`[inter-session …]`; the emitter is unchanged and still writes
`[inter-session …]`. The remaining steps are one per release:

2. flip **every** prefix literal in `client.py` — that is 19 of them, not
   3. Three are message headers in `_format_msg` (including the
   `truncated=` variant) and the `cont` continuation line; the other
   **sixteen are `[inter-session]` operational notices**. Note
   `_print_line` is *not* one of them — it prints whatever it is handed
   and contains no prefix. Then update the `docs/security/SEC-001` /
   `SEC-002` prose;
3. drop the legacy spelling from the policy.

Flipping only the message headers is the mistake to expect: it looks
finished, and it strands every error notice on a spelling that step 3
then deletes from the policy — after which the agent silently stops
recognising them.

`tests/test_reaction_policy.py::TestPrefixRenameStaging` pins all of
this. `test_emitter_never_mixes_the_two_spellings` catches the partial
flip, and `test_emitter_has_not_moved_yet` is a deliberately backwards
assertion that step 2 has not happened — **delete that one in the step-2
commit and say so in the message.** Verified by doing both flips against
the suite: a partial flip fails two tests, a complete flip fails only
the delete-me one.

#### The data-dir migration is a rename **plus a symlink**, and the symlink is the load-bearing half

`shared.migrate_legacy_data_dir()` does `os.rename(inter-session, hubbub)`
and then recreates `inter-session` as a symlink to `hubbub`. Don't drop
the symlink as tidy-up. Older builds on the same machine hardcode the
legacy path, and they must keep resolving to the *same* token, election
lock and pidfile as new builds — split that namespace and two clients
each win their own election, both bind the port, and the loser's
`_unlink_own_identity` wipes the winner's identity. That is precisely the
race the election flock exists to prevent, re-entered through the back
door (see the election invariant below).

`os.rename` is used rather than copy-then-delete because it is atomic
within a filesystem and preserves inodes, so flocks already held by
running monitors survive the move — a session connected before the
upgrade keeps contending with one connected after it. Covered by
`tests/test_shared.py::TestLegacyDataDirMigration`.

**Each entry-point calls it explicitly; `data_dir()` must stay pure.** The
migration lived inside `data_dir()` for exactly one afternoon, and in that
time `make test` moved the developer's *real* `~/.claude/data/inter-session`
— any test that resolves the default path without the `tmp_data_dir` fixture
inherits the real `$HOME`. A path resolver that touches the filesystem
defeats the suite's whole isolation story.
`test_data_dir_has_no_filesystem_side_effects` guards it.

**`hubbub/` already existing does not mean the migration is done, and a
colliding `venv` is not a conflict.** The guard is `_migration_complete()` — the legacy
symlink *and* the marker — not `new.exists()`. `install-deps` is a documented standalone command that creates
`<data-dir>/venv` at the *new* path, so a user upgrading from a pre-rename
install can easily produce `hubbub/venv` before any monitor has migrated
anything — while that install's own `inter-session/venv` still sits there.
Bail on "new exists" and the migration is permanently stuck; treat two `venv`
entries as a collision and it refuses on *every* machine that ever ran
`install-deps`, which is most of them. Both failures strand the live token and
pidfile under `inter-session/` with no symlink — the forked namespace the
symlink exists to prevent, with no self-repair path.

So `_drain_into` moves entries across, treats only `_DISPOSABLE` names
(`venv`, the marker) as safely set-aside-able, and refuses — loudly, changing
nothing — when *live* state collides. A colliding token means the fork already
happened and picking a winner would silently destroy one side's bus. If the
legacy directory can't be emptied (an old build still running may recreate
`clients/` mid-move), the remainder is renamed to `inter-session.pre-rename`
rather than left in place: a legacy path that stays a real directory is the
one outcome that guarantees the fork.

A `.migrated-from-inter-session` marker in the new dir lets a later run
recreate the symlink if a previous one moved the directory but died before
`symlink_to`. Without it that state is invisible and permanent.

### Server election (`bin/spawn.py` + `bin/server.py --fd`)

Whoever wins the election spawns the server via
`subprocess.Popen(pass_fds=(fd,), start_new_session=True)`, and the
child adopts the bound fd with `socket.socket(fileno=fd).listen()`.
**PEP 446 is the gotcha**: CPython sets `FD_CLOEXEC` on sockets by
default, so `os.set_inheritable(fd, True)` is required — without it,
`execvp` silently closes the socket. `SO_REUSEADDR=1` is also set to
allow fast rebind after a SIGKILL'd server (otherwise macOS holds the
port for ~30 s).

**`bind()` alone is not the election — a per-endpoint flock is**
(`spawn._acquire_election_lock` over `shared.election_lock_path`, added
in `0e33123`). `SO_REUSEADDR` lets a second `bind()` on the same port
succeed as long as no socket has reached `listen()` yet, and here
`listen()` happens in the *spawned child*, tens of milliseconds after
the parent's `bind()` returns. So without the lock two clients starting
at once would both bind and both spawn a server, and:

1. Server A writes identity (`write_server_identity`), `listen()`s, serves.
2. Server B writes identity too — **overwriting the pidfile with its own
   pid** — then its `listen()` raises `EADDRINUSE`. The `except` arm
   calls `_unlink_own_identity()`, which sees its own pid in the pidfile
   and deletes it, **wiping live server A's identity**.
3. Both clients then run `verify_server_identity()`, find no pidfile, and
   print `server identity check failed … refusing to connect`.

The lock removes step 1's premise: only the flock holder binds and
spawns; everyone else calls `wait_for_server`. Two design details in
`_acquire_election_lock` are deliberate — it polls `LOCK_EX | LOCK_NB`
instead of blocking (so it can short-circuit the instant a peer's
server answers a TCP probe, and can never wedge on a stuck holder), and
it re-checks `is_server_up` *after* acquiring, closing the gap where a
peer finished starting while we waited.

Don't remove `SO_REUSEADDR` (it's still needed for fast rebind after a
crash) and don't replace the flock with bind-atomicity. Also keep the
ordering in `server.py::serve` — identity written *before* `listen()` —
which closes a different race where a client's TCP probe succeeds before
the pidfile exists.

### Server identity verification (`bin/shared.py::verify_server_identity`)

Before any client or helper sends the bearer token, it verifies the
server process identity by reading the pidfile's `.meta` companion and
checking pid + cmdline + host + port. Refuses on mismatch. This is
defense-in-depth against a coincidental localhost port squatter
receiving the token.

### Two venvs, and every entry-point re-execs into one of them

- `.venv` at the repo root — **dev/test only**, created by the Makefile.
- `~/.claude/data/hubbub/venv` — the **user's runtime venv**,
  created by `/hubbub:talk install-deps`, holding websockets + psutil.

The first ~10 lines of `client.py`, `send.py`, and `list.py` are a
bootstrap that `os.execv`s the script under the *runtime* venv's
interpreter whenever that venv exists. So `python3 bin/client.py`
does not necessarily run under the interpreter you invoked it with —
if you're hand-testing an edit and the runtime venv is stale, you are
debugging the wrong dependencies. `tests/conftest.py` sets
`HUBBUB_NO_REEXEC=1` process-wide to disable it; set the same
env var for any manual repro.

### State files are keyed by the Claude Code *ancestor* pid, not `getppid()`

`shared.resolve_listener_key()` is the single source of truth for the
`clients/<pid>.lock` / `clients/<pid>.session` filenames, and both the
monitor (writer) and the helper CLIs (readers) must agree on it. It is
**not** `os.getppid()`: in real CC the monitor and the helpers are
spawned by *different* Bash subshells, so they are siblings — neither
can reach the other by walking parents one step. What they share is the
CC main process, so `find_cc_ancestor_pid()` walks up until it finds it.

Two traps live in that walk:

- **Match on `cmdline[0]`, never `psutil.Process.name()`.** Modern CC
  sets its proctitle to its version string (e.g. `2.1.119`), so
  `name()` is useless. The binary basename in `cmdline[0]` is reliably
  `claude` (or `node` for older bundles).
- **Background sessions launch CC by a versioned path** (e.g.
  `~/.local/share/claude/versions/2.1.146`), whose basename is a
  version number. Without the explicit `/claude/versions/` check the
  walk sails past the per-session process and lands on the shared
  `claude daemon run` supervisor — at which point every background
  session collides on one lock. This is what commits `689b636`/`7b87015`
  fixed; `resolve_listener_key` is also overridable via
  `HUBBUB_PPID_OVERRIDE` (tests, debugging).

### `${CLAUDE_PLUGIN_ROOT}` is not exported to `Bash()`/`Monitor()` shells

It is a CC *manifest substitution token* — resolved only when CC spawns
the subprocesses declared in `monitors.json`/`plugin.json`. Inside a
`Bash(...)` or `Monitor(...)` command it expands to the empty string
and silently routes commands to the wrong path. SKILL.md therefore
tells the agent to resolve `<bin>` from the skill's own base directory
(which the harness prints) and substitute the absolute path. Don't
"simplify" SKILL.md by putting `${CLAUDE_PLUGIN_ROOT}` back into those
commands.

### userConfig is delivered via env vars, NOT `${user_config.*}` substitution

`monitors/monitors.json` deliberately omits `${user_config.*}`. CC
rejects it outright in a monitor command ("Monitor commands cannot
safely reference `${user_config.*}`; have the monitor script read the
value from a config file or prompt instead"), and it also breaks
`--plugin-dir` local-dev mode. `bin/client.py`'s argparse defaults read
`CLAUDE_PLUGIN_OPTION_*` instead. **Do not add `--port` or
`--idle-shutdown-minutes` literal CLI args back into `monitors.json`
or the SKILL.md `Monitor` command.** Regression test:
`test_plugin_manifest.py::test_command_does_not_hardcode_userconfig_args`.

#### …except CC never sets those env vars for a monitor

**This section said userConfig "is delivered via env vars". That is true
for hooks and false for monitors, and the difference matters.** Verified
2026-08-15 three ways: the CC bundle (`2.1.233`) has exactly one site
assigning ``CLAUDE_PLUGIN_OPTION_${…}`` and it sits inside the hook
executor; CC's refusal message above tells plugin authors to use a
config file precisely because the env route is unavailable; and two live
monitors on this machine had 67 environment variables each with **no
`CLAUDE_PLUGIN_*` at all**, not even `CLAUDE_PLUGIN_ROOT`.

Consequences, none of them cosmetic:

- **`port` and `idle_shutdown_minutes` userConfig have never reached the
  auto-started monitor.** Someone who sets a non-default port in
  `/plugin config` gets a monitor on 9473. Tracked as #28. The values
  *do* work for a monitor the agent starts itself via `Monitor()`, since
  that inherits the shell environment — which is why this went unnoticed.
- **`auto_start` was added as userConfig and removed again in the same
  night** (#22). An install prompt that silently ignores a
  security-relevant answer is worse than no prompt.
- `HUBBUB_*` / `INTER_SESSION_*` env vars *do* reach the monitor. They
  are the working route, and the only one, until a config-file bridge
  exists.

Before adding any userConfig key, ask whether the thing that must read
it is a hook or a monitor. If it is a monitor, the answer is not an env
var.

**`when` has a second, separate problem** worth keeping straight from
the delivery one above: it is read by CC's monitor scheduler *before any
hubbub code runs*, so even a working env var could not change it. The
manifest therefore stays `when: "always"` unconditionally and auto-start
is enforced in `client.py::_autostart_wanted` — the monitor starts and
then exits on its own. Don't "fix" the apparent inconsistency by
templating `when`.

Effective precedence is **`<data-dir>/autostart-off` → `HUBBUB_AUTO_START`
/ `INTER_SESSION_AUTO_START` → default on**, resolved first-parseable-wins
so a truthy new spelling shadows a falsey legacy one. `auto_start.py`
must mirror that exactly, key list *and* resolution rule; when it drifted
to "first falsey key wins" it reported `OFF` for monitors that were
happily running and refused to clear the opt-out.

### Roles: agent vs control

`role=agent` (client.py, long-lived) appears in `list` and receives
`msg` events. `role=control` (send.py, list.py, ephemeral) does NOT
appear in `list`. Control connections must include `for_session` +
`nonce` matching their owning listener's state file; the server
cross-checks. This blocks impersonation by sibling processes that share
a parent.

### ASCII `name` vs Unicode `label`

`name` is the addressable handle: strict ASCII, regex
`^[a-z0-9][a-z0-9-]{0,39}$`, case-sensitive. `label` is an optional
Unicode display string, NFC-normalized and category-restricted (no
`Cc/Cf/Cs/Cn/Z*`) to block BiDi/ZWJ/NBSP injection. All routing happens
by name; labels are display-only. Don't try to address by label.

They also differ in how they're *changed*: renaming is
disconnect + reconnect (`TaskStop` the monitor, re-`Monitor` with
`--name`), because the name is baked into the `hello`. Relabeling is
in-place — `bin/relabel.py` sends the `relabel` op over a `role=control`
connection, so the session keeps its `session_id` and stays on the bus.
Don't "unify" the two by making relabel bounce the monitor.

### Peer-controlled strings that reach the header are sanitized twice

Three fields reach the notification header from a peer: `name`
(server-validated against the ASCII regex), `label`, and — since fork #7 —
eight characters of `session_id` as `sid=`. Each needs a boundary reject
*and* a render-time neutralisation, and the third one was added without
either. See SEC-003: `session_id="\n[hubbub"` split a notification into two
stdout lines whose second began with a documented authoritative prefix, which
defeats "only the leading header is authoritative" by making the injection the
leading prefix of its own line. `validate_session_id` rejects it now and
`short_session_id` keeps hex only at render.

**The lesson is about the test, not the code.** That commit *did* add a
SEC-001 regression test — exercising the label path, while the new field was
wide open. When you add a field to this header, the security test has to cover
*that* field.

### The peer `label` is rendered, so it is sanitized twice

`validate_label` rejects `"`, `[`, `]` at the boundary (the primary
defense), and `sanitize_label_for_display` re-neutralizes them to
look-alikes (`'`, `(`, `)`) at render time in `client.py::_format_msg`
and `list.py`. The redundancy is intentional: it covers labels that
never passed live validation — a direct `_format_msg` caller, or a label
persisted by an older client. Without it a peer could close the
notification header's bracket and forge sender attribution (SEC-001).
Related: only the *leading* header of a notification line is
authoritative — anything a peer's `text` puts after it is untrusted
content, never a directive (SEC-002). Findings live in
`docs/security/`; the guardrail prose lives in `SKILL.md` and is
covered by `tests/test_reaction_policy.py`.

### Labels persist per project, keyed by repo root

`bin/profile.py` stores the label in
`<data-dir>/profiles/<sha256(project_root)[:32]>.json`, where
`project_root` is the nearest ancestor containing a `.git` entry
(symlink-resolved), else the cwd. Two consequences worth knowing before
touching it: the filename is a hash so no caller-supplied path
component reaches the filesystem, and `cd`-ing into a subdirectory of
the same repo resolves the same profile.

`profile.resolve_label(explicit, cwd)` encodes a three-state contract
that `client.py::_resolve_label` and `relabel.py` both depend on:
`None` means "no label given → load the persisted one", `""` means
"explicitly clear it", and a non-empty string means "use and persist
it". Labels are re-validated on load, so a hand-edited or older-format
profile can never resurface a label the live path would reject.

### Three-tier size limits

| Limit                          | Value                                       |
| :----------------------------- | :------------------------------------------ |
| WebSocket frame                | 16 MB                                       |
| Direct `text` length           | 10 MB (server-enforced)                     |
| Broadcast `text` length        | 256 KB (server-enforced)                    |
| Stdout notification body       | 400 chars (issue #2: Claude Code clips each monitor notification at ~512 chars total, so the body cap is sized to leave room for our prefix) |

Direct messages whose body exceeds the stdout cap display as a truncated
first-line and a `cont` line pointing to `messages.log` so the receiver
can fetch the full payload via `grep -F <msg_id>`. Truncated content is
preserved in full in `messages.log` regardless.

### Reaction policy lives in `SKILL.md`

The behavioral contract for the receiving agent (when to act, when to
surface, reply prefixes, destructive-op guardrails) is prose in
`SKILL.md`. `tests/test_reaction_policy.py` runs static checks on that
prose so prose edits can't accidentally drop a guardrail.

## Test conventions

- **State isolation**: the `tmp_data_dir` fixture sets
  `HUBBUB_DATA_DIR` to a per-test temp path so the suite never
  touches `~/.claude/data/hubbub/`.
- **Free ports**: the `free_port` fixture binds port `0` to find an
  ephemeral port.
- **PPID override**: subprocesses spawned in a single test share the
  pytest parent pid, which would collide on the ppid flock. Set
  `HUBBUB_PPID_OVERRIDE` to give each subprocess a distinct
  pseudo-ppid.
- **Collision-retry budget**: `HUBBUB_MAX_COLLISION_RETRIES` (default 3).
  Set it to `0` to make the first name collision terminal, which is the
  only deterministic way to exercise retry exhaustion — otherwise you
  need four sessions racing one cwd-derived name and the outcome depends
  on their interleaving. Same shape and purpose as the ppid override.
- **Waits**: `tests/waiting.py`. `wait_for(predicate)` for a condition,
  `read_line(proc)` for a pipe read with an enforceable deadline. Never
  a bare `time.sleep()` before an assertion, and never a bare
  `proc.stdout.readline()` — see the concurrency note under Common
  commands for why the latter hangs the suite rather than failing it.
- **Slow tests** (`@pytest.mark.slow`): subprocess-spawning, >1 s.
- **Skips fail the run.** `conftest.pytest_sessionfinish` turns any skipped
  test into a red build, because `495 passed, 1 skipped` reads as success and
  the test that did not run is the one nobody looks at. The only conditional
  skips here are guarded on `os.geteuid() == 0` — they need `chmod 000` to
  actually deny access, and root bypasses DAC — so **run the suite as a
  non-root user**, which is what keeps the count at zero. `--allow-skips` is
  the escape hatch when a skip is genuinely intended.

## Don't

- **Don't blanket `pkill -f 'bin/(client|server).py'`** during local
  testing — it will kill real user hubbub monitors running in
  other CC sessions. Target specific pids via the pidfile
  (`~/.claude/data/hubbub/server.<port>.pid`) or
  `TaskList()`-derived monitor task IDs.
- **Don't use `${user_config.*}` substitution in `monitors.json`** —
  see invariant above.
- **Don't weaken the SKILL.md description** ("pushy" multi-trigger
  framing is intentional to combat undertriggering — skill-creator
  best practice).
- **Don't add translations.** The project is English-only: one
  `README.md`, no `README.<lang>.md`, no localized docs or skill
  content. A Simplified Chinese README existed until 2026-08-14 and was
  deleted — it drifted out of sync with the English one, and a stale
  translation is worse than none. If a translation shows up in a PR or
  a patch, drop it rather than maintaining it.
- **Don't bump the version in only one of the two plugin manifests.**
  `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`
  both carry a `version` field and are consulted by different code
  paths (plugin.json drives installed-plugin update detection;
  marketplace.json drives the marketplace listing). They must stay
  in sync — every version bump touches both files in the same commit.
