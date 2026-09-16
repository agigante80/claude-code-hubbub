import json
import os
import shutil
import socket
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "talk"
# Put the skill dir first so `from bin import ...` resolves to
# <skill-dir>/bin/, where the runtime now lives.
sys.path.insert(0, str(SKILL_DIR))

# Tests run with the dev environment's Python (which has websockets/psutil
# installed via requirements-dev.txt). Disable the entry-point bootstrap
# that would re-exec under ~/.claude/data/hubbub/venv if one exists —
# that venv is for the user's runtime, not for tests.
os.environ["HUBBUB_NO_REEXEC"] = "1"


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    d = tmp_path / "hubbub"
    monkeypatch.setenv("HUBBUB_DATA_DIR", str(d))
    # Legacy spelling stays cleared so a stray export in the developer's
    # shell can't leak a real data dir into a test.
    monkeypatch.delenv("INTER_SESSION_DATA_DIR", raising=False)
    return d


@pytest.fixture
def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def copy_plugin_root():
    """A callable that materialises a *real* plugin root under a temp dir.

    Returns `copy_plugin_root(dst, *, when=None) -> Path`, which copies
    `.claude-plugin/plugin.json`, the **real** `monitors/monitors.json` and
    `skills/talk/` into `dst`, rewriting the monitor entry's `when` only when
    asked.

    One fixture shape, two callers with different needs:

    - `tests/test_cc_harness.py::fake_install` takes it as shipped — `when:
      "always"` and the command carrying `--from-monitor` — because the whole
      point there is to spawn *the command CC would spawn*. A synthetic
      command string would test a command that ships nowhere.
    - `tests/test_auto_start.py::fake_plugin_root` takes it with
      `when="on-skill-invoke:talk"`, because two of its tests
      (`test_lazy_default`, `test_off_writes_lazy`) assert on that starting
      value. It previously wrote a synthetic command *without*
      `--from-monitor`; the copy fixes that without changing an assertion.

    Copying rather than pointing at the checkout is load-bearing: any
    `auto_start.py --off` run against the real root would either hit
    `_git_worktree_root`'s refusal or mutate the tracked manifest (the reason
    for `.NOTPARALLEL` in the Makefile).
    """
    def _copy(dst, *, when: str | None = None) -> Path:
        dst = Path(dst)
        (dst / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / ".claude-plugin" / "plugin.json",
                     dst / ".claude-plugin" / "plugin.json")
        (dst / "monitors").mkdir(parents=True, exist_ok=True)
        monitors = json.loads((REPO_ROOT / "monitors" / "monitors.json").read_text())
        if when is not None:
            for entry in monitors:
                entry["when"] = when
        (dst / "monitors" / "monitors.json").write_text(
            json.dumps(monitors, indent=2) + "\n")
        skill_dst = dst / "skills" / "talk"
        if not skill_dst.exists():
            # __pycache__ would be copied as a stale byte-code shadow of the
            # scripts we are about to run under a different interpreter.
            shutil.copytree(SKILL_DIR, skill_dst,
                            ignore=shutil.ignore_patterns("__pycache__"))
        return dst
    return _copy


@pytest.fixture(autouse=True)
def _reset_migration_flag():
    """`shared._unmigrated_this_run` is process-global and written by
    `migrate_legacy_data_dir()`. A test that drives it True leaves it set for
    the rest of the run, and any later test that clears both *_DATA_DIR vars
    and calls `data_dir()` then resolves against the developer's real $HOME —
    passing today only because this machine's legacy path is already a symlink.
    """
    from bin import shared
    shared._unmigrated_this_run = False
    yield
    shared._unmigrated_this_run = False


def pytest_addoption(parser):
    parser.addoption(
        "--allow-skips", action="store_true", default=False,
        help="Permit skipped tests. Without this the run fails if any test "
             "was skipped — see pytest_sessionfinish for why.",
    )


def pytest_sessionfinish(session, exitstatus):
    """Fail the run if any test was skipped.

    A skip is invisible in a green summary: `495 passed, 1 skipped` reads as
    success, and the one that did not run is the one nobody looks at. The two
    conditional skips in this suite are guarded on `os.geteuid() == 0` — they
    verify that an unreadable data directory is *reported* rather than
    crashing the diagnostic, and root bypasses DAC so `chmod 000` denies it
    nothing. CI containers commonly run as root, so precisely those two would
    have vanished in the environment that matters most.

    Making the run red instead means a skip has to be a decision. Pass
    `--allow-skips` when you genuinely intend one (an OS-specific test on the
    wrong OS, say) — and prefer running the suite as a non-root user, which
    is what makes the count zero here.
    """
    if session.config.getoption("--allow-skips"):
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return
    skipped = reporter.stats.get("skipped", [])
    if not skipped:
        return
    reporter.write_sep("=", "skipped tests are not allowed", red=True)
    for rep in skipped:
        where = getattr(rep, "nodeid", "?")
        reason = ""
        if isinstance(getattr(rep, "longrepr", None), tuple) and len(rep.longrepr) == 3:
            reason = rep.longrepr[2]
        reporter.write_line(f"  {where}  {reason}")
    reporter.write_line(
        "Re-run as a non-root user, or pass --allow-skips if the skip is "
        "intended."
    )
    session.exitstatus = 1
