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

## Common commands

Local dev runs entirely in a project-local venv at `.venv`. The
Makefile bootstraps it on first use (uv preferred, stdlib `venv` as
fallback). System Python is never touched.

```bash
make test                                    # full suite (~60 s, 248 tests)
make test-fast                               # skip the 17 @pytest.mark.slow tests
make clean                                   # remove .venv
```

To run pytest with non-make flags, use the venv's pytest directly:

```bash
.venv/bin/pytest tests/test_server.py -v                      # one file
.venv/bin/pytest tests/test_server.py::TestX::test_y -v       # one test
.venv/bin/pytest -k "election" -v                             # by substring
```

Never run two pytest sessions concurrently — the subprocess-spawning
tests bind real ports and race each other into spurious failures.

No build step, no linter configured. Runtime deps live at
`skills/talk/requirements.txt` (websockets + psutil); dev
deps inherit those plus pytest via `requirements-dev.txt`. Both reqs
files install into `.venv` via `make test` — there's nothing to install
by hand.

### Suite status

Green as of 2026-08-14: `248 passed in ~58 s` on Linux 7.0 / CPython
3.14. The four tests that used to fail all start **two listeners at
once**, and they were reporting the real server-election race — fixed
in `0e33123` by the election flock (see the election invariant below).
If any of them regresses, suspect the election, not the assertions.

One `PytestUnraisableExceptionWarning` from CPython 3.14's asyncio
`_SelectorTransport.__del__` is expected noise, not a product bug.

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

`monitors/monitors.json` deliberately omits `${user_config.*}` because
that substitution breaks `--plugin-dir` local-dev mode (CC doesn't
prompt + substitute in that mode). Instead, CC injects userConfig as
`CLAUDE_PLUGIN_OPTION_*` env vars, and `bin/client.py`'s argparse
defaults read those. **Do not add `--port` or
`--idle-shutdown-minutes` literal CLI args back into `monitors.json`
or the SKILL.md `Monitor` command** — they silently nullify the
user's plugin config. Regression test:
`test_plugin_manifest.py::test_command_does_not_hardcode_userconfig_args`.

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
- **Slow tests** (`@pytest.mark.slow`): subprocess-spawning, >1 s.

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
