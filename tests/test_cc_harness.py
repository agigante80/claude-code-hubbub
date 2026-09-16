"""Tier A of the Claude Code layer: spawn *the command CC would spawn*.

Every other e2e test in this suite stops at `client.py` invoked the way a test
finds convenient. The layer between Claude Code and this plugin — the process
CC starts from `monitors/monitors.json`, the `${CLAUDE_PLUGIN_ROOT}`
substitution, the re-exec into the runtime venv, what a session literally
receives on stdout — had no automated coverage, and it is where both failures
recorded in `docs/DELIVERY.md` happened. The bus was working correctly both
times.

**Nothing in this file needs a `claude` binary.** `.github/workflows/ci.yml`
installs Python and `uv` and never Claude Code, and `conftest.pytest_sessionfinish`
fails the run on any skip — so a pytest test that needed `claude` would be
either red in CI or a skip, and both are forbidden. The half that does need a
real session is `scripts/probe_cc_layer.py` (`make probe-cc`), on demand and
never in CI; the half neither can reach is `docs/guides/release-checklist.md`.

Three rules hold for every spawn here, and each has a failure mode attached:

- **`${CLAUDE_PLUGIN_ROOT}` is substituted into the command string and never
  exported.** It is a CC *manifest substitution token*, not an environment
  variable — inside a `Bash()`/`Monitor()` shell it expands to the empty
  string. A harness that exported it would pass while testing a delivery route
  that does not exist. The one process here that legitimately receives it as an
  env var is `auto_start.py`, which honours it as an explicit override, and it
  is set for that subprocess only.
- **`python3` is swapped for `sys.executable`, `TestReexecBootstrap`
  included.** Under the clean env's `PATH=/usr/bin:/bin` a bare `python3` is
  the system 3.12 under *both* `make test-both` venvs, so "green on both
  interpreters" could not hold. Fidelity survives the swap because the
  bootstrap at `client.py:11-23` is stdlib-only and decides on nothing but
  `$HOME`, `HUBBUB_NO_REEXEC` and `sys.prefix`.
- **The child env is clean and carries no `CLAUDE_*` name at all** — not
  `CLAUDE_PLUGIN_ROOT`, `CLAUDE_PLUGIN_DATA`, `CLAUDE_PROJECT_DIR` or
  `CLAUDE_PLUGIN_OPTION_*`. That is deliberately the *stronger* property
  rather than a claim about what CC exports: the current plugins-reference
  says monitors receive the first three, CLAUDE.md's dated `2.1.233`
  measurement says none arrived, the two disagree and no pytest test can
  settle it. What holds either way is what these tests prove — a monitor
  spawned with none of them registers, because `client.py` resolves its own
  paths from `__file__` and `Path.home()`. Which names the build under test
  actually exports is measured by the release checklist's environ step, and is
  input to #28.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from bin import shared

from tests import waiting

REPO = Path(__file__).resolve().parent.parent
BIN_DIR = REPO / "skills" / "talk" / "bin"

# `read_line` waits for a line that must arrive. This is the opposite: the
# deadline a line must *not* beat, for the assertions that stdout stayed empty.
QUIET = 1.0


def _monitor_command(plugin_root: Path, *, root=None,
                     quote_root: bool = False) -> str:
    """The shipped monitor command with the token substituted, as a string.

    String-substituted exactly as CC does it, then `python3` swapped for the
    interpreter running the suite — see the module docstring for why both.
    The result is still a *shell command line*, because that is what CC hands
    its monitor runner; callers that want argv use `_monitor_argv`.

    `root` substitutes a root other than the one the manifest was read from
    (the #16 shape: a manifest that is fine and a root that is not).
    `quote_root` double-quotes the expansion, the spelling the plugins-reference's
    own monitor example uses and the one #53 adopts.

    Substituting into the raw string rather than post-editing the result
    matters: `sys.executable` here lives *under* the repo root, so a
    `.replace(str(REPO), …)` on the finished command rewrites the interpreter
    path too and the test ends up measuring a missing Python.
    """
    monitors = json.loads(
        (Path(plugin_root) / "monitors" / "monitors.json").read_text())
    assert len(monitors) == 1, monitors
    target = str(root if root is not None else plugin_root)
    expansion = f'"{target}"' if quote_root else target
    command = monitors[0]["command"].replace("${CLAUDE_PLUGIN_ROOT}", expansion)
    assert command.startswith("python3 "), command
    return shlex.quote(sys.executable) + command[len("python3"):]


def _monitor_argv(plugin_root: Path) -> list[str]:
    return shlex.split(_monitor_command(plugin_root))


def _clean_env(*, data_dir, port, ppid, home=None, extra=None) -> dict:
    """The child environment: everything the monitor needs and nothing else.

    `HUBBUB_DATA_DIR` is mandatory rather than convenient — without it a
    substituted-root spawn elects a real server against
    `~/.claude/data/hubbub/` on port 9473, i.e. against the developer's live
    bus. `HUBBUB_PORT` is the documented route for the same reason `#28`
    exists: monitors.json carries no `--port` and `CLAUDE_PLUGIN_OPTION_PORT`
    never arrives, so a harness that set the plugin-option spelling would pass
    while testing a delivery path that does not exist.

    `coverage_env()` is spliced in because this dict is built from scratch:
    `COVERAGE_PROCESS_START` would otherwise never reach the child and
    `client.py`'s `--from-monitor` branches would read as uncovered.
    """
    env = {
        "PATH": "/usr/bin:/bin",
        "HUBBUB_NO_REEXEC": "1",
        "HUBBUB_DATA_DIR": str(data_dir),
        "HUBBUB_PORT": str(port),
        "HUBBUB_PPID_OVERRIDE": str(ppid),
        **waiting.coverage_env(),
    }
    if home is not None:
        env["HOME"] = str(home)
    if extra:
        env.update(extra)
    return env


def _spawn_monitor_command(plugin_root, *, cwd, ppid, port, data_dir,
                           extra_env=None, home=None,
                           via_shell=False, command=None) -> subprocess.Popen:
    """Spawn the substituted monitors.json command.

    `via_shell` runs it through `/bin/sh -c` the way CC's monitor runner
    executes a command line, which is the only way the unquoted-expansion
    question (#53) can be asked at all. `command` overrides the string, for
    the two space-in-the-root cases that need a different quoting.
    """
    env = _clean_env(data_dir=data_dir, port=port, ppid=ppid, home=home,
                     extra=extra_env)
    if via_shell:
        argv = ["/bin/sh", "-c", command or _monitor_command(plugin_root)]
    else:
        argv = shlex.split(command) if command else _monitor_argv(plugin_root)
    return subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, cwd=str(cwd), env=env,
    )


def _run_helper(script, *args, data_dir, port, ppid, cwd=None, bin_dir=None,
                timeout=15.0) -> subprocess.CompletedProcess:
    """A short-lived control CLI under the same listener key as its monitor.

    The override has to be on `send.py`/`list.py` too: they discover their
    owning session through `resolve_listener_key`, so a helper that computed a
    different key would find no `.session` and report `not connected` against a
    monitor sitting right there.
    """
    return subprocess.run(
        [sys.executable, str((bin_dir or BIN_DIR) / script), *args],
        capture_output=True, text=True, timeout=timeout,
        cwd=str(cwd or REPO),
        env=_clean_env(data_dir=data_dir, port=port, ppid=ppid),
    )


def _reap(*procs) -> None:
    for proc in procs:
        if proc is None:
            continue
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        except OSError:
            pass


def _kill_server(data_dir: Path) -> None:
    """Kill the server this test's client elected.

    Endpoint-scoped pidfile names (`server.<port>.pid`), globbed — the same
    shape as `tests/test_helpers.py::_kill_server`. Never `pkill -f
    bin/server.py`: that reaches the developer's real monitors in other
    sessions.
    """
    if not Path(data_dir).exists():
        return
    for pid_path in Path(data_dir).glob("server.*.pid"):
        try:
            os.kill(int(pid_path.read_text().strip()), signal.SIGKILL)
        except (OSError, ValueError):
            pass


def _session_path(data_dir, ppid) -> Path:
    return Path(data_dir) / "clients" / f"{ppid}.session"


def _await_session(data_dir, ppid, timeout=waiting.DEFAULT_TIMEOUT) -> dict:
    """Wait for a registered `.session` and return it.

    Asserts rather than returning None: a silent None turns "the monitor never
    registered" into a confusing failure fifteen seconds later, at whichever
    assertion happens to read the name.
    """
    path = _session_path(data_dir, ppid)

    def _registered():
        return path.exists() and json.loads(path.read_text()).get("session_id")

    assert waiting.wait_for(_registered, timeout=timeout), (
        f"no {path} after {timeout}s — the monitor never registered")
    return json.loads(path.read_text())


@pytest.fixture
def fake_install(tmp_path, copy_plugin_root) -> Path:
    """A copy of the shipped plugin root, taken exactly as shipped.

    `when: "always"` and the command carrying `--from-monitor`, because the
    point of this file is to spawn the command that ships. The copy exists so
    `auto_start.py --off` has something it may write to: run against the
    checkout it would either hit `_git_worktree_root`'s refusal or mutate the
    tracked manifest (the `.NOTPARALLEL` reason in the Makefile).

    Tests that only *read* `monitors.json` use `REPO` directly instead — a
    copy proves less there, not more.
    """
    return copy_plugin_root(tmp_path / "plugin")


@pytest.mark.slow
class TestMonitorCommand:
    """The spawn path nobody invokes: `when: "always"` means this command runs
    in every session on the machine, before the user has typed anything."""

    def test_registers_from_monitors_json(self, tmp_data_dir, free_port, tmp_path):
        cwd = tmp_path / "proj-a"
        cwd.mkdir()
        proc = _spawn_monitor_command(REPO, cwd=cwd, ppid=50001, port=free_port,
                                      data_dir=tmp_data_dir)
        try:
            state = _await_session(tmp_data_dir, 50001)
            assert state["name"] == "proj-a", state
            assert proc.poll() is None, "the monitor exited after registering"
            # The `no --name given; auto-named …` notice is housekeeping, so
            # `--from-monitor` routes it to stderr. Anything on stdout here
            # would be a notification in a session whose user has never used
            # hubbub.
            assert waiting.read_line(proc, timeout=QUIET) == ""
            r = _run_helper("list.py", "--self", data_dir=tmp_data_dir,
                            port=free_port, ppid=50001, cwd=cwd)
            assert r.returncode == 0, r.stdout + r.stderr
            assert "name=proj-a" in r.stdout, r.stdout + r.stderr
        finally:
            _reap(proc)
            _kill_server(tmp_data_dir)

    def test_optout_file_exits_before_registering(self, tmp_data_dir, free_port,
                                                  tmp_path):
        """The durable opt-out, from the monitor's side.

        `tests/test_auto_start.py::TestDurableOptOut` covers this from
        `--status`'s side — that the flag is written and reported. This is the
        other half: that a spawned monitor honours it, silently, and exits.
        """
        cwd = tmp_path / "proj-a"
        cwd.mkdir()
        tmp_data_dir.mkdir(parents=True, exist_ok=True)
        (tmp_data_dir / "autostart-off").touch()
        proc = _spawn_monitor_command(REPO, cwd=cwd, ppid=50001, port=free_port,
                                      data_dir=tmp_data_dir)
        try:
            assert proc.wait(timeout=waiting.DEFAULT_TIMEOUT) == 0
            assert not _session_path(tmp_data_dir, 50001).exists()
            assert proc.stdout.read() == ""
        finally:
            _reap(proc)
            _kill_server(tmp_data_dir)

    def test_port_squatter_is_loud_even_when_auto_started(self, tmp_data_dir,
                                                          free_port, tmp_path):
        """The one fault `_print_unless_auto`'s docstring says must never be
        quiet: with `when: "always"` the user never runs connect, so stderr is
        somewhere nobody looks, and silencing this means a squatted port
        harvesting the bearer token produces no signal anywhere.

        No existing test asserts it on a spawned `--from-monitor` process.
        """
        squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        squatter.bind(("127.0.0.1", free_port))
        squatter.listen(5)
        cwd = tmp_path / "proj-a"
        cwd.mkdir()
        proc = _spawn_monitor_command(REPO, cwd=cwd, ppid=50001, port=free_port,
                                      data_dir=tmp_data_dir)
        try:
            line = waiting.read_line(proc)
            assert line.startswith("[hubbub] server identity check failed"), line
            assert "refusing to connect" in line, line
        finally:
            _reap(proc)
            squatter.close()
            _kill_server(tmp_data_dir)


@pytest.mark.slow
class TestPluginRootSubstitution:
    """What the substitution result has to survive: a root that is wrong, and
    a root that contains a space."""

    def test_nonexistent_root_spawns_nothing(self, tmp_data_dir, free_port,
                                             tmp_path):
        """#16's shape, at the monitor rather than at `auto_start.py`.

        This is the silent-dead-monitor failure written down: no exception
        anyone sees, no log line, just a session quietly not on the bus. The
        interpreter's exit 2 lands in the monitor's output file, which nobody
        opens.
        """
        bogus = tmp_path / "nonexistent"
        command = _monitor_command(REPO, root=bogus)
        cwd = tmp_path / "proj-a"
        cwd.mkdir()
        r = subprocess.run(
            shlex.split(command), capture_output=True, text=True, timeout=15,
            cwd=str(cwd),
            env=_clean_env(data_dir=tmp_data_dir, port=free_port, ppid=50001),
        )
        assert r.returncode == 2, r.stdout + r.stderr
        assert "can't open file" in r.stderr, r.stderr
        assert "No such file or directory" in r.stderr, r.stderr
        assert not (tmp_data_dir / "clients").exists()

    def _space_root(self, tmp_path, copy_plugin_root) -> Path:
        root = tmp_path / "with space" / "plugin"
        root.mkdir(parents=True)
        return copy_plugin_root(root)

    def test_quoted_root_with_a_space_registers(self, tmp_data_dir, free_port,
                                                tmp_path, copy_plugin_root):
        """The control for the xfail below: the space itself is fine.

        Quoting the expansion is the spelling the plugins-reference's own
        monitor example uses (`cd "${CLAUDE_PLUGIN_ROOT}" && …`) and the one
        #53 adopts. Run through `/bin/sh -c`, the way CC's monitor runner
        executes a command line — via argv the question cannot be asked.
        """
        root = self._space_root(tmp_path, copy_plugin_root)
        # cwd is the root itself, so the auto-name is its basename: `plugin`
        # either way, which keeps the assertion about the *spawn* rather than
        # about whichever temp directory pytest handed this test.
        proc = _spawn_monitor_command(
            root, cwd=root, ppid=50001, port=free_port, data_dir=tmp_data_dir,
            via_shell=True, command=_monitor_command(root, quote_root=True))
        try:
            assert _await_session(tmp_data_dir, 50001)["name"] == "plugin"
        finally:
            _reap(proc)
            _kill_server(tmp_data_dir)

    @pytest.mark.xfail(
        strict=True,
        reason="#53: monitors.json does not quote ${CLAUDE_PLUGIN_ROOT}, so a "
               "plugin root containing a space word-splits and the monitor "
               "dies with exit 2 before anything hubbub-shaped runs",
    )
    def test_shipped_root_with_a_space_registers(self, tmp_data_dir, free_port,
                                                 tmp_path, copy_plugin_root):
        """The command exactly as shipped, against a root with a space.

        Asserts the *fixed* outcome, so it is XFAIL today and flips
        red-as-XPASS the moment #53 quotes the token — at which point the
        marker comes off rather than the assertion changing. Today the shell
        word-splits at the space and the interpreter reports `can't open file
        '…/with'`.
        """
        root = self._space_root(tmp_path, copy_plugin_root)
        proc = _spawn_monitor_command(root, cwd=root, ppid=50001,
                                      port=free_port, data_dir=tmp_data_dir,
                                      via_shell=True)
        try:
            assert _await_session(tmp_data_dir, 50001, timeout=8.0)["name"] == "plugin"
        finally:
            _reap(proc)
            _kill_server(tmp_data_dir)


@pytest.mark.slow
class TestReexecBootstrap:
    """`client.py:11-23`: production spawns a bare `python3` and relies on the
    bootstrap to `os.execv` into `~/.claude/data/hubbub/venv/bin/python` — a
    path built from `Path.home()`, *not* from `HUBBUB_DATA_DIR`. Six
    entry-points carry a byte-identical copy of it and nothing tested any of
    them.

    Every other child in this file keeps `HUBBUB_NO_REEXEC=1`. These two are
    the only place it is off, and the fake venv is what stops the re-exec
    reaching the developer's real one.
    """

    def _wrapper_home(self, tmp_path: Path) -> Path:
        home = tmp_path / "home"
        venv_bin = home / ".claude" / "data" / "hubbub" / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        wrapper = venv_bin / "python"
        # `HUBBUB_NO_REEXEC=1` on the exec line is what stops the re-exec'd
        # child re-exec'ing forever: its `sys.prefix` is still the real
        # interpreter's, so the bootstrap's `sys.prefix != venv` test would
        # stay true on every hop. The env passes through to the exec'd
        # interpreter, so `make coverage`'s hook still reaches it.
        wrapper.write_text(
            "#!/bin/sh\n"
            f'echo "$@" >> {shlex.quote(str(tmp_path / "reexec.log"))}\n'
            f'HUBBUB_NO_REEXEC=1 exec {shlex.quote(sys.executable)} "$@"\n'
        )
        wrapper.chmod(0o755)
        return home

    def test_bare_spawn_reexecs_into_the_runtime_venv(self, tmp_data_dir,
                                                      free_port, tmp_path):
        home = self._wrapper_home(tmp_path)
        cwd = tmp_path / "proj-a"
        cwd.mkdir()
        env = _clean_env(data_dir=tmp_data_dir, port=free_port, ppid=50001,
                         home=home)
        del env["HUBBUB_NO_REEXEC"]
        proc = subprocess.Popen(
            _monitor_argv(REPO), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(cwd), env=env,
        )
        try:
            assert _await_session(tmp_data_dir, 50001)["name"] == "proj-a"
            log = (tmp_path / "reexec.log").read_text().splitlines()
            # Exactly one: the server child spawned under the wrapper inherits
            # HUBBUB_NO_REEXEC=1 from the wrapper's exec env, so it does not
            # add a second line.
            assert log == [f"{BIN_DIR / 'client.py'} --from-monitor"], log
        finally:
            _reap(proc)
            _kill_server(tmp_data_dir)

    def test_no_reexec_env_is_honoured(self, tmp_data_dir, free_port, tmp_path):
        """The opt-out the whole suite depends on: `tests/conftest.py` sets it
        process-wide, and without it every subprocess test would silently run
        under the developer's runtime venv instead of the one under test."""
        home = self._wrapper_home(tmp_path)
        cwd = tmp_path / "proj-a"
        cwd.mkdir()
        proc = _spawn_monitor_command(REPO, cwd=cwd, ppid=50001, port=free_port,
                                      data_dir=tmp_data_dir, home=home)
        try:
            assert _await_session(tmp_data_dir, 50001)["name"] == "proj-a"
            assert not (tmp_path / "reexec.log").exists()
        finally:
            _reap(proc)
            _kill_server(tmp_data_dir)


@pytest.mark.slow
class TestDeliveryBudget:
    """What a session literally receives when a message is over the stdout cap.

    `TestFormatMsg` pins the same arithmetic in-process. This pins it through
    a real spawned monitor, which is where the `cont` pointer's path and the
    `messages.log` record it points at can actually be checked.
    """

    def _two_monitors(self, tmp_data_dir, free_port, tmp_path):
        cwd_a, cwd_b = tmp_path / "alpha", tmp_path / "beta"
        cwd_a.mkdir()
        cwd_b.mkdir()
        proc_a = _spawn_monitor_command(REPO, cwd=cwd_a, ppid=50001,
                                        port=free_port, data_dir=tmp_data_dir)
        _await_session(tmp_data_dir, 50001)
        proc_b = _spawn_monitor_command(REPO, cwd=cwd_b, ppid=50002,
                                        port=free_port, data_dir=tmp_data_dir)
        _await_session(tmp_data_dir, 50002)
        return proc_a, proc_b

    def test_oversize_message_arrives_as_two_lines(self, tmp_data_dir,
                                                   free_port, tmp_path):
        proc_a = proc_b = None
        try:
            proc_a, proc_b = self._two_monitors(tmp_data_dir, free_port, tmp_path)
            text = "x" * 5000
            r = _run_helper("send.py", "--to", "beta", "--text", text,
                            data_dir=tmp_data_dir, port=free_port, ppid=50001)
            assert r.returncode == 0, r.stdout + r.stderr

            line1 = waiting.read_line(proc_b)
            assert line1.startswith("[hubbub msg="), line1
            assert 'from="alpha"' in line1, line1
            assert "truncated=5000]" in line1, line1
            # Literals, deliberately, not `shared.STDOUT_CAP` /
            # `shared.NOTIFICATION_CLIP`. A test that reads the constant it is
            # pinning *follows* a change to it instead of catching one:
            # dropping STDOUT_CAP to 399 leaves a symbol-reading version of
            # these two tests green, which was checked rather than assumed.
            # The numbers are #38's measurement (Claude Code 2.1.270) and the
            # reason this file exists.
            assert shared.utf16_len(line1) <= 500, shared.utf16_len(line1)
            body = line1.split("] ", 1)[1]
            assert len(body) == 400, len(body)

            msg_id = line1.split("msg=", 1)[1].split()[0]
            line2 = waiting.read_line(proc_b)
            assert line2 == (
                f"[hubbub msg={msg_id} cont] full text 5000 chars at "
                f"{tmp_data_dir / 'messages.log'}"
            ), line2

            # The pointer has to point at something: SKILL.md tells the agent
            # to `grep -F <msg_id>` this file for the full payload.
            records = [
                json.loads(ln)
                for ln in (tmp_data_dir / "messages.log").read_text().splitlines()
                if f'"{msg_id}"' in ln
            ]
            matching = [rec for rec in records if rec.get("msg_id") == msg_id]
            assert len(matching) == 1, matching
            assert matching[0]["text"] == text
        finally:
            _reap(proc_a, proc_b)
            _kill_server(tmp_data_dir)

    def test_message_at_the_cap_is_one_line(self, tmp_data_dir, free_port,
                                            tmp_path):
        """Exactly at `STDOUT_CAP`: no `truncated=`, and no `cont` line.

        An off-by-one here is the expensive kind — a `cont` pointer at the cap
        would tell the agent to go read a log for text it already has, on
        every message of exactly that length.
        """
        proc_a = proc_b = None
        try:
            proc_a, proc_b = self._two_monitors(tmp_data_dir, free_port, tmp_path)
            # 400, the literal, for the reason given in the test above.
            text = "y" * 400
            r = _run_helper("send.py", "--to", "beta", "--text", text,
                            data_dir=tmp_data_dir, port=free_port, ppid=50001)
            assert r.returncode == 0, r.stdout + r.stderr

            line1 = waiting.read_line(proc_b)
            assert "truncated=" not in line1, line1
            assert line1.endswith(text), line1
            assert waiting.read_line(proc_b, timeout=QUIET) == ""
        finally:
            _reap(proc_a, proc_b)
            _kill_server(tmp_data_dir)


@pytest.mark.slow
class TestSameCwd:
    """Two sessions opened in one repo. The name comes from the cwd basename
    (monitors.json passes no `--name`), so this is the routine case, not the
    exotic one."""

    def test_second_session_is_suffixed_and_reachable(self, tmp_data_dir,
                                                      free_port, tmp_path):
        cwd = tmp_path / "proj-a"
        cwd.mkdir()
        proc_a = proc_b = None
        try:
            proc_a = _spawn_monitor_command(REPO, cwd=cwd, ppid=50001,
                                            port=free_port, data_dir=tmp_data_dir)
            # Second only after the first has registered: starting both at
            # once would race the server election, which is
            # `tests/test_helpers.py::TestElection`'s subject, not this one.
            _await_session(tmp_data_dir, 50001)
            proc_b = _spawn_monitor_command(REPO, cwd=cwd, ppid=50002,
                                            port=free_port, data_dir=tmp_data_dir)
            state_b = _await_session(tmp_data_dir, 50002)
            assert state_b["name"] == "proj-a-2", state_b
            assert json.loads(
                _session_path(tmp_data_dir, 50001).read_text())["name"] == "proj-a"
            assert proc_a.poll() is None and proc_b.poll() is None

            r = _run_helper("send.py", "--to", "proj-a-2", "--text", "hello",
                            data_dir=tmp_data_dir, port=free_port, ppid=50001)
            assert r.returncode == 0, r.stdout + r.stderr
            line = waiting.read_line(proc_b)
            assert 'from="proj-a"' in line, line
            assert line.endswith(" hello"), line
            # The rename notice is housekeeping — a normal outcome, not a
            # fault — so `--from-monitor` keeps it off the notification
            # channel.
            assert "taken; using" not in line
        finally:
            _reap(proc_a, proc_b)
            _kill_server(tmp_data_dir)

    def test_exhausted_budget_is_loud_even_when_auto_started(
            self, tmp_data_dir, free_port, tmp_path):
        """Exhaustion stays on stdout while the retry notice does not: the
        retry reports one machine-wide fact N times, exhaustion is *this*
        session failing to join at all, and silence would leave it quietly
        missing from `list` with nothing to explain it.

        `HUBBUB_MAX_COLLISION_RETRIES=0` is the only deterministic way to get
        here — otherwise it takes four sessions racing one cwd-derived name
        and the outcome depends on their interleaving.
        """
        cwd = tmp_path / "proj-a"
        cwd.mkdir()
        proc_a = proc_b = None
        try:
            proc_a = _spawn_monitor_command(REPO, cwd=cwd, ppid=50001,
                                            port=free_port, data_dir=tmp_data_dir)
            _await_session(tmp_data_dir, 50001)
            proc_b = _spawn_monitor_command(
                REPO, cwd=cwd, ppid=50002, port=free_port, data_dir=tmp_data_dir,
                extra_env={"HUBBUB_MAX_COLLISION_RETRIES": "0"})
            line = waiting.read_line(proc_b)
            assert line == (
                "[hubbub] name 'proj-a' taken after 0 retries; "
                "run /hubbub:talk connect <other-name>"
            ), line
            assert proc_b.wait(timeout=waiting.DEFAULT_TIMEOUT) == 0
            assert not _session_path(tmp_data_dir, 50002).exists()
        finally:
            _reap(proc_a, proc_b)
            _kill_server(tmp_data_dir)


@pytest.mark.slow
class TestExternalKill:
    """A monitor that dies outside its own control flow — `TaskStop`, a
    reboot, an OOM kill. The session's state file is what every helper CLI
    reads, so what it says afterwards decides whether the agent can recover."""

    def test_sigterm_then_respawn_reconnects(self, tmp_data_dir, free_port,
                                             tmp_path):
        cwd = tmp_path / "proj-a"
        cwd.mkdir()
        proc = proc2 = None
        try:
            proc = _spawn_monitor_command(REPO, cwd=cwd, ppid=50001,
                                          port=free_port, data_dir=tmp_data_dir)
            first = _await_session(tmp_data_dir, 50001)
            os.kill(first["listener_pid"], signal.SIGTERM)
            assert proc.wait(timeout=8) == 0

            r = _run_helper("list.py", "--self", data_dir=tmp_data_dir,
                            port=free_port, ppid=50001, cwd=cwd)
            assert r.returncode == 0, r.stdout + r.stderr
            # atexit deleted the state file, so this is the clean spelling —
            # not the `(stale state cleaned up)` one below.
            assert r.stdout.strip() == "not connected", r.stdout

            proc2 = _spawn_monitor_command(REPO, cwd=cwd, ppid=50001,
                                           port=free_port, data_dir=tmp_data_dir)
            second = _await_session(tmp_data_dir, 50001)
            assert second["session_id"] != first["session_id"]
        finally:
            _reap(proc, proc2)
            _kill_server(tmp_data_dir)

    def test_sigkill_leaves_stale_state_that_self_cleans(self, tmp_data_dir,
                                                         free_port, tmp_path):
        cwd = tmp_path / "proj-a"
        cwd.mkdir()
        proc = None
        try:
            proc = _spawn_monitor_command(REPO, cwd=cwd, ppid=50001,
                                          port=free_port, data_dir=tmp_data_dir)
            state = _await_session(tmp_data_dir, 50001)
            os.kill(state["listener_pid"], signal.SIGKILL)
            # Reap it: a zombie still satisfies `pid_exists`, so without this
            # the staleness check would see a live listener and the test would
            # be measuring pytest's failure to wait().
            proc.wait(timeout=8)
            assert _session_path(tmp_data_dir, 50001).exists(), \
                "SIGKILL should bypass atexit and leave the file behind"

            r = _run_helper("list.py", "--self", data_dir=tmp_data_dir,
                            port=free_port, ppid=50001, cwd=cwd)
            assert r.returncode == 0, r.stdout + r.stderr
            assert r.stdout.strip() == "not connected (stale state cleaned up)", \
                r.stdout
            assert not _session_path(tmp_data_dir, 50001).exists()
        finally:
            _reap(proc)
            _kill_server(tmp_data_dir)


@pytest.mark.slow
class TestAutoStartOptOut:
    """#22, from the monitor's side. `auto_start.py` is the one process here
    that legitimately receives `CLAUDE_PLUGIN_ROOT` as an env var — it honours
    it as an explicit override — and it is set for that subprocess only."""

    def _auto_start(self, args, root, data_dir, extra_env=None):
        env = {
            "PATH": "/usr/bin:/bin",
            "HUBBUB_NO_REEXEC": "1",
            "HUBBUB_DATA_DIR": str(data_dir),
            "CLAUDE_PLUGIN_ROOT": str(root),
            **waiting.coverage_env(),
        }
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(root / "skills" / "talk" / "bin" / "auto_start.py"),
             *args],
            capture_output=True, text=True, env=env, timeout=30,
        )

    def test_optout_survives_a_simulated_plugin_update(self, fake_install,
                                                       tmp_data_dir, free_port,
                                                       tmp_path):
        """`/plugin update` ships `when: "always"` again. The durable flag in
        the data dir is what makes the opt-out survive it — and the monitor,
        not the manifest, is where that has to hold."""
        off = self._auto_start(["--off"], fake_install, tmp_data_dir)
        assert off.returncode == 0, off.stdout + off.stderr
        assert (tmp_data_dir / "autostart-off").exists()

        manifest = fake_install / "monitors" / "monitors.json"
        data = json.loads(manifest.read_text())
        data[0]["when"] = "always"
        manifest.write_text(json.dumps(data, indent=2) + "\n")

        cwd = tmp_path / "proj-a"
        cwd.mkdir()
        proc = _spawn_monitor_command(fake_install, cwd=cwd, ppid=50001,
                                      port=free_port, data_dir=tmp_data_dir)
        try:
            assert proc.wait(timeout=waiting.DEFAULT_TIMEOUT) == 0
            assert not _session_path(tmp_data_dir, 50001).exists()
            assert proc.stdout.read() == ""
        finally:
            _reap(proc)
            _kill_server(tmp_data_dir)

        status = self._auto_start(["--status"], fake_install, tmp_data_dir)
        assert "OFF (forced" in status.stdout, status.stdout
        assert "opt-out:" in status.stdout, status.stdout
        assert "wins" in status.stdout, status.stdout

    def test_on_is_refused_while_env_says_off(self, fake_install, tmp_data_dir,
                                              free_port, tmp_path):
        """Turning it back on while the environment says off would produce a
        manifest that claims one thing and a monitor that does another."""
        self._auto_start(["--off"], fake_install, tmp_data_dir)
        on = self._auto_start(["--on"], fake_install, tmp_data_dir,
                              extra_env={"HUBBUB_AUTO_START": "0"})
        assert on.returncode == 1, on.stdout + on.stderr
        assert "NOT applied — HUBBUB_AUTO_START is set to a false value" \
            in on.stdout, on.stdout
        assert (tmp_data_dir / "autostart-off").exists()

        cwd = tmp_path / "proj-a"
        cwd.mkdir()
        proc = _spawn_monitor_command(fake_install, cwd=cwd, ppid=50001,
                                      port=free_port, data_dir=tmp_data_dir)
        try:
            assert proc.wait(timeout=waiting.DEFAULT_TIMEOUT) == 0
            assert not _session_path(tmp_data_dir, 50001).exists()
        finally:
            _reap(proc)
            _kill_server(tmp_data_dir)


@pytest.mark.slow
class TestStandaloneSkill:
    """The second supported install mode: `skills/talk/` copied (or symlinked)
    to `~/.claude/skills/talk`, with no plugin manifest anywhere.

    Copied rather than symlinked on purpose — a symlink would resolve back
    into the checkout and prove nothing about a self-contained skill
    directory. The connect step is `client.py --name solo`, which is what
    `/talk connect` issues via `Monitor()`; no `--from-monitor`, because the
    user asked for this one.
    """

    @pytest.fixture
    def install(self, tmp_path) -> Path:
        dst = tmp_path / "install" / "skills" / "talk"
        shutil.copytree(REPO / "skills" / "talk", dst,
                        ignore=shutil.ignore_patterns("__pycache__"))
        return dst

    def test_copied_skill_connects_without_a_manifest(self, install,
                                                      tmp_data_dir, free_port,
                                                      tmp_path):
        proc = subprocess.Popen(
            [sys.executable, str(install / "bin" / "client.py"), "--name", "solo"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, cwd=str(tmp_path),
            env=_clean_env(data_dir=tmp_data_dir, port=free_port, ppid=50001),
        )
        try:
            assert _await_session(tmp_data_dir, 50001)["name"] == "solo"
            r = _run_helper("list.py", "--self", data_dir=tmp_data_dir,
                            port=free_port, ppid=50001,
                            bin_dir=install / "bin")
            assert r.returncode == 0, r.stdout + r.stderr
            assert "name=solo" in r.stdout, r.stdout + r.stderr
        finally:
            _reap(proc)
            _kill_server(tmp_data_dir)

    def test_copied_skill_duplicate_connect_is_loud(self, install, tmp_data_dir,
                                                    free_port, tmp_path):
        """SKILL.md's connect step has no upfront dedup check on purpose: it
        picks a name and calls `Monitor()`, and the ppid flock is the single
        source of truth for the race. That only works if the second monitor
        says so on *stdout*, where the agent sees it.

        The other standalone negative — "nothing to configure" when there is
        no manifest — is not duplicated here; it stays
        `tests/test_auto_start.py::TestRootResolution::test_standalone_install_says_so_instead_of_hunting`.
        """
        first = second = None
        try:
            first = subprocess.Popen(
                [sys.executable, str(install / "bin" / "client.py"),
                 "--name", "solo"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, cwd=str(tmp_path),
                env=_clean_env(data_dir=tmp_data_dir, port=free_port, ppid=50001),
            )
            _await_session(tmp_data_dir, 50001)
            second = subprocess.Popen(
                [sys.executable, str(install / "bin" / "client.py"),
                 "--name", "solo"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, cwd=str(tmp_path),
                env=_clean_env(data_dir=tmp_data_dir, port=free_port, ppid=50001),
            )
            line = waiting.read_line(second)
            assert line.startswith(
                "[hubbub] another monitor for this session is already running"
            ), line
            assert second.wait(timeout=waiting.DEFAULT_TIMEOUT) == 0
        finally:
            _reap(first, second)
            _kill_server(tmp_data_dir)
