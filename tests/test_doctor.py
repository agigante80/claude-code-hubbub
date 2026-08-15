"""Tests for bin/doctor.py — the read-only data-dir diagnostic (fork #15)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "talk" / "bin" / "doctor.py"


def _run(home: Path, port: int = 59999) -> subprocess.CompletedProcess:
    """Runs against a fake HOME so the real data dir is never touched, and with
    no HUBBUB_DATA_DIR override — the whole point is exercising the default
    legacy/new resolution."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--port", str(port)],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HUBBUB_NO_REEXEC": "1",
             "HOME": str(home), "PYTHONPATH": str(REPO / "skills" / "talk")},
    )


def _mkhome(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".claude" / "data").mkdir(parents=True)
    return home


class TestHealthy:
    def test_migrated_install_is_healthy(self, tmp_path: Path):
        home = _mkhome(tmp_path)
        data = home / ".claude" / "data"
        (data / "hubbub").mkdir()
        (data / "hubbub" / ".migrated-from-inter-session").touch()
        (data / "hubbub" / "token").write_text("t")
        (data / "inter-session").symlink_to(data / "hubbub")
        r = _run(home)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "healthy" in r.stdout
        assert "marker: present" in r.stdout


class TestForkDetection:
    """The failure this command exists for: two real directories with
    different tokens. Both halves work, neither can see the other, and nothing
    anywhere reports an error."""

    def _forked(self, tmp_path: Path) -> Path:
        home = _mkhome(tmp_path)
        data = home / ".claude" / "data"
        for name, tok in (("hubbub", "NEW"), ("inter-session", "OLD")):
            (data / name).mkdir()
            (data / name / "token").write_text(tok)
        return home

    def test_fork_is_reported_and_exits_nonzero(self, tmp_path: Path):
        r = _run(self._forked(tmp_path))
        assert r.returncode == 1, r.stdout
        assert "NEEDS ATTENTION" in r.stdout
        assert "FORKED" in r.stdout

    def test_fork_report_names_both_sides(self, tmp_path: Path):
        """A user has to choose which side to keep, so the output must give
        them the basis for that choice rather than just announcing a problem."""
        r = _run(self._forked(tmp_path))
        assert "legacy" in r.stdout and "new" in r.stdout
        assert "token mtime" in r.stdout
        assert "live listeners" in r.stdout

    def test_does_not_offer_to_repair_automatically(self, tmp_path: Path):
        """Deliberate scope limit. Choosing a side ends the other side's bus
        and cannot be undone; this command must not imply it will do that."""
        r = _run(self._forked(tmp_path))
        assert "not automated" in r.stdout

    def test_same_token_is_not_a_fork(self, tmp_path: Path):
        """Two real directories sharing a token means the migration is
        incomplete, not that the bus is split — a much milder finding."""
        home = _mkhome(tmp_path)
        data = home / ".claude" / "data"
        for name in ("hubbub", "inter-session"):
            (data / name).mkdir()
            (data / name / "token").write_text("SAME")
        r = _run(home)
        assert "FORKED" not in r.stdout
        assert "share a token" in r.stdout


class TestBrokenLegacyLink:
    def test_dangling_legacy_symlink_is_a_problem(self, tmp_path: Path):
        """Old builds hardcode the legacy path, so a link that leads nowhere
        breaks them while new builds carry on — asymmetric and confusing."""
        home = _mkhome(tmp_path)
        data = home / ".claude" / "data"
        (data / "hubbub").mkdir()
        (data / "inter-session").symlink_to(data / "gone")
        r = _run(home)
        assert r.returncode == 1
        assert "leads nowhere" in r.stdout

    def test_symlink_loop_does_not_crash(self, tmp_path: Path):
        """The shape that took down all five entry-points on 3.12 (#19). A
        diagnostic that dies on the broken case is worthless."""
        home = _mkhome(tmp_path)
        data = home / ".claude" / "data"
        (data / "hubbub").mkdir()
        (data / "inter-session").symlink_to(data / "inter-session")
        r = _run(home)
        assert "Traceback" not in r.stderr, r.stderr
        assert "unresolvable" in r.stdout or "leads nowhere" in r.stdout


class TestUnreadableLegacy:
    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores mode 000")
    def test_unreadable_legacy_dir_is_reported(self, tmp_path: Path):
        """`is_dir()` is True on a permission-denied directory, so we route
        state there and every entry-point dies with an uncaught PermissionError
        out of ensure_token. Reproduced while reviewing #19; the fix belongs
        here rather than as a third special case in `_still_on_legacy`."""
        home = _mkhome(tmp_path)
        data = home / ".claude" / "data"
        (data / "hubbub").mkdir()
        legacy = data / "inter-session"
        legacy.mkdir()
        (legacy / "token").write_text("OLD")
        os.chmod(legacy, 0o000)
        try:
            r = _run(home)
            assert "cannot be read" in r.stdout, r.stdout
            assert r.returncode == 1
        finally:
            os.chmod(legacy, 0o755)


class TestPortReporting:
    def test_no_listener_is_stated_plainly(self, tmp_path: Path):
        r = _run(_mkhome(tmp_path), port=59998)
        assert "nothing is listening" in r.stdout

    def test_listener_without_pidfile_is_flagged(self, tmp_path: Path):
        """The pre-flock identity-wipe shape: a live listener whose pidfile the
        losing server's cleanup deleted. Every client then refuses to connect
        and the bus is down with zero clients — and until now the only way to
        recognise it was to know the story."""
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            r = _run(_mkhome(tmp_path), port=port)
            assert "NO pidfile" in r.stdout
            assert "identity-wipe" in r.stdout
            assert r.returncode == 1
        finally:
            srv.close()


class TestDoctorSurvivesTheStatesItReports:
    """A diagnostic that crashes on the broken case is worse than none. This
    class exists because the first version did exactly that: `Path.glob` on an
    unreadable directory raises PermissionError on 3.12 and returns empty on
    3.14, so `make test` was green and `make test-system` was not (#24)."""

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores mode 000")
    def test_unreadable_live_dir_does_not_traceback(self, tmp_path: Path):
        home = _mkhome(tmp_path)
        data = home / ".claude" / "data"
        (data / "hubbub").mkdir()
        legacy = data / "inter-session"
        legacy.mkdir()
        (legacy / "token").write_text("OLD")
        os.chmod(legacy, 0o000)
        try:
            r = _run(home)
            assert "Traceback" not in r.stderr, r.stderr
            assert r.returncode == 1
            assert "NEEDS ATTENTION" in r.stdout
        finally:
            os.chmod(legacy, 0o755)

    def test_missing_data_dir_entirely_does_not_traceback(self, tmp_path: Path):
        home = tmp_path / "bare"
        (home / ".claude").mkdir(parents=True)
        r = _run(home)
        assert "Traceback" not in r.stderr, r.stderr
        assert "hubbub doctor:" in r.stdout


class TestEndpointResolution:
    """fork #29 review, HIGH. Hardcoded defaults meant doctor probed 9473 on an
    install configured for anything else — reporting "healthy" exit 0 without
    ever looking at the real bus, for a command whose whole job is diagnosing a
    bus that behaves impossibly."""

    def _run_env(self, home: Path, extra: dict) -> subprocess.CompletedProcess:
        env = {"PATH": "/usr/bin:/bin", "HUBBUB_NO_REEXEC": "1",
               "HOME": str(home),
               "PYTHONPATH": str(REPO / "skills" / "talk")}
        env.update(extra)
        return subprocess.run([sys.executable, str(SCRIPT)],
                              capture_output=True, text=True, env=env)

    def test_hubbub_port_is_honoured(self, tmp_path: Path):
        r = self._run_env(_mkhome(tmp_path), {"HUBBUB_PORT": "59321"})
        assert "port 127.0.0.1:59321" in r.stdout, r.stdout

    def test_plugin_option_port_wins(self, tmp_path: Path):
        r = self._run_env(_mkhome(tmp_path),
                          {"CLAUDE_PLUGIN_OPTION_PORT": "59322",
                           "HUBBUB_PORT": "59323"})
        assert "port 127.0.0.1:59322" in r.stdout, r.stdout

    def test_legacy_spelling_is_honoured(self, tmp_path: Path):
        r = self._run_env(_mkhome(tmp_path),
                          {"INTER_SESSION_PORT": "59324"})
        assert "port 127.0.0.1:59324" in r.stdout, r.stdout

    def test_unparseable_port_falls_back(self, tmp_path: Path):
        r = self._run_env(_mkhome(tmp_path), {"HUBBUB_PORT": "banana"})
        assert "port 127.0.0.1:9473" in r.stdout, r.stdout


class TestReadOnly:
    """fork #29 review. Four places promised read-only while `main()` opened by
    running a migration that renames directories, drains live entries, creates
    symlinks and parks remainders — so a user inspecting an unmigrated split
    had it migrated out from under them, then shown the post-mutation state."""

    def test_does_not_migrate_an_unmigrated_install(self, tmp_path: Path):
        home = _mkhome(tmp_path)
        data = home / ".claude" / "data"
        legacy = data / "inter-session"
        legacy.mkdir()
        (legacy / "token").write_text("LIVE")
        before = sorted(p.name for p in data.iterdir())
        r = _run(home)
        after = sorted(p.name for p in data.iterdir())
        assert after == before, (
            f"doctor mutated the data dir it was asked to inspect: "
            f"{before} -> {after}"
        )
        assert legacy.is_dir() and not legacy.is_symlink()
        assert (legacy / "token").read_text() == "LIVE"
        # And it must still report the truth: live state is on the legacy path.
        assert str(legacy) in r.stdout

    def test_reports_the_live_dir_as_legacy_when_unmigrated(self, tmp_path: Path):
        home = _mkhome(tmp_path)
        data = home / ".claude" / "data"
        (data / "inter-session").mkdir()
        (data / "inter-session" / "token").write_text("LIVE")
        r = _run(home)
        line = [l for l in r.stdout.splitlines() if "live data dir" in l]
        assert line and "inter-session" in line[0], r.stdout


class TestUnusablePathShapes:
    def test_a_regular_file_at_the_legacy_path_is_a_problem(self, tmp_path: Path):
        """Nothing downstream handles this — the fork branch finds no token,
        the dangling branch needs a symlink, the unreadable probe needs a
        directory — so it reported healthy while old builds that hardcode the
        path could not start and the migration had no branch to complete."""
        home = _mkhome(tmp_path)
        data = home / ".claude" / "data"
        (data / "hubbub").mkdir()
        (data / "inter-session").write_text("not a directory")
        r = _run(home)
        assert r.returncode == 1, r.stdout
        assert "not a directory" in r.stdout

    def test_symlink_loop_is_named_as_a_loop(self, tmp_path: Path):
        """Distinguished via ELOOP rather than inferred from resolve() failing:
        that only raises on 3.12, so on 3.14 a loop was reported as "leads
        nowhere" while the docs claimed the two were told apart."""
        home = _mkhome(tmp_path)
        data = home / ".claude" / "data"
        (data / "hubbub").mkdir()
        (data / "inter-session").symlink_to(data / "inter-session")
        r = _run(home)
        assert "a loop" in r.stdout, r.stdout
        assert r.returncode == 1
