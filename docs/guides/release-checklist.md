# Release checklist: the Claude Code layer

Everything in this file is here for one reason: **no automated tier can reach
it.**

`tests/test_cc_harness.py` (Tier A, in CI, gating) spawns the command CC would
spawn and proves a monitor registers, a message is delivered, and the first
line fits the clip — all without a `claude` binary, because
`.github/workflows/ci.yml` has none and `tests/conftest.py` fails the run on
any skip. `scripts/probe_cc_layer.py` (`make probe-cc`, on demand, never in
CI) drives the plugin through a real `claude -p` session.

What is left is the part that needs an **interactive** Claude Code session.
Plugin monitors — including `when: "always"` — run only in interactive CLI
sessions ([plugins-reference, §Monitors](https://code.claude.com/docs/en/plugins-reference)),
so no `claude -p` invocation can observe the auto-start path at all. Nor can
one observe `/plugin marketplace add`.

Run these before a release, in order. Record the observed result and the CC
version next to each: several of the claims below are *empirical* and dated,
and the point of re-running them is to find out when they stop being true.

---

## 0. Prerequisites

- The suite is green on both interpreters: `make test-both`.
- Coverage is above the floor: `make coverage`.
- A CC version to test against: `claude --version`. **Write it down** — every
  observation below is about that build and nothing else.

## 1. `when: "always"` fires at interactive session open

1. Ensure auto-start is on: `/hubbub:talk auto-start status` prints `ON`.
2. Open a **new interactive** Claude Code session in any project directory —
   do not invoke the skill, do not type anything about hubbub.
3. In that session run `/hubbub:talk` and ask it to run `list.py --self`, or
   run the equivalent from a shell in the same session.

**Expected:** `name=<cwd basename>` — a monitor is already connected, having
been started by CC's monitor scheduler rather than by anything the user did.

**If it fails:** the always-on promise is broken and the README's *Install*
section is wrong. This is the single most valuable step in the file, because
a monitor that silently never spawns produces no exception and no log line —
just a session quietly not on the bus.

## 2. `on-skill-invoke:talk` — does it spawn anything?

CLAUDE.md records, empirically, that `on-skill-invoke` **may not** reliably
auto-spawn a working monitor. That claim has no test anywhere and cannot get
one; this step is the only thing that re-checks it.

1. `/hubbub:talk auto-start off` (writes `when: "on-skill-invoke:talk"` **and**
   the durable `<data-dir>/autostart-off` flag).
2. Remove the durable flag only — `rm ~/.claude/data/hubbub/autostart-off` —
   so the manifest's `when` is the only thing left deciding. (Putting it back
   is step 3.)
3. Open a new interactive session. Confirm no monitor: `list.py --self` prints
   `not connected`.
4. Invoke the skill: `/hubbub:talk` with no arguments.

**Expected, per the empirical claim:** *no* monitor appears from the
scheduler; the connection is established only when the agent itself calls
`Monitor()` per SKILL.md's connect step.

**Record either outcome.** If a monitor *does* appear, CLAUDE.md's *What this
is* sentence is stale and should be updated in that release.

5. Restore: `/hubbub:talk auto-start on`.

## 3. The durable opt-out survives a plugin update

1. `/hubbub:talk auto-start off`.
2. `/plugin update hubbub` (or re-add the marketplace), which ships
   `when: "always"` again.
3. Open a new interactive session.

**Expected:** no monitor; `/hubbub:talk auto-start status` prints `OFF (forced`
with an `opt-out:` line saying the durable flag `wins`.

Tier A pins the monitor's half of this
(`TestAutoStartOptOut::test_optout_survives_a_simulated_plugin_update`); what
it cannot do is make a real `/plugin update` happen.

## 4. `/plugin marketplace add` → `/plugin install`, and which manifest CC reads

1. `/plugin marketplace add <this repo's URL or path>`.
2. `/plugin install hubbub@hubbub`.
3. Check the installed version matches `.claude-plugin/plugin.json`.
4. Answer the `userConfig` prompts with a **non-default port** (say 9474).
5. Open a new interactive session and read the monitor's port:
   `list.py --self` prints `port=…`.

**Expected today:** `port=9473`, the default — **not** the configured one.
That is #28: `CLAUDE_PLUGIN_OPTION_*` is injected for *hooks* only, so
userConfig has never reached the monitor. If this step ever prints `9474`, the
delivery route changed and #28 can close.

## 5. Which `CLAUDE_*` variables the auto-started monitor receives

The one observation that decides a design question, and the one place it can
be made. Two sources disagree: the current plugins-reference (§Monitors) says
monitor processes receive `CLAUDE_PLUGIN_ROOT`, `CLAUDE_PLUGIN_DATA` and
`CLAUDE_PROJECT_DIR`; CLAUDE.md's dated `2.1.233` measurement found two live
monitors with 67 variables each and **no** `CLAUDE_PLUGIN_*` at all. No pytest
test can settle it, and `make probe-cc` can only measure the `Monitor()`-shell
route, not the scheduler's.

With an auto-started monitor running (step 1):

```bash
# listener_pid from `list.py --self`, or from the .session file directly
PID=$(python3 ~/.claude/plugins/.../skills/talk/bin/list.py --self | sed -n 's/^listener_pid=//p')
tr '\0' '\n' < /proc/$PID/environ | grep '^CLAUDE_' | cut -d= -f1
```

**Record the full list of names and the CC version**, even when it is empty —
"no `CLAUDE_*` at all" is the finding that matters. Compare against both
sources above.

**Why it matters:** if `CLAUDE_PLUGIN_DATA` is present, it is a candidate
location for the config-file bridge #28 needs. #28 is the ticket that acts on
this; this step only measures it. Do not edit the measurement into CLAUDE.md's
*…except CC never sets those env vars for a monitor* section — that section's
`2.1.233` reading stays as written, dated; add a new dated line instead.

## 6. `make probe-cc`

```bash
make probe-cc
```

**This spends the operator's Claude credential**, one session per case, and
must never run unattended. Two cases: `plugin-dir` (the `/hubbub:talk` form)
and `standalone` (the `/talk` form, which needs
`ln -s <repo>/skills/talk ~/.claude/skills/talk`).

**Expected:** `PASS plugin-dir: header intact, 500 units delivered` and the
same for `standalone`.

A missing prerequisite is **exit 2 with a reason**, never a skip — `claude`
not on `PATH`, the runtime deps unimportable, or the standalone symlink
absent. Paste the per-case lines and the recorded `CLAUDE_*` list into the
release notes, `$HOME` redacted (the script does this for its own output; do
it for anything you add).

## 7. The standalone install, interactively

1. `ln -s <repo>/skills/talk ~/.claude/skills/talk`.
2. Open a new interactive session with the **plugin uninstalled**.
3. `/talk connect solo` — note the spelling: standalone skills are invoked by
   directory name, so `/talk`, not `/hubbub:talk`.

**Expected:** a monitor registers; `list.py --self` prints `name=solo`. There
is no `monitors.json` in this mode, so nothing auto-starts and
`/talk auto-start off` correctly reports that there is nothing to configure.

Tier A covers this at the subprocess level with a *copied* `skills/talk/`
(`TestStandaloneSkill`); this step covers the symlink and the interactive
invocation.
