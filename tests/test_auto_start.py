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
         data_dir: Path | None = None) -> subprocess.CompletedProcess:
    # auto_start now mirrors the setting into the data dir (so a plugin update
    # can't silently undo an opt-out), so every run needs one of its own or it
    # would reach into the developer's real ~/.claude/data.
    env = {"PATH": "/usr/bin:/bin", "HUBBUB_NO_REEXEC": "1",
           "HUBBUB_DATA_DIR": str(data_dir if data_dir is not None
                                  else Path(tempfile.mkdtemp()) / "data")}
    if plugin_root is not None:
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
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
    def test_no_env_falls_back_to_script_relative(self):
        """Without CLAUDE_PLUGIN_ROOT, the script self-locates from
        __file__ (bin/auto_start.py → repo root → monitors/monitors.json).
        It should succeed against the real repo."""
        r = _run(["--status"], plugin_root=None)
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


class TestPartialFailure:
    """The two halves — the plugin manifest and the durable data-dir flag —
    must be independent. Ordering them only chooses which one silently gets
    dropped when the other fails."""

    def test_missing_manifest_does_not_touch_durable_state(self, tmp_path: Path):
        """A standalone-skill copy has no monitors.json and governs no plugin
        monitor, so it must not reach over and flip the flag a plugin install
        reads. `--on` doing so would re-enable the plugin's always-on monitor
        and then exit 2, reporting failure.

        The copy is real, not a bogus CLAUDE_PLUGIN_ROOT: resolution falls back
        to walking up from the script, so pointing the env var at nothing just
        lands on this repo's own monitors.json.
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
