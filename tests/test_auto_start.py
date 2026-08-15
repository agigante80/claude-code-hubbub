"""Tests for bin/auto_start.py — the /hubbub:talk auto-start helper."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "talk" / "bin" / "auto_start.py"
ALWAYS = "always"
LAZY = "on-skill-invoke:talk"


@pytest.fixture
def fake_plugin_root(tmp_path: Path) -> Path:
    monitors_dir = tmp_path / "monitors"
    monitors_dir.mkdir()
    (monitors_dir / "monitors.json").write_text(json.dumps([
        {
            "name": "hubbub-client",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/skills/talk/bin/client.py",
            "description": "hubbub messages",
            "when": LAZY,
        }
    ], indent=2) + "\n")
    return tmp_path


def _run(args: list[str], plugin_root: Path | None,
         data_dir: Path | None = None,
         extra_env: dict | None = None) -> subprocess.CompletedProcess:
    # auto_start now mirrors the setting into the data dir (so a plugin update
    # can't silently undo an opt-out), so every run needs one of its own or it
    # would reach into the developer's real ~/.claude/data.
    if data_dir is None:
        # Under the plugin root (the tmp_path fixture) so pytest reclaims it —
        # a bare mkdtemp() leaked one directory per call. Unique per call as
        # well: sharing one dir across calls in a test would silently carry the
        # durable autostart-off flag between them.
        if plugin_root is None:
            raise AssertionError(
                "pass data_dir when plugin_root is None; a bare mkdtemp here "
                "is never reclaimed"
            )
        data_dir = Path(tempfile.mkdtemp(dir=str(plugin_root))) / "data"
    env = {"PATH": "/usr/bin:/bin", "HUBBUB_NO_REEXEC": "1",
           "HUBBUB_DATA_DIR": str(data_dir)}
    if plugin_root is not None:
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, env=env,
    )


class TestStatus:
    def test_lazy_default(self, fake_plugin_root: Path):
        r = _run(["--status"], fake_plugin_root)
        assert r.returncode == 0
        assert "OFF" in r.stdout
        assert LAZY in r.stdout

    def test_after_set_on(self, fake_plugin_root: Path):
        m = fake_plugin_root / "monitors" / "monitors.json"
        data = json.loads(m.read_text())
        data[0]["when"] = ALWAYS
        m.write_text(json.dumps(data) + "\n")
        r = _run(["--status"], fake_plugin_root)
        assert "ON" in r.stdout

    def test_custom_value(self, fake_plugin_root: Path):
        m = fake_plugin_root / "monitors" / "monitors.json"
        data = json.loads(m.read_text())
        data[0]["when"] = "on-skill-invoke:other-skill"
        m.write_text(json.dumps(data) + "\n")
        r = _run(["--status"], fake_plugin_root)
        assert "CUSTOM" in r.stdout


class TestSet:
    def test_on_writes_always(self, fake_plugin_root: Path):
        r = _run(["--on"], fake_plugin_root)
        assert r.returncode == 0
        data = json.loads((fake_plugin_root / "monitors" / "monitors.json").read_text())
        assert data[0]["when"] == ALWAYS

    def test_off_writes_lazy(self, fake_plugin_root: Path):
        # First flip to ON so we can verify OFF is a real change
        _run(["--on"], fake_plugin_root)
        r = _run(["--off"], fake_plugin_root)
        assert r.returncode == 0
        data = json.loads((fake_plugin_root / "monitors" / "monitors.json").read_text())
        assert data[0]["when"] == LAZY

    def test_no_change_when_already_target(self, fake_plugin_root: Path, tmp_path: Path):
        # Twice: the first run still creates the durable flag (a real change),
        # so "no change" is only true once both halves already agree.
        data_dir = tmp_path / "data"
        _run(["--off"], fake_plugin_root, data_dir=data_dir)
        r = _run(["--off"], fake_plugin_root, data_dir=data_dir)
        assert r.returncode == 0
        assert "no change" in r.stdout
        # File contents preserved
        data = json.loads((fake_plugin_root / "monitors" / "monitors.json").read_text())
        assert data[0]["when"] == LAZY

    def test_preserves_other_monitor_fields(self, fake_plugin_root: Path):
        m = fake_plugin_root / "monitors" / "monitors.json"
        before = json.loads(m.read_text())[0]
        _run(["--on"], fake_plugin_root)
        after = json.loads(m.read_text())[0]
        for k in ("name", "command", "description"):
            assert before[k] == after[k]

    def test_reload_instruction_printed(self, fake_plugin_root: Path):
        r = _run(["--on"], fake_plugin_root)
        assert "/reload-plugins" in r.stdout or "new Claude Code session" in r.stdout


class TestErrors:
    def test_no_env_falls_back_to_script_relative(self, tmp_path: Path):
        """Without CLAUDE_PLUGIN_ROOT, the script self-locates from
        __file__ (bin/auto_start.py → repo root → monitors/monitors.json).
        It should succeed against the real repo."""
        r = _run(["--status"], plugin_root=None, data_dir=tmp_path / "data")
        assert r.returncode == 0
        assert "auto-start:" in r.stdout

    def test_env_root_takes_precedence(self, fake_plugin_root: Path):
        """When CLAUDE_PLUGIN_ROOT IS set, prefer it over script-relative."""
        m = fake_plugin_root / "monitors" / "monitors.json"
        data = json.loads(m.read_text())
        data[0]["when"] = ALWAYS  # set the fake to ALWAYS
        m.write_text(json.dumps(data) + "\n")
        r = _run(["--status"], fake_plugin_root)
        # Should reflect the fake's value, not the real repo's.
        assert "ON" in r.stdout

    def test_missing_monitor_entry(self, fake_plugin_root: Path):
        m = fake_plugin_root / "monitors" / "monitors.json"
        m.write_text(json.dumps([{"name": "other-monitor", "when": "always"}]))
        r = _run(["--status"], fake_plugin_root)
        assert r.returncode == 2
        assert "hubbub-client" in r.stderr

    def test_requires_one_of(self, fake_plugin_root: Path):
        # No flags → argparse should fail (mutually exclusive group, required=True)
        r = _run([], fake_plugin_root)
        assert r.returncode != 0


class TestDurableOptOut:
    """`auto-start off` expresses itself by rewriting `when` in the plugin's
    own monitors.json, which `/plugin update` overwrites with the shipped file.
    Now that the shipped default is `always`, an update would silently hand
    always-on monitors back to a user who turned them off — so the setting is
    mirrored into the data dir, which updates cannot touch."""

    def test_off_writes_the_optout(self, fake_plugin_root: Path, tmp_path: Path):
        data = tmp_path / "data"
        r = _run(["--off"], fake_plugin_root, data_dir=data)
        assert r.returncode == 0, r.stderr
        assert (data / "autostart-off").exists()

    def test_on_clears_the_optout(self, fake_plugin_root: Path, tmp_path: Path):
        data = tmp_path / "data"
        _run(["--off"], fake_plugin_root, data_dir=data)
        r = _run(["--on"], fake_plugin_root, data_dir=data)
        assert r.returncode == 0, r.stderr
        assert not (data / "autostart-off").exists()

    def test_off_is_reasserted_when_when_already_matches(
            self, fake_plugin_root: Path, tmp_path: Path):
        """A plugin update restores the shipped `when` but leaves the data dir
        alone, so the two can disagree. `--off` must converge them even when
        `when` already matches — and must not call that "no change"."""
        data = tmp_path / "data"
        r = _run(["--off"], fake_plugin_root, data_dir=data)   # fixture ships LAZY
        assert r.returncode == 0, r.stderr
        assert (data / "autostart-off").exists()
        assert "no change" not in r.stdout

    def test_status_flags_an_update_that_undid_the_optout(
            self, fake_plugin_root: Path, tmp_path: Path):
        data = tmp_path / "data"
        _run(["--off"], fake_plugin_root, data_dir=data)
        _run(["--on"], fake_plugin_root, data_dir=data)
        # Simulate `/plugin update`: shipped `always` returns, opt-out restored
        # by the user's earlier choice.
        (data / "autostart-off").parent.mkdir(parents=True, exist_ok=True)
        (data / "autostart-off").touch()
        r = _run(["--status"], fake_plugin_root, data_dir=data)
        assert "opt-out" in r.stdout
        assert "wins" in r.stdout


class TestEnvAutoStartOff:
    """fork #22, after the userConfig route was found inert. The env vars are
    the only switch besides the opt-out file that actually reaches a monitor,
    so they are the ones this command must not hide."""

    OFF = {"HUBBUB_AUTO_START": "false"}

    def test_status_names_the_variable(
            self, fake_plugin_root: Path, tmp_path: Path):
        r = _run(["--status"], fake_plugin_root, data_dir=tmp_path / "d",
                 extra_env=self.OFF)
        assert "HUBBUB_AUTO_START" in r.stdout
        # The headline must not claim ON while a later line says otherwise;
        # the headline is the part that gets summarized.
        assert "ON  (" not in r.stdout

    def test_on_refuses_before_touching_state(
            self, fake_plugin_root: Path, tmp_path: Path):
        """The ordering matters more than the message. Detecting this after
        `_set_optout(False)` would mean a command reporting "nothing applied"
        had in fact deleted the user's durable opt-out."""
        data = tmp_path / "d"
        _run(["--off"], fake_plugin_root, data_dir=data)
        assert (data / "autostart-off").exists()
        r = _run(["--on"], fake_plugin_root, data_dir=data, extra_env=self.OFF)
        assert "NOT applied" in r.stdout
        assert r.returncode == 1
        assert (data / "autostart-off").exists(), (
            "the opt-out was destroyed by a command that said it did nothing"
        )

    def test_absent_env_is_not_read_as_off(
            self, fake_plugin_root: Path, tmp_path: Path):
        r = _run(["--on"], fake_plugin_root, data_dir=tmp_path / "d")
        assert "NOT applied" not in r.stdout

    def test_precedence_matches_the_client_not_just_the_key_list(
            self, fake_plugin_root: Path, tmp_path: Path):
        """Mirroring `client.py`'s key *list* is not enough; the resolution
        rule has to match too.

        `_env_bool` returns on the first **parseable** key, so a truthy
        `HUBBUB_AUTO_START` shadows a falsey legacy `INTER_SESSION_AUTO_START`
        and the monitor stays up. Scanning for the first *falsey* key instead
        disagreed: it reported `OFF (forced)` and refused `auto-start on`,
        which is precisely the upgrader's case — a stale legacy export in the
        profile plus the new spelling from the current docs — and it left no
        in-product way to clear the durable opt-out.
        """
        r = _run(["--on"], fake_plugin_root, data_dir=tmp_path / "d",
                 extra_env={"HUBBUB_AUTO_START": "true",
                            "INTER_SESSION_AUTO_START": "false"})
        assert "NOT applied" not in r.stdout, (
            "refused an --on for a monitor that would actually have started"
        )

    def test_legacy_key_still_decides_when_new_one_is_absent(
            self, fake_plugin_root: Path, tmp_path: Path):
        r = _run(["--status"], fake_plugin_root, data_dir=tmp_path / "d",
                 extra_env={"INTER_SESSION_AUTO_START": "false"})
        assert "INTER_SESSION_AUTO_START" in r.stdout

    def test_plugin_option_is_ignored(
            self, fake_plugin_root: Path, tmp_path: Path):
        """CC never injects CLAUDE_PLUGIN_OPTION_* into a monitor, so acting
        on it here would report a state the monitor does not share."""
        r = _run(["--on"], fake_plugin_root, data_dir=tmp_path / "d",
                 extra_env={"CLAUDE_PLUGIN_OPTION_AUTO_START": "false"})
        assert "NOT applied" not in r.stdout



class TestRootResolution:
    """fork #16. Two ways this used to reach the wrong `monitors.json`."""

    def test_bogus_explicit_root_fails_instead_of_falling_through(
            self, tmp_path: Path):
        """The one that actually bit: during the 0.2.0 review, `--off` with
        CLAUDE_PLUGIN_ROOT pointing at a non-existent path fell through to the
        script-relative candidate and rewrote **this repo's own**
        monitors/monitors.json; the tree had to be restored with git checkout.

        Note a plugin-marker check would not have prevented it — the repo is a
        perfectly valid hubbub plugin root. The fix is that an explicit
        override is authoritative: honour it or fail, never substitute a guess.
        """
        before = (REPO / "monitors" / "monitors.json").read_text()
        r = _run(["--off"], plugin_root=tmp_path / "does-not-exist",
                 data_dir=tmp_path / "data")
        assert r.returncode == 2
        assert "does not exist" in r.stderr
        assert (REPO / "monitors" / "monitors.json").read_text() == before, (
            "the repo's own manifest was modified by a run pointed elsewhere"
        )

    def test_inferred_root_must_carry_our_plugin_manifest(self, tmp_path: Path):
        """A standalone-skill install lives at ~/.claude/skills/talk/bin/, and
        four parents up from there is ~/.claude — so an unrelated
        ~/.claude/monitors/monitors.json would be loaded and edited. Simulated
        by planting a foreign manifest at the same relative position."""
        root = tmp_path / "not-hubbub"
        (root / "monitors").mkdir(parents=True)
        (root / "monitors" / "monitors.json").write_text(json.dumps(
            [{"name": "hubbub-client", "when": "always"}]
        ))
        skill_bin = root / "skills" / "talk" / "bin"
        skill_bin.mkdir(parents=True)
        shutil.copy(SCRIPT, skill_bin / "auto_start.py")
        shutil.copytree(REPO / "skills" / "talk" / "bin",
                        skill_bin, dirs_exist_ok=True)
        before = (root / "monitors" / "monitors.json").read_text()
        r = subprocess.run(
            [sys.executable, str(skill_bin / "auto_start.py"), "--off"],
            capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "HUBBUB_NO_REEXEC": "1",
                 "HUBBUB_DATA_DIR": str(tmp_path / "data")},
        )
        assert r.returncode == 2, r.stdout
        assert "not a hubbub plugin root" in r.stderr
        assert (root / "monitors" / "monitors.json").read_text() == before

    def test_standalone_install_says_so_instead_of_hunting(self, tmp_path: Path):
        """No manifest anywhere: the answer is "there is nothing to
        configure", not a search that might find someone else's file."""
        skill_bin = tmp_path / "install" / "skills" / "talk" / "bin"
        skill_bin.mkdir(parents=True)
        shutil.copytree(REPO / "skills" / "talk" / "bin",
                        skill_bin, dirs_exist_ok=True)
        r = subprocess.run(
            [sys.executable, str(skill_bin / "auto_start.py"), "--status"],
            capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "HUBBUB_NO_REEXEC": "1",
                 "HUBBUB_DATA_DIR": str(tmp_path / "data")},
        )
        assert r.returncode == 2
        assert "standalone skill install" in r.stderr
        assert "nothing to configure" in r.stderr


class TestPartialFailure:
    """The two halves — the plugin manifest and the durable data-dir flag —
    must be independent. Ordering them only chooses which one silently gets
    dropped when the other fails."""

    def test_missing_manifest_does_not_touch_durable_state(self, tmp_path: Path):
        """A standalone-skill copy has no monitors.json and governs no plugin
        monitor, so it must not reach over and flip the flag a plugin install
        reads. `--on` doing so would re-enable the plugin's always-on monitor
        and then exit 2, reporting failure.

        The copy is real rather than a bogus CLAUDE_PLUGIN_ROOT. That used to
        be a *workaround*: resolution fell back to walking up from the script,
        so pointing the env var at nothing landed on this repo's own
        monitors.json. Since fork #16 an explicit root is honoured or fatal,
        so the workaround is no longer required — but the copy is kept because
        it exercises the real standalone layout, which the env var never can.
        `TestRootResolution` covers the bogus-env-var path directly.
        """
        standalone = tmp_path / "install" / "skills" / "talk"
        standalone.parent.mkdir(parents=True)
        shutil.copytree(REPO / "skills" / "talk", standalone)
        data = tmp_path / "data"
        data.mkdir()
        (data / "autostart-off").touch()
        r = subprocess.run(
            [sys.executable, str(standalone / "bin" / "auto_start.py"), "--on"],
            capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "HUBBUB_NO_REEXEC": "1",
                 "HUBBUB_DATA_DIR": str(data)},
        )
        assert r.returncode == 2, r.stdout
        assert (data / "autostart-off").exists(), (
            "durable opt-out was cleared by a command that reported failure"
        )

    def test_unwritable_manifest_still_records_the_optout(
            self, fake_plugin_root: Path, tmp_path: Path):
        """The durable flag is the half that survives /plugin update, so a
        read-only plugin dir must not cost us the setting."""
        data = tmp_path / "data"
        monitors = fake_plugin_root / "monitors"
        monitors.chmod(0o500)
        try:
            r = _run(["--off"], fake_plugin_root, data_dir=data)
        finally:
            monitors.chmod(0o700)
        assert (data / "autostart-off").exists()
        # Off is in force via the flag alone, so this is a success.
        assert r.returncode == 0, r.stderr

    def test_unwritable_data_dir_still_applies_the_manifest(
            self, fake_plugin_root: Path, tmp_path: Path):
        """Mirror-write failure must not cost us the manifest edit, which is
        what CC actually reads at session open."""
        m = fake_plugin_root / "monitors" / "monitors.json"
        entries = json.loads(m.read_text())
        entries[0]["when"] = ALWAYS
        m.write_text(json.dumps(entries, indent=2) + "\n")
        blocked = tmp_path / "blocked"
        blocked.mkdir(mode=0o500)
        try:
            r = _run(["--off"], fake_plugin_root, data_dir=blocked / "data")
            when = json.loads(m.read_text())[0]["when"]
        finally:
            blocked.chmod(0o700)
        assert when == LAZY, "manifest edit was skipped because the mirror failed"
        assert "could not update" in r.stderr
        # Off is genuinely in force through `when` alone, so this succeeded.
        assert r.returncode == 0

    def test_reconcile_is_not_reported_as_no_change(
            self, fake_plugin_root: Path, tmp_path: Path):
        """Post-upgrade shape: monitors.json ships `always`, the durable
        opt-out is still there. `--on` really does change state."""
        data = tmp_path / "data"
        data.mkdir()
        (data / "autostart-off").touch()
        m = fake_plugin_root / "monitors" / "monitors.json"
        entries = json.loads(m.read_text())
        entries[0]["when"] = ALWAYS
        m.write_text(json.dumps(entries, indent=2) + "\n")
        r = _run(["--on"], fake_plugin_root, data_dir=data)
        assert r.returncode == 0, r.stderr
        assert "no change" not in r.stdout
        assert not (data / "autostart-off").exists()


class TestFailureReporting:
    """SKILL.md tells the agent to surface this command's stdout, so a message
    that mis-names which half failed is a message that sends the user to fix
    the wrong thing. Both mis-attributions below shipped once."""

    def test_names_only_the_half_that_failed(self, fake_plugin_root: Path, tmp_path: Path):
        """Manifest unwritable, opt-out already correct. Reporting 'neither
        could be written' was wrong — the saved setting was fine."""
        monitors = fake_plugin_root / "monitors"
        monitors.chmod(0o500)
        try:
            r = _run(["--on"], fake_plugin_root, data_dir=tmp_path / "d")
        finally:
            monitors.chmod(0o700)
        assert "NOT applied" in r.stdout, r.stdout
        assert "the plugin manifest" in r.stdout
        assert "the saved setting" not in r.stdout
        assert r.returncode == 1

    def test_credits_the_half_that_carries_the_setting(
            self, fake_plugin_root: Path, tmp_path: Path):
        """`off` holds through the manifest alone when the mirror can't be
        written — but it must not claim the mirror is what carries it. The
        fixture already ships LAZY, so `when` needs no write and the mirror is
        the only half that moves."""
        blocked = tmp_path / "blocked"
        blocked.mkdir(mode=0o500)
        try:
            r = _run(["--off"], fake_plugin_root, data_dir=blocked / "d")
        finally:
            blocked.chmod(0o700)
        assert "in effect via the plugin manifest" in r.stdout, r.stdout
        assert "could not write the saved setting" in r.stdout
        assert r.returncode == 0

    def test_reports_when_nothing_landed(self, fake_plugin_root: Path, tmp_path: Path):
        # `when` must differ from the target, or no manifest write is attempted
        # and the manifest half cannot fail.
        m = fake_plugin_root / "monitors" / "monitors.json"
        entries = json.loads(m.read_text())
        entries[0]["when"] = ALWAYS
        m.write_text(json.dumps(entries, indent=2) + "\n")
        monitors = fake_plugin_root / "monitors"
        blocked = tmp_path / "blocked"
        blocked.mkdir(mode=0o500)
        monitors.chmod(0o500)
        try:
            r = _run(["--off"], fake_plugin_root, data_dir=blocked / "d")
        finally:
            monitors.chmod(0o700)
            blocked.chmod(0o700)
        assert "NOT applied" in r.stdout, r.stdout
        assert "the plugin manifest" in r.stdout
        assert "the saved setting" in r.stdout
        assert r.returncode == 1


class TestOnDoesNotDestroyTheOptOutOnFailure:
    """`--on` deletes the durable flag. Doing that before the manifest write
    meant a read-only plugin dir reported `NOT applied` while the user's
    opt-out was already gone — and the next `/plugin update` restores the
    shipped `always`, so the session goes always-on with nothing left
    recording that they turned it off."""

    def test_optout_survives_a_failed_manifest_write(
            self, fake_plugin_root: Path, tmp_path: Path):
        data = tmp_path / "data"
        data.mkdir()
        (data / "autostart-off").touch()
        monitors = fake_plugin_root / "monitors"
        monitors.chmod(0o500)
        try:
            r = _run(["--on"], fake_plugin_root, data_dir=data)
        finally:
            monitors.chmod(0o700)
        assert r.returncode == 1, r.stdout
        assert "NOT applied" in r.stdout
        assert (data / "autostart-off").exists(), (
            "durable opt-out destroyed by a command that reported failure"
        )

    def test_optout_is_cleared_when_the_manifest_lands(
            self, fake_plugin_root: Path, tmp_path: Path):
        data = tmp_path / "data"
        data.mkdir()
        (data / "autostart-off").touch()
        r = _run(["--on"], fake_plugin_root, data_dir=data)
        assert r.returncode == 0, r.stderr
        assert not (data / "autostart-off").exists()


class TestFailureMessageNamesOnlyRealFailures:
    def test_untouched_optout_is_not_reported_as_failed(
            self, fake_plugin_root: Path, tmp_path: Path):
        """`--on` with an existing opt-out and an unwritable manifest never
        attempts the opt-out write — so saying it could not be written, and
        pointing at a stderr line that was never printed, is wrong."""
        data = tmp_path / "data"
        data.mkdir()
        (data / "autostart-off").touch()
        monitors = fake_plugin_root / "monitors"
        monitors.chmod(0o500)
        try:
            r = _run(["--on"], fake_plugin_root, data_dir=data)
        finally:
            monitors.chmod(0o700)
        assert "the plugin manifest" in r.stdout, r.stdout
        assert "the saved setting" not in r.stdout
        assert "NOT applied" in r.stdout
        assert (data / "autostart-off").exists()
